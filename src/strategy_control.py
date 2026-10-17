# -*- coding: utf-8 -*-
"""Каталог стратегий и управление параметрами торговли из веб-панели.

Конфиг стратегии Hummingbot — плоский YAML, поэтому ключи обновляются
регулярками по строкам: это надёжнее, чем тянуть PyYAML в venv моста.

Стратегии делятся на:
  ready    — есть скрипт и конфиг, можно переключить прямо сейчас;
  planned  — спроектированы, но ещё не реализованы (переключение запрещено,
             чтобы в панели не было кнопок в пустоту).
"""
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUMMINGBOT = "D:/Hummingbot"
CONF_SCRIPTS = os.path.join(HUMMINGBOT, "conf", "scripts")
SCRIPTS = os.path.join(HUMMINGBOT, "scripts")
ACTIVE_CONF = os.path.join(CONF_SCRIPTS, "multi_exchange_signal_poller.yml")
DOCKER = r"D:\Docker\Desktop\resources\bin\docker.exe"
CONTAINER = "hummingbot"
HBOT_PASSWORD = "aura-trader"

STRATEGIES = [
    {
        "id": "multi_exchange_signal_poller",
        "name": "Мультибиржевой сигнальный",
        "status": "ready",
        "script": "multi_exchange_signal_poller.py",
        "config": "multi_exchange_signal_poller.yml",
        "summary": "Оракул оценивает рынок и отдаёт сигнал. Стратегия ставит market-ордер "
                   "на каждую подходящую биржу сразу, а не только на лучшую.",
        "details": [
            "Источник решения: L1/L2 модели оракула + эвристика (Polymarket, BTC-цена).",
            "Порог входа: conf не ниже min_confidence, только LONG/SHORT.",
            "Пауза между пачками: cooldown_sec, чтобы не входить слишком часто.",
            "Ограничения: max_concurrent_orders и max_total_exposure_quote.",
            "Размер одного ордера: order_quote (в USDT) на каждой бирже.",
        ],
        "risks": [
            "Работает в paper trade — реальных денег не двигает.",
            "Без стоп-лоссов: убыточная позиция удерживается, пока не сработает сигнал.",
            "При одновременной покупке на 9 биржах суммарный размер умножается на их число.",
        ],
    },
    {
        "id": "funding_carry",
        "name": "Funding Carry (нейтральный)",
        "status": "ready",
        "script": "funding_carry.py",
        "config": "funding_carry.yml",
        "summary": "Дельта-нейтральная позиция: спот BTC в лонг + перп в шорт. "
                   "Цена уже не важна — доход идёт от ставки финансирования.",
        "details": [
            "Источник решения: фид ставки финансирования (оракул пишет funding.json).",
            "Вход: ставка ≥ enter_funding_bps и базис спот/перп в пределах max_basis_bps.",
            "Выход: ставка ниже exit_funding_bps, базис за лимитом, аварийный стоп или закрыть всё.",
            "Входы лимитные (maker): комиссия ~0 вместо 5-10 bps taker.",
            "По истории OKX: средняя ставка 0.0042%/8ч = 4.58% APY, 85% периодов в плюс.",
        ],
        "risks": [
            "Доход не гарантирован: ставка может стать отрицательной.",
            "Нужен запас маржи на шорт-перпе: при росте BTC риск ликвидации.",
            "Basis risk: при закрытии спот и перп могут разойтись по цене.",
            "Paper trade не начисляет настоящее финансирование — доход проверяется только на боевом.",
        ],
    },
    {
        "id": "cross_exchange_arbitrage",
        "name": "Арбитраж между биржами",
        "status": "ready",
        "script": "cross_exchange_arbitrage.py",
        "config": "cross_exchange_arbitrage.yml",
        "summary": "Ищет расхождение цены одного актива между двумя биржами: покупает "
                   "дешевле, продаёт дороже, забирая разницу за вычетом комиссий.",
        "details": [
            "Пары маршрутов: bybit↔okx, okx/bitget, bybit/bitget (только paper-коннекторы).",
            "Вход только при чистой прибыли ≥ min_net_bps (по умолчанию 45 bps) — "
            "gross-спред минус комиссии обеих ног минус проскальзывание.",
            "Защиты (arbitrage_guard, 37 тестов): VWAP/глубина стакана, комиссии, "
            "подтверждение спреда, лимиты/429, закрытый кошелёк, сети, экономика "
            "ребаланса, разрыв одной ноги.",
            "Живой прогон: bybit→okx gross 0.5-1.1 bps, net −18.9 → пропуск "
            "(спред меньше комиссий) — ложных входов не было.",
            "Кулдаун/лимиты: max_concurrent_orders, request_per_minute 240.",
        ],
        "risks": [
            "Две ноги (покупка + продажа) вживую НЕ проверялись — при первом "
            "запуске следи за логом обеих ног и за риском разрыва (одна нога).",
            "Требует остатка на обеих биржах одновременно (paper-баланс есть).",
            "При несинхронных сделках остаётся открытая позиция — guard откатывает "
            "или фиксирует, но это самое уязвимое место.",
        ],
    },
    {
        "id": "spot_perpetual_arbitrage",
        "name": "Спот против перпетуала (базис)",
        "status": "ready",
        "script": "spot_perpetual_arbitrage.py",
        "config": "spot_perpetual_arbitrage.yml",
        "summary": "Покупает базис: лонг спот + шорт перп, когда перп дороже "
                   "спота больше порога; при схождении фиксирует разницу.",
        "details": [
            "basis = (перп − спот)/спот в bps; вход при basis >= entry_min_bps (15), "
            "выход при basis <= exit_target_bps (5), стоп при расширении на 40.",
            "PnL = схождение базиса + полученное финансирование − комиссии.",
            "Гейты перед входом: спред каждой ноги ≤ 50 bps, проскальзывание ≤ 25 bps, "
            "ставка финансирования ≥ 0 (иначе мы бы платили за шорт).",
            "Ноги по очереди, подтверждение исполнения — события Hummingbot; "
            "закрытие с ретраями и time force-close за сутки.",
            "Пороги иллюстративные (basis-funding-arbitrage-bot), не продакшн-тюнинг.",
        ],
        "risks": [
            "Разрыв одной ноги: если перп не открылся — позиция разворачивается "
            "автоматически, но до этого остаётся одинокий спот-лонг.",
            "Ликвидация шорт-перпа при ралле (держать плечо/размер низкими).",
            "Отрицательный funding: гейт ≥ 0 уменьшает, но не исключает убыток "
            "от удержания.",
            "Paper trade: реальное финансирование и проскальзывание не начисляются.",
        ],
    },
    {
        "id": "pump_momentum",
        "name": "Памп-моментум",
        "status": "ready",
        "script": "pump_momentum.py",
        "config": "pump_momentum.yml",
        "summary": "Входит на всплеске объёма с движением цены, подтверждённым "
                   "второй биржей; выходит по ATR-стопу/тейку.",
        "details": [
            "Детекция: volume > SMA(volume,20)×2.0 И |Δclose| ≥ 1% за бар "
            "(свечи okx_perpetual 1m, пороги из публичного pine-скрипта).",
            "Подтверждение: за 90с okx и bybit сдвинулись в том же направлении "
            "≥0.2% — одиночные всплески отсекаются.",
            "Направление: бычий памп → LONG, медвежий → SHORT (perp).",
            "Вход лимитником (maker), гейт спреда ≤ 50 bps; ATR фиксируется "
            "при входе: стоп 1×ATR, тейк 2×ATR (минимум 1:2).",
            "Выходы: стоп/тейк по ценам, time force-close 30 мин, аварийный "
            "стоп 3%, «закрыть всё» из панели.",
        ],
        "risks": [
            "Гонка за пампом: вход после 2× объёма часто по раздутой цене — "
            "мгновенный откат возможен (стоп 1×ATR и есть защита).",
            "Свечной фид прогревается ~30 минут (max_records=30) — до этого "
            "детекции нет.",
            "Пампы — самый волатильный участок; на реальном счёте нужен "
            "малый размер.",
            "Paper trade: проскальзывание и объёмы не реальные.",
        ],
    },
]

