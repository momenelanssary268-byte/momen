"""Offline $10 isolated-margin stress scenarios, NOT exchange liquidation quotes."""
import argparse
import json
from pathlib import Path

from backtest import load_csv
from main import HOUR, MIN_BARS, FEE, SLIPPAGE, analyze


def scenario(frame, leverage, start_balance=10.0, fee=FEE, slippage=SLIPPAGE,
             maintenance=0.005, funding_per_8h=0.0001):
    """One position, all remaining cash as isolated collateral; no capital top-ups.

    Margin equity <= maintenance * entry notional triggers an approximate
    liquidation. Entry notional is fixed. This is an explicitly simplified
    stress model, not Bitget's tiered mark-price liquidation calculation.
    """
    if leverage < 1 or not 0 <= maintenance < 1 / leverage:
        raise ValueError("Invalid leverage/maintenance combination")
    cash, peak, drawdown = start_balance, start_balance, 0.0
    position = None
    liquidations = trades = wins = 0
    funding_paid = fees_paid = 0.0
    last_close = float(frame.iloc[MIN_BARS]["open"])
    first_open = last_close

    def mark(value):
        nonlocal peak, drawdown
        value = max(0.0, value)
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak else 0)

    def finish(raw_price, liquidated=False):
        nonlocal position, cash, liquidations, trades, wins, fees_paid
        p = position
        exit_price = raw_price * (1 - p["direction"] * slippage)
        exit_fee = exit_price * p["qty"] * fee
        fees_paid += exit_fee
        ending = max(0.0, p["collateral"] + p["direction"] * (exit_price - p["entry"]) * p["qty"] - exit_fee)
        if liquidated:
            # Conservative stress: all allocated collateral lost; no recoveries.
            ending = 0.0
            liquidations += 1
        cash = ending
        trades += 1
        wins += ending > p["before_entry"]
        position = None
        mark(cash)

    for idx in range(MIN_BARS, len(frame)):
        row, previous = frame.iloc[idx], frame.iloc[idx - 1]
        if position is None and cash > 0:
            side, _, _ = analyze(previous)
            if side:
                direction = 1 if side == "LONG" else -1
                entry = float(row["open"]) * (1 + direction * slippage)
                # Choose N so the initial margin plus entry fee fits available cash.
                notional = cash / (1 / leverage + fee)
                entry_fee = notional * fee
                fees_paid += entry_fee
                distance = max(1.5 * float(previous["atr"]), entry * 0.005)
                position = {"entry": entry, "qty": notional / entry,
                    "notional": notional, "direction": direction,
                    "collateral": cash - entry_fee, "before_entry": cash,
                    "stop": entry - direction * distance,
                    "target": entry + direction * 2 * distance}
                cash = 0.0
        if position:
            p = position
            charge = p["notional"] * funding_per_8h / 8
            funding_paid += charge
            p["collateral"] -= charge
            threshold = p["entry"] + p["direction"] * (
                maintenance * p["notional"] - p["collateral"]) / p["qty"]
            adverse = float(row["low"] if p["direction"] == 1 else row["high"])
            # When intrabar order is unknown, liquidation wins over an ordinary stop.
            if p["collateral"] <= maintenance * p["notional"] or p["direction"] * (adverse - threshold) <= 0:
                finish(threshold, True)
            else:
                opening = float(row["open"])
                if p["direction"] * (opening - p["stop"]) <= 0:
                    finish(opening)
                elif p["direction"] * (opening - p["target"]) >= 0:
                    finish(p["target"])
                elif (row["low"] <= p["stop"] if p["direction"] == 1 else row["high"] >= p["stop"]):
                    finish(p["stop"])
                elif (row["high"] >= p["target"] if p["direction"] == 1 else row["low"] <= p["target"]):
                    finish(p["target"])
        last_close = float(row["close"])
        if position:
            p = position
            mark(p["collateral"] + p["direction"] * (last_close - p["entry"]) * p["qty"])
        else:
            mark(cash)
    if position:
        finish(last_close)
    buy_qty = start_balance / (first_open * (1 + slippage) * (1 + fee))
    buy_end = buy_qty * last_close * (1 - slippage) * (1 - fee)
    return {"leverage": leverage, "start_balance": start_balance, "final_balance": cash,
        "return_pct": (cash / start_balance - 1) * 100,
        "max_sampled_drawdown_pct": drawdown * 100, "trades": trades,
        "win_rate_pct": wins / trades * 100 if trades else None,
        "liquidations": liquidations, "liquidation_pct_of_trades": liquidations / trades * 100 if trades else 0,
        "account_wiped_out": cash <= 0, "fees_paid": fees_paid, "funding_paid": funding_paid,
        "unleveraged_buy_hold_final_balance": buy_end,
        "start_time_ms": int(frame.iloc[MIN_BARS]["time"]),
        "end_time_ms": int(frame.iloc[-1]["time"]) + HOUR}


def compare(frame, levels=(1, 2, 5, 10, 20), funding_per_8h=0.0001):
    if len(frame) < MIN_BARS + 40:
        raise ValueError("At least 100 bars required")
    cut = MIN_BARS + int((len(frame) - MIN_BARS) * 0.7)
    windows = {"in_sample": frame.iloc[:cut].reset_index(drop=True),
               "holdout": frame.iloc[cut - MIN_BARS:].reset_index(drop=True)}
    return {"paper_only": True, "model": "simplified isolated-margin stress test, not exchange-accurate",
        "assumptions": {"start_balance": 10, "leverage_levels": list(levels),
            "fee_per_side": FEE, "slippage_per_side": SLIPPAGE,
            "maintenance_ratio": 0.005, "funding_cost_per_8h": funding_per_8h,
            "funding": "charged hourly pro rata in both directions, no credits",
            "sizing": "one position at a time; all remaining cash isolated as collateral",
            "liquidation": "fixed entry-notional maintenance; whole collateral lost; no top-ups",
            "ambiguous_bar": "liquidation before stop or target, pessimistic assumption"},
        "windows": {name: [scenario(data, level, funding_per_8h=funding_per_8h)
                            for level in levels] for name, data in windows.items()},
        "limits": ["Not the hourly simulator's 1% risk-capped sizing; this deliberately stresses leverage.",
            "Not Bitget tiered maintenance, mark-price or liquidation-fee rules.",
            "No order minima, quantity steps, depth, cross margin or negative-balance claims.",
            "Sampled drawdown excludes intrabar equity minima except modeled liquidations.",
            "One sample and fixed assumptions cannot establish profitability or a safe leverage."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--output", default="leverage_report.json")
    parser.add_argument("--funding-per-8h", type=float, default=0.0001)
    args = parser.parse_args()
    if not 0 <= args.funding_per_8h <= 0.01:
        parser.error("funding must be between 0 and 0.01 per eight hours")
    result = compare(load_csv(args.csv), funding_per_8h=args.funding_per_8h)
    Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
