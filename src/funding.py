# -*- coding: utf-8 -*-
"""Фид ставки финансирования и базиса BTC-перпа для funding-carry.

Оракул пишет данные в D:/Hummingbot/data/funding.json (в контейнере это
/home/hummingbot/data/funding.json) — оттуда их читает стратегия
funding_carry.py. Запись атомарная (temp + os.replace).
"""
import json
import os
import time

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (polymarket-oracle/1.0)"}
FUNDING_PATH = os.path.join("D:/Hummingbot", "data", "funding.json")
FUNDING_HIST_PATH = os.path.join("D:/Hummingbot", "data", "funding_history.json")
SPOT_INST = "BTC-USDT"
PERP_INST = "BTC-USDT-SWAP"


def _update_history(payload, hist_path=FUNDING_HIST_PATH, keep=2016):
    """Архив ставок -> percentile (mid-rank) и positive_streak в фиде.

    По брифу: аномальность ставки меряем ПЕРЦЕНТИЛЕМ собственного архива
    (не абсолютными числами), вход в carry — ставка >= медианы архива.
    mid-rank (n_below + 0.5*n_equal)/n: при плоском архиве = 50, без падений.
    periods — по смене next_funding_ms (один расчёт OKX = одна запись),
    positive_streak — подряд положительных расчётов (контекст для входа).
    """
    try:
        with open(hist_path, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    rates = hist.get("rates") or []       # [[ts, rate_bps], ...] — 5-мин семплы
    periods = hist.get("periods") or []   # [[next_ms, rate_bps], ...] — по расчётам
    now = float(payload.get("ts") or time.time())
    rb = float(payload.get("rate_bps") or 0.0)
    if not rates or now - rates[-1][0] >= 60:
        rates.append([now, rb])
        rates = rates[-keep:]
    key = int(payload.get("next_funding_ms") or 0)
    if not periods or periods[-1][0] != key:
        periods.append([key, rb])
        periods = periods[-200:]
    hist = {"rates": rates, "periods": periods}
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)
    tmp = hist_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp, hist_path)
    # перцентиль (mid-rank), warmup: <100 семплов (~8ч) — None, гейт не блокирует
    pct = None
    if len(rates) >= 100:
        vals = [v for _, v in rates]
        below = sum(1 for v in vals if v < rb)
        equal = sum(1 for v in vals if v == rb)
        pct = round(100.0 * (below + 0.5 * equal) / len(vals), 1)
    streak = 0
    for _, v in reversed(periods):
        if v > 0:
            streak += 1
        else:
            break
    payload["percentile"] = pct
    payload["positive_streak"] = streak
    payload["history_n"] = len(rates)
    return payload


def fetch(proxies=None, timeout=15):
    """Ставка финансирования + базис спот/перп.

    rate      — доля за 8ч (0.0001 = 0.01% = 1 bps)
    rate_bps  — то же в bps за 8ч (входные пороги задаются в bps)
    basis_bps — (перп - спот) / спот * 10000
    """
    fr = requests.get("https://www.okx.com/api/v5/public/funding-rate",
                      params={"instId": PERP_INST}, headers=HEADERS,
                      timeout=timeout, proxies=proxies).json()
    data = fr.get("data") or [{}]
    rate = float(data[0].get("fundingRate") or 0.0)
    next_rate = data[0].get("nextFundingRate")
    ts_next = data[0].get("nextFundingTime")

    st = requests.get("https://www.okx.com/api/v5/market/ticker",
                      params={"instId": SPOT_INST}, headers=HEADERS,
                      timeout=timeout, proxies=proxies).json()["data"][0]
    pt = requests.get("https://www.okx.com/api/v5/market/ticker",
                      params={"instId": PERP_INST}, headers=HEADERS,
                      timeout=timeout, proxies=proxies).json()["data"][0]
    spot = float(st["last"])
    perp = float(pt["last"])
    return {
        "inst": PERP_INST,
        "rate": rate,
        "rate_bps": round(rate * 10000.0, 6),
        "next_rate_bps": (round(float(next_rate) * 10000.0, 6) if next_rate else None),
        "next_funding_ms": int(ts_next) if ts_next else None,
        "spot": spot,
        "perp": perp,
        "basis_bps": round((perp - spot) / spot * 10000.0, 4),
        "ts": time.time(),
    }


def write_feed(proxies=None, path=FUNDING_PATH, timeout=15):
    """Собрать фид (с архивом/перцентилем) и атомарно записать. Возвращает данные."""
    payload = fetch(proxies=proxies, timeout=timeout)
    try:
        payload = _update_history(payload)
    except Exception:
        payload.setdefault("percentile", None)
        payload.setdefault("positive_streak", 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return payload


if __name__ == "__main__":
    print(json.dumps(fetch(), ensure_ascii=False, indent=2))
