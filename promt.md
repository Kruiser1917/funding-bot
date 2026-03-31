Ты — senior Python разработчик и DevOps инженер.
Твоя задача: создать торгового бота для арбитража
Funding Rate на бирже OKX.

═══════════════════════════════════════════════
КОНТЕКСТ ПРОЕКТА
═══════════════════════════════════════════════

Стратегия: Funding Rate Arbitrage

- Покупаем крипту на споте
- Одновременно открываем SHORT на фьючерс той же монеты
- Получаем Funding Rate каждые 8 часов (00:00, 08:00, 16:00 UTC)
- Цена монеты не важна — позиция нейтральна к рынку
- Зарабатываем только на разнице Funding Rate

Биржа: OKX (okx.com)
API: read-only на старте (потом добавим торговые права)
Режим: Paper Trading (симуляция) до пополнения депозита

═══════════════════════════════════════════════
ТЕХНИЧЕСКИЙ СТЕК
═══════════════════════════════════════════════

Язык: Python 3.11+
Библиотеки:

- okx-sdk-api       (официальный SDK OKX)
- python-dotenv     (переменные окружения)
- requests          (HTTP запросы)
- pandas            (работа с данными)
- tabulate          (красивые таблицы в консоли)
- schedule          (планировщик задач)
- python-telegram-bot==20.7 (уведомления)
- asyncio           (асинхронность)

═══════════════════════════════════════════════
СТРУКТУРА ПРОЕКТА
═══════════════════════════════════════════════

okx-funding-bot/
├── .env                  ← секреты (не в git!)
├── .gitignore
├── requirements.txt
├── config.py             ← загрузка всех настроек
├── main.py               ← точка входа, главный цикл
├── data/
│   └── positions.json    ← хранилище позиций (создаётся автоматически)
└── src/
    ├── __init__.py
    ├── funding.py        ← получение Funding Rate с OKX API
    ├── simulator.py      ← paper trading (симуляция сделок)
    ├── notifier.py       ← Telegram уведомления
    └── reporter.py       ← отчёты, статистика, вывод в консоль

═══════════════════════════════════════════════
ДЕТАЛЬНОЕ ОПИСАНИЕ КАЖДОГО МОДУЛЯ
═══════════════════════════════════════════════

[config.py]

- Загружает все переменные из .env через python-dotenv
- Класс Config со статическими полями:
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  TG_TOKEN, TG_CHAT_ID
  MIN_FUNDING_RATE (минимальный rate для входа, default=0.03%)
  SIMULATION_CAPITAL (стартовый капитал симуляции, default=1000 USD)
  FUNDING_INTERVAL_HOURS = 8
  SYMBOLS = список из 10 популярных свопов:
    BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP,
    BNB-USDT-SWAP, XRP-USDT-SWAP, DOGE-USDT-SWAP,
    ADA-USDT-SWAP, AVAX-USDT-SWAP, MATIC-USDT-SWAP,
    LINK-USDT-SWAP

[src/funding.py]
Функции (все используют только публичные endpoints OKX — без авторизации):
  get_funding_rate(symbol) → dict
    Поля: symbol, rate, rate_pct, next_rate, next_rate_pct,
          annual_pct (rate *3* 365 * 100), fund_time, ts

  get_all_rates(symbols) → DataFrame
    Возвращает таблицу по всем монетам
    Отсортировано по annual_pct по убыванию
    Добавить колонку "signal":
      "🔥 ENTER" если annual_pct > 20
      "👀 WATCH" если annual_pct > 10
      "😴 SKIP"  иначе

  get_funding_history(symbol, limit=90) → DataFrame
    История за последние 90 периодов
    Добавить: avg_rate_pct, avg_annual_pct,
              positive_ratio (% положительных периодов),
              min_rate_pct, max_rate_pct

  get_spot_price(symbol) → float
    symbol: BTC-USDT-SWAP → запрос по BTC-USDT

  get_market_summary() → DataFrame
    Вызывает get_all_rates() + для каждой монеты
    из топ-3 по rate вызывает get_funding_history()
    Возвращает расширенную таблицу с историческими данными

[src/simulator.py]
Dataclass Position:
  symbol, entry_price, size_usd, size_coin,
  opened_at, funding_earned=0.0, funding_count=0,
  status="open", closed_at=None, pnl=0.0,
  commissions=0.0

Класс PaperTrader:
  __init__(capital: float)
    - Загружает positions.json если существует
    - Иначе создаёт пустое состояние

  open_position(symbol, usd_amount) → Position
    - Проверяет: нет ли уже открытой позиции по этому symbol
    - Проверяет: достаточно ли баланса
    - Получает текущую цену через get_spot_price()
    - Считает комиссию входа: size_usd * 0.001 (0.1% — спот + фьючерс)
    - Сохраняет позицию
    - Уменьшает баланс

  apply_funding() → dict {symbol: earned_usd}
    - Для каждой открытой позиции получает текущий rate
    - Начисляет: earned = size_usd * rate
    - Обновляет funding_earned, funding_count
    - Увеличивает баланс
    - Возвращает словарь с начислениями

  close_position(symbol) → Position
    - Считает комиссию закрытия: size_usd * 0.001
    - pnl = funding_earned - commissions_total
    - Возвращает капитал на баланс
    - Сохраняет результат

  summary() → dict
    Поля:
      capital, balance, free_balance,
      total_funding_earned, total_pnl,
      total_commissions, roi_pct,
      open_positions_count, closed_trades_count,
      days_running (от первой сделки до сейчас),
      projected_monthly (на основе среднего дневного заработка),
      projected_annual

  get_position_detail(symbol) → dict
    Детали позиции + текущий unrealized funding

