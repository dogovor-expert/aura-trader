# -*- coding: utf-8 -*-
"""Check LLM providers reachability via proxy + validate keys if set.
Run: venv/Scripts/python src/check_llm.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm import PROVIDERS, call_llm, SYSTEM_PROMPT
from proxyutil import build_proxies, redact
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(BASE, "config", "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
proxies = build_proxies(cfg)
print("proxy:", redact(proxies["https"]) if proxies else "none (direct)")

TEST = "INPUT:\n{\"btc_price\": 83000, \"btc_chg_pct\": 0.1, \"heuristic_verdict\": \"NEUTRAL\", \"deltas\": []}"

for name, p in PROVIDERS.items():
    key = os.getenv(p["key_env"], "")
    if not key:
        # reachability probe: URL without key should give 401 (not timeout/block)
        import requests
        try:
            if name == "anthropic":
                r = requests.get(p["url"], proxies=proxies, timeout=20)
            elif name == "gemini":
                r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                                 proxies=proxies, timeout=20)
            else:
                r = requests.get(p["url"].replace("/chat/completions", "/models"),
                                 proxies=proxies, timeout=20)
            print(f"{name:10s} reachable={r.status_code} (no key, {p['key_env']} empty)")
        except Exception as e:
            print(f"{name:10s} UNREACHABLE: {str(e)[:120]}")
        continue
    try:
        ans = call_llm(name, p["default_model"], SYSTEM_PROMPT, TEST, proxies, 40)
        print(f"{name:10s} KEY OK model={p['default_model']} -> {ans[:150]}")
    except Exception as e:
        print(f"{name:10s} KEY FAIL: {str(e)[:200]}")
