# -*- coding: utf-8 -*-
"""Test system LLM keys via proxy: Groq, OpenAI, DeepSeek, OpenRouter."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from proxyutil import build_proxies

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = json.load(open(os.path.join(BASE, "config", "config.json"), encoding="utf-8"))
proxies = build_proxies(cfg)

def test(name, url, key, body=None):
    if not key:
        print(f"{name}: no key"); return
    try:
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        if body:
            r = requests.post(url, headers=h, json=body, proxies=proxies, timeout=40)
        else:
            r = requests.get(url, headers=h, proxies=proxies, timeout=25)
        print(f"{name}: HTTP {r.status_code} len={len(r.text)}")
        print("  " + r.text[:400].replace("\n", " "))
    except Exception as e:
        print(f"{name}: FAIL {str(e)[:200]}")

CHAT = {"model": "MODEL", "temperature": 0, "max_tokens": 50,
        "messages": [{"role": "user", "content": 'Reply {"signal":"WAIT"} only'}]}

test("groq-list", "https://api.groq.com/openai/v1/models", os.getenv("GROQ_API_KEY"))
b = dict(CHAT); b["model"] = "llama-3.3-70b-versatile"
test("groq-chat", "https://api.groq.com/openai/v1/chat/completions", os.getenv("GROQ_API_KEY"), b)
test("openai-list", "https://api.openai.com/v1/models", os.getenv("OPENAI_API_KEY"))
b = dict(CHAT); b["model"] = "gpt-4o-mini"
test("openai-chat", "https://api.openai.com/v1/chat/completions", os.getenv("OPENAI_API_KEY"), b)
test("deepseek-balance", "https://api.deepseek.com/user/balance", os.getenv("DEEPSEEK_API_KEY"))
test("openrouter-list", "https://openrouter.ai/api/v1/models", "x")