SCALARS = (
    "min_confidence",
    "order_quote",
    "cooldown_sec",
    "poll_interval_sec",
    "max_total_exposure_quote",
    "max_concurrent_orders",
)

SCALAR_META = {
    "min_confidence": {"label": "Порог уверенности", "min": 0.0, "max": 1.0, "step": 0.05,
                       "hint": "Ниже — сигнал с меньшей уверенностью тоже торгуется"},
    "order_quote": {"label": "Сумма ордера, USDT", "min": 0.1, "max": 100000, "step": 0.1,
                    "hint": "Размер одного market-ордера на каждой бирже"},
    "cooldown_sec": {"label": "Пауза между пачками, с", "min": 0, "max": 86400, "step": 30,
                     "hint": "Не входить чаще, чем раз в N секунд"},
    "poll_interval_sec": {"label": "Проверка сигнала, с", "min": 1, "max": 600, "step": 1,
                          "hint": "Как часто стратегия смотрит файл сигналов"},
    "max_total_exposure_quote": {"label": "Общий лимит, USDT", "min": 1, "max": 1000000, "step": 1,
                                 "hint": "Потолок суммы всех активных ордеров"},
    "max_concurrent_orders": {"label": "Максимум ордеров", "min": 1, "max": 50, "step": 1,
                              "hint": "Сколько бирж может участвовать в одной пачке"},
}

