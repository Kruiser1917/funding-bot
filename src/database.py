# -*- coding: utf-8 -*-
"""SQLite хранилище для симулятора (вместо JSON). Обеспечивает ACID."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from config import Config

logger = logging.getLogger(__name__)


class Database:
    """Управление SQLite БД."""

    def __init__(self, db_path: str = None):
        Config.ensure_dirs()
        self.db_path = db_path or os.path.join(Config.DATA_DIR, "bot.db")
        self._init_db()

    def _get_conn(self):
        """Возвращает соединение к БД."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Создание таблиц, если их нет."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            
            # Таблица состояния портфеля (одна строка)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    total_funding_earned REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    total_commissions REAL DEFAULT 0,
                    closed_trades_count INTEGER DEFAULT 0
                )
            ''')
            
            # Таблица позиций
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    size_usd REAL NOT NULL,
                    margin_usd REAL DEFAULT 0,
                    size_coin REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    funding_earned REAL DEFAULT 0,
                    funding_count INTEGER DEFAULT 0,
                    commissions REAL DEFAULT 0,
                    open_time TEXT NOT NULL,
                    order_type TEXT DEFAULT 'maker'
                )
            ''')
            
            # Миграция: динамическое добавление колонок, если таблица создана до V8
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN margin_usd REAL DEFAULT 0")
                cursor.execute("UPDATE positions SET margin_usd = size_usd WHERE margin_usd = 0")
            except sqlite3.OperationalError:
                pass # Уже есть
                
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN order_type TEXT DEFAULT 'maker'")
            except sqlite3.OperationalError:
                pass # Уже есть
            
            # Таблица логирования equity
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    portfolio_value REAL NOT NULL
                )
            ''')
            
            # Инициализация стейта, если пуст
            cursor.execute("SELECT id FROM state WHERE id = 1")
            if not cursor.fetchone():
                now = datetime.now(timezone.utc).isoformat()
                cursor.execute('''
                    INSERT INTO state (id, balance, created_at)
                    VALUES (1, ?, ?)
                ''', (Config.SIMULATION_CAPITAL, now))
            
            conn.commit()

    # ─── State / Portfolio ───

    def get_state(self) -> dict:
        """Получить текущее состояние портфеля."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM state WHERE id = 1")
            row = cursor.fetchone()
            return dict(row) if row else {}

    def update_state(self, updates: dict) -> None:
        """Обновить поля состояния (balance, total_pnl, etc)."""
        valid_keys = {"balance", "total_funding_earned", "total_pnl", 
                      "total_commissions", "closed_trades_count"}
        
        set_clauses = []
        values = []
        for k, v in updates.items():
            if k in valid_keys:
                set_clauses.append(f"{k} = ?")
                values.append(v)
                
        if not set_clauses:
            return
            
        with self._get_conn() as conn:
            query = f"UPDATE state SET {', '.join(set_clauses)} WHERE id = 1"
            conn.execute(query, values)
            conn.commit()

    # ─── Positions ───

    def get_open_positions(self) -> list[dict]:
        """Все открытые позиции."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM positions")
            return [dict(row) for row in cursor.fetchall()]

    def get_position(self, symbol: str) -> dict | None:
        """Получить позицию по символу."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def add_position(self, pos: dict) -> None:
        """Открытие новой позиции."""
        with self._get_conn() as conn:
            conn.execute('''
                INSERT INTO positions (symbol, size_usd, margin_usd, size_coin, entry_price, 
                                       funding_earned, funding_count, commissions, open_time, order_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pos["symbol"], pos["size_usd"], pos.get("margin_usd", pos["size_usd"]), pos["size_coin"], pos["entry_price"],
                pos.get("funding_earned", 0), pos.get("funding_count", 0),
                pos.get("commissions", 0), pos.get("open_time", datetime.now(timezone.utc).isoformat()),
                pos.get("order_type", "maker")
            ))
            conn.commit()

    def update_position(self, symbol: str, updates: dict) -> None:
        """Обновление позиции (например, при начислении funding)."""
        valid_keys = {"size_usd", "funding_earned", "funding_count", "commissions"}
        set_clauses = []
        values = []
        for k, v in updates.items():
            if k in valid_keys:
                set_clauses.append(f"{k} = ?")
                values.append(v)
                
        if not set_clauses:
            return
            
        with self._get_conn() as conn:
            values.append(symbol)
            query = f"UPDATE positions SET {', '.join(set_clauses)} WHERE symbol = ?"
            conn.execute(query, values)
            conn.commit()

    def delete_position(self, symbol: str) -> None:
        """Удаление позиции (при закрытии)."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            conn.commit()

    # ─── Equity History ───

    def snapshot_equity(self, portfolio_value: float) -> None:
        """Сохранить снимок стоимости портфеля."""
        with self._get_conn() as conn:
            conn.execute('''
                INSERT INTO equity_history (timestamp, portfolio_value)
                VALUES (?, ?)
            ''', (datetime.now(timezone.utc).isoformat(), portfolio_value))
            conn.commit()

    def get_equity_history(self) -> list[dict]:
        """Получить историю портфеля (для графика)."""
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT * FROM equity_history ORDER BY id ASC")
            return [dict(row) for row in cursor.fetchall()]

    # ─── JSON Migration ───

    def migrate_from_json(self, json_path: str) -> None:
        """Миграция данных из старого positions.json, если SQLite пуст."""
        if not os.path.exists(json_path):
            return

        with self._get_conn() as conn:
            # Проверяем, есть ли уже позиции или изменен баланс
            cursor = conn.execute("SELECT COUNT(*) FROM positions")
            pos_count = cursor.fetchone()[0]
            if pos_count > 0:
                logger.debug("БД не пуста, миграция JSON пропущена.")
                return

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            logger.info("Migrating data from positions.json to SQLite...")
            
            # Миграция state
            state = data.get("state", {})
            if state:
                # В старом state был только balance и created_at. Статистика была отдельным блоком, но потом мы её не сохраняли в state
                # Мы можем перенести баланс
                updates = {"balance": state.get("balance", Config.SIMULATION_CAPITAL)}
                stats = data.get("stats", {})
                if stats:
                    updates.update(stats)
                self.update_state(updates)
                
            # Миграция открытых позиций
            open_pos = data.get("open_positions", [])
            for p in open_pos:
                self.add_position(p)
                
            logger.info("Миграция JSON -> SQLite завершена (%d позиций)", len(open_pos))
            
            # Бекап JSON
            os.rename(json_path, json_path + ".bak")
            
        except Exception as exc:
            logger.error("Ошибка при миграции JSON: %s", exc)
