# -*- coding: utf-8 -*-
"""Multi-Exchange Signal Poller Strategy для Hummingbot V2.

Читает сигналы от Polymarket-оракула и исполняет ордера на ЛУЧШЕЙ доступной бирже.

Критерии выбора биржи (в порядке приоритета):
1. Готовность коннектора (ready=True)
2. Наличие торговой пары
3. Ликвидность (минимальный спред)
3. Комиссия (maker/taker)
4. Наличие достаточного баланса

Поддерживаемые типы коннекторов:
- Spot: binance, okx, bybit, kraken, kucoin, gate_io, etc.
- Perpetual: binance_perpetual, okx_perpetual, bybit_perpetual, etc.
- Testnet: bybit_testnet, bybit_perpetual_testnet, okx_perpetual_testnet, etc.

Конфигурация бирж через connector_configs (список конфигов в conf/connectors/)
"""
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from pydantic import Field, ConfigDict, BaseModel

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class ExchangeConfig(BaseModel):
    """Конфигурация одной биржи."""
    model_config = ConfigDict(extra='allow')
    name: str = Field(description="Имя коннектора")
    trading_pairs: List[str] = Field(default_factory=list)
    priority: int = Field(default=100)
    enabled: bool = Field(default=True)
    min_spread_bps: float = Field(default=50.0)
    max_position_quote: Decimal = Field(default=Decimal("500"))
    is_perpetual: bool = Field(default=False)


class MultiExchangeSignalPollerConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = []
    
    signals_file: str = Field(default="/home/hummingbot/data/signals.json")
    min_confidence: float = Field(default=0.75)
    order_quote: Decimal = Field(default=Decimal("100"))
    cooldown_sec: int = Field(default=1800)
    # Выходы: без них позиции копятся вечно и прибыль не считается.
    max_hold_minutes: int = Field(default=360)
    trades_file: str = Field(default="/home/hummingbot/data/trades.json")
    poll_interval_sec: int = Field(default=15)
    allow_test_signals: bool = Field(default=False)
    # Свежесть сигнала: файл, пролежавший дольше этого времени (например,
    # восстановленный из бэкапа старый сигнал), не исполняется.
    signal_ttl_sec: int = Field(default=1800)
    # Дедупликация переживает рестарт: иначе последний сигнал из файла
    # исполнялся бы заново при каждом перезапуске стратегии.
    state_file: str = Field(default="/home/hummingbot/data/signal_state.json")

    # Боевой режим и предохранители. control_file — файл-переключатель,
    # который кнопка «Торговля» в панели пишет извне: система продолжает
    # следить за рынком, но ордера выставляет только когда trading_enabled.
    control_file: str = Field(default="/home/hummingbot/data/trading_control.json")
    emergency_loss_pct: float = Field(default=3.0)      # стоп всего портфеля
    default_sl_pct: float = Field(default=2.0)       # если сигнал без стопа
    default_tp_pct: float = Field(default=3.5)       # если сигнал без тейка
    min_free_quote: Decimal = Field(default=Decimal("0"))
    max_daily_loss_quote: Decimal = Field(default=Decimal("20"))
    
    exchanges: List[ExchangeConfig] = Field(default_factory=list)
    max_total_exposure_quote: Decimal = Field(default=Decimal("5000"))
    max_concurrent_orders: int = Field(default=3)

    # Allow extra fields from YAML
    model_config = ConfigDict(extra='allow', arbitrary_types_allowed=True)
    
    def update_markets(self, markets):
        for exch in (self.exchanges or []):
            name = exch.name if hasattr(exch, "name") else exch.get("name") if isinstance(exch, dict) else None
            pairs = exch.trading_pairs if hasattr(exch, "trading_pairs") else exch.get("trading_pairs", []) if isinstance(exch, dict) else []
            enabled = exch.enabled if hasattr(exch, "enabled") else exch.get("enabled", True) if isinstance(exch, dict) else True
            if not name or not enabled:
                continue
            for pair in pairs:
                markets[name] = markets.get(name, set()) | {pair}
        return markets