_run_lock = {"ts": 0.0, "result": None}


CONTROL_FILE = os.path.join(HUMMINGBOT, "data", "trading_control.json")
LOG_FILE = os.path.join(BASE, "logs", "bridge.log")


def _log(message: str):
    """Запись в лог моста (append, без падения при ошибке)."""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


def set_trading(enabled: bool, note: str = ""):
    """Переключатель торговли: система продолжает анализ, ордера ставятся
    только при trading_enabled=true. Отсутствие файла = торговля выключена."""
    payload = {
        "trading_enabled": bool(enabled),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": note or ("включено вручную" if enabled else "остановлено вручную"),
    }
    os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTROL_FILE)  # атомарно: стратегия никогда не прочитает полуфайл
    _log(f"trading_enabled={enabled} ({payload['note']})")
    return payload


def trading_state():
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"trading_enabled": False}
    except Exception:
        return {"trading_enabled": False}


def request_close_all(reason: str = "закрыть всё по кнопке"):
    """Просьба закрыть все открытые позиции по рынку.

    Флаг кладётся в тот же файл-переключатель: стратегура читает его на
    каждом тике и закрывает позиции рыночными ордерами. Торговля при этом
    может оставаться выключенной — выходы должны работать всегда.
    """
    state = trading_state()
    state["close_all_requested"] = datetime.now().astimezone().isoformat(timespec="seconds")
    state["close_all_reason"] = reason
    os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTROL_FILE)
    _log(f"close_all requested: {reason}")
    return state


CONNECTORS_DIR = os.path.join(HUMMINGBOT, "conf", "connectors")
PAPER_SUFFIX = "_paper_trade"


def connector_keys():
    """Какие боевые коннекторы уже подключены (есть файл с ключами).

    Hummingbot хранит подключённый коннектор как conf/connectors/<name>.yml.
    Пока ключей нет, переключение в реальные деньги бессмысленно и опасно.
    """
    try:
        return {fn[:-4] for fn in os.listdir(CONNECTORS_DIR) if fn.endswith(".yml")}
    except OSError:
        return set()


def _swap_connector(name, mode):
    name = str(name).strip()
    if mode == "demo":
        return name if name.endswith(PAPER_SUFFIX) else name + PAPER_SUFFIX
    if name.endswith(PAPER_SUFFIX):
        return name[: -len(PAPER_SUFFIX)]
    return name


