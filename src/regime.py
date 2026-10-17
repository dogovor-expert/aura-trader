# -*- coding: utf-8 -*-
"""Классификация рыночного режима BTC (regime) для фильтрации сигналов.

По данным walk-forward исследования режимов (espressodoppio, собрано в брифе):
режим работает как ФИЛЬТР (голосование 2-of-3: ADX>=20 | ER>=0.30 | CHOP<=55),
а не как свитчер: переключение стратегий по режиму дало -392 bps, фильтрация —
положительна. Волатильностьный режим предсказуем (ATR-перцентили), направленный —
нет, поэтому направление мы НЕ пытаемся предсказывать — только фильтруем входы.

Формулы:
  ER (Kaufman Efficiency Ratio) = |C_t - C_{t-N}| / sum(|dC_i|), N=20
  CHOP = 100 * log10(sum(TR_n) / (max_H - min_L)) / log10(n)
  ADX(14) по Уайлдеру (TR, +DM, -DM -> DX -> сглаживание 1/period)
Гистерезис: выйти из trend можно только при ADX>=20 и votes>=1 (гистерезис порога 25->20).
"""
import json
import math
import os
from typing import Any, Dict, List, Optional

OKX_CANDLES = "https://www.okx.com/api/v5/market/candles"
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "regime_state.json")


