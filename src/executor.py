"""Исполнитель: paper-режим + вебхук для Hummingbot + заготовка Bybit live."""
import datetime, json, os
from typing import Dict

def _ts():
    return datetime.datetime.now().isoformat(timespec="seconds")

def paper_execute(analysis: Dict, log_path: str):
    # Размер и выходы считаются по измеренной волатильности, а не по мнению
    # модели: см. src/sizing.py и arXiv 2609.05663.
    plan = analysis.get("trade_plan") or {}
    if plan:
        analysis = dict(analysis)
        analysis["size_mult"] = plan.get("size_mult", 1.0)
        analysis["stop_loss_pct"] = plan.get("stop_loss_pct")
        analysis["take_profit_pct"] = plan.get("take_profit_pct")
    lines = [f"[{_ts()}] verdict={analysis.get('verdict')} "
             f"BTC={analysis.get('btc_price')} ({analysis.get('btc_chg_pct')}%)"]
    news = analysis.get("news_sentiment") or {}
    if news:
        lines.append(f"  NEWS: pos={news.get('positive', 0)} neg={news.get('negative', 0)} "
                     f"neutral={news.get('neutral', 0)} net={news.get('score', 0)}")
    for tag in ("verify_l1", "verify_l2"):
        chk = analysis.get(tag)
        if not chk:
            continue
        if chk.get("ok"):
            lines.append(f"  VERIFY {tag[-2:].upper()}: clean, numbers match input")
        else:
            issues = "; ".join(chk.get("issues") or [])[:180]
            lines.append(f"  VERIFY {tag[-2:].upper()}: {issues}")
    for s in analysis.get("signals", [])[:15]:
        q = s['market'][:80].encode('ascii', 'replace').decode()
        lines.append(f"  {s['action']} {s['event']}: {q} "
                     f"{s['prev_prob']*100:.1f}%->{s['cur_prob']*100:.1f}% "
                     f"(d{s['delta_pp']:+.1f} pp) div={s['divergence']}")
    if len(analysis.get("signals", [])) > 15:
        lines.append(f"  ... +{len(analysis['signals'])-15} more")
    l1 = analysis.get("llm_l1")
    if l1:
        lines.append(f"  LLM L1: {l1.get('signal')} conf={l1.get('confidence')} "
                     f"regime={l1.get('regime')} edge={l1.get('edge_bps')}bps")
        for r in (l1.get("reasons") or [])[:3]:
            lines.append("    - " + str(r)[:120])
        if l1.get("null_hypothesis"):
            lines.append("    null: " + str(l1["null_hypothesis"])[:150])
    l2 = analysis.get("llm_l2")
    if l2:
        lines.append(f"  LLM L2: {l2.get('signal')} conf={l2.get('confidence')}")
    if analysis.get("final_signal"):
        lines.append(f"  FINAL: {analysis['final_signal']} "
                     f"conf={analysis.get('final_confidence')}")
    if plan:
        lines.append(f"  PLAN: size x{analysis.get('size_mult')} "
                     f"(vol={plan.get('vol_pct')}% ratio={plan.get('vol_ratio')} "
                     f"regime={plan.get('regime')}) "
                     f"SL={analysis.get('stop_loss_pct')}% TP={analysis.get('take_profit_pct')}%")
    if analysis.get("llm_note"):
        lines.append("  note: " + str(analysis["llm_note"])[:200])
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        print("\n".join(lines))
    except UnicodeEncodeError:
        print("\n".join(lines).encode("cp1251", "replace").decode("cp1251"))
    return lines

def send_to_hummingbot(signal: Dict, webhook_url: str, timeout=10,
                      proxies=None) -> str:
    """POST verdict to bridge/webhook. No URL -> skip."""
    if not webhook_url:
        return "skip: no webhook_url"
    import requests
    # bridge is local, never via proxy
    px = None if "127.0.0.1" in webhook_url or "localhost" in webhook_url else proxies
    r = requests.post(webhook_url, json={"signal": signal, "ts": _ts()},
                      timeout=timeout, proxies=px)
    return f"webhook {r.status_code}: {r.text[:200]}"

