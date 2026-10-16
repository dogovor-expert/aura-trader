"""Мульти-источник цены BTC: OKX -> Kraken -> Coinbase -> CoinGecko -> Bybit."""
import requests
from typing import Tuple, Optional

HEADERS = {"User-Agent": "Mozilla/5.0 (polymarket-oracle/1.0)"}

def _get(url, params, timeout=15, proxies=None):
    r = requests.get(url, params=params, headers=HEADERS,
                     timeout=timeout, proxies=proxies)
    r.raise_for_status()
    return r.json()

def okx(timeout=15, proxies=None) -> float:
    d = _get("https://www.okx.com/api/v5/market/ticker",
             {"instId": "BTC-USDT"}, timeout, proxies)
    return float(d["data"][0]["last"])

def kraken(timeout=15, proxies=None) -> float:
    d = _get("https://api.kraken.com/0/public/Ticker",
             {"pair": "XBTUSD"}, timeout, proxies)
    k = list(d["result"].keys())[0]
    return float(d["result"][k]["c"][0])

def coinbase(timeout=15, proxies=None) -> float:
    d = _get("https://api.coinbase.com/v2/prices/BTC-USD/spot",
             {}, timeout, proxies)
    return float(d["data"]["amount"])

def coingecko(timeout=20, proxies=None) -> float:
    d = _get("https://api.coingecko.com/api/v3/simple/price",
             {"ids": "bitcoin", "vs_currencies": "usd"}, timeout, proxies)
    return float(d["bitcoin"]["usd"])

def bybit(category="spot", symbol="BTCUSDT", timeout=15, proxies=None) -> float:
    d = _get("https://api.bybit.com/v5/market/tickers",
             {"category": category, "symbol": symbol}, timeout, proxies)
    return float(d["result"]["list"][0]["lastPrice"])

ORDER = ["okx", "kraken", "coinbase", "coingecko", "bybit"]
FUNCS = {"okx": okx, "kraken": kraken, "coinbase": coinbase,
         "coingecko": coingecko, "bybit": bybit}

def get_btc_price(prefer: str = "okx", proxies=None) -> Tuple[float, str]:
    """Возвращает (цена, источник). Пробует prefer первым, дальше по ORDER."""
    seq = [prefer] + [s for s in ORDER if s != prefer]
    last_err = None
    for name in seq:
        try:
            return FUNCS[name](), name
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"все источники цены недоступны: {last_err}")

if __name__ == "__main__":
    p, s = get_btc_price()
    print(f"{p} via {s}")
