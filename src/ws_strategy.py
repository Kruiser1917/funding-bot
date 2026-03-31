# -*- coding: utf-8 -*-
"""Событийная стратегия для WS-бота (V7).

Принимает сообщения из asyncio.Queue и принимает решения об открытии позиций.

Фильтр от ложных пробоев (Spike Filter):
  Ставка должна превысить порог в ДВУХ подряд идущих push-пакетах (~30-90с каждый).
  Один кратковременный флаш-памп — игнорируется.

Защита от Race Condition:
  asyncio.Lock() гарантирует, что при 10 одновременных сигналах
  открывается только 1 позиция, а не несколько параллельно.
"""

import asyncio
import logging
import time

from config import Config
from src.funding import get_basis, get_funding_history
from src.notifier import Notifier
from src.simulator import PaperTrader

logger = logging.getLogger(__name__)


class WsStrategy:
    """Обработчик событий funding-rate push."""

    def __init__(self, trader: PaperTrader, notifier: Notifier):
        self.trader = trader
        self.notifier = notifier
        # symbol -> consecutive высокий signal. Нужно 2 подряд для входа.
        self._signal_streak: dict[str, int] = {}
        # Блокировка открытия новых позиций (защита от race условий)
        self._lock = asyncio.Lock()

    def _parse_funding(self, msg: dict) -> tuple[str, float, float] | None:
        """Извлекаем instId, fundingRate (float), annual_pct из push-данных OKX."""
        data_list = msg.get("data", [])
        if not data_list:
            return None

        d = data_list[0]
        inst_id = d.get("instId", "")
        # OKX шлёт строки, безопасно парсим
        try:
            rate = float(d.get("fundingRate") or 0)
        except (ValueError, TypeError):
            return None

        # Годовых: 3 выплаты в день × 365
        annual_pct = rate * 3 * 365 * 100
        return inst_id, rate, annual_pct

    async def handle_message(self, msg: dict) -> None:
        """Главный обработчик входящего WS-сообщения."""
        parsed = self._parse_funding(msg)
        if not parsed:
            return

        sym, rate, annual_pct = parsed

        # ── ЛОГИКА ВЫХОДА (проверяем открытые позиции) ──
        if self.trader.db.get_position(sym):
            if annual_pct < Config.EXIT_THRESHOLD:
                async with self._lock:
                    pos = self.trader.db.get_position(sym)
                    if pos:  # двойная проверка под локом
                        result = self.trader.close_position(sym)
                        if result:
                            logger.info("[WS-BOT] Закрыта позиция %s: annual=%.1f%%", sym, annual_pct)
                            self.notifier.send(
                                f"[WS-BOT] ❌ <b>ПОЗИЦИЯ ЗАКРЫТА</b>\n"
                                f"Монета: <code>{sym}</code>\n"
                                f"Rate: <b>{rate * 100:.4f}%</b> (annual {annual_pct:.1f}%)"
                            )
            # Сбрасываем счётчик уверенности для уже открытой монеты
            self._signal_streak.pop(sym, None)
            return

        # ── ЛОГИКА ВХОДА (spike filter) ──
        if annual_pct >= Config.ENTER_THRESHOLD:
            streak = self._signal_streak.get(sym, 0) + 1
            self._signal_streak[sym] = streak
            logger.debug("[WS-BOT] %s streak=%d annual=%.1f%%", sym, streak, annual_pct)
        else:
            # Ставка упала ниже порога — сбрасываем счётчик
            self._signal_streak.pop(sym, None)
            return

        # Нужно 2 подряд подтверждения чтобы войти
        if streak < 2:
            logger.info(
                "[WS-BOT] %s: signal 1/2 (annual=%.1f%%, ждём подтверждения)", sym, annual_pct
            )
            return

        self._signal_streak.pop(sym, None)  # сбрасываем после входа

        # ── Открытие позиции под локом ──
        async with self._lock:
            # Перепроверяем под локом: позиция могла открыться параллельно
            if self.trader.db.get_position(sym):
                return

            # Проверяем лимит позиций
            open_count = len(self.trader.db.get_open_positions())
            if open_count >= Config.MAX_POSITIONS:
                logger.info("[WS-BOT] Лимит позиций (%d). Пропуск %s", Config.MAX_POSITIONS, sym)
                return

            # Проверяем баланс
            pos_size = Config.POSITION_SIZE
            if self.trader.balance < pos_size:
                logger.info("[WS-BOT] Недостаточно баланса: %.2f < %.2f", self.trader.balance, pos_size)
                return

            # Проверяем Basis (спред Spot vs Swap) — в фоне чтобы не блокировать loop
            try:
                basis = await asyncio.get_event_loop().run_in_executor(None, get_basis, sym)
            except Exception:
                basis = None

            if basis and basis.get("basis_pct", 0) < -Config.MAX_BASIS_LOSS_PCT:
                logger.info(
                    "[WS-BOT] %s: плохой Basis (%.4f%%). Пропуск.",
                    sym, basis["basis_pct"]
                )
                return

            # Проверяем стабильность (historical positive_ratio) в фоне
            try:
                hist = await asyncio.get_event_loop().run_in_executor(
                    None, get_funding_history, sym, 30
                )
                pos_ratio = hist.attrs.get("positive_ratio", 0) if not hist.empty else 0
            except Exception:
                pos_ratio = 0

            if pos_ratio < Config.MIN_POSITIVE_RATIO:
                logger.info(
                    "[WS-BOT] %s: pos_ratio=%.0f%% < %.0f%%. Пропуск.",
                    sym, pos_ratio, Config.MIN_POSITIVE_RATIO
                )
                return

            # Всё проверено — открываем
            pos = self.trader.open_position(sym, pos_size)
            if pos:
                logger.info("[WS-BOT] ✅ Позиция открыта: %s annual=%.1f%%", sym, annual_pct)
                self.notifier.send(
                    f"[WS-BOT] ✅ <b>ВХОД (WS)</b>\n"
                    f"Монета: <code>{sym}</code>\n"
                    f"Маржа: <b>${pos_size:.2f}</b> (x{Config.LEVERAGE})\n"
                    f"Rate: <b>{rate * 100:.4f}%</b> | Annual: <b>{annual_pct:.1f}%</b>\n"
                    f"Basis: <b>{basis['basis_pct']:.4f}%</b> | PosRatio: <b>{pos_ratio:.0f}%</b>"
                    if basis else
                    f"[WS-BOT] ✅ <b>ВХОД (WS)</b>\n"
                    f"Монета: <code>{sym}</code>\n"
                    f"Маржа: <b>${pos_size:.2f}</b> (x{Config.LEVERAGE})\n"
                    f"Rate: <b>{rate * 100:.4f}%</b> | Annual: <b>{annual_pct:.1f}%</b>"
                )
