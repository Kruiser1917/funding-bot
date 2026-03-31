# -*- coding: utf-8 -*-
"""Paper Trading симулятор поверх SQLite."""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from config import Config
from src.database import Database
from src.funding import get_funding_rate, get_spot_price

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Симулированная позиция (спот long + фьючерс short)."""
    symbol: str
    entry_price: float
    size_usd: float     # Номинал позиции с плечом
    margin_usd: float   # Заблокированная маржа
    size_coin: float
    open_time: str
    order_type: str = "maker"
    funding_earned: float = 0.0
    funding_count: int = 0
    commissions: float = 0.0
    
    # Виртуальные поля для возврата из методов, в БД не хранятся
    pnl: float = 0.0
    status: str = "open"


class PaperTrader:
    """Симулятор paper-trading портфеля на SQLite."""

    def __init__(self, capital: float | None = None):
        Config.ensure_dirs()
        self.db = Database()
        # Выполняем миграцию, если старый JSON еще существует
        self.db.migrate_from_json(Config.POSITIONS_FILE)
        
        # Если передан явный капитал (для тестов), обновляем стейт
        if capital:
            self.db.update_state({"balance": capital})

    # ── Helpers ──────────────────────────────────────────────

    def _find_open(self, symbol: str) -> dict | None:
        """Найти открытую позицию по символу."""
        return self.db.get_position(symbol)

    @property
    def balance(self) -> float:
        state = self.db.get_state()
        return round(state.get("balance", Config.SIMULATION_CAPITAL), 4)

    @property
    def initial_capital(self) -> float:
        return Config.SIMULATION_CAPITAL

    # ── Core methods ─────────────────────────────────────────

    def open_position(self, symbol: str, usd_amount: float, order_type: str = "maker") -> dict | None:
        """Открыть симулированную позицию."""
        if self._find_open(symbol):
            logger.warning("Позиция %s уже открыта", symbol)
            return None

        if usd_amount > self.balance:
            logger.warning("Недостаточно баланса: %.4f < %.4f", self.balance, usd_amount)
            return None

        price = get_spot_price(symbol)
        if not price:
            logger.error("Не удалось получить цену %s", symbol)
            return None

        # Кредитное плечо
        actual_size = usd_amount * Config.LEVERAGE
        
        # Комиссия входа (спот покупка + фьючерс шорт)
        fee_rate = Config.FEE_MAKER if order_type == "maker" else Config.FEE_TAKER
        commission = round(actual_size * fee_rate, 4)
        size_coin = round(actual_size / price, 8)

        pos_dict = {
            "symbol": symbol,
            "entry_price": price,
            "size_usd": actual_size,
            "margin_usd": usd_amount,
            "size_coin": size_coin,
            "open_time": datetime.now(timezone.utc).isoformat(),
            "commissions": commission,
            "order_type": order_type
        }

        # Обновляем баланс и сохраняем
        self.db.update_state({"balance": self.balance - usd_amount})
        self.db.add_position(pos_dict)
        
        logger.info("Открыта позиция %s: маржа $%.2f, номинал $%.2f (x%d) @ %.4f",
                    symbol, usd_amount, actual_size, Config.LEVERAGE, price)
        return Position(
            symbol=pos_dict["symbol"],
            entry_price=pos_dict["entry_price"],
            size_usd=pos_dict["size_usd"],
            margin_usd=pos_dict["margin_usd"],
            order_type=pos_dict["order_type"],
            size_coin=pos_dict["size_coin"],
            open_time=pos_dict["open_time"],
            commissions=pos_dict["commissions"],
        )

    def apply_funding(self) -> dict[str, float]:
        """Начислить funding rate для всех открытых позиций."""
        results: dict[str, float] = {}
        open_pos = self.db.get_open_positions()
        
        total_earned = 0.0
        
        for pos in open_pos:
            symbol = pos["symbol"]
            info = get_funding_rate(symbol)
            if not info:
                logger.warning("Не удалось получить rate для %s", symbol)
                continue

            rate = info["rate"]
            earned = round(pos["size_usd"] * rate, 4)
            
            # Обновляем позицию
            self.db.update_position(symbol, {
                "funding_earned": round(pos["funding_earned"] + earned, 4),
                "funding_count": pos["funding_count"] + 1
            })
            
            total_earned += earned
            results[symbol] = earned
            logger.info("Funding %s: rate=%.6f earned=$%.4f", symbol, rate, earned)

        if results:
            state = self.db.get_state()
            new_balance = state["balance"] + total_earned
            new_total = state.get("total_funding_earned", 0) + total_earned
            self.db.update_state({
                "balance": round(new_balance, 4),
                "total_funding_earned": round(new_total, 4)
            })

        return results

    def close_position(self, symbol: str) -> Position | None:
        """Закрыть симулированную позицию и зафиксировать PnL в State."""
        pos = self._find_open(symbol)
        if not pos:
            logger.warning("Нет открытой позиции %s", symbol)
            return None

        state = self.db.get_state()

        # Комиссия закрытия
        fee_rate = Config.FEE_MAKER if pos.get("order_type", "maker") == "maker" else Config.FEE_TAKER
        close_commission = round(pos["size_usd"] * fee_rate, 4)
        total_commission = round(pos["commissions"] + close_commission, 4)
        pnl = round(pos["funding_earned"] - total_commission, 4)

        # Возвращаем капитал (обратно маржу + pnl)
        margin_usd = pos.get("margin_usd", pos["size_usd"])
        new_balance = state["balance"] + margin_usd + pnl
        
        # Обновляем глобальную статистику
        self.db.update_state({
            "balance": round(new_balance, 4),
            "total_pnl": round(state.get("total_pnl", 0) + pnl, 4),
            "total_commissions": round(state.get("total_commissions", 0) + total_commission, 4),
            "closed_trades_count": state.get("closed_trades_count", 0) + 1
        })

        self.db.delete_position(symbol)
        logger.info("Закрыта позиция %s, PnL=$%.4f", symbol, pnl)
        
        # Возвращаем объект Position для нотификатора (он ждет .pnl)
        margin_usd = pos.get("margin_usd", pos["size_usd"])
        order_type = pos.get("order_type", "maker")
        return Position(
            symbol=symbol,
            entry_price=pos["entry_price"],
            size_usd=pos["size_usd"],
            margin_usd=margin_usd,
            order_type=order_type,
            size_coin=pos["size_coin"],
            open_time=pos["open_time"],
            funding_earned=pos["funding_earned"],
            funding_count=pos["funding_count"],
            commissions=total_commission,
            pnl=pnl,
            status="closed"
        )

    def summary(self) -> dict:
        """Сводка по портфелю из State и Positions."""
        state = self.db.get_state()
        open_positions = self.db.get_open_positions()

        # Аккумулируем открытые
        current_funding = sum(p["funding_earned"] for p in open_positions)
        current_commissions = sum(p["commissions"] for p in open_positions)

        total_funding = state.get("total_funding_earned", 0) + current_funding
        total_pnl = state.get("total_pnl", 0)
        total_commissions = state.get("total_commissions", 0) + current_commissions

        capital = self.initial_capital
        locked = sum(p.get("margin_usd", p["size_usd"]) for p in open_positions)
        portfolio_value = round(self.balance + locked, 4)
        roi_pct = round((portfolio_value - capital) / capital * 100, 4) if capital else 0

        # Дни работы
        created = state.get("created_at", datetime.now(timezone.utc).isoformat())
        start = datetime.fromisoformat(created)
        days = max((datetime.now(timezone.utc) - start).days, 1)

        # Прогнозы
        net_profit = portfolio_value - capital
        daily_avg = net_profit / days if days > 0 else 0
        projected_monthly = round(daily_avg * 30, 4)
        projected_annual = round(daily_avg * 365, 4)

        return {
            "capital": capital,
            "balance": self.balance,
            "portfolio_value": portfolio_value,
            "free_balance": round(self.balance, 4),
            "total_funding_earned": round(total_funding, 4),
            "total_pnl": round(total_pnl, 4),
            "total_commissions": round(total_commissions, 4),
            "roi_pct": roi_pct,
            "open_positions_count": len(open_positions),
            "closed_trades_count": state.get("closed_trades_count", 0),
            "days_running": days,
            "projected_monthly": projected_monthly,
            "projected_annual": projected_annual,
        }

    def get_position_detail(self, symbol: str) -> dict | None:
        """Детали позиции + текущий unrealized funding."""
        pos = self._find_open(symbol)
        if not pos:
            return None

        info = get_funding_rate(symbol)
        unrealized = 0.0
        if info:
            unrealized = round(pos["size_usd"] * info["rate"], 4)

        return {
            **pos,
            "unrealized_funding": unrealized,
            "current_rate_pct": info["rate_pct"] if info else 0,
        }

    def get_open_positions(self) -> list[dict]:
        """Список всех открытых позиций."""
        return self.db.get_open_positions()
