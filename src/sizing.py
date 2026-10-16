# -*- coding: utf-8 -*-
"""Волатильностно-адаптивный размер позиции и механический выход.

Зачем: по данным arXiv 2609.05663 (DXAP, 7.5M вызовов) медианное плечо было
5.0x во ВСЕХ секстилях волатильности (Spearman -0.001), из-за чего доля
ликвидаций росла с 0.7% до 4.3%. Там же механические выходы 2%/4% дали
+39.0 bps на позицию, а все 16 «обученных» exit-политик были убыточны.

Идея: размер режется не «по мнению модели», а по измеренной волатильности,
и выход задаётся числами, а не рассуждением.
"""
import math
from statistics import pstdev
from typing import Any, Dict, List

# Волатильность-эталон: среднее |изменение| BTC за цикл в спокойном рынке, %.
REF_VOL_PCT = 0.15
# Границы множителя: не раздуваем и не обнуляем.
MIN_MULT = 0.2
MAX_MULT = 1.0
# Механический выход по данным исследования.
STOP_LOSS_PCT = 2.0
TAKE_PROFIT_PCT = 4.0
# Дневной лимит убытка в доллетах — последний рубеж перед kill switch.
DAILY_LOSS_LIMIT_USD = float(20.0)
MAX_DAILY_LOSSES = 6


def realized_vol(cycles: List[Dict[str, Any]], lookback: int = 12) -> float:
    """Среднее абсолютное изменение BTC в процентах по последним циклам."""
    prices = [float(c["btc"]) for c in cycles[-lookback:] if c.get("btc")]
    if len(prices) < 3:
        return 0.0
    moves = [(prices[i] - prices[i - 1]) / prices[i - 1] * 100.0
             for i in range(1, len(prices))]
    moves = [m for m in moves if math.isfinite(m)]
    return sum(abs(m) for m in moves) / len(moves) if moves else 0.0


def size_multiplier(vol_pct: float, base: float = 1.0) -> float:
    """Размер падает по закону 1/sqrt(vol) относительно эталонной волатильности.

    vol == REF  -> 1.0 (спокойный рынок, размер не трогаем)
    vol = 4x REF -> 0.5 (шторм, вдвое меньше)
    """
    if vol_pct <= 0:
        return MAX_MULT
    mult = base * math.sqrt(REF_VOL_PCT / max(vol_pct, 1e-6))
    return round(max(MIN_MULT, min(MAX_MULT, mult)), 3)


def plan_from_cycles(cycles: List[Dict[str, Any]], base_mult: float = 1.0) -> Dict[str, Any]:
    """План сделки на текущий цикл: размер + механический выход."""
    vol = realized_vol(cycles)
    mult = size_multiplier(vol, base_mult)
    # Чем шторм, тем ближе стоп: тот же риск в волатильных единицах.
    vol_ratio = vol / REF_VOL_PCT if REF_VOL_PCT else 1.0
    stop = round(max(1.0, min(STOP_LOSS_PCT * (0.6 + 0.4 * vol_ratio), 6.0)), 2)
    # Цель не растягиваем: в исследовании 43% позиций доходили до +300 bps
    # и почти все закрывались в минус — дальний TP просто не добирается.
    take = round(max(2.0, min(TAKE_PROFIT_PCT * (0.8 + 0.2 * vol_ratio), 5.0)), 2)
    return {
        "size_mult": mult,
        "vol_pct": round(vol, 4),
        "vol_ratio": round(vol_ratio, 2),
        "regime": "volatile" if vol_ratio >= 3 else ("calm" if vol_ratio <= 0.7 else "normal"),
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "daily_loss_limit_usd": DAILY_LOSS_LIMIT_USD,
        "max_daily_losses": MAX_DAILY_LOSSES,
    }


if __name__ == "__main__":
    flat = [{"btc": 82000 + (i % 3) * 5} for i in range(14)]
    storm = [{"btc": 82000 * (1 + (0.02 if i % 2 else -0.02))} for i in range(14)]
    print("calm  :", plan_from_cycles(flat))
    print("storm :", plan_from_cycles(storm))