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

    Текущий срез из окна ИСКЛЮЧАЕТСЯ (series[:-1]): иначе медиана всегда
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


def update_btc_history(path: str, price: float, keep: int = 300) -> List[float]:
    """Кольцевой ряд цен BTC (5-мин) — нужен для z-score движения BTC."""
    import json
    series = []
    try:
        with open(path, encoding="utf-8") as f:
            series = json.load(f).get("prices") or []
    except Exception:
        series = []
    if price and series and series[-1] == price:
        return series
    if price:
        series.append(float(price))
        if len(series) > keep:
            del series[: len(series) - keep]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"prices": series}, f)
    return series


def _zscore(value: float, series: List[float], floor: float = 1e-9) -> float:
    if len(series) < 5:
        return 0.0
    m = sum(series) / len(series)
    sd = (sum((x - m) ** 2 for x in series) / len(series)) ** 0.5
    sd = max(sd, floor)
    return max(-10.0, min(10.0, (value - m) / sd))


def _logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    import math
    return math.log(p / (1.0 - p))


def poly_logit_z(prob_history: Dict[str, Any], k: int = 3) -> Dict[str, float]:
    """z-score логит-ROC вероятности (p_t - p_{t-k} в log-odds), кросс-секционно.

    По брифу (OpenMarket + методология): сырой (p_t-p_{t-k}) ломается у границ
    0/1, нормально считать ROC в logit-шкале; sigma берём по всем рынкам цикла
    (кросс-секционная аппроксимация окна 200-1000 баров — у нас keep=12).
    """
    rocs = {}
    for label, bucket in (prob_history or {}).items():
        if not isinstance(bucket, dict):
            continue
        for q, series in bucket.items():
            s = series or []
            if len(s) < k + 2:
                continue
            roc = _logit(s[-1]) - _logit(s[-1 - k])
            rocs[q] = roc
    if not rocs:
        return {}
    vals = list(rocs.values())
    m = sum(vals) / len(vals)
    sd = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5
    sd = max(sd, 0.005)
    return {q: max(-10.0, min(10.0, (r - m) / sd)) for q, r in rocs.items()}


def btc_zscore(btc_series: List[float]) -> Dict[str, float]:
    """z последнего 5-мин изменения BTC к распределению прошлых изменений."""
    if len(btc_series) < 8:
        return {"chg_pct": 0.0, "z": 0.0}
    chgs = [(btc_series[i] - btc_series[i - 1]) / btc_series[i - 1] * 100.0
            for i in range(1, len(btc_series))]
    return {"chg_pct": round(chgs[-1], 4),
            "z": round(_zscore(chgs[-1], chgs[:-1], floor=0.02), 2)}


def heuristic_signal(poly_snapshot, btc_price, btc_prev, threshold=0.10,
                     zinfo=None) -> Dict[str, Any]:
    """Эвристика дельт + двухфакторное подтверждение (zinfo из z_poly/z_btc).

    Правила качества (бриф, arXiv 2607.26245 — Polymarket лагает за Binance):
      - подтверждение = знак z_btc совпадает с дельтой и |z_btc| >= 1 (двухфактор);
      - сильное ПРОТИВОПОЛОЖНОЕ движение BTC (|z|>=2, знак против дельты) =
        запаздывающий/шумовой спайк книги -> вето направления;
      - тонкий рынок (spread/depth из CLOB) -> вето направления (illiquid).
    """
    zinfo = zinfo or {}
    z_poly_map = zinfo.get("z_poly") or {}
    z_btc = float(zinfo.get("z_btc") or 0.0)
    signals, notes = [], []
    btc_chg = ((btc_price - btc_prev) / btc_prev * 100) if btc_prev else 0.0
    for label, ev in poly_snapshot.items():
        if ev.get("error"):
            continue
        for m in ev.get("markets", []):
            # нас интересуют рынки с сохранённой дельтой — дельты считает main.py
            for q, p0, p1, d in m.get("_spikes", []):
                side = "BULLISH" if d > 0 else "BEARISH"
                book = m.get("_book") or {}
                thin = bool(book.get("thin"))
                zp = round(float(z_poly_map.get(q, 0.0)), 2)
                # двухфактор: z вероятности И z BTC ≥ порогов в знаке дельты
                two_factor = (abs(zp) >= 2.0 and abs(z_btc) >= 1.0
                              and (zp > 0) == (d > 0) and (z_btc > 0) == (d > 0))
                contra = abs(z_btc) >= 2.0 and (z_btc > 0) != (d > 0)
                # дивергенция: вероятность выросла, а BTC стоит/падает
                div = (d > 0 and btc_chg < 0.3) or (d < 0 and btc_chg > -0.3)
                # направление даёт дивергенция ЛИБО двухфакторное согласие
                # (тренд подтверждает движение вероятности) — чистое совпадение
                # без дельты остаётся WATCH.
                bull = d > 0 and (div or two_factor)
                bear = d < 0 and (div or two_factor)
                action = "WATCH_LONG" if bull else ("WATCH_SHORT" if bear else "WATCH")
                veto = None
                if thin:
                    action, veto = "WATCH", "thin_market"
                elif contra:
                    action, veto = "WATCH", "btc_contradicts_prob"
                signals.append({
                    "event": label, "market": q, "side": side,
                    "prev_prob": round(p0, 4), "cur_prob": round(p1, 4),
                    "delta_pp": round(d * 100, 2),
                    "btc_price": btc_price, "btc_chg_pct": round(btc_chg, 3),
                    "divergence": div, "action": action,
                    "z_poly": zp, "z_btc": z_btc,
                    "two_factor": two_factor, "veto": veto,
                    "spread_pts": book.get("spread_pts"),
                    "depth_usd": book.get("depth_usd"),
                })
    verdict = "NEUTRAL"
    if any(s["action"] == "WATCH_LONG" for s in signals):
        verdict = "BULLISH_DIVERGENCE"
    elif any(s["action"] == "WATCH_SHORT" for s in signals):
        verdict = "BEARISH_DIVERGENCE"
    return {"verdict": verdict, "btc_price": btc_price,
            "btc_chg_pct": round(btc_chg, 3), "signals": signals,
            "z_btc": z_btc, "z_poly_max": max([abs(float(v)) for v in z_poly_map.values()], default=0.0),
            "notes": notes or ["эвристика: дельта Polymarket + двухфактор (z-подтверждение)"]}

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
