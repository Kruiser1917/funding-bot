# -*- coding: utf-8 -*-
"""V11: Интерактивный Telegram-бот (2-way communication).

Позволяет запрашивать статус, статистику и экстренно закрывать позиции.
Работает параллельно с main.py и ws_daemon.py, читая/записывая прямо в bot.db
(SQLite ACID-гарантии предотвращают конфликты).

Запуск:
    $ python tg_daemon.py
"""

import logging
import os
import sys
import time

import telebot
from telebot.types import Message

from config import Config
from src.database import Database
from src.log import setup_logging
from src.simulator import PaperTrader

logger = setup_logging("tg_daemon")

if not Config.TG_TOKEN or not Config.TG_CHAT_ID:
    logger.error("Для работы tg_daemon.py необходимо заполнить TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в .env")
    sys.exit(1)

# Авторизуем бота
bot = telebot.TeleBot(Config.TG_TOKEN, parse_mode="HTML")
# Доверенный Chat ID — только из него слушаем команды
ALLOWED_CHAT_ID = int(Config.TG_CHAT_ID)


def _check_auth(message: Message) -> bool:
    """Проверка, что команда пришла от админа (из .env)."""
    if message.chat.id != ALLOWED_CHAT_ID:
        logger.warning("Попытка доступа от чужого аккаунта: chat_id=%s, user=%s",
                       message.chat.id, message.from_user.username)
        return False
    return True


@bot.message_handler(commands=['start', 'help'])
def send_help(message: Message):
    if not _check_auth(message):
        return
    text = (
        "🤖 <b>OKX Funding Arbitrage Bot</b>\n\n"
        "Доступные команды:\n"
        "📈 /status — Ежедневный отчет, капитал, ROI\n"
        "💼 /positions — Список открытых сделок и их PnL\n"
        "📊 /weekly — Еженедельный отчёт по обоим ботам\n"
        "🚨 /close_all — <b>Паник-кнопка:</b> закрыть все сделки немедленно"
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=['status'])
def send_status(message: Message):
    if not _check_auth(message):
        return
    
    # REST Bot (bot.db)
    trader_main = PaperTrader()
    summary_main = trader_main.summary()
    
    # WS Bot (bot_ws.db)
    ws_db_path = Config.DB_WS_PATH
    db_ws = Database(db_path=ws_db_path)
    trader_ws = PaperTrader()
    trader_ws.db = db_ws
    summary_ws = trader_ws.summary()
    
    # Объединенные итоги (каждый бот стартует с $1000 независимо)
    total_capital = 2 * Config.SIMULATION_CAPITAL
    total_portfolio = summary_main['portfolio_value'] + summary_ws['portfolio_value']
    total_funding = summary_main['total_funding_earned'] + summary_ws['total_funding_earned']
    total_positions = summary_main['open_positions_count'] + summary_ws['open_positions_count']
    total_roi = round((total_portfolio - total_capital) / total_capital * 100, 4) if total_capital else 0
    
    text = (
        f"📊 <b>СТАТУС ПОРТФЕЛЯ</b>\n\n"
        f"🔹 <b>REST Бот (main.py):</b>\n"
        f"Портфель: <b>${summary_main['portfolio_value']:,.2f}</b> | Сделок: <b>{summary_main['open_positions_count']}</b>\n"
        f"Фандинг: <b>${summary_main['total_funding_earned']:,.4f}</b> | ROI: <b>{summary_main['roi_pct']:+.2f}%</b>\n\n"
        f"⚡️ <b>WS Бот (ws_daemon.py):</b>\n"
        f"Портфель: <b>${summary_ws['portfolio_value']:,.2f}</b> | Сделок: <b>{summary_ws['open_positions_count']}</b>\n"
        f"Фандинг: <b>${summary_ws['total_funding_earned']:,.4f}</b> | ROI: <b>{summary_ws['roi_pct']:+.2f}%</b>\n\n"
        f"💰 <b>ИТОГО:</b>\n"
        f"Портфель: <b>${total_portfolio:,.2f}</b> из <b>${total_capital:,.0f}</b>\n"
        f"Фандинг: <b>${total_funding:,.4f}</b> | ROI: <b>{total_roi:+.2f}%</b>\n"
        f"В сделках: <b>{total_positions}</b>"
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=['positions'])
def send_positions(message: Message):
    if not _check_auth(message):
        return
        
    trader_main = PaperTrader()
    open_pos_main = trader_main.get_open_positions()
    
    ws_db_path = Config.DB_WS_PATH
    db_ws = Database(db_path=ws_db_path)
    trader_ws = PaperTrader()
    trader_ws.db = db_ws
    open_pos_ws = trader_ws.get_open_positions()
    
    if not open_pos_main and not open_pos_ws:
        bot.reply_to(message, "😴 В данный момент нет открытых позиций ни в одном из ботов.")
        return
        
    lines = ["💼 <b>ОТКРЫТЫЕ СДЕЛКИ:</b>\n"]
    
    if open_pos_main:
        lines.append("🔹 <b>REST Бот:</b>")
        for pos in open_pos_main:
            sym = pos['symbol']
            margin = pos.get('margin_usd', pos['size_usd'])
            funding = pos.get('funding_earned', 0)
            lines.append(f"🔸 <code>{sym}</code> (залог ${margin:,.0f}) → фандинг <b>${funding:+.4f}</b>")
        lines.append("")
            
    if open_pos_ws:
        lines.append("⚡️ <b>WS Бот:</b>")
        for pos in open_pos_ws:
            sym = pos['symbol']
            margin = pos.get('margin_usd', pos['size_usd'])
            funding = pos.get('funding_earned', 0)
            lines.append(f"🔸 <code>{sym}</code> (залог ${margin:,.0f}) → фандинг <b>${funding:+.4f}</b>")
            
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=['weekly'])
def send_weekly(message: Message):
    """Еженедельный отчёт по запросу."""
    if not _check_auth(message):
        return

    from src.notifier import Notifier
    trader_main = PaperTrader()
    summary_main = trader_main.summary()

    ws_db_path = Config.DB_WS_PATH
    db_ws = Database(db_path=ws_db_path)
    trader_ws = PaperTrader()
    trader_ws.db = db_ws
    summary_ws = trader_ws.summary()

    n = Notifier()
    n._chat_id = str(message.chat.id)
    n.weekly_report(summary_main, summary_ws)


