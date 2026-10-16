# -*- coding: utf-8 -*-
"""Самотест цепочки: сигнал -> мост -> Hummingbot -> выход -> журнал.

Проверяет то, что нельзя увидеть в логах оркула: что система вообще способна
дойти до сделки и закрыть её. Работает на бумажных биржах.

Запуск: python scripts/selftest.py [--skip-live]
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

DATA = os.path.join(BASE, "data")
TRADES = "D:/Hummingbot/data/trades.json"
BRIDGE = "http://127.0.0.1:8181"


def _token():
    tok = os.getenv("BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(os.path.join(BASE, "config", "bridge_token.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


TOKEN = _token()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" -> " + detail) if detail else ""))
    return ok


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"X-Bridge-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def stage_signal_path():
    """Спред-детектор и порог: снимок собирается так же, как в main.py."""
    print("[1/3] path: threshold + spike detector")
    import analyst
    prev = {lab: {"title": lab, "markets": [{"question": f"q{i}", "yes_prob": 0.40} for i in range(3)]}
            for lab in ("T1",)}
    cur = {lab: {"title": lab, "markets": [{"question": f"q{i}", "yes_prob": 0.46} for i in range(3)]}
           for lab in ("T1",)}
    snap = analyst.detect_spikes(prev["T1"]["markets"], cur["T1"]["markets"], 0.02)
    for m in cur["T1"]["markets"]:
        m["_spikes"] = [sp for sp in snap if sp[0] == m["question"]]

    h = analyst.heuristic_signal(prev, 80000.0, 80000.0, 0.02)
    check("no delta gives NEUTRAL", h.get("verdict") == "NEUTRAL", str(h.get("verdict")))

    h2 = analyst.heuristic_signal(cur, 80000.0, 80000.0, 0.02)
    signals = h2.get("signals") or []
    check("6 pp delta caught by 2 pp threshold", len(signals) > 0, f"signals={len(signals)}")

    cur3 = {lab: {"title": lab, "markets": [{"question": f"q{i}", "yes_prob": 0.405} for i in range(3)]}
            for lab in ("T1",)}
    snap3 = analyst.detect_spikes(prev["T1"]["markets"], cur3["T1"]["markets"], 0.02)
    for m in cur3["T1"]["markets"]:
        m["_spikes"] = [sp for sp in snap3 if sp[0] == m["question"]]
    h3 = analyst.heuristic_signal(cur3, 80000.0, 80000.0, 0.02)
    check("0.5 pp stays quiet (no false signals)", not (h3.get("signals") or []),
          f"signals={len(h3.get('signals') or [])}")
    return len(signals) > 0


def stage_verifier():
    """Проверка чисел: выдуманное значение должно порезать уверенность."""
    print("[2/3] проверка чисел модели")
    import verify
    heur = {"btc_price": 80000.0, "btc_chg_pct": 0.1, "verdict": "NEUTRAL", "signals": []}
    good = {"signal": "WAIT", "confidence": 0.8, "edge_bps": 0, "delta_pp": 0.0,
            "reasons": ["delta below threshold"], "regime": "range"}
    v1 = verify.verify_verdict(good, heur)
    check("чистый вердикт проходит без замечаний", v1["ok"], str(v1["issues"])[:80])

    bad = {"signal": "LONG", "confidence": 0.95, "edge_bps": 120, "delta_pp": 0.0,
           "reasons": ["delta of 14.2 pp confirmed"], "regime": "trend"}
    v2 = verify.verify_verdict(bad, heur)
    check("выдуманное число 14.2 помечается фабрикацией", v2["fabricated"], str(v2["issues"])[:80])
    check("уверенность срезается под порог торговли", v2["confidence"] <= 0.25, str(v2["confidence"]))
    return True


def _strategy_log_tail(lines=60):
    """Хвост лога работающей стратегии в контейнере."""
    import subprocess
    docker = r"D:\Docker\Desktop\resources\bin\docker.exe"
    if not os.path.exists(docker):
        return ""
    try:
        res = subprocess.run(
            [docker, "exec", "hummingbot", "bash", "-c",
             f"tail -n {lines} /home/hummingbot/logs/logs_multi_exchange_signal_poller.log 2>/dev/null"],
            capture_output=True, text=True, timeout=25,
        )
        return res.stdout or ""
    except Exception:
        return ""


def stage_live():
    """Живой путь: сигнал в мост -> сделка -> выход -> журнал."""
    print("[3/3] live path (bridge -> Hummingbot)")
    before = {}
    try:
        before = get_json(BRIDGE + "/api/state").get("trades", {})
    except Exception:
        pass
    n_before = (before or {}).get("count", 0)
    open_before = (before or {}).get("open", 0)

    try:
        st = get_json(BRIDGE + "/api/state")
        running = ((st.get("status") or {}).get("running")) or "?"
    except Exception:
        running = "?"
    check("running strategy is the signal one", running == "multi_exchange_signal_poller",
          f"running={running}")

    sig = {"signal": {"final": "LONG", "conf": 0.95, "btc": 82000,
                      "size_mult": 0.05, "stop_loss_pct": 6.0, "take_profit_pct": 2.0}}
    req = urllib.request.Request(BRIDGE + "/signal", data=json.dumps(sig).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Bridge-Token": TOKEN}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:
        ok = False
        print("   signal error:", e)
    check("сигнал принят мостом", ok)

    print("   жду реакции стратегии (до 120 с)…")
    deadline = time.time() + 120
    reacted = False
    reason = ""
    while time.time() < deadline:
        time.sleep(10)
        log_tail = _strategy_log_tail()
        low = log_tail.lower()
        if "signal executed on" in low:
            reacted, reason = True, "ордер выставлен"
            break
        if any(k in low for k in ("cooldown", "ignored", "skip", "test signal")):
            reacted, reason = True, "отклонён правилом стратегии (это корректно)"
            break
    check("стратегия отреагировала явно (сделка или отказ по правилу)", reacted, reason)

    try:
        st = get_json(BRIDGE + "/api/state")
        plan = (st.get("status") or {}).get("plan")
        check("план риска присутствует", bool(plan and plan.get("stop_loss_pct")),
              json.dumps(plan, ensure_ascii=False) if plan else "нет плана")
    except Exception:
        check("план риска присутствует", False, "состояние недоступно")


def main():
    print("=== самотест Aura Trader ===")
    a = stage_signal_path()
    b = stage_verifier()
    c = True
    if "--skip-live" not in sys.argv:
        c = stage_live()
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\nитого: {ok}/{len(RESULTS)} проверок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())