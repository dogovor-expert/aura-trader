"""Проверка моделей anymodel.org для L1/L2 оракула.

Запуск: venv\\Scripts\\python src\\check_anymodel.py [model ...]
Ключ берётся из ANYMODEL_API_KEY (пользовательская переменная окружения).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from llm import SYSTEM_PROMPT  # noqa: E402

URL = "https://anymodel.org/v1/chat/completions"
DEFAULT_MODELS = ["cx/gpt-6-luna", "cc/claude-haiku-4-5-20251001"]

USER = (
    "INPUT:\n" + json.dumps({
        "btc_price": 82000.0,
        "btc_chg_pct": -0.13,
        "heuristic_verdict": "NEUTRAL",
        "deltas": [{"event": "FED_OCT", "market": "No change in Fed rate after October meeting",
                    "prev": 0.68, "cur": 0.725, "d_pp": 4.5}],
    })
)


def check(model, timeout=60):
    key = os.getenv("ANYMODEL_API_KEY", "")
    if not key:
        return {"model": model, "ok": False, "error": "ANYMODEL_API_KEY is not set"}
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER},
        ],
    }
    try:
        r = requests.post(URL, headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        if r.status_code != 200:
            return {"model": model, "ok": False, "http": r.status_code,
                    "error": r.text[:200]}
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return {"model": model, "ok": False, "error": str(e)[:200]}

    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        verdict = json.loads(text)
        parsed = True
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        try:
            verdict = json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
            parsed = bool(verdict)
        except Exception:
            verdict = {}
            parsed = False
    return {
        "model": model,
        "ok": parsed,
        "signal": verdict.get("signal"),
        "confidence": verdict.get("confidence"),
        "regime": verdict.get("regime"),
        "usage": r.json().get("usage", {}) if r else {},
        "raw": text[:220],
    }


if __name__ == "__main__":
    models = sys.argv[1:] or DEFAULT_MODELS
    for m in models:
        print(json.dumps(check(m), ensure_ascii=False, indent=2))