# -*- coding: utf-8 -*-
"""Flask-сервер для Web Dashboard."""

import os

import pandas as pd
from flask import Flask, jsonify, send_from_directory

from src.funding import get_all_rates, get_funding_history
from src.simulator import PaperTrader

app = Flask(__name__, static_folder="static")

# Получаем путь к директории проекта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    """Отдача index.html."""
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/api/status")
def api_status():
    """Данные портфеля."""
    trader = PaperTrader()
    summary = trader.summary()
    open_pos = trader.get_open_positions()
    
    # Добавляем unrealized funding и текущий rate для открытых позиций
    positions = []
    for pos in open_pos:
        detail = trader.get_position_detail(pos["symbol"])
        positions.append(detail if detail else pos)

    return jsonify({
        "summary": summary,
        "positions": positions
    })


@app.route("/api/rates")
def api_rates():
    """Текущие Funding Rates по всем символам."""
    df = get_all_rates()
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/history/<symbol>")
def api_history(symbol):
    """История Funding Rate для графика (ограничение до 50 точек для наглядности)."""
    df = get_funding_history(symbol, limit=50)
    if df.empty:
        return jsonify([])
    # Сортировка по времени (старые -> новые) для графика
    df = df.sort_values("ts", ascending=True)
    
    # Форматируем данные для Chart.js
    labels = df["ts"].apply(lambda t: pd.to_datetime(t, unit='ms').strftime('%d.%m %H:%M')).tolist()
    data = df["rate_pct"].tolist()
    
    return jsonify({
        "labels": labels,
        "data": data,
        "symbol": symbol
    })


@app.route("/api/equity")
def api_equity():
    """История изменения капитала (Equity Curve)."""
    trader = PaperTrader()
    history = trader.db.get_equity_history()
    
    if not history:
        # Если истории еще нет, делаем точку старта
        return jsonify({"labels": [], "data": []})
        
    labels = [pd.to_datetime(h["timestamp"]).strftime('%d.%m %H:%M') for h in history]
    data = [h["portfolio_value"] for h in history]
    
    return jsonify({
        "labels": labels,
        "data": data
    })



@app.route("/api/health")
def api_health():
    """Статус всех демонов (heartbeat check)."""
    from src.healthcheck import check_all
    return jsonify(check_all())


if __name__ == "__main__":
    print("🚀 Web Dashboard запускается...")
    print("Открой в браузере: http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)
