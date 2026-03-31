#!/usr/bin/env bash
# Скрипт развёртывания OKX Funding Rate Bot на VPS (Ubuntu/Debian)
# Запускать от root: bash setup.sh
set -euo pipefail

APP_DIR="/opt/funding-bot"
APP_USER="deploy"
PYTHON_VERSION="3.12"

echo "=== 1. Установка системных зависимостей ==="
apt update && apt install -y python${PYTHON_VERSION} python${PYTHON_VERSION}-venv git

echo "=== 2. Создание пользователя ==="
id -u $APP_USER &>/dev/null || useradd -r -m -s /bin/bash $APP_USER

echo "=== 3. Копирование проекта ==="
mkdir -p $APP_DIR
# Если запускается из директории проекта:
cp -r . $APP_DIR/ 2>/dev/null || echo "Скопируйте проект в $APP_DIR вручную"
chown -R $APP_USER:$APP_USER $APP_DIR

echo "=== 4. Виртуальное окружение ==="
sudo -u $APP_USER python${PYTHON_VERSION} -m venv $APP_DIR/venv
sudo -u $APP_USER $APP_DIR/venv/bin/pip install --upgrade pip
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt

echo "=== 5. Создание .env (если нет) ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp $APP_DIR/.env.example $APP_DIR/.env 2>/dev/null || echo "Создайте $APP_DIR/.env вручную"
    chmod 600 $APP_DIR/.env
    chown $APP_USER:$APP_USER $APP_DIR/.env
fi

echo "=== 6. Установка systemd-сервисов ==="
cp $APP_DIR/deploy/funding-main.service /etc/systemd/system/
cp $APP_DIR/deploy/funding-ws.service /etc/systemd/system/
cp $APP_DIR/deploy/funding-tg.service /etc/systemd/system/
cp $APP_DIR/deploy/funding-dashboard.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable funding-main funding-ws funding-tg funding-dashboard

echo "=== 7. Запуск сервисов ==="
systemctl start funding-main
systemctl start funding-ws
systemctl start funding-tg
systemctl start funding-dashboard

echo ""
echo "=== Готово! ==="
echo "Проверка статуса:  systemctl status funding-main funding-ws funding-tg funding-dashboard"
echo "Логи:              journalctl -u funding-main -f"
echo "Dashboard:          http://<IP>:5050"
echo ""
echo "ВАЖНО: Отредактируйте $APP_DIR/.env с вашими ключами!"
