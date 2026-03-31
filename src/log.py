# -*- coding: utf-8 -*-
"""Единая настройка логирования для всех демонов."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import Config


def setup_logging(name: str, log_filename: str | None = None) -> logging.Logger:
    """Настраивает root-логгер: файл с ротацией + stdout.

    Args:
        name: имя логгера (например 'main', 'ws_daemon', 'tg_daemon')
        log_filename: имя файла в logs/ (по умолчанию {name}.log)

    Returns:
        Logger с заданным именем.
    """
    Config.ensure_dirs()

    if log_filename is None:
        log_filename = f"{name}.log"

    log_path = os.path.join(Config.LOG_DIR, log_filename)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Не добавляем обработчики повторно
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(stream_handler)

    # Глушим шум от внешних библиотек
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger(name)
