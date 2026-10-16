# -*- coding: utf-8 -*-
"""Тесты защит арбитража. Запуск: python tests/test_arbitrage_guard.py

Проверяются все семь ловушек из брифа: VWAP вместо Top of Book, комиссионный
порог, спуфинг, глубина, лимиты API, закрытый кошелёк, перекос и хедж.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from arbitrage_guard import (  # noqa: E402
    BPS, DepthGate, LegRiskGuard, RateLimiter, RebalanceEconomics, RoutePolicy,
    RouteRegistry, SpreadConfirmation, check_depth, evaluate_route, vwap_for_side,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_vwap_beats_top_of_book():
    print("1. Иллюзия ликвидности: VWAP, а не Top of Book")
    asks = [[100.0, 0.01], [101.0, 0.01], [150.0, 10.0]]
    small = vwap_for_side(asks, "buy", 0.02)
    check("мелкий ордер берётся по первым уровням", small.ok and abs(small.price - 100.5) < 1e-9,
          f"got {small.price}")
    check("проскальзывание ненулевое", small.slippage_bps > 0, f"{small.slippage_bps}")

    big = vwap_for_side(asks, "buy", 5.0)
    check("крупный ордер уходит вглубь и дорожает", big.price > 120, f"got {big.price}")
    check("проскальзывание крупного ордера заметное", big.slippage_bps > 100,
          f"{big.slippage_bps:.0f}bps")

    shallow = vwap_for_side([[100.0, 0.001]], "buy", 1.0)
    check("нехватка глубины отвергается", not shallow.ok and "depth" in shallow.reason,
          shallow.reason)


def test_fee_threshold():
    print("2. Комиссионный порог 0.35–0.45% против двойного тейкера")
    buy = vwap_for_side([[100.0, 1.0]], "buy", 0.1)
    # спред 0.5% = 50bps, комиссии 10+10 = 20bps -> net 30bps < 45bps -> отказ
    sell_cheap_edge = vwap_for_side([[100.5, 1.0]], "sell", 0.1)
    res = evaluate_route(buy, sell_cheap_edge, 10, 10, min_net_bps=45, max_slippage_bps=50)
    check("30bps не проходит порог 45bps", not res.ok, f"net={res.net_bps:.0f}")

    # спред 1.0% = 100bps -> net 80bps -> проходит
    sell_wide = vwap_for_side([[101.0, 1.0]], "sell", 0.1)
    res2 = evaluate_route(buy, sell_wide, 10, 10, min_net_bps=45, max_slippage_bps=50)
    check("80bps проходит порог", res2.ok, f"net={res2.net_bps:.0f}")
    check("издержки посчитаны верно", abs(res2.fees_bps - 20) < 1e-9, f"{res2.fees_bps}")

    # проскальзывание убивает сделку даже при большом номинале спреда
    sell_deep = vwap_for_side([[101.0, 0.02], [140.0, 1.0]], "sell", 1.0)
    res3 = evaluate_route(vwap_for_side([[100.0, 0.02], [60.0, 1.0]], "buy", 1.0),
                          sell_deep, 10, 10, min_net_bps=45, max_slippage_bps=20)
    check("проскальзывание выше лимита отвергает сделку", not res3.ok, res3.reason)


def test_spoofing_confirmation():
    print("3. Спуфинг: спред должен продержаться")
    conf = SpreadConfirmation(required=3)
    for _ in range(3):
        conf.observe("bybit->okx", 60.0)
    check("три одинаковых опроса подтвердили спред", conf.confirmed("bybit->okx", 60.0))

    conf.reset("bybit->okx")
    conf.observe("bybit->okx", 300.0)
    check("после сброса нужен новый стрик", not conf.confirmed("bybit->okx", 300.0))

    for _ in range(3):
        conf.observe("flappy", 50.0)
    check("прыгающий спред не подтверждается", not conf.confirmed("flappy", 900.0))


def test_depth_gate():
    print("4. Глубина стакана")
    gate = DepthGate(min_depth_usd=200.0, max_top_share=0.6)
    thin = [[100.0, 0.5]]
    ok, why, depth = check_depth(gate, "buy", thin, 0.5)
    check("тонкий стакан отвергается", not ok and "below" in why, why)
    check("глубина посчитана верно", abs(depth - 50.0) < 1e-9, f"{depth}")

    # Реалистичный стакан: верхний уровень тонкий, ниже — объём растёт.
    ladder = [[100.0, 0.5], [100.1, 1.0], [100.2, 2.0], [100.3, 5.0]]
    ok2, why2, depth2 = check_depth(gate, "buy", ladder, 0.2)
    check("толстый стакан проходит", ok2, why2)

    # Ордер, съедающий больше 60% верхнего уровня, блокируется даже в толстом стакане.
    ok3, why3, _ = check_depth(gate, "buy", ladder, 1.0)
    check("огромный ордер относительно верхнего уровня блокируется", not ok3, why3)


def test_rate_limiter():
    print("5. Асимметрия лимитов API")
    now = 1_000.0
    rl = RateLimiter({"okx": 100, "gate_io": 10}, default_per_minute=30)
    allowed = all(rl.allow("gate_io", now + i * 0.01) for i in range(10))
    check("бюджет малой биржи исчерпан корректно", allowed)
    check("11-й запрос gate_io запрещён", not rl.allow("gate_io", now + 0.2))
    check("OKX в это время свободен", rl.allow("okx", now + 0.2))

    waited = rl.penalize("gate_io", now + 1.0)
    check("после 429 биржа блокируется", waited > 0, f"{waited:.0f}s")
    check("во время блокировки запросы не проходят", not rl.allow("gate_io", now + 2.0))
    check("после истечения блокировки снова доступна", rl.allow("gate_io", now + 200.0))


def test_wallet_maintenance():
    print("6. Закрытый кошелёк: вывод монеты выключен")
    reg = RouteRegistry([
        RoutePolicy("bybit->okx", "bybit", "okx", "BTC-USDT"),
        RoutePolicy("bybit->gate", "bybit", "gate_io", "BTC-USDT",
                    sell_withdrawal_enabled=False),
    ])
    ok, why = reg.tradable("bybit->okx")
    check("обычный маршрут торгуется", ok, why)
    ok2, why2 = reg.tradable("bybit->gate")
    check("маршрут с выключенным выводом заблокирован", not ok2 and "withdrawal" in why2, why2)

    snap = {s["route_id"]: s for s in reg.snapshot()}
    check("снимок показывает блокировку", snap["bybit->gate"]["tradable"] is False)


def test_network_and_rebalance():
    print("7. Несовпадение сетей и кассовый разрыв")
    guard = RebalanceEconomics(max_skew_usd=10.0, withdraw_fee_usd=25.0)
    same = RoutePolicy("a", "bybit", "okx", "BTC-USDT", asset_network=("btc", "btc"))
    diff = RoutePolicy("b", "bybit", "gate", "BTC-USDT", asset_network=("btc", "wbtc"))

    ok, why = guard.rebalance_possible(same)
    check("одинаковые сети — ребалансировка возможна", ok, why)
    ok2, why2 = guard.rebalance_possible(diff)
    check("разные сети — ребалансировка невозможна", not ok2 and "mismatch" in why2, why2)

    poor = guard.economics("a", buy_side_quote=15.0, sell_side_quote=5.0, earned_usd=15.0)
    check("15 USD спреда против 25 USD вывода — убыточно", not poor["worth_rebalancing"])
    check("перекос зафиксирован", abs(poor["skew_usd"] - 10.0) < 1e-9, f"{poor['skew_usd']}")
    rich = guard.economics("a", buy_side_quote=11.0, sell_side_quote=9.0, earned_usd=60.0)
    check("60 USD спреда перекрывает вывод", rich["worth_rebalancing"])


def test_leg_risk():
    print("8. Риск одной ноги и хедж")
    guard = LegRiskGuard(timeout_seconds=2.0)
    now = time.time()
    guard.track("bybit->okx", "ord-1", "bybit", "BUY", 0.5, now)
    check("до дедлайна хедж не нужен", guard.check("bybit->okx", other_filled=False, now=now + 0.5) is None)
    check("после дедлайна нужен хедж",
          guard.check("bybit->okx", other_filled=False, now=now + 2.5) == "hedge")
    check("событие одной ноги записано", guard.one_leg_count == 1)
    guard.clear("bybit->okx")
    check("после закрытия обеих ног состояние чистое", not guard.legs)


if __name__ == "__main__":
    for fn in (test_vwap_beats_top_of_book, test_fee_threshold, test_spoofing_confirmation,
               test_depth_gate, test_rate_limiter, test_wallet_maintenance,
               test_network_and_rebalance, test_leg_risk):
        fn()
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("Все проверки защит пройдены.")