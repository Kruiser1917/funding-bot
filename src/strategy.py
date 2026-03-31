# -*- coding: utf-8 -*-
"""Авто-стратегия: вход/выход на основе Funding Rate."""

import logging

from config import Config
from src.funding import get_all_rates, get_basis, get_funding_history
from src.notifier import Notifier
from src.simulator import PaperTrader

logger = logging.getLogger(__name__)


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
            from src.funding import get_funding_rate
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

        # ── Проверка на открытие ──────────────────────────────
        # Условия: есть место + есть свободный баланс
        can_open = Config.MAX_POSITIONS - len(open_positions) + len(result["to_close"])
        free_balance = self._trader.balance
        
        if can_open <= 0:
            return result

        # Расчет размера позиции с учетом Auto-compounding
        if Config.USE_COMPOUNDING:
            # Распределяем весь доступный баланс поровну на оставшиеся свободные слоты
            pos_size = round(free_balance / can_open, 2)
            # Ограничитель минимальной позиции (например $10)
            if pos_size < 10:
                pos_size = Config.POSITION_SIZE
        else:
            pos_size = Config.POSITION_SIZE

        if free_balance < pos_size:
            return result

        # Сканируем рынок
        df = get_all_rates()
        if df.empty:
            return result

        # ── Динамическая адаптация стратегии (V5) ──
        # 1. Считаем "температуру" рынка по топ-20 монетам
        top_20 = df.head(20)
        market_avg_annual = top_20["annual_pct"].mean()
        market_bullishness = (df["rate"] > 0).astype(int).mean() * 100

        # 2. Динамический ENTER_THRESHOLD
        # Берем 80% от среднего топа, но не ниже половины от базовых настроек и не выше 35%
        dynamic_enter = max(Config.ENTER_THRESHOLD / 2, min(market_avg_annual * 0.8, 35.0))
        
        # 3. Динамический MIN_POSITIVE_RATIO
        # Если рынок сильно зеленый (>70% монет в плюсе), можно рисковать и брать монеты с ratio похуже.
        # Если рынок красный, ужесточаем фильтр.
        if market_bullishness > 70:
            dynamic_pos_ratio = max(50.0, Config.MIN_POSITIVE_RATIO - 10)
        else:
            dynamic_pos_ratio = min(90.0, Config.MIN_POSITIVE_RATIO + 10)

        logger.info("Market Adapt: AvgTop20=%.1f%%, Bullish=%.0f%% -> Enter_Thresh=%.1f%%, PosRatio_Thresh=%.0f%%", 
                    market_avg_annual, market_bullishness, dynamic_enter, dynamic_pos_ratio)

        for _, row in df.iterrows():
            if can_open <= 0 or free_balance < pos_size:
                break

            sym = row["symbol"]
            annual = row["annual_pct"]

            # Пропускаем уже открытые
            if sym in open_symbols:
                continue

            # Проверка динамического порога
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

            # Basis Filter (спред Spot vs Swap)
            basis_info = get_basis(sym)
            if not basis_info:
                continue
            
            basis_pct = basis_info["basis_pct"]
            if basis_pct < -Config.MAX_BASIS_LOSS_PCT:
                logger.info("Пропуск %s: Basis=%.4f%% (ниже порога -%.2f%%)",
                            sym, basis_pct, Config.MAX_BASIS_LOSS_PCT)
                continue

            result["to_open"].append(
                (sym, f"annual={annual:.1f}%, pos={positive_ratio:.0f}%, basis={basis_pct:.2f}%", pos_size)
            )
            open_symbols.add(sym)
            can_open -= 1
            free_balance -= pos_size

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

    from src.funding import get_funding_rate, get_basis
    
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
