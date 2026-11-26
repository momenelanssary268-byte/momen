"""Paper-only 1-minute candle-pattern simulator. No authenticated exchange operations.

Strategy under test (from the user's MEXC AI Strategy screenshot):
  - Open LONG after 5 consecutive completed red 1-minute candles (close < open).
  - Open SHORT after 5 consecutive completed green 1-minute candles (close > open).
  - Take profit at +15% ROI, stop loss at -50% ROI, where ROI is measured on the
    paper margin with a fixed leverage multiplier (default 50x, configurable).

This module never places real orders. It is not an AI model and does not promise profit.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
from pathlib import Path
import tempfile
import time

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://api.bitget.com"
MEXC_BASE = "https://contract.mexc.com"
# Bitget TRUMPUSDT is the OFFICIAL TRUMP token; MEXC lists it as TRUMPOFFICIAL_USDT.
MEXC_SYMBOL_MAP = {"TRUMPUSDT": "TRUMPOFFICIAL_USDT"}
MINUTE = 60_000
START_BALANCE = 20.0
FEE = 0.0006
SLIPPAGE = 0.0005
STATE_FILE = "candle_state.json"
SYMBOL = "KAITOUSDT"
PATTERN = 5
TP_ROI = 0.15
SL_ROI = 0.50
LEVERAGE = 50.0
MARGIN_DOLLARS = 4.0
CANDLE_LIMIT = 200
STRATEGY_VERSION = 1

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"], respect_retry_after_header=True)))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh_state(symbol=SYMBOL, start_balance=START_BALANCE):
    return {"balance": start_balance, "symbol": symbol, "position": None,
            "history": [], "processed_until": 0}


def validate_state(state):
    """Reject bad state instead of silently resetting a paper account."""
    if not isinstance(state, dict) or not finite(state.get("balance")):
        raise ValueError("Invalid state balance; original file left untouched")
    if state["balance"] < 0:
        raise ValueError("Negative balance; inspect state before continuing")
    if not isinstance(state.get("history"), list):
        raise ValueError("Invalid state history")
    if not isinstance(state.get("processed_until"), int) or state["processed_until"] < 0:
        raise ValueError("Invalid processed_until")
    if not isinstance(state.get("symbol"), str) or not state["symbol"]:
        raise ValueError("Invalid state symbol")
    pos = state.get("position")
    if pos is not None:
        if pos.get("side") not in ("LONG", "SHORT") or pos.get("symbol") != state["symbol"]:
            raise ValueError("Invalid position identity")
        for key in ("entry", "stop", "target", "quantity", "margin", "entry_time"):
            if not finite(pos.get(key)) or pos[key] <= 0:
                raise ValueError(f"Invalid position {key}")
        if not finite(pos.get("entry_fee", 0)) or pos.get("entry_fee", 0) < 0:
            raise ValueError("Invalid entry fee")
    return state


def load_state(path=STATE_FILE, symbol=SYMBOL, start_balance=START_BALANCE):
    if not Path(path).exists():
        return fresh_state(symbol, start_balance)
    with open(path, encoding="utf-8") as handle:
        state = validate_state(json.load(handle))
    if state["symbol"] != symbol:
        raise ValueError(f"State file belongs to {state['symbol']}, not {symbol}")
    return state


def save_state(state, path=STATE_FILE):
    validate_state(state)
    dest = Path(path)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dest.parent,
                                         prefix=dest.name + ".", delete=False) as handle:
            name = handle.name
            json.dump(state, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, dest)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def market_get(path, params):
    allowed = {"/api/v2/mix/market/candles"}
    if path not in allowed:
        raise ValueError("Only public read-only market endpoints are allowed")
    response = SESSION.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "00000" or not isinstance(payload.get("data"), list):
        raise ValueError("Exchange returned an unsuccessful market-data response")
    return payload["data"]


def mexc_symbol(symbol):
    if symbol in MEXC_SYMBOL_MAP:
        return MEXC_SYMBOL_MAP[symbol]
    if not symbol.endswith("USDT"):
        raise ValueError(f"Cannot map {symbol} to a MEXC USDT perpetual")
    return symbol[:-4] + "_USDT"


def mexc_get(path, params):
    allowed_prefixes = ("/api/v1/contract/kline/", "/api/v1/contract/detail")
    if not any(path.startswith(p) for p in allowed_prefixes):
        raise ValueError("Only public read-only market endpoints are allowed")
    response = SESSION.get(MEXC_BASE + path, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success") or payload.get("data") is None:
        raise ValueError("MEXC returned an unsuccessful market-data response")
    return payload


def get_candles_mexc(symbol, now_ms=None):
    payload = mexc_get(f"/api/v1/contract/kline/{mexc_symbol(symbol)}",
                       {"interval": "Min1", "limit": "2000"})
    d = payload["data"]
    cols = [d["time"], d["open"], d["high"], d["low"], d["close"], d["vol"], d["amount"]]
    if len({len(c) for c in cols}) != 1:
        raise ValueError("MEXC kline arrays have mismatched lengths")
    rows = [[int(t) * 1000, o, h, l, c, v, a] for t, o, h, l, c, v, a in zip(*cols)]
    return clean_candles(rows, now_ms)


def get_mexc_taker_fee(symbol):
    payload = mexc_get("/api/v1/contract/detail", {"symbol": mexc_symbol(symbol)})
    rate = float(payload["data"].get("takerFeeRate", 0.0002))
    if not 0 <= rate <= 0.01:
        raise ValueError("Implausible MEXC taker fee rate")
    return rate


def clean_candles(rows, now_ms=None):
    """Only complete UTC 1-minute bars with valid, finite OHLCV values."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "quote"])
    frame = pd.DataFrame(rows)
    if frame.shape[1] < 7:
        raise ValueError("Expected seven candle columns")
    frame = frame.iloc[:, :7].copy()
    frame.columns = ["time", "open", "high", "low", "close", "volume", "quote"]
    for col in frame:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
        if not frame[col].map(math.isfinite).all():
            raise ValueError("Non-finite candle value")
    if ((frame[["open", "high", "low", "close"]] <= 0).any().any()
            or (frame[["volume", "quote"]] < 0).any().any()
            or (frame["time"] % MINUTE != 0).any()
            or (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Invalid candle geometry or timestamp")
    frame = frame[frame["time"] + MINUTE <= now_ms]
    return frame.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def get_candles(symbol, now_ms=None):
    rows = market_get("/api/v2/mix/market/candles", {
        "symbol": symbol, "granularity": "1m", "limit": str(CANDLE_LIMIT),
        "productType": "USDT-FUTURES"})
    return clean_candles(rows, now_ms)


def detect_signal(frame, idx):
    """Pattern in the PATTERN completed bars ending just before bar idx.

    Returns "LONG" after PATTERN consecutive red bars, "SHORT" after PATTERN
    consecutive green bars, otherwise None. A doji (close == open) breaks the run.
    Uses only bars strictly before idx: no look-ahead.
    """
    if idx < PATTERN:
        return None
    window = frame.iloc[idx - PATTERN:idx]
    if (window["close"] < window["open"]).all():
        return "LONG"
    if (window["close"] > window["open"]).all():
        return "SHORT"
    return None


def open_position(state, symbol, side, raw_price, entry_time, leverage, margin_dollars,
                  fee=FEE, slippage=SLIPPAGE, tp_roi=TP_ROI, sl_roi=SL_ROI):
    if state["position"] is not None or state["balance"] <= 0:
        return False
    direction = 1 if side == "LONG" else -1
    entry = raw_price * (1 + direction * slippage)
    margin = min(margin_dollars, state["balance"])
    notional = margin * leverage
    quantity = notional / entry
    entry_fee = notional * fee
    stop = entry - direction * entry * (sl_roi / leverage)
    target = entry + direction * entry * (tp_roi / leverage)
    if stop <= 0 or margin <= 0 or state["balance"] - entry_fee < 0:
        return False
    state["balance"] -= entry_fee
    state["position"] = {"symbol": symbol, "side": side, "entry": entry,
        "stop": stop, "target": target, "quantity": quantity, "margin": margin,
        "entry_fee": entry_fee, "entry_time": int(entry_time),
        "fee_rate": fee, "slippage_rate": slippage, "leverage": leverage,
        "strategy_version": STRATEGY_VERSION,
        "analysis": f"{PATTERN} consecutive {'red' if side == 'LONG' else 'green'} 1m candles"}
    return True


def close_position(state, raw_price, reason, candle_time):
    pos = state["position"]
    direction = 1 if pos["side"] == "LONG" else -1
    price = raw_price * (1 - direction * pos.get("slippage_rate", SLIPPAGE))
    gross = (price - pos["entry"]) * pos["quantity"] * direction
    exit_fee = price * pos["quantity"] * pos.get("fee_rate", FEE)
    # Entry fee was already deducted when the position opened. Never deduct it twice.
    state["balance"] += gross - exit_fee
    state["history"].append({"symbol": pos["symbol"], "side": pos["side"],
        "entry": pos["entry"], "exit": price, "quantity": pos["quantity"],
        "margin": pos["margin"], "leverage": pos["leverage"],
        "gross_pnl": gross, "entry_fee": pos.get("entry_fee", 0), "exit_fee": exit_fee,
        "net_pnl": gross - exit_fee - pos.get("entry_fee", 0), "reason": reason,
        "entry_time": pos["entry_time"], "exit_time": int(candle_time),
        "accounting_version": 1})
    state["position"] = None


def manage_bar(state, bar):
    pos = state["position"]
    if pos is None or bar["time"] < pos["entry_time"]:
        return
    direction = 1 if pos["side"] == "LONG" else -1
    stop, target, opening = pos["stop"], pos["target"], float(bar["open"])
    if direction * (opening - stop) <= 0:
        raw_price, reason = opening, "STOP GAP"
    elif direction * (opening - target) >= 0:
        raw_price, reason = target, "TAKE PROFIT"  # no optimistic gap improvement
    elif (bar["low"] <= stop if direction == 1 else bar["high"] >= stop):
        raw_price, reason = stop, "STOP LOSS"  # stop wins if both touched
    elif (bar["high"] >= target if direction == 1 else bar["low"] <= target):
        raw_price, reason = target, "TAKE PROFIT"
    else:
        return
    close_position(state, raw_price, reason, int(bar["time"]))


def unrealized(state, last_close):
    pos = state["position"]
    if pos is None:
        return 0.0
    direction = 1 if pos["side"] == "LONG" else -1
    return (last_close - pos["entry"]) * pos["quantity"] * direction


def metrics(state, last_close):
    pnl = [trade["net_pnl"] for trade in state["history"]]
    wins, losses = sum(p > 0 for p in pnl), sum(p <= 0 for p in pnl)
    profit, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    return {"balance": state["balance"],
        "equity": state["balance"] + unrealized(state, last_close),
        "closed_trades": len(pnl), "wins": wins, "losses": losses,
        "win_rate_pct": 100 * wins / len(pnl) if pnl else None,
        "net_realized_pnl": sum(pnl),
        "profit_factor": profit / loss if loss else None,
        "open_position": state["position"]}


def run_cycle(state, frame, symbol, leverage, margin_dollars, tp_roi=TP_ROI, sl_roi=SL_ROI,
              fee=FEE):
    """Replay unseen closed 1-minute bars in order.

    Every unseen bar first manages the open paper position, then may open a new
    one at its own open price when the PATTERN bars before it qualify. This
    replays exactly what a continuously running bot would have seen: signals use
    only bars that closed before the entry bar, never the future.
    """
    state = copy.deepcopy(state)
    warnings = []
    if len(frame) < PATTERN + 1:
        warnings.append(f"{symbol}: insufficient candle history")
        return state, {"warnings": warnings}, frame["close"].iloc[-1] if len(frame) else None
    if not frame["time"].diff().dropna().eq(MINUTE).all():
        warnings.append(f"{symbol}: non-contiguous candle history")
    last_close = float(frame.iloc[-1]["close"])
    processed = state["processed_until"]
    newest = int(frame.iloc[-1]["time"])
    if processed and newest - processed > len(frame) * MINUTE:
        warnings.append(f"{symbol}: gap larger than fetched window; some bars replayed from partial history")
    for idx in range(len(frame)):
        bar = frame.iloc[idx]
        if int(bar["time"]) <= processed:
            continue
        had_position = state["position"] is not None
        manage_bar(state, bar)
        if state["position"] is None and not had_position:
            # Enter only when the bot was flat at this bar's open. A bar that
            # just closed a position already passed its open price, so the
            # earliest honest re-entry is the next bar.
            side = detect_signal(frame, idx)
            if side:
                open_position(state, symbol, side, float(bar["open"]), int(bar["time"]),
                              leverage, margin_dollars, tp_roi=tp_roi, sl_roi=sl_roi, fee=fee)
                manage_bar(state, bar)  # entry bar is live for the new position
        state["processed_until"] = int(bar["time"])
    report = metrics(state, last_close)
    report["warnings"] = warnings
    return state, report, last_close


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--report", default="candle_report.json")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--leverage", type=float, default=LEVERAGE)
    parser.add_argument("--margin", type=float, default=MARGIN_DOLLARS)
    parser.add_argument("--start-balance", type=float, default=START_BALANCE)
    parser.add_argument("--tp-roi", type=float, default=TP_ROI * 100,
                        help="take-profit ROI percent (default 15)")
    parser.add_argument("--sl-roi", type=float, default=SL_ROI * 100,
                        help="stop-loss ROI percent (default 50)")
    parser.add_argument("--source", choices=["bitget", "mexc"], default="bitget",
                        help="price data source (default bitget)")
    args = parser.parse_args()
    if not 1 <= args.leverage <= 125:
        parser.error("--leverage must be between 1 and 125")
    if not 0 < args.margin <= args.start_balance:
        parser.error("--margin must be in (0, --start-balance]")
    if not 0 < args.start_balance <= 10000:
        parser.error("--start-balance must be in (0, 10000]")
    if not re.fullmatch(r"[A-Z0-9]{2,30}", args.symbol):
        parser.error("--symbol must be an uppercase futures symbol like KAITOUSDT")
    if not 0 < args.tp_roi <= 1000:
        parser.error("--tp-roi must be in (0, 1000]")
    if not 0 < args.sl_roi <= 1000:
        parser.error("--sl-roi must be in (0, 1000]")
    state = load_state(args.state, args.symbol, args.start_balance)
    if args.source == "mexc":
        frame = get_candles_mexc(args.symbol)
        fee = get_mexc_taker_fee(args.symbol)
    else:
        frame = get_candles(args.symbol)
        fee = FEE
    if frame.empty:
        raise RuntimeError("No valid market data; state left untouched")
    updated, report, last_close = run_cycle(state, frame, args.symbol,
                                            args.leverage, args.margin,
                                            tp_roi=args.tp_roi / 100, sl_roi=args.sl_roi / 100,
                                            fee=fee)
    report.update({
        "paper_only": True,
        "source": args.source,
        "symbol": args.symbol,
        "strategy": f"{PATTERN} consecutive 1m candles, fade the run",
        "take_profit_roi_pct": args.tp_roi,
        "stop_loss_roi_pct": args.sl_roi,
        "leverage": args.leverage,
        "margin_dollars_per_trade": args.margin,
        "start_balance": args.start_balance,
        "fee_per_side": fee,
        "slippage_per_side": SLIPPAGE,
        "last_close": last_close,
    })
    save_state(updated, args.state)
    Path(args.report).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    win_rate = report["win_rate_pct"]
    summary = (f"## Candle-pattern paper simulator, real money disabled\n\n"
        f"Symbol: {args.symbol} (1m candles)\n\n"
        f"Balance: ${report['balance']:.4f}\n\n"
        f"Marked equity: ${report['equity']:.4f}\n\n"
        f"Closed trades: {report['closed_trades']}\n\n"
        f"Win rate: {win_rate if win_rate is None else round(win_rate, 1)}%\n\n"
        f"Open position: {'yes' if report['open_position'] else 'no'}\n\n"
        f"Data warnings: {len(report['warnings'])}\n\n"
        "Historical simulation only. No real orders. No profit guarantee.\n")
    print(summary)
    print(json.dumps(report, indent=2, allow_nan=False))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)


if __name__ == "__main__":
    main()

