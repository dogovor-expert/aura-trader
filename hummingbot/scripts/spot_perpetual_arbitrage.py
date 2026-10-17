# -*- coding: utf-8 -*-
"""Spot-Perpetual Basis Arbitrage (V2) — дельта-нейтральная базисная сделка.

basis = (P_perp − P_spot) / P_spot, в bps. Вход: ЛОНГ спот + ШОРТ перп, когда
базис положительный (перп дороже спота). При схождении базиса фиксируем
разницу: PnL ≈ (b_entry − b_exit) + полученное финансирование − комиссии.

Пороги (по исследованиям MRowhani/basis-funding-arbitrage-bot, иллюстративные):
  entry_min_bps 15   — покрывает ~12 bps round-trip комиссий + запас;
  exit_target_bps 5  — базис сошёлся, фиксируем прибыль;
  max_adverse_bps 40 — стоп, если базис расширился от входа;
  спред/проскальзывание каждой ноги — гейты перед входом.

Ноги по очереди (спот, затем перп-шорт), подтверждение — СОБЫТИЯ Hummingbot
(did_complete_*), закрытие — state-машина с ретраями (как в funding_carry).
Фид ставки (funding.json) — бонусный гейт: не входим в шорт, когда ставка
отрицательная (тогда мы бы платили).
"""
import json
import os
import time
from decimal import Decimal

from pydantic import Field, ConfigDict

from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class SpotPerpArbitrageConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: list = []

    spot_connector: str = Field(default="okx_paper_trade")
    perp_connector: str = Field(default="okx_perpetual_paper_trade")
    trading_pair: str = Field(default="BTC-USDT")

    # Базис: вход при basis >= entry_min_bps, выход при basis <= exit_target_bps.
    entry_min_bps: float = Field(default=15.0)
    exit_target_bps: float = Field(default=5.0)
    max_adverse_bps: float = Field(default=40.0)   # стоп: entry + это

    # Фид ставки (бонусный гейт): не входим в шорт при ставке < min_funding_bps.
    funding_file: str = Field(default="/home/hummingbot/data/funding.json")
    funding_ttl_sec: int = Field(default=1800)
    min_funding_bps: float = Field(default=0.0)

    # Ликвидность ног (гейты перед входом).
    max_spread_bps: float = Field(default=50.0)    # (ask-bid)/mid каждой ноги
    max_slippage_bps: float = Field(default=25.0)  # |лучшая цена − mid|/mid

    # Размер ноги и режим входа.
    order_quote: Decimal = Field(default=Decimal("100"))
    maker_entry: bool = Field(default=True)          # limit (maker) вместо market
    fill_timeout_sec: int = Field(default=45)        # лимит не исполнился -> отменить

    # Предохранители.
    emergency_loss_pct: float = Field(default=2.0)   # убыток позиции, % от нотационной
    max_hold_sec: int = Field(default=86400)         # time force-close (backstop)
    poll_interval_sec: int = Field(default=60)
    control_file: str = Field(default="/home/hummingbot/data/trading_control.json")
    state_file: str = Field(default="/home/hummingbot/data/basis_state.json")
    # Отдельный файл для панельного публиша: раньше он перетирал
    # state_file другой схемой (ноги терялись, _filled исчезал).
    publish_file: str = Field(default="/home/hummingbot/data/basis_publish.json")

    model_config = ConfigDict(extra='allow', arbitrary_types_allowed=True)

    def update_markets(self, markets):
        markets[self.spot_connector] = markets.get(self.spot_connector, set()) | {self.trading_pair}
        markets[self.perp_connector] = markets.get(self.perp_connector, set()) | {self.trading_pair}
        return markets


