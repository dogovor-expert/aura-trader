# -*- coding: utf-8 -*-
"""V2 script-стратегия Hummingbot: читает сигналы Polymarket-оракула.

Файл сигналов пишет D:\\polymarket-oracle\\src\\bridge.py:
  D:/Hummingbot/signals.json          (для Windows)
  D:/Hummingbot/data/signals.json     (маунт контейнера -> /home/hummingbot/data/signals.json)

Длинный/короткий сигнал с conf >= min_confidence исполняется как MARKET-ордер
на paper-коннекторе (по умолчанию binance_paper_trade). Повтор одного и того же
сигнала отсекается по полю ts, между ордерами — cooldown_sec.
"""
import json
import os
from decimal import Decimal
from typing import Dict, List

from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import MarketDict
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class SignalPollerConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    controllers_config: List[str] = []
    connector_name: str = Field(default="binance_paper_trade")
    trading_pair: str = Field(default="BTC-USDT")
    signals_file: str = Field(default="/home/hummingbot/data/signals.json")
    min_confidence: float = Field(default=0.75)
    order_quote: Decimal = Field(default=Decimal("100"))
    cooldown_sec: int = Field(default=1800)
    poll_interval_sec: int = Field(default=15)
    allow_test_signals: bool = Field(default=False)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.connector_name] = markets.get(self.connector_name, set()) | {self.trading_pair}
        return markets


class SignalPoller(StrategyV2Base):
    def __init__(self, connectors: Dict[str, ConnectorBase], config: SignalPollerConfig):
        super().__init__(connectors, config)
        self.config = config
        self._last_poll_ts = 0.0
        self._seen_signal_ts = None
        self._last_order_ts = 0.0
        self._last_verdict = "n/a"

    def _read_signals(self) -> dict:
        try:
            with open(self.config.signals_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.logger().warning(f"signals file unreadable: {str(e)[:120]}")
            return {}

    def on_tick(self):
        now = self.current_timestamp
        if now - self._last_poll_ts < self.config.poll_interval_sec:
            return
        self._last_poll_ts = now

        data = self._read_signals()
        if not data:
            return

        sig_ts = data.get("ts")
        sig = data.get("signal") or {}
        if sig_ts is None or sig_ts == self._seen_signal_ts:
            return  # этот сигнал уже обработан
        self._seen_signal_ts = sig_ts

        final = str(sig.get("final") or "").upper()
        try:
            conf = float(sig.get("conf") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        is_test = bool(sig.get("test"))
        self._last_verdict = f"{final} conf={conf}" + (" (test)" if is_test else "")
        self.logger().info(
            f"signal {sig_ts}: {self._last_verdict} btc={sig.get('btc')}")

        if final not in ("LONG", "SHORT"):
            return  # WAIT — ничего не исполняем
        if is_test and not self.config.allow_test_signals:
            self.logger().info("test signal skipped (allow_test_signals=false)")
            return
        if conf < self.config.min_confidence:
            self.logger().info(
                f"confidence {conf} < {self.config.min_confidence} — skipped")
            return
        if now - self._last_order_ts < self.config.cooldown_sec:
            self.logger().info(
                f"cooldown active ({int(now - self._last_order_ts)}s of "
                f"{self.config.cooldown_sec}s) — skipped")
            return

        connector = self.connectors.get(self.config.connector_name)
        if connector is None or not connector.ready:
            self.logger().warning(f"connector {self.config.connector_name} not ready")
            return

        is_buy = final == "LONG"
        try:
            price = connector.get_price(self.config.trading_pair, is_buy)
        except Exception as e:
            self.logger().warning(f"price unavailable: {str(e)[:120]}")
            return
        if price is None or price <= 0:
            return

        amount = (self.config.order_quote / price)
        side = TradeType.BUY if is_buy else TradeType.SELL
        candidate = OrderCandidate(
            trading_pair=self.config.trading_pair,
            is_maker=False,
            order_type=OrderType.MARKET,
            order_side=side,
            amount=amount,
            price=price,
        )
        try:
            adjusted = connector.budget_checker.adjust_candidate(candidate, all_or_none=False)
        except Exception as e:
            self.logger().warning(f"budget check failed: {str(e)[:120]}")
            return
        if adjusted.amount <= 0:
            self.logger().warning("insufficient balance for order (paper)")
            return

        if is_buy:
            order_id = self.buy(self.config.connector_name, self.config.trading_pair,
                                adjusted.amount, OrderType.MARKET, price=price)
        else:
            order_id = self.sell(self.config.connector_name, self.config.trading_pair,
                                 adjusted.amount, OrderType.MARKET, price=price)
        self._last_order_ts = now
        self.logger().info(
            f"{'BUY' if is_buy else 'SELL'} {adjusted.amount} "
            f"{self.config.trading_pair} @~{price} order_id={order_id}")

    def format_status(self) -> str:
        lines = [
            f"Signal poller | last signal: {self._seen_signal_ts}",
            f"last verdict: {self._last_verdict}",
            f"connector: {self.config.connector_name} | pair: {self.config.trading_pair} | "
            f"order_quote: {self.config.order_quote} USDT | min_conf: {self.config.min_confidence}",
        ]
        return "\n".join(lines)

