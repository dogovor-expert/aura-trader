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
HBOT_PASSWORD = os.getenv("HBOT_PASSWORD", "")

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
        "id": "cross_exchange_arbitrage",
        "name": "Арбитраж между биржами",
        "status": "planned",
        "summary": "Ищет расхождение цены одного актива между двумя биржами: покупает "
                   "дешевле, продаёт дороже, забирая разницу.",
        "details": [
            "Построен на встроенном ArbitrageExecutor Hummingbot.",
            "Порог прибыли задаётся в процентах и должен перекрывать комиссии обеих бирж.",
            "Требует остатка на обеих биржах: покупка и продажа идут одновременно.",
            "Отдельная стратегия, запускается вместо сигнальной.",
        ],
        "risks": [
            "Не реализован: переключение заблокировано, кнопка в панели неактивна.",
            "При несинхронных сделках остаётся открытая позиция — нужен контроль.",
        ],
    },
    {
        "id": "spot_perpetual_arbitrage",
        "name": "Спот против перпетуала",
        "status": "planned",
        "summary": "Держит спот и шорт на перпетуале, если фандинг положительный: "
                   "собирает переплату за держание позиции.",
        "details": [
            "Встроенная стратегия Hummingbot spot_perpetual_arbitrage.",
            "Работает на одной паре, где есть и спот, и бессрочный контракт.",
            "Прибыль зависит от ставки фандинга, а не от направления цены.",
        ],
        "risks": [
            "Не реализован: переключение заблокировано.",
            "Риск ликвидации по перпетуалу при резком движении цены.",
        ],
    },
    {
        "id": "pump_momentum",
        "name": "Поиск пампов",
        "status": "planned",
        "summary": "Сканирует биржи на резкие движения и всплески объёма, чтобы "
                   "войти в начало роста на малом капитале.",
        "details": [
            "Отдельный сканер движений: рост цены за окно + аномалия объёма.",
            "Подтверждает движение второй биржей, чтобы отсечь одиночные всплески.",
            "Заявка уходит в тот же сигнальный файл, что читает действующая стратегия.",
        ],
        "risks": [
            "Не реализован: переключение заблокировано.",
            "Пампы — самый волатильный участок; без стоп-лосса убыток быстрый.",
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
    """Заменить имена коннекторов (connector_name и элементы exchanges) под режим."""
    changed = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(connector_name):\s*(\S+)\s*$", line)
        if not m:
            m = re.match(r"^(\s*-\s*name):\s*(\S+)\s*$", line)
        if m:
            new = _swap_connector(m.group(2), mode)
            if new != m.group(2):
                prefix = m.group(1)
                lines[i] = f"{prefix}: {new}\n"
                changed += 1
    return changed


def set_trade_mode(mode):
    """Тумблер «Демо / Реальные деньги»: правит имена коннекторов и перезапускает
    стратегию. Демо = добавляет _paper_trade, реально = убирает его.

    Переключение в реальные деньги разрешено только при наличии хотя бы одного
    подключённого боевого коннектора — иначе боевые ордера уйдут в пустоту.
    """
    mode = str(mode or "").lower()
    if mode not in ("demo", "live"):
        return {"ok": False, "error": "режим должен быть demo или live"}
    if not os.path.exists(ACTIVE_CONF):
        return {"ok": False, "error": "конфиг стратегии не найден"}
    if mode == "live" and not connector_keys():
        return {"ok": False,
                "error": "нет ни одного API-ключа — сначала добавь боевой коннектор "
                         "(hbot connect bybit), иначе реальные ордера уйдут в пустоту"}
    shutil.copy2(ACTIVE_CONF, ACTIVE_CONF + ".bak-mode")
    with open(ACTIVE_CONF, encoding="utf-8") as f:
        lines = f.readlines()
    changed = _rewrite_mode_lines(lines, mode)
    if not changed:
        return {"ok": True, "mode": mode, "changed": 0, "note": "уже в этом режиме"}
    with open(ACTIVE_CONF, "w", encoding="utf-8") as f:
        f.writelines(lines)
    _log(f"trade_mode -> {mode} (изменено коннекторов: {changed})")
    _push_config()
    stop = _hbot("stop --force", timeout=120)
    time.sleep(12)
    start = _hbot("start multi_exchange_signal_poller.yml --timeout 240", timeout=420)
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
    for name in ("multi_exchange_signal_poller.yml",):
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
    start = _hbot("start multi_exchange_signal_poller.yml --timeout 240", timeout=420)
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