def _rewrite_mode_lines(lines, mode):
    """Заменить имена коннекторов под режим.

    Ловим любые поля-*connector (connector_name, spot_connector, perp_connector)
    и элементы exchanges (- name: ...).
    """
    changed = 0
    for i, line in enumerate(lines):
        m = re.match(r"^([a-z_]*connector[a-z_]*):\s*(\S+)\s*$", line)
        if not m:
            m = re.match(r"^(\s*-\s*name):\s*(\S+)\s*$", line)
        if m:
            new = _swap_connector(m.group(2), mode)
            if new != m.group(2):
                prefix = m.group(1)
                lines[i] = f"{prefix}: {new}\n"
                changed += 1
    return changed


def active_conf_name():
    """Имя yml реально запущенной стратегии (фолбэк — первая ready)."""
    act = active_strategy()
    if act:
        for s in STRATEGIES:
            if s["id"] == act and s.get("config"):
                return s["config"]
    for s in STRATEGIES:
        if s["status"] == "ready" and s.get("config") and \
                os.path.exists(os.path.join(CONF_SCRIPTS, s["config"])):
            return s["config"]
    return os.path.basename(ACTIVE_CONF)


def active_conf_path():
    return os.path.join(CONF_SCRIPTS, active_conf_name())


def set_trade_mode(mode):
    """Тумблер «Демо / Реальные деньги»: правит имена коннекторов и перезапускает
    стратегию. Демо = добавляет _paper_trade, реально = убирает его.

    Переключение в реальные деньги разрешено только при наличии хотя бы одного
    подключённого боевого коннектора — иначе боевые ордера уйдут в пустоту.
    """
    conf = active_conf_path()
    mode = str(mode or "").lower()
    if mode not in ("demo", "live"):
        return {"ok": False, "error": "режим должен быть demo или live"}
    if not os.path.exists(conf):
        return {"ok": False, "error": "конфиг стратегии не найден"}
    if mode == "live" and not connector_keys():
        return {"ok": False,
                "error": "нет ни одного API-ключа — сначала добавь боевой коннектор "
                         "(hbot connect bybit), иначе реальные ордера уйдут в пустоту"}
    shutil.copy2(conf, conf + ".bak-mode")
    with open(conf, encoding="utf-8") as f:
        lines = f.readlines()
    changed = _rewrite_mode_lines(lines, mode)
    if not changed:
        return {"ok": True, "mode": mode, "changed": 0, "note": "уже в этом режиме"}
    with open(conf, "w", encoding="utf-8") as f:
        f.writelines(lines)
    _log(f"trade_mode -> {mode} (изменено коннекторов: {changed})")
    _push_config()
    stop = _hbot("stop --force", timeout=120)
    time.sleep(12)
    start = _hbot(f"start {active_conf_name()} --timeout 240", timeout=420)
    return {"ok": bool(start.get("ok")), "mode": mode, "changed": changed, "stop": stop, "start": start}


