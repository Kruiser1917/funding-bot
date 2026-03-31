# -*- coding: utf-8 -*-
"""Асинхронный WebSocket-клиент для OKX Public API (V7).

Подключается к wss://ws.okx.com:8443/ws/v5/public,
подписывается на канал funding-rate и кладёт входящие сообщения в asyncio.Queue.
Автоматически переподключается при разрыве.
"""

import asyncio
import json
import logging
import time

import websockets
from websockets.exceptions import ConnectionClosed

from config import Config

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
# OKX разрывает соединение если нет активности >30с. Пингуем каждые 25с.
PING_INTERVAL = 25
# Экспоненциальный backoff: [1, 2, 4, 8, 16, 30] секунд
MAX_BACKOFF = 30


def _build_subscribe_msg(symbols: list[str]) -> str:
    """Формируем JSON-пакет подписки на funding-rate для всех символов."""
    args = [{"channel": "funding-rate", "instId": sym} for sym in symbols]
    return json.dumps({"op": "subscribe", "args": args})


async def ws_listener(queue: asyncio.Queue, symbols: list[str]) -> None:
    """Бесконечный цикл: подключение -> подписка -> чтение -> reconnect."""
    backoff = 1
    while True:
        try:
            logger.info("[WS] Подключение к %s", WS_URL)
            # ping_interval/ping_timeout отключаем — делаем heartbeat вручную
            # через текстовый "ping", как требует OKX (не WS Ping-Frame)
            async with websockets.connect(
                WS_URL,
                ping_interval=None,
                ping_timeout=None,
                open_timeout=15,
            ) as ws:
                backoff = 1  # успешное подключение — сбрасываем backoff
                logger.info("[WS] Соединение установлено")

                # Подписываемся на все символы одним пакетом
                await ws.send(_build_subscribe_msg(symbols))
                logger.info("[WS] Подписка отправлена (%d символов)", len(symbols))

                # Запускаем задачу heartbeat параллельно с чтением
                heartbeat_task = asyncio.create_task(_heartbeat(ws))

                try:
                    async for raw_msg in ws:
                        # OKX шлёт "pong" в ответ на наш "ping"
                        if raw_msg == "pong":
                            logger.debug("[WS] pong получен")
                            continue

                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.warning("[WS] Не удалось распарсить: %s", raw_msg[:120])
                            continue

                        # Пропускаем системные события (subscribe/error)
                        if "event" in msg:
                            ev = msg["event"]
                            if ev == "error":
                                logger.error("[WS] Ошибка от OKX: %s", msg)
                            else:
                                logger.info("[WS] Event: %s", msg)
                            continue

                        # Кладём данные в очередь для обработки стратегией
                        logger.info("[WS] Push: ch=%s instId=%s",
                                    msg.get("arg", {}).get("channel", "?"),
                                    msg.get("arg", {}).get("instId", "?"))
                        await queue.put(msg)

                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

        except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
            logger.warning("[WS] Разрыв соединения: %s. Переподключение через %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        except Exception as exc:  # noqa: BLE001
            logger.error("[WS] Непредвиденная ошибка: %s. Переподключение через %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


async def _heartbeat(ws: websockets.WebSocketClientProtocol) -> None:
    """Отправляет текстовый 'ping' каждые PING_INTERVAL секунд.
    
    OKX ожидает именно текстовый ping, не WS Ping-Frame (RFC 6455).
    """
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.send("ping")
            logger.debug("[WS] ping отправлен")
        except ConnectionClosed:
            break  # ws_listener поймает исключение сам