[src/notifier.py]
Класс Notifier:

- Если TG_TOKEN не задан → выводит всё в консоль (не падает с ошибкой)
- Все методы работают синхронно (asyncio.run внутри)
  
  Методы:
  send(text)                     — базовая отправка HTML
  funding_report(df)             — таблица всех rate с сигналами
  position_opened(pos)           — уведомление об открытии
  funding_applied(results, balance) — сводка начислений за период
  position_closed(pos)           — итог закрытой позиции
  daily_report(summary)          — ежедневная сводка
  alert_high_rate(symbol, annual_pct) — алерт о высоком rate

[src/reporter.py]
Функции для красивого вывода в консоль:
  print_rates_table(df)
    Таблица с колонками:
    Монета | Rate% | Следующий% | Год% | Сигнал | Время выплаты

  print_history_stats(symbol, df)
    Статистика истории:
    Среднее, мин, макс, % положительных периодов
    Мини-график последних 10 значений (ASCII)

  print_portfolio(trader)
    Текущий портфель:
    Баланс | Открытые позиции | Заработано | ROI%
    Таблица открытых позиций с деталями

  print_summary(summary)
    Полная сводка с прогнозом

[main.py]
Режимы запуска через аргументы командной строки:

  python main.py --mode scan
    Показывает текущие Funding Rate по всем монетам
    Выводит топ-3 кандидата для входа с историей
    Отправляет отчёт в Telegram

  python main.py --mode status
    Показывает текущий портфель и статистику

  python main.py --mode open --symbol BTC-USDT-SWAP --amount 500
    Открывает симулированную позицию

  python main.py --mode close --symbol BTC-USDT-SWAP
    Закрывает симулированную позицию

  python main.py --mode funding
    Вручную применяет funding (для тестов)
    В реальном режиме вызывается по расписанию

  python main.py --mode history --symbol BTC-USDT-SWAP
    Показывает историю Funding Rate с аналитикой

  python main.py --mode daemon
    Запускает в фоновом режиме:
    - Каждый час: сканирует rate, алертит если высокий
    - Каждые 8 часов (00:05, 08:05, 16:05 UTC): apply_funding()
    - Каждый день в 09:00 МСК: daily_report()
    - Каждые 5 минут: проверяет не стал ли rate отрицательным

═══════════════════════════════════════════════
ВАЖНЫЕ ТРЕБОВАНИЯ К КОДУ
═══════════════════════════════════════════════

1. Все API запросы через try/except с понятными сообщениями об ошибках
2. Логирование в файл logs/bot.log (ротация, макс 10MB, 5 файлов)
3. Все суммы округлять до 4 знаков после запятой
4. Комментарии на русском языке
5. Никаких hardcoded значений — всё через config.py
6. positions.json создаётся автоматически если не существует
7. При запуске main.py — всегда показывать текущий баланс симуляции

═══════════════════════════════════════════════
ПРИМЕР ВЫВОДА В КОНСОЛЬ (ориентир для дизайна)
═══════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  OKX Funding Rate Bot | Paper Trading Mode
  Баланс: $1,000.00 | Позиций: 2 | ROI: +2.34%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 ТЕКУЩИЕ FUNDING RATES
┌──────────────────┬────────┬──────────┬──────────┬────────────┐
│ Монета           │ Rate%  │ След.%   │ Год%     │ Сигнал     │
├──────────────────┼────────┼──────────┼──────────┼────────────┤
│ BTC-USDT-SWAP    │ 0.0821 │ 0.0750   │  90.1%   │ 🔥 ENTER   │
│ ETH-USDT-SWAP    │ 0.0500 │ 0.0480   │  54.8%   │ 🔥 ENTER   │
│ SOL-USDT-SWAP    │ 0.0210 │ 0.0190   │  23.0%   │ 👀 WATCH   │
│ DOGE-USDT-SWAP   │ 0.0050 │ 0.0040   │   5.5%   │ 😴 SKIP    │
└──────────────────┴────────┴──────────┴──────────┴────────────┘

═══════════════════════════════════════════════
ПЕРВЫЙ ЗАПУСК
═══════════════════════════════════════════════

После создания всех файлов:

1. pip install -r requirements.txt
2. Заполнить .env файл
3. python main.py --mode scan   ← проверить что всё работает
4. python main.py --mode open --symbol BTC-USDT-SWAP --amount 500
5. python main.py --mode status
6. python main.py --mode daemon  ← запустить основной цикл

Создай все файлы сразу, полностью, готовые к запуску.
Не используй заглушки и TODO комментарии — только рабочий код.
