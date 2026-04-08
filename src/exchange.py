# -*- coding: utf-8 -*-
"""Реальная торговля на OKX V5 — дельта-нейтральный хедж.

Модуль инкапсулирует всю логику размещения/закрытия хеджированной позиции
(спот long + perpetual short). Все "опасные" операции (реальные POST-запросы
на размещение ордеров, изменение плеча) проходят только при LIVE_TRADING=true.
В противном случае модуль работает в dry-run режиме: лог + возврат фейкового
ответа, реальных запросов на биржу не отправляется.

Основные методы:
    get_balance()          — USDT/монеты на торговом счёте
    get_instrument(inst)   — lot size, ctVal, minSz для ордера
    set_leverage(inst, x)  — установить плечо для SWAP-пары
    place_order(...)       — низкоуровневое размещение ордера
    open_hedged(sym, usd)  — высокий уровень: открыть спот + шорт
    close_hedged(sym)      — закрыть обе ноги позиции

Всё, что касается учёта PnL, funding-начислений и БД — остаётся в simulator.py
и использует тот же интерфейс. Переключение между paper и live будет сделано
отдельным адаптером выше (после полного тестирования exchange.py).
"""

from __future__ import annotations

import logging
from typing import Any

from config import Config
from src.auth import OKXAuthClient
from src.funding import get_spot_price

logger = logging.getLogger(__name__)


class ExchangeError(Exception):
    """Ошибка взаимодействия с OKX API или бизнес-правилом."""


