"""Orchestrator: sensor -> price -> analyst -> llm cascade -> executor."""
import json, os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sensor import poll_all
from price import get_btc_price
from analyst import (load_history, save_history, detect_spikes,
                      update_prob_history, baseline_markets,
                      heuristic_signal)
from proxyutil import build_proxies, redact
from llm import run_cascade
from executor import paper_execute
import news as news_mod
import pumps as pumps_mod
import sizing as sizing_mod

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(BASE, "config", "config.json")
HIST_PATH = os.path.join(BASE, "data", "history.json")
PROB_HIST_PATH = os.path.join(BASE, "data", "prob_history.json")
PRICE_PATH = os.path.join(BASE, "data", "last_price.json")
LOG_PATH = os.path.join(BASE, "logs", "signals.log")

def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)

def one_cycle(cfg):
    proxies = build_proxies(cfg)
    if proxies:
        print("proxy: " + redact(proxies.get("https", "")))
    slugs = [(e["slug"], e["label"]) for e in cfg["polymarket_events"]]
    snap = poll_all(slugs, proxies=proxies)
    prefer = cfg.get("price_source", {}).get("primary", "okx")
    try:
        btc, src = get_btc_price(prefer, proxies)
    except Exception as e:
        print(f"цена недоступна: {e}")
        return
    print(f"BTC={btc} via {src}")

    hist = load_history(HIST_PATH)
    # Порог 0.10 (10 п.п. за 5 минут) не срабатывал НИКОГДА: дельта была
    # всегда нулевой, и система за 500 циклов не выдала ни одного сигнала.
    thr = cfg.get("signal_rules", {}).get("prob_spike_threshold", 0.02)
    # Собственный буфер вероятностей: baseline = медиана последних ~1 часа,
    # а не один 5-минутный срез (иначе шум съедал всё движение).
    prob_hist = update_prob_history(PROB_HIST_PATH, snap)
    dead_events = []
    for label, ev in snap.items():
        prev_mk = baseline_markets(prob_hist, label) or (hist.get(label) or {}).get("markets", [])
        cur_mk = ev.get("markets", [])
        spikes = detect_spikes(prev_mk, cur_mk, thr)
        for m in cur_mk:
            m["_spikes"] = [sp for sp in spikes if sp[0] == m["question"]]
        n_ok = len([m for m in cur_mk if m.get('prices')])
        # Рынок без цен или разрешённый (цены ровно 0/1) сигнала не даёт.
        n_live = len([m for m in cur_mk if m.get('prices') and not m.get('_resolved')])
        if n_live == 0:
            dead_events.append(label)
        print(f"  {label}: {ev.get('title','')[:60]} | рынков={len(cur_mk)} "
              f"с ценами={n_ok} активных={n_live} | спайков={len(spikes)}" +
              (f" | ERR {ev.get('error')}" if ev.get('error') else ""))
    if dead_events:
        print(f"  ВНИМАНИЕ: нет активных рынков у событий {', '.join(dead_events)} — "
              f"слаги устарели/разрешены, обнови polymarket_events в config.json")
    # prev btc
    try:
        with open(PRICE_PATH, encoding="utf-8") as f:
            btc_prev = json.load(f).get("price")
    except Exception:
        btc_prev = None
    heur = heuristic_signal(snap, btc, btc_prev or btc, thr)
    heur["dead_events"] = dead_events
    # Новости: собираем каждый цикл и передаём модели как контекст.
    try:
        news_cfg = cfg.get("news", {})
        news_payload = news_mod.collect(proxies=proxies,
                                        limit=int(news_cfg.get("limit", 25)))
        heur["news_sentiment"] = news_payload.get("sentiment", {})
        heur["news_digest"] = news_mod.digest()
        print(f"  news: {news_payload.get('count', 0)} заголовков, "
              f"тональность {heur['news_sentiment'].get('score', 0)}")
    except Exception as e:
        print(f"news error: {str(e)[:120]}")
        heur.setdefault("news_sentiment", {})
        heur.setdefault("news_digest", "")
    # Пампы: сканер по публичным API с подтверждением второй биржей.
    try:
        pump_data = pumps_mod.scan()
        heur["pumps_digest"] = pumps_mod.digest()
        heur["pumps_count"] = len(pump_data.get("pumps") or [])
        scanned = pump_data.get("scanned") or {}
        print(f"  pumps: {len(scanned)} бирж, подтверждённых {heur['pumps_count']}")
    except Exception as e:
        print(f"pumps error: {str(e)[:120]}")
        heur.setdefault("pumps_digest", "")
        heur.setdefault("pumps_count", 0)
    full = run_cascade(heur, cfg.get("llm", {}), proxies)
    # План сделки по измеренной волатильности: размер и механический выход.
    try:
        from dashboard_data import recent_cycles
        plan = sizing_mod.plan_from_cycles(recent_cycles(limit=14))
        full["trade_plan"] = plan
        print(f"PLAN: size x{plan['size_mult']} vol={plan['vol_pct']}% "
              f"regime={plan['regime']} SL={plan['stop_loss_pct']}% "
              f"TP={plan['take_profit_pct']}%")
    except Exception as e:
        print(f"plan error: {str(e)[:120]}")
    if full.get("llm_l1"):
        print("LLM L1:", full["llm_l1"].get("signal"),
              full["llm_l1"].get("confidence"))
    if full.get("llm_l2"):
        print("LLM L2:", full["llm_l2"].get("signal"),
              full["llm_l2"].get("confidence"))
    if full.get("llm_note"):
        print(full["llm_note"])
    if full.get("final_signal"):
        print(f"FINAL: {full['final_signal']} conf={full['final_confidence']}")
    paper_execute(full, LOG_PATH)
    # forward LONG/SHORT to Hummingbot bridge
    try:
        hb = cfg.get("execution", {}).get("hummingbot", {})
        if full.get("final_signal") in (hb.get("send_on") or ["LONG", "SHORT"]):
            from executor import send_to_hummingbot
            res = send_to_hummingbot(
                {"final": full["final_signal"], "conf": full.get("final_confidence"),
                 "btc": btc, "verdict": full.get("verdict"),
                 "size_mult": (full.get("trade_plan") or {}).get("size_mult", 1.0),
                 "stop_loss_pct": (full.get("trade_plan") or {}).get("stop_loss_pct"),
                 "take_profit_pct": (full.get("trade_plan") or {}).get("take_profit_pct"),
                 "l1": full.get("llm_l1"), "l2": full.get("llm_l2")},
                hb.get("webhook_url", ""), proxies=proxies)
            print("bridge:", res)
    except Exception as e:
        print("bridge error:", str(e)[:150])
    save_history(HIST_PATH, {k: {"title": v.get("title"), "markets":
                [{kk: vv for kk, vv in m.items() if kk != "_spikes"}
                 for v in [v] for m in v.get("markets", [])]}
                for k, v in snap.items()})
    with open(PRICE_PATH, "w", encoding="utf-8") as f:
        json.dump({"price": btc, "src": src}, f)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    ap.add_argument("--loop", action="store_true", help="бесконечный цикл")
    a = ap.parse_args()
    cfg = load_cfg()
    interval = cfg.get("poll_interval_sec", 300)
    if a.loop and not a.once:
        print(f"цикл каждые {interval}с. Ctrl+C для остановки.")
        while True:
            try:
                one_cycle(cfg)
            except KeyboardInterrupt:
                break
            except Exception:
                traceback.print_exc()
            time.sleep(interval)
    else:
        one_cycle(cfg)

if __name__ == "__main__":
    main()
