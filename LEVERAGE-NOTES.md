# $10 leverage stress-test example

One BTCUSDT hourly sample, fetched from Bitget's public candles endpoint on November 24, 2026. Results below are an example run, not proof of an edge. Do not select leverage based on this short period. No parameters were fitted, but a single holdout is still weak evidence.

Source endpoint: https://api.bitget.com/api/v2/mix/market/candles
Parameters: symbol=BTCUSDT, productType=USDT-FUTURES, granularity=1H, limit=1000. There were 999 completed bars after the unfinished bar was removed.

## Model

Separate offline isolated-margin stress test, one position at a time with all remaining cash committed as collateral. This is more aggressive sizing than the hourly simulator's risk cap. Every window starts with $10. Fees are 0.06% per side, slippage 0.05% per side, maintenance 0.5% of initial notional. Funding costs 0.01% per eight hours, accrued hourly in both directions without credits. Liquidation consumes the whole remaining account. No top-ups or further entries after wipeout.

This is NOT an exchange-accurate liquidation calculation. It omits actual maintenance tiers, mark prices, liquidation fees, market depth, order minimums and historical funding rates. If liquidation and a normal stop are both touched in one bar, liquidation wins. This can overstate losses when the true intrabar path would have hit the stop first. Other omissions may understate losses.

## Results

In-sample: October 16, 2026 05:00 UTC through November 12, 2026 14:00 UTC. Holdout: November 12, 2026 14:00 UTC through November 24, 2026 08:00 UTC. Reported endpoints are the exclusive closing boundary after the last bar.

| Leverage | In-sample final $ | Sampled drawdown | Liquidations / trades | Holdout final $ | Sampled drawdown | Liquidations / trades |
|---|---:|---:|---:|---:|---:|---:|
| 1x | 9.8526 | 6.56% | 0 / 26 | 10.7444 | 5.02% | 0 / 7 |
| 2x | 9.6638 | 12.94% | 0 / 26 | 11.4708 | 9.88% | 0 / 7 |
| 5x | 8.8800 | 30.74% | 0 / 26 | 13.4675 | 23.52% | 0 / 7 |
| 10x | 7.0709 | 55.27% | 0 / 26 | 15.7661 | 43.30% | 0 / 7 |
| 20x | 3.2291 | 85.04% | 0 / 26 | 0.0000 | 100.00% | 1 / 4 |

Unleveraged buy-and-hold, using the same trading fees and slippage, ended at $8.8057 in-sample and $8.3149 in the holdout. Buy-and-hold has no modeled funding charge. Each comparison window uses its own $10 start. The raw JSON report can be reproduced with the matching candle CSV; re-downloading later changes the rolling data window.

The honest conclusion is not that 10x is best. In this sample, losses and drawdowns grew with leverage, while 20x wiped out the account in the holdout under the stated pessimistic model. The apparently positive smaller-leverage holdout has only seven closed trades. That is far too little to establish reliability, and the in-sample period lost money at every leverage tested. No live trading or real-money feature was added.
