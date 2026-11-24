"""Fair head-to-head backtest of the trend bot (main.py) and the mean-reversion bot.

Both bots get the SAME hourly candles, the SAME $20 start, the SAME fees and
slippage, and are replayed bar by bar through their own run_cycle() with no
look-ahead. Paper only: reads public candles, places nothing. A short history
is weak evidence, not proof either bot makes money.

Usage: python meanrev/compare.py [--coins 25] [--bars 600]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot as mean_bot  # noqa: E402
import main as trend_bot  # noqa: E402


def replay(module, frames, bars):
    state = module.fresh_state()
    n = min(len(f) for f in frames.values())
    start = max(module.MIN_BARS + 2, n - bars)
    curve = []
    for end in range(start, n + 1):
        window = {s: f.iloc[:end].reset_index(drop=True) for s, f in frames.items()}
        state, report = module.run_cycle(state, window)
        curve.append(report["equity"])
    return state, report, curve


def summarize(name, state, curve):
    hist = state["history"]
    pnl = [t["net_pnl"] for t in hist]
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0)
    gp, gl = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    return {"bot": name, "start": 20.0, "final_equity": round(curve[-1], 4),
            "return_pct": round((curve[-1] / 20.0 - 1) * 100, 2), "closed_trades": len(pnl),
            "win_rate_pct": round(100 * sum(p > 0 for p in pnl) / len(pnl), 1) if pnl else None,
            "profit_factor": round(gp / gl, 2) if gl else None,
            "max_drawdown_pct": round(dd * 100, 2), "open_at_end": len(state["positions"])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", type=int, default=25)
    parser.add_argument("--bars", type=int, default=600)
    args = parser.parse_args()
    frames = {}
    for symbol in mean_bot.get_symbols(args.coins):
        try:
            frames[symbol] = mean_bot.get_candles(symbol)
        except Exception:
            continue
        time.sleep(0.25)
    # Align every coin to the same recent hours so both bots see identical data.
    common = min(len(f) for f in frames.values())
    frames = {s: f.iloc[-common:].reset_index(drop=True) for s, f in frames.items()}
    out = []
    for name, module in (("trend (main.py)", trend_bot), ("mean-reversion (meanrev)", mean_bot)):
        state, report, curve = replay(module, frames, args.bars)
        out.append(summarize(name, state, curve))
    print(json.dumps({"coins": len(frames), "bars_replayed": args.bars, "results": out,
                      "warning": "Backtest on a short window. Not a forecast. Paper only."}, indent=2))


if __name__ == "__main__":
    main()
