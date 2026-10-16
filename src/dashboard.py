"""Дашборд: текущие вероятности Polymarket + цена BTC. Запуск: python src/dashboard.py"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyst import load_history
from price import get_btc_price

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def bar(p: float, w: int = 30) -> str:
    f = int(max(0, min(1, p)) * w)
    return "#" * f + "-" * (w - f)

def main():
    hist = load_history(os.path.join(BASE, "data", "history.json"))
    try:
        btc, src = get_btc_price()
    except Exception as e:
        btc, src = 0, f"ERR {e}"
    print(f"\n=== Polymarket Oracle dashboard ===  BTC={btc} ({src})\n")
    if not hist:
        print("Нет данных. Запусти: python src/main.py --once")
        return
    for label, ev in hist.items():
        print(f"--- {label}: {ev.get('title','')[:80]}")
        for m in ev.get("markets", []):
            if m.get("_resolved"):
                continue
            p = float(m.get("yes_prob", 0))
            q = m.get("question", "")[:65]
            print(f"  {p*100:5.1f}% [{bar(p)}] {q}")
        print()

if __name__ == "__main__":
    main()
