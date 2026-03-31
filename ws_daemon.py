# -*- coding: utf-8 -*-
"""V7: WebSocket Streaming Bot — точка входа.

Запускается ПАРАЛЛЕЛЬНО с основным main.py --mode daemon.
Использует ИЗОЛИРОВАННУЮ БД data/bot_ws.db (не трогает bot.db).

$ python ws_daemon.py

Прерывание: Ctrl+C
"""

import asyncio
import logging
import os

from config import Config
from src.database import Database
from src.log import setup_logging
from src.notifier import Notifier
from src.simulator import PaperTrader
from src.ws_client import ws_listener
from src.ws_strategy import WsStrategy

logger = setup_logging("ws_daemon", "ws_bot.log")


async def apply_funding_loop(trader: PaperTrader, notifier: Notifier, interval: int = 8 * 3600) -> None:
    """Начисляет фандинг каждые 8 часов в фоне."""
    while True:
        await asyncio.sleep(interval)
        logger.info("[WS-BOT] Начисление funding...")
        results = await asyncio.get_event_loop().run_in_executor(None, trader.apply_funding)
        if results:
            notifier.funding_applied(results, trader.balance)
        # Снимок equity
        summary = await asyncio.get_event_loop().run_in_executor(None, trader.summary)
        trader.db.snapshot_equity(summary["portfolio_value"])


async def monitor_risks_loop(trader: PaperTrader, notifier: Notifier, interval: int = 120) -> None:
    """Мониторинг рисков (Basis Stop-Loss и др.) раз в 2 минуты в фоне."""
    from src.strategy import monitor_risks
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.get_event_loop().run_in_executor(None, monitor_risks, trader, notifier)
        except Exception as exc:
            logger.error("[WS-BOT] Ошибка мониторинга рисков: %s", exc)


async def main() -> None:
    logger.info("=" * 60)
    logger.info(" V7 WebSocket Bot запущен (изолированный)")
    logger.info("=" * 60)

    # Изолированная БД — не пересекается с основным ботом
    ws_db_path = Config.DB_WS_PATH
    db = Database(db_path=ws_db_path)

    # PaperTrader с переопределённой БД
    trader = PaperTrader()
    trader.db = db  # подменяем БД на изолированную

    notifier = Notifier()

    # Очередь для сообщений от WS → стратегия
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    strategy = WsStrategy(trader, notifier)

    symbols = Config.SYMBOLS
    logger.info("[WS-BOT] Подписка на %d символов: %s", len(symbols), symbols)

    # Обработчик очереди сообщений
    async def consume_queue() -> None:
        while True:
            msg = await queue.get()
            try:
                await strategy.handle_message(msg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[WS-BOT] Ошибка в стратегии: %s", exc)
            finally:
                queue.task_done()

    notifier.send("[WS-BOT] 🚀 WebSocket бот запущен (V7, изолированный)")

    async def heartbeat_loop(interval: int = 30) -> None:
        from src.healthcheck import heartbeat
        while True:
            heartbeat("ws_daemon")
            await asyncio.sleep(interval)

    # Запускаем все задачи параллельно
    await asyncio.gather(
        ws_listener(queue, symbols),
        consume_queue(),
        apply_funding_loop(trader, notifier),
        monitor_risks_loop(trader, notifier),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[WS-BOT] Остановлен вручную")
