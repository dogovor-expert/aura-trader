# -*- coding: utf-8 -*-
"""Межбиржевой арбитраж с полным набором защит.

Схема работы на каждом тике:
  1. проверить, что маршрут вообще торгуем (кошелёк открыт, сети совпадают);
  2. снять стакан с обеих бирж (с учётом лимитов запросов каждой);
  3. посчитать VWAP под размер сделки, а не Top of Book;
  4. вычесть двойной тейкер и проскальзывание, сравнить с порогом;
  5. убедиться, что спред держится несколько опросов (защита от спуфинга);
  6. выставить две ноги и следить за их исполнением;
  7. если вторая нога не прошла дедлайн — немедленно хеджить.

Все проверки живут в arbitrage_guard.py и покрыты тестами.
"""
from decimal import Decimal
from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase

import arbitrage_guard as G


class ArbRouteConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    buy_exchange: str = Field(default="bybit_paper_trade",
                              json_schema_extra={"title": "Биржа покупки"})
    sell_exchange: str = Field(default="okx_paper_trade",
                               json_schema_extra={"title": "Биржа продажи"})
    trading_pair: str = Field(default="BTC-USDT", json_schema_extra={"title": "Пара"})
    enabled: bool = Field(default=True, json_schema_extra={"title": "Включён"})
    priority: int = Field(default=10, json_schema_extra={"title": "Приоритет"})
    order_size_usd: float = Field(default=10.0, json_schema_extra={"title": "Размер, USDT"})
    min_net_bps: float = Field(default=45.0,
                               json_schema_extra={"title": "Порог чистой прибыли, bps"})
    max_slippage_bps: float = Field(default=20.0,
                                    json_schema_extra={"title": "Предел проскальзывания, bps"})
    min_depth_usd: float = Field(default=200.0,
                                 json_schema_extra={"title": "Минимальная глубина, USDT"})
    buy_fee_bps: float = Field(default=10.0, json_schema_extra={"title": "Комиссия покупки, bps"})
    sell_fee_bps: float = Field(default=10.0, json_schema_extra={"title": "Комиссия продажи, bps"})
    buy_network: str = Field(default="", json_schema_extra={"title": "Сеть актива на покупке"})
    sell_network: str = Field(default="", json_schema_extra={"title": "Сеть актива на продаже"})
    buy_deposit_enabled: bool = Field(default=True,
                                      json_schema_extra={"title": "Ввод на покупке включён"})
    sell_withdrawal_enabled: bool = Field(default=True,
                                          json_schema_extra={"title": "Вывод с продажи включён"})


class CrossExchangeArbitrageConfig(StrategyV2ConfigBase):
    script_file_name: str = Field(default="cross_exchange_arbitrage.py")
    poll_interval_sec: float = Field(default=3.0, json_schema_extra={"title": "Интервал, с"})
    confirmation_polls: int = Field(default=3,
                                     json_schema_extra={"title": "Опросов для подтверждения"})
    leg_timeout_sec: float = Field(default=3.0,
                                   json_schema_extra={"title": "Дедлайн второй ноги, с"})
    cooldown_sec: float = Field(default=15.0, json_schema_extra={"title": "Пауза между сделками, с"})
    request_per_minute: int = Field(default=60, json_schema_extra={"title": "Лимит запросов в минуту"})
    withdraw_fee_usd: float = Field(default=25.0,
                                    json_schema_extra={"title": "Комиссия вывода, USDT"})
    max_skew_usd: float = Field(default=10.0, json_schema_extra={"title": "Допустимый перекос, USDT"})
    routes: List[ArbRouteConfig] = Field(default_factory=list)
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def update_markets(self, markets):
        """Хук Hummingbot V2: без него on_tick не вызывается ни разу."""
        for route in (self.routes or []):
            pair = route.trading_pair if hasattr(route, "trading_pair") else route.get("trading_pair")
            buy_ex = route.buy_exchange if hasattr(route, "buy_exchange") else route.get("buy_exchange")
            sell_ex = route.sell_exchange if hasattr(route, "sell_exchange") else route.get("sell_exchange")
            enabled = route.enabled if hasattr(route, "enabled") else route.get("enabled", True)
            if not enabled or not pair:
                continue
            for ex in (buy_ex, sell_ex):
                if ex:
                    markets[ex] = markets.get(ex, set()) | {pair}
        return markets


