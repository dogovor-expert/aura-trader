# -*- coding: utf-8 -*-
"""Тесты проверки чисел в ответе модели. Запуск: python tests/test_verify.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from verify import FABRICATION_CAP, verify_verdict  # noqa: E402

HEUR = {
    "btc_price": 82150.0,
    "btc_chg_pct": 0.42,
    "verdict": "NEUTRAL",
    "signals": [
        {"prev_prob": 0.55, "cur_prob": 0.61, "delta_pp": 6.0},
        {"prev_prob": 0.30, "cur_prob": 0.29, "delta_pp": -1.0},
    ],
    "news_sentiment": {"score": 0.12},
}

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def main():
    print("== clean verdict ==")
    r = verify_verdict({"signal": "LONG", "confidence": 0.82, "edge_bps": 30.0,
                        "delta_pp": 6.0, "regime": "trend",
                        "reasons": ["delta 6 pp on FED_OCT", "BTC up 0.42%"],
                        "null_hypothesis": "normal noise"}, HEUR)
    check("clean is ok", r["ok"], r["issues"])
    check("clean keeps confidence", r["confidence"] == 0.82, r["confidence"])
    check("clean not capped", not r["capped"])

    print("== fabricated delta ==")
    r = verify_verdict({"signal": "LONG", "confidence": 0.9, "delta_pp": 87.0,
                        "reasons": ["huge shift"]}, HEUR)
    check("fabricated delta flagged", "delta_pp=87.0" in r["fabricated"], r["fabricated"])
    check("fabricated caps confidence", r["confidence"] <= FABRICATION_CAP, r["confidence"])

    print("== fabricated number in reasons ==")
    r = verify_verdict({"signal": "SHORT", "confidence": 0.8,
                        "reasons": ["inflow of 5000 BTC confirmed", "spread 0.5%"]}, HEUR)
    check("reasons number flagged", any("5000" in x for x in r["fabricated"]), r["fabricated"])
    check("capped", r["capped"])

    print("== out of range confidence ==")
    r = verify_verdict({"signal": "LONG", "confidence": 1.7}, HEUR)
    check("conf clamped", r["confidence"] == 1.0, r["confidence"])
    check("issue reported", any("out of range" in i for i in r["issues"]), r["issues"])

    print("== unknown signal / regime ==")
    r = verify_verdict({"signal": "MOON", "confidence": 0.5, "regime": "wild"}, HEUR)
    check("unknown signal -> WAIT", r["verdict"]["signal"] == "WAIT", r["verdict"])
    check("unknown regime reported", any("regime" in i for i in r["issues"]), r["issues"])

    print("== implausible edge ==")
    r = verify_verdict({"signal": "LONG", "confidence": 0.6, "edge_bps": 99999}, HEUR)
    check("edge flagged", any("edge_bps" in i for i in r["issues"]), r["issues"])

    print("== missing confidence ==")
    r = verify_verdict({"signal": "WAIT"}, HEUR)
    check("missing conf -> 0", r["confidence"] == 0.0, r["confidence"])
    check("missing conf reported", any("missing" in i for i in r["issues"]), r["issues"])

    print("== news sentiment number allowed ==")
    r = verify_verdict({"signal": "LONG", "confidence": 0.6,
                        "reasons": ["news net 0.12", "delta 6 pp"]}, HEUR)
    check("news score allowed", r["ok"] or not r["capped"], r["issues"])

    print("== config/constant numbers allowed (no false fabrication) ==")
    r = verify_verdict({"signal": "LONG", "confidence": 0.8,
                        "reasons": ["confirmed on 2 exchanges", "matches min confidence 0.75"]},
                       HEUR, extra=[0.75])
    check("count and config number allowed", not r["capped"], r["fabricated"])
    r = verify_verdict({"signal": "LONG", "confidence": 0.8,
                        "reasons": ["inflow 0.87 vs 0.9"]}, HEUR, extra=[0.75])
    check("unrelated decimal still flagged", r["capped"], r["fabricated"])

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {FAILURES}")
        return 1
    print("ALL VERIFY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())