def _f(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def fetch_candles(limit: int = 300, proxies=None, timeout: int = 20) -> Optional[List[Dict[str, float]]]:
    """BTC-USDT 5m свечи OKX (хронологически). None при ошибке."""
    import requests
    r = requests.get(OKX_CANDLES, params={"instId": "BTC-USDT", "bar": "5m",
                                          "limit": min(limit, 300)},
                     timeout=timeout, proxies=proxies)
    r.raise_for_status()
    rows = r.json().get("data") or []
    out = []
    for row in reversed(rows):  # OKX отдаёт новые первыми
        try:
            out.append({"o": _f(row[1]), "h": _f(row[2]),
                        "l": _f(row[3]), "c": _f(row[4])})
        except Exception:
            continue
    return out or None


def _wilder(vals: List[float], period: int) -> List[float]:
    """Wilder smoothing: первый = среднее первых period, далее (prev*(p-1)+x)/p."""
    if len(vals) < period:
        return []
    out = [sum(vals[:period]) / period]
    for x in vals[period:]:
        out.append((out[-1] * (period - 1) + x) / period)
    return out


def adx(candles: List[Dict[str, float]], period: int = 14) -> float:
    if len(candles) < period * 2 + 1:
        return 0.0
    trs, pdm, ndm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - candles[i - 1]["h"], candles[i - 1]["l"] - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    atr_s = _wilder(trs, period)
    ps = _wilder(pdm, period)
    ns = _wilder(ndm, period)
    dx = []
    for a, p, n in zip(atr_s, ps, ns):
        if a <= 0:
            dx.append(0.0)
            continue
        pdi, ndi = 100.0 * p / a, 100.0 * n / a
        s = pdi + ndi
        dx.append(100.0 * abs(pdi - ndi) / s if s else 0.0)
    if len(dx) < period:
        return dx[-1] if dx else 0.0
    return sum(dx[-period:]) / period


def efficiency_ratio(closes: List[float], n: int = 20) -> float:
    if len(closes) <= n:
        return 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    if path <= 0:
        return 0.0
    return abs(closes[-1] - closes[-1 - n]) / path


def choppiness(candles: List[Dict[str, float]], n: int = 14) -> float:
    if len(candles) < n + 1:
        return 50.0
    tail = candles[-(n + 1):]
    trs = [max(t["h"] - t["l"], abs(t["h"] - p["c"]), abs(t["l"] - p["c"]))
           for t, p in zip(tail[1:], tail[:-1])]
    rng = max(c["h"] for c in tail) - min(c["l"] for c in tail)
    if rng <= 0 or sum(trs) <= 0:
        return 50.0
    val = 100.0 * math.log10(sum(trs) / rng) / math.log10(n)
    return max(0.0, min(100.0, val))


def atr_pct_series(candles: List[Dict[str, float]], period: int = 14) -> List[float]:
    if len(candles) < period + 2:
        return []
    trs = [max(t["h"] - t["l"], abs(t["h"] - p["c"]), abs(t["l"] - p["c"]))
           for t, p in zip(candles[1:], candles[:-1])]
    atr_s = _wilder(trs, period)
    out = []
    for i, a in enumerate(atr_s):
        c = candles[i + period]["c"]
        if c > 0:
            out.append(a / c * 100.0)
    return out


def percentile_of(value: float, series: List[float]) -> float:
    """Перцентиль value внутри series (0..100)."""
    if not series:
        return 50.0
    below = sum(1 for v in series if v < value)
    return round(100.0 * below / len(series), 1)


def bb_width_pct(closes: List[float], n: int = 20) -> List[float]:
    if len(closes) < n + 1:
        return []
    out = []
    for i in range(n, len(closes)):
        w = closes[i - n:i]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        if m > 0:
            out.append(4.0 * sd / m * 100.0)  # (upper-lower)/mid = 4*sd/mid
    return out


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def classify(candles: Optional[List[Dict[str, float]]] = None,
             proxies=None) -> Dict[str, Any]:
    """-> {regime: trend/range/unknown, vol: high/mid/low, adx, er, chop, votes,
    atr_pct, atr_pctile, bb_width_pct, bb_pctile, note}"""
    if candles is None:
        try:
            candles = fetch_candles(proxies=proxies)
        except Exception:
            candles = None
    if not candles or len(candles) < 40:
        return {"regime": "unknown", "vol": "unknown", "adx": 0.0, "er": 0.0,
                "chop": 50.0, "votes": 0, "atr_pct": 0.0, "atr_pctile": 50.0,
                "bb_width_pct": 0.0, "bb_pctile": 50.0,
                "note": "нет свечей OKX — режим неизвестен, фильтр не применяется"}
    closes = [c["c"] for c in candles]
    a = adx(candles)
    er = efficiency_ratio(closes)
    ch = choppiness(candles)
    votes = int(a >= 20.0) + int(er >= 0.30) + int(ch <= 55.0)
    prev = _load_state().get("regime")
    trend = votes >= 2
    # гистерезис: из trend выходим только при ADX>=20 (порог 25 вход / 20 выход)
    if prev == "trend" and not trend and a >= 20.0 and votes >= 1:
        trend = True
    regime = "trend" if trend else "range"
    atr_s = atr_pct_series(candles)
    atr_now = atr_s[-1] if atr_s else 0.0
    atr_pctile = percentile_of(atr_now, atr_s) if atr_s else 50.0
    bbw_s = bb_width_pct(closes)
    bbw_now = bbw_s[-1] if bbw_s else 0.0
    bb_pctile = percentile_of(bbw_now, bbw_s) if bbw_s else 50.0
    vol = "high" if (atr_pctile >= 80 or bb_pctile >= 80) else \
          ("low" if (atr_pctile <= 20 and bb_pctile <= 20) else "mid")
    _save_state({"regime": regime, "adx": round(a, 1),
                 "votes": votes, "atr_pctile": atr_pctile})
    return {"regime": regime, "vol": vol, "adx": round(a, 1),
            "er": round(er, 3), "chop": round(ch, 1), "votes": votes,
            "atr_pct": round(atr_now, 3), "atr_pctile": atr_pctile,
            "bb_width_pct": round(bbw_now, 3), "bb_pctile": bb_pctile,
            "note": f"голос 2-of-3: ADX{'+' if a >= 20 else '-'} "
                    f"ER{'+' if er >= 0.30 else '-'} CHOP{'+' if ch <= 55 else '-'}"}


if __name__ == "__main__":
    print(json.dumps(classify(), ensure_ascii=False, indent=1))
