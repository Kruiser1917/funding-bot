# -*- coding: utf-8 -*-
"""Простой heartbeat-механизм для мониторинга демонов."""

import json
import os
import time
from datetime import datetime, timezone

from config import Config

HEARTBEAT_DIR = os.path.join(Config.DATA_DIR, "heartbeats")


def heartbeat(daemon_name: str) -> None:
    """Записать heartbeat-файл для демона."""
    os.makedirs(HEARTBEAT_DIR, exist_ok=True)
    path = os.path.join(HEARTBEAT_DIR, f"{daemon_name}.json")
    data = {
        "daemon": daemon_name,
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_sec": time.monotonic(),
    }
    with open(path, "w") as f:
        json.dump(data, f)


def check_all(max_age_sec: int = 300) -> dict[str, dict]:
    """Проверить состояние всех демонов.

    Returns:
        {daemon_name: {"alive": bool, "last_seen": str, "pid": int}}
    """
    result = {}
    if not os.path.isdir(HEARTBEAT_DIR):
        return result

    now = datetime.now(timezone.utc)
    for fname in os.listdir(HEARTBEAT_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(HEARTBEAT_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
            last = datetime.fromisoformat(data["timestamp"])
            age = (now - last).total_seconds()
            result[data["daemon"]] = {
                "alive": age < max_age_sec,
                "last_seen": data["timestamp"],
                "pid": data.get("pid"),
                "age_sec": round(age),
            }
        except (json.JSONDecodeError, KeyError):
            continue

    return result
