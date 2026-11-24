"""Offline tests for the Bybit DEMO-only executor. No network, no keys."""
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bybit_demo as demo


class DemoHostGuardTest(unittest.TestCase):
    def test_demo_host_accepted(self):
        self.assertEqual(demo.assert_demo_url("https://api-demo.bybit.com/v5/order/create"),
                         "https://api-demo.bybit.com/v5/order/create")

    def test_live_and_lookalike_hosts_rejected(self):
        bad = ["https://api.bybit.com/v5/order/create",
               "https://api-testnet.bybit.com/v5/order/create",
               "http://api-demo.bybit.com/v5/order/create",
               "https://api-demo.bybit.com.evil.example/v5/order/create",
               "https://api-demo.bybit.com@evil.example/v5/order/create"]
        for url in bad:
            with self.assertRaises(demo.DemoSafetyError, msg=url):
                demo.assert_demo_url(url)

    def test_request_guard_runs_before_send(self):
        # Even with keys set, a non-demo URL must raise before any network use.
        env = {"BYBIT_DEMO_API_KEY": "k", "BYBIT_DEMO_API_SECRET": "s"}
        with mock.patch.object(demo, "DEMO_HOST", "https://api.bybit.com"), \
             mock.patch("requests.post") as post:
            with self.assertRaises(demo.DemoSafetyError):
                demo.demo_request("POST", "/v5/order/create", payload={}, env=env)
            post.assert_not_called()


class KeyRequirementTest(unittest.TestCase):
    def test_missing_keys_raise_system_exit(self):
        with self.assertRaises(SystemExit):
            demo.require_demo_keys({})

    def test_keys_returned_when_present(self):
        self.assertEqual(demo.require_demo_keys(
            {"BYBIT_DEMO_API_KEY": "k", "BYBIT_DEMO_API_SECRET": "s"}), ("k", "s"))

    def test_run_skips_cleanly_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                report = demo.run(state_path=os.path.join(tmp, "s.json"),
                                  report_path=os.path.join(tmp, "r.json"), env={})
        self.assertEqual(report["status"], "skipped_no_demo_keys")
        self.assertTrue(report["demo_only"])


class SigningTest(unittest.TestCase):
    def test_signature_matches_hmac(self):
        expected = hmac.new(b"secret", b"1700000000000key5000{}", hashlib.sha256).hexdigest()
        self.assertEqual(demo.sign_payload("secret", "1700000000000", "key", "5000", "{}"), expected)


class OrderBuildingTest(unittest.TestCase):
    def test_round_down(self):
        self.assertEqual(demo.round_down(Decimal("1.23456"), Decimal("0.001")), Decimal("1.234"))
        self.assertEqual(demo.round_down(Decimal("5"), Decimal("0.5")), Decimal("5.0"))
        with self.assertRaises(ValueError):
            demo.round_down(Decimal("1"), Decimal("0"))

    def test_order_has_tp_sl_and_one_way_mode(self):
        order = demo.build_order("BTCUSDT", "LONG", Decimal("0.001"),
                                 Decimal("30000"), Decimal("33000"), "demo-BTCUSDT-1")
        self.assertEqual(order["category"], "linear")
        self.assertEqual(order["side"], "Buy")
        self.assertEqual(order["orderType"], "Market")
        self.assertEqual(order["positionIdx"], 0)
        self.assertEqual(order["takeProfit"], "33000")
        self.assertEqual(order["stopLoss"], "30000")
        self.assertLessEqual(len(order["orderLinkId"]), 36)

    def test_short_prices_bracket_reference(self):
        stop, target = demo.build_prices("SHORT", 100, 2, Decimal("0.1"))
        self.assertLess(target, Decimal("100"))
        self.assertGreater(stop, Decimal("100"))
        stop, target = demo.build_prices("LONG", 100, 2, Decimal("0.1"))
        self.assertLess(stop, Decimal("100"))
        self.assertGreater(target, Decimal("100"))


class IsolationTest(unittest.TestCase):
    def test_module_never_references_paper_state(self):
        source = Path(demo.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"paper_state.json"', source)
        self.assertNotIn("'paper_state.json'", source)

    def test_module_has_no_live_endpoints(self):
        source = Path(demo.__file__).read_text(encoding="utf-8")
        for live in ("api.bybit.com\"", "api-testnet.bybit.com", "stream.bybit.com"):
            self.assertNotIn(live, source.replace("api-demo.bybit.com", ""))

    def test_demo_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo_state.json")
            state = {"positions": [], "history": [], "last_signal": {}}
            demo.save_demo_state(state, path)
            self.assertEqual(demo.load_demo_state(path), state)


if __name__ == "__main__":
    unittest.main()
