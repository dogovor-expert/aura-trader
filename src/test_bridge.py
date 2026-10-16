# -*- coding: utf-8 -*-
"""Test bridge: POST fake LONG signal, check file."""
import json
import urllib.request

data = json.dumps({"signal": {"final": "LONG", "conf": 0.82, "btc": 83800,
                              "test": True}}).encode()
req = urllib.request.Request("http://127.0.0.1:8181/signal", data=data,
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=10).read().decode())
print(open("D:/Hummingbot/signals.json", encoding="utf-8").read()[:500])
