import sys

sys.path.insert(0, "src")
from sizing import plan_from_cycles  # noqa: E402

flat = [{"btc": 82000 + (i % 3) * 5} for i in range(14)]
storm = [{"btc": 82000 * (1 + (0.02 if i % 2 else -0.02))} for i in range(14)]
tiny = [{"btc": 82000 + (i % 2) * 0.2} for i in range(14)]
short = [{"btc": 82000} for i in range(2)]

for name, cyc in (("calm", flat), ("storm", storm), ("tiny", tiny), ("short", short)):
    p = plan_from_cycles(cyc)
    print(f"{name:6} vol={p['vol_pct']:.3f}% ratio={p['vol_ratio']:>6} "
          f"mult={p['size_mult']:<5} SL={p['stop_loss_pct']}% "
          f"TP={p['take_profit_pct']}% regime={p['regime']}")