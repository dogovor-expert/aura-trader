# -*- coding: utf-8 -*-
"""Оценка качества сигналов: что происходило с ценой BTC ПОСЛЕ сигнала.

Логи сигналов не отвечают на главный вопрос — предсказывают ли сигналы
движение вообще. Этот скрипт берёт каждый цикл из logs/signals.log
(время, вердикт эвристики, сигналы LLM L1/final, цена BTC) и считает
forward-доходность BTC через +1ч и +4ч, а также сравнивает с базой
(«всегда WAIT» = средний дрейф рынка).

Запуск: python scripts/eval_signals.py [--horizons 60,240]
"""
import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

from dashboard_data import recent_cycles  # noqa: E402

OUT_JSON = os.path.join(BASE, "data", "signal_eval.json")


def _dt(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def build_series(cycles):
    """Хронологический ряд (время, цена) из циклов со временем и ценой."""
    series = []
    for c in cycles:
        d = _dt(c.get("ts"))
        btc = c.get("btc")
        if d is None or not btc:
            continue
        series.append((d, float(btc)))
    series.sort(key=lambda x: x[0])
    return series


def forward_returns(series, horizon_min):
    """Для каждой точки ряда — доходность до ближайшего цикла >= t+horizon."""
    times = [d for d, _ in series]
    prices = [p for _, p in series]
    out = []
    for i, (d, p) in enumerate(series):
        if p <= 0:
            out.append(None)
            continue
        j = bisect.bisect_left(times, d + timedelta(minutes=horizon_min))
        out.append(None if j >= len(times) else round((prices[j] - p) / p * 100.0, 4))
    return out


def hit(kind, label, ret):
    """Совпало ли направление: LONG/BULLISH ждёт рост, SHORT/BEARISH — падение."""
    if ret is None:
        return None
    up = ("LONG", "BULLISH_DIVERGENCE", "BULLISH")
    down = ("SHORT", "BEARISH_DIVERGENCE", "BEARISH")
    if label in up:
        return ret > 0
    if label in down:
        return ret < 0
    return None


def _agg(rets):
    vals = [r for r in rets if r is not None]
    if not vals:
        return None
    vals_sorted = sorted(vals)
    return {
        "n": len(vals),
        "avg": round(sum(vals) / len(vals), 4),
        "median": round(vals_sorted[len(vals_sorted) // 2], 4),
        "best": round(max(vals), 4),
        "worst": round(min(vals), 4),
    }


def _brier(cycles, fwd, idx, horizons, getter):
    """Brier score для направленных сигналов группы.

    Прогноз = уверенность модели (conf) в том, что направление верно;
    факт = совпало ли движение. Brier = mean((conf - fact)^2).
    0.25 = случайное угадывание, меньше — лучше калибровка.
    """
    res = {}
    for h in horizons:
        pairs = []
        for c in cycles:
            sig = getter(c) or {}
            s = sig.get("signal") if isinstance(sig, dict) else None
            if s not in ("LONG", "SHORT"):
                continue
            d = _dt(c.get("ts"))
            btc = c.get("btc")
            if d is None or not btc:
                continue
            j = idx.get((d, float(btc)))
            if j is None:
                continue
            r = fwd[h][j]
            if r is None:
                continue
            outcome = 1 if ((s == "LONG" and r > 0) or (s == "SHORT" and r < 0)) else 0
            pairs.append((float(sig.get("conf") or 0.5), outcome))
        if not pairs:
            res[h] = {"n": 0}
            continue
        res[h] = {"n": len(pairs),
                  "brier": round(sum((p - o) ** 2 for p, o in pairs) / len(pairs), 4),
                  "hit": round(sum(o for _p, o in pairs) / len(pairs), 3)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="60,240", help="горизонты в минутах через запятую")
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    cycles = recent_cycles(limit=100000)  # newest-first
    cycles = list(reversed(cycles))       # chronological
    series = build_series(cycles)
    if len(series) < 3:
        print("мало данных в signals.log — нужно несколько часов циклов")
        return 0

    fwd = {h: forward_returns(series, h) for h in horizons}
    idx = {}
    for i, (d, _) in enumerate(series):
        idx[(d, series[i][1])] = i

    print(f"=== Оценка сигналов Aura Trader ===")
    print(f"циклов с ценой: {len(series)} | период: "
          f"{series[0][0].isoformat(timespec='minutes')} .. {series[-1][0].isoformat(timespec='minutes')}")

    report = {"cycles": len(series),
              "period": [series[0][0].isoformat(), series[-1][0].isoformat()],
              "horizons": {}}

    for h in horizons:
        print(f"\n--- горизонт +{h} мин ---")
        base = _agg(fwd[h])
        if base:
            print(f"  рынок (все циклы, база WAIT): n={base['n']} "
                  f"avg={base['avg']:+.3f}% median={base['median']:+.3f}%")
        else:
            print("  нет данных")
        report["horizons"][h] = {"market": base, "groups": {}}
        for group, getter in (("эвристика", lambda c: c.get("verdict")),
                              ("L1", lambda c: (c.get("l1") or {}).get("signal")),
                              ("final", lambda c: (c.get("final") or {}).get("signal"))):
            buckets = {}
            for i, c in enumerate(cycles):
                d = _dt(c.get("ts"))
                btc = c.get("btc")
                if d is None or not btc:
                    continue
                j = idx.get((d, float(btc)))
                if j is None:
                    continue
                label = getter(c)
                if not label:
                    continue
                buckets.setdefault(label, {"rets": [], "hits": []})
                buckets[label]["rets"].append(fwd[h][j])
                buckets[label]["hits"].append(hit(group, label, fwd[h][j]))
            for label, data in sorted(buckets.items()):
                s = _agg(data["rets"])
                if not s:
                    continue
                hs = [x for x in data["hits"] if x is not None]
                hitrate = f"{sum(hs) / len(hs) * 100:.0f}%" if hs else "—"
                print(f"  [{group}] {label:20s} n={s['n']:4d} avg={s['avg']:+.3f}% "
                      f"медиана={s['median']:+.3f}% hit={hitrate}")
                report["horizons"][h]["groups"].setdefault(group, {})[label] = {
                    **s, "hit_rate": (round(sum(hs) / len(hs), 3) if hs else None)}

    directional = sum(1 for c in cycles
                      if (c.get("final") or {}).get("signal") in ("LONG", "SHORT"))

    print("\n--- Brier (калибровка уверенности, 0.25 = случайно) ---")
    report["brier"] = {}
    for group, getter in (("L1", lambda c: c.get("l1")), ("final", lambda c: c.get("final"))):
        br = _brier(cycles, fwd, idx, horizons, getter)
        report["brier"][group] = br
        for h in horizons:
            b = br[h]
            if not b.get("n"):
                print(f"  [{group}] +{h}мин: нет направленных сигналов")
            else:
                print(f"  [{group}] +{h}мин: n={b['n']} Brier={b['brier']} "
                      f"hit={b['hit']*100:.0f}%")

    print(f"\nнаправленных сигналов (final LONG/SHORT): {directional}")
    report["directional_final"] = directional
    try:
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"отчёт: {OUT_JSON}")
    except OSError as e:
        print("не удалось записать отчёт:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())