# -*- coding: utf-8 -*-
"""Живая проверка verify.py на настоящем ответе модели anymodel."""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import llm  # noqa: E402
from verify import verify_verdict  # noqa: E402

cfg = json.load(open(os.path.join(BASE, "config", "config.json"), encoding="utf-8"))
llm_cfg = cfg.get("llm", {})

heur = {
    "btc_price": 82150.0,
    "btc_chg_pct": 0.42,
    "verdict": "NEUTRAL",
    "signals": [{"prev_prob": 0.55, "cur_prob": 0.61, "delta_pp": 6.0}],
    "news_sentiment": {"score": 0.12},
}

user = llm.build_user_prompt(heur)
prov = llm_cfg.get("provider")
model = llm_cfg.get("l1_model")
raw = llm.call_llm(prov, model, llm.SYSTEM_PROMPT, user, timeout=60)
v = llm.parse_verdict(raw)
print("RAW VERDICT:", json.dumps(v, ensure_ascii=False)[:300])
check = verify_verdict(v, heur)
print("VERIFY:", json.dumps(check, ensure_ascii=False)[:500])
print("SIGNAL AFTER VERIFY:", check["verdict"].get("signal"), "conf", check["confidence"])