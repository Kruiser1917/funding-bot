# -*- coding: utf-8 -*-
"""Авто-стратегия: вход/выход на основе Funding Rate."""

import logging

from config import Config
from src.funding import get_all_rates, get_basis, get_funding_history, get_funding_rate, get_open_interest
from src.notifier import Notifier
from src.simulator import PaperTrader

logger = logging.getLogger(__name__)


def calc_position_size(annual_pct: float) -> float:
    """Рассчитать размер позиции по annual_pct.

    Линейная интерполяция: ENTER_THRESHOLD → POSITION_SIZE,
    RATE_CAP_FOR_SIZING → POSITION_SIZE_MAX.
    """
    if not Config.DYNAMIC_SIZING:
        return Config.POSITION_SIZE

    low = Config.ENTER_THRESHOLD
    high = Config.RATE_CAP_FOR_SIZING
    if high <= low:
        return Config.POSITION_SIZE

    t = min(max((annual_pct - low) / (high - low), 0.0), 1.0)
    size = Config.POSITION_SIZE + t * (Config.POSITION_SIZE_MAX - Config.POSITION_SIZE)
    return round(size, 2)


class AutoStrategy:
    """Автоматический вход/выход по funding rate."""

    def __init__(self, trader: PaperTrader, notifier: Notifier):
        self._trader = trader
        self._notifier = notifier

    def evaluate(self) -> dict:
        """Анализ рынка: какие позиции открыть/закрыть.

        Returns:
            {"to_open": [(symbol, reason)], "to_close": [(symbol, reason)]}
        """
        result = {"to_open": [], "to_close": []}
        open_positions = self._trader.get_open_positions()
        open_symbols = {p["symbol"] for p in open_positions}

        # ── Проверка на закрытие ──────────────────────────────
        for pos in open_positions:
            sym = pos["symbol"]
            info = get_funding_rate(sym)
            if not info:
                continue

            # Rate стал отрицательным → закрываем
            if info["annual_pct"] < Config.EXIT_THRESHOLD:
                result["to_close"].append(
                    (sym, f"rate отрицательный: {info['annual_pct']:.1f}% годовых")
                )
                continue

            # Слишком долго держим (>30 дней = 90 периодов)
            if pos["funding_count"] >= 90:
                result["to_close"].append(
                    (sym, f"достигнут лимит {pos['funding_count']} периодов")
                )

        # ── Сканируем рынок ──────────────────────────────────
        df = get_all_rates()
        if df.empty:
            return result

        # ── Динамическая адаптация стратегии (V5) ──
        top_20 = df.head(20)
        market_avg_annual = top_20["annual_pct"].mean()
        market_bullishness = (df["rate"] > 0).astype(int).mean() * 100

        # Динамический порог не может опуститься НИЖЕ ENTER_THRESHOLD (только выше)
        dynamic_enter = max(Config.ENTER_THRESHOLD, min(market_avg_annual * 0.8, 35.0))

        if market_bullishness > 70:
            dynamic_pos_ratio = max(Config.MIN_POSITIVE_RATIO - 5, 60.0)
        else:
            dynamic_pos_ratio = min(90.0, Config.MIN_POSITIVE_RATIO + 10)

        logger.info("Market Adapt: AvgTop20=%.1f%%, Bullish=%.0f%% -> Enter=%.1f%%, PosRatio=%.0f%%",
                    market_avg_annual, market_bullishness, dynamic_enter, dynamic_pos_ratio)

        # ── Rotation: найти худшую открытую позицию ──────────
        worst_pos = None
        worst_annual = float("inf")
        for pos in open_positions:
            sym = pos["symbol"]
            if sym in [s for s, _ in result["to_close"]]:
                continue  # уже планируем закрыть
            info = get_funding_rate(sym)
            if info:
                if info["annual_pct"] < worst_annual:
                    worst_annual = info["annual_pct"]
                    worst_pos = pos

        # ── Проверка на открытие ──────────────────────────────
        can_open = Config.MAX_POSITIONS - len(open_positions) + len(result["to_close"])
        free_balance = self._trader.balance

        for _, row in df.iterrows():
            sym = row["symbol"]
            annual = row["annual_pct"]

            if sym in open_symbols:
                continue

            logger.info("Scan %s: annual=%.1f%% (порог=%.1f%%)", sym, annual, dynamic_enter)

            if annual < dynamic_enter:
                continue

            # Проверка стабильности через историю
            hist = get_funding_history(sym, limit=90)
            if hist.empty:
                continue

            positive_ratio = hist.attrs.get("positive_ratio", 0)
            if positive_ratio < dynamic_pos_ratio:
                logger.info("Пропуск %s: positive_ratio=%.1f%% < %.1f%% (dynamic)",
                            sym, positive_ratio, dynamic_pos_ratio)
                continue

            # Liquidity Filter (OI + Volume)
            if Config.MIN_VOLUME_24H > 0 or Config.MIN_OI > 0:
                liq = get_open_interest(sym)
                if liq:
                    if liq["vol24h_usd"] < Config.MIN_VOLUME_24H:
                        logger.info("Пропуск %s: vol24h=$%.0f < $%.0f",
                                    sym, liq["vol24h_usd"], Config.MIN_VOLUME_24H)
                        continue
                    if liq["oi_usd"] < Config.MIN_OI:
                        logger.info("Пропуск %s: OI=$%.0f < $%.0f",
                                    sym, liq["oi_usd"], Config.MIN_OI)
                        continue

            # Basis Filter
            basis_info = get_basis(sym)
            if not basis_info:
                continue

            basis_pct = basis_info["basis_pct"]
            if basis_pct < -Config.MAX_BASIS_LOSS_PCT:
                logger.info("Пропуск %s: Basis=%.4f%% (ниже порога -%.2f%%)",
                            sym, basis_pct, Config.MAX_BASIS_LOSS_PCT)
                continue

            # Динамический размер позиции
            pos_size = calc_position_size(annual)

            # Есть свободный слот — просто открываем
            if can_open > 0 and free_balance >= pos_size:
                result["to_open"].append(
                    (sym, f"annual={annual:.1f}%, pos={positive_ratio:.0f}%, basis={basis_pct:.2f}%", pos_size)
                )
                open_symbols.add(sym)
                can_open -= 1
                free_balance -= pos_size
                continue

            # ── Rotation: все слоты заняты, но кандидат сильно лучше худшей позиции ──
            if worst_pos and annual > worst_annual * 1.5 and annual - worst_annual >= 5.0:
                worst_sym = worst_pos["symbol"]
                logger.info("ROTATION: %s (%.1f%%) вытесняет %s (%.1f%%)",
                            sym, annual, worst_sym, worst_annual)
                result["to_close"].append(
                    (worst_sym, f"rotation: замена на {sym} ({annual:.1f}% vs {worst_annual:.1f}%)")
                )
                margin_back = worst_pos.get("margin_usd", worst_pos["size_usd"])
                result["to_open"].append(
                    (sym, f"rotation: annual={annual:.1f}% (заменил {worst_sym} {worst_annual:.1f}%)", pos_size)
                )
                open_symbols.add(sym)
                open_symbols.discard(worst_sym)
                free_balance += margin_back - pos_size
                worst_pos = None
                continue

            if can_open <= 0:
                break

        return result

    def run(self) -> None:
        """Один цикл: анализ → решения → исполнение."""
        logger.info("AutoStrategy: запуск цикла")
        decisions = self.evaluate()

        # Закрытие
        for sym, reason in decisions["to_close"]:
            logger.info("AUTO CLOSE %s: %s", sym, reason)
            pos = self._trader.close_position(sym)
            if pos:
                self._notifier.position_closed(pos)
                self._notifier.send(f"🤖 <b>АВТО-ЗАКРЫТИЕ</b>\nПричина: {reason}")

        # Открытие
        for sym, reason, size in decisions["to_open"]:
            logger.info("AUTO OPEN %s ($%.2f): %s", sym, size, reason)
            pos = self._trader.open_position(sym, size)
            if pos:
                self._notifier.position_opened(pos)
                self._notifier.send(f"🤖 <b>АВТО-ВХОД</b>\nПричина: {reason}")

        # Сводка
        if decisions["to_open"] or decisions["to_close"]:
            summary = self._trader.summary()
            print(f"\n  🤖 Авто-стратегия: "
                  f"открыто {len(decisions['to_open'])}, "
                  f"закрыто {len(decisions['to_close'])}")
            print(f"  Баланс: ${summary['balance']:,.2f} | "
                  f"Портфель: ${summary['portfolio_value']:,.2f} | "
                  f"Позиций: {summary['open_positions_count']}")
        else:
            print("  🤖 Авто-стратегия: нет действий")