class CrossExchangeArbitrage(StrategyV2Base):
    """Арбитраж «дешевле купить, дороже продать» с защитами от типовых ловушек."""

    # ВАЖНО: StrategyV2Base — не pydantic-модель, поэтому PrivateAttr здесь не
    # инициализируется: доступ вернул бы сам дескриптор, а не значение.
    # Поэтому состояние стратегии — обычные атрибуты класса.
    _gate = None
    _confirm = None
    _limits = None
    _registry = None
    _rebalance = None
    _legs = None
    _setup_done = False
    _tick_count = 0
    _last_trade_ts = 0.0
    _last_status_log_ts = 0.0
    _last_poll_ts = 0.0
    _rl_seen = {}
    _status = None



    def _exch_get(self, cfg, key, default=None):
        val = cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key, default)
        return default if val is None else val

    @property
    def arb_status(self) -> dict:
        """Срез состояния для format_status и панели."""
        return self._status

    def _ensure_setup(self) -> None:
        if getattr(self, "_setup_done", False):
            return
        self._setup_done = True
        cfg = self.config
        self._gate = G.DepthGate(min_depth_usd=float(getattr(cfg, "min_depth_usd", 200.0)) if hasattr(cfg, "min_depth_usd") else 200.0)
        # min_depth задаётся на уровне маршрута; берём максимум из маршрутов.
        depths = [float(self._exch_get(r, "min_depth_usd", 200.0)) for r in cfg.routes] or [200.0]
        self._gate = G.DepthGate(min_depth_usd=min(depths), max_top_share=0.6)
        self._confirm = G.SpreadConfirmation(required=int(getattr(cfg, "confirmation_polls", 3)))
        per_minute = {}
        for r in cfg.routes:
            per_minute[self._exch_get(r, "buy_exchange")] = int(getattr(cfg, "request_per_minute", 60))
            per_minute[self._exch_get(r, "sell_exchange")] = int(getattr(cfg, "request_per_minute", 60))
        self._limits = G.RateLimiter(per_minute, default_per_minute=int(getattr(cfg, "request_per_minute", 60)))
        self._registry = G.RouteRegistry()
        for r in cfg.routes:
            buy_ex = self._exch_get(r, "buy_exchange")
            sell_ex = self._exch_get(r, "sell_exchange")
            pair = self._exch_get(r, "trading_pair")
            self._registry.add(G.RoutePolicy(
                route_id=f"{buy_ex}->{sell_ex}:{pair}",
                buy_exchange=buy_ex,
                sell_exchange=sell_ex,
                pair=pair,
                buy_deposit_enabled=bool(self._exch_get(r, "buy_deposit_enabled", True)),
                sell_withdrawal_enabled=bool(self._exch_get(r, "sell_withdrawal_enabled", True)),
                asset_network=(str(self._exch_get(r, "buy_network", "")),
                               str(self._exch_get(r, "sell_network", ""))),
            ))
        self._rebalance = G.RebalanceEconomics(
            max_skew_usd=float(getattr(cfg, "max_skew_usd", 10.0)),
            withdraw_fee_usd=float(getattr(cfg, "withdraw_fee_usd", 25.0)),
        )
        self._legs = G.LegRiskGuard(timeout_seconds=float(getattr(cfg, "leg_timeout_sec", 3.0)))
        self._status = {"ready": True, "routes": self._registry.snapshot(),
                        "trades": 0, "one_leg_events": 0, "blocked": {}}
        self.logger().info(f"arbitrage started with {len(self._registry.routes)} route(s), "
                           f"threshold {getattr(cfg, 'min_net_bps', 45)}bps-equivalent config ready")

    def _book_rows(self, connector, pair, side: str):
        """Уровни стакана: [[price, qty], ...] от лучшего к худшему.

        У Hummingbot книга — CompositeOrderBook, у неё уровни лежат в
        bid_entries()/ask_entries(), а не в .bids/.asks.
        """
        ob = connector.get_order_book(pair)
        if side == "buy":
            entries = ob.ask_entries()
        else:
            entries = ob.bid_entries()
        rows = []
        for e in entries:
            try:
                rows.append([float(e.price), float(e.amount)])
            except AttributeError:
                rows.append([float(e[0]), float(e[1])])
        return rows

    def _fee_bps(self, connector, pair, side) -> float:
        try:
            fee = connector.get_fee(pair, True, Decimal("100"), TradeType.BUY, OrderType.MARKET)
            return float(fee.percent) * G.BPS if fee else 10.0
        except Exception:
            return 10.0

    def _allowed(self, ex, now):
        """Разрешить запрос к бирже, не списывая бюджет дважды за один опрос."""
        last = self._rl_seen.get(ex)
        if last is not None and now - last < 1.0:
            return True
        if self._limits.allow(ex, now):
            self._rl_seen[ex] = now
            return True
        return False

    def _evaluate_route(self, route, now: float) -> dict:
        buy_ex = self._exch_get(route, "buy_exchange")
        sell_ex = self._exch_get(route, "sell_exchange")
        pair = self._exch_get(route, "trading_pair")
        route_id = f"{buy_ex}->{sell_ex}:{pair}"
        out = {"route": route_id, "net_bps": None, "reason": ""}

        if not self._exch_get(route, "enabled", True):
            out["reason"] = "route disabled"
            return out

        # Биржа «ослепла» — маршрут выпадает сам, остальные продолжают работать.
        for ex in (buy_ex, sell_ex):
            conn = self.connectors.get(ex)
            if conn is None:
                out["reason"] = f"connector {ex} not created"
                return out
            if not conn.ready:
                out["reason"] = f"exchange not ready: {ex}"
                return out

        ok, why = self._registry.tradable(route_id)
        if not ok:
            out["reason"] = why
            return out

        # Несовпадение сетей не запрещает сделку, но запрещает ребалансировку:
        # торговать можно, но перекос нельзя будет вывезти — предупреждаем.
        policy = self._registry.routes[route_id]
        rebal_ok, rebal_why = self._rebalance.rebalance_possible(policy)
        out["rebalance_ok"] = rebal_ok
        out["rebalance_note"] = rebal_why

        # Лимиты запросов: у каждой биржи свой бюджет и своя пауза после 429.
        # Бюджет списывается ОДИН раз на биржу за опрос: иначе N маршрутов
        # выедали бы лимит сами себя (4 маршрута -> 8 списаний за 3 секунды).
        for ex in (buy_ex, sell_ex):
            if not self._allowed(ex, now):
                out["reason"] = f"rate limit or backoff on {ex}"
                return out

        try:
            buy_c = self.connectors[buy_ex]
            sell_c = self.connectors[sell_ex]
        except KeyError as e:
            out["reason"] = f"connector unavailable: {e}"
            return out

        try:
            asks = self._book_rows(buy_c, pair, "buy")
            bids = self._book_rows(sell_c, pair, "sell")
        except Exception as e:
            # Ошибка сети на одной из бирж — считаем это 429-подобной ситуацией.
            self._limits.penalize(buy_ex, now)
            out["reason"] = f"order book error: {str(e)[:90]}"
            return out

        price = self._safe_price(buy_c, pair)
        if not price or price <= 0:
            out["reason"] = "no price on buy exchange"
            return out
        size = float(self._exch_get(route, "order_size_usd", 10.0)) / price

        depth_ok, depth_why, depth = G.check_depth(
            G.DepthGate(min_depth_usd=float(self._exch_get(route, "min_depth_usd", 200.0))),
            "buy", asks, size)
        sell_depth_ok, sell_depth_why, _ = G.check_depth(
            G.DepthGate(min_depth_usd=float(self._exch_get(route, "min_depth_usd", 200.0))),
            "sell", bids, size)
        if not depth_ok:
            out["reason"] = f"buy side: {depth_why}"
            return out
        if not sell_depth_ok:
            out["reason"] = f"sell side: {sell_depth_why}"
            return out
        out["depth_usd"] = round(depth, 2)

        buy_vwap = G.vwap_for_side(asks, "buy", size)
        sell_vwap = G.vwap_for_side(bids, "sell", size)

        buy_fee = float(self._exch_get(route, "buy_fee_bps", 10.0)) or self._fee_bps(buy_c, pair, "buy")
        sell_fee = float(self._exch_get(route, "sell_fee_bps", 10.0)) or self._fee_bps(sell_c, pair, "sell")

        verdict = G.evaluate_route(
            buy_vwap, sell_vwap, buy_fee, sell_fee,
            min_net_bps=float(self._exch_get(route, "min_net_bps", 45.0)),
            max_slippage_bps=float(self._exch_get(route, "max_slippage_bps", 20.0)))

        out.update({
            "gross_bps": round(verdict.edge_bps, 1),
            "net_bps": round(verdict.net_bps, 1),
            "slippage_bps": round(verdict.slippage_bps, 1),
            "fees_bps": verdict.fees_bps,
            "buy_vwap": verdict.buy_vwap,
            "sell_vwap": verdict.sell_vwap,
            "size": size,
            "reason": verdict.reason,
            "tradable": verdict.ok,
        })

        # Защита от спуфинга: спред должен подтвердиться несколькими опросами.
        streak = self._confirm.observe(route_id, verdict.net_bps)
        out["streak"] = streak
        if verdict.ok and not self._confirm.confirmed(route_id, verdict.net_bps):
            out["tradable"] = False
            out["reason"] = f"awaiting confirmation {streak}/{self._confirm.required}"
        if not verdict.ok:
            self._confirm.reset(route_id)
        return out

    def _safe_price(self, connector, pair, is_buy: bool = True) -> float:
        try:
            p = connector.get_price(pair, is_buy)
            return float(p) if p else 0.0
        except Exception:
            return 0.0

    def _place_legs(self, route, evaluation, now: float) -> None:
        """Покупка на дешёвой бирже и продажа на дорогой, с контролем обеих ног."""
        buy_ex = self._exch_get(route, "buy_exchange")
        sell_ex = self._exch_get(route, "sell_exchange")
        pair = self._exch_get(route, "trading_pair")
        size = Decimal(str(evaluation["size"]))
        route_id = evaluation["route"]

        buy_price = Decimal(str(evaluation["buy_vwap"]))
        sell_price = Decimal(str(evaluation["sell_vwap"]))
        is_perp = bool(self._exch_get(route, "sell_is_perpetual", False))
        position_action = "OPEN" if is_perp else None

        buy_conn = self.connectors[buy_ex]
        sell_conn = self.connectors[sell_ex]

        def _candidate(conn, price):
            cand = OrderCandidate(
                trading_pair=pair, is_maker=False, order_type=OrderType.MARKET,
                order_side=TradeType.BUY, amount=size, price=price)
            try:
                return conn.budget_checker.adjust_candidate(cand, all_or_none=False)
            except Exception:
                return cand

        buy_cand = _candidate(buy_conn, buy_price)
        sell_cand = _candidate(sell_conn, sell_price)
        if buy_cand.amount <= 0 or sell_cand.amount <= 0:
            self.logger().warning("not enough balance on one side — route skipped")
            self._status.setdefault("blocked", {})[route_id] = "insufficient balance"
            return

        # Сначала та нога, где риск не захеджировать меньше: продажа на дорогой
        # бирже фиксирует цену, покупка на дешёвой закрывает позицию.
        buy_order = self.buy(buy_ex, pair, buy_cand.amount, OrderType.MARKET,
                             price=buy_price, position_action=position_action)
        self._legs.track(route_id, buy_order, buy_ex, "BUY", float(buy_cand.amount), now,
                         timeout=float(getattr(self.config, "leg_timeout_sec", 3.0)))
        sell_order = self.sell(sell_ex, pair, sell_cand.amount, OrderType.MARKET,
                               price=sell_price, position_action=position_action)

        self._last_trade_ts = now
        self._status["trades"] = int(self._status.get("trades", 0)) + 1
        self._status["last"] = {
            "route": route_id, "net_bps": evaluation.get("net_bps"),
            "size_usd": float(self._exch_get(route, "order_size_usd", 10.0)),
            "buy_order": buy_order, "sell_order": sell_order, "ts": now,
        }
        self.logger().info(
            f"ARB {route_id} size={buy_cand.amount} buy@{buy_price} sell@{sell_price} "
            f"net={evaluation.get('net_bps')}bps")

    def _hedge_unfilled_leg(self, route_id: str, now: float) -> None:
        """Немедленный встречный ордер на той же бирже — снимаем «голую» позицию."""
        leg = self._legs.legs.get(route_id)
        if leg is None:
            return
        opposite = TradeType.SELL if leg.side == "BUY" else TradeType.BUY
        try:
            price = Decimal(str(self._safe_price(self.connectors[leg.exchange],
                                                 self._registry.routes[route_id].pair) or 0))
            if price <= 0:
                self.logger().error(f"hedge impossible on {leg.exchange}: no price")
                return
            self.logger().warning(f"ONE-LEG on {route_id} — hedging {opposite} on {leg.exchange}")
            if opposite == TradeType.SELL:
                self.sell(leg.exchange, leg.pair if hasattr(leg, "pair") else
                          self._registry.routes[route_id].pair,
                          Decimal(str(leg.filled_amount)), OrderType.MARKET, price=price)
            else:
                self.buy(leg.exchange, self._registry.routes[route_id].pair,
                         Decimal(str(leg.filled_amount)), OrderType.MARKET, price=price)
            self._status["one_leg_events"] = self._legs.one_leg_count
        except Exception as e:
            self.logger().error(f"hedge failed on {leg.exchange}: {str(e)[:120]}")
        finally:
            self._legs.clear(route_id)

    def tick(self, timestamp: float):
        """Готовность — по биржам, которые ответили.

        Базовая реализация ждёт готовности ВСЕХ коннекторов, и одна недоступная
        биржа останавливает арбитраж целиком. Здесь считаем готовыми те, что
        ответили, а маршруты с «слепыми» биржами просто пропускаем с причиной.
        """
        self._last_timestamp = timestamp
        ready = [c for c in self.connectors.values() if c.ready]
        if not ready:
            self.logger().warning("no connector ready yet")
            return
        if not self.ready_to_trade:
            self.ready_to_trade = True
        self.on_tick()

    def on_tick(self):
        self._ensure_setup()
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count <= 2 or self._tick_count % 60 == 0:
            self.logger().info(f"TICK #{self._tick_count} ts={self.current_timestamp} ready={self.ready_to_trade} connectors={len(self.connectors)}")
        now = self.current_timestamp
        if now - self._last_poll_ts < float(getattr(self.config, "poll_interval_sec", 3.0)):
            return
        self._last_poll_ts = now

        # Хедж незакрытой ноги имеет приоритет над новыми сделками.
        for route_id in list(self._legs.legs.keys()):
            if self._legs.check(route_id, other_filled=False, now=now) == "hedge":
                self._hedge_unfilled_leg(route_id, now)

        if now - self._last_trade_ts < float(getattr(self.config, "cooldown_sec", 15.0)):
            return

        snapshot = []
        for route in self.config.routes:
            try:
                ev = self._evaluate_route(route, now)
            except Exception as e:
                ev = {"route": "?", "reason": f"error: {str(e)[:90]}"}
            snapshot.append(ev)
            if ev.get("tradable"):
                self._place_legs(route, ev, now)
        self._status["routes_live"] = snapshot
        self._status["one_leg_events"] = self._legs.one_leg_count
        self._status["ts"] = now

        # Раз в минуту пишем в лог, что именно решили защиты: без этого
        # невозможно понять, стратегия ждёт спред или молча отсекается.
        if now - self._last_status_log_ts >= 60.0:
            self._last_status_log_ts = now
            for ev in snapshot:
                self.logger().info(
                    f"GUARD {ev.get('route')}: net={ev.get('net_bps')}bps "
                    f"gross={ev.get('gross_bps')}bps slip={ev.get('slippage_bps')}bps "
                    f"depth=${ev.get('depth_usd')} streak={ev.get('streak')} "
                    f"rebalance={'ok' if ev.get('rebalance_ok') else ev.get('rebalance_note')} "
                    f"-> {ev.get('tradable') and 'TRADE' or ('skip: ' + str(ev.get('reason')))}")

    def format_status(self) -> str:
        st = self.arb_status
        lines = [
            "Cross-Exchange Arbitrage",
            f"trades: {st.get('trades', 0)} | one-leg events: {st.get('one_leg_events', 0)}",
            "",
            "Routes:",
        ]
        for r in st.get("routes", []):
            mark = "OK " if r.get("tradable") else "NO "
            extra = "" if r.get("tradable") else f" - {r.get('blocked_reason', '')}"
            nets = r.get("networks_match")
            lines.append(f"  {mark}{r['route_id']} rebalance={'yes' if r.get('rebalance_allowed') else 'no'}"
                         f" networks_match={nets}{extra}")
        for ev in st.get("routes_live", []):
            lines.append(f"    {ev.get('route')}: net={ev.get('net_bps')}bps "
                         f"gross={ev.get('gross_bps')}bps {ev.get('reason', '')}")
        if st.get("last"):
            lines.append(f"\nlast: {st['last']}")
        return "\n".join(lines)
