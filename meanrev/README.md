# Mean-reversion paper bot

Paper only. It reads public Bitget candles and places no orders. No API keys. No profit guarantee.

It is separate from the trend simulator (`main.py`). It has its own state (`meanrev/paper_state.json`) and report, and it never touches `paper_state.json`.

## Rules
- Universe: top 60 USDT futures by volume, 1-hour candles (same data source as `main.py`).
- Long: close below the lower Bollinger band (20, 2) and RSI(14) below 25. Short: the mirror image.
- Only in choppy markets: 20-bar efficiency ratio below 0.35. Trending coins are skipped.
- Target: the 20-bar average at signal time. Stop: 2 ATR. Skip if reward is smaller than risk.
- Leaves after 24 hours (time exit).
- Risk 1% of balance per trade, max 3 open, 0.06% fee and 0.05% slippage per side (same as `main.py`).
- Tests live in `meanrev/` so they cannot affect the trend bot hourly run.
- Start balance $20, same as the trend bot, so the two can be compared.

## Fair comparison
`python meanrev/compare.py` replays both bots over the same candles, same start, same fees, bar by bar with no look-ahead.

## Honest limits
- On a ~500-hour, 20-coin window the final settings came out roughly flat (about 0%, 48 trades, profit factor about 1.0). Earlier looser settings lost money (about -16%). The trend bot gained about +7.6% on the same window.
- The final settings were chosen after looking at that same window, so the result is optimistic (in-sample). Treat it as no proven edge.
- Mean reversion tends to lose in strong trends and to win small and lose big. The efficiency filter and time exit reduce this but do not remove it.
- Hourly sampling, simplified fills, no funding fees, no liquidity or order-size checks.

## Going live
The scheduled workflow only runs after this is merged to `main`. Merging it starts a second paper account. Nothing is deleted. Removing the weaker bot is a separate decision.
