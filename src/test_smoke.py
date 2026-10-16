# -*- coding: utf-8 -*-
"""Smoke tests for sensor/analyst/price.
Run: venv/Scripts/python src/test_smoke.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_sensor_yes_index():
    from sensor import parse_markets
    ev = {"markets": [
        {"question": "Q1", "outcomes": '["Yes","No"]',
         "outcomePrices": '["0.805","0.195"]', "lastTradePrice": "0.8"},
        {"question": "Q2", "outcomes": '["No","Yes"]',
         "outcomePrices": '["0.2","0.8"]', "lastTradePrice": "0.8"},
    ]}
    ms = parse_markets(ev)
    assert abs(ms[0]["yes_prob"] - 0.805) < 1e-6, ms[0]
    assert abs(ms[1]["yes_prob"] - 0.8) < 1e-6, ms[1]
    print("OK sensor yes-index")

def test_no_spike_on_first_run():
    from analyst import detect_spikes
    assert detect_spikes([], [{"question": "Q", "yes_prob": 0.99}]) == []
    print("OK first-run quiet")

def test_spike_detected():
    from analyst import detect_spikes
    prev = [{"question": "Q", "yes_prob": 0.35}]
    cur = [{"question": "Q", "yes_prob": 0.70}]
    sp = detect_spikes(prev, cur, 0.10)
    assert len(sp) == 1 and abs(sp[0][3] - 0.35) < 1e-6, sp
    print("OK spike detection")

def test_resolved_skipped():
    from analyst import detect_spikes
    prev = [{"question": "Q", "yes_prob": 0.5}]
    cur = [{"question": "Q", "yes_prob": 1.0, "_resolved": True}]
    assert detect_spikes(prev, cur, 0.10) == []
    print("OK resolved skipped")

def test_price_sources():
    from price import get_btc_price
    p, s = get_btc_price()
    assert 10000 < p < 1000000, p
    print(f"OK price {p} via {s}")

if __name__ == "__main__":
    test_sensor_yes_index()
    test_no_spike_on_first_run()
    test_spike_detected()
    test_resolved_skipped()
    test_price_sources()
    print("ALL TESTS PASSED")
