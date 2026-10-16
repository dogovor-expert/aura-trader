# -*- coding: utf-8 -*-
"""Защиты межбиржевого арбитража — чистая логика без Hummingbot.

Каждая функция закрывает конкретную ловушку:

  vwap_for_side()          иллюзия ликвидности: цена не из Top of Book, а
                           средняя по реальному объёму до нужного размера
  evaluate_route()         комиссионный порог: спред считается после двойной
                           комиссии тейкера и проскальзывания
  SpreadConfirmation()     спуфинг: спред должен продержаться N опросов
  DepthGate()              ликвидность: суммарная глубина под сделку
  RateLimiter()            асимметрия лимитов API: свой бюджет на биржу и
                           backoff после 429
  RouteRegistry()          «закрытый кошелёк»: ввод/вывод монеты выключен ->
                           маршрут запрещён, а не «ищем спред»
  NetworkGuard()           несовпадение сетей: ребалансировка между сетями
                           невозможна -> лимит накопительного перекоса
  RebalanceEconomics()     кассовый разрыв: сбор спреда против комиссии вывода
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

BPS = 10_000


def bps_to_pct(bps: float) -> float:
    return bps / 100.0


def pct_to_bps(pct: float) -> float:
    return pct * 100.0


# --------------------------------------------------------------------------
# 1. Иллюзия ликвидности: считаем VWAP по стакану, а не по лучшей цене
# --------------------------------------------------------------------------

@dataclass
class VwapResult:
    ok: bool
    price: float = 0.0
    filled: float = 0.0            # сколько монет реально нашлось
    levels_used: int = 0
    slippage_bps: float = 0.0      # отклонение от Top of Book
    reason: str = ""


def vwap_for_side(levels: Sequence[Tuple[float, float]], side: str, size: float) -> VwapResult:
    """Средняя цена исполнения на нужную величину.

    levels — [[price, qty], ...] как отдаёт биржа; для покупки берём возрастание
    цены (asks), для продажи — убывание (bids). Стакан всегда должен быть уже
    отсортирован биржей: мы не пересортировываем, чтобы не спорить с форматом.
    """
    if not levels or size <= 0:
        return VwapResult(False, reason="empty book or zero size")

    rows: List[Tuple[float, float]] = []
    for item in levels:
        try:
            price = float(item[0])
            qty = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty > 0:
            rows.append((price, qty))
    if not rows:
        return VwapResult(False, reason="book has no usable levels")

    best = rows[0][0]
    remaining = size
    spent = 0.0
    got = 0.0
    used = 0
    for price, qty in rows:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        spent += take * price
        got += take
        remaining -= take
        used += 1

    if remaining > 1e-12:
        return VwapResult(False, filled=got, levels_used=used,
                          reason=f"not enough depth: short {remaining:.8g}")

    avg = spent / got if got else 0.0
    slip = abs(avg - best) / best * BPS if best else 0.0
    return VwapResult(True, price=avg, filled=got, levels_used=used, slippage_bps=slip)


# --------------------------------------------------------------------------
# 2. Комиссионный порог и проскальзывание
# --------------------------------------------------------------------------

@dataclass
class RouteEval:
    ok: bool
    edge_bps: float = 0.0
    net_bps: float = 0.0
    buy_vwap: float = 0.0
    sell_vwap: float = 0.0
    slippage_bps: float = 0.0
    fees_bps: float = 0.0
    reason: str = ""


def evaluate_route(buy: VwapResult, sell: VwapResult,
                   buy_fee_bps: float, sell_fee_bps: float,
                   min_net_bps: float, max_slippage_bps: float) -> RouteEval:
    """Спред после всех издержек. min_net_bps — порог безубыточности."""
    if not buy.ok or not sell.ok:
        return RouteEval(False, reason=f"no executable side: {buy.reason or sell.reason}")

    if sell.price <= buy.price:
        return RouteEval(False, buy_vwap=buy.price, sell_vwap=sell.price,
                         reason="sell price not above buy price")

    gross_bps = (sell.price - buy.price) / buy.price * BPS
    slip = max(buy.slippage_bps, sell.slippage_bps)
    if slip > max_slippage_bps:
        return RouteEval(False, edge_bps=gross_bps, slippage_bps=slip,
                         buy_vwap=buy.price, sell_vwap=sell.price,
                         reason=f"slippage {slip:.0f}bps over limit {max_slippage_bps:.0f}")

    fees = buy_fee_bps + sell_fee_bps
    net = gross_bps - fees - slip
    if net < min_net_bps:
        return RouteEval(False, edge_bps=gross_bps, net_bps=net, slippage_bps=slip,
                         fees_bps=fees, buy_vwap=buy.price, sell_vwap=sell.price,
                         reason=f"net {net:.0f}bps below threshold {min_net_bps:.0f}bps")
    return RouteEval(True, edge_bps=gross_bps, net_bps=net, slippage_bps=slip,
                     fees_bps=fees, buy_vwap=buy.price, sell_vwap=sell.price)


# --------------------------------------------------------------------------
# 3. Спуфинг: спред должен подтвердиться несколькими опросами
# --------------------------------------------------------------------------

class SpreadConfirmation:
    """Спред считается настоящим, только если держится N опросов подряд."""

    def __init__(self, required: int = 3):
        self.required = max(1, int(required))
        self._streak: Dict[str, int] = {}
        self._last: Dict[str, float] = {}

    def observe(self, route_id: str, net_bps: float) -> int:
        self._streak[route_id] = self._streak.get(route_id, 0) + 1
        self._last[route_id] = net_bps
        return self._streak[route_id]

    def reset(self, route_id: str) -> None:
        self._streak[route_id] = 0

    def confirmed(self, route_id: str, net_bps: float, tolerance_bps: float = 15.0) -> bool:
        """Подтверждён, если стрик достигнут и спред не «прыгает» между опросами."""
        if self._streak.get(route_id, 0) < self.required:
            return False
        prev = self._last.get(route_id)
        if prev is None:
            return False
        return abs(prev - net_bps) <= tolerance_bps

    def streak(self, route_id: str) -> int:
        return self._streak.get(route_id, 0)


# --------------------------------------------------------------------------
# 4. Глубина стакана
# --------------------------------------------------------------------------

@dataclass
class DepthGate:
    min_depth_usd: float = 200.0
    max_top_share: float = 0.6      # нельзя съедать больше 60% одного уровня


def check_depth(gate: DepthGate, side: str, levels: Sequence[Tuple[float, float]],
                size: float) -> Tuple[bool, str, float]:
    """Хватает ли глубины и не втыкаемся ли мы в одну тонкую стену."""
    rows = [(float(p), float(q)) for p, q in levels if float(p) > 0 and float(q) > 0]
    if not rows:
        return False, "empty book", 0.0
    total_quote = sum(p * q for p, q in rows)
    if total_quote < gate.min_depth_usd:
        return False, f"depth ${total_quote:.0f} below ${gate.min_depth_usd:.0f}", total_quote
    top_quote = rows[0][0] * rows[0][1]
    if top_quote > 0 and size * rows[0][0] > gate.max_top_share * top_quote:
        return False, f"order eats {gate.max_top_share:.0%}+ of top level", total_quote
    return True, "", total_quote


# --------------------------------------------------------------------------
# 5. Асимметрия лимитов API
# --------------------------------------------------------------------------

class RateLimiter:
    """Свой бюджет запросов на биржу + backoff после 429."""

    def __init__(self, per_minute: Dict[str, int], default_per_minute: int = 60):
        self.limits = dict(per_minute)
        self.default = default_per_minute
        self._calls: Dict[str, List[float]] = {}
        self._blocked_until: Dict[str, float] = {}

    def allow(self, exchange: str, now: float) -> bool:
        if now < self._blocked_until.get(exchange, 0.0):
            return False
        limit = self.limits.get(exchange, self.default)
        bucket = self._calls.setdefault(exchange, [])
        cutoff = now - 60.0
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def penalize(self, exchange: str, now: float, base_seconds: float = 30.0) -> float:
        """Экспоненциальный запрет: 30с, 60с, 120с…"""
        prev = self._blocked_until.get(exchange, 0.0)
        level = 1 if prev <= now else 2
        until = now + base_seconds * level
        self._blocked_until[exchange] = until
        return until - now

    def blocked_for(self, exchange: str, now: float) -> float:
        return max(0.0, self._blocked_until.get(exchange, 0.0) - now)

    def used(self, exchange: str, now: float) -> int:
        cutoff = now - 60.0
        return len([t for t in self._calls.get(exchange, []) if t >= cutoff])


# --------------------------------------------------------------------------
# 6. «Закрытый кошелёк»: ввод/вывод выключен
# --------------------------------------------------------------------------

@dataclass
class RoutePolicy:
    route_id: str
    buy_exchange: str
    sell_exchange: str
    pair: str
    buy_deposit_enabled: bool = True
    sell_withdrawal_enabled: bool = True
    asset_network: Tuple[str, str] = ("", "")   # сеть актива на каждой бирже
    rebalance_allowed: bool = True


class RouteRegistry:
    def __init__(self, routes: Optional[List[RoutePolicy]] = None):
        self.routes: Dict[str, RoutePolicy] = {}
        for r in routes or []:
            self.routes[r.route_id] = r

    def add(self, route: RoutePolicy) -> None:
        self.routes[route.route_id] = route

    def tradable(self, route_id: str) -> Tuple[bool, str]:
        r = self.routes.get(route_id)
        if not r:
            return False, "unknown route"
        if not r.buy_deposit_enabled:
            return False, f"deposit disabled on {r.buy_exchange}"
        if not r.sell_withdrawal_enabled:
            return False, f"withdrawal disabled on {r.sell_exchange}"
        if not r.rebalance_allowed:
            return False, "route marked no-rebalance"
        return True, ""

    def snapshot(self) -> List[dict]:
        out = []
        for r in self.routes.values():
            ok, why = self.tradable(r.route_id)
            nets = r.asset_network
            item = asdict(r)
            item["asset_network"] = list(nets)
            item["tradable"] = ok
            item["blocked_reason"] = why
            item["networks_match"] = bool(nets[0]) and nets[0] == nets[1]
            out.append(item)
        return out


# --------------------------------------------------------------------------
# 7. Несовпадение сетей: ребалансировка невозможна
# --------------------------------------------------------------------------

@dataclass
class RebalanceEconomics:
    """Следит за перекосом баланса и запрещает торговлю, когда перекос не вывезти."""

    max_skew_usd: float = 10.0
    withdraw_fee_usd: float = 25.0
    _buy_side_skew: Dict[str, float] = field(default_factory=dict)

    def update(self, route_id: str, buy_side_quote: float, sell_side_quote: float) -> float:
        self._buy_side_skew[route_id] = buy_side_quote
        return buy_side_quote - sell_side_quote

    def skew(self, route_id: str) -> float:
        return self._buy_side_skew.get(route_id, 0.0)

    def rebalance_possible(self, route: RoutePolicy) -> Tuple[bool, str]:
        if not route.rebalance_allowed:
            return False, "rebalance disabled for route"
        a, b = route.asset_network
        if a and b and a != b:
            return False, f"network mismatch {a} vs {b}"
        return True, ""

    def economics(self, route_id: str, sell_side_quote: float,
                  buy_side_quote: float, earned_usd: float) -> dict:
        """Сколько заработано против цены вывода: имеет ли смысл возить активы."""
        skew = abs(self.update(route_id, buy_side_quote, sell_side_quote))
        profitable = earned_usd > self.withdraw_fee_usd
        return {
            "earned_usd": round(earned_usd, 4),
            "withdraw_fee_usd": self.withdraw_fee_usd,
            "skew_usd": round(skew, 4),
            "net_after_rebalance": round(earned_usd - self.withdraw_fee_usd, 4),
            "worth_rebalancing": profitable,
            "blocked_by_skew": skew > self.max_skew_usd,
        }


# Историческое имя класса оставлено как псевдоним.
SkewGuard = RebalanceEconomics


# --------------------------------------------------------------------------
# 8. Риск «одной ноги»
# --------------------------------------------------------------------------

@dataclass
class LegState:
    order_id: str
    exchange: str
    side: str
    filled_amount: float
    deadline: float


class LegRiskGuard:
    """Следит за тем, чтобы вторая нога закрылась, иначе — немедленный хедж."""

    def __init__(self, timeout_seconds: float = 3.0):
        self.timeout = timeout_seconds
        self.legs: Dict[str, LegState] = {}
        self.one_leg_events: List[dict] = []

    def track(self, route_id: str, order_id: str, exchange: str, side: str,
              amount: float, now: float, timeout: Optional[float] = None) -> LegState:
        leg = LegState(order_id, exchange, side, amount, now + (timeout or self.timeout))
        self.legs[route_id] = leg
        return leg

    def check(self, route_id: str, other_filled: bool, now: float) -> Optional[str]:
        """Возвращает 'hedge', если пора хеджить, иначе None."""
        leg = self.legs.get(route_id)
        if not leg:
            return None
        if other_filled:
            return None
        if now >= leg.deadline:
            self.one_leg_events.append({
                "ts": now, "exchange": leg.exchange, "side": leg.side,
                "amount": leg.filled_amount,
            })
            return "hedge"
        return None

    def clear(self, route_id: str) -> None:
        self.legs.pop(route_id, None)

    @property
    def one_leg_count(self) -> int:
        return len(self.one_leg_events)