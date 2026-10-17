# -*- coding: utf-8 -*-
"""Бэктест порога prob_spike_threshold на РЕАЛЬНОЙ истории.

Данные:
  * исторические вероятности Polymarket — CLOB prices-history (по каждой
    конфигурируемой паре вопросов);
  * часовые свечи BTC — публичный API Kraken.

Проверяет гипотезу «вероятность выросла, а BTC стоит/падает -> LONG».
Для каждого часа строится baseline (медиана окна назад), считается дельта,
применяется порог и правило дивергенции — как в live-эвристике. Затем
считается forward-доходность BTC через +1/+4/+24 ч. Пороги перебираются,
данные делятся на in-sample (обучение) и out-of-sample (проверка), чтобы
не поймать переобучение.

Запуск: python scripts/backtest_threshold.py [--lookback 12] [--fidelity 60]
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

UA = {"User-Agent": "Mozilla/5.0 (aura-trader backtest)"}
HORIZONS_H = [1, 4, 24]          # часы вперёд
DEFAULT_THRESHOLDS = [0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.10]

# В окружении HTTP(S)_PROXY указывает на SOCKS5 как на http:// — urllib на этом
# виснет. Для публичных API (Polymarket CLOB, Kraken) прокси не нужен.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def load_slugs():
    with open(os.path.join(BASE, "config", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return [e.get("slug") for e in (cfg.get("polymarket_events") or []) if e.get("slug")]


def fetch_markets(slugs):
    """[(question, {hour: prob})] по всем событиям."""
    markets = []
    for slug in slugs:
        try:
            ev = http_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
        except Exception as e:
            print(f"  gamma {slug}: ошибка {str(e)[:80]}")
            continue
        toks = []
        for e in (ev or []):
            for m in (e.get("markets") or []):
                ids = m.get("clobTokenIds")
                if isinstance(ids, str):
                    try:
                        ids = json.loads(ids)
                    except Exception:
                        continue
                if not ids:
                    continue
                toks.append((m.get("question") or "", ids[0]))
        for q, tok in toks:
            try:
                ph = http_json(
                    f"https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity=60")
            except Exception:
                continue
            pts = ph.get("history") or []
            if not pts:
                continue
            series = {}
            for p in pts:
                series[int(p["t"]) // 3600 * 3600] = round(float(p["p"]), 6)
            markets.append((q[:70], series))
    return markets


def fetch_btc_hourly():
    r = http_json("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60")
    result = r.get("result") or {}
    rows = []
    for k, v in result.items():        # ключ пары приходит как XXBTZUSD
        if isinstance(v, list) and v:
            rows = v
            break
    out = {}
    for row in rows:
        out[int(row[0]) // 3600 * 3600] = float(row[4])
    return out


def replay(markets, btc, threshold, lookback):
    """Для каждого часа — вердикт эвристики и forward-доходности BTC."""
    hours = sorted(btc)
    rows = []
    for i in range(lookback + 1, len(hours)):
        t = hours[i]
        window = hours[i - lookback:i]
        btc_now = btc[t]
        btc_prev = btc[window[0]]
        btc_chg = (btc_now - btc_prev) / btc_prev * 100.0 if btc_prev else 0.0
        bull = bear = False
        for _q, series in markets:
            d = series.get(t)
            if d is None:
                continue
            vals = [v for v in (series.get(h) for h in window) if v is not None and 0.001 < v < 0.999]
            if not vals:
                continue
            delta = d - statistics.median(vals)
            if abs(delta) < threshold:
                continue
            div = (delta > 0 and btc_chg < 0.3) or (delta < 0 and btc_chg > -0.3)
            if delta > 0 and div:
                bull = True
            elif delta < 0 and div:
                bear = True
        verdict = "BULLISH_DIVERGENCE" if bull else "BEARISH_DIVERGENCE" if bear else "NEUTRAL"
        fwd = {}
        for hz in HORIZONS_H:
            j = i + hz
            fwd[hz] = round((btc[hours[j]] - btc_now) / btc_now * 100.0, 4) if j < len(hours) else None
        rows.append((t, verdict, fwd))
    return rows


def _stats(rows, verdict):
    rets = {hz: [] for hz in HORIZONS_H}
    hits = {hz: [] for hz in HORIZONS_H}
    for _t, v, fwd in rows:
        if v != verdict:
            continue
        for hz in HORIZONS_H:
            r = fwd[hz]
            if r is None:
                continue
            rets[hz].append(r)
            if verdict == "BULLISH_DIVERGENCE":
                hits[hz].append(r > 0)
            elif verdict == "BEARISH_DIVERGENCE":
                hits[hz].append(r < 0)
    out = {"n": len([1 for _t, v, _f in rows if v == verdict])}
    for hz in HORIZONS_H:
        v = rets[hz]
        out[hz] = {
            "avg": round(sum(v) / len(v), 4) if v else None,
            "hit": round(sum(hits[hz]) / len(hits[hz]), 3) if hits[hz] else None,
        }
    return out


def _fmt(x):
    return "  —  " if x is None else f"{x:+.3f}"


def _slice(rows, subset, split):
    if subset == "train":
        return rows[:split]
    if subset == "out":
        return rows[split:]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=12, help="окно baseline в часах")
    ap.add_argument("--fidelity", type=int, default=60)
    a = ap.parse_args()

    slugs = load_slugs()
    print(f"=== бэктест порога: {len(slugs)} событий, окно {a.lookback}ч ===")
    print("тяну историю Polymarket (CLOB) ...")
    markets = fetch_markets(slugs)
    print(f"  рынков с историей: {len(markets)}")
    print("тяну часовые свечи BTC (Kraken) ...")
    btc = fetch_btc_hourly()
    print(f"  часов BTC: {len(btc)}")
    if not markets or len(btc) < a.lookback + 2:
        print("недостаточно данных для бэктеста")
        return 1

    overlap = sorted(set(btc) & set().union(*[set(s) for _q, s in markets]))
    if overlap:
        d0 = dt.datetime.fromtimestamp(overlap[0], dt.timezone.utc).date()
        d1 = dt.datetime.fromtimestamp(overlap[-1], dt.timezone.utc).date()
        print(f"  период: {d0} .. {d1}")

    # Порог 10.0 недостижим -> все часы NEUTRAL = рыночная база («всегда WAIT»).
    base_rows = replay(markets, btc, 10.0, a.lookback)
    all_rows = {thr: replay(markets, btc, thr, a.lookback) for thr in DEFAULT_THRESHOLDS}
    split = len(base_rows) * 7 // 10

    for label, sub in (("ВСЕ ДАННЫЕ", None), ("train 70% (in-sample)", "train"),
                       ("test 30% (out-of-sample)", "out")):
        brows = _slice(base_rows, sub, split)
        bstat = _stats(brows, "NEUTRAL")
        print(f"\n===== {label} =====")
        print("  база рынка (все часы, «всегда WAIT»): " +
              "  ".join(f"+{h}ч {_fmt(bstat[h]['avg'])}%" for h in HORIZONS_H))
        print("  порог  n_sig  " + "  ".join(f"+{h}ч avg/d/hit" for h in HORIZONS_H) + "   short n")
        for thr in DEFAULT_THRESHOLDS:
            rows = _slice(all_rows[thr], sub, split)
            sig = _stats(rows, "BULLISH_DIVERGENCE")
            sigb = _stats(rows, "BEARISH_DIVERGENCE")
            cells = []
            for h in HORIZONS_H:
                b = sig[h]
                if b["avg"] is None:
                    cells.append("   —")
                    continue
                d = b["avg"] - bstat[h]["avg"]
                hit = "—" if b["hit"] is None else f"{b['hit']*100:.0f}%"
                cells.append(f"{b['avg']:+.3f}/{d:+.3f}/{hit}")
            print(f"  {thr:<6} {sig['n']:>4}   " + "  ".join(f"{c:>13}" for c in cells) +
                  f"   {sigb['n']:>4}")
    print("\navg — ср. доходность BTC после BULLISH_DIVERGENCE; d — превышение над базой "
          "(альфа); hit — доля роста.")
    print("Гипотеза оправдана, только если Δ устойчиво > 0 и hit заметно выше базы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())