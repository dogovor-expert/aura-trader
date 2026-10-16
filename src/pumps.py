# -*- coding: utf-8 -*-
"""Сканер пампов по публичным API с подтверждением второй биржей.

Идея: аномалия на одной бирже — это часто спуфинг или собственная ошибка
котировки. Настоящее движение подтверждается тем, что вторая площадка
движется в ту же сторону.

Источники (без ключей, публичные эндпоинты):
  Binance  /api/v3/ticker/24hr        — все пары разом
  Bybit    /v5/market/tickers?category=linear
  OKX      /api/v5/market/tickers?instType=SWAP

Для каждой пары считаем:
  chg_5m / chg_1h / chg_24h (из klines), объём 5m против медианы часа,
  и итоговый признак пампа. Затем ищем пары, где движение совпало на ≥2
  биржах, и ранжируем по силе.

Результат: data/pumps.json (для панели) + digest() для промпта модели.
"""
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUMPS_PATH = os.path.join(BASE, "data", "pumps.json")
# Bybit (и ряд других бирж) отдают 403 на самодельный User-Agent и тем более
# через прокси. Рабочий заголовок — браузерный, соединение прямое.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Порог пампа: рост/падение за 5 минут плюс подтверждение объёмом.
MIN_MOVE_PCT = 2.0        # за 5 минут
MIN_VOLUME_MULT = 3.0     # объём 5m к медиане предыдущего часа
MIN_CONFIRM = 2           # число бирж с однонаправленным движением
MAX_PAIRS = 14            # сколько пар тянем с каждой биржи
CACHE_TTL = 150           # секунд между полными сканированиями

_cache = {"ts": 0.0, "data": None}