def read_scalars(path=ACTIVE_CONF):
    """Текущие значения плоских ключей конфига."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^([a-z_]+):\s*(\S+)\s*$", line)
                if m and m.group(1) in SCALARS:
                    try:
                        out[m.group(1)] = float(m.group(2)) if "." in m.group(2) else int(m.group(2))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def write_scalars(values, path=ACTIVE_CONF):
    """Точечное обновление ключей; перед записью — бэкап."""
    if not values:
        return False
    shutil.copy2(path, path + ".bak-panel")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([a-z_]+):\s*(\S+)\s*$", line)
        if m and m.group(1) in values:
            key = m.group(1)
            raw = values[key]
            if isinstance(raw, float) and raw.is_integer():
                raw = int(raw)
            lines[i] = f"{key}: {raw}\n"
            seen.add(key)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return sorted(seen)


def active_strategy():
    """Что реально запущено: свежесть лога стратегии (logs_<script>.log).

    Раньше возвращалась первая ready-стратегия из каталога — панель показывала
    «активной» сигнальную даже когда крутится funding_carry.
    """
    now = time.time()
    for s in STRATEGIES:
        script = s.get("script")
        if not script or s.get("status") != "ready":
            continue
        log_path = os.path.join(HUMMINGBOT, "logs", "logs_" + script[:-3] + ".log")
        try:
            if now - os.path.getmtime(log_path) < 90:
                return s["id"]
        except OSError:
            continue
    # Ни один лог не свежий — фолбэк на каталог.
    for s in STRATEGIES:
        if s["status"] == "ready" and os.path.exists(os.path.join(CONF_SCRIPTS, s["config"])):
            return s["id"]
    return None


def catalog():
    values = read_scalars()
    return {
        "active": active_strategy(),
        "trading": trading_state(),
        "settings": values,
        "settings_meta": {k: SCALAR_META[k] for k in values if k in SCALAR_META},
        "strategies": STRATEGIES,
        "presets_order_quote": [1, 5, 10, 25, 50, 100],
        "container": os.path.exists(DOCKER),
    }


def _hbot(action, timeout=300):
    if not os.path.exists(DOCKER):
        return {"ok": False, "error": "docker не найден"}
    cmd = [
        DOCKER, "exec", CONTAINER, "bash", "-lc",
        "source /opt/conda/etc/profile.d/conda.sh && conda activate hummingbot && "
        f"export HBOT_PASSWORD={HBOT_PASSWORD} && hbot {action}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (res.stdout or "") + (res.stderr or "")
        return {"ok": res.returncode == 0, "output": out.strip()[-800:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "таймаут"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _push_config():
    """Перелить все конфиги стратегий в контейнер (fs смонтирован, но на всякий
    случай синхронизируем — панель может править файлы на хосте)."""
    for s in STRATEGIES:
        name = s.get("config")
        if not name:
            continue  # у planned-стратегий конфига ещё нет
        src = os.path.join(CONF_SCRIPTS, name)
        if os.path.exists(src):
            subprocess.run([DOCKER, "cp", src, f"{CONTAINER}:/home/hummingbot/conf/scripts/{name}"],
                           capture_output=True, text=True, timeout=90)


def apply_settings(values):
    """Сохранить параметры, перелить конфиг в контейнер и перезапустить стратегию."""
    clean = {}
    for key, meta in SCALAR_META.items():
        if key in values:
            try:
                num = float(values[key])
            except (TypeError, ValueError):
                return {"ok": False, "error": f"поле {key}: нужно число"}
            num = max(meta["min"], min(meta["max"], num))
            clean[key] = int(num) if float(num).is_integer() else num
    if not clean:
        return {"ok": False, "error": "нечего применять"}
    written = write_scalars(clean)
    _push_config()
    stop = _hbot("stop --force", timeout=120)
    time.sleep(12)
    start = _hbot(f"start {active_conf_name()} --timeout 240", timeout=420)
    return {"ok": bool(start.get("ok")), "written": written, "stop": stop, "start": start}


def switch_strategy(strategy_id):
    """Переключение стратегии. Разрешены только реализованные (ready)."""
    for s in STRATEGIES:
        if s["id"] != strategy_id:
            continue
        if s["status"] != "ready":
            return {"ok": False,
                    "error": f"«{s['name']}» ещё не реализована — переключение заблокировано"}
        script = os.path.join(SCRIPTS, s["script"])
        conf = os.path.join(CONF_SCRIPTS, s["config"])
        if not (os.path.exists(script) and os.path.exists(conf)):
            return {"ok": False, "error": "нет файлов стратегии"}
        subprocess.run([DOCKER, "cp", script, f"{CONTAINER}:/home/hummingbot/scripts/{s['script']}"],
                       capture_output=True, text=True, timeout=90)
        _push_config()
        _hbot("stop --force", timeout=120)
        time.sleep(12)
        res = _hbot(f"start {s['config']} --timeout 240", timeout=420)
        return {"ok": bool(res.get("ok")), "strategy": s["name"], "start": res}
    return {"ok": False, "error": "неизвестная стратегия"}


if __name__ == "__main__":
    import json
    print(json.dumps(catalog(), ensure_ascii=False, indent=2))