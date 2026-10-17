# -*- coding: utf-8 -*-
"""Бэктест скальпинга: окупается лиHFT торговля за вычетом комиссий.

Берёт реальные минутные свечи OKX BTC-USDT-SWAP и оценивает два класса
скальп-стратегий:
  1. Momentum (taker): вход по close, выход через N минут по close.
     Издержки = taker-комиссия x 2 (вход+выход).
  2. Spread-capture (maker с двух сторон): прибыль = спред - makerx2.
     Если исполняется только одна нога — остаёшься с инвентарём (риск).

Также считает точку безубыточности: какое движение нужно, чтобы покрыть
комиссии. Если типичное движение меньше — стратегия убыточна по конструкции.

Запуск: python scripts/backtest_scalp.py [--inst BTC-USDT-SWAP] [--taker 5] [--maker 2]
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "aura-trader backtest"}
OKX_BASE = "https://www.okx.com/api/v5"


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def candles(inst, bar="1m", limit=300):
    d = http_json(f"{OKX_BASE}/market/candles?instId={inst}&bar={bar}&limit={limit}")["data"]
    # OKX отдаёт newest-first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    rows = [(int(x[0]), float(x[1]), float(x[4])) for x in d]  # (ts, open, close)
    rows.sort()
    return rows


def spread_bps(inst):
    bk = http_json(f"{OKX_BASE}/market/books?instId={inst}&sz=1")["data"][0]
    bid, ask = float(bk["bids"][0][0]), float(bk["asks"][0][0])
    return (ask - bid) / ((ask + bid) / 2) * 10000.0


def _stats(vals):
    vals = sorted(vals)
    if not vals:
        return {}
    n = len(vals)
    return {"n": n, "avg": sum(vals) / n, "median": vals[n // 2],
            "p10": vals[n // 10], "p90": vals[9 * n // 10]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--taker", type=float, default=5.0, help="taker-комиссия, bps/сторону")
    ap.add_argument("--maker", type=float, default=2.0, help="maker-комиссия, bps/сторону")
    a = ap.parse_args()

    rows = candles(a.inst)
    closes = [c for _t, _o, c in rows]
    if len(closes) < 30:
        print("мало свечей")
        return 1
    sp = spread_bps(a.inst)
    print(f"=== Бэктест скальпинга на {a.inst} ===")
    print(f"свечей: {len(rows)} (1м, ~{len(rows)} мин) | спред сейчас: {sp:.4f} bps")
    print(f"комиссии: taker {a.taker} bps/сторону, maker {a.maker} bps/сторону\n")

    # 1. Momentum (taker): нужен ход больше round-trip комиссии.
    rt_taker = 2 * a.taker
    print(f"--- Momentum taker (round-trip = {rt_taker} bps) ---")
    for N in (1, 3, 5):
        rets = []
        for i in range(len(closes) - N):
            rets.append((closes[i + N] - closes[i]) / closes[i] * 10000.0)
        gross = _stats(rets)
        net = [r - rt_taker for r in rets]
        wins = sum(1 for x in net if x > 0)
        nets = _stats(net)
        print(f"  N={N}м: |ход| медиана {abs(gross['median']):.2f} bps | "
              f"gross avg {gross['avg']:+.2f} | net avg {nets['avg']:+.2f} bps | "
              f"направление-верно {wins/len(net)*100:.0f}% | breakeven-ход {rt_taker} bps")
    need = rt_taker
    abs_moves = _stats([abs((closes[i+1]-closes[i])/closes[i]*10000) for i in range(len(closes)-1)])
    print(f"  типичное |1м-движение|: медиана {abs_moves['median']:.2f} bps, p90 {abs_moves['p90']:.2f} bps")
    print(f"  -> чтобы taker-скальп окупился, цена должна пройти {need} bps; "
          f"в {100*sum(1 for x in [abs((closes[i+1]-closes[i])/closes[i]*10000) for i in range(len(closes)-1)] if x>=need)/len(closes):.0f}% минут движение это превышает\n")

    # 2. Spread-capture maker: прибыль = спред - maker*2 (если обе ноги исполнились).
    rt_maker = 2 * a.maker
    net_mm = sp - rt_maker
    print(f"--- Spread-capture maker (прибыль = спред - makerx2) ---")
    print(f"  спред {sp:.4f} bps - makerx2 {rt_maker:.2f} bps = нетто {net_mm:+.4f} bps за круг")
    print(f"  при этом одна нога может не исполниться (adverse selection) — тогда держишь инвентарь\n")

    verdict = "НЕ ОКУПАЕТСЯ" if net_mm < 0 and abs_moves["median"] < need else "пограничный"
    print(f"ВЕРДИКТ: скальпинг {verdict} в текущих комиссиях.")
    print(f"  • taker-скальпу нужно {need} bps хода; медиана минутного хода — "
          f"{abs_moves['median']:.2f} bps (~в {need/max(abs_moves['median'],0.001):.0f}x меньше).")
    print(f"  • maker-маркет-мейкинг: спред {sp:.4f} bps против комиссии {rt_maker:.2f} bps/круг "
          f"(спред меньше комиссии в {rt_maker/max(sp,0.0001):.0f}x).")
    print("  • Единственный путь к плюсу — maker-rebate / VIP-тариф (комиссия ≤ 0), "
          "тогда spread-capture становится положительным. На VIP0 — нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())