def monitor_risks(trader: PaperTrader, notifier: Notifier) -> None:
    """Мониторинг рисков: отрицательный фандинг и Basis Stop-Loss (V10)."""
    open_pos = trader.get_open_positions()
    if not open_pos:
        return

    for pos in open_pos:
        sym = pos["symbol"]
        
        # 1. Проверка Basis Stop-Loss (Защита маржи при x3 плече)
        try:
            basis_info = get_basis(sym)
            if basis_info:
                basis_pct = basis_info["basis_pct"]
                if basis_pct > Config.BASIS_STOP_LOSS_PCT:
                    logger.warning("🚨 Сработал Basis Stop-Loss для %s: %.2f%% > %.2f%%", 
                                   sym, basis_pct, Config.BASIS_STOP_LOSS_PCT)
                    
                    # Экстренное закрытие
                    result = trader.close_position(sym)
                    if result:
                        notifier.send(
                            f"🚨 <b>STOP-LOSS TRIGGERED</b> 🚨\n"
                            f"Монета: <code>{sym}</code>\n"
                            f"Спред Спот/Фьючерс разошелся до: <b>{basis_pct:.2f}%</b>\n"
                            f"Позиция экстренно закрыта для спасения маржи!\n"
                            f"PnL фиксации: <b>${result.pnl:+.4f}</b>"
                        )
                    continue  # Позиция закрыта, дальше не проверяем
        except Exception as exc:
            logger.error("Ошибка при проверке Basis Stop-Loss для %s: %s", sym, exc)
                
        # 2. Проверка критического падения фандинга
        try:
            info = get_funding_rate(sym)
            if info and info["annual_pct"] < Config.EXIT_THRESHOLD:
                logger.info("🚨 Экстренное закрытие %s: фандинг упал до %.1f%% годовых", sym, info["annual_pct"])
                result = trader.close_position(sym)
                if result:
                    notifier.send(
                        f"⚠️ <b>АВТО-ЗАКРЫТИЕ (ПАДЕНИЕ СТАВКИ)</b>\n"
                        f"Монета: <code>{sym}</code>\n"
                        f"Rate (Годовых): <b>{info['annual_pct']:.1f}%</b>\n"
                        f"Позиция ликвидирована (ниже порога {Config.EXIT_THRESHOLD}%)."
                    )
        except Exception as exc:
            logger.error("Ошибка при проверке критического фандинга для %s: %s", sym, exc)
