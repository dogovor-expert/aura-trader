# -*- coding: utf-8 -*-
"""Test groq gpt-oss models for trading verdict via proxy."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from proxyutil import build_proxies

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = json.load(open(os.path.join(BASE, "config", "config.json"), encoding="utf-8"))
proxies = build_proxies(cfg)
key = os.environ.get("GROQ_API_KEY", "")

SYS = ("You are a quant crypto analyst. Reply JSON only: "
       '{"signal":"LONG/SHORT/WAIT","confidence":0-1,"reasons":[]}')
USR = ('INPUT: {"btc_price":83823,"btc_chg_pct":0.43,"deltas":[]}. '
        "No deltas, BTC flat. Reply JSON.")

for model in ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": "Bearer " + key},
                          json={"model": model, "temperature": 0, "max_tokens": 200,
                                "messages": [{"role": "system", "content": SYS},
                                             {"role": "user", "content": USR}]},
                          proxies=proxies, timeout=60)
        print(f"{model}: HTTP {r.status_code}")
        print("  " + r.text[:500].replace("\n", " "))
    except Exception as e:
        print(f"{model}: FAIL {str(e)[:150]}")
