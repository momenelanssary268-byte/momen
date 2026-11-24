# Momen paper market simulator

This project simulates trades with public Bitget market data. It never places real orders, accepts exchange keys, or connects to a trading account. It is not an AI model and does not promise profit.

## What's different

- Scans up to 60 USDT futures symbols ranked by reported USDT trading volume. Existing positions are always requested even when they leave the ranking. A symbol is not necessarily a cryptocurrency; Bitget can list other underlying assets.
- Uses EMA 20/50, RSI 14, MACD 12/26/9, ATR 14, Bollinger bands 20/2, and relative volume. All five directional filters must agree. The score is a count of matching rules, not a probability or an AI confidence score.
- Uses completed hourly candles only. Indicators use a previous candle, with simulated entry at the next candle open. Stops and targets are automatic paper decisions.
- Uses ATR-based stops, a 2:1 gross target/stop distance, a 1% estimated loss budget and a maximum of three open positions. New position notional is capped at the current paper balance per position. This is not a margin or liquidation model.
- Includes fees of 0.06% and adverse slippage of 0.05% per side by default. Gaps through stops fill at the adverse opening price. If a candle touches both stop and target, the stop wins.
- Catches up missed exits but does not invent missed entries; only the newest completed bar can open a paper trade.
- Saves state atomically and fails rather than resetting invalid state. Per-symbol checkpoints prevent processing the same candle twice.
- Reports marked equity, fee-inclusive realized P&L, win rate, profit factor, sampled drawdown, open positions and data warnings. The workflow summary is brief; the JSON artifact contains the full rule analysis and warnings.

More indicators do not establish an edge. Validate on long, varied, unseen data before drawing any conclusion.

## Local use

Python 3.11 or later:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python main.py --max-coins 60
```

The last command reads public market endpoints and updates local `paper_state.json` and `paper_report.json`. Use `--state scratch_state.json --report scratch_report.json` to try a separate paper account. No credentials are needed. Do not run two processes against the same state file.

The hourly workflow still runs on the default `main` branch after review and merge. It serializes scheduled/manual runs, tests first, commits only state/report files, and uploads the report artifact. The pull-request workflow runs offline tests with read-only permissions. Neither workflow deploys a live trader.

## Existing paper state

The upgrade does **not** replace or reset `paper_state.json`. Existing balances, positions and history are kept. Extra metadata is added only when the simulator runs.

Old closed trades omitted entry fees from reported `net_pnl`. They cannot be reliably reconstructed from the old history, so new fee-correct trade statistics exclude them and show the excluded count. The balance still includes the original account's history. Compare new trades separately; do not mix the two accounting regimes.

Old positions retain their stops and targets. Old entry timestamps represent the signal candle close, so the upgrade will not use that same candle's high/low to close them. If the available 1,000-bar window cannot cover a position or a missed checkpoint, that symbol pauses with a warning instead of inventing an exit. Obtain the missing history and review the account manually; do not delete positions just to clear warnings.

Drawdown is sampled hourly since the upgrade, with the original $20 account baseline. Historical pre-upgrade peaks and intrabar worst prices are not known. A missing symbol can leave an equity mark stale; read warnings before trusting the report.

## Offline historical backtest

Supply your own historical CSVs. Each filename stem is its symbol. Required columns:

```text
time,open,high,low,close,volume,quote
```

`time` must be the UTC hourly candle **opening time in milliseconds**, with one contiguous row per hour, positive valid OHLC prices and nonnegative volumes. At least 80 candles per symbol and 20 overlapping post-warm-up candles are required. Larger samples are strongly preferred. Duplicate, future/unfinished and missing bars are rejected. Avoid selecting symbols because you already know they performed well.

```bash
python backtest.py data/BTCUSDT.csv data/ETHUSDT.csv --output backtest_report.json
python backtest.py data/BTCUSDT.csv --fee 0.001 --slippage 0.001
```

The first 70% of the common post-warm-up period is reported as in-sample. The remaining 30% is a separate holdout account with reset capital, using older bars only to warm up indicators. No parameter fitting is performed. Never keep tuning against the same holdout and call it unseen.

The backtester shares indicator, entry, sizing and exit logic with the hourly simulator. It processes all symbols on one common clock, applies the three-position cap, closes open positions at the end with fees/slippage, and compares each window with an equal-weight buy-and-hold portfolio using the same cost assumptions. It does not touch the live paper state or download data.

Limitations: fixed slippage, no funding payments, borrow fees, market depth, exchange minimums, margin or liquidation. Missing market data and survivorship bias can change conclusions. Stop/target ordering is a conservative assumption, not intrabar knowledge. Past simulated results are not evidence of future profit. This is research software, not financial advice.

## Tests

The offline suite covers candle validation, no forward-looking indicators, fees, adverse gaps, stop/target ambiguity, repeated runs, invalid-state protection, legacy compatibility, symbol coverage and deterministic backtests. Synthetic fixtures are not trading-performance evidence. A current-data smoke test does not validate a strategy.
