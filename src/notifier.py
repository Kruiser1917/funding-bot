# -*- coding: utf-8 -*-
"""Telegram-уведомления. Fallback на консоль если токен не задан."""

import logging

import pandas as pd
import requests

from config import Config

logger = logging.getLogger(__name__)

# Таймаут HTTP запросов к Telegram
_TG_TIMEOUT = 10


class Notifier:
    """Отправка уведомлений в Telegram (HTML) или в консоль."""

    def __init__(self):
        self._token = Config.TG_TOKEN
        self._chat_id = Config.TG_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            self._url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            logger.info("Telegram notifier инициализирован")
        else:
            self._url = ""
            logger.info("Telegram не настроен — вывод в консоль")

    def send(self, text: str) -> None:
        """Базовая отправка сообщения (HTML) через REST API."""
        if self._enabled:
            try:
                resp = requests.post(self._url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }, timeout=_TG_TIMEOUT)
                if resp.status_code != 200:
                    logger.error("TG API error: %s", resp.text[:200])
                else:
                    logger.debug("TG sent: %s...", text[:50])
            except requests.RequestException as exc:
                logger.error("Ошибка отправки в TG: %s", exc)
                print(f"[TG ERROR] {text}")
        else:
            print(f"[NOTIFY] {text}")

    def funding_report(self, df: pd.DataFrame) -> None:
        """Таблица funding rates с сигналами."""
        if df.empty:
            self.send("📊 Нет данных по Funding Rate")
            return

        lines = ["📊 <b>FUNDING RATES</b>\n"]
        for _, row in df.iterrows():
            emoji = row.get("signal", "")
            lines.append(
                f"<code>{row['symbol']:18s}</code> "
                f"Rate: <b>{row['rate_pct']:.4f}%</b> | "
                f"Год: <b>{row['annual_pct']:.1f}%</b> | "
                f"{emoji}"
            )
        self.send("\n".join(lines))

    def position_opened(self, pos) -> None:
        """Уведомление об открытии позиции."""
        text = (
            f"✅ <b>ВХОД (x{Config.LEVERAGE} {pos.order_type.upper()})</b>\n"
            f"Монета: <code>{pos.symbol}</code>\n"
            f"Маржа (Залог): <b>${pos.margin_usd:.2f}</b>\n"
            f"Номинал (Фактич.): <b>${pos.size_usd:.2f}</b>\n"
            f"Цена входа: <b>${pos.entry_price:.4f}</b>\n"
            f"Монет: <b>{pos.size_coin:.8f}</b>\n"
            f"Комиссия: <b>${pos.commissions:.4f}</b>"
        )
        self.send(text)

    def funding_applied(self, results: dict, balance: float) -> None:
        """Сводка начислений funding за период."""
        if not results:
            return

        lines = ["💰 <b>FUNDING НАЧИСЛЕН</b>\n"]
        total = 0.0
        for sym, earned in results.items():
            total += earned
            lines.append(f"<code>{sym:18s}</code> → <b>${earned:+.4f}</b>")

        lines.append(f"\nИтого: <b>${total:+.4f}</b>")
        lines.append(f"Баланс: <b>${balance:,.2f}</b>")
        self.send("\n".join(lines))

    def position_closed(self, pos) -> None:
        """Итог закрытой позиции."""
        emoji = "📈" if pos.pnl >= 0 else "📉"
        text = (
            f"{emoji} <b>ВЫХОД (x{Config.LEVERAGE} {pos.order_type.upper()})</b>\n"
            f"Монета: <code>{pos.symbol}</code>\n"
            f"Заработано funding: <b>${pos.funding_earned:.4f}</b>\n"
            f"Комиссии: <b>${pos.commissions:.4f}</b>\n"
            f"Чистый PnL: <b>${pos.pnl:+.4f}</b>\n"
            f"Периодов: <b>{pos.funding_count}</b>"
        )
        self.send(text)

    def daily_report(self, summary: dict) -> None:
        """Ежедневная сводка."""
        text = (
            f"📋 <b>ЕЖЕДНЕВНЫЙ ОТЧЁТ</b>\n\n"
            f"Баланс: <b>${summary['balance']:,.2f}</b>\n"
            f"ROI: <b>{summary['roi_pct']:+.2f}%</b>\n"
            f"Заработано: <b>${summary['total_funding_earned']:,.4f}</b>\n"
            f"Комиссии: <b>${summary['total_commissions']:,.4f}</b>\n"
            f"Открыто позиций: <b>{summary['open_positions_count']}</b>\n"
            f"Закрыто сделок: <b>{summary['closed_trades_count']}</b>\n"
            f"Дней работы: <b>{summary['days_running']}</b>\n\n"
            f"📈 Прогноз/мес: <b>${summary['projected_monthly']:+,.2f}</b>\n"
            f"📈 Прогноз/год: <b>${summary['projected_annual']:+,.2f}</b>"
        )
        self.send(text)

    def alert_high_rate(self, symbol: str, annual_pct: float) -> None:
        """Алерт о высоком funding rate."""
        self.send(
            f"🔥 <b>ВЫСОКИЙ RATE</b>\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Годовая доходность: <b>{annual_pct:.1f}%</b>"
        )