class SpotPerpArbitrage(StrategyV2Base):
    def __init__(self, connectors, config):
        super().__init__(connectors, config)
        self.config = config
        self._last_poll_ts = 0.0
        self._close_all_done = None
        self._close_all_closed = 0
        self._last_feed = None
        self._last_publish_ts = 0.0
        # Исполненные ордера фиксируются событиями Hummingbot
        # (did_complete_buy/sell_order): коннекторы НЕ имеют get_order(),
        # поэтому опрос статуса невозможен — только события.
        self._filled_orders = set()
        self._state = self._load_state()

    # ---------- события исполнения ----------

    def did_complete_buy_order(self, event):
        try:
            self._filled_orders.add(event.order_id)
            self._save_state()  # пережить рестарт между fill и переходом ноги
            self.logger().info(f"BASIS fill: {event.order_id}")
        except Exception:
            pass

    def did_complete_sell_order(self, event):
        try:
            self._filled_orders.add(event.order_id)
            self._save_state()
            self.logger().info(f"BASIS fill: {event.order_id}")
        except Exception:
            pass

    # ---------- состояние (переживает рестарт) ----------

    def _load_state(self):
        path = self.config.state_file
        for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(cand, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Подтверждённые исполнения переживают рестарт процесса
                    # (иначе после рестарта уже закрытые выходы «не подтвердятся»).
                    filled = data.pop("_filled", None)
                    if isinstance(filled, list):
                        self._filled_orders.update(str(x) for x in filled[-500:])
                    pos = data.get("position")
                    if isinstance(pos, dict) and "spot_leg" not in pos:
                        # В файл попал панельный публиш-формат (старый баг
                        # перетирания): ноги неизвестны — состояние неуправляемо.
                        data["position"] = None
                        data["last_close"] = {
                            "ts": time.time(),
                            "reason": "битое состояние (формат публиша) — позиция снята, проверь остаток!",
                            "pnl_quote": 0, "funding_est": 0, "unconfirmed": True}
                    return data
            except Exception:
                continue
        return {"position": None, "realized_pnl": 0.0, "closed": 0}

    def _save_state(self):
        path = self.config.state_file
        for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                os.makedirs(os.path.dirname(cand), exist_ok=True)
                tmp = cand + ".tmp"
                out = dict(self._state)
                out["_filled"] = sorted(self._filled_orders)[-500:]
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, cand)
                return
            except Exception:
                continue

    # ---------- фид ставки ----------

    def _read_feed(self):
        path = self.config.funding_file
        for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(cand, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("ts"):
                    data["age_sec"] = max(0.0, time.time() - float(data["ts"]))
                    return data
            except Exception:
                continue
        return None

    # ---------- управление переключателем ----------

    def _control_state(self):
        path = getattr(self.config, "control_file", None)
        if not path:
            return {"trading_enabled": False}
        for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(cand, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {"trading_enabled": False}
            except Exception:
                continue
        return {"trading_enabled": False}

    def _ack_close_all(self, requested):
        path = getattr(self.config, "control_file", None)
        if not path:
            return
        for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(cand, encoding="utf-8") as f:
                    data = json.load(f)
                data.pop("close_all_requested", None)
                data.pop("close_all_reason", None)
                data["close_all_done"] = requested
                tmp = cand + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, cand)
                return
            except Exception:
                continue

    # ---------- цены ----------

    def _price(self, conn_name, is_buy):
        conn = self.connectors.get(conn_name)
        if conn is None:
            return None
        try:
            p = conn.get_price(self.config.trading_pair, is_buy)
            return float(p) if p and float(p) > 0 else None
        except Exception:
            return None

    # ---------- исполнение ордера ----------

    def _order_done(self, conn_name, order_id):
        """True — ордер исполнен (по событию Hummingbot), False — ещё в работе.

        ВАЖНО: у коннекторов Hummingbot НЕТ метода get_order() (проверено по
        исходникам ConnectorBase/ExchangeBase) — раньше этот путь молча
        бросал AttributeError и подтверждение было невозможно никогда
        (позиция «не подтверждалась» 45с и разворачивалась, хотя ордер
        исполнялся через 100мс). Единственный источник истины — события
        did_complete_buy/sell_order.
        """
        if not order_id:
            return False
        return order_id in self._filled_orders

    def _cancel(self, conn_name, order_id):
        conn = self.connectors.get(conn_name)
        if conn is None:
            return
        try:
            conn.cancel_order(order_id=order_id, trading_pair=self.config.trading_pair)
        except TypeError:
            try:
                conn.cancel_order(self.config.trading_pair, order_id)
            except Exception:
                pass
        except Exception:
            pass

    def _submit(self, conn_name, side, amount, price, maker, position_action=None):
        """Limit (maker) или market. Возвращает order_id или None."""
        conn = self.connectors.get(conn_name)
        if conn is None or not conn.ready:
            return None
        price_dec = Decimal(str(price))
        order_type = OrderType.LIMIT if maker else OrderType.MARKET
        trade_side = TradeType.BUY if side == "BUY" else TradeType.SELL
        candidate = OrderCandidate(
            trading_pair=self.config.trading_pair,
            is_maker=maker,
            order_type=order_type,
            order_side=trade_side,
            amount=Decimal(str(amount)),
            price=price_dec,
        )
        try:
            adjusted = conn.budget_checker.adjust_candidate(candidate, all_or_none=False)
        except Exception as e:
            self.logger().warning(f"budget check failed on {conn_name}: {str(e)[:110]}")
            return None
        if adjusted.amount <= 0:
            self.logger().warning(f"insufficient balance on {conn_name} for {side}")
            return None
        try:
            if side == "BUY":
                return self.buy(conn_name, self.config.trading_pair, adjusted.amount,
                                order_type, price=price_dec, position_action=position_action)
            return self.sell(conn_name, self.config.trading_pair, adjusted.amount,
                             order_type, price=price_dec, position_action=position_action)
        except Exception as e:
            self.logger().warning(f"order failed on {conn_name}: {str(e)[:110]}")
            return None

    # ---------- позиция ----------

    def _position_pnl(self, pos):
        """(pnl_quote, pnl_pct, funding_est) по открытым ногам + оценка финансирования.

        Незакрытая/неоткрытая нога (нет цены входа) не участвует в расчёте:
        иначе отсутствие perp_price читается как 0 и даёт фейковый P&L
        в размере всей нотационной суммы.
        """
        amt = float(pos.get("amount") or 0.0)
        notional = float(pos.get("notional") or 0.0)
        spot_pnl = perp_pnl = 0.0
        spot_entry = float(pos.get("spot_price") or 0.0)
        perp_entry = float(pos.get("perp_price") or 0.0)
        if spot_entry > 0:
            spot_now = self._price(self.config.spot_connector, False)
            if spot_now:
                spot_pnl = (spot_now - spot_entry) * amt
        if perp_entry > 0:
            perp_now = self._price(self.config.perp_connector, False)
            if perp_now:
                perp_pnl = (perp_entry - perp_now) * amt  # шорт
        age = time.time() - float(pos.get("opened_at") or time.time())
        rate_bps = float((self._last_feed or {}).get("rate_bps") or 0.0)
        funding_est = rate_bps / 10000.0 * notional * (age / 28800.0)  # 8ч = 28800с
        total = spot_pnl + perp_pnl + funding_est
        pnl_pct = (total / notional * 100.0) if notional else 0.0
        return total, pnl_pct, funding_est

    def _open_position(self, feed, basis_bps):
        """Первая нога — спот-лонг; вторая ставится, когда первая исполнена."""
        price = self._price(self.config.spot_connector, True)
        if price is None:
            return
        quote = float(self.config.order_quote)
        amount = quote / price
        order_id = self._submit(self.config.spot_connector, "BUY", amount, price,
                                maker=bool(self.config.maker_entry))
        if not order_id:
            return
        self._state["position"] = {
            "opened_at": time.time(),
            "amount": amount,
            "spot_price": price,
            "spot_leg": "pending",
            "perp_leg": "waiting",
            "notional": quote,
            "entry_basis_bps": basis_bps,
            "entry_order_id": order_id,
            "entry_order_since": time.time(),
        }
        self._save_state()
        self.logger().info(f"BASIS open #1 (spot BUY {amount:.8f} @ {price} "
                           f"{'maker' if self.config.maker_entry else 'market'}) order={order_id}")

    def _open_second_leg(self, pos, feed):
        price = self._price(self.config.perp_connector, False)  # ask = наш шорт
        if price is None:
            return
        order_id = self._submit(self.config.perp_connector, "SELL", pos.get("amount"),
                                price, maker=bool(self.config.maker_entry),
                                position_action="OPEN")
        if not order_id:
            return
        pos["perp_price"] = price
        pos["perp_leg"] = "pending"
        pos["hedge_order_id"] = order_id
        pos["hedge_order_since"] = time.time()
        self._save_state()
        self.logger().info(f"BASIS open #2 (perp SELL {pos.get('amount'):.8f} @ {price} "
                           f"{'maker' if self.config.maker_entry else 'market'}) order={order_id}")

    def _held_legs(self, pos):
        """(спот держим, перп держим) по событийным подтверждениям.

        pending-нога, которую события НЕ подтвердили, = не исполнена → её не
        надо закрывать (иначе получили бы фантомный шорт на споте).
        """
        spot_held = pos.get("spot_leg") == "open" or (
            pos.get("spot_leg") == "pending"
            and pos.get("entry_order_id") in self._filled_orders)
        perp_held = pos.get("perp_leg") == "open" or (
            pos.get("perp_leg") == "pending"
            and pos.get("hedge_order_id") in self._filled_orders)
        return bool(spot_held), bool(perp_held)

    def _close_position(self, pos, reason):
        """Начать закрытие: отменить недоделанные входы, отправить выходы
        только по реально держимым ногам. Завершение — в _manage_close,
        когда события подтвердят исполнение выходов (или по таймауту с
        предупреждением)."""
        for leg, oid_key, conn_name in (
                ("spot_leg", "entry_order_id", self.config.spot_connector),
                ("perp_leg", "hedge_order_id", self.config.perp_connector)):
            if pos.get(leg) == "pending" and pos.get(oid_key):
                self._cancel(conn_name, pos[oid_key])
        spot_held, perp_held = self._held_legs(pos)
        pos["closing"] = {
            "reason": reason,
            "since": time.time(),
            "needs_spot": spot_held,
            "needs_perp": perp_held,
            "spot_exit": None,
            "perp_exit": None,
        }
        self._save_state()
        self.logger().info(
            f"BASIS close → unwind ({reason}): spot={'yes' if spot_held else 'no'} "
            f"perp={'yes' if perp_held else 'no'}")
        if not spot_held and not perp_held:
            # Ничего не держим — закрывать нечего.
            self._finalize_close(pos, reason)

    def _send_closing_legs(self, pos):
        """Отправить/дослать market-выходы по держимым ногам (ретрай при сбое)."""
        closing = pos.get("closing") or {}
        amt = pos.get("amount")
        if closing.get("needs_spot") and not closing.get("spot_exit"):
            oid = self._submit(self.config.spot_connector, "SELL", amt,
                               self._price(self.config.spot_connector, False) or 0,
                               maker=False)
            if oid:
                closing["spot_exit"] = oid
                self._save_state()
            else:
                self.logger().warning("BASIS: спот-выход не отправился — ретрай")
        if closing.get("needs_perp") and not closing.get("perp_exit"):
            oid = self._submit(self.config.perp_connector, "BUY", amt,
                               self._price(self.config.perp_connector, True) or 0,
                               maker=False, position_action="CLOSE")
            if oid:
                closing["perp_exit"] = oid
                self._save_state()
            else:
                self.logger().warning("BASIS: перп-выход не отправился — ретрай")

    def _manage_close(self, pos, now):
        """Подтверждение выходов событиями; таймаут -> завершение с WARNING."""
        closing = pos.get("closing")
        if not isinstance(closing, dict):
            self._finalize_close(pos, "некорректное состояние закрытия")
            return
        self._send_closing_legs(pos)
        closing = pos["closing"]
        needs = [k for k, need in (("spot_exit", closing.get("needs_spot")),
                                   ("perp_exit", closing.get("needs_perp"))) if need]
        got = sum(1 for k in needs if closing.get(k) in self._filled_orders)
        if needs and got >= len(needs):
            self._finalize_close(pos, closing.get("reason") or "закрыто")
            return
        if now - float(closing.get("since") or now) > self.config.fill_timeout_sec:
            self.logger().warning(
                f"BASIS: выход не подтвердился за {self.config.fill_timeout_sec}s "
                f"({got}/{len(needs)} ног) — закрываю состояние, проверь остаток!")
            closing["unconfirmed"] = True
            self._finalize_close(pos, closing.get("reason") or "закрыто (без подтверждения)")

    def _finalize_close(self, pos, reason):
        pnl, pnl_pct, funding = self._position_pnl(pos)
        unconfirmed = bool((pos.get("closing") or {}).get("unconfirmed"))
        self._state["realized_pnl"] = round(float(self._state.get("realized_pnl") or 0.0) + pnl, 8)
        self._state["closed"] = int(self._state.get("closed") or 0) + 1
        self._state["last_close"] = {"ts": time.time(), "reason": reason,
                                     "pnl_quote": round(pnl, 4), "funding_est": round(funding, 4),
                                     "unconfirmed": unconfirmed}
        self._state["position"] = None
        self._save_state()
        self.logger().info(f"BASIS close ({reason}): pnl={pnl:+.4f} USDT "
                           f"({pnl_pct:+.3f}%), funding_est={funding:+.4f}"
                           + (" [ВЫХОДЫ НЕ ПОДТВЕРЖДЕНЫ]" if unconfirmed else ""))

    # ---------- цикл ----------

    def tick(self, timestamp: float):
        """Не ждём готовности ВСЕХ коннекторов — только двух своих."""
        self._last_timestamp = timestamp
        need = [self.config.spot_connector, self.config.perp_connector]
        ready = [n for n in need if getattr(self.connectors.get(n), "ready", False)]
        if len(ready) < len(need):
            missing = [n for n in need if n not in ready]
            now = time.time()
            # Повторяем каждые 60с: «тишина в логе» не должна скрывать
            # неготовый коннектор (так потеряли бы проблему в проде).
            if now - float(getattr(self, "_last_missing_log", 0)) > 60:
                self._last_missing_log = now
                self.logger().warning(f"waiting for connectors: {missing}")
            return
        if not self.ready_to_trade:
            self.ready_to_trade = True
            self._warned_missing = False
            self.logger().info(f"BASIS trading with {self.config.spot_connector} + "
                               f"{self.config.perp_connector}")
        self.on_tick()

    def on_tick(self):
        now = self.current_timestamp
        feed = self._read_feed()
        if feed:
            self._last_feed = feed
        feed = self._last_feed
        control = self._control_state()
        pos = self._state.get("position")
        at_poll = now - self._last_poll_ts >= self.config.poll_interval_sec
        if at_poll:
            self._last_poll_ts = now
            self.logger().info(
                f"BASIS poll: rate={(feed or {}).get('rate_bps')}b basis={(feed or {}).get('basis_bps')}b "
                f"age={int((feed or {}).get('age_sec') or -1)}s "
                f"enabled={bool(control.get('trading_enabled'))} "
                f"pos={'yes' if pos else 'no'}")

        # «Закрыть всё» работает всегда, даже при выключенной торговле.
        requested = control.get("close_all_requested")
        if requested and requested != self._close_all_done:
            self._close_all_closed = 0
            if pos:
                self._close_position(pos, "close-all по кнопке")
                self._close_all_closed += 1
            self._close_all_done = requested
            self._ack_close_all(requested)
            self._publish_state("running" if control.get("trading_enabled") else "paused")
            return

        # Управление позицией — КАЖДЫЙ тик: подтверждение исполнения приходит
        # по событиям, а стопы/таймауты не должны ждать poll (раньше таймаут
        # 45с отрабатывал раньше первой проверки из-за poll 60с).
        if pos:
            if pos.get("closing"):
                self._manage_close(pos, now)
            else:
                self._manage_position(pos, feed, now)
            if at_poll or now - float(getattr(self, "_last_publish_ts", 0)) > 10:
                self._last_publish_ts = now
                self._publish_state(
                    "running" if control.get("trading_enabled") else "paused")
            return

        # Плоский портфель: лог/публикация/входы — по poll.
        if not at_poll:
            return
        if not control.get("trading_enabled"):
            self._publish_state("paused")
            return
        self._publish_state("running")
        self._maybe_open(feed, now)

    def _manage_position(self, pos, feed, now):
        # Продвижение ног (вход по частям).
        if pos.get("spot_leg") == "pending":
            done = self._order_done(self.config.spot_connector, pos.get("entry_order_id"))
            if done:
                pos["spot_leg"] = "open"
                pos.pop("entry_order_id", None)
                self._save_state()
                self._open_second_leg(pos, feed)
            elif now - float(pos.get("entry_order_since") or 0) > self.config.fill_timeout_sec:
                # Спот не подтвердился за fill_timeout: отменяем и аккуратно
                # выходим (если нога всё-таки исполнилась — продаём её, а не
                # оставляем одинокий лонг).
                self._cancel(self.config.spot_connector, pos.get("entry_order_id"))
                self.logger().warning("BASIS: spot leg not confirmed in time — unwinding")
                self._close_position(pos, "спот-нога не подтвердилась")
                return
        elif pos.get("perp_leg") == "pending":
            done = self._order_done(self.config.perp_connector, pos.get("hedge_order_id"))
            if done:
                pos["perp_leg"] = "open"
                pos.pop("hedge_order_id", None)
                self._save_state()
                self.logger().info("BASIS position fully open (spot long + perp short)")
            elif now - float(pos.get("hedge_order_since") or 0) > self.config.fill_timeout_sec:
                # Шорт не исполнился: раскрыться в ноль (спот уже куплен).
                self._cancel(self.config.perp_connector, pos.get("hedge_order_id"))
                self.logger().warning("BASIS: hedge not filled — unwinding spot leg")
                self._close_position(pos, "hedge не исполнился")
                return

        # Обе ноги открыты — следим за рисками.
        if pos.get("spot_leg") == "open" and pos.get("perp_leg") == "open":
            pnl, pnl_pct, _ = self._position_pnl(pos)
            if pnl_pct <= -float(self.config.emergency_loss_pct):
                self._close_position(pos, f"аварийный стоп {pnl_pct:+.2f}%")
                return
            basis = self._exec_basis_bps()
            if basis is not None:
                entry_b = float(pos.get("entry_basis_bps") or basis)
                if basis <= self.config.exit_target_bps:
                    self._close_position(
                        pos, f"базис сошёлся {basis:.1f}b <= {self.config.exit_target_bps}b")
                    return
                if basis >= entry_b + self.config.max_adverse_bps:
                    self._close_position(
                        pos, f"базис расширился {basis:.1f}b >= "
                             f"{entry_b:.1f}+{self.config.max_adverse_bps}b")
                    return
            age = now - float(pos.get("opened_at") or now)
            if age > self.config.max_hold_sec:
                self._close_position(pos, f"time force-close ({int(age)}s)")
                return

    def _exec_basis_bps(self):
        """Исполняемый базис (perp_bid − spot_ask)/spot_ask в bps: именно по
        этим ценам войдут/выйдут обе ноги, спреды уже учтены."""
        spot_ask = self._price(self.config.spot_connector, True)
        perp_bid = self._price(self.config.perp_connector, False)
        if not spot_ask or not perp_bid:
            return None
        return (perp_bid - spot_ask) / spot_ask * 10000.0

    def _liquidity_ok(self):
        """Гейты ликвидности: спред и проскальзывание каждой ноги."""
        for name, is_buy in ((self.config.spot_connector, True),
                             (self.config.perp_connector, False)):
            conn = self.connectors.get(name)
            if conn is None:
                return False
            try:
                bid = float(conn.get_price(self.config.trading_pair, False) or 0)
                ask = float(conn.get_price(self.config.trading_pair, True) or 0)
            except Exception:
                return False
            if bid <= 0 or ask <= 0:
                return False
            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10000.0
            exec_px = ask if is_buy else bid
            slip_bps = abs(exec_px - mid) / mid * 10000.0
            if spread_bps > self.config.max_spread_bps:
                self.logger().info(
                    f"BASIS: спред {name} {spread_bps:.1f}b > {self.config.max_spread_bps}b — вход отложен")
                return False
            if slip_bps > self.config.max_slippage_bps:
                self.logger().info(
                    f"BASIS: проскальзывание {name} {slip_bps:.1f}b > {self.config.max_slippage_bps}b — вход отложен")
                return False
        return True

    def _maybe_open(self, feed, now):
        basis = self._exec_basis_bps()
        if basis is None:
            return
        if basis < self.config.entry_min_bps:
            return  # базис не созрел — обычное состояние рынка (лог не засоряем)
        if not self._liquidity_ok():
            return
        # Бонусный гейт по свежему фиду: не входим в шорт, когда ставка
        # отрицательная (тогда мы бы платили за удержание).
        if feed and (feed.get("age_sec") or 0) <= self.config.funding_ttl_sec:
            rate = float(feed.get("rate_bps") or 0.0)
            if rate < self.config.min_funding_bps:
                self.logger().info(
                    f"BASIS: ставка {rate}b < {self.config.min_funding_bps}b — вход отложен")
                return
        self.logger().info(
            f"BASIS entry: базис {basis:.1f}b >= {self.config.entry_min_bps}b")
        self._open_position(feed, basis)
        # Вторую ногу поставит _manage_position после подтверждения спота.

    # ---------- состояние для панели ----------

    def _publish_state(self, mode):
        try:
            feed = self._last_feed or {}
            pos = self._state.get("position")
            payload = {
                "mode": mode,
                "trading_enabled": mode == "running",
                "updated_at": time.time(),
                "funding_rate_bps": feed.get("rate_bps"),
                "basis_bps": feed.get("basis_bps"),
                "feed_age_sec": feed.get("age_sec"),
                "realized_pnl": round(float(self._state.get("realized_pnl") or 0.0), 8),
                "closed": int(self._state.get("closed") or 0),
                "position": None,
            }
            if pos:
                pnl, pnl_pct, funding = self._position_pnl(pos)
                payload["position"] = {
                    "pair": self.config.trading_pair,
                    "amount": pos.get("amount"),
                    "notional": pos.get("notional"),
                    "opened_at": pos.get("opened_at"),
                    "legs": [pos.get("spot_leg"), pos.get("perp_leg")],
                    "pnl_quote": round(pnl, 6),
                    "pnl_pct": round(pnl_pct, 3),
                    "funding_est": round(funding, 6),
                    "age_min": round((time.time() - float(pos.get("opened_at") or time.time())) / 60.0, 1),
                }
            path = self.config.publish_file
            for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
                try:
                    os.makedirs(os.path.dirname(cand), exist_ok=True)
                    tmp = cand + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, cand)
                    return
                except Exception:
                    continue
        except Exception as e:
            self.logger().warning(f"BASIS state publish failed: {str(e)[:80]}")

    def format_status(self):
        feed = self._last_feed or {}
        pos = self._state.get("position")
        lines = [
            f"Funding BASIS | ставка={feed.get('rate_bps')}b/8ч базис={feed.get('basis_bps')}b "
            f"(фид: {int(feed.get('age_sec') or -1)}s назад)",
            f"realized={self._state.get('realized_pnl', 0)} USDT, закрытий={self._state.get('closed', 0)}",
            "",
        ]
        if pos:
            pnl, pnl_pct, funding = self._position_pnl(pos)
            lines.append(f"  ПОЗИЦИЯ: {pos.get('amount')} {self.config.trading_pair} "
                         f"sp={pos.get('spot_price')} perp={pos.get('perp_price')} "
                         f"legs={pos.get('spot_leg')}/{pos.get('perp_leg')} "
                         f"pnl={pnl:+.4f} ({pnl_pct:+.3f}%) funding~{funding:+.4f}")
        else:
            lines.append("  flat — жду входа по ставке")
        return "\n".join(lines)
