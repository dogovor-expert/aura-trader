# -*- coding: utf-8 -*-
"""Pump / Momentum (V2) — вход на всплеске объёма с подтверждением 2 бирж.

Детекция (по свечам okx_perpetual 1m, пороги из публичного pine Volume_Spike):
  volume > SMA(volume, volume_period) * volume_multiplier   (по умолч. 2.0)
  И |Δclose| за бар >= price_move_threshold %               (по умолч. 1.0%)
Направление: бычий памп -> LONG, медвежий -> SHORT (perp, position_action OPEN).

Подтверждение (свой код, готового примера в HB нет): за confirm_window_sec
обе биржи (okx_perpetual + bybit_perpetual, last_traded_price) сдвинулись
в ТОМ ЖЕ направлении хотя бы на confirm_min_move_pct. Иначе — одиночный
всплеск, входа нет.

Вход: лимитник по bid/ask (maker), гейты спреда/проскальзывания.
Выходы: стоп 1×ATR, тейк 2×ATR (цены фиксируются при входе), time
force-close, аварийный убыток, «закрыть всё». Подтверждение исполнения —
СОБЫТИЯ Hummingbot (did_complete_*), закрытие — state-машина с ретраями.
"""
import json
import os
import time
from collections import deque
from decimal import Decimal

from pydantic import Field, ConfigDict

from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class PumpMomentumConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: list = []

    # Детекция (свечи).
    candle_connector: str = Field(default="okx_perpetual")   # источник свечей (без _paper)
    candle_interval: str = Field(default="1m")
    max_records: int = Field(default=30)     # >= volume_period + запас (warmup ~30 мин)
    volume_period: int = Field(default=20)
    volume_multiplier: float = Field(default=2.0)
    price_move_threshold_pct: float = Field(default=1.0)

    # Подтверждение второй биржей (last_traded_price).
    entry_connector: str = Field(default="okx_perpetual_paper_trade")
    confirm_connector: str = Field(default="bybit_perpetual_paper_trade")
    trading_pair: str = Field(default="BTC-USDT")
    confirm_window_sec: int = Field(default=90)
    confirm_min_move_pct: float = Field(default=0.2)

    # ATR-выходы.
    atr_length: int = Field(default=14)
    sl_atr_mult: float = Field(default=1.0)
    tp_atr_mult: float = Field(default=2.0)

    # Исполнение и лимиты.
    order_quote: Decimal = Field(default=Decimal("100"))
    max_spread_bps: float = Field(default=50.0)
    cooldown_sec: int = Field(default=600)
    max_hold_sec: int = Field(default=1800)

    # Предохранители.
    emergency_loss_pct: float = Field(default=3.0)
    fill_timeout_sec: int = Field(default=45)
    poll_interval_sec: int = Field(default=60)
    control_file: str = Field(default="/home/hummingbot/data/trading_control.json")
    state_file: str = Field(default="/home/hummingbot/data/pump_state.json")
    publish_file: str = Field(default="/home/hummingbot/data/pump_publish.json")

    model_config = ConfigDict(extra='allow', arbitrary_types_allowed=True)

    def update_markets(self, markets):
        markets[self.entry_connector] = markets.get(self.entry_connector, set()) | {self.trading_pair}
        markets[self.confirm_connector] = markets.get(self.confirm_connector, set()) | {self.trading_pair}
        return markets


