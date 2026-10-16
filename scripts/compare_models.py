# -*- coding: utf-8 -*-
"""Сравнение двух моделей на ОДНИХ И ТЕХ ЖЕ реальных данных оракула.

Критерий: не «какая звучит умнее», а совпадает ли её решение с данными,
не выдумывает ли числа и сколько занимает времени.
"""
import json
import sys
import time
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

from src import analyst, dashboard_data, llm, verify  # noqa: E402
from sensor import poll_all  # noqa: E402

MODELS = [("cx/gpt-6-luna", "текущая L1"), ("ag/gemini-3.8-flash-high", "кандидат L1")]


def build_case():
    """Собирает реальный INPUT оракула (как в main.py) для честного сравнения."""
    import config as cfgmod
    ev = (cfgmod.load_config() if hasattr(cfgmod, 'load_config') else {}).get('polymarket_events', [])
    pairs = [(e.get('slug'), e.get('label')) for e in ev if e.get('slug')]
    snap = poll_all(pairs, proxies=None) if pairs else {}
    btc = 82100.0
    heur = analyst.heuristic_signal(snap, btc, btc * 0.999, 0.02)
    heur.setdefault("news_digest", "NO NEWS AVAILABLE")
    heur.setdefault("pumps_digest", "NO PUMPS")
    return heur


def run(model, heur, cfg):
    started = time.time()
    text = llm.call_llm(cfg["provider"], model, llm.SYSTEM_PROMPT + " " + llm.RISK_RULES,
                        llm.build_user_prompt(heur), None, int(cfg.get("timeout", 60)))
    elapsed = round(time.time() - started, 2)
    verdict = llm.parse_verdict(text)
    check = verify.verify_verdict(verdict, heur)
    return {"signal": verdict.get("signal"), "conf": verdict.get("confidence"),
            "regime": verdict.get("regime"), "sec": elapsed,
            "fabricated": check["fabricated"], "capped": check["capped"],
            "issues": check["issues"][:2]}


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "config", "config.json"), encoding="utf-8"))["llm"]
    heur = build_case()
    print("INPUT: deltas =", len(heur.get("signals", [])), "| verdict =", heur.get("verdict"))
    table = []
    for model, role in MODELS:
        for attempt in range(2):
            try:
                row = run(model, heur, cfg)
            except Exception as e:
                row = {"signal": "ERROR", "conf": 0, "sec": 0, "error": str(e)[:90]}
            row.update({"model": model, "role": role, "run": attempt + 1})
            table.append(row)
            print(f"{model:28s} #{attempt + 1} -> {row['signal']:6s} conf={row['conf']} "
                  f"{row['sec']:>5.1f}s fabricated={row.get('fabricated')} {row.get('issues', '')}")

    print("\n=== ИТОГ ===")
    for model, role in MODELS:
        rows = [r for r in table if r["model"] == model]
        ok = [r for r in rows if r.get("signal") in ("LONG", "SHORT", "WAIT")]
        fab = [r for r in rows if r.get("fabricated")]
        avg = round(sum(r["sec"] for r in rows) / max(1, len(rows)), 1)
        agree = len({r.get("signal") for r in ok}) == 1 if ok else False
        print(f"{role:14s} {model:28s} ответов={len(ok)}/2 устойчивость={'да' if agree else 'нет'} "
              f"фабрикаций={len(fab)} среднее={avg}s")


if __name__ == "__main__":
    main()