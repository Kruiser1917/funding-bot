# -*- coding: utf-8 -*-
"""Движок бэктестинга (V6).
Закачивает историю фандинга за 3-6 месяцев и прогоняет по ней авто-стратегию
с динамическими порогами (V5), учитывая комиссии.
"""

import logging
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime
from collections import defaultdict

import pandas as pd
from tqdm import tqdm

from config import Config
from src.funding import get_all_rates, get_funding_history
from src.simulator import PaperTrader

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")

# Отключаем вывод от других модулей
logging.getLogger("src.funding").setLevel(logging.WARNING)
logging.getLogger("src.simulator").setLevel(logging.WARNING)


def fetch_historical_data(months: int = 3, top_n: int = 30) -> pd.DataFrame:
    """Скачать датасет по топ-N монетам за N месяцев (1 месяц = ~90 периодов)."""
    limit = months * 90
    logger.info("Получение топ-%d монет...", top_n)
    
    current_market = get_all_rates()
    if current_market.empty:
        logger.error("Нет подключения к OKX")
        sys.exit(1)
        
    top_symbols = current_market.head(top_n)["symbol"].tolist()
    
    all_history = []
    logger.info("Скачивание истории фандинга (до %d записей/монета)...", limit)
    
    for sym in tqdm(top_symbols, desc="Загрузка данных"):
        # Качаем с запасом +90 периодов для первого lookback
        df_hist = get_funding_history(sym, limit=limit + 90)
        if not df_hist.empty:
            all_history.append(df_hist)
            
    if not all_history:
        return pd.DataFrame()
        
    master_df = pd.concat(all_history, ignore_index=True)
    # Сортируем так, чтобы самая старая дата была первой
    master_df = master_df.sort_values(by="ts", ascending=True).reset_index(drop=True)
    return master_df