class MultiExchangeSignalPoller(StrategyV2Base):
    def __init__(self, connectors, config):
        super().__init__(connectors, config)
        self.config = config
        self._last_poll_ts = 0.0
        self._seen_signal_ts = None
        self._positions = []
        self._closed_trades = []
        self._last_conf, self._last_sl, self._last_tp = 0.0, 0.0, 0.0
        self._load_trades()
        self._last_order_ts = 0.0
        self._close_all_done = None
        self._close_all_closed = 0
        self._active_orders = {}
        self._exchange_metrics = {}
        self._load_state()
        
    def _read_signals(self):
        try:
            with open(self.config.signals_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.logger().warning(f"signals file unreadable: {str(e)[:120]}")
            return {}
    def _exch_get(self, exch, key, default=None):
        if hasattr(exch, key):
            return getattr(exch, key)
        if isinstance(exch, dict):
            return exch.get(key, default)
        return default

    def _update_exchange_metrics(self):
        self._exchange_metrics = {}
        for exch in (self.config.exchanges or []):
            name = self._exch_get(exch, "name")
            if not name:
                continue
            if not self._exch_get(exch, "enabled", True):
                continue
            trading_pairs = self._exch_get(exch, "trading_pairs", []) or []
            connector = self.connectors.get(name)
            if connector is None:
                self._exchange_metrics[name] = {"ready": False, "reason": "connector not found"}
                continue
            if not connector.ready:
                self._exchange_metrics[name] = {"ready": False, "reason": "not ready"}
                continue

            available_pairs = []
            for pair in trading_pairs:
                if pair in connector.trading_pairs:
                    available_pairs.append(pair)

            if not available_pairs:
                self._exchange_metrics[name] = {"ready": False, "reason": "no trading pairs"}
                continue

            pair = available_pairs[0]
            try:
                bid = connector.get_price(pair, False)
                ask = connector.get_price(pair, True)
                if bid and ask and ask > 0:
                    spread_bps = ((ask - bid) / ask) * 10000
                else:
                    spread_bps = 9999
            except Exception:
                spread_bps = 9999

            try:
                fee = connector.get_fee(pair, True, Decimal('100'), TradeType.BUY, OrderType.MARKET)
                fee_bps = float(fee.percent * 10000) if fee else 10.0
            except Exception:
                fee_bps = 10.0

            try:
                quote_currency = trading_pairs[0].split('-')[1] if '-' in trading_pairs[0] else "USDT"
                balance = connector.get_balance(quote_currency)
                available = connector.get_available_balance(quote_currency)
            except Exception:
                balance = Decimal('0')
                available = Decimal('0')

            self._exchange_metrics[name] = {
                "ready": True,
                "spread_bps": spread_bps,
                "fee_bps": fee_bps,
                "balance": balance,
                "available": available,
                "pairs": available_pairs,
                "priority": self._exch_get(exch, "priority", 100),
                "config": exch,
            }
    def _exposure_on(self, exchange_name):
        """Текущая экспозиция (в quote) по одной бирже."""
        return sum(abs(float(o.get("amount_quote", 0))) for o in self._active_orders.values()
                   if o.get("exchange") == exchange_name)

    def _total_exposure(self):
        return sum(abs(float(o.get("amount_quote", 0))) for o in self._active_orders.values())

    def _select_exchanges(self, limit=None):
        """Все пригодные биржи, лучшие — первыми.

        Раньше выбиралась одна биржа, поэтому остальные восемь молчали.
        Теперь сигнал исполняется на всех подходящих биржах сразу, но не
        больше max_concurrent_orders и в пределах лимитов экспозиции:
        общий max_total_exposure_quote и per-exchange max_position_quote.
        """
        self._update_exchange_metrics()
        limit = self.config.max_concurrent_orders if limit is None else limit
        slots = max(0, int(limit) - len(self._active_orders))
        order_quote = float(self.config.order_quote)

        candidates = []
        for name, metrics in self._exchange_metrics.items():
            if not metrics.get("ready"):
                continue
            cfg = metrics["config"]
            if metrics["spread_bps"] > self._exch_get(cfg, "min_spread_bps", 50.0):
                continue
            if metrics["available"] < self.config.order_quote:
                continue
            pair = metrics["pairs"][0] if metrics["pairs"] else None
            if not pair:
                continue
            per_exchange_cap = float(self._exch_get(cfg, "max_position_quote", 0) or 0)
            if per_exchange_cap and self._exposure_on(name) + order_quote > per_exchange_cap:
                continue
            if self._total_exposure() + order_quote > float(self.config.max_total_exposure_quote):
                continue
            score = (float(self._exch_get(cfg, "priority", 100)) * 1000.0
                     + float(metrics["spread_bps"]) * 10.0
                     + float(metrics["fee_bps"]) * 5.0)
            candidates.append((score, name, cfg, pair))

        candidates.sort(key=lambda x: x[0])
        return [(name, cfg, pair) for _, name, cfg, pair in candidates[:slots]]

    def _select_best_exchange(self, signal_final, signal_conf):
        chosen = self._select_exchanges(limit=1)
        if not chosen:
            return None
        name, cfg, pair = chosen[0]
        return name, cfg, pair
    
    def _ack_close_all(self, requested: str):
        """Снимает флаг после выполнения: иначе при рестарте стратегия
        закрыла бы позиции, открытые уже ПОСЛЕ этого закрытия."""
        path = getattr(self.config, "control_file", None)
        if not path:
            return
        for candidate in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(candidate, encoding="utf-8") as f:
                    data = json.load(f)
                data.pop("close_all_requested", None)
                data.pop("close_all_reason", None)
                data["close_all_done"] = requested
                tmp = candidate + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, candidate)
                return
            except Exception:
                continue

    def _control_state(self):
        """Читает файл-переключатель, который правит кнопка в панели.

        Формат: {"trading_enabled": true, "updated_at": "...", "note": "..."}
        Отсутствие файла = торговля выключена (безопасное состояние по умолчанию).
        """
        path = getattr(self.config, "control_file", None)
        if not path:
            return {"trading_enabled": False}
        for candidate in (path, path.replace("/home/hummingbot/", "D:/Hummingbot/")):
            try:
                with open(candidate, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {"trading_enabled": False}
            except Exception:
                continue
        return {"trading_enabled": False}

    def _trading_enabled(self):
        return bool(self._control_state().get("trading_enabled", False))

    def _manage_open_positions(self):
        """Выходы по стоп-лоссу, тейку и времени. Без этого позиции копятся
        вечно, прибыль не считается и уверенность модели нечем калибровать."""
        now = self.current_timestamp
        open_pos = [p for p in self._positions if p.get("status") == "open"]

        # Кнопка «закрыть всё»: работает независимо от торгового переключателя,
        # потому что выход из позиции нужен и при остановленной торговле.
        control = self._control_state()
        requested = control.get("close_all_requested")
        if requested and requested != self._close_all_done:
            self._close_all_closed = 0
            if open_pos:
                self.logger().warning(f"CLOSE ALL requested ({control.get('close_all_reason', '')[:60]}) "
                                      f"-> closing {len(open_pos)} position(s)")
                for p in list(open_pos):
                    try:
                        if self._close_position(p, now, "close-all по кнопке"):
                            self._close_all_closed += 1
                    except Exception as e:
                        self.logger().warning(f"close-all failed on {p.get('exchange')}: {str(e)[:90]}")
                self.logger().info(f"CLOSE ALL done: {self._close_all_closed} position(s) closed")
            else:
                self.logger().info("CLOSE ALL: открытых позиций нет")
            self._close_all_done = requested
            self._ack_close_all(requested)
            self._publish_state("paused" if not self._trading_enabled() else "running")

        # Аварийный тормоз: портфель ушёл за общий стоп — закрываем всё.
        limit = float(getattr(self.config, "emergency_loss_pct", 0) or 0)
        if limit > 0 and open_pos:
            total = sum(float(p.get("pnl_quote") or 0.0) for p in open_pos)
            base = sum(float(p.get("entry_price", 0)) * float(p.get("amount", 0)) for p in open_pos)
            if base > 0 and (total / base * 100.0) <= -limit:
                self.logger().warning(f"EMERGENCY: portfolio loss {total / base * 100:.2f}% "
                                      f"<= -{limit:.2f}% — closing everything")
                for p in open_pos:
                    try:
                        self._close_position(p, now, f"emergency portfolio stop {limit}%")
                    except Exception as e:
                        self.logger().warning(f"emergency close failed: {str(e)[:100]}")
                return

        for pos in list(open_pos):
            ex, pair = pos["exchange"], pos["pair"]
            conn = self.connectors.get(ex)
            if conn is None or not conn.ready:
                continue
            try:
                price = conn.get_price(pair, True)   # цена продажи (bid)
            except Exception:
                continue
            if price is None or price <= 0:
                continue
            entry = float(pos["entry_price"])
            pnl_pct = (float(price) - entry) / entry * 100.0
            pos["last_price"] = float(price)
            pos["pnl_pct"] = round(pnl_pct, 3)

            sl = float(pos.get("sl_pct") or 0) or None
            tp = float(pos.get("tp_pct") or 0) or None
            age_min = (now - float(pos["ts"])) / 60.0
            reason = None
            if sl and pnl_pct <= -sl:
                reason = f"stop-loss {pnl_pct:.2f}% (порог -{sl:.2f}%)"
            elif tp and pnl_pct >= tp:
                reason = f"take-profit {pnl_pct:.2f}% (порог +{tp:.2f}%)"
            elif age_min >= float(getattr(self.config, "max_hold_minutes", 360)):
                reason = f"time exit after {age_min:.0f} min ({pnl_pct:+.2f}%)"
            if not reason:
                continue
            self._close_position(pos, now, reason, price)

    def _close_position(self, pos, now, reason, price=None):
        """Закрытие позиции рыночным ордером + запись в журнал сделок."""
        ex, pair = pos["exchange"], pos["pair"]
        conn = self.connectors.get(ex)
        if price is None:
            if conn is None:
                return False
            try:
                price = conn.get_price(pair, True)
            except Exception:
                return False
        if not price or float(price) <= 0:
            return False
        price = float(price)
        entry = float(pos["entry_price"])
        amount = float(pos["amount"])
        try:
            if pos.get("side") == "BUY":
                oid = self.sell(ex, pair, Decimal(str(round(amount, 8))),
                                OrderType.MARKET, price=Decimal(str(price)))
            else:
                oid = self.buy(ex, pair, Decimal(str(round(amount, 8))),
                               OrderType.MARKET, price=Decimal(str(price)))
        except Exception as e:
            self.logger().warning(f"exit failed on {ex}: {str(e)[:120]}")
            return False

        pnl_quote = (price - entry) * amount if pos.get("side") == "BUY" else (entry - price) * amount
        pos.update({"status": "closed", "close_price": price,
                    "close_ts": now, "reason": reason,
                    "pnl_quote": round(pnl_quote, 6), "order_id": oid})
        self._closed_trades.append({k: pos.get(k) for k in
                                    ("exchange", "pair", "side", "amount", "entry_price",
                                     "close_price", "pnl_pct", "pnl_quote", "reason",
                                     "conf", "sl_pct", "tp_pct", "ts", "close_ts")})
        del self._closed_trades[:-60]
        self._write_trades()
        self.logger().info(f"EXIT {pos['side']} {pair} on {ex} @~{price} -> {reason}")
        return True

    def _publish_state(self, mode: str):
        """Публикует живое состояние для панели: режим торговли, позиции, P&L."""
        try:
            path = getattr(self.config, "trades_file", "/home/hummingbot/data/trades.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"trades": [], "open": 0}
        open_pos = [p for p in self._positions if p.get("status") == "open"]
        unrealized = sum(float(p.get("pnl_quote") or 0.0) for p in open_pos)
        realized = sum(float(t.get("pnl_quote") or 0.0) for t in (data.get("trades") or []))
        payload = {
            "mode": mode,
            "trading_enabled": mode == "running",
            "updated_at": time.time(),
            "open_positions": [
                {"exchange": p.get("exchange"), "pair": p.get("pair"), "side": p.get("side"),
                 "amount": p.get("amount"), "entry_price": p.get("entry_price"),
                 "last_price": p.get("last_price"), "pnl_pct": p.get("pnl_pct"),
                 "sl_pct": p.get("sl_pct"), "tp_pct": p.get("tp_pct"), "conf": p.get("conf"),
                 "age_min": round((self.current_timestamp - float(p.get("ts", 0))) / 60.0, 1)}
                for p in open_pos],
            "realized_pnl": round(realized, 8),
            "unrealized_pnl": round(unrealized, 8),
            "close_all_done": self._close_all_done,
            "close_all_closed": self._close_all_closed,
        }
        try:
            with open(path.replace("/home/hummingbot/data/trades.json",
                                   "/home/hummingbot/data/trading_state.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            self.logger().warning(f"state publish failed: {str(e)[:80]}")

    def _write_trades(self):
        """Журнал сделок + открытых позиций.

        Открытые позиции сохраняются тоже: иначе после перезапуска контейнера
        они остались бы без стоп-лосса и без учёта в P&L — незащищённая
        экспозиция на реальных деньгах.
        """
        path = getattr(self.config, "trades_file", "/home/hummingbot/data/trades.json")
        open_positions = [p for p in self._positions if p.get("status") == "open"]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"updated_at": datetime.now().isoformat(timespec="seconds"),
                           "open": len(open_positions),
                           "open_positions": open_positions,
                           "trades": self._closed_trades[-60:]}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger().warning(f"trades write failed: {str(e)[:120]}")

    def _load_trades(self):
        """Восстанавливает закрытые сделки и ОТКРЫТЫЕ позиции после рестарта."""
        path = getattr(self.config, "trades_file", "/home/hummingbot/data/trades.json")
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._closed_trades = data.get("trades", [])[-60:]
        except Exception:
            self._closed_trades = []
        restored = 0
        for pos in data.get("open_positions") or []:
            if isinstance(pos, dict) and pos.get("status") == "open" and pos.get("exchange") and pos.get("pair"):
                self._positions.append(pos)
                restored += 1
        if restored:
            self.logger().info(f"restored {restored} open position(s) after restart")

    def _state_path(self):
        # Стратегия исполняется в Linux-контейнере: путь из конфига — уже
        # путь контейнера (/home/hummingbot/...), каталог смонтирован на хост.
        return getattr(self.config, "state_file", "/home/hummingbot/data/signal_state.json")

    def _load_state(self):
        """Восстанавливает дедупликацию сигналов после рестарта.

        Без этого последний LONG/SHORT из signals.json исполнялся бы снова
        при каждом перезапуске, а кулдаун обнулялся.
        """
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                data = json.load(f)
            self._seen_signal_ts = data.get("seen_signal_ts") or self._seen_signal_ts
            ts = data.get("last_order_ts")
            if ts:
                self._last_order_ts = float(ts)
            if self._seen_signal_ts:
                self.logger().info(f"restored signal state: last seen {self._seen_signal_ts}, "
                                   f"last order ts {self._last_order_ts}")
        except Exception:
            pass

    def _save_state(self):
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen_signal_ts": self._seen_signal_ts,
                           "last_order_ts": self._last_order_ts}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            self.logger().warning(f"state save failed: {str(e)[:80]}")

    def _signal_age_sec(self):
        """Возраст файла сигнала в секундах (по mtime маунта — без проблем
        с часовыми поясами контейнера и хоста)."""
        try:
            return max(0.0, time.time() - os.path.getmtime(self.config.signals_file))
        except Exception:
            return None

    def _daily_realized_pnl(self):
        """Реализованный P&L за текущие сутки (UTC, по epoch закрытия)."""
        now = self.current_timestamp
        day_start = now - (now % 86400)
        total = 0.0
        for t in self._closed_trades:
            try:
                if float(t.get("close_ts") or 0) >= day_start:
                    total += float(t.get("pnl_quote") or 0.0)
            except (TypeError, ValueError):
                continue
        return total

    def _cleanup_completed_orders(self):
        to_remove = []
        for order_id, info in self._active_orders.items():
            try:
                connector = self.connectors[info["exchange"]]
                order = connector.get_order(order_id)
                if order and order.is_done:
                    to_remove.append(order_id)
                    self.logger().info(f"order {order_id} on {info['exchange']} completed: {order.executed_amount_base} @ {order.average_executed_price}")
            except Exception:
                pass
        for oid in to_remove:
            del self._active_orders[oid]
    def tick(self, timestamp: float):
        """Не ждём готовности всех бирж.

        Базовая реализация держит стратегию на паузе, пока не отвечает
        КАЖДЫЙ коннектор. Одна медленная биржа (kucoin, okx) останавливала
        торговлю целиком. Здесь считаем готовыми ответившие; неготовые
        отсекаются в _select_exchanges, где проверяется metrics["ready"].
        """
        self._last_timestamp = timestamp
        ready = [c for c in self.connectors.values() if c.ready]
        if not ready:
            self.logger().warning("no connector ready yet")
            return
        if not self.ready_to_trade:
            self.ready_to_trade = True
            self.logger().info(f"trading with {len(ready)}/{len(self.connectors)} ready connectors")
        self.on_tick()

    def on_tick(self):
        now = self.current_timestamp
        if now - self._last_poll_ts < self.config.poll_interval_sec:
            return
        self._last_poll_ts = now
        
        self._cleanup_completed_orders()
        self._manage_open_positions()

        # Торговля включается кнопкой в панели: система продолжает читать
        # сигналы и вести позиции, но новые ордера не выставляет.
        if not self._trading_enabled():
            self._publish_state("paused")
            data = self._read_signals()
            if data and data.get("ts") and data["ts"] != self._seen_signal_ts:
                self._seen_signal_ts = data["ts"]
                self._save_state()
                sig = data.get("signal", data)
                self.logger().info(
                    f"signal {data['ts']} {sig.get('final')} conf={sig.get('conf')} — trading disabled, skipped")
            return
        self._publish_state("running")

        data = self._read_signals()
        if not data:
            return

        # Устаревший сигнал (из бэкапа или залежавшийся) не исполняем: его
        # цена уже неактуальна, а вход по нему — это вход вслепую.
        age = self._signal_age_sec()
        if age is not None and age > self.config.signal_ttl_sec:
            if data.get("ts") and data["ts"] != self._seen_signal_ts:
                self._seen_signal_ts = data["ts"]
                self._save_state()
                self.logger().info(f"stale signal ignored (age {int(age)}s > ttl "
                                   f"{self.config.signal_ttl_sec}s)")
            return

        sig_ts = data.get("ts")
        sig = data.get("signal") or {}
        if sig_ts is None or sig_ts == self._seen_signal_ts:
            return
        self._seen_signal_ts = sig_ts
        self._save_state()
        
        final = str(sig.get("final") or "").upper()
        try:
            conf = float(sig.get("conf") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        is_test = bool(sig.get("test"))
        # Волатильностно-адаптивный размер: множитель приходит от оракула
        # (src/sizing.py). Сырой размер из конфига — потолок, а не факт.
        try:
            size_mult = float(sig.get("size_mult") or 1.0)
        except (TypeError, ValueError):
            size_mult = 1.0
        size_mult = max(0.05, min(1.0, size_mult))
        # Сохраняем для позиции: по ней потом считаются выходы и калибровка.
        def _num(key, default=0.0):
            try:
                return float(sig.get(key) or default)
            except (TypeError, ValueError):
                return default
        self._last_conf = conf
        self._last_sl = _num("stop_loss_pct")
        self._last_tp = _num("take_profit_pct")
        # Страховка: сигнал может прийти без риск-плана (например, из
        # восстановленного файла). Без стоп-лосса позиция осталась бы
        # незащищённой — на реальных деньгах это недопустимо.
        if self._last_sl <= 0:
            self._last_sl = float(getattr(self.config, "default_sl_pct", 2.0) or 2.0)
        if self._last_tp <= 0:
            self._last_tp = float(getattr(self.config, "default_tp_pct", 3.5) or 3.5)
        self._last_verdict = f"{final} conf={conf}" + (" (test)" if is_test else "")
        self.logger().info(f"signal {sig_ts}: {self._last_verdict} btc={sig.get('btc')} "
                           f"size_mult={size_mult} sl={sig.get('stop_loss_pct')} "
                           f"tp={sig.get('take_profit_pct')}")
        
        if final not in ("LONG", "SHORT"):
            return
        if is_test and not self.config.allow_test_signals:
            self.logger().info("test signal skipped")
            return
        if conf < self.config.min_confidence:
            self.logger().info(f"confidence {conf} < {self.config.min_confidence} — skipped")
            return
        if now - self._last_order_ts < self.config.cooldown_sec:
            self.logger().info(f"cooldown active ({int(now - self._last_order_ts)}s) — skipped")
            return
        # Дневной лимит убытка: kill-switch. Без него серия убыточных сделок
        # за день не ограничена ничем.
        dl_limit = float(getattr(self.config, "max_daily_loss_quote", 0) or 0)
        if dl_limit > 0:
            daily = self._daily_realized_pnl()
            if daily <= -dl_limit:
                self.logger().warning(f"daily loss limit reached ({daily:.4f} <= -{dl_limit}) "
                                      f"— new entries blocked until next UTC day")
                return
        if len(self._active_orders) >= self.config.max_concurrent_orders:
            self.logger().info("max concurrent orders reached — skipped")
            return
        
        selections = self._select_exchanges()
        if not selections:
            self.logger().info("no suitable exchange available — skipped")
            return

        placed = 0
        for exch_name, exch_cfg, pair in selections:
            if self._place_order(exch_name, exch_cfg, pair, final, conf, now, size_mult):
                placed += 1
        if placed:
            self.logger().info(f"signal executed on {placed} exchange(s): "
                               + ", ".join(s[0] for s in selections[:placed]))

    def _place_order(self, exch_name, exch_cfg, pair, final, conf, now, size_mult=1.0):
        """Один market-ордер на одной бирже. True, если ордер выставлен."""
        connector = self.connectors[exch_name]
        is_buy = final == "LONG"
        try:
            price = connector.get_price(pair, is_buy)
        except Exception as e:
            self.logger().warning(f"price unavailable on {exch_name}: {str(e)[:120]}")
            return False
        if price is None or price <= 0:
            return False

        quote = float(self.config.order_quote) * max(0.05, min(1.0, size_mult or 1.0))
        # Цена из коннектора приходит Decimal, а коннекторы смешивают типы
        # (float * Decimal -> TypeError). Внутри OrderCandidate нужны обе
        # величины одного типа, поэтому приводим всё к Decimal.
        price_dec = Decimal(str(price))
        amount = Decimal(str(round(quote, 8))) / price_dec
        side = TradeType.BUY if is_buy else TradeType.SELL
        candidate = OrderCandidate(
            trading_pair=pair,
            is_maker=False,
            order_type=OrderType.MARKET,
            order_side=side,
            amount=amount,
            price=price_dec,
        )
        try:
            adjusted = connector.budget_checker.adjust_candidate(candidate, all_or_none=False)
        except Exception as e:
            self.logger().warning(f"budget check failed on {exch_name}: {str(e)[:120]}")
            return False
        if adjusted.amount <= 0:
            self.logger().warning(f"insufficient balance for order on {exch_name}")
            return False

        position_action = None
        if self._exch_get(exch_cfg, "is_perpetual", False):
            position_action = "OPEN"

        try:
            if is_buy:
                order_id = self.buy(exch_name, pair, adjusted.amount, OrderType.MARKET,
                                    price=price_dec, position_action=position_action)
            else:
                order_id = self.sell(exch_name, pair, adjusted.amount, OrderType.MARKET,
                                     price=price_dec, position_action=position_action)
        except Exception as e:
            self.logger().warning(f"order failed on {exch_name}: {str(e)[:120]}")
            return False

        self._last_order_ts = now
        self._save_state()
        self._active_orders[order_id] = {
            "exchange": exch_name,
            "pair": pair,
            "side": "BUY" if is_buy else "SELL",
            "amount": adjusted.amount,
            "amount_quote": quote,
            "price": price,
            "ts": now,
            "signal_conf": conf,
        }
        self.logger().info(
            f"{'BUY' if is_buy else 'SELL'} {adjusted.amount} {pair} @~{price} on {exch_name} order_id={order_id}")
        # Позиция для выходов: стоп, тейк и уверенность сигнала едут вместе с ней.
        self._positions.append({
            "side": "BUY" if is_buy else "SELL",
            "exchange": exch_name, "pair": pair,
            "amount": float(adjusted.amount), "entry_price": float(price),
            "ts": self.current_timestamp,
            "conf": self._last_conf, "sl_pct": self._last_sl, "tp_pct": self._last_tp,
            "status": "open",
        })
        self._write_trades()
        return True
    def format_status(self):
        lines = [
            f"Multi-Exchange Signal Poller | last signal: {self._seen_signal_ts}",
            f"last verdict: {getattr(self, '_last_verdict', 'n/a')}",
            f"active orders: {len(self._active_orders)} / {self.config.max_concurrent_orders}",
            f"total exposure: {sum(o.get('amount_quote',0) for o in self._active_orders.values())} / {self.config.max_total_exposure_quote} USDT",
            "",
        ]
        for name, metrics in self._exchange_metrics.items():
            status = "✓" if metrics.get("ready") else "✗"
            if metrics.get("ready"):
                lines.append(f"  {status} {name}: spread={metrics['spread_bps']:.1f}bps fee={metrics['fee_bps']:.1f}bps avail={metrics['available']:.2f} pairs={metrics['pairs']}")
            else:
                reason = metrics.get("reason", "")
                lines.append(f"  {status} {name}: {reason}")
        if self._active_orders:
            lines.append("\nActive orders:")
            for oid, info in self._active_orders.items():
                lines.append(f"  {oid[:12]}: {info['side']} {info['amount']:.6f} {info['pair']} @{info['price']:.2f} on {info['exchange']}")
        return "\n".join(lines)
