# -*- coding: utf-8 -*-
"""Сбор состояния системы для веб-панели.

Читает только локальные файлы (никаких сетевых вызовов), чтобы панель
отдавалась мгновенно и не зависела от доступности бирж.

Запуск отдельно: python src/dashboard_data.py   (печатает JSON в консоль)
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
LOGS = os.path.join(BASE, "logs")
CFG = os.path.join(BASE, "config", "config.json")
HISTORY = os.path.join(DATA, "history.json")
PRICE = os.path.join(DATA, "last_price.json")
SIGNALS_LOG = os.path.join(LOGS, "signals.log")
HUMMINGBOT = "D:/Hummingbot"
HB_SIGNALS = os.path.join(HUMMINGBOT, "data", "signals.json")
HB_STRATEGY_CFG = os.path.join(HUMMINGBOT, "conf", "scripts", "multi_exchange_signal_poller.yml")
DOCKER = r"D:\Docker\Desktop\resources\bin\docker.exe"
HUMMINGBOT_HOST = "D:/Hummingbot"
CONTAINER = "hummingbot"

_docker_cache = {"ts": 0.0, "value": None}
_orders_cache = {"ts": 0.0, "value": None}
STRATEGY_LOG = "hummingbot_logs_dir"  # заполняется в orders()

ORDER_RE = re.compile(
    r"^(?P<ts>[\d\-: ,]+)\s+-\s+\d+\s+-\s+.*?-\s+(?P<verb>BUY|SELL)\s+(?P<amount>[\d.]+)\s+"
    r"(?P<pair>\S+)\s+@~(?P<price>[\d.]+)\s+on\s+(?P<exchange>\S+)"
)
EXEC_RE = re.compile(r"signal executed on (?P<n>\d+) exchange\(s\): (?P<list>.+)")


def orders(limit=12):
    """Последние ордера стратегии: сколько на каких биржах."""
    now = time.time()
    if _orders_cache["value"] is not None and now - _orders_cache["ts"] < 60:
        return _orders_cache["value"]
    value = {"source": "unavailable", "recent": [], "per_exchange": {}, "last_batch": None, "batches": 0}
    if os.path.exists(DOCKER):
        try:
            res = subprocess.run(
                [DOCKER, "exec", CONTAINER, "bash", "-c",
                 "tail -n 400 /home/hummingbot/logs/logs_multi_exchange_signal_poller.log 2>/dev/null"],
                capture_output=True, text=True, timeout=20,
            )
            lines = (res.stdout or "").splitlines()
            per = {}
            recent = []
            for line in lines:
                m = EXEC_RE.search(line)
                if m:
                    value["batches"] += 1
                    value["last_batch"] = {"ts": line[:23].strip(), "count": int(m.group("n")),
                                           "exchanges": [x.strip() for x in m.group("list").split(",")]}
                m = ORDER_RE.search(line.strip())
                if m:
                    ex = m.group("exchange")
                    per[ex] = per.get(ex, 0) + 1
                    recent.append({
                        "ts": m.group("ts").strip()[-8:],
                        "side": m.group("verb"),
                        "pair": m.group("pair"),
                        "amount": m.group("amount"),
                        "price": m.group("price"),
                        "exchange": ex,
                    })
            recent.reverse()
            value = {"source": "log", "recent": recent[:limit], "per_exchange": per,
                     "last_batch": value["last_batch"], "batches": value["batches"]}
        except Exception as e:
            value = {"source": "error", "error": str(e)[:120], "recent": [], "per_exchange": {},
                     "last_batch": None, "batches": 0}
    _orders_cache["ts"] = now
    _orders_cache["value"] = value
    return value


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _mtime(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except Exception:
        return None


def _age_seconds(path):
    try:
        return int(time.time() - os.path.getmtime(path))
    except Exception:
        return None


CYCLE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+verdict=(?P<verdict>\S+)\s+BTC=(?P<btc>[\d.]+)\s*\((?P<chg>-?[\d.]+)%\)")
L1_RE = re.compile(r"LLM L1:\s+(?P<sig>\S+)\s+conf=(?P<conf>[\d.]+)(?:\s+regime=(?P<regime>\S+))?(?:\s+edge=(?P<edge>-?[\d.]+)bps)?")
L2_RE = re.compile(r"LLM L2:\s+(?P<sig>\S+)\s+conf=(?P<conf>[\d.]+)")
FINAL_RE = re.compile(r"FINAL:\s+(?P<sig>\S+)\s+conf=(?P<conf>[\d.]+)")


def recent_cycles(limit=8):
    """Разбор последних циклов из signals.log."""
    try:
        with open(SIGNALS_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    cycles = []
    cur = None
    for line in lines:
        m = CYCLE_RE.match(line.strip())
        if m:
            if cur:
                cycles.append(cur)
            cur = {
                "ts": m.group("ts"),
                "verdict": m.group("verdict"),
                "btc": float(m.group("btc")),
                "chg": float(m.group("chg")),
                "l1": None,
                "l2": None,
                "final": None,
                "note": None,
            }
            continue
        if cur is None:
            continue
        s = line.strip()
        m1 = L1_RE.search(s)
        if m1:
            cur["l1"] = {
                "signal": m1.group("sig"),
                "conf": float(m1.group("conf")),
                "regime": m1.group("regime"),
                "edge": m1.group("edge"),
            }
            continue
        mf = FINAL_RE.search(s)
        if mf:
            cur["final"] = {"signal": mf.group("sig"), "conf": float(mf.group("conf"))}
            continue
        m2 = L2_RE.search(s)
        if m2:
            cur["l2"] = {"signal": m2.group("sig"), "conf": float(m2.group("conf"))}
            continue
        if s.startswith("note:"):
            cur["note"] = s[5:].strip()
    if cur:
        cycles.append(cur)
    cycles.reverse()
    return cycles[:limit]


def markets():
    """Открытые рынки, сгруппированные по событиям."""
    hist = _read_json(HISTORY, {}) or {}
    groups = []
    for label, ev in hist.items():
        rows = []
        for m in (ev.get("markets") or []):
            if m.get("_resolved"):
                continue
            prob = m.get("yes_prob")
            if prob is None:
                continue
            rows.append(
                {
                    "question": m.get("question", ""),
                    "prob": round(float(prob), 4),
                    "volume": m.get("volume"),
                    "slug": m.get("slug"),
                }
            )
        rows.sort(key=lambda r: -r["prob"])
        groups.append(
            {
                "label": label,
                "title": ev.get("title", label),
                "open": len(rows),
                "total": len(ev.get("markets") or []),
                "markets": rows[:8],
            }
        )
    return groups


def strategy_exchanges():
    """Список бирж из YAML стратегии (без зависимости от PyYAML)."""
    try:
        with open(HB_STRATEGY_CFG, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception:
        return []
    out = []
    block = raw.split("exchanges:", 1)
    if len(block) < 2:
        return out
    body = block[1]
    for chunk in re.split(r"\n\s{2}-\s", body):
        chunk = chunk.strip()
        if not chunk.startswith("name:"):
            continue
        item = {}
        for key in ("name", "is_perpetual", "enabled", "priority", "min_spread_bps", "max_position_quote"):
            m = re.search(rf"{key}:\s*(\S+)", chunk)
            if m:
                val = m.group(1).strip()
                if val in ("true", "false"):
                    item[key] = val == "true"
                else:
                    try:
                        item[key] = float(val) if "." in val else int(val)
                    except ValueError:
                        item[key] = val
        if item.get("name"):
            out.append(item)
    return out


def trade_mode():
    """Демо или реальные деньги: по именам коннекторов активной стратегии.

    Hummingbot считает бумажный коннектор (bybit_paper_trade) отдельным от
    боевого (bybit). Коннектор без суффикса _paper_trade шлёт ордера на биржу
    с реального баланса — поэтому имя коннектора и есть источник правды.
    """
    enabled = [e for e in strategy_exchanges() if e.get("enabled", True)]
    paper = [e["name"] for e in enabled if str(e.get("name", "")).endswith("_paper_trade")]
    live = [e["name"] for e in enabled if not str(e.get("name", "")).endswith("_paper_trade")]
    if not enabled:
        mode, label = "unknown", "неизвестно"
    elif live and paper:
        mode, label = "mixed", "СМЕШАННЫЙ РЕЖИМ"
    elif live:
        mode, label = "live", "РЕАЛЬНЫЕ ДЕНЬГИ"
    else:
        mode, label = "demo", "ДЕМО — бумажная торговля"
    note = {
        "demo": "Ордера идут в симулятор Hummingbot. Реальный баланс не тратится.",
        "live": "Ордера уходят на биржи и тратят реальный баланс.",
        "mixed": "Часть бирж боевые — реальные деньги в игре.",
        "unknown": "Не удалось прочитать конфиг стратегии.",
    }[mode]
    conn_dir = os.path.join(HUMMINGBOT, "conf", "connectors")
    have = set()
    try:
        have = {fn[:-4] for fn in os.listdir(conn_dir) if fn.endswith(".yml")}
    except OSError:
        pass
    targets = sorted({n[: -len("_paper_trade")] if n.endswith("_paper_trade") else n
                      for n in [e["name"] for e in enabled]})
    return {
        "mode": mode,
        "label": label,
        "note": note,
        "paper": paper,
        "live": live,
        "paper_count": len(paper),
        "live_count": len(live),
        "live_targets": targets,
        "keys_ready": sorted(n for n in targets if n in have),
    }


def docker_state():
    """Статус контейнера Hummingbot (кэш 30 секунд)."""
    now = time.time()
    if _docker_cache["value"] is not None and now - _docker_cache["ts"] < 30:
        return _docker_cache["value"]
    value = {"available": False, "status": "unknown", "container": CONTAINER}
    if os.path.exists(DOCKER):
        try:
            res = subprocess.run(
                [DOCKER, "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}|{{.Status}}"],
                capture_output=True, text=True, timeout=12,
            )
            line = (res.stdout or "").strip().splitlines()
            if line:
                name, _, status = line[0].partition("|")
                value = {"available": True, "status": status.strip(), "container": name.strip()}
            else:
                value = {"available": True, "status": "not running", "container": CONTAINER}
        except Exception as e:
            value = {"available": False, "status": f"error: {str(e)[:80]}", "container": CONTAINER}
    else:
        value = {"available": False, "status": "docker не найден", "container": CONTAINER}
    _docker_cache["ts"] = now
    _docker_cache["value"] = value
    return value


def models_info():
    """Активные LLM-модели из config + их последние вердикты из лога."""
    cfg = _read_json(CFG, {}) or {}
    llm = cfg.get("llm") or {}
    cycles = recent_cycles(limit=40)
    l1 = next((c.get("l1") for c in cycles if c.get("l1")), None)
    l2 = next((c.get("l2") for c in cycles if c.get("l2")), None)
    last = cycles[0] if cycles else None
    prov = llm.get("provider") or "—"
    names = {"groq": "Groq", "anymodel": "anymodel.org", "openai": "OpenAI",
             "anthropic": "Anthropic", "deepseek": "DeepSeek", "gemini": "Gemini",
             "openrouter": "OpenRouter"}
    return {
        "provider": prov,
        "provider_label": names.get(prov, prov),
        "l1": {"model": llm.get("l1_model") or "—", "verdict": l1},
        "l2": {"model": (llm.get("l2_model") or "—") if llm.get("l2_enabled") else "выключена",
               "enabled": bool(llm.get("l2_enabled")),
               "trigger": llm.get("l2_trigger_conf"),
               "verdict": l2},
        "trade_conf": llm.get("trade_conf"),
        "final": (last or {}).get("final"),
        "ts": (last or {}).get("ts"),
    }


def news_block():
    """Свежие новости: тональность, срочные и последние заголовки."""
    try:
        import news as news_mod
        data = news_mod.load()
    except Exception as e:
        return {"available": False, "error": str(e)[:120], "items": [], "sentiment": {}}
    items = data.get("items") or []
    return {
        "available": bool(items),
        "updated_at": data.get("updated_at"),
        "sentiment": data.get("sentiment", {}),
        "urgent": data.get("urgent", [])[:3],
        "items": [{"title": i.get("title"), "source": i.get("source"),
                   "age_h": i.get("age_h"), "score": (i.get("sentiment") or {}).get("score", 0)}
                  for i in items[:12]],
        "errors": data.get("errors", []),
    }


ARB_LOG = "logs_cross_exchange_arbitrage.log"
SIG_LOG = "logs_multi_exchange_signal_poller.log"
GUARD_RE = re.compile(
    r"GUARD\s+(?P<route>\S+):\s+net=(?P<net>-?[\d.]+)bps\s+gross=(?P<gross>-?[\d.]+)bps\s+"
    r"slip=(?P<slip>-?[\d.]+)bps\s+depth=\$(?P<depth>[\d.]+)\s+streak=(?P<streak>\d+)\s+"
    r"rebalance=(?P<reb>\S+)\s+->\s+(?P<decision>TRADE|skip)(?::\s*(?P<reason>.*))?$"
)
PLAN_RE = re.compile(
    r"PLAN:\s+size\s+x(?P<mult>[\d.]+)\s+\(vol=(?P<vol>[\d.]+)%\s+ratio=(?P<ratio>[\d.]+)\s+"
    r"regime=(?P<regime>\w+)\)\s+SL=(?P<sl>[\d.]+)%\s+TP=(?P<tp>[\d.]+)%"
)

_routes_cache = {"ts": 0.0, "value": None}


def _tail_container_log(name, lines=500):
    if not os.path.exists(DOCKER):
        return None
    try:
        res = subprocess.run(
            [DOCKER, "exec", CONTAINER, "bash", "-c", f"tail -n {lines} /home/hummingbot/logs/{name} 2>/dev/null"],
            capture_output=True, text=True, timeout=25,
        )
        return res.stdout or ""
    except Exception:
        return None


def _running_strategy():
    """Активная стратегия — из процесса Hummingbot, а не из логов."""
    if not os.path.exists(DOCKER):
        return None
    try:
        res = subprocess.run(
            [DOCKER, "exec", CONTAINER, "bash", "-c",
             "ps aux | grep '[c]li.engine' | head -3"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    m = re.search(r"--name\s+([A-Za-z0-9_\-]+)", res.stdout or "")
    return m.group(1) if m else None


def strategy_status():
    """Что реально запущено сейчас и как выглядят защиты/маршруты."""
    now = time.time()
    if _routes_cache["value"] is not None and now - _routes_cache["ts"] < 30:
        return _routes_cache["value"]

    value = {"running": _running_strategy(), "routes": [], "plan": None, "signals_log": False}
    if value["running"] == "cross_exchange_arbitrage":
        raw = _tail_container_log(ARB_LOG, 400)
        if raw:
            latest = {}
            for line in raw.splitlines():
                m = GUARD_RE.search(line)
                if m:
                    latest[m.group("route")] = {
                        "route": m.group("route"),
                        "net_bps": float(m.group("net")),
                        "gross_bps": float(m.group("gross")),
                        "slip_bps": float(m.group("slip")),
                        "depth_usd": float(m.group("depth")),
                        "streak": int(m.group("streak")),
                        "rebalance": m.group("reb"),
                        "decision": m.group("decision"),
                        "reason": (m.group("reason") or "").strip()[:110],
                        "ts": line[:23].strip(),
                    }
            value["routes"] = sorted(latest.values(), key=lambda r: -r["net_bps"])

    raw2 = _tail_container_log(SIG_LOG, 300)
    if raw2:
        for line in reversed(raw2.splitlines()):
            m = PLAN_RE.search(line)
            if m:
                value["plan"] = {
                    "size_mult": float(m.group("mult")),
                    "vol_pct": float(m.group("vol")),
                    "vol_ratio": float(m.group("ratio")),
                    "regime": m.group("regime"),
                    "stop_loss_pct": float(m.group("sl")),
                    "take_profit_pct": float(m.group("tp")),
                }
                break

    # План риска считает оракул и пишет его в свой лог (не в лог Hummingbot).
    if value["plan"] is None:
        try:
            with open(SIGNALS_LOG, encoding="utf-8", errors="replace") as f:
                for line in reversed(f.read().splitlines()):
                    m = PLAN_RE.search(line)
                    if m:
                        value["plan"] = {
                            "size_mult": float(m.group("mult")),
                            "vol_pct": float(m.group("vol")),
                            "vol_ratio": float(m.group("ratio")),
                            "regime": m.group("regime"),
                            "stop_loss_pct": float(m.group("sl")),
                            "take_profit_pct": float(m.group("tp")),
                        }
                        break
        except OSError:
            pass

    _routes_cache["ts"] = now
    _routes_cache["value"] = value
    return value


def pumps_block():
    """Сканер пампов: читает результат последнего скана оракула из файла.

    Панель НЕ запускает сканер сам — иначе каждый её запрос делал бы ~70
    обращений к биржам и страница вставала бы в очередь.
    """
    try:
        import pumps as pumps_mod
        data = pumps_mod.load()
    except Exception as e:
        return {"available": False, "error": str(e)[:140], "pumps": [], "scanned": {}}
    return {
        "available": bool(data.get("scanned")),
        "updated_at": data.get("updated_at"),
        "scanned": data.get("scanned") or {},
        "errors": data.get("errors") or {},
        "thresholds": data.get("thresholds") or {},
        "pumps": data.get("pumps") or [],
    }


TRADES_PATH = os.path.join(HUMMINGBOT, "data", "trades.json")


def trades_block():
    """Закрытые сделки и калибровка уверенности по факту."""
    try:
        with open(TRADES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"available": False, "trades": [], "stats": {}}
    items = data.get("trades") or []
    wins = [t for t in items if (t.get("pnl_quote") or 0) > 0]
    losses = [t for t in items if (t.get("pnl_quote") or 0) < 0]
    pnl = sum(float(t.get("pnl_quote") or 0) for t in items)
    # Калибровка: средний P&L в корзинах уверенности модели.
    buckets = {}
    for t in items:
        c = t.get("conf")
        if c is None:
            continue
        key = "0.75–0.85" if c < 0.85 else ("0.85–0.95" if c < 0.95 else "0.95+")
        b = buckets.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
        b["n"] += 1
        b["pnl"] += float(t.get("pnl_quote") or 0)
        if (t.get("pnl_quote") or 0) > 0:
            b["wins"] += 1
    for b in buckets.values():
        b["avg_pnl"] = round(b["pnl"] / b["n"], 6)
        b["win_rate"] = round(b["wins"] / b["n"], 3)
    return {
        "available": bool(items),
        "open": data.get("open", 0),
        "updated_at": data.get("updated_at"),
        "count": len(items),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(items), 3) if items else None,
        "pnl_quote": round(pnl, 6),
        "calibration": buckets,
        "recent": [{"exchange": t.get("exchange"), "pair": t.get("pair"),
                    "pnl_pct": t.get("pnl_pct"), "pnl_quote": t.get("pnl_quote"),
                    "conf": t.get("conf"), "reason": t.get("reason")}
                   for t in items[-8:][::-1]],
    }


TRADES_HOST = os.path.join(HUMMINGBOT_HOST, "data", "trades.json")
STATE_HOST = os.path.join(HUMMINGBOT_HOST, "data", "trading_state.json")
CONTROL_HOST = os.path.join(HUMMINGBOT_HOST, "data", "trading_control.json")


def trading_control():
    """Состояние переключателя торговли и запроса закрытия позиций."""
    control = _read_json(CONTROL_HOST, {}) or {}
    state = _read_json(STATE_HOST, {}) or {}
    return {
        "trading_enabled": bool(control.get("trading_enabled", False)),
        "updated_at": control.get("updated_at"),
        "note": control.get("note"),
        "close_all_requested": control.get("close_all_requested"),
        "close_all_reason": control.get("close_all_reason"),
        "close_all_done": control.get("close_all_done") or (state.get("close_all_done") if state else None),
        "close_all_count": (state or {}).get("close_all_closed"),
    }


def pnl_block():
    """Доход и убыток: реализованный, открытый, по биржам, по дням, кривая."""
    trades_data = _read_json(TRADES_HOST, {}) or {}
    state = _read_json(STATE_HOST, {}) or {}
    trades = list(trades_data.get("trades") or [])
    open_pos = state.get("open_positions") or []

    realized = [float(t.get("pnl_quote") or 0) for t in trades]
    gross_profit = sum(x for x in realized if x > 0)
    gross_loss = abs(sum(x for x in realized if x < 0))
    wins = [x for x in realized if x > 0]
    losses = [x for x in realized if x < 0]
    pcts = [float(t.get("pnl_pct") or 0) for t in trades]

    by_exchange = {}
    for t in trades:
        ex = (t.get("exchange") or "?").replace("_paper_trade", "")
        row = by_exchange.setdefault(ex, {"trades": 0, "pnl": 0.0, "wins": 0})
        row["trades"] += 1
        pnl = float(t.get("pnl_quote") or 0)
        row["pnl"] += pnl
        if pnl > 0:
            row["wins"] += 1
    for row in by_exchange.values():
        row["pnl"] = round(row["pnl"], 6)
        row["win_rate"] = round(row["wins"] / row["trades"], 3) if row["trades"] else 0.0

    by_day = {}
    for t in trades:
        ts = t.get("close_ts") or t.get("ts")
        day = None
        if ts:
            try:
                day = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
            except Exception:
                day = None
        if not day:
            continue
        row = by_day.setdefault(day, {"pnl": 0.0, "trades": 0, "wins": 0})
        pnl = float(t.get("pnl_quote") or 0)
        row["pnl"] += pnl
        row["trades"] += 1
        if pnl > 0:
            row["wins"] += 1
    days = [{"day": d, **{k: (round(v, 6) if k == "pnl" else v) for k, v in row.items()}}
            for d, row in sorted(by_day.items())]

    curve = []
    total = 0.0
    for t in trades:
        total += float(t.get("pnl_quote") or 0)
        curve.append(round(total, 8))

    unrealized = float(state.get("unrealized_pnl") or 0)
    realized_total = sum(realized)
    best = max(realized) if realized else 0.0
    worst = min(realized) if realized else 0.0
    today = days[-1] if days else {"day": None, "pnl": 0.0, "trades": 0}

    return {
        "available": bool(trades) or bool(open_pos),
        "updated_at": trades_data.get("updated_at") or state.get("updated_at"),
        "realized_pnl": round(realized_total, 8),
        "unrealized_pnl": round(unrealized, 8),
        "total_pnl": round(realized_total + unrealized, 8),
        "today_pnl": today["pnl"],
        "trades": len(trades),
        "open_positions": len(open_pos),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 3) if trades else 0.0,
        "avg_win": round(gross_profit / len(wins), 6) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 6) if losses else 0.0,
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "best_trade": round(best, 6),
        "worst_trade": round(worst, 6),
        "avg_pnl_pct": round(sum(pcts) / len(pcts), 4) if pcts else 0.0,
        "by_exchange": sorted(by_exchange.items(), key=lambda kv: -kv[1]["pnl"]),
        "by_day": days,
        "curve": curve[-40:],
        "open": [{"exchange": (p.get("exchange") or "").replace("_paper_trade", ""),
                  "pair": p.get("pair"), "side": p.get("side"),
                  "entry_price": p.get("entry_price"), "last_price": p.get("last_price"),
                  "pnl_pct": p.get("pnl_pct"), "sl_pct": p.get("sl_pct"),
                  "tp_pct": p.get("tp_pct"), "age_min": p.get("age_min")}
                 for p in open_pos],
        "exit_reasons": _exit_reasons(trades),
        "control": trading_control(),
        "mode": state.get("mode", "unknown"),
    }


def _exit_reasons(trades):
    """Почему закрывались позиции — сколько стопов, сколько тейков."""
    counts = {}
    for t in trades:
        reason = (t.get("reason") or "").split()[0] if t.get("reason") else "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def signal_eval_block():
    """Качество сигналов: forward-доходность BTC после сигнала vs база.

    Источник — data/signal_eval.json (пишет scripts/eval_signals.py).
    """
    data = _read_json(os.path.join(DATA, "signal_eval.json"), None)
    if not isinstance(data, dict) or not data.get("horizons"):
        return {"available": False}
    horizons = []
    for h, hd in (data.get("horizons") or {}).items():
        rows = []
        for gname, labels in (hd.get("groups") or {}).items():
            for label, s in (labels or {}).items():
                hs = s.get("hit_rate")
                rows.append({"group": gname, "label": label, "n": s.get("n"),
                             "avg": s.get("avg"),
                             "hit": (round(hs * 100) if hs is not None else None)})
        horizons.append({"h": int(h), "market": hd.get("market"), "rows": rows})
    horizons.sort(key=lambda x: x["h"])
    return {"available": True, "cycles": data.get("cycles"), "period": data.get("period"),
            "directional_final": data.get("directional_final"), "horizons": horizons}


def collect_state():
    cfg = _read_json(CFG, {}) or {}
    price = _read_json(PRICE, {}) or {}
    cycles = recent_cycles()
    hb = _read_json(HB_SIGNALS)
    last = cycles[0] if cycles else None
    interval = int(cfg.get("poll_interval_sec", 300) or 300)
    age = _age_seconds(PRICE)
    next_in = max(0, interval - (age or 0)) if age is not None else None
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "oracle": {
            "btc": price.get("price"),
            "btc_src": price.get("src"),
            "cycle_interval_sec": interval,
            "seconds_since_cycle": age,
            "seconds_to_next_cycle": next_in,
            "history_updated": _mtime(HISTORY),
            "last_verdict": (last or {}).get("verdict"),
            "last_final": (last or {}).get("final"),
        },
        "bridge": {
            "signal_delivered": hb,
            "signal_age_sec": _age_seconds(HB_SIGNALS),
            "log": SIGNALS_LOG,
        },
        "hummingbot": docker_state(),
        "models": models_info(),
        "news": news_block(),
        "pumps": pumps_block(),
        "orders": orders(),
        "status": strategy_status(),
        "trade_mode": trade_mode(),
        "trades": trades_block(),
        "pnl": pnl_block(),
        "signal_eval": signal_eval_block(),
        "exchanges": strategy_exchanges(),
        "events": cfg.get("polymarket_events", []),
        "groups": markets(),
        "cycles": cycles,
    }


if __name__ == "__main__":
    print(json.dumps(collect_state(), ensure_ascii=False, indent=2)[:4000])
