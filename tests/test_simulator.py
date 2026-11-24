import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import main
from backtest import evaluate, load_csv


def candles(count=180, start=1_700_002_800_000):
    start = start // main.HOUR * main.HOUR
    rows = []
    for index in range(count):
        price = 100 + index * 0.02 + (index % 9) * 0.1
        rows.append([start + index * main.HOUR, price, price + 0.6,
                     price - 0.6, price + 0.1, 100 + index % 4, 10000])
    return main.clean_candles(rows, start + count * main.HOUR)


def position(**changes):
    result = {"symbol": "BTCUSDT", "side": "LONG", "entry": 100.0,
        "stop": 99.0, "target": 102.0, "quantity": 1.0,
        "entry_fee": 0.06, "entry_time": main.HOUR,
        "fee_rate": 0.0006, "slippage_rate": 0.0005, "strategy_version": 5}
    result.update(changes)
    return result


class SimulatorTests(unittest.TestCase):
    def test_completed_bars_and_sort(self):
        frame = candles(5)
        rows = frame.values.tolist()[::-1]
        result = main.clean_candles(rows, int(frame.iloc[-1]["time"]))
        self.assertEqual(len(result), 4)
        self.assertTrue(result["time"].is_monotonic_increasing)

    def test_invalid_values_rejected(self):
        rows = candles(1).values.tolist()
        rows[0][4] = float("inf")
        with self.assertRaises(ValueError):
            main.clean_candles(rows)

    def test_flat_and_rising_rsi(self):
        frame = candles()
        frame[["open", "high", "low", "close"]] = 100.0
        self.assertEqual(main.prepare(frame).iloc[-1]["rsi"], 50)
        frame["close"] = list(range(100, 100 + len(frame)))
        self.assertEqual(main.prepare(frame).iloc[-1]["rsi"], 100)

    def test_indicators_have_no_future_leak(self):
        frame = candles()
        expected = main.prepare(frame).iloc[100]
        frame.loc[101:, "close"] *= 4
        actual = main.prepare(frame).iloc[100]
        for key in ("ema20", "ema50", "rsi", "macd", "atr", "bb_upper", "volume_ratio"):
            self.assertAlmostEqual(actual[key], expected[key])

    def test_stop_before_target(self):
        state = main.fresh_state()
        state["balance"] -= 0.06
        state["positions"] = [position()]
        main.manage_bar(state, "BTCUSDT", {"time": main.HOUR, "open": 100, "high": 103, "low": 98})
        self.assertEqual(state["history"][0]["reason"], "STOP LOSS")
        self.assertLess(state["history"][0]["exit"], 99)
        self.assertAlmostEqual(state["balance"] - 20, state["history"][0]["net_pnl"])

    def test_gap_loss_and_short(self):
        state = main.fresh_state()
        state["positions"] = [position()]
        main.manage_bar(state, "BTCUSDT", {"time": main.HOUR, "open": 95, "high": 97, "low": 94})
        self.assertEqual(state["history"][0]["reason"], "STOP GAP")
        self.assertLess(state["history"][0]["exit"], 95)
        state["positions"] = [position(side="SHORT", stop=101, target=98)]
        main.manage_bar(state, "BTCUSDT", {"time": main.HOUR, "open": 100, "high": 102, "low": 97})
        self.assertGreater(state["history"][-1]["exit"], 101)

    def test_atomic_save_and_invalid_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            main.save_state(main.fresh_state(), path)
            before = path.read_bytes()
            state = main.fresh_state()
            state["balance"] = float("nan")
            with self.assertRaises(ValueError):
                main.save_state(state, path)
            self.assertEqual(path.read_bytes(), before)
            path.write_text("{bad json")
            with self.assertRaises(json.JSONDecodeError):
                main.load_state(path)

    def test_existing_state_compatible_and_unchanged(self):
        path = Path(__file__).parent / "fixtures" / "legacy_state.json"
        original = path.read_bytes()
        state = main.load_state(path)
        self.assertEqual(len(state["positions"]), 3)
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "copy.json"
            main.save_state(state, dest)
            self.assertEqual(main.load_state(dest), state)
        self.assertEqual(path.read_bytes(), original)

    def test_repeated_run_is_idempotent(self):
        state = main.fresh_state()
        updated, _ = main.run_cycle(state, {"BTCUSDT": candles()})
        again, _ = main.run_cycle(updated, {"BTCUSDT": candles()})
        self.assertEqual(updated, again)
        self.assertEqual(state, main.fresh_state())

    def test_old_position_without_coverage_preserved(self):
        state = main.fresh_state()
        state["positions"] = [position(entry_time=main.HOUR)]
        updated, report = main.run_cycle(state, {"BTCUSDT": candles()})
        self.assertEqual(updated["positions"], state["positions"])
        self.assertTrue(report["warnings"])

    def test_missing_checkpoint_bars_preserved(self):
        frame = candles()
        stamp = int(frame.iloc[70]["time"])
        state = main.fresh_state()
        state["positions"] = [position(entry_time=stamp)]
        state["processed_until"] = {"BTCUSDT": stamp}
        updated, report = main.run_cycle(state, {"BTCUSDT": frame.iloc[80:].reset_index(drop=True)})
        self.assertEqual(updated["positions"], state["positions"])
        self.assertTrue(report["warnings"])

    def test_legacy_history_not_rewritten(self):
        state = main.fresh_state()
        state["history"] = [{"net_pnl": 2}]
        report = main.metrics(state, [20, 18, 21])
        self.assertEqual(report["legacy_trades_excluded"], 1)
        self.assertEqual(report["closed_trades"], 0)
        self.assertAlmostEqual(report["max_drawdown_pct"], 10)

    def test_position_size_and_duplicate_signal(self):
        state = main.fresh_state()
        row = main.prepare(candles()).iloc[-1]
        with patch("main.analyze", return_value=("LONG", 5, "test")):
            self.assertTrue(main.open_position(state, "BTCUSDT", row, 100, int(row["time"]) + main.HOUR))
            self.assertFalse(main.open_position(state, "BTCUSDT", row, 100, int(row["time"]) + main.HOUR))
        self.assertLessEqual(state["positions"][0]["quantity"] * state["positions"][0]["entry"], 20)

    def test_public_endpoint_allowlist(self):
        with self.assertRaises(ValueError):
            main.market_get("/api/v2/mix/order/place-order", {})

    def test_backtest_deterministic_and_no_state_mutation(self):
        frame = main.prepare(candles())
        before = frame.copy(deep=True)
        result = evaluate({"BTCUSDT": frame})
        self.assertEqual(evaluate({"BTCUSDT": frame}), result)
        pd.testing.assert_frame_equal(before, frame)
        self.assertEqual(result["holdout"]["open_positions"], 0)
        self.assertIn("equal_weight_buy_hold_return_pct", result["holdout"])

    def test_backtest_gaps_and_future_rejected(self):
        with self.assertRaises(ValueError):
            evaluate({"BTCUSDT": main.prepare(candles().drop(index=90))})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "BTC.csv"
            frame = candles()
            frame["time"] += 10**14
            frame.to_csv(path, index=False)
            with self.assertRaises(ValueError):
                load_csv(path)

    def test_legacy_position_never_exits_on_signal_bar(self):
        state = main.fresh_state()
        pos = position()
        pos.pop("strategy_version")
        state["positions"] = [pos]
        main.manage_bar(state, "BTCUSDT", {"time": main.HOUR, "open": 100, "high": 103, "low": 98})
        self.assertEqual(state["positions"], [pos])
        self.assertFalse(state["history"])

    def test_backtest_fees_and_entry_lag(self):
        import backtest
        frame = main.prepare(candles())
        start, end = int(frame.iloc[60]["time"]), int(frame.iloc[90]["time"])
        original = backtest.open_position
        seen = []
        def check(state, symbol, previous, price, stamp, fee, slippage):
            seen.append((int(previous["time"]), stamp))
            return original(state, symbol, previous, price, stamp, fee, slippage)
        with patch("backtest.open_position", side_effect=check), patch("main.analyze", return_value=("LONG", 5, "test")):
            charged = backtest.simulate({"BTCUSDT": frame}, start, end)
        with patch("main.analyze", return_value=("LONG", 5, "test")):
            free = backtest.simulate({"BTCUSDT": frame}, start, end, 0, 0)
        self.assertTrue(seen)
        self.assertTrue(all(stamp - previous == main.HOUR for previous, stamp in seen))
        self.assertGreater(charged["closed_trades"], 0)
        self.assertLess(charged["balance"], free["balance"])

    def test_api_error_does_not_become_empty_data(self):
        response = unittest.mock.Mock()
        response.json.return_value = {"code": "40000", "data": []}
        with patch.object(main.SESSION, "get", return_value=response):
            with self.assertRaises(ValueError):
                main.get_symbols()

    def test_larger_universe_sorted_by_usdt_volume(self):
        rows = [{"symbol": f"COIN{n}USDT", "usdtVolume": str(n + 1), "quoteVolume": "0"} for n in range(80)]
        with patch("main.market_get", return_value=rows):
            symbols = main.get_symbols()
        self.assertEqual(len(symbols), 60)
        self.assertEqual(symbols[0], "COIN79USDT")

    def test_missing_bars_anywhere_pause_symbol(self):
        frame = candles().drop(index=90).reset_index(drop=True)
        updated, report = main.run_cycle(main.fresh_state(), {"BTCUSDT": frame})
        self.assertTrue(report["warnings"])
        self.assertEqual(updated["processed_until"], {})

    def test_catchup_does_not_invent_historical_entries(self):
        frame = candles()
        state = main.fresh_state()
        state["processed_until"] = {"BTCUSDT": int(frame.iloc[100]["time"])}
        with patch("main.open_position", return_value=False) as opened:
            main.run_cycle(state, {"BTCUSDT": frame})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[4], int(frame.iloc[-1]["time"]))

    def test_main_keeps_held_symbols_if_ranking_changes(self):
        state = main.fresh_state()
        state["positions"] = [position()]
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            report_path = Path(folder) / "report.json"
            main.save_state(state, state_path)
            with patch("sys.argv", ["main.py", "--state", str(state_path), "--report", str(report_path)]), \
                 patch("main.get_symbols", return_value=["ETHUSDT"]), \
                 patch("main.get_candles", return_value=candles()) as get, \
                 patch("main.time.sleep"), patch("builtins.print"), patch.dict(os.environ, {}, clear=True):
                main.main()
            self.assertEqual({call.args[0] for call in get.call_args_list}, {"BTCUSDT", "ETHUSDT"})


if __name__ == "__main__":
    unittest.main()
