import json
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

TARGETS = [
    ("okx", "https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
    ("binance", "https://api.binance.com/api/v3/ping"),
    ("bybit", "https://api.bybit.com/v5/market/time"),
    ("bitget", "https://api.bitget.com/api/v2/ping"),
    ("bingx", "https://open-api.bingx.com/openApi/spot/v1/ticker/24hr?symbol=BTC-USDT"),
    ("kucoin", "https://api.kucoin.com/api/v1/market/allTickers"),
    ("gate", "https://api.gateio.ws/api/v4/spot/tickers"),
    ("mexc", "https://api.mexc.com/api/v3/ping"),
    ("huobi", "https://api.huobi.pro/market/tickers"),
    ("kraken", "https://api.kraken.com/0/public/Time"),
    ("coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/stats"),
    ("poloniex", "https://api.poloniex.com/markets/ticker24h"),
]

for name, url in TARGETS:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read(200)
            print(f"{name:10} OK   {r.status}  {body[:70]!r}")
    except Exception as e:
        print(f"{name:10} FAIL {str(e)[:70]}")