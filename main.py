"""Paper-only multi-indicator simulator. No authenticated exchange operations."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import time

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://api.bitget.com"
HOUR = 3_600_000
START_BALANCE = 20.0
RISK = 0.01
FEE = 0.0006
SLIPPAGE = 0.0005
MAX_COINS = 60
MAX_OPEN = 3
STATE_FILE = "paper_state.json"
MIN_BARS = 60
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"], respect_retry_after_header=True)))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh_state():
    return {"balance": START_BALANCE, "positions": [], "history": [], "last_signal": {}}


def validate_state(state):
    """Reject bad state instead of silently resetting a paper account."""
    if not isinstance(state, dict) or not finite(state.get("balance")):
        raise ValueError("Invalid state balance; original file left untouched")
    if state["balance"] < 0:
        raise ValueError("Negative balance; inspect state before continuing")
    for key in ("positions", "history"):
        if not isinstance(state.get(key), list):
            raise ValueError(f"Invalid state {key}")
    if not isinstance(state.get("last_signal"), dict):
        raise ValueError("Invalid last_signal")
    for pos in state["positions"]:
        if not isinstance(pos.get("symbol"), str) or pos.get("side") not in ("LONG", "SHORT"):
            raise ValueError("Invalid position identity")
        for key in ("entry", "stop", "target", "quantity", "entry_time"):
            if not finite(pos.get(key)) or pos[key] <= 0:
                raise ValueError(f"Invalid position {key}")
        if not finite(pos.get("entry_fee", 0)) or pos.get("entry_fee", 0) < 0:
            raise ValueError("Invalid entry fee")
    return state


def load_state(path=STATE_FILE):
    if not Path(path).exists():
        return fresh_state()
    with open(path, encoding="utf-8") as handle:
        return validate_state(json.load(handle))


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
    allowed = {"/api/v2/mix/market/tickers", "/api/v2/mix/market/candles"}
    if path not in allowed:
        raise ValueError("Only public read-only market endpoints are allowed")
    response = SESSION.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "00000" or not isinstance(payload.get("data"), list):
        raise ValueError("Exchange returned an unsuccessful market-data response")
    return payload["data"]


def get_symbols(limit=MAX_COINS):
    rows = market_get("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"})
    ranked = []
    for row in rows:
        try:
            symbol, volume = row["symbol"], float(row.get("usdtVolume") or row.get("quoteVolume") or 0)
            if symbol.endswith("USDT") and math.isfinite(volume) and volume > 0:
                ranked.append((symbol, volume))
        except (TypeError, ValueError, KeyError):
            continue
    return list(dict.fromkeys(s for s, _ in sorted(ranked, key=lambda x: x[1], reverse=True)))[:limit]


def clean_candles(rows, now_ms=None):
    """Only complete UTC hourly bars with valid, finite OHLCV values."""
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
            or (frame["time"] % HOUR != 0).any()
            or (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Invalid candle geometry or timestamp")
    frame = frame[frame["time"] + HOUR <= now_ms]
    return frame.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def get_candles(symbol, now_ms=None):
    rows = market_get("/api/v2/mix/market/candles", {
        "symbol": symbol, "granularity": "1H", "limit": "1000", "productType": "USDT-FUTURES"})
    return clean_candles(rows, now_ms)


def prepare(frame):
    frame = frame.copy()
    close = frame["close"]
    frame["ema20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain, loss = delta.clip(lower=0).rolling(14).mean(), -delta.clip(upper=0).rolling(14).mean()
    ratio = gain / loss.where(loss != 0)
    frame["rsi"] = 100 - 100 / (1 + ratio)
    frame.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    frame.loc[(loss == 0) & (gain == 0), "rsi"] = 50
    frame["macd"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    previous = close.shift()
    tr = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous).abs(),
                    (frame["low"] - previous).abs()], axis=1).max(axis=1)
    frame["atr"] = tr.rolling(14).mean()
    middle, std = close.rolling(20).mean(), close.rolling(20).std(ddof=0)
    frame["bb_mid"], frame["bb_upper"], frame["bb_lower"] = middle, middle + 2 * std, middle - 2 * std
    frame["volume_ratio"] = frame["volume"] / frame["volume"].shift().rolling(20).mean().replace(0, float("nan"))
    return frame


def analyze(row):
    """An explainable rule score, NOT machine learning or a probability."""
    required = ["ema20", "ema50", "rsi", "macd", "macd_signal", "atr", "volume_ratio", "bb_upper", "bb_lower"]
    if any(not finite(float(row[key])) for key in required):
        return None, 0, "indicator warm-up or missing volume"
    if not 0.002 <= row["atr"] / row["close"] <= 0.08:
        return None, 0, "volatility outside filter"
    long = [row["ema20"] > row["ema50"], 50 < row["rsi"] < 68,
            row["macd"] > row["macd_signal"], row["close"] < row["bb_upper"], row["volume_ratio"] >= 0.8]
    short = [row["ema20"] < row["ema50"], 32 < row["rsi"] < 50,
             row["macd"] < row["macd_signal"], row["close"] > row["bb_lower"], row["volume_ratio"] >= 0.8]
    if all(long):
        return "LONG", 5, "EMA trend + RSI + MACD + Bollinger + volume"
    if all(short):
        return "SHORT", 5, "EMA trend + RSI + MACD + Bollinger + volume"
    return None, max(sum(long), sum(short)), "filters not aligned"


def open_position(state, symbol, signal_row, raw_price, entry_time, fee=FEE, slippage=SLIPPAGE):
    side, score, reason = analyze(signal_row)
    if (not side or state["balance"] <= 0 or len(state["positions"]) >= MAX_OPEN
            or any(p["symbol"] == symbol for p in state["positions"])
            or state["last_signal"].get(symbol, -1) >= int(signal_row["time"])):
        return False
    direction = 1 if side == "LONG" else -1
    entry = raw_price * (1 + direction * slippage)
    distance = max(1.5 * float(signal_row["atr"]), entry * 0.005)
    # Loss budget includes both fees and a conservative estimated stop slippage.
    risk_per_unit = distance + entry * (2 * fee + slippage)
    quantity = min(state["balance"] * RISK / risk_per_unit, state["balance"] / entry)
    if entry - 2 * distance <= 0 or quantity <= 0:
        return False
    entry_fee = quantity * entry * fee
    state["balance"] -= entry_fee
    state["positions"].append({"symbol": symbol, "side": side, "entry": entry,
        "stop": entry - direction * distance, "target": entry + direction * 2 * distance,
        "quantity": quantity, "entry_fee": entry_fee, "entry_time": int(entry_time),
        "fee_rate": fee, "slippage_rate": slippage, "strategy_version": 5,
        "score": score, "analysis": reason})
    state["last_signal"][symbol] = int(signal_row["time"])
    return True


def close_position(state, pos, raw_price, reason, candle_time):
    direction = 1 if pos["side"] == "LONG" else -1
    price = raw_price * (1 - direction * pos.get("slippage_rate", SLIPPAGE))
    gross = (price - pos["entry"]) * pos["quantity"] * direction
    exit_fee = price * pos["quantity"] * pos.get("fee_rate", FEE)
    # Entry fee was already deducted when the position opened. Never deduct it twice.
    state["balance"] += gross - exit_fee
    state["history"].append({"symbol": pos["symbol"], "side": pos["side"],
        "entry": pos["entry"], "exit": price, "quantity": pos["quantity"],
        "gross_pnl": gross, "entry_fee": pos.get("entry_fee", 0), "exit_fee": exit_fee,
        "net_pnl": gross - exit_fee - pos.get("entry_fee", 0), "reason": reason,
        "entry_time": pos["entry_time"], "exit_time": int(candle_time),
        "accounting_version": 2})


def manage_bar(state, symbol, bar):
    remaining = []
    for pos in state["positions"]:
        before_entry = (bar["time"] < pos["entry_time"] if pos.get("strategy_version") == 5
                        else bar["time"] <= pos["entry_time"])
        if pos["symbol"] != symbol or before_entry:
            remaining.append(pos)
            continue
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
            remaining.append(pos)
            continue
        close_position(state, pos, raw_price, reason, int(bar["time"]))
    state["positions"] = remaining


def equity(state, prices):
    return state["balance"] + sum((prices.get(p["symbol"], p["entry"]) - p["entry"])
        * p["quantity"] * (1 if p["side"] == "LONG" else -1) for p in state["positions"])


def metrics(state, curve):
    modern = [trade for trade in state["history"] if trade.get("accounting_version") == 2]
    pnl = [trade["net_pnl"] for trade in modern]
    wins, losses = sum(p > 0 for p in pnl), sum(p <= 0 for p in pnl)
    profit, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    peak = curve[0] if curve else START_BALANCE
    drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak > 0 else 0)
    return {"balance": state["balance"], "equity": curve[-1] if curve else state["balance"],
        "closed_trades": len(pnl), "wins": wins, "losses": losses,
        "win_rate_pct": 100 * wins / len(pnl) if pnl else None,
        "net_realized_pnl": sum(pnl), "profit_factor": profit / loss if loss else None,
        "max_drawdown_pct": drawdown * 100, "open_positions": len(state["positions"]),
        "legacy_trades_excluded": len(state["history"]) - len(modern)}


def run_cycle(state, frames):
    """Replay unseen closed bars. Each entry uses the PRIOR bar's indicators."""
    state = copy.deepcopy(state)
    checkpoints = state.setdefault("processed_until", {})
    prices = dict(state.get("last_prices", {}))
    events, warnings, analyses = {}, [], []
    for symbol, raw_frame in frames.items():
        frame = prepare(raw_frame)
        if not frame["time"].diff().dropna().eq(HOUR).all():
            warnings.append(f"{symbol}: non-contiguous candle history; symbol paused")
            continue
        if len(frame) < MIN_BARS + 1:
            warnings.append(f"{symbol}: insufficient candle history")
            continue
        stamp = int(frame.iloc[-1]["time"])
        last = checkpoints.get(symbol)
        positions = [p for p in state["positions"] if p["symbol"] == symbol]
        if last is None:
            # Existing positions need every bar since entry. No invented migration exits.
            last = min(p["entry_time"] for p in positions) - 1 if positions else stamp - 1
        unseen = frame[frame["time"] > last]
        if len(unseen) and last < stamp - 1 and int(unseen.iloc[0]["time"]) > last + HOUR:
            warnings.append(f"{symbol}: missing bars since checkpoint; position left untouched")
            continue
        if positions and int(frame.iloc[0]["time"]) > min(p["entry_time"] for p in positions) and symbol not in checkpoints:
            warnings.append(f"{symbol}: entry predates available history; position left untouched")
            continue
        for idx in unseen.index:
            if idx < MIN_BARS:
                warnings.append(f"{symbol}: missing indicator warm-up for unseen bars")
                break
            bar = frame.loc[idx]
            if int(bar["time"]) - int(frame.loc[idx - 1, "time"]) != HOUR:
                warnings.append(f"{symbol}: non-contiguous candles; symbol paused")
                break
            events.setdefault(int(bar["time"]), []).append((symbol, frame.loc[idx - 1], bar))
        side, score, why = analyze(frame.iloc[-1])
        analyses.append({"symbol": symbol, "side": side or "WAIT", "score_out_of_5": int(score), "reason": why})
    curve = list(state.get("equity_curve", []))
    latest_event = max(events, default=-1)
    previous_global = max((point["time"] - HOUR for point in curve), default=-1)
    for stamp, batch in sorted(events.items()):
        # Catch up exits only. Do not invent a series of missed historical entries.
        # At the newest open, reserve capacity BEFORE intrabar exits are known.
        for symbol, signal, bar in sorted(batch, key=lambda item: item[0]):
            if (stamp == latest_event and stamp > previous_global
                    and not any(p["symbol"] == symbol for p in state["positions"])):
                open_position(state, symbol, signal, float(bar["open"]), stamp)
        for symbol, signal, bar in batch:
            manage_bar(state, symbol, bar)
            prices[symbol] = float(bar["close"])
            checkpoints[symbol] = stamp
        if stamp > previous_global:
            curve.append({"time": stamp + HOUR, "equity": equity(state, prices)})
    state["equity_curve"] = curve  # Preserve the full reporting baseline.
    state["last_prices"] = prices
    report = metrics(state, [START_BALANCE] + [point["equity"] for point in curve])
    report.update({"paper_only": True, "warnings": warnings, "analysis": analyses,
        "equity_marks_may_be_stale": bool(warnings) or any(p["symbol"] not in frames for p in state["positions"]),
        "drawdown_scope": "sampled hourly since upgrade; not intrabar or pre-upgrade"})
    return state, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--max-coins", type=int, default=MAX_COINS)
    parser.add_argument("--report", default="paper_report.json")
    args = parser.parse_args()
    if not 1 <= args.max_coins <= 100:
        parser.error("--max-coins must be between 1 and 100")
    state = load_state(args.state)
    held = list(dict.fromkeys(p["symbol"] for p in state["positions"]))
    warnings = []
    try:
        symbols = list(dict.fromkeys(held + get_symbols(args.max_coins)))
    except (requests.RequestException, ValueError) as exc:
        if not held:
            raise
        symbols = held
        warnings.append(f"Universe unavailable, managing held symbols only: {type(exc).__name__}")
    frames = {}
    now_ms = int(time.time() * 1000)
    for symbol in symbols:
        try:
            frames[symbol] = get_candles(symbol, now_ms)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"{symbol}: market data unavailable ({type(exc).__name__})")
        time.sleep(0.25)
    if not frames:
        raise RuntimeError("No valid market data; state left untouched")
    updated, report = run_cycle(state, frames)
    report["warnings"].extend(warnings)
    report["symbols_requested"], report["symbols_fetched"] = len(symbols), len(frames)
    save_state(updated, args.state)
    Path(args.report).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    summary = (f"## Paper simulator, real money disabled\n\n"
        f"Balance: ${report['balance']:.4f}\n\n"
        f"Marked equity: ${report['equity']:.4f}\n\n"
        f"Closed trades (new accounting): {report['closed_trades']}\n\n"
        f"Win rate: {report['win_rate_pct']}%\n\n"
        f"Sampled drawdown: {report['max_drawdown_pct']:.2f}%\n\n"
        f"Open positions: {report['open_positions']}\n\n"
        f"Data warnings: {len(report['warnings'])}\n\n"
        "Historical simulation only. No real orders. No profit guarantee.\n")
    print(summary)
    print(json.dumps(report, indent=2, allow_nan=False))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)


if __name__ == "__main__":
    main()
