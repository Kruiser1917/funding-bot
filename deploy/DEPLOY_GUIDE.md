# Деплой OKX Funding Rate Bot на Beget Cloud VPS

## Шаг 1: Заказ VPS на Beget

1. Зайди на https://beget.com/ru/cloud
2. Зарегистрируйся (или войди в аккаунт)
3. Создай Cloud VPS:
   - **ОС:** Ubuntu 22.04 LTS
   - **CPU:** 1 vCPU
   - **RAM:** 1 GB
   - **Диск:** 10 GB NVMe
   - **Тариф:** ~210 ₽/мес (или текущий минимальный)
4. Оплати картой Мир / СБП
5. Запиши **IP-адрес** и **root-пароль** из письма

---

## Шаг 2: Первое подключение и настройка безопасности

```bash
# Подключись с Mac/Linux
ssh root@<IP_СЕРВЕРА>

# Обнови систему
apt update && apt upgrade -y

# Создай пользователя (не работай от root)
adduser deploy
usermod -aG sudo deploy

# Настрой SSH-ключ (на своём Mac)
# В НОВОМ терминале на своём компьютере:
ssh-keygen -t ed25519 -C "funding-bot"
ssh-copy-id deploy@<IP_СЕРВЕРА>

# Вернись на сервер и отключи вход по паролю
sudo nano /etc/ssh/sshd_config
# Найди и измени:
#   PasswordAuthentication no
#   PermitRootLogin no
sudo systemctl restart sshd
```

---

## Шаг 3: Установка Python и зависимостей

```bash
# Подключись как deploy
ssh deploy@<IP_СЕРВЕРА>

# Установи Python 3.12 и инструменты
sudo apt install -y python3.12 python3.12-venv python3-pip git ufw

# Настрой файрвол
sudo ufw allow OpenSSH
sudo ufw allow 5050/tcp   # Dashboard
sudo ufw enable
```

---

## Шаг 4: Загрузка проекта на сервер

### Вариант A: через Git (рекомендуется)

На своём Mac — инициализируй git и запушь:
```bash
cd /Users/admin/Documents/Antigravity/FundingRate
git init
git add -A
git commit -m "Initial commit"

# Создай приватный репозиторий на GitHub/Gitea и запушь
git remote add origin git@github.com:<USER>/funding-bot.git
git push -u origin main
```

На сервере:
```bash
sudo mkdir -p /opt/funding-bot
sudo chown deploy:deploy /opt/funding-bot
git clone git@github.com:<USER>/funding-bot.git /opt/funding-bot
```

### Вариант B: через scp (без Git)

На своём Mac:
```bash
# Копируем проект (исключая venv и данные)
rsync -avz --exclude='venv' --exclude='__pycache__' --exclude='data/*.db' \
  /Users/admin/Documents/Antigravity/FundingRate/ \
  deploy@<IP_СЕРВЕРА>:/opt/funding-bot/
```

---

## Шаг 5: Настройка окружения на сервере

```bash
cd /opt/funding-bot

# Создай виртуальное окружение
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Создай .env с реальными ключами
cp .env.example .env
nano .env
# Заполни: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OKX ключи

# Защити файл с секретами
chmod 600 .env

# Создай директории
mkdir -p data logs data/heartbeats
```

---

## Шаг 6: Тестовый запуск

```bash
cd /opt/funding-bot
source venv/bin/activate

# Проверь что бот стартует
python main.py --mode scan

# Если всё ОК — видишь таблицу с Funding Rate
# Ctrl+C для остановки
```

---

## Шаг 7: Установка systemd-сервисов

```bash
# Скопируй unit-файлы
sudo cp deploy/funding-main.service /etc/systemd/system/
sudo cp deploy/funding-ws.service /etc/systemd/system/
sudo cp deploy/funding-tg.service /etc/systemd/system/
sudo cp deploy/funding-dashboard.service /etc/systemd/system/

# Перезагрузи systemd
sudo systemctl daemon-reload

# Включи автозапуск
sudo systemctl enable funding-main funding-ws funding-tg funding-dashboard

# Запусти все сервисы
sudo systemctl start funding-main
sudo systemctl start funding-ws
sudo systemctl start funding-tg
sudo systemctl start funding-dashboard
```

---

## Шаг 8: Проверка работы

```bash
# Статус всех сервисов
sudo systemctl status funding-main funding-ws funding-tg funding-dashboard

# Логи в реальном времени
journalctl -u funding-main -f          # Main daemon
journalctl -u funding-ws -f            # WebSocket daemon
journalctl -u funding-tg -f            # Telegram daemon

# Проверь Dashboard в браузере
# http://<IP_СЕРВЕРА>:5050

# Проверь Telegram-бота
# Отправь /status в Telegram
```

---

## Шаг 9: Мониторинг и обслуживание

### Полезные команды

```bash
# Перезапуск одного сервиса
sudo systemctl restart funding-main

# Перезапуск всех
sudo systemctl restart funding-main funding-ws funding-tg funding-dashboard

# Остановка всех
sudo systemctl stop funding-main funding-ws funding-tg funding-dashboard

# Просмотр логов за последний час
journalctl -u funding-ws --since "1 hour ago"

# Проверка healthcheck
curl http://localhost:5050/api/health
```

### Обновление кода

```bash
# Вариант A (Git):
cd /opt/funding-bot
git pull origin main
sudo systemctl restart funding-main funding-ws funding-tg funding-dashboard

# Вариант B (scp): с Mac
rsync -avz --exclude='venv' --exclude='data' --exclude='.env' \
  /Users/admin/Documents/Antigravity/FundingRate/ \
  deploy@<IP_СЕРВЕРА>:/opt/funding-bot/
ssh deploy@<IP_СЕРВЕРА> "sudo systemctl restart funding-main funding-ws funding-tg funding-dashboard"
```

---

## Возможные проблемы

| Проблема | Решение |
|----------|---------|
| `python3.12: command not found` | `sudo apt install software-properties-common && sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv` |
| Сервис падает сразу | `journalctl -u funding-main -n 50` — смотри ошибку |
| Dashboard не открывается | Проверь `sudo ufw status`, порт 5050 открыт? |
| Telegram бот не отвечает | Проверь TELEGRAM_TOKEN в .env, `journalctl -u funding-tg -f` |
| OKX API недоступен | Beget не блокирует OKX, но проверь: `curl https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` |
| Не хватает RAM | `free -h` — если <100MB свободно, увеличь тариф до 2GB |
