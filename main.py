# -*- coding: utf-8 -*-
"""Точка входа OKX Funding Rate Arbitrage Bot (Paper Trading).

Режимы:
  python main.py --mode scan      — текущие Funding Rate
  python main.py --mode status    — портфель и статистика
  python main.py --mode open --symbol BTC-USDT-SWAP --amount 500
  python main.py --mode close --symbol BTC-USDT-SWAP
  python main.py --mode funding   — ручное начисление funding
  python main.py --mode history --symbol BTC-USDT-SWAP
  python main.py --mode auto      — запуск авто-стратегии
  python main.py --mode daemon    — фоновый режим
"""

import argparse
import logging
import sys
import time

import schedule

from config import Config
from src.funding import get_all_rates, get_funding_history, get_market_summary
from src.log import setup_logging
from src.notifier import Notifier
from src.reporter import (
    print_history_stats,
    print_portfolio,
    print_rates_table,
    print_summary,
)
from src.simulator import PaperTrader
from src.strategy import AutoStrategy


def show_banner(trader: PaperTrader) -> None:
    """Баннер при запуске."""
    s = trader.summary()
    line = "━" * 50
    print(f"\n{line}")
    print(f"  OKX Funding Rate Bot | Paper Trading Mode")
    print(f"  Портфель: ${s['portfolio_value']:,.2f} | "
          f"Позиций: {s['open_positions_count']} | "
          f"ROI: {s['roi_pct']:+.2f}%")
    print(f"{line}\n")


# ── Команды ──────────────────────────────────────────────

def cmd_scan(trader: PaperTrader, notifier: Notifier) -> None:
    """Сканирование текущих Funding Rate."""
    print("  Загрузка Funding Rate...")
    df = get_market_summary()
    if df.empty:
        print("  Не удалось получить данные")
        return

    print_rates_table(df)

    # История для топ-3
    top3 = df.head(3)["symbol"].tolist()
    for sym in top3:
        hist = get_funding_history(sym, limit=90)
        print_history_stats(sym, hist)

    # Telegram отчёт
    notifier.funding_report(df)

    # Алерты о высоких rate
    for _, row in df.iterrows():
        if row["annual_pct"] > 20:
            notifier.alert_high_rate(row["symbol"], row["annual_pct"])


def cmd_status(trader: PaperTrader) -> None:
    """Текущий портфель."""
    print_portfolio(trader)
    print_summary(trader.summary())


def cmd_open(trader: PaperTrader, notifier: Notifier,
             symbol: str, amount: float) -> None:
    """Открытие позиции."""
    pos = trader.open_position(symbol, amount)
    if pos:
        print(f"  ✅ Позиция открыта: {symbol} на ${amount:.2f}")
        notifier.position_opened(pos)
    else:
        print(f"  ❌ Не удалось открыть позицию {symbol}")


def cmd_close(trader: PaperTrader, notifier: Notifier, symbol: str) -> None:
    """Закрытие позиции."""
    pos = trader.close_position(symbol)
    if pos:
        print(f"  ✅ Позиция закрыта: {symbol}, PnL: ${pos.pnl:+.4f}")
        notifier.position_closed(pos)
    else:
        print(f"  ❌ Нет открытой позиции {symbol}")


def cmd_funding(trader: PaperTrader, notifier: Notifier) -> None:
    """Ручное начисление funding."""
    results = trader.apply_funding()
    if results:
        total = sum(results.values())
        print(f"  💰 Начислено funding: ${total:+.4f}")
        for sym, earned in results.items():
            print(f"     {sym}: ${earned:+.4f}")
        notifier.funding_applied(results, trader.balance)
    else:
        print("  Нет открытых позиций или не удалось получить rate")


def cmd_history(symbol: str) -> None:
    """История Funding Rate."""
    print(f"  Загрузка истории {symbol}...")
    hist = get_funding_history(symbol, limit=90)
    print_history_stats(symbol, hist)