@bot.message_handler(commands=['close_all'])
def close_all_positions(message: Message):
    """Экстренное завершение всех сделок в обоих ботах."""
    if not _check_auth(message):
        return
        
    trader_main = PaperTrader()
    open_pos_main = trader_main.get_open_positions()
    
    ws_db_path = Config.DB_WS_PATH
    db_ws = Database(db_path=ws_db_path)
    trader_ws = PaperTrader()
    trader_ws.db = db_ws
    open_pos_ws = trader_ws.get_open_positions()
    
    if not open_pos_main and not open_pos_ws:
        bot.reply_to(message, "🚨 Нет открытых позиций для закрытия.")
        return
        
    count = 0
    total_pnl = 0.0
    
    for pos in list(open_pos_main):
        result = trader_main.close_position(pos["symbol"])
        if result:
            count += 1
            total_pnl += result.pnl
            
    for pos in list(open_pos_ws):
        result = trader_ws.close_position(pos["symbol"])
        if result:
            count += 1
            total_pnl += result.pnl
            
    bot.reply_to(
        message, 
        f"🚨 <b>ПАНИК-КНОПКА АКТИВИРОВАНА</b>\n\n"
        f"Закрыто позиций: <b>{count}</b> (в обоих ботах)\n"
        f"Общий PnL фиксации: <b>${total_pnl:+.4f}</b>"
    )


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info(" V11 Telegram Interactive Bot запущен")
    logger.info(" Ожидание команд от chat_id=%s", ALLOWED_CHAT_ID)
    logger.info("=" * 60)

    import threading
    from src.healthcheck import heartbeat

    def _heartbeat_loop():
        """Фоновый поток: обновляет heartbeat каждые 60 секунд."""
        while True:
            try:
                heartbeat("tg_daemon")
            except Exception:
                pass
            time.sleep(60)

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as exc:
            logger.error("Ошибка Telegram Polling: %s. Переподключение...", exc)
            time.sleep(5)