def _proxies():
    """Прокси оракула: у части бирж прямой доступ не работает."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import json as _json
        from proxyutil import build_proxies
        cfg_path = os.path.join(BASE, "config", "config.json")
        cfg = _json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
        return build_proxies(cfg)
    except Exception:
        return None


_proxies_cache = {"value": False}
_openers = {}


def _get_opener(use_proxy: bool):
    key = bool(use_proxy)
    if key in _openers:
        return _openers[key]
    if key:
        px = _proxies()
        if not px:
            return None
        op = urllib.request.build_opener(urllib.request.ProxyHandler(px))
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _openers[key] = op
    return op


def _get(url, timeout=12):
    """Сначала прямое соединение, при отказе — через прокси оракула."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last = None
    for use_proxy in (False, True):
        op = _get_opener(use_proxy)
        if op is None:
            continue
        try:
            with op.open(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
    raise last if last else RuntimeError("no route")


def _pct(a, b):
    try:
        if not b:
            return 0.0
        return (a - b) / b * 100.0
    except Exception:
        return 0.0


def binance(proxies=None):
    out = {}
    d = _get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
    quotes = [t for t in d if t.get("symbol", "").endswith("USDT")]
    quotes.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
    for t in quotes[: MAX_PAIRS * 3]:
        sym = t["symbol"]
        try:
            k = _get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=13", timeout=10)
        except Exception:
            continue
        if not k or len(k) < 7:
            continue
        closes = [float(x[4]) for x in k]
        vols = [float(x[5]) for x in k]
        chg_5m = _pct(closes[-1], closes[-2])
        chg_1h = _pct(closes[-1], closes[-6])
        chg_24h = float(t.get("priceChangePercent", 0) or 0)
        base_vol = statistics.median(vols[-7:-1]) or 0.0
        vol_mult = (vols[-1] / base_vol) if base_vol else 0.0
        out[sym] = {"exchange": "binance", "pair": sym,
                    "chg_5m": round(chg_5m, 3), "chg_1h": round(chg_1h, 3),
                    "chg_24h": round(chg_24h, 2), "vol_mult": round(vol_mult, 2),
                    "quote_volume": float(t.get("quoteVolume", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def bybit(proxies=None):
    out = {}
    d = _get("https://api.bybit.com/v5/market/tickers?category=linear", timeout=15)
    rows = [t for t in (d.get("result") or {}).get("list", [])
            if t.get("symbol", "").endswith("USDT")]
    rows.sort(key=lambda t: float(t.get("turnover24h", 0) or 0), reverse=True)
    for t in rows[: MAX_PAIRS * 2]:
        sym = t["symbol"]
        try:
            k = _get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
                     f"&interval=5&limit=13", timeout=10)
        except Exception:
            continue
        data = k.get("result") or {}
        rows_k = data.get("list") or []
        if len(rows_k) < 7:
            continue
        closes = [float(x[4]) for x in rows_k]
        vols = [float(x[5]) for x in rows_k]
        out[sym] = {"exchange": "bybit", "pair": sym,
                    "chg_5m": round(_pct(closes[0], closes[1]), 3),
                    "chg_1h": round(_pct(closes[0], closes[5]), 3),
                    "chg_24h": round(float(t.get("price24hPcnt", 0) or 0) * 100, 2),
                    "vol_mult": round((vols[0] / statistics.median(vols[1:6]))
                                     if statistics.median(vols[1:6]) else 0.0, 2),
                    "quote_volume": float(t.get("turnover24h", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def okx(proxies=None):
    out = {}
    d = _get("https://www.okx.com/api/v5/market/tickers?instType=SWAP", timeout=15)
    rows = [t for t in (d.get("data") or [])
            if t.get("instId", "").endswith("-USDT-SWAP")]
    rows.sort(key=lambda t: float(t.get("volCcy24h", 0) or 0), reverse=True)
    for t in rows[: MAX_PAIRS * 2]:
        sym = t["instId"]
        try:
            k = _get(f"https://www.okx.com/api/v5/market/candles?instId={sym}"
                     f"&bar=5m&limit=13", timeout=10)
        except Exception:
            continue
        rows_k = k.get("data") or []
        if len(rows_k) < 7:
            continue
        closes = [float(x[4]) for x in rows_k]
        vols = [float(x[5]) for x in rows_k]
        out[sym] = {"exchange": "okx", "pair": sym.replace("-SWAP", ""),
                    "chg_5m": round(_pct(closes[0], closes[1]), 3),
                    "chg_1h": round(_pct(closes[0], closes[5]), 3),
                    "chg_24h": round(float(t.get("last24hPct", "0") or 0) * 100, 2),
                    "vol_mult": round((vols[0] / statistics.median(vols[1:6]))
                                     if statistics.median(vols[1:6]) else 0.0, 2),
                    "quote_volume": float(t.get("volCcy24h", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def gate(proxies=None):
    """Gate.io — второй эшелон, в т.ч. из брифа владельца про закрытый кошелёк."""
    out = {}
    tickers = _get("https://api.gateio.ws/api/v4/spot/tickers", timeout=15)
    rows = [t for t in tickers if t.get("currency_pair", "").endswith("_USDT")]
    rows.sort(key=lambda t: float(t.get("quote_volume", 0) or 0), reverse=True)
    for t in rows[: MAX_PAIRS * 2]:
        sym = t["currency_pair"]
        try:
            k = _get("https://api.gateio.ws/api/v4/spot/candlesticks"
                     f"?currency_pair={sym}&interval=5m&limit=13", timeout=10)
        except Exception:
            continue
        if not k or len(k) < 7:
            continue
        closes = [float(x[2]) for x in k]      # [t, v, close, high, low, open, sum]
        vols = [float(x[1]) for x in k]
        out[sym.replace("_", "")] = {"exchange": "gate", "pair": sym.replace("_", "/"),
                                     "chg_5m": round(_pct(closes[-1], closes[-2]), 3),
                                     "chg_1h": round(_pct(closes[-1], closes[-6]), 3),
                                     "chg_24h": round(float(t.get("change_percentage", 0) or 0), 2),
                                     "vol_mult": round((vols[-1] / statistics.median(vols[-7:-1]))
                                                       if statistics.median(vols[-7:-1]) else 0.0, 2),
                                     "quote_volume": float(t.get("quote_volume", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def huobi(proxies=None):
    """HTX/Huobi."""
    out = {}
    d = _get("https://api.huobi.pro/market/tickers", timeout=15)
    rows = [t for t in (d.get("data") or []) if t.get("symbol", "").endswith("usdt")]
    rows.sort(key=lambda t: float(t.get("amount", 0) or 0), reverse=True)
    for t in rows[: MAX_PAIRS * 2]:
        sym = t["symbol"]
        try:
            k = _get(f"https://api.huobi.pro/market/history/kline?symbol={sym}&period=5min&size=13",
                     timeout=10)
        except Exception:
            continue
        rows_k = k.get("data") or []
        if len(rows_k) < 7:
            continue
        closes = [float(x[4]) for x in rows_k]
        vols = [float(x[5]) for x in rows_k]
        out[sym.upper()] = {"exchange": "huobi", "pair": sym.upper(),
                            "chg_5m": round(_pct(closes[-1], closes[-2]), 3),
                            "chg_1h": round(_pct(closes[-1], closes[-6]), 3),
                            "chg_24h": round(float(t.get("changeRate", 0) or 0) * 100, 2),
                            "vol_mult": round((vols[-1] / statistics.median(vols[-7:-1]))
                                              if statistics.median(vols[-7:-1]) else 0.0, 2),
                            "quote_volume": float(t.get("amount", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def bingx(proxies=None):
    """BingX — тоже доступен с этой сети."""
    out = {}
    d = _get("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr?symbol=BTC-USDT", timeout=15)
    tickers = _get("https://open-api.bingx.com/openApi/spot/v1/tickers", timeout=15)
    rows = [t for t in (tickers.get("data") or []) if t.get("symbol", "").endswith("-USDT")]
    rows.sort(key=lambda t: float(t.get("quoteVolume", 0) or 0), reverse=True)
    for t in rows[: MAX_PAIRS * 2]:
        sym = t["symbol"]
        try:
            k = _get("https://open-api.bingx.com/openApi/spot/v1/kline"
                     f"?symbol={sym}&interval=5m&limit=13", timeout=10)
        except Exception:
            continue
        rows_k = k.get("data") or []
        if len(rows_k) < 7:
            continue
        rows_k = list(reversed(rows_k))
        closes = [float(x[2]) for x in rows_k]
        vols = [float(x[5]) for x in rows_k]
        out[sym.replace("-", "")] = {"exchange": "bingx", "pair": sym.replace("-", "/"),
                                     "chg_5m": round(_pct(closes[-1], closes[-2]), 3),
                                     "chg_1h": round(_pct(closes[-1], closes[-6]), 3),
                                     "chg_24h": round(float(t.get("priceChangePercent", 0) or 0), 2),
                                     "vol_mult": round((vols[-1] / statistics.median(vols[-7:-1]))
                                                       if statistics.median(vols[-7:-1]) else 0.0, 2),
                                     "quote_volume": float(t.get("quoteVolume", 0) or 0)}
        if len(out) >= MAX_PAIRS:
            break
    return out


def coinbase(proxies=None):
    """Coinbase (пары USD — для криптовалют это практический аналог USDT)."""
    out = {}
    tickers = _get("https://api.exchange.coinbase.com/products?status=online", timeout=15)
    usd = [t["id"] for t in tickers if t.get("quote_currency") == "USD"][:60]
    for pid in usd:
        if not pid.startswith(("BTC-", "ETH-", "SOL-", "XRP-", "DOGE-", "LINK-", "AVAX-")):
            continue
        try:
            k = _get(f"https://api.exchange.coinbase.com/products/{pid}/candles?granularity=300",
                     timeout=8)
        except Exception:
            continue
        if not k or len(k) < 7:
            continue
        rows_k = sorted(k, key=lambda x: x[0])
        closes = [float(x[4]) for x in rows_k]
        vols = [float(x[5]) for x in rows_k]
        base = pid.split("-")[0]
        out[base + "USD"] = {"exchange": "coinbase", "pair": pid.replace("-", "/"),
                             "chg_5m": round(_pct(closes[-1], closes[-2]), 3),
                             "chg_1h": round(_pct(closes[-1], closes[-6]), 3),
                             "chg_24h": round(_pct(closes[-1], closes[0]) * 1.0, 2),
                             "vol_mult": round((vols[-1] / statistics.median(vols[-7:-1]))
                                               if statistics.median(vols[-7:-1]) else 0.0, 2),
                             "quote_volume": 0.0}
    return out


FEEDS = (("binance", binance), ("bybit", bybit), ("okx", okx), ("gate", gate),
         ("coinbase", coinbase), ("huobi", huobi))


def _base(pair):
    return pair.replace("-SWAP", "").replace("/", "").replace(":", "").upper()


def scan(force=False):
    """Полное сканирование. Возвращает подтверждённые пампы."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    per_exchange = {}
    errors = {}
    for name, fn in FEEDS:
        try:
            per_exchange[name] = fn()
        except Exception as e:
            errors[name] = str(e)[:120]

    # Группируем по базовому активу между биржами.
    grouped = {}
    for ex, items in per_exchange.items():
        for pair, row in items.items():
            grouped.setdefault(_base(pair), []).append(row)

    pumps = []
    for base, rows in grouped.items():
        ups = [r for r in rows if r["chg_5m"] >= MIN_MOVE_PCT and r["vol_mult"] >= MIN_VOLUME_MULT]
        downs = [r for r in rows if r["chg_5m"] <= -MIN_MOVE_PCT and r["vol_mult"] >= MIN_VOLUME_MULT]
        for side, group in (("UP", ups), ("DOWN", downs)):
            if len(group) < MIN_CONFIRM:
                continue
            best = max(group, key=lambda r: abs(r["chg_5m"]))
            pumps.append({
                "symbol": base,
                "side": side,
                "chg_5m": best["chg_5m"],
                "chg_1h": best["chg_1h"],
                "chg_24h": best["chg_24h"],
                "vol_mult": best["vol_mult"],
                "confirmations": len(group),
                "exchanges": sorted(r["exchange"] for r in group),
                "quote_volume": max(r["quote_volume"] for r in group),
                "strength": round(abs(best["chg_5m"]) * len(group), 2),
            })

    pumps.sort(key=lambda p: -p["strength"])
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanned": {k: len(v) for k, v in per_exchange.items()},
        "errors": errors,
        "thresholds": {"min_move_5m_pct": MIN_MOVE_PCT, "min_volume_mult": MIN_VOLUME_MULT,
                       "min_confirmations": MIN_CONFIRM},
        "pumps": pumps[:10],
    }
    try:
        os.makedirs(os.path.dirname(PUMPS_PATH), exist_ok=True)
        with open(PUMPS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _cache["ts"] = now
    _cache["data"] = payload
    return payload


def load():
    try:
        with open(PUMPS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pumps": [], "scanned": {}, "errors": {}}


def digest(max_items=6, max_chars=900):
    """Короткий блок для промпта: только подтверждённые пампы."""
    data = load()
    pumps = data.get("pumps") or []
    if not pumps:
        return "PUMPS: none confirmed on 2+ exchanges"
    lines = []
    for p in pumps[:max_items]:
        lines.append(f"{p['side']} {p['symbol']} 5m={p['chg_5m']}% 1h={p['chg_1h']}% "
                     f"volx{p['vol_mult']} confirm={p['confirmations']}({','.join(p['exchanges'])})")
    return ("PUMPS (confirmed on 2+ exchanges, context only):\n" + "\n".join(lines))[:max_chars]


if __name__ == "__main__":
    import sys
    data = scan(force="--force" in sys.argv)
    print(json.dumps({k: v for k, v in data.items() if k != "pumps"},
                     ensure_ascii=False, indent=2))
    print(f"\nconfirmed pumps: {len(data['pumps'])}")
    for p in data["pumps"]:
        print(f"  {p['side']:4} {p['symbol']:12} 5m={p['chg_5m']:+6.2f}% "
              f"1h={p['chg_1h']:+6.2f}% vol×{p['vol_mult']:<5} "
              f"confirm={p['confirmations']} ({','.join(p['exchanges'])})")