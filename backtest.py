"""Deterministic, offline portfolio backtest. CSV files are the only input."""
import argparse
import json
from pathlib import Path

import pandas as pd

from main import (HOUR, MIN_BARS, START_BALANCE, FEE, SLIPPAGE, clean_candles,
                  prepare, fresh_state, open_position, manage_bar, close_position,
                  equity, metrics)


def load_csv(path):
    frame = pd.read_csv(path)
    columns = ["time", "open", "high", "low", "close", "volume", "quote"]
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing CSV columns: {sorted(missing)}")
    if frame["time"].duplicated().any():
        raise ValueError("Duplicate CSV timestamps")
    cleaned = clean_candles(frame[columns].values.tolist())
    if len(cleaned) != len(frame):
        raise ValueError("CSV contains an unfinished or duplicate bar")
    if not cleaned["time"].diff().dropna().eq(HOUR).all():
        raise ValueError("CSV must contain contiguous hourly candles")
    return prepare(cleaned)


def simulate(frames, start, end, fee=FEE, slippage=SLIPPAGE):
    """Common clock. Signal at t-1 close, fill at t open, never at signal close."""
    state = fresh_state()
    curve = [START_BALANCE]
    prices = {}
    benchmark = {}
    by_symbol = {symbol: frame.set_index("time", drop=False) for symbol, frame in frames.items()}
    for stamp in range(start, end + HOUR, HOUR):
        for symbol in sorted(frames):
            indexed = by_symbol[symbol]
            row = indexed.loc[stamp]
            previous = indexed.loc[stamp - HOUR]
            if symbol not in benchmark:
                entry = float(row["open"]) * (1 + slippage)
                allocation = START_BALANCE / len(frames)
                benchmark[symbol] = allocation / (entry * (1 + fee))
            open_position(state, symbol, previous, float(row["open"]), stamp, fee, slippage)
        for symbol in sorted(frames):
            row = by_symbol[symbol].loc[stamp]
            manage_bar(state, symbol, row)
            prices[symbol] = float(row["close"])
        curve.append(equity(state, prices))
    for pos in list(state["positions"]):
        close_position(state, pos, prices[pos["symbol"]], "END OF TEST", end + HOUR)
    state["positions"] = []
    curve.append(state["balance"])
    result = metrics(state, curve)
    benchmark_equity = sum(quantity * prices[symbol] * (1 - slippage) * (1 - fee)
                           for symbol, quantity in benchmark.items())
    result.update({"start_time_ms": start, "end_time_ms": end + HOUR,
        "return_pct": (state["balance"] / START_BALANCE - 1) * 100,
        "equal_weight_buy_hold_return_pct": (benchmark_equity / START_BALANCE - 1) * 100,
        "fee_per_side": fee, "slippage_per_side": slippage,
        "drawdown_sampling": "hourly marked equity plus final liquidation; intrabar loss may be worse"})
    return result


def evaluate(frames, split=0.7, fee=FEE, slippage=SLIPPAGE):
    if not frames or not 0.5 <= split <= 0.9:
        raise ValueError("Need data and a split between 0.5 and 0.9")
    for frame in frames.values():
        if len(frame) < MIN_BARS + 20 or not frame["time"].diff().dropna().eq(HOUR).all():
            raise ValueError("Need at least 80 contiguous hourly candles per symbol")
    start = max(int(frame.iloc[MIN_BARS]["time"]) for frame in frames.values())
    end = min(int(frame.iloc[-1]["time"]) for frame in frames.values())
    count = (end - start) // HOUR + 1
    if count < 20:
        raise ValueError("Need at least 20 overlapping post-warm-up bars")
    cut = start + int(count * split) * HOUR
    return {"paper_only": True, "symbols": sorted(frames),
        "in_sample": simulate(frames, start, cut - HOUR, fee, slippage),
        "holdout": simulate(frames, cut, end, fee, slippage),
        "limitations": [
            "Fixed rules, no AI model, no tuning or fitted probability.",
            "Holdout resets capital; prior bars warm indicators only, never future bars.",
            "Synthetic fixed slippage; excludes funding, borrow costs, liquidation and depth.",
            "Current/manual symbol selection may introduce survivorship bias.",
            "Stop before target when both touched; adverse opening gaps honored.",
            "No profit guarantee. Paper results are not live execution results."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="+", help="CSV files, filename stem becomes symbol")
    parser.add_argument("--split", type=float, default=0.7)
    parser.add_argument("--fee", type=float, default=FEE)
    parser.add_argument("--slippage", type=float, default=SLIPPAGE)
    parser.add_argument("--output", default="backtest_report.json")
    args = parser.parse_args()
    if not 0 <= args.fee <= 0.01 or not 0 <= args.slippage <= 0.05:
        parser.error("fee must be 0..0.01 and slippage 0..0.05")
    paths = [Path(path) for path in args.csv]
    if len(set(path.stem for path in paths)) != len(paths):
        parser.error("CSV filename stems must be unique")
    result = evaluate({path.stem: load_csv(path) for path in paths}, args.split, args.fee, args.slippage)
    Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
