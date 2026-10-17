# -*- coding: utf-8 -*-
import json, math, os, sys, tempfile, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import sizing
import analyst
import regime as rg
import llm

# --- sizing: R:R инвариант на любом входе (шторм/покой/журнал/флаги) ---
for label, cycles in (
    ("flat", [{"btc": 82000 + (i % 3) * 5} for i in range(14)]),
    ("storm", [{"btc": 82000 * (1 + (0.02 if i % 2 else -0.02))} for i in range(14)]),
    ("mild", [{"btc": 80000 * (1 + 0.004 * i)} for i in range(14)]),
):
    for reg in (None, {"vol": "high"}, {"vol": "low"}):
        for liq in (False, True):
            p = sizing.plan_from_cycles(cycles, regime=reg, liquidation_risk=liq)
            assert p["rr"] >= 2.0 - 1e-9, (label, reg, liq, p)
            assert 0.2 <= p["size_mult"] <= 1.0, (label, p)
print("sizing R:R>=2 + mult bounds: OK")

# kelly fallback и активация
assert sizing.kelly_scale([1, -1, 5]) == 1.0
many = [1.2] * 20 + [-0.6] * 10
k = sizing.kelly_scale(many)
assert 0.5 <= k <= 1.5, k
# сильный журнал (p_hat high) -> масштаб выше нейтрального
strong = [1.5] * 24 + [-0.3] * 6
assert sizing.kelly_scale(strong) >= k, (strong, k)
print("kelly: OK (weak=%s strong=%s)" % (k, sizing.kelly_scale(strong)))

assert isinstance(sizing.in_settle_window(), bool)
print("settle window: OK")

# --- analyst: z логит-ROC + btc z ---
hist = {"L": {"q": [0.50, 0.50, 0.50, 0.50, 0.50, 0.60],
              "flat": [0.30, 0.30, 0.30, 0.30, 0.30, 0.30],
              "flat2": [0.70, 0.70, 0.70, 0.70, 0.70, 0.70],
              "flat3": [0.40, 0.40, 0.40, 0.40, 0.40, 0.40]}}
zp = analyst.poly_logit_z(hist, k=3)
assert "q" in zp and math.isfinite(zp["q"]) and abs(zp["q"]) > 1.0, zp
btc = [100.0 * (1.0001 ** i) for i in range(49)]
btc.append(btc[-1] * 1.01)  # резкий всплеск против фона
bz = analyst.btc_zscore(btc)
assert bz["z"] > 1.0, bz  # скачок в 1% на фоне 0.01% шума -> высокий z
# фон без всплесков -> z около 0 (нет сюрприза)
bz2 = analyst.btc_zscore([100.0 * (1.0001 ** i) for i in range(50)])
assert abs(bz2["z"]) < 2.0, bz2
print("z_poly/z_btc: OK", zp, bz)

# --- heuristic: вето тонкого рынка и противоположного BTC, двухфактор ---
snap = {"E": {"error": None, "markets": [
    {"question": "A", "yes_prob": 0.6, "_spikes": [("A", 0.5, 0.6, 0.1)],
     "_book": {"thin": True, "spread_pts": 0.05, "depth_usd": 500}},
    {"question": "B", "yes_prob": 0.4, "_spikes": [("B", 0.5, 0.4, -0.1)]},
]}}
zinfo = {"z_poly": {"A": 3.0, "B": -3.0}, "z_btc": 2.5}  # B: prob вниз, BTC вверх -> contra
h = analyst.heuristic_signal(snap, 82000, 81000, 0.02, zinfo=zinfo)
sA = next(s for s in h["signals"] if s["market"] == "A")
sB = next(s for s in h["signals"] if s["market"] == "B")
assert sA["veto"] == "thin_market" and sA["action"] == "WATCH", sA
assert sB["veto"] == "btc_contradicts_prob" and sB["action"] == "WATCH", sB
assert h["verdict"] == "NEUTRAL"
# подтверждение (двухфактор): без thin-книги, зоны в знак дельты
snap_ok = {"E": {"error": None, "markets": [
    {"question": "A", "yes_prob": 0.6, "_spikes": [("A", 0.5, 0.6, 0.1)]},
    {"question": "B", "yes_prob": 0.4, "_spikes": [("B", 0.5, 0.4, -0.1)]},
]}}
h2 = analyst.heuristic_signal(snap_ok, 82000, 81000, 0.02,
                              zinfo={"z_poly": {"A": 3.0, "B": -3.0}, "z_btc": 2.5})
sA2 = next(s for s in h2["signals"] if s["market"] == "A")
assert sA2["two_factor"] is True and sA2["action"] == "WATCH_LONG", sA2
print("heuristic vetos/two_factor: OK")

# --- llm: дневной кап ---
tmp_cap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm_cap.json")
backup = None
if os.path.exists(tmp_cap):
    backup = open(tmp_cap, encoding="utf-8").read()
try:
    today = datetime.datetime.utcnow().date().isoformat()
    json.dump({"date": today, "count": 5}, open(tmp_cap, "w"))
    cfg = {"max_directional_per_day": 6}
    assert llm._daily_directional_gate(cfg, consume=True) is True   # 5->6
    assert llm._daily_directional_gate(cfg, consume=True) is False  # cap
    assert json.load(open(tmp_cap))["count"] == 6
    json.dump({"date": today, "count": 0}, open(tmp_cap, "w"))
    assert llm._daily_directional_gate({"max_directional_per_day": 0}, consume=True) is True
finally:
    if backup is None:
        if os.path.exists(tmp_cap):
            os.remove(tmp_cap)
    else:
        open(tmp_cap, "w").write(backup)
print("daily cap: OK")

# --- regime: синтетический тренд должен дать votes>=2 и regime=trend ---
cand = []
px = 100.0
for i in range(120):
    o = px; px += 0.5
    cand.append({"o": o, "h": px + 0.1, "l": o - 0.1, "c": px})
st = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "regime_state.json")
st_backup = None
if os.path.exists(st):
    st_backup = open(st, encoding="utf-8").read()
try:
    r = rg.classify(candles=cand)
    assert r["regime"] == "trend", r
    assert r["votes"] >= 2, r
finally:
    if st_backup is None and os.path.exists(st):
        os.remove(st)
    elif st_backup:
        open(st, "w").write(st_backup)
# флэт: без тренда -> range (state сбрасываем выше)
cand2 = []
px = 100.0
for i in range(120):
    px = 100.0 + (1.0 if i % 2 else -1.0)
    o = px
    cand2.append({"o": o, "h": px + 0.2, "l": px - 0.2, "c": px})
if os.path.exists(st):
    st_backup = open(st, encoding="utf-8").read()
try:
    r2 = rg.classify(candles=cand2)
    assert r2["regime"] == "range", r2
finally:
    if st_backup:
        open(st, "w").write(st_backup)
    elif os.path.exists(st):
        os.remove(st)
print("regime: OK")
print("ALL UNIT OK")
