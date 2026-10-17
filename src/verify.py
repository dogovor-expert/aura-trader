# -*- coding: utf-8 -*-
"""Проверка ответа модели: каждое число должно существовать во входе.

Зачем: по данным исследований (arXiv 2604.26091) модели «выдумывают» правила
и цифры — rule fabrication до 57% кейсов. Если модель сослалась на дельту
0.87 п.п., которой нет во входе, значит вердикт построен на мусоре.

Что проверяем:
  * confidence вне [0, 1]              -> clamp + замечание;
  * delta_pp, которого нет во входе    -> фабрикация;
  * любое число в reasons/reasons_text, отсутствующее во входе -> фабрикация;
  * edge_bps вне разумных границ       -> замечание;
  * regime/signal не из разрешённых    -> замечание.

Вердикт не выбрасывается: фабрикация срезает уверенность до FABRICATION_CAP,
поэтому при trade_conf выше порога решение автоматически станет WAIT.
"""
import re
from typing import Any, Dict, List, Tuple

ALLOWED_SIGNALS = ("LONG", "SHORT", "WAIT")
ALLOWED_REGIMES = ("trend", "range", "volatile", "illiquid")
FABRICATION_CAP = 0.2
MAX_EDGE_BPS = 500.0

NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?%?")


def _f(value) -> float:
    try:
        return float(str(value).replace(",", ".").rstrip("%"))
    except (TypeError, ValueError):
        return None


def allowed_numbers(heur: Dict[str, Any], extra: List[float] = None) -> List[float]:
    """Все числа, которые legitimately есть во входе (+ числа из конфига).

    extra — значения из настроек (порог уверенности, счётчики бирж/сигналов):
    модель их законно называет в тексте, поэтому они не должны считаться
    выдумкой (раньше «2 биржи» помечалось как fabrication).
    """
    out = []
    for key in ("btc_price", "btc_chg_pct"):
        v = _f(heur.get(key))
        if v is not None:
            out.append(v)
    for s in heur.get("signals", []) or []:
        for key in ("prev_prob", "cur_prob", "delta_pp"):
            v = _f(s.get(key))
            if v is not None:
                out.append(v)
    sentiment = (heur.get("news_sentiment") or {}).get("score")
    v = _f(sentiment)
    if v is not None:
        out.append(v)
    for x in (extra or []):
        v = _f(x)
        if v is not None:
            out.append(v)
    return out


def _close(value: float, pool: List[float], rel_tol=0.02, abs_tol=0.05) -> bool:
    for p in pool:
        if abs(value - p) <= abs_tol or (abs(p) > 1e-9 and abs(value - p) / abs(p) <= rel_tol):
            return True
    return False


def verify_verdict(verdict: Dict[str, Any], heur: Dict[str, Any],
                   extra: List[float] = None) -> Dict[str, Any]:
    """Возвращает {ok, issues, fabricated, capped, confidence}."""
    issues: List[str] = []
    fabricated: List[str] = []
    if not isinstance(verdict, dict):
        return {"ok": False, "issues": ["verdict is not a dict"], "fabricated": [],
                "capped": True, "confidence": FABRICATION_CAP}

    pool = allowed_numbers(heur, extra)
    pool_abs = [abs(p) for p in pool]
    v = dict(verdict)

    signal = str(v.get("signal", "")).upper()
    if signal not in ALLOWED_SIGNALS:
        issues.append(f"unknown signal: {v.get('signal')!r}")
        v["signal"] = "WAIT"

    regime = str(v.get("regime", "")).lower()
    if regime and regime not in ALLOWED_REGIMES:
        issues.append(f"unknown regime: {v.get('regime')!r}")

    conf = _f(v.get("confidence"))
    if conf is None:
        issues.append("confidence missing")
        conf = 0.0
    elif not (0.0 <= conf <= 1.0):
        issues.append(f"confidence out of range: {conf}")
        conf = max(0.0, min(1.0, conf))

    edge = _f(v.get("edge_bps"))
    if edge is not None and abs(edge) > MAX_EDGE_BPS:
        issues.append(f"edge_bps implausible: {edge}")

    # delta_pp сверяется только с дельтами из входа, а не со всеми числами:
    # когда дельт нет, модель законно пишет 0, и это НЕ выдумка.
    signals = heur.get("signals") or []
    delta = _f(v.get("delta_pp"))
    if delta is not None:
        delta_pool = [abs(_f(s.get("delta_pp"))) for s in signals
                      if _f(s.get("delta_pp")) is not None]
        if not delta_pool:
            if abs(delta) > 0.5:
                fabricated.append(f"delta_pp={delta} при пустом входе дельт")
        elif not _close(abs(delta), delta_pool):
            fabricated.append(f"delta_pp={delta}")

    # Числа в текстовых полях должны встречаться во входе.
    text_parts = []
    for key in ("reasons", "null_hypothesis", "reason", "note"):
        val = v.get(key)
        if isinstance(val, (list, tuple)):
            text_parts.extend(str(x) for x in val)
        elif val:
            text_parts.append(str(val))
    for chunk in text_parts:
        for token in NUM_RE.findall(chunk):
            num = _f(token)
            if num is None:
                continue
            # Знак не важен: модель законно называет модуль числа из входа
            # («BTC -0.366%» -> в тексте «0.366%»), это не выдумка.
            if (_close(num, pool) or _close(abs(num), pool_abs)
                    or _close(num, [conf], abs_tol=0.01)):
                continue
            # Малые целые — это счётчики и пороги из конфига («2 биржи»,
            # «45 bps»), а не выдуманные измеримые величины. Дробные
            # (0.87, 12.5) и большие числа по-прежнему проверяются.
            if num == int(num) and abs(num) <= 20:
                continue
            fabricated.append(f"{key}:{token}")

    fabricated = sorted(set(fabricated))
    capped = False
    if fabricated:
        issues.append("fabricated numbers: " + ", ".join(fabricated[:5]))
        conf = min(conf, FABRICATION_CAP)
        capped = True

    return {
        "ok": not issues,
        "issues": issues,
        "fabricated": fabricated,
        "capped": capped,
        "confidence": round(conf, 4),
        "verdict": v,
    }


def format_note(tag: str, check: Dict[str, Any]) -> str:
    """Строка для лога."""
    if check["ok"]:
        return f"VERIFY {tag}: clean (conf {check['confidence']})"
    parts = "; ".join(check["issues"])[:220]
    return f"VERIFY {tag}: {parts} (conf -> {check['confidence']})"