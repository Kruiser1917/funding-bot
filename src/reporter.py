# -*- coding: utf-8 -*-
"""Консольные отчёты: таблицы, графики, сводки."""

import pandas as pd
from tabulate import tabulate


def _header(title: str) -> str:
    """Форматирование заголовка секции."""
    line = "━" * 50
    return f"\n{line}\n  {title}\n{line}"


def _mini_chart(values: list[float], width: int = 10) -> str:
    """ASCII мини-график из последних значений."""
    if not values:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    return "".join(
        chars[min(int((v - mn) / rng * (len(chars) - 1)), len(chars) - 1)]
        for v in values[-width:]
    )


def print_rates_table(df: pd.DataFrame) -> None:
    """Таблица текущих Funding Rate."""
    if df.empty:
        print("  Нет данных")
        return

    print(_header("📊 ТЕКУЩИЕ FUNDING RATES"))
    table_data = []
    for _, row in df.iterrows():
        table_data.append([
            row["symbol"],
            f"{row['rate_pct']:.4f}",
            f"{row['next_rate_pct']:.4f}",
            f"{row['annual_pct']:.1f}",
            row["signal"],
            row.get("fund_time", ""),
        ])

    print(tabulate(
        table_data,
        headers=["Монета", "Rate%", "След.%", "Год%", "Сигнал", "Время выплаты"],
        tablefmt="rounded_grid",
        stralign="right",
    ))


def print_history_stats(symbol: str, df: pd.DataFrame) -> None:
    """Статистика истории Funding Rate для символа."""
    if df.empty:
        print(f"  Нет истории для {symbol}")
        return

    print(f"\n  📈 ИСТОРИЯ: {symbol}")
    print(f"  Средний Rate:      {df.attrs.get('avg_rate_pct', 0):.4f}%")
    print(f"  Средний годовой:   {df.attrs.get('avg_annual_pct', 0):.1f}%")
    print(f"  Мин / Макс Rate:   {df.attrs.get('min_rate_pct', 0):.4f}% / "
          f"{df.attrs.get('max_rate_pct', 0):.4f}%")
    print(f"  Положительных:     {df.attrs.get('positive_ratio', 0):.1f}%")

    # Мини-график последних 10 значений
    recent = df["rate_pct"].tail(10).tolist()
    chart = _mini_chart(recent)
    if chart:
        print(f"  Последние 10:      {chart}")
    print()


def print_portfolio(trader) -> None:
    """Текущий портфель: баланс и открытые позиции."""
    summary = trader.summary()
    open_pos = trader.get_open_positions()

    print(_header(
        f"OKX Funding Rate Bot | Paper Trading Mode\n"
        f"  Портфель: ${summary['portfolio_value']:,.2f} | "
        f"Позиций: {summary['open_positions_count']} | "
        f"ROI: {summary['roi_pct']:+.2f}%"
    ))

    if not open_pos:
        print("\n  Нет открытых позиций\n")
        return

    table_data = []
    for p in open_pos:
        table_data.append([
            p["symbol"],
            f"${p['size_usd']:.2f}",
            f"${p['entry_price']:.4f}",
            f"${p['funding_earned']:.4f}",
            p["funding_count"],
            f"${p['commissions']:.4f}",
        ])

    print("\n📦 ОТКРЫТЫЕ ПОЗИЦИИ:")
    print(tabulate(
        table_data,
        headers=["Монета", "Размер", "Цена входа", "Funding", "Периоды", "Комиссии"],
        tablefmt="rounded_grid",
    ))
    print()


def print_summary(summary: dict) -> None:
    """Полная сводка портфеля с прогнозом."""
    print(_header("📋 СВОДКА ПОРТФЕЛЯ"))
    print(f"  Начальный капитал:  ${summary['capital']:,.2f}")
    print(f"  Стоимость портфеля: ${summary['portfolio_value']:,.2f}")
    print(f"  Свободный баланс:   ${summary['free_balance']:,.2f}")
    print(f"  ROI:                {summary['roi_pct']:+.2f}%")
    print()
    print(f"  💰 Заработано funding: ${summary['total_funding_earned']:,.4f}")
    print(f"  📈 Общий PnL:         ${summary['total_pnl']:+,.4f}")
    print(f"  💸 Комиссии:           ${summary['total_commissions']:,.4f}")
    print()
    print(f"  📊 Открыто позиций:    {summary['open_positions_count']}")
    print(f"  ✅ Закрыто сделок:     {summary['closed_trades_count']}")
    print(f"  📅 Дней работы:        {summary['days_running']}")
    print()
    print(f"  📈 Прогноз/мес:        ${summary['projected_monthly']:+,.2f}")
    print(f"  📈 Прогноз/год:        ${summary['projected_annual']:+,.2f}")
    print()
