"""Orchestrator: sensor -> price -> analyst -> llm cascade -> executor."""
import json, os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sensor import poll_all, fetch_book_stats
from price import get_btc_price
from analyst import (load_history, save_history, detect_spikes,
                      update_prob_history, baseline_markets,
                      heuristic_signal, update_btc_history, poly_logit_z,
                      btc_zscore)
from proxyutil import build_proxies, redact
from llm import run_cascade
from executor import paper_execute
import news as news_mod
import pumps as pumps_mod
import sizing as sizing_mod
import regime as regime_mod

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(BASE, "config", "config.json")
HIST_PATH = os.path.join(BASE, "data", "history.json")
PROB_HIST_PATH = os.path.join(BASE, "data", "prob_history.json")
PRICE_PATH = os.path.join(BASE, "data", "last_price.json")
BTC_HIST_PATH = os.path.join(BASE, "data", "btc_history.json")
OI_PATH = os.path.join(BASE, "data", "oi_history.json")
LOG_PATH = os.path.join(BASE, "logs", "signals.log")
JOURNAL_PATH = "D:/Hummingbot/data/trades.json"

def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_journal():
    """pnl_pct из журнала закрытых сделок Hummingbot (для Fractional Kelly)."""
    out = []
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            d = json.load(f)
        for t in (d.get("trades") or []):
            try:
                out.append(float(t.get("pnl_pct")))
            except Exception:
                continue
    except Exception:
        return []
    return out


def oi_liquidation_risk(proxies=None, keep=300):
    """Открытый интерес OKX BTC-SWAP: z(дельта 1ч) — прокси ликвидационного
    каскада (бриф): |z|>=3 ИЛИ падение OI > 10%/час -> де-леверидж."""
    import requests
    r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                     params={"instId": "BTC-USDT-SWAP"}, timeout=10, proxies=proxies)
    r.raise_for_status()
    oi = float(r.json()["data"][0].get("oiCcy") or 0.0)
    if oi <= 0:
        return False, 0.0
    hist = {"ts": [], "oi": []}
    try:
        with open(OI_PATH, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        pass
    ts = int(time.time())
    if not hist.get("ts") or ts - hist["ts"][-1] >= 60:
        hist.setdefault("ts", []).append(ts)
        hist.setdefault("oi", []).append(oi)
        hist["ts"] = hist["ts"][-keep:]
        hist["oi"] = hist["oi"][-keep:]
        os.makedirs(os.path.dirname(OI_PATH), exist_ok=True)
        with open(OI_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f)
    ois = hist["oi"]
    if len(ois) < 25 or ois[-13] <= 0:
        return False, 0.0
    d1h = (ois[-1] - ois[-13]) / ois[-13] * 100.0
    past = []
    for i in range(13, len(ois)):
        if ois[i - 13] > 0:
            past.append((ois[i] - ois[i - 13]) / ois[i - 13] * 100.0)
    if len(past) < 10:
        return (d1h <= -10.0), round(d1h, 2)
    m = sum(past) / len(past)
    sd = max((sum((x - m) ** 2 for x in past) / len(past)) ** 0.5, 0.05)
    z = (d1h - m) / sd
    return (abs(z) >= 3.0 or d1h <= -10.0), round(z, 2)

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
    # Стакан CLOB только для рынков со спайком: анти-сквиз (тонкий рынок = шум).
    for label, ev in snap.items():
        if ev.get("error"):
            continue
        for m in ev.get("markets", []):
            if m.get("_spikes") and m.get("clob_token"):
                book = fetch_book_stats(m["clob_token"], proxies=proxies)
                if book:
                    m["_book"] = book
                    if book.get("thin"):
                        print(f"    thin {label}: spread={book.get('spread_pts')} "
                              f"depth=${book.get('depth_usd')}")
    # Двухфакторное согласие: z логит-ROC вероятности и z движения BTC.
    btc_series = update_btc_history(BTC_HIST_PATH, btc)
    zinfo = {"z_poly": poly_logit_z(prob_hist),
             "z_btc": btc_zscore(btc_series).get("z", 0.0)}
    heur = heuristic_signal(snap, btc, btc_prev or btc, thr, zinfo=zinfo)
    heur["dead_events"] = dead_events
    # Рыночный режим (2-of-3 ADX/ER/CHOP) — фильтр, не свитчер.
    try:
        reg = regime_mod.classify(proxies=proxies)
        heur["market_regime"] = reg
        print(f"  regime: {reg['regime']} vol={reg['vol']} votes={reg['votes']} "
              f"ADX={reg['adx']} ER={reg['er']} CHOP={reg['chop']} "
              f"atr%ile={reg['atr_pctile']}")
    except Exception as e:
        heur["market_regime"] = {"regime": "unknown", "vol": "unknown",
                                 "note": str(e)[:120]}
    # Ликвидационный прокси (дельта OI): анти-чейз во время де-левериджа.
    try:
        liq, oi_z = oi_liquidation_risk(proxies)
        heur["liquidation_risk"] = bool(liq)
        heur["oi_z"] = oi_z
        if liq:
            print(f"  OI risk: z={oi_z} — де-леверидж-прокси, режем размер")
    except Exception as e:
        heur["liquidation_risk"] = False
        heur["oi_z"] = None
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
    # Ставка финансирования + базис: фид для стратегии funding_carry.
    try:
        import funding as funding_mod
        f = funding_mod.write_feed(proxies=proxies)
        print(f"  funding: rate={f['rate_bps']}b/8ч basis={f['basis_bps']}b "
              f"(spot={f['spot']} perp={f['perp']})")
    except Exception as e:
        print(f"funding feed error: {str(e)[:120]}")
    full = run_cascade(heur, cfg.get("llm", {}), proxies)
    # План сделки: вола x quarter-Kelly(журнал) x риск-гейты (режим/OI/сеттл).
    try:
        from dashboard_data import recent_cycles
        plan = sizing_mod.plan_from_cycles(
            recent_cycles(limit=14),
            journal_pnls=_load_journal(),
            regime=heur.get("market_regime"),
            liquidation_risk=bool(heur.get("liquidation_risk")))
        full["trade_plan"] = plan
        print(f"PLAN: size x{plan['size_mult']} vol={plan['vol_pct']}% "
              f"regime={plan['regime']} SL={plan['stop_loss_pct']}% "
              f"TP={plan['take_profit_pct']}% RR={plan['rr']} "
              f"kelly={plan['kelly_scale']}" +
              (f" notes={','.join(plan['risk_notes'])}" if plan.get("risk_notes") else ""))
    except Exception as e:
        print(f"plan error: {str(e)[:120]}")
    # Фильтр режима: в флэте направленные сигналы — статистически шум
    # (walk-forward: фильтр +, свитчер -392 bps).
    reg = full.get("market_regime") or {}
    if full.get("final_signal") in ("LONG", "SHORT") and reg.get("regime") == "range":
        print(f"REGIME FILTER: {full['final_signal']} conf={full.get('final_confidence')} "
              f"-> WAIT (range, votes={reg.get('votes')})")
        full["final_signal"] = "WAIT"
        full["final_confidence"] = min(float(full.get("final_confidence") or 0), 0.5)
        full["regime_filter"] = "range"
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
