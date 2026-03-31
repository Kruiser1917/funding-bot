# -*- coding: utf-8 -*-
"""Получение Funding Rate и цен с OKX REST API (публичные endpoints)."""

import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from config import Config

logger = logging.getLogger(__name__)

# Таймаут HTTP запросов (секунды)
_TIMEOUT = 10


def _okx_get(path: str, params: dict | None = None, retries: int = 3) -> list[dict]:
    """GET-запрос к OKX API с retry и exponential backoff."""
    url = f"{Config.OKX_BASE_URL}{path}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != "0":
                msg = body.get("msg", "unknown error")
                logger.error("OKX API error %s: %s", path, msg)
                return []
            return body.get("data", [])
        except requests.RequestException as exc:
            delay = 2 ** attempt  # 1, 2, 4 сек
            logger.warning("HTTP error %s (попытка %d/%d): %s",
                           path, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                logger.error("HTTP error %s: все %d попытки исчерпаны", path, retries)
    return []


def get_funding_rate(symbol: str) -> dict | None:
    """Текущий и прогнозный Funding Rate для symbol.

    Returns:
        dict с полями: symbol, rate, rate_pct, next_rate, next_rate_pct,
                       annual_pct, fund_time, ts
    """
    data = _okx_get("/api/v5/public/funding-rate", {"instId": symbol})
    if not data:
        return None

    d = data[0]
    rate = float(d.get("fundingRate") or 0)
    next_rate = float(d.get("nextFundingRate") or 0)
    fund_time_ms = int(d.get("fundingTime") or 0)
    fund_time = datetime.fromtimestamp(fund_time_ms / 1000, tz=timezone.utc)

    return {
        "symbol": symbol,
        "rate": round(rate, 8),
        "rate_pct": round(rate * 100, 6),
        "next_rate": round(next_rate, 8),
        "next_rate_pct": round(next_rate * 100, 6),
        # 3 раза в день × 365 дней
        "annual_pct": round(rate * 3 * 365 * 100, 4),
        "fund_time": fund_time.strftime("%Y-%m-%d %H:%M UTC"),
        "ts": int(time.time()),
    }


def _signal(annual_pct: float) -> str:
    """Определяет сигнал по годовой доходности."""
    if annual_pct > 20:
        return "🔥 ENTER"
    if annual_pct > 10:
        return "👀 WATCH"
    return "😴 SKIP"


def get_all_rates(symbols: list[str] | None = None) -> pd.DataFrame:
    """Таблица Funding Rate по всем монетам, отсортирована по annual_pct desc."""
    symbols = symbols or Config.SYMBOLS
    rows = []
    for sym in symbols:
        info = get_funding_rate(sym)
        if info:
            info["signal"] = _signal(info["annual_pct"])
            rows.append(info)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.sort_values("annual_pct", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def get_funding_history(symbol: str, limit: int = 90) -> pd.DataFrame:
    """История Funding Rate за последние limit периодов.

    Добавляет агрегаты: avg_rate_pct, avg_annual_pct,
    positive_ratio, min_rate_pct, max_rate_pct.
    """
    all_records: list[dict] = []
    # OKX отдаёт макс 100 записей за раз, пагинация через after
    after = ""
    remaining = limit
    while remaining > 0:
        params = {"instId": symbol, "limit": str(min(remaining, 100))}
        if after:
            params["after"] = after
        data = _okx_get("/api/v5/public/funding-rate-history", params)
        if not data:
            break
        for d in data:
            rate = float(d.get("fundingRate") or 0)
            all_records.append({
                "symbol": symbol,
                "rate": rate,
                "rate_pct": round(rate * 100, 6),
                "annual_pct": round(rate * 3 * 365 * 100, 4),
                "ts": int(d.get("fundingTime", 0)),
            })
        after = data[-1].get("fundingTime", "")
        remaining -= len(data)
        if len(data) < 100:
            break

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    # Агрегаты
    df.attrs["avg_rate_pct"] = round(df["rate_pct"].mean(), 6)
    df.attrs["avg_annual_pct"] = round(df["annual_pct"].mean(), 4)
    df.attrs["positive_ratio"] = round(
        (df["rate"] > 0).sum() / len(df) * 100, 2
    )
    df.attrs["min_rate_pct"] = round(df["rate_pct"].min(), 6)
    df.attrs["max_rate_pct"] = round(df["rate_pct"].max(), 6)
    return df


def get_spot_price(symbol: str) -> float | None:
    """Цена спот-пары. BTC-USDT-SWAP → запрос BTC-USDT."""
    spot_symbol = symbol.replace("-SWAP", "")
    data = _okx_get("/api/v5/market/ticker", {"instId": spot_symbol})
    if not data:
        return None
    return round(float(data[0].get("last", 0)), 4)


def get_basis(symbol: str) -> dict | None:
    """Расчет спреда (Basis) между Spot и Swap.
    
    Returns:
        {"spot": float, "swap": float, "basis_pct": float}
    """
    spot_price = get_spot_price(symbol)
    if not spot_price:
        return None
        
    data = _okx_get("/api/v5/market/ticker", {"instId": symbol})
    if not data:
        return None
        
    swap_price = round(float(data[0].get("last", 0)), 4)
    basis_pct = round((swap_price - spot_price) / spot_price * 100, 4)
    
    return {
        "spot": spot_price,
        "swap": swap_price,
        "basis_pct": basis_pct
    }



def get_market_summary(symbols: list[str] | None = None) -> pd.DataFrame:
    """Расширенная таблица: все rates + история для топ-3 по rate."""
    df = get_all_rates(symbols)
    if df.empty:
        return df

    # Для топ-3 получаем историю
    top3 = df.head(3)["symbol"].tolist()
    history_cols = {
        "avg_rate_pct": [], "avg_annual_pct": [],
        "positive_ratio": [], "min_rate_pct": [], "max_rate_pct": [],
    }

    for _, row in df.iterrows():
        sym = row["symbol"]
        if sym in top3:
            hist = get_funding_history(sym, limit=90)
            if not hist.empty:
                for col in history_cols:
                    history_cols[col].append(hist.attrs.get(col, 0))
                continue
        # Заполняем NaN для остальных
        for col in history_cols:
            history_cols[col].append(None)

    for col, vals in history_cols.items():
        df[f"hist_{col}"] = vals

    return df
