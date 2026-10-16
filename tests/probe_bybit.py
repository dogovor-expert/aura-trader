# -*- coding: utf-8 -*-
"""Почему Bybit отдаёт 403 и через какой маршрут он работает."""
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import proxyutil  # noqa: E402

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HOSTS = [
    ("api.bybit.com v5", "https://api.bybit.com/v5/market/time"),
    ("api.bybit.com spot", "https://api.bybit.com/v5/market/tickers?category=spot"),
    ("api.bybit.com linear", "https://api.bybit.com/v5/market/tickers?category=linear"),
    ("api.bytick.com", "https://api.bytick.com/v5/market/time"),
    ("stream.bybit.com", "https://stream.bybit.com/v5/public/linear"),
]

proxies = None
try:
    proxies = proxyutil.build_proxies()
    print("proxy configured:", bool(proxies), list((proxies or {}).keys()))
except Exception as e:
    print("proxy build failed:", str(e)[:120])


def probe(label, url, use_proxy):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies if use_proxy else {}))
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=12) as r:
            body = r.read(120)
            print(f"  [{label:7}] OK  {r.status}  {body[:70]!r}")
    except Exception as e:
        print(f"  [{label:7}] FAIL {str(e)[:80]}")


for name, url in HOSTS:
    print(name)
    probe("direct", url, False)
    probe("proxy", url, True)