class PumpMomentum(StrategyV2Base):
    def __init__(self, connectors, config):
        super().__init__(connectors, config)
        self.config = config
        self._last_poll_ts = 0.0
        self._last_publish_ts = 0.0
        self._close_all_done = None
        self._close_all_closed = 0
        self._last_entry_ts = 0.0
        self._filled_orders = set()
        self._state = self._load_state()
        # Окно цен для подтверждения: connector -> deque[(ts, price)]
        self._price_window = {config.entry_connector: deque(),
                              config.confirm_connector: deque()}
        # Свечи: источник детекции (только чтение).
        self._candles = CandlesFactory.get_candle(CandlesConfig(
            connector=config.candle_connector, trading_pair=config.trading_pair,
            interval=config.candle_interval, max_records=config.max_records))
        try:
            self._candles.start()
        except Exception as e:
            self.logger().warning(f"PUMP: candles start failed: {str(e)[:120]}")

    # ---------- события исполнения ----------

    def did_complete_buy_order(self, event):
        try:
            self._filled_orders.add(event.order_id)
            self._save_state()
            self.logger().info(f"PUMP fill: {event.order_id}")
        except Exception:
            pass

    def did_complete_sell_order(self, event):
        try:
            self._filled_orders.add(event.order_id)
            self._save_state()
            self.logger().info(f"PUMP fill: {event.order_id}")
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
                    filled = data.pop("_filled", None)
                    if isinstance(filled, list):
                        self._filled_orders.update(str(x) for x in filled[-500:])
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

    # ---------- переключатель из панели ----------

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

    # ---------- цены/ордера ----------

    def _price(self, conn_name, is_buy):
        conn = self.connectors.get(conn_name)
        if conn is None:
            return None
        try:
            p = conn.get_price(self.config.trading_pair, is_buy)
            return float(p) if p and float(p) > 0 else None
        except Exception:
            return None

    def _last_price(self, conn_name):
        conn = self.connectors.get(conn_name)
        if conn is None:
            return None
        try:
            p = conn.get_last_traded_price(self.config.trading_pair)
            return float(p) if p and float(p) > 0 else None
        except Exception:
            return None

    def _submit(self, conn_name, side, amount, price, maker, position_action=None):
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
            self.logger().warning(f"PUMP: budget check failed: {str(e)[:110]}")
            return None
        if adjusted.amount <= 0:
            self.logger().warning(f"PUMP: insufficient balance for {side}")
            return None
        try:
            if side == "BUY":
                return self.buy(conn_name, self.config.trading_pair, adjusted.amount,
                                order_type, price=price_dec, position_action=position_action)
            return self.sell(conn_name, self.config.trading_pair, adjusted.amount,
                             order_type, price=price_dec, position_action=position_action)
        except Exception as e:
            self.logger().warning(f"PUMP: order failed: {str(e)[:110]}")
            return None

    def _order_done(self, order_id):
        return bool(order_id) and order_id in self._filled_orders

    # ---------- детекция ----------

    def _atr(self):
        """ATR по последним свечам (в цене). None, если данных мало."""
        try:
            df = self._candles.candles_df
            n = self.config.atr_length
            if df is None or len(df) < n + 2:
                return None
            tail = df.tail(n + 1)
            highs = [float(x) for x in tail["high"].tolist()]
            lows = [float(x) for x in tail["low"].tolist()]
            closes = [float(x) for x in tail["close"].tolist()]
            trs = []
            for i in range(1, len(tail)):
                tr = max(highs[i] - lows[i],
                         abs(highs[i] - closes[i - 1]),
                         abs(lows[i] - closes[i - 1]))
                trs.append(tr)
            return sum(trs) / len(trs)
        except Exception:
            return None

    def _detect(self):
        """(dir, reason) либо None. dir: +1 памп вверх, -1 вниз."""
        try:
            if not getattr(self._candles, "ready", False):
                return None
            df = self._candles.candles_df
            vp, mult, thr = (self.config.volume_period, self.config.volume_multiplier,
                             self.config.price_move_threshold_pct)
            if df is None or len(df) < vp + 2:
                return None
            last = df.iloc[-1]
            vols = [float(x) for x in df["volume"].tolist()[-(vp + 1):-1]]
            vol_ma = sum(vols) / len(vols)
            vol = float(last["volume"])
            prev_close = float(df.iloc[-2]["close"])
            close = float(last["close"])
            move_pct = (close - prev_close) / prev_close * 100.0 if prev_close else 0.0
            spike = vol_ma > 0 and vol > vol_ma * mult
            if not spike:
                return None
            if abs(move_pct) < thr:
                return None
            direction = 1 if move_pct > 0 else -1
            # Направление свечи должно совпадать с движением цены.
            if direction == 1 and not (float(last["close"]) >= float(last["open"])):
                return None
            if direction == -1 and not (float(last["close"]) < float(last["open"])):
                return None
            return direction, f"vol x{vol / vol_ma:.1f} move {move_pct:+.2f}%"
        except Exception as e:
            self.logger().warning(f"PUMP detect error: {str(e)[:120]}")
            return None

    def _confirm(self, direction):
        """Обе биржи сдвинулись в том же направлении за окно (свой код)."""
        now = time.time()
        win = self.config.confirm_window_sec
        moves = []
        for name in (self.config.entry_connector, self.config.confirm_connector):
            price = self._last_price(name)
            dq = self._price_window[name]
            if price:
                dq.append((now, price))
            while dq and now - dq[0][0] > win:
                dq.popleft()
            if len(dq) < 2:
                return False
            base = dq[0][1]
            delta_pct = (dq[-1][1] - base) / base * 100.0
            moves.append(delta_pct)
        need = self.config.confirm_min_move_pct
        for d in moves:
            if direction == 1 and d < need:
                return False
            if direction == -1 and d > -need:
                return False
        return True

    def _spread_ok(self):
        conn = self.connectors.get(self.config.entry_connector)
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
        return (ask - bid) / mid * 10000.0 <= self.config.max_spread_bps

    # ---------- позиция (одна нога, перп) ----------

    def _open_position(self, direction, reason, now):
        side = "BUY" if direction == 1 else "SELL"
        price = self._price(self.config.entry_connector, direction == 1)
        if price is None:
            return
        quote = float(self.config.order_quote)
        amount = quote / price
        atr = self._atr()
        if not atr or atr <= 0:
            self.logger().info("PUMP: нет ATR — вход отложен")
            return
        order_id = self._submit(self.config.entry_connector, side, amount, price,
                                maker=True, position_action="OPEN")
        if not order_id:
            return
        self._state["position"] = {
            "opened_at": now,
            "side": side,
            "direction": direction,
            "amount": amount,
            "notional": quote,
            "entry_price": price,
            "sl_price": price - self.config.sl_atr_mult * atr if direction == 1
                        else price + self.config.sl_atr_mult * atr,
            "tp_price": price + self.config.tp_atr_mult * atr if direction == 1
                        else price - self.config.tp_atr_mult * atr,
            "atr": atr,
            "leg": "pending",
            "entry_order_id": order_id,
            "entry_order_since": now,
            "reason": reason,
        }
        self._save_state()
        self.logger().info(
            f"PUMP open ({side} {amount:.8f} @ {price}, {reason}) "
            f"sl={self._state['position']['sl_price']:.1f} "
            f"tp={self._state['position']['tp_price']:.1f} order={order_id}")

    def _close_position(self, pos, reason):
        """Начать закрытие: market-выход + подтверждение событием."""
        side = "SELL" if pos.get("side") == "BUY" else "BUY"
        price = self._price(self.config.entry_connector, side == "BUY")
        if price is None:
            self.logger().warning("PUMP: цена закрытия недоступна — ретрай")
            return
        oid = self._submit(self.config.entry_connector, side, pos.get("amount"),
                           price, maker=False, position_action="CLOSE")
        if not oid:
            self.logger().warning("PUMP: выход не отправился — ретрай")
            return
        pos["closing"] = {"reason": reason, "since": time.time(), "exit": oid}
        self._save_state()
        self.logger().info(f"PUMP close → {reason}")

    def _finalize_close(self, pos, reason):
        entry = float(pos.get("entry_price") or 0)
        exit_px = float(pos.get("last_exit_price") or 0)
        pnl = 0.0
        amt = float(pos.get("amount") or 0)
        if entry > 0 and exit_px > 0:
            diff = (exit_px - entry) * amt
            pnl = diff if pos.get("side") == "BUY" else -diff
        self._state["realized_pnl"] = round(
            float(self._state.get("realized_pnl") or 0.0) + pnl, 8)
        self._state["closed"] = int(self._state.get("closed") or 0) + 1
        self._state["last_close"] = {
            "ts": time.time(), "reason": reason, "pnl_quote": round(pnl, 4),
            "unconfirmed": bool((pos.get("closing") or {}).get("unconfirmed"))}
        self._state["position"] = None
        self._save_state()
        self.logger().info(f"PUMP closed ({reason}): pnl={pnl:+.4f} USDT")

    def _manage_close(self, pos, now):
        closing = pos.get("closing")
        if not isinstance(closing, dict):
            self._finalize_close(pos, "некорректное состояние закрытия")
            return
        exit_id = closing.get("exit")
        if exit_id and self._order_done(exit_id):
            self._finalize_close(pos, closing.get("reason") or "закрыто")
            return
        if now - float(closing.get("since") or now) > self.config.fill_timeout_sec:
            self.logger().warning(
                "PUMP: выход не подтвердился за "
                f"{self.config.fill_timeout_sec}s — закрываю состояние, проверь остаток!")
            closing["unconfirmed"] = True
            self._finalize_close(pos, closing.get("reason") or "закрыто (без подтверждения)")

    def _manage_position(self, pos, now):
        if pos.get("leg") == "pending":
            if self._order_done(pos.get("entry_order_id")):
                pos["leg"] = "open"
                pos.pop("entry_order_id", None)
                self._save_state()
                self.logger().info("PUMP position open")
                return
            if now - float(pos.get("entry_order_since") or 0) > self.config.fill_timeout_sec:
                self.logger().warning("PUMP: вход не подтвердился — отмена")
                self._finalize_close(pos, "вход не исполнился")
                return
            return
        # Открытая позиция: стоп/тейк/время/авария.
        is_long = pos.get("side") == "BUY"
        price = self._price(self.config.entry_connector, not is_long)  # цена выхода
        if price is None:
            return
        sl = float(pos.get("sl_price") or 0)
        tp = float(pos.get("tp_price") or 0)
        if is_long:
            if sl and price <= sl:
                self._close_position(pos, f"стоп {price:.1f} <= {sl:.1f}")
                return
            if tp and price >= tp:
                self._close_position(pos, f"тейк {price:.1f} >= {tp:.1f}")
                return
        else:
            if sl and price >= sl:
                self._close_position(pos, f"стоп {price:.1f} >= {sl:.1f}")
                return
            if tp and price <= tp:
                self._close_position(pos, f"тейк {price:.1f} <= {tp:.1f}")
                return
        entry = float(pos.get("entry_price") or 0)
        if entry > 0:
            pnl_pct = (price - entry) / entry * 100.0 * (1 if is_long else -1)
            if pnl_pct <= -float(self.config.emergency_loss_pct):
                self._close_position(pos, f"аварийный стоп {pnl_pct:+.2f}%")
                return
        if now - float(pos.get("opened_at") or now) > self.config.max_hold_sec:
            self._close_position(pos, f"time force-close ({int(now - pos.get('opened_at', now))}s)")

    # ---------- цикл ----------

    def tick(self, timestamp: float):
        self._last_timestamp = timestamp
        need = [self.config.entry_connector, self.config.confirm_connector]
        ready = [n for n in need if getattr(self.connectors.get(n), "ready", False)]
        if len(ready) < len(need):
            missing = [n for n in need if n not in ready]
            now = time.time()
            if now - float(getattr(self, "_last_missing_log", 0)) > 60:
                self._last_missing_log = now
                self.logger().warning(f"waiting for connectors: {missing}")
            return
        if not self.ready_to_trade:
            self.ready_to_trade = True
            self.logger().info(f"pump trading with {need}")
        self.on_tick()

    def on_tick(self):
        now = self.current_timestamp
        control = self._control_state()
        pos = self._state.get("position")
        at_poll = now - self._last_poll_ts >= self.config.poll_interval_sec
        if at_poll:
            self._last_poll_ts = now

        requested = control.get("close_all_requested")
        if requested and requested != self._close_all_done:
            self._close_all_closed = 0
            if pos and not pos.get("closing"):
                self._close_position(pos, "close-all по кнопке")
                self._close_all_closed += 1
            self._close_all_done = requested
            self._ack_close_all(requested)
            return

        if pos:
            if pos.get("closing"):
                self._manage_close(pos, now)
            else:
                self._manage_position(pos, now)
            if at_poll or now - float(getattr(self, "_last_publish_ts", 0)) > 10:
                self._last_publish_ts = now
                self._publish_state(
                    "running" if control.get("trading_enabled") else "paused")
            return

        if not at_poll:
            return
        if not control.get("trading_enabled"):
            self._publish_state("paused")
            return
        self._publish_state("running")
        self._maybe_entry(now)

    def _maybe_entry(self, now):
        if not getattr(self._candles, "ready", False):
            stamp = time.time()
            if stamp - float(getattr(self, "_warmup_log", 0)) > 300:
                self._warmup_log = stamp
                try:
                    left = self._candles._candles.maxlen - len(self._candles._candles)
                except Exception:
                    left = "?"
                self.logger().info(f"PUMP: свечи прогреваются (осталось {left} баров)")
            return
        if now - self._last_entry_ts < self.config.cooldown_sec:
            return
        found = self._detect()
        if found is None:
            return
        direction, reason = found
        if not self._confirm(direction):
            if getattr(self, "_last_reject_log", 0) < now - 300:
                self._last_reject_log = now
                self.logger().info(f"PUMP: без подтверждения второй биржей ({reason}) — пропуск")
            return
        if not self._spread_ok():
            self.logger().info("PUMP: спред шире лимита — пропуск")
            return
        self._last_entry_ts = now
        self._open_position(direction, reason, now)

    def on_stop(self):
        try:
            self._candles.stop()
        except Exception:
            pass

    # ---------- публикация для панели ----------

    def _publish_state(self, mode):
        try:
            pos = self._state.get("position")
            payload = {
                "mode": mode,
                "trading_enabled": mode == "running",
                "updated_at": time.time(),
                "realized_pnl": round(float(self._state.get("realized_pnl") or 0.0), 8),
                "closed": int(self._state.get("closed") or 0),
                "position": None,
            }
            if pos:
                payload["position"] = {
                    "pair": self.config.trading_pair,
                    "side": pos.get("side"),
                    "amount": pos.get("amount"),
                    "entry_price": pos.get("entry_price"),
                    "sl_price": pos.get("sl_price"),
                    "tp_price": pos.get("tp_price"),
                    "legs": [pos.get("leg"), "closing" if pos.get("closing") else None],
                    "opened_at": pos.get("opened_at"),
                    "reason": pos.get("reason"),
                }
            path = self.config.publish_file
            for cand in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
                try:
                    os.makedirs(os.path.dirname(cand), exist_ok=True)
                    tmp = cand + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False)
                        f.flush()
                    os.replace(tmp, cand)
                    return
                except Exception:
                    continue
        except Exception:
            pass
