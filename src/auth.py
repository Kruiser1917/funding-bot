# -*- coding: utf-8 -*-
"""Аутентификация и работа с приватным API OKX (заготовка для V5 Live Trading)."""

import base64
import hmac
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import requests

from config import Config

logger = logging.getLogger(__name__)


class OKXAuthClient:
    """Клиент для выполнения подписанных (Private) запросов к OKX V5 API."""
    
    def __init__(self):
        self.api_key = os.getenv("OKX_API_KEY", "")
        self.secret_key = os.getenv("OKX_SECRET_KEY", "")
        self.passphrase = os.getenv("OKX_PASSPHRASE", "")
        self.base_url = Config.OKX_BASE_URL
        self.is_configured = bool(self.api_key and self.secret_key and self.passphrase)

    def _get_timestamp(self) -> str:
        """OKX требует ISO 8601 формат с миллисекундами
        Пример: 2020-12-08T09:08:57.715Z"""
        return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Сборка и подпись строки запроса HMAC SHA256."""
        # Формат подписи: timestamp + method + requestPath + body
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            bytes(self.secret_key, encoding='utf-8'),
            bytes(message, encoding='utf-8'),
            digestmod='sha256'
        )
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _get_headers(self, request_path: str, method: str = "GET", body: str = "") -> dict:
        """Генерация заголовков для OKX V5."""
        if not self.is_configured:
            raise ValueError("OKX API ключи не настроены в .env")
            
        timestamp = self._get_timestamp()
        sign = self._sign(timestamp, method, request_path, body)

        return {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }

    def request(self, method: str, path: str, params: dict = None, body: dict = None) -> dict | None:
        """Выполнить подписанный HTTP запрос."""
        if not self.is_configured:
            logger.error("Попытка приватного запроса без API ключей")
            return None

        url = f"{self.base_url}{path}"
        query_string = ""
        
        if params:
            # Формируем query_string для GET
            query_string = "?" + urllib.parse.urlencode(params)
            url += query_string
            
        request_path = path + query_string
        body_str = json.dumps(body) if body else ""

        headers = self._get_headers(request_path, method, body_str)

        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                resp = requests.post(url, headers=headers, data=body_str, timeout=10)
            else:
                raise ValueError(f"Неподдерживаемый метод: {method}")
                
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("code") != "0":
                logger.error("OKX Private API Error %s: %s", path, data.get("msg"))
                return None
                
            return data.get("data")
            
        except requests.RequestException as exc:
            logger.error("HTTP error in OKXAuthClient: %s", exc)
            return None