def run_backtest(df: pd.DataFrame, enter_th: float = Config.ENTER_THRESHOLD, exit_th: float = Config.EXIT_THRESHOLD, quiet: bool = False) -> float:
    """Симуляция стратегии штрих за штрихом."""
    # Подготовка данных: делаем pivot (индекс - ts, колонки - символы, значения - rate)
    pivot_rate = df.pivot(index="ts", columns="symbol", values="rate").fillna(0)
    pivot_annual = df.pivot(index="ts", columns="symbol", values="annual_pct").fillna(0)
    
    # 90 периодов (1 месяц) используем чисто для 'разогрева' (lookback)
    if len(pivot_rate) < 90:
        logger.error("Недостаточно данных для бэктеста")
        return
        
    # Капитал
    capital = Config.SIMULATION_CAPITAL
    balance = capital
    open_positions = {}  # symbol -> dict
    closed_trades = 0
    total_commissions = 0
    
    # Метрики
    equity_curve = []
    
    if not quiet:
        logger.info("Старт симуляции на %d периодах (8ч)...", len(pivot_rate) - 90)
        
    iterator = range(90, len(pivot_rate))
    if not quiet:
        iterator = tqdm(iterator, desc="Бэктест")
        
    for i in iterator:
        current_ts = pivot_rate.index[i]
        
        # Получаем срез рынка "сейчас"
        current_rates = pivot_rate.iloc[i]
        current_annual = pivot_annual.iloc[i]
        
        # Срез истории для lookback (последние 90 периодов)
        lookback = pivot_rate.iloc[i-90:i]
        
        # MARKET SENTIMENT (V5)
        # 1. Считаем среднюю по рынку в этот момент
        top20_annual = current_annual.nlargest(20)
        market_avg_annual = top20_annual.mean()
        
        # 2. Бычье настроение (сколько монет имеют Rate > 0)
        market_bullishness = (current_rates > 0).mean() * 100
        
        # 3. Динамические пороги
        dynamic_enter = max(enter_th / 2, min(market_avg_annual * 0.8, 35.0))
        if market_bullishness > 70:
            dynamic_pos_ratio = max(50.0, Config.MIN_POSITIVE_RATIO - 10)
        else:
            dynamic_pos_ratio = min(90.0, Config.MIN_POSITIVE_RATIO + 10)
            
        # ── ОБРАБОТКА ТЕКУЩИХ ПОЗИЦИЙ ──
        to_close = []
        earned_this_tick = 0
        
        for sym, pos in open_positions.items():
            rate = current_rates[sym]
            annual = current_annual[sym]
            
            # Начисление фандинга
            earned = pos["size"] * rate
            pos["earned"] += earned
            pos["count"] += 1
            earned_this_tick += earned
            
            # Логика закрытия
            if annual < exit_th:
                to_close.append((sym, "rate_negative"))
                
        # Применяем фандинг к балансу
        balance += earned_this_tick
        
        # Закрываем
        for sym, reason in to_close:
            pos = open_positions.pop(sym)
            close_comm = pos["size"] * Config.FEE_TAKER
            total_comm = pos["comm"] + close_comm
            pnl = pos["earned"] - total_comm
            
            balance += (pos["size"] + pnl) # возвращаем тело с профитом
            closed_trades += 1
            total_commissions += total_comm
            
        # ── ОТКРЫТИЕ НОВЫХ ПОЗИЦИЙ ──
        can_open = Config.MAX_POSITIONS - len(open_positions)
        
        # Compounding logic
        if Config.USE_COMPOUNDING and can_open > 0:
            pos_size = round(balance / can_open, 2)
            if pos_size < 10: pos_size = Config.POSITION_SIZE
        else:
            pos_size = Config.POSITION_SIZE
            
        potential = []
        if can_open > 0 and balance >= pos_size:
            # Находим кандидатов
            for sym in current_annual.index:
                if sym in open_positions: continue
                
                annual = current_annual[sym]
                if annual < dynamic_enter: continue
                
                # Считаем positive_ratio за lookback
                rates_90 = lookback[sym]
                pos_ratio = (rates_90 > 0).mean() * 100
                if pos_ratio < dynamic_pos_ratio: continue
                
                # Мы игнорируем Basis в оффлайн тесте, так как свечи Спреда сложно вытянуть.
                # Считаем, что входим по идеальным ценам как тейкер (коммиссия 0.1%).
                potential.append((sym, annual, pos_ratio))
                
        # Сортируем лучших
        potential.sort(key=lambda x: x[1], reverse=True)
        
        for p in potential[:can_open]:
            if balance < pos_size: break
            sym = p[0]
            open_comm = pos_size * Config.FEE_TAKER
            open_positions[sym] = {
                "size": pos_size,
                "earned": 0,
                "count": 0,
                "comm": open_comm
            }
            balance -= pos_size
            
        # Снимок эквити
        locked = sum(p["size"] for p in open_positions.values())
        portfolio_value = balance + locked
        dt = datetime.fromtimestamp(current_ts / 1000)
        equity_curve.append({"date": dt.strftime("%Y-%m-%d %H:%M"), "value": portfolio_value})
        
    # --- Итоги ---
    final_locked = sum(p["size"] for p in open_positions.values())
    final_equity = balance + final_locked
    roi_pct = (final_equity - capital) / capital * 100
    annualized = roi_pct * (365 / (max(len(equity_curve)//3, 1)))
    
    if not quiet:
        print("\n" + "="*50)
        print(f" 🎯 БЭКТЕСТ ЗАВЕРШЕН (Периодов: {len(equity_curve)} = ~{len(equity_curve)//3} дней)")
        print("="*50)
        print(f"Начальный капитал: ${capital:,.2f}")
        print(f"Итоговый капитал:  ${final_equity:,.2f}")
        print(f"Чистая прибыль:    ${final_equity - capital:,.2f}")
        print(f"ROI (за период):   {roi_pct:+.2f}%")
        print(f"ROI (годовых):     {annualized:+.2f}%")
        print(f"\nВсего сделок (закрыто): {closed_trades}")
        print(f"Уплачено комиссий:      ${total_commissions:.2f}")
        
        # Максимальная просадка
        max_eq = capital
        max_dd = 0
        for eq in equity_curve:
            val = eq["value"]
            if val > max_eq: max_eq = val
            dd = (max_eq - val) / max_eq * 100
            if dd > max_dd: max_dd = dd
            
        print(f"Макс. просадка (Drawdown): {max_dd:.2f}%")
        print("="*50)
        
    return annualized


if __name__ == "__main__":
    print("Запуск модуля бэктестирования (V6).")
    df = fetch_historical_data(months=3, top_n=20)
    if df.empty:
        print("Ошибка получения исторических данных.")
        sys.exit(1)
        
    print("\n--- Запуск подбора параметров (Grid Search) ---")
    best_roi = -100
    best_params = None
    
    enter_range = [10.0, 15.0, 20.0, 25.0, 30.0]
    exit_range = [0.0, 5.0, -5.0]
    
    for en in enter_range:
        for ex in exit_range:
            roi = run_backtest(df, enter_th=en, exit_th=ex, quiet=True)
            print(f"ENTER={en:5.1f} | EXIT={ex:5.1f}  =>  ROI: {roi:+.2f}%")
            if roi > best_roi:
                best_roi = roi
                best_params = (en, ex)
                
    print(f"\n🏆 ЛУЧШИЕ ПАРАМЕТРЫ: ENTER_THRESHOLD={best_params[0]}, EXIT_THRESHOLD={best_params[1]} с ROI={best_roi:+.2f}%")
    
    print("\n--- Детальный прогон лучшей стратегии ---")
    run_backtest(df, enter_th=best_params[0], exit_th=best_params[1], quiet=False)
