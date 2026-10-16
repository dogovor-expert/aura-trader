"""Аналитик: эвристика дельт + опциональный LLM. Без ключа работает offline."""
import os
from typing import Dict, Any, List

def load_history(path: str) -> Dict[str, Any]:
    import json
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_history(path: str, data: Dict[str, Any]):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _yes_prob(m: Dict[str, Any]) -> float:
    return float(m.get("yes_prob", 0) or 0)

def detect_spikes(prev_markets: List[Dict], cur_markets: List[Dict],
                  threshold: float = 0.02):
    """Сравнивает по question: возвращает [(question, prev, cur, delta)].

    threshold — в долях единицы (0.02 = 2 п.п.). Порог 0.10 означал 10 п.п.
    за один 5-минутный цикл — такое на Polymarket не случается НИКОГДА,
    поэтому дельта была всегда нулевой и система не выдала ни одного сигнала.

    Пропускает resolved/мертвые рынки (цены ровно 0/1) — они не несут сигнала.
    Новые рынки сигнализируют только если есть baseline (prev не пуст).
    """
    if not prev_markets:
        return []  # первый прогон = калибровка baseline, не сигналы
    prev = {m["question"]: _yes_prob(m) for m in prev_markets}
    spikes = []
    for m in cur_markets:
        if m.get("_resolved"):
            continue
        q = m["question"]
        cur = _yes_prob(m)
        if q in prev:
            d = cur - prev[q]
            if abs(d) >= threshold:
                spikes.append((q, prev[q], cur, d))
    return spikes


def update_prob_history(path: str, poly_snapshot: Dict[str, Any], keep: int = 12) -> Dict[str, Any]:
    """Кольцевой буфер вероятностей: даёт дельту не только к предыдущему циклу.

    Без этого сравнение шло только с 5-минутной точки назад, где шум рынка
    съедал всё движение. Теперь baseline = медиана последних N циклов (~1 час
    при цикле 300 с), а дельта отражает устойчивое изменение ожиданий.
    """
    hist = load_history(path) or {}
    for label, ev in (poly_snapshot or {}).items():
        if ev.get("error"):
            continue
        bucket = hist.setdefault(label, {})
        for m in (ev.get("markets") or []):
            q = m.get("question")
            if not q or m.get("_resolved"):
                continue
            series = bucket.setdefault(q, [])
            series.append(round(_yes_prob(m), 6))
            if len(series) > keep:
                del series[: len(series) - keep]
    save_history(path, hist)
    return hist


def baseline_markets(prob_history: Dict[str, Any], label: str) -> List[Dict]:
    """Восстанавливает prev-срез как медиану по окну (устойчиво к шуму).

    Текущий срез из окна ИСКЛЮЧАЁТСЯ (series[:-1]): иначе медиана всегда
    притягивается к текущей цене и дельта систематически занижается.
    """
    out = []
    bucket = (prob_history or {}).get(label) or {}
    for q, series in bucket.items():
        vals = [v for v in (series or [])[:-1] if v is not None and 0.001 < v < 0.999]
        if not vals:
            continue
        vals = sorted(vals)
        mid = len(vals) // 2
        med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        out.append({"question": q, "yes_prob": med})
    return out

def heuristic_signal(poly_snapshot, btc_price, btc_prev, threshold=0.10) -> Dict[str, Any]:
    signals, notes = [], []
    btc_chg = ((btc_price - btc_prev) / btc_prev * 100) if btc_prev else 0.0
    for label, ev in poly_snapshot.items():
        if ev.get("error"):
            continue
        for m in ev.get("markets", []):
            # нас интересуют рынки с сохранённой дельтой — дельты считает main.py
            for q, p0, p1, d in m.get("_spikes", []):
                side = "BULLISH" if d > 0 else "BEARISH"
                # дивергенция: вероятность выросла, а BTC стоит/падает
                div = (d > 0 and btc_chg < 0.3) or (d < 0 and btc_chg > -0.3)
                action = "WATCH_LONG" if (d > 0 and div) else ("WATCH_SHORT" if (d < 0 and div) else "WATCH")
                signals.append({
                    "event": label, "market": q, "side": side,
                    "prev_prob": round(p0, 4), "cur_prob": round(p1, 4),
                    "delta_pp": round(d * 100, 2),
                    "btc_price": btc_price, "btc_chg_pct": round(btc_chg, 3),
                    "divergence": div, "action": action,
                })
    verdict = "NEUTRAL"
    if any(s["action"] == "WATCH_LONG" for s in signals):
        verdict = "BULLISH_DIVERGENCE"
    elif any(s["action"] == "WATCH_SHORT" for s in signals):
        verdict = "BEARISH_DIVERGENCE"
    return {"verdict": verdict, "btc_price": btc_price,
            "btc_chg_pct": round(btc_chg, 3), "signals": signals,
            "notes": notes or ["эвристика: дельта Polymarket vs движение BTC"]}

def llm_review(prompt: str, provider="none", model="", timeout=60) -> str:
    """Опциональный LLM-слой. provider=none -> пропуск."""
    if provider in (None, "", "none"):
        return ""
    api_key = os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "[llm пропущен: нет API-ключа]"
    try:
        if provider == "openai":
            import requests
            r = requests.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model or "gpt-4o-mini",
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout)
            return r.json()["choices"][0]["message"]["content"]
        if provider == "ollama":
            import requests
            r = requests.post("http://localhost:11434/api/generate",
                json={"model": model or "llama3.1", "prompt": prompt,
                      "stream": False}, timeout=timeout)
            return r.json().get("response", "")
    except Exception as e:
        return f"[llm ошибка: {e}]"
    return "[llm: провайдер не поддерживается]"

def build_prompt(heur: Dict[str, Any]) -> str:
    import json
    return ("Ты — аналитик event-driven криптофонда. Polymarket — опережающий "
            "индикатор, цена BTC — отстающая. Оцени дивергенции и дай вердикт "
            "LONG/SHORT/WAIT с уверенностью 0-100 и horizon (минуты/часы).\n"
            f"Данные:\n{json.dumps(heur, ensure_ascii=False, indent=2)[:4000]}")
