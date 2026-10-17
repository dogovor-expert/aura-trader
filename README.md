# Aura Trader

Торговая система на стыке прогнозного рынка и крипто-бирж:

**Polymarket (прогнозы) + BTC-рынок → LLM-оракул → мост → Hummingbot → биржи**

Оракул собирает вероятности по событиям Polymarket и цену BTC, прогоняет их через
каскад LLM-моделей (L1 + проверка L2), выдаёт сигнал LONG/SHORT/WAIT и отдаёт его
в Hummingbot, который ставит market-ордера на выбранных биржах. Есть веб-панель
мониторинга и защита от типичных ошибок (стоп-лоссы, риск-план, проверка чисел
модели, ограничения экспозиции).

## Структура

```
src/                  приложение оракула
  bridge.py           веб-мост (HTTP :8181) для сигналов и панели
  dashboard_data.py   сбор состояния для панели
  strategy_control.py управление стратегией и параметрами
  llm.py news.py pumps.py verify.py sizing.py analyst.py ...
  web/index.html      веб-панель
config/
  config.example.json образец конфига (скопировать в config.json)
  proxy.example.yaml  образец прокси
docs/                 AI-STRATEGIES-RESEARCH.md, LIVE-START.md
scripts/              selftest.py, compare_models.py, eval_signals.py,
                      backtest_threshold.py, backtest_scalp.py
tests/                тесты (pytest)
hummingbot/
  scripts/            кастомные стратегии Hummingbot (signal_poller, arbitrage, guard)
  conf/scripts/       их конфиги (YAML)
  tests/              тесты защит арбитража
```

## Быстрый старт

1. Python 3.10+ и Docker с Hummingbot (`hummingbot/hummingbot:latest`).
2. Конфиг и секреты:
   ```bat
   copy config\config.example.json config\config.json
   copy .env.example .env
   ```
   Заполни `config/config.json` (прокси, события Polymarket) и переменные окружения
   (ключи LLM, пароль keystore Hummingbot).
3. Зависимости и один цикл оракула:
   ```bat
   python -m venv venv
   venv\Scripts\python -m pip install -r requirements.txt
   venv\Scripts\python src\main.py --once
   ```
4. Мост и панель: `venv\Scripts\python src\bridge.py` → http://127.0.0.1:8181
   (панель сама встраивает токен; прямые запросы к `/api/*` и `/signal` требуют
   заголовок `X-Bridge-Token` — токен создаётся в `config/bridge_token.txt`).
5. Стратегия Hummingbot: файлы из `hummingbot/` скопировать в свой `D:\Hummingbot`
   (`scripts/`, `conf/scripts/`). По умолчанию всё работает в **paper trade**
   (демо-симулятор), реальные ордера — только после подключения боевых коннекторов с ключами.

Подробности боевого запуска — в `docs/LIVE-START.md`.

## Секреты

В репозиторий **не попадают**:

- `config/config.json`, `config/proxy.yaml` — содержат логин/пароль прокси;
- `config/bridge_token.txt` — токен доступа к мосту (создаётся автоматически);
- `.env` и переменные окружения (ключи LLM, `HBOT_PASSWORD`, `BRIDGE_TOKEN`);
- `logs/`, `data/` — рабочие данные (сделки, сигналы);
- ключи бирж Hummingbot (`conf/connectors/`), файлы пароля keystore.

Шаблоны — `config/config.example.json`, `config/proxy.example.yaml`, `.env.example`.
Секреты задаются локально и перечислены в `.gitignore`.

## Статус

Все 5 стратегий каталога реализованы и переключаемы из панели:

- **Сигнальная** (`multi_exchange_signal_poller`) — проверена на бумаге:
  вход, стоп/тейк, TTL устаревших сигналов, журнал сделок, P&L.
- **Funding Carry** (`funding_carry`) — проверен полный цикл: вход обеими
  ногами с подтверждением по событиям (~2 с), закрытие по кнопке, P&L.
- **Межбиржевой арбитраж** (`cross_exchange_arbitrage`) — защиты
  (`arbitrage_guard.py`, 37 тестов) проверены вживую: маршруты считаются,
  ложных входов нет; две ноги исполнялись только на бумаге.
- **Базисный арбитраж спот/перп** (`spot_perpetual_arbitrage`) — запускается,
  входит при basis ≥ 15 bps, выходит по схождению/стопу/time-force.
- **Памп-моментум** (`pump_momentum`) — детекция объёма 2×SMA(20) + 1% за бар
  с подтверждением второй биржей, ATR-стопы; свечной фид прогревается ~секунды.

Надёжность: мост следит за зависшими коннекторами и сам перезапускает
стратегию через 15 минут (watchdog); подтверждение исполнения ордеров — по
событиям Hummingbot (у коннекторов нет `get_order`).

> Торговля связана с риском потери средств. Это исследовательский проект, не
> инвестиционный совет. Запускай на реальные деньги только осознанно и на малые суммы.