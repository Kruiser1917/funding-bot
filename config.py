# -*- coding: utf-8 -*-
"""Конфигурация бота. Загрузка из .env, дефолты, константы."""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Все настройки проекта — из переменных окружения или дефолты."""

    # OKX API (пока только для будущего — используем публичные endpoints)
    OKX_API_KEY: str = os.getenv("OKX_API_KEY", "")
    OKX_SECRET_KEY: str = os.getenv("OKX_SECRET_KEY", "")
    OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")

    # Telegram
    TG_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    TG_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Стратегия
    MIN_FUNDING_RATE: float = float(os.getenv("MIN_FUNDING_RATE", "0.03"))  # %
    SIMULATION_CAPITAL: float = float(os.getenv("SIMULATION_CAPITAL", "1000"))  # USD
    FUNDING_INTERVAL_HOURS: int = 8

    # Комиссии входа/выхода (спот + фьючерс)
    FEE_TAKER: float = float(os.getenv("FEE_TAKER", "0.001"))     # 0.100%
    FEE_MAKER: float = float(os.getenv("FEE_MAKER", "0.0002"))    # 0.020%
    
    # Кредитное плечо (x1 = без плеча)
    LEVERAGE: float = float(os.getenv("LEVERAGE", "3"))

    # Авто-стратегия
    ENTER_THRESHOLD: float = float(os.getenv("ENTER_THRESHOLD", "15"))  # мин. годовой % для входа
    EXIT_THRESHOLD: float = float(os.getenv("EXIT_THRESHOLD", "0"))    # ниже — закрываем
    POSITION_SIZE: float = float(os.getenv("POSITION_SIZE", "200"))     # USD на позицию
    MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "3"))           # макс. одновременных
    MIN_POSITIVE_RATIO: float = float(os.getenv("MIN_POSITIVE_RATIO", "60"))  # % положительных периодов
    
    # V4: Продвинутые параметры стратегии
    USE_COMPOUNDING: bool = os.getenv("USE_COMPOUNDING", "false").lower() == "true"
    # Динамический размер позиции: масштабируем от POSITION_SIZE до POSITION_SIZE_MAX
    # в зависимости от annual_pct (от ENTER_THRESHOLD до RATE_CAP_FOR_SIZING)
    DYNAMIC_SIZING: bool = os.getenv("DYNAMIC_SIZING", "true").lower() == "true"
    POSITION_SIZE_MAX: float = float(os.getenv("POSITION_SIZE_MAX", "400"))  # максимум USD
    RATE_CAP_FOR_SIZING: float = float(os.getenv("RATE_CAP_FOR_SIZING", "40"))  # % годовых для макс. размера
    # Максимальный разрешенный отрицательный спред (например 0.1% означает что фьючерс может быть на 0.1% дешевле спота)
    MAX_BASIS_LOSS_PCT: float = float(os.getenv("MAX_BASIS_LOSS_PCT", "0.1"))
    
    # V10: Basis Stop-Loss (Защита маржи плеча x3 от ликвидации)
    BASIS_STOP_LOSS_PCT: float = float(os.getenv("BASIS_STOP_LOSS_PCT", "2.0"))

    # Фильтр ликвидности: минимальный 24h объём (USD) для входа
    MIN_VOLUME_24H: float = float(os.getenv("MIN_VOLUME_24H", "5000000"))  # $5M
    # Минимальный Open Interest (USD)
    MIN_OI: float = float(os.getenv("MIN_OI", "2000000"))  # $2M

    # Реальная торговля (V5 Live Trading)
    # LIVE_TRADING=false → модуль exchange НИКОГДА не отправляет ордера на биржу,
    # только логирует что сделал бы. Безопасный dry-run режим.
    LIVE_TRADING: bool = os.getenv("LIVE_TRADING", "false").lower() == "true"
    # Режим маржи для фьючерса: "cross" или "isolated"
    TRADE_MARGIN_MODE: str = os.getenv("TRADE_MARGIN_MODE", "cross")

    # Базовый URL OKX REST API
    OKX_BASE_URL: str = "https://www.okx.com"

    # Символы для мониторинга
    SYMBOLS: list[str] = [
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "BNB-USDT-SWAP",
        "XRP-USDT-SWAP",
        "DOGE-USDT-SWAP",
        "ADA-USDT-SWAP",
        "AVAX-USDT-SWAP",
        "POL-USDT-SWAP",
        "LINK-USDT-SWAP",
    ]

    # Пути
    BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR: str = os.path.join(BASE_DIR, "data")
    LOG_DIR: str = os.path.join(BASE_DIR, "logs")
    POSITIONS_FILE: str = os.path.join(DATA_DIR, "positions.json")

    # Базы данных
    DB_MAIN_PATH: str = os.path.join(DATA_DIR, "bot.db")
    DB_WS_PATH: str = os.path.join(DATA_DIR, "bot_ws.db")

    @classmethod
    def ensure_dirs(cls) -> None:
        """Создаёт директории data/ и logs/ если не существуют."""
        os.makedirs(cls.DATA_DIR, exist_ok=True)
        os.makedirs(cls.LOG_DIR, exist_ok=True)

    @classmethod
    def validate(cls) -> list[str]:
        """Проверяет критичные параметры. Возвращает список предупреждений."""
        warnings = []
        if cls.LEVERAGE < 1 or cls.LEVERAGE > 100:
            warnings.append(f"LEVERAGE={cls.LEVERAGE} вне разумного диапазона (1-100)")
        if cls.POSITION_SIZE <= 0:
            warnings.append(f"POSITION_SIZE={cls.POSITION_SIZE} должен быть > 0")
        if cls.MAX_POSITIONS < 1:
            warnings.append(f"MAX_POSITIONS={cls.MAX_POSITIONS} должен быть >= 1")
        if cls.SIMULATION_CAPITAL <= 0:
            warnings.append(f"SIMULATION_CAPITAL={cls.SIMULATION_CAPITAL} должен быть > 0")
        if cls.POSITION_SIZE * cls.MAX_POSITIONS > cls.SIMULATION_CAPITAL:
            warnings.append(
                f"POSITION_SIZE * MAX_POSITIONS ({cls.POSITION_SIZE * cls.MAX_POSITIONS}) "
                f"> SIMULATION_CAPITAL ({cls.SIMULATION_CAPITAL})"
            )
        if not cls.TG_TOKEN:
            warnings.append("TELEGRAM_TOKEN не задан — Telegram-уведомления отключены")
        if cls.DYNAMIC_SIZING and cls.POSITION_SIZE_MAX < cls.POSITION_SIZE:
            warnings.append(
                f"POSITION_SIZE_MAX ({cls.POSITION_SIZE_MAX}) < POSITION_SIZE ({cls.POSITION_SIZE})"
            )
        return warnings
