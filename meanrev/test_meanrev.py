import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402

H = bot.HOUR


def frame(closes, start=0):
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * 1.001, min(prev, c) * 0.999
        rows.append([start + i * H, prev, hi, lo, c, 100, 1000])
        prev = c
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "quote"])


def choppy_then_drop():
    base = [100 + (0.3 if i % 2 else -0.3) for i in range(80)]
    return base + [99.0, 98.0, 97.0, 96.0, 95.0, 94.0]


class MeanRevTests(unittest.TestCase):
    def test_no_signal_in_quiet_market(self):
        f = bot.prepare(frame([100 + (0.3 if i % 2 else -0.3) for i in range(80)]))
        side, _, _ = bot.analyze(f.iloc[-1])
        self.assertIsNone(side)

    def test_long_signal_after_sharp_drop_in_chop(self):
        f = bot.prepare(frame(choppy_then_drop()))
        old = bot.ER_MAX
        bot.ER_MAX = 1.0
        try:
            side, _, _ = bot.analyze(f.iloc[-1])
        finally:
            bot.ER_MAX = old
        self.assertEqual(side, "LONG")

    def test_trending_market_is_skipped(self):
        f = bot.prepare(frame([100 + i * 0.5 for i in range(80)] + [100 + 79 * 0.5 - 6]))
        side, _, why = bot.analyze(f.iloc[-1])
        self.assertIsNone(side)

    def test_accounting_charges_fees_once(self):
        state = bot.fresh_state()
        pos = {"symbol": "X", "side": "LONG", "entry": 100.0, "stop": 95.0, "target": 105.0,
               "quantity": 1.0, "entry_fee": 0.06, "entry_time": H, "fee_rate": 0.0006, "slippage_rate": 0.0}
        state["balance"] -= 0.06
        state["positions"].append(pos)
        bar = pd.Series({"time": 2 * H, "open": 101.0, "high": 106.0, "low": 100.5, "close": 105.0})
        bot.manage_bar(state, "X", bar)
        self.assertEqual(state["positions"], [])
        trade = state["history"][0]
        self.assertAlmostEqual(trade["net_pnl"], 5.0 - 0.063 - 0.06, places=6)
        self.assertAlmostEqual(state["balance"], 20.0 + trade["net_pnl"], places=6)

    def test_stop_wins_when_both_touched(self):
        state = bot.fresh_state()
        state["positions"].append({"symbol": "X", "side": "SHORT", "entry": 100.0, "stop": 104.0,
            "target": 96.0, "quantity": 1.0, "entry_fee": 0, "entry_time": H, "fee_rate": 0, "slippage_rate": 0})
        bot.manage_bar(state, "X", pd.Series({"time": 2 * H, "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0}))
        self.assertEqual(state["history"][0]["reason"], "STOP LOSS")

    def test_time_exit(self):
        state = bot.fresh_state()
        state["positions"].append({"symbol": "X", "side": "LONG", "entry": 100.0, "stop": 90.0,
            "target": 110.0, "quantity": 1.0, "entry_fee": 0, "entry_time": 0, "fee_rate": 0, "slippage_rate": 0})
        bar = pd.Series({"time": (bot.MAX_HOLD_BARS - 1) * H, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5})
        bot.manage_bar(state, "X", bar)
        self.assertEqual(state["history"][0]["reason"], "TIME EXIT")

    def test_bad_state_rejected(self):
        with self.assertRaises(ValueError):
            bot.validate_state({"balance": float("nan"), "positions": [], "history": []})

    def test_only_public_market_endpoints(self):
        with self.assertRaises(ValueError):
            bot.market_get("/api/v2/mix/order/place-order", {})

    def test_state_is_separate_from_trend_bot(self):
        self.assertTrue(bot.STATE_FILE.endswith("meanrev/paper_state.json"))

    def test_run_cycle_does_not_mutate_input(self):
        state = bot.fresh_state()
        frames = {"X": frame(choppy_then_drop())}
        before = repr(state)
        bot.run_cycle(state, frames)
        self.assertEqual(repr(state), before)


if __name__ == "__main__":
    unittest.main()
