import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

import candle_pattern as cp


def frame_from(bars, start=1_795_670_400_000):
    """bars: list of (open, high, low, close). One row per minute."""
    start = start // cp.MINUTE * cp.MINUTE
    rows = []
    for index, (o, h, l, c) in enumerate(bars):
        rows.append([start + index * cp.MINUTE, o, h, l, c, 100, 1000])
    return cp.clean_candles(rows, start + len(bars) * cp.MINUTE)


def red_run(count=5, price=100.0):
    bars = []
    for i in range(count):
        o = price - i * 0.1
        bars.append((o, o + 0.02, o - 0.12, o - 0.1))
    return bars


def green_run(count=5, price=100.0):
    bars = []
    for i in range(count):
        o = price + i * 0.1
        bars.append((o, o + 0.12, o - 0.02, o + 0.1))
    return bars


class CandlePatternTests(unittest.TestCase):
    def test_clean_candles_rejects_non_minute(self):
        rows = [[1_795_670_430_000, 1, 1, 1, 1, 1, 1]]  # 30s offset
        with self.assertRaises(ValueError):
            cp.clean_candles(rows, 1_795_670_430_000 + cp.MINUTE * 2)

    def test_clean_candles_drops_unfinished(self):
        # now is mid-bar: the bar has not closed yet and must be dropped.
        rows = [[1_795_670_400_000, 100, 100.5, 99.5, 100.2, 100, 1000]]
        frame = cp.clean_candles(rows, 1_795_670_400_000 + 30_000)
        self.assertEqual(len(frame), 0)
        # at exactly the close the bar is complete and kept.
        frame = cp.clean_candles(rows, 1_795_670_400_000 + cp.MINUTE)
        self.assertEqual(len(frame), 1)

    def test_detect_long_after_five_red(self):
        frame = frame_from(red_run() + [(99.5, 99.6, 99.4, 99.55)])
        self.assertEqual(cp.detect_signal(frame, 5), "LONG")

    def test_detect_short_after_five_green(self):
        frame = frame_from(green_run() + [(100.5, 100.6, 100.4, 100.55)])
        self.assertEqual(cp.detect_signal(frame, 5), "SHORT")

    def test_mixed_run_no_signal(self):
        bars = red_run(3) + green_run(2) + [(100, 100.1, 99.9, 100.05)]
        frame = frame_from(bars)
        self.assertIsNone(cp.detect_signal(frame, 5))

    def test_doji_breaks_run(self):
        bars = red_run(4) + [(99.0, 99.05, 98.95, 99.0)] + [(98.9, 99.0, 98.8, 98.85)]
        frame = frame_from(bars)
        self.assertIsNone(cp.detect_signal(frame, 5))

    def test_no_lookahead(self):
        bars = red_run(5) + [(99.5, 99.6, 99.4, 99.55)]
        frame = frame_from(bars)
        expected = cp.detect_signal(frame, 5)
        changed = frame.copy()
        changed.loc[5, ["open", "high", "low", "close"]] = [1, 2, 0.5, 1.5]
        self.assertEqual(cp.detect_signal(changed, 5), expected)

    def test_open_sets_roi_based_levels(self):
        state = cp.fresh_state()
        self.assertTrue(cp.open_position(state, "KAITOUSDT", "LONG", 100.0,
                                         cp.MINUTE, leverage=10.0, margin_pct=0.2))
        pos = state["position"]
        entry = 100.0 * (1 + cp.SLIPPAGE)
        self.assertAlmostEqual(pos["entry"], entry)
        self.assertAlmostEqual(pos["target"], entry * (1 + 0.15 / 10.0))
        self.assertAlmostEqual(pos["stop"], entry * (1 - 0.50 / 10.0))
        self.assertAlmostEqual(pos["quantity"] * entry, 20.0 * 0.2 * 10.0)
        self.assertAlmostEqual(state["balance"], 20.0 - pos["entry_fee"])

    def test_take_profit_and_stop(self):
        state = cp.fresh_state()
        cp.open_position(state, "KAITOUSDT", "LONG", 100.0, 0, leverage=10.0, margin_pct=0.2)
        pos = state["position"]
        cp.manage_bar(state, {"time": cp.MINUTE, "open": 100.5,
                              "high": pos["target"] + 0.01, "low": 100.2})
        self.assertIsNone(state["position"])
        self.assertEqual(state["history"][0]["reason"], "TAKE PROFIT")
        self.assertGreater(state["history"][0]["net_pnl"], 0)

    def test_stop_wins_when_both_touched(self):
        state = cp.fresh_state()
        cp.open_position(state, "KAITOUSDT", "LONG", 100.0, 0, leverage=10.0, margin_pct=0.2)
        pos = state["position"]
        cp.manage_bar(state, {"time": cp.MINUTE, "open": 100.0,
                              "high": pos["target"] + 1, "low": pos["stop"] - 0.01})
        self.assertEqual(state["history"][0]["reason"], "STOP LOSS")
        self.assertLess(state["history"][0]["net_pnl"], 0)

    def test_gap_through_stop_fills_at_open(self):
        state = cp.fresh_state()
        cp.open_position(state, "KAITOUSDT", "LONG", 100.0, 0, leverage=10.0, margin_pct=0.2)
        pos = state["position"]
        cp.manage_bar(state, {"time": cp.MINUTE, "open": pos["stop"] - 1,
                              "high": pos["stop"] - 0.5, "low": pos["stop"] - 2})
        trade = state["history"][0]
        self.assertEqual(trade["reason"], "STOP GAP")
        self.assertLess(trade["exit"], pos["stop"])

    def test_run_cycle_replays_and_closes(self):
        entry_bar = (99.5, 100.0, 99.4, 99.55)       # signal bar: 5 red before it
        tp_bar = (99.55, 102.0, 99.5, 101.9)         # target ~1.5% above entry
        frame = frame_from(red_run() + [entry_bar, tp_bar])
        state, report, _ = cp.run_cycle(cp.fresh_state(), frame, "KAITOUSDT", 10.0, 0.2)
        self.assertIsNone(state["position"])
        self.assertEqual(len(state["history"]), 1)
        trade = state["history"][0]
        self.assertEqual(trade["side"], "LONG")
        self.assertEqual(trade["reason"], "TAKE PROFIT")
        self.assertEqual(report["closed_trades"], 1)
        self.assertGreater(report["net_realized_pnl"], 0)

    def test_run_cycle_is_idempotent(self):
        bars = red_run() + [(99.5, 100.0, 99.4, 99.55), (99.55, 102.0, 99.5, 101.9)]
        frame = frame_from(bars)
        first, _, _ = cp.run_cycle(cp.fresh_state(), frame, "KAITOUSDT", 10.0, 0.2)
        second, report, _ = cp.run_cycle(first, frame, "KAITOUSDT", 10.0, 0.2)
        self.assertEqual(len(second["history"]), 1)
        self.assertEqual(report["closed_trades"], 1)

    def test_validate_state_rejects_bad(self):
        with self.assertRaises(ValueError):
            cp.validate_state({"balance": -1, "history": [], "processed_until": 0})
        with self.assertRaises(ValueError):
            cp.validate_state({"balance": "x", "history": [], "processed_until": 0})

    def test_state_roundtrip(self):
        state = cp.fresh_state()
        cp.open_position(state, "KAITOUSDT", "SHORT", 50.0, cp.MINUTE,
                         leverage=10.0, margin_pct=0.2)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            cp.save_state(state, path)
            loaded = cp.load_state(path)
        self.assertEqual(loaded["position"]["side"], "SHORT")
        json.dumps(state, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
