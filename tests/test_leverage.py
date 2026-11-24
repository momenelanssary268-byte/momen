import unittest
from unittest.mock import patch

from main import prepare
from leverage_backtest import scenario, compare
from test_simulator import candles


class LeverageTests(unittest.TestCase):
    def test_invalid_leverage_rejected(self):
        with self.assertRaises(ValueError):
            scenario(prepare(candles()), 0)
        with self.assertRaises(ValueError):
            scenario(prepare(candles()), 200)

    def test_wipeout_cannot_reenter_or_go_negative(self):
        frame = candles()
        frame.loc[60, 'low'] = 1.0
        with patch('leverage_backtest.analyze', return_value=('LONG', 5, 'test')):
            result = scenario(prepare(frame), 20)
        self.assertEqual(result['final_balance'], 0)
        self.assertEqual(result['liquidations'], 1)
        self.assertEqual(result['trades'], 1)
        self.assertEqual(result['max_sampled_drawdown_pct'], 100)

    def test_funding_and_costs_reduce_balance(self):
        frame = candles()
        frame[['open', 'close']] = 100.0
        frame['high'], frame['low'] = 100.2, 99.8
        frame = prepare(frame)
        with patch('leverage_backtest.analyze', return_value=('LONG', 5, 'test')):
            charged = scenario(frame, 2)
            free = scenario(frame, 2, fee=0, slippage=0, funding_per_8h=0)
        self.assertGreater(charged['fees_paid'], 0)
        self.assertGreater(charged['funding_paid'], 0)
        self.assertLess(charged['final_balance'], free['final_balance'])
        self.assertAlmostEqual(free['final_balance'], 10)

    def test_holdout_and_no_trade_control(self):
        frame = prepare(candles())
        with patch('leverage_backtest.analyze', return_value=(None, 0, 'wait')):
            result = compare(frame)
        for window in result['windows'].values():
            self.assertEqual(len(window), 5)
            self.assertTrue(all(row['final_balance'] == 10 for row in window))
        self.assertEqual(result['windows']['in_sample'][0]['end_time_ms'],
                         result['windows']['holdout'][0]['start_time_ms'])


if __name__ == '__main__':
    unittest.main()
