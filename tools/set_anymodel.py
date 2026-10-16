"""Обновляет блок llm в config/config.json под провайдера anymodel.org.

Запуск: venv\\Scripts\\python tools/set_anymodel.py
"""
import io
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(BASE, "config", "config.json")

with io.open(CFG, encoding="utf-8") as f:
    cfg = json.load(f)

llm = cfg.setdefault("llm", {})
llm.update({
    "provider": "anymodel",
    "l1_model": "cx/gpt-6-luna",
    "l2_enabled": True,
    "l2_provider": "anymodel",
    "l2_model": "cc/claude-haiku-4-5-20251001",
    "l2_trigger_conf": 0.65,
    "trade_conf": 0.75,
    "timeout": 60,
    "note": ("L1=cx/gpt-6-luna, L2=cc/claude-haiku-4-5-20251001 via anymodel.org "
             "(OpenAI-compatible). Key in ANYMODEL_API_KEY (user env). "
             "Fallback without paid keys: groq openai/gpt-oss-120b + qwen/qwen3.8-27b."),
})

bak = CFG + ".bak-anymodel"
if not os.path.exists(bak):
    with io.open(bak, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2))

with io.open(CFG, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print(json.dumps(llm, ensure_ascii=False, indent=2))