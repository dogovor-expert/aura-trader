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
# Минимум соотношения риск/прибыль (арифметика expectancy: при W=40% и R=2
# ожидание +0.2R даже до комиссий; ниже 1.5:1 при 40% — ноль до издержек).
MIN_RR = 2.0
# Нейтральный quarter-Kelly (f*=0.25 -> 0.0625 банка) — точка масштаба=1.0.
KELLY_REF = 0.0625
MIN_JOURNAL_N = 30  # меньше — статистика журнала шум, не угадываем


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


def kelly_scale(journal_pnls: List[float]) -> float:
    """Fractional (quarter) Kelly из журнала сделок -> множитель размера.

    f* = (p(b+1)-1)/b, p с shrinkage (p_hat - SE) — переоценка p на +0.05
    раздувает full-Kelly на 23%, поэтому доля 0.25 (Thorp: кванты используют
    0.25-0.5 f*). Масштаб: f_q / KELLY_REF (нейтраль = 1.0), clamp 0.5..1.5.
    < MIN_JOURNAL_N сделок -> 1.0 (нет статистики — размер не трогаем).
    """
    j = [float(x) for x in (journal_pnls or []) if math.isfinite(float(x))]
    if len(j) < MIN_JOURNAL_N:
        return 1.0
    wins = [x for x in j if x > 0]
    losses = [x for x in j if x <= 0]
    if not wins or not losses:
        return 1.0
    n = len(j)
    p_hat = len(wins) / n
    se = math.sqrt(p_hat * (1.0 - p_hat) / n)
    p = max(0.01, min(0.99, p_hat - se))
    avg_w = sum(wins) / len(wins)
    avg_l = abs(sum(losses) / len(losses))
    if avg_l <= 0:
        return 1.0
    b = avg_w / avg_l
    f = (p * (b + 1.0) - 1.0) / b
    f_q = 0.25 * max(f, 0.0)
    return round(max(0.5, min(1.5, f_q / KELLY_REF)), 2)


def in_settle_window(now=None) -> bool:
    """Окно ±15 мин от расчёта финансирования OKX (00/08/16 UTC) — хедж-флоу
    маркет-мейкеров вокруг сеттла повышает волатильность и спред."""
    import datetime
    now = now or datetime.datetime.utcnow()
    minutes = now.hour * 60 + now.minute
    dist = min(min(abs(minutes - h * 60), 1440 - abs(minutes - h * 60))
               for h in (0, 8, 16))
    return dist <= 15


def plan_from_cycles(cycles: List[Dict[str, Any]], base_mult: float = 1.0,
                     journal_pnls: List[float] = None,
                     regime: Dict[str, Any] = None,
                     liquidation_risk: bool = False) -> Dict[str, Any]:
    """План сделки: размер (вола x Kelly x риск-гейты) + механический выход."""
    vol = realized_vol(cycles)
    mult = size_multiplier(vol, base_mult)
    # Чем шторм, тем ближе стоп: тот же риск в волатильных единицах.
    vol_ratio = vol / REF_VOL_PCT if REF_VOL_PCT else 1.0
    stop = round(max(1.0, min(STOP_LOSS_PCT * (0.6 + 0.4 * vol_ratio), 6.0)), 2)
    # Цель не растягиваем: в исследовании 43% позиций доходили до +300 bps
    # и почти все закрывались в минус — дальний TP просто не добирается.
    take = round(max(2.0, min(TAKE_PROFIT_PCT * (0.8 + 0.2 * vol_ratio), 5.0)), 2)
    # R:R >= MIN_RR принудительно: ужимаем стоп (не раздуваем тейк выше капа).
    if take < MIN_RR * stop:
        stop = round(max(1.0, take / MIN_RR), 2)
    risk_notes = []
    # Fractional Kelly по журналу (включается при >= MIN_JOURNAL_N сделок).
    kelly = kelly_scale(journal_pnls)
    if kelly != 1.0:
        risk_notes.append(f"kelly x{kelly}")
    mult = max(MIN_MULT, min(MAX_MULT, mult * kelly))
    # Режимный фильтр: высокая волатильность (ATR/BB P80+) — режем размер.
    vol_cls = (regime or {}).get("vol") if regime else None
    if vol_cls == "high":
        mult = max(MIN_MULT, mult * 0.75)
        risk_notes.append("vol P80+")
    # Ликвидационный каскад (z(DeltaOI)>=3): анти-чейз — размер вниз.
    if liquidation_risk:
        mult = max(MIN_MULT, mult * 0.6)
        risk_notes.append("liquidation_risk")
    # Окно сеттла финансирования: размер вниз (хедж-флоу, расширенный спред).
    settle = in_settle_window()
    if settle:
        mult = max(MIN_MULT, mult * 0.5)
        risk_notes.append("settle_window")
    mult = round(mult, 3)
    return {
        "size_mult": mult,
        "vol_pct": round(vol, 4),
        "vol_ratio": round(vol_ratio, 2),
        "regime": "volatile" if vol_ratio >= 3 else ("calm" if vol_ratio <= 0.7 else "normal"),
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "rr": round(take / stop, 2),
        "kelly_scale": kelly,
        "settle_window": settle,
        "risk_notes": risk_notes,
        "daily_loss_limit_usd": DAILY_LOSS_LIMIT_USD,
        "max_daily_losses": MAX_DAILY_LOSSES,
    }


if __name__ == "__main__":
    flat = [{"btc": 82000 + (i % 3) * 5} for i in range(14)]
    storm = [{"btc": 82000 * (1 + (0.02 if i % 2 else -0.02))} for i in range(14)]
    print("calm  :", plan_from_cycles(flat))
    print("storm :", plan_from_cycles(storm))
    print("journal<30 -> kelly:", kelly_scale([1, -1, 5, -1, 0.5]))
    print("journal>=30 -> kelly:", kelly_scale([1.0] * 18 + [-0.5] * 12))
    print("settle_window:", in_settle_window())