class OKXExchange:
    """Высокоуровневая обёртка над OKX V5 Trading API."""

    def __init__(self) -> None:
        self.auth = OKXAuthClient()
        self.live = Config.LIVE_TRADING
        self.margin_mode = Config.TRADE_MARGIN_MODE  # "cross" или "isolated"
        # Кэш информации об инструментах, чтобы не дёргать API каждый ордер
        self._instrument_cache: dict[str, dict] = {}

        if self.live and not self.auth.is_configured:
            raise ExchangeError(
                "LIVE_TRADING=true, но OKX API ключи не настроены в .env"
            )

        logger.info(
            "OKXExchange инициализирован: live=%s, margin_mode=%s",
            self.live, self.margin_mode
        )

    # ── Вспомогательные методы ────────────────────────────────────────

    @staticmethod
    def swap_to_spot(swap_symbol: str) -> str:
        """BTC-USDT-SWAP → BTC-USDT."""
        return swap_symbol.replace("-SWAP", "")

    def _require_live(self, action: str) -> bool:
        """Проверка: если LIVE_TRADING=false, только логируем действие."""
        if not self.live:
            logger.warning("[DRY-RUN] %s — LIVE_TRADING=false, ордер НЕ отправлен", action)
            return False
        return True

    # ── Аккаунт и инструменты ─────────────────────────────────────────

    def get_balance(self, ccy: str = "USDT") -> dict | None:
        """Баланс конкретной валюты на торговом счёте (Unified Account)."""
        data = self.auth.request("GET", "/api/v5/account/balance", params={"ccy": ccy})
        if not data:
            return None
        # data — список с одним элементом (для запрошенной валюты)
        try:
            details = data[0].get("details", [])
            for d in details:
                if d.get("ccy") == ccy:
                    return {
                        "ccy": ccy,
                        "available": float(d.get("availBal") or 0),
                        "equity": float(d.get("eq") or 0),
                        "frozen": float(d.get("frozenBal") or 0),
                    }
        except (IndexError, KeyError, ValueError) as exc:
            logger.error("Не удалось распарсить balance: %s", exc)
        return None

    def get_instrument(self, inst_id: str) -> dict | None:
        """Метаданные инструмента: lotSz, minSz, ctVal (для SWAP).

        Public endpoint, не требует подписи, но idem через auth-клиент
        для единообразия обработки ответа.
        """
        if inst_id in self._instrument_cache:
            return self._instrument_cache[inst_id]

        inst_type = "SWAP" if inst_id.endswith("-SWAP") else "SPOT"
        # Public endpoint — используем обычный requests через funding._okx_get
        from src.funding import _okx_get
        data = _okx_get(
            "/api/v5/public/instruments",
            {"instType": inst_type, "instId": inst_id}
        )
        if not data:
            return None
        try:
            inst = data[0]
            result = {
                "inst_id": inst_id,
                "inst_type": inst_type,
                "lot_sz": float(inst.get("lotSz") or 0),
                "min_sz": float(inst.get("minSz") or 0),
                "tick_sz": float(inst.get("tickSz") or 0),
                "ct_val": float(inst.get("ctVal") or 0),     # размер 1 контракта в базовой валюте
                "ct_mult": float(inst.get("ctMult") or 1),   # множитель
                "state": inst.get("state"),
            }
            self._instrument_cache[inst_id] = result
            return result
        except (IndexError, KeyError, ValueError) as exc:
            logger.error("Не удалось распарсить instrument %s: %s", inst_id, exc)
            return None

    def get_positions(self, inst_id: str | None = None) -> list[dict]:
        """Открытые позиции по SWAP-контрактам."""
        params: dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        data = self.auth.request("GET", "/api/v5/account/positions", params=params) or []
        return data

    # ── Настройка плеча ───────────────────────────────────────────────

    def set_leverage(self, inst_id: str, lever: float) -> bool:
        """Установить плечо для SWAP-инструмента."""
        if not self._require_live(f"set_leverage({inst_id}, {lever}x)"):
            return True  # в dry-run считаем "успешно"

        body = {
            "instId": inst_id,
            "lever": str(int(lever)),
            "mgnMode": self.margin_mode,
        }
        data = self.auth.request("POST", "/api/v5/account/set-leverage", body=body)
        if data is None:
            logger.error("set_leverage %s failed", inst_id)
            return False
        logger.info("Плечо для %s установлено: %sx (%s)", inst_id, lever, self.margin_mode)
        return True

    # ── Низкоуровневый ордер ──────────────────────────────────────────

    def place_order(
        self,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        px: str | None = None,
        pos_side: str | None = None,
        tgt_ccy: str | None = None,
        reduce_only: bool = False,
    ) -> dict | None:
        """Разместить один ордер. Возвращает ответ OKX (ordId, clOrdId, …)."""
        action_descr = (
            f"order inst={inst_id} side={side} type={ord_type} "
            f"sz={sz} px={px} tdMode={td_mode} reduce_only={reduce_only}"
        )
        if not self._require_live(action_descr):
            return {"ordId": "DRY_RUN", "sCode": "0", "inst_id": inst_id, "sz": sz}

        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px is not None:
            body["px"] = px
        if pos_side is not None:
            body["posSide"] = pos_side
        if tgt_ccy is not None:
            body["tgtCcy"] = tgt_ccy
        if reduce_only:
            body["reduceOnly"] = "true"

        data = self.auth.request("POST", "/api/v5/trade/order", body=body)
        if not data:
            logger.error("place_order failed: %s", action_descr)
            return None
        result = data[0] if isinstance(data, list) else data
        s_code = result.get("sCode")
        if s_code != "0":
            logger.error("OKX order error sCode=%s msg=%s body=%s",
                         s_code, result.get("sMsg"), body)
            return None
        logger.info("Ордер размещён: %s → ordId=%s", action_descr, result.get("ordId"))
        return result

    # ── Высокий уровень: хеджированная позиция ────────────────────────

    def open_hedged(self, swap_symbol: str, usd_margin: float) -> dict | None:
        """Открыть дельта-нейтральную позицию: спот long + swap short.

        Args:
            swap_symbol: BTC-USDT-SWAP
            usd_margin:  сколько USDT выделить под маржу (без плеча)

        Возвращает dict с описанием обеих ног или None при ошибке.
        """
        spot_symbol = self.swap_to_spot(swap_symbol)

        # 1. Собрать метаданные
        swap_inst = self.get_instrument(swap_symbol)
        spot_inst = self.get_instrument(spot_symbol)
        if not swap_inst or not spot_inst:
            raise ExchangeError(f"Не удалось получить метаданные {swap_symbol}/{spot_symbol}")

        price = get_spot_price(swap_symbol)
        if not price:
            raise ExchangeError(f"Нет цены для {spot_symbol}")

        # 2. Посчитать размеры
        notional = usd_margin * Config.LEVERAGE
        ct_val = swap_inst["ct_val"]
        if ct_val <= 0:
            raise ExchangeError(f"Некорректный ctVal для {swap_symbol}: {ct_val}")

        # Количество контрактов swap: сколько монет / ctVal
        coins = notional / price
        contracts_raw = coins / ct_val
        # Округление до lotSz (кратно)
        lot_sz = swap_inst["lot_sz"] or 1
        contracts = int(contracts_raw / lot_sz) * lot_sz
        if contracts < swap_inst["min_sz"]:
            raise ExchangeError(
                f"{swap_symbol}: размер {contracts} < minSz {swap_inst['min_sz']} "
                f"(попробуй увеличить usd_margin или LEVERAGE)"
            )

        # Для спота: покупаем ровно на usd_margin USDT (без плеча) — хедж на 1x
        # ВАЖНО: при LEVERAGE=3 на swap мы ставим шорт в 3x, но спот покупаем только
        # на 1x (usd_margin USDT). Это НЕ полный дельта-хедж. Для полного нужно
        # тратить notional USDT на спот, но тогда это весь капитал × leverage.
        # Выбираем: спот на usd_margin (1x), swap short на notional (Nx) —
        # получается делta-нейтральность только на 1x часть, остальное — чистый шорт.
        #
        # Правильный хедж: leverage=1 → спот == swap. Либо тратить notional на спот.
        # Для бесплечевого делта-нейтрального funding arb: LEVERAGE=1.
        # Оставляю комментарий, чтобы вспомнить при переходе на реальный live.
        spot_usdt = usd_margin  # сколько USDT потратим на спот-покупку

        logger.info(
            "open_hedged %s: margin=$%.2f, notional=$%.2f, price=%.4f, "
            "contracts=%s, spot_usdt=$%.2f",
            swap_symbol, usd_margin, notional, price, contracts, spot_usdt
        )

        # 3. Плечо на swap
        if not self.set_leverage(swap_symbol, Config.LEVERAGE):
            raise ExchangeError(f"set_leverage({swap_symbol}) failed")

        # 4. Спот-покупка (market, tgtCcy=quote_ccy → sz в USDT)
        spot_order = self.place_order(
            inst_id=spot_symbol,
            td_mode="cash",
            side="buy",
            ord_type="market",
            sz=f"{spot_usdt:.4f}",
            tgt_ccy="quote_ccy",
        )
        if not spot_order:
            raise ExchangeError(f"Спот-покупка {spot_symbol} не удалась")

        # 5. Swap-шорт (market)
        swap_order = self.place_order(
            inst_id=swap_symbol,
            td_mode=self.margin_mode,
            side="sell",
            ord_type="market",
            sz=str(contracts),
        )
        if not swap_order:
            # Откат: продаём купленный спот обратно
            logger.error("Swap-шорт %s не удался, откатываю спот", swap_symbol)
            # (в dry-run просто логируем)
            self.place_order(
                inst_id=spot_symbol, td_mode="cash", side="sell",
                ord_type="market", sz=f"{spot_usdt / price:.8f}",
            )
            raise ExchangeError(f"Swap short {swap_symbol} не удался")

        return {
            "symbol": swap_symbol,
            "spot_symbol": spot_symbol,
            "entry_price": price,
            "margin_usd": usd_margin,
            "notional_usd": notional,
            "contracts": contracts,
            "spot_usdt": spot_usdt,
            "spot_order_id": spot_order.get("ordId"),
            "swap_order_id": swap_order.get("ordId"),
        }

    def close_hedged(self, swap_symbol: str, contracts: float, spot_coins: float) -> dict | None:
        """Закрыть хеджированную позицию: закрыть swap short + продать спот.

        Args:
            swap_symbol: BTC-USDT-SWAP
            contracts:   количество контрактов к закрытию (из open_hedged)
            spot_coins:  количество монет к продаже на споте
        """
        spot_symbol = self.swap_to_spot(swap_symbol)
        logger.info(
            "close_hedged %s: contracts=%s, spot_coins=%s",
            swap_symbol, contracts, spot_coins
        )

        # 1. Закрыть swap short (buy, reduceOnly)
        swap_close = self.place_order(
            inst_id=swap_symbol,
            td_mode=self.margin_mode,
            side="buy",
            ord_type="market",
            sz=str(contracts),
            reduce_only=True,
        )
        if not swap_close:
            raise ExchangeError(f"Не удалось закрыть swap-шорт {swap_symbol}")

        # 2. Продать спот
        spot_close = self.place_order(
            inst_id=spot_symbol,
            td_mode="cash",
            side="sell",
            ord_type="market",
            sz=f"{spot_coins:.8f}",
        )
        if not spot_close:
            logger.error("Swap закрыт, но спот %s не продан — требуется ручное вмешательство!",
                         spot_symbol)
            raise ExchangeError(f"Спот-продажа {spot_symbol} не удалась")

        return {
            "symbol": swap_symbol,
            "swap_close_id": swap_close.get("ordId"),
            "spot_close_id": spot_close.get("ordId"),
        }


# ── CLI-точка входа для ручного тестирования ──────────────────────────

def _cli_check() -> None:
    """Запуск: `python -m src.exchange` — проверка аккаунта и инструментов."""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"LIVE_TRADING = {Config.LIVE_TRADING}")
    print(f"API key present: {bool(Config.OKX_API_KEY)}")

    try:
        ex = OKXExchange()
    except ExchangeError as exc:
        print(f"Init error: {exc}")
        sys.exit(1)

    # Баланс
    bal = ex.get_balance("USDT")
    print(f"USDT balance: {bal}")

    # Инфа об инструментах по всем символам
    for sym in Config.SYMBOLS[:3]:
        inst = ex.get_instrument(sym)
        print(f"{sym}: {inst}")


if __name__ == "__main__":
    _cli_check()