def cmd_daemon(trader: PaperTrader, notifier: Notifier) -> None:
    """Фоновый режим: расписание задач."""
    logger = logging.getLogger(__name__)
    print("  🤖 Запуск daemon-режима (Ctrl+C для остановки)")
    print("  Расписание:")
    print("    - Каждый час: сканирование rate + авто-стратегия")
    print("    - 00:05, 08:05, 16:05 UTC: начисление funding")
    print("    - 06:00 UTC (09:00 МСК): ежедневный отчёт")
    print("    - Каждые 2 мин: мониторинг рисков (Basis Stop-Loss)")
    print("    - Каждые 5 мин: watchdog (здоровье демонов)\n")

    strategy = AutoStrategy(trader, notifier)

    def hourly_scan():
        """Ежечасное сканирование + авто-стратегия."""
        logger.info("Hourly scan + auto-strategy")
        df = get_all_rates()
        if df.empty:
            return
        for _, row in df.iterrows():
            if row["annual_pct"] > 20:
                notifier.alert_high_rate(row["symbol"], row["annual_pct"])
        # Запуск авто-стратегии
        strategy.run()

    def apply_funding_job():
        """Начисление funding по расписанию."""
        logger.info("Applying funding")
        results = trader.apply_funding()
        if results:
            notifier.funding_applied(results, trader.balance)
            
        # V6: Сохраняем снимок капитала после начисления фандинга
        trader.db.snapshot_equity(trader.summary()["portfolio_value"])

    def daily_report_job():
        """Ежедневный отчёт."""
        logger.info("Daily report")
        notifier.daily_report(trader.summary())

    def weekly_report_job():
        """Еженедельный отчёт по обоим ботам."""
        logger.info("Weekly report")
        from src.database import Database
        summary_main = trader.summary()
        db_ws = Database(db_path=Config.DB_WS_PATH)
        trader_ws = PaperTrader()
        trader_ws.db = db_ws
        summary_ws = trader_ws.summary()
        notifier.weekly_report(summary_main, summary_ws)

    def monitor_risks_job():
        """Мониторинг Basis Stop-Loss и отрицательного фандинга."""
        logger.info("Мониторинг рисков (Basis Stop-Loss)")
        from src.strategy import monitor_risks
        monitor_risks(trader, notifier)

    # Множество уже отправленных алертов (чтобы не спамить)
    _watchdog_alerted: set[str] = set()

    def watchdog_job():
        """Проверка здоровья всех демонов, алерт если кто-то упал."""
        from src.healthcheck import check_all
        statuses = check_all(max_age_sec=300)
        # daemon_name -> systemd service name
        _service_map = {"ws_daemon": "funding-ws", "tg_daemon": "funding-tg"}
        for name, service in _service_map.items():
            info = statuses.get(name)
            if not info or not info["alive"]:
                if name not in _watchdog_alerted:
                    age = info["age_sec"] if info else "N/A"
                    notifier.send(
                        f"🚨 <b>WATCHDOG: {name} НЕ ОТВЕЧАЕТ</b>\n"
                        f"Последний heartbeat: <b>{age}с назад</b>\n"
                        f"Проверьте: <code>sudo systemctl status {service}</code>"
                    )
                    _watchdog_alerted.add(name)
                    logger.warning("Watchdog: %s не отвечает (age=%s)", name, age)
            else:
                if name in _watchdog_alerted:
                    notifier.send(f"✅ <b>WATCHDOG: {name} снова в строю</b>")
                    _watchdog_alerted.discard(name)

    # Расписание
    schedule.every(1).hours.do(hourly_scan)
    schedule.every().day.at("00:05").do(apply_funding_job)
    schedule.every().day.at("08:05").do(apply_funding_job)
    schedule.every().day.at("16:05").do(apply_funding_job)
    schedule.every().day.at("06:00").do(daily_report_job)  # 09:00 МСК
    schedule.every().monday.at("06:00").do(weekly_report_job)  # Понедельник 09:00 МСК
    schedule.every(2).minutes.do(monitor_risks_job)
    schedule.every(5).minutes.do(watchdog_job)

    # Запуск scan сразу при старте
    hourly_scan()

    from src.healthcheck import heartbeat
    try:
        while True:
            schedule.run_pending()
            heartbeat("main_daemon")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n  Daemon остановлен")


def main() -> None:
    """Точка входа."""
    setup_logging("main", "bot.log")

    parser = argparse.ArgumentParser(
        description="OKX Funding Rate Arbitrage Bot (Paper Trading)"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["scan", "status", "open", "close", "funding", "history", "auto", "daemon"],
        help="Режим работы",
    )
    parser.add_argument("--symbol", type=str, help="Символ (напр. BTC-USDT-SWAP)")
    parser.add_argument("--amount", type=float, help="Сумма в USD")
    args = parser.parse_args()

    # Валидация конфигурации
    for w in Config.validate():
        print(f"  ⚠️  {w}")

    trader = PaperTrader()
    notifier = Notifier()

    # Баннер при каждом запуске
    show_banner(trader)

    if args.mode == "scan":
        cmd_scan(trader, notifier)
    elif args.mode == "status":
        cmd_status(trader)
    elif args.mode == "open":
        if not args.symbol or not args.amount:
            print("  ❌ Требуется --symbol и --amount")
            sys.exit(1)
        cmd_open(trader, notifier, args.symbol, args.amount)
    elif args.mode == "close":
        if not args.symbol:
            print("  ❌ Требуется --symbol")
            sys.exit(1)
        cmd_close(trader, notifier, args.symbol)
    elif args.mode == "funding":
        cmd_funding(trader, notifier)
    elif args.mode == "history":
        if not args.symbol:
            print("  ❌ Требуется --symbol")
            sys.exit(1)
        cmd_history(args.symbol)
    elif args.mode == "auto":
        strategy = AutoStrategy(trader, notifier)
        strategy.run()
    elif args.mode == "daemon":
        cmd_daemon(trader, notifier)


if __name__ == "__main__":
    main()
