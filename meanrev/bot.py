"""Paper-only MEAN-REVERSION simulator. Separate from main.py. No orders, no keys.

Idea: fade stretched moves. Buy when price closes below the lower Bollinger band
with RSI oversold, sell short when it closes above the upper band with RSI
overbought. Target is the 20-bar average (the "mean"). Not machine learning,
not a prediction, no profit guarantee.
"""
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
START_BALANCE = 20.0   # same start as the trend simulator so results compare
RISK = 0.01            # 1% of balance risked per trade (stop distance)
FEE = 0.0006
SLIPPAGE = 0.0005
MAX_COINS = 60
MAX_OPEN = 3
MAX_HOLD_BARS = 24     # time stop: leave a stale trade after 24 hours
RSI_LOW, RSI_HIGH = 25, 75
STOP_ATR = 2.0
ER_MAX = 0.35          # only fade when the market is choppy (efficiency ratio below this)
MIN_RR = 1.0           # skip if the target is too close versus the stop
MIN_BARS = 60
HERE = Path(__file__).resolve().parent
STATE_FILE = str(HERE / "paper_state.json")
REPORT_FILE = str(HERE / "paper_report.json")
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"], respect_retry_after_header=True)))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh_state():
    return {"strategy": "mean_reversion_v1", "balance": START_BALANCE, "positions": [],
            "history": [], "processed_until": {}, "equity_curve": [], "last_prices": {}}


def validate_state(state):
    if not isinstance(state, dict) or not finite(state.get("balance")) or state["balance"] < 0:
        raise ValueError("Invalid state balance; file left untouched")
    for key in ("positions", "history"):
        if not isinstance(state.get(key), list):
            raise ValueError(f"Invalid state {key}")
    for pos in state["positions"]:
        if not isinstance(pos.get("symbol"), str) or pos.get("side") not in ("LONG", "SHORT"):
            raise ValueError("Invalid position identity")
        for key in ("entry", "stop", "target", "quantity", "entry_time"):
            if not finite(pos.get(key)) or pos[key] <= 0:
                raise ValueError(f"Invalid position {key}")
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
    if path not in {"/api/v2/mix/market/tickers", "/api/v2/mix/market/candles"}:
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
    delta = close.diff()
    gain, loss = delta.clip(lower=0).rolling(14).mean(), -delta.clip(upper=0).rolling(14).mean()
    ratio = gain / loss.where(loss != 0)
    frame["rsi"] = 100 - 100 / (1 + ratio)
    frame.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    frame.loc[(loss == 0) & (gain == 0), "rsi"] = 50
    previous = close.shift()
    tr = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous).abs(),
                    (frame["low"] - previous).abs()], axis=1).max(axis=1)
    frame["atr"] = tr.rolling(14).mean()
    mid, std = close.rolling(20).mean(), close.rolling(20).std(ddof=0)
    net = (close - close.shift(20)).abs()
    path = close.diff().abs().rolling(20).sum()
    frame["er"] = net / path.replace(0, float("nan"))
    frame["bb_mid"], frame["bb_upper"], frame["bb_lower"] = mid, mid + 2 * std, mid - 2 * std
    return frame


def analyze(row):
    """Return (side, strength, reason). Strength = how stretched RSI is."""
    required = ["rsi", "atr", "bb_mid", "bb_upper", "bb_lower", "er"]
    if any(not finite(float(row[key])) for key in required):
        return None, 0.0, "indicator warm-up"
    if not 0.002 <= row["atr"] / row["close"] <= 0.08:
        return None, 0.0, "volatility outside filter"
    if row["er"] > ER_MAX:
        return None, 0.0, "market trending, not choppy"
    if row["close"] < row["bb_lower"] and row["rsi"] < RSI_LOW:
        return "LONG", RSI_LOW - row["rsi"], "closed below lower Bollinger band, RSI oversold, aiming back to the 20-bar mean"
    if row["close"] > row["bb_upper"] and row["rsi"] > RSI_HIGH:
        return "SHORT", row["rsi"] - RSI_HIGH, "closed above upper Bollinger band, RSI overbought, aiming back to the 20-bar mean"
    return None, 0.0, "price not stretched"


def open_position(state, symbol, signal_row, raw_price, entry_time, fee=FEE, slippage=SLIPPAGE):
    side, strength, reason = analyze(signal_row)
    if (not side or state["balance"] <= 0 or len(state["positions"]) >= MAX_OPEN
            or any(p["symbol"] == symbol for p in state["positions"])):
        return False
    direction = 1 if side == "LONG" else -1
    entry = raw_price * (1 + direction * slippage)
    stop_distance = max(STOP_ATR * float(signal_row["atr"]), entry * 0.005)
    target = float(signal_row["bb_mid"])
    target_distance = direction * (target - entry)
    if target_distance <= 0 or target_distance < MIN_RR * stop_distance:
        return False
    stop = entry - direction * stop_distance
    if stop <= 0:
        return False
    risk_per_unit = stop_distance + entry * (2 * fee + slippage)
    quantity = min(state["balance"] * RISK / risk_per_unit, state["balance"] / entry)
    if quantity <= 0:
        return False
    entry_fee = quantity * entry * fee
    state["balance"] -= entry_fee
    state["positions"].append({"symbol": symbol, "side": side, "entry": entry, "stop": stop,
        "target": target, "quantity": quantity, "entry_fee": entry_fee,
        "entry_time": int(entry_time), "fee_rate": fee, "slippage_rate": slippage,
        "strategy": "mean_reversion_v1", "analysis": reason,
        "entry_rsi": float(signal_row["rsi"]), "reward_to_risk": target_distance / stop_distance})
    return True


def close_position(state, pos, raw_price, reason, candle_time):
    direction = 1 if pos["side"] == "LONG" else -1
    price = raw_price * (1 - direction * pos.get("slippage_rate", SLIPPAGE))
    gross = (price - pos["entry"]) * pos["quantity"] * direction
    exit_fee = price * pos["quantity"] * pos.get("fee_rate", FEE)
    state["balance"] += gross - exit_fee  # entry fee was deducted at entry
    state["history"].append({"symbol": pos["symbol"], "side": pos["side"],
        "entry": pos["entry"], "exit": price, "quantity": pos["quantity"],
        "gross_pnl": gross, "entry_fee": pos["entry_fee"], "exit_fee": exit_fee,
        "net_pnl": gross - exit_fee - pos["entry_fee"], "reason": reason,
        "entry_reason": pos.get("analysis", ""), "entry_time": pos["entry_time"],
        "exit_time": int(candle_time)})


def manage_bar(state, symbol, bar):
    remaining = []
    for pos in state["positions"]:
        if pos["symbol"] != symbol or bar["time"] < pos["entry_time"]:
            remaining.append(pos)
            continue
        direction = 1 if pos["side"] == "LONG" else -1
        stop, target, opening = pos["stop"], pos["target"], float(bar["open"])
        if direction * (opening - stop) <= 0:
            raw_price, reason = opening, "STOP GAP"
        elif direction * (opening - target) >= 0:
            raw_price, reason = target, "TARGET (MEAN)"
        elif (bar["low"] <= stop if direction == 1 else bar["high"] >= stop):
            raw_price, reason = stop, "STOP LOSS"  # stop wins if both touched
        elif (bar["high"] >= target if direction == 1 else bar["low"] <= target):
            raw_price, reason = target, "TARGET (MEAN)"
        elif bar["time"] + HOUR >= pos["entry_time"] + MAX_HOLD_BARS * HOUR:
            raw_price, reason = float(bar["close"]), "TIME EXIT"
        else:
            remaining.append(pos)
            continue
        close_position(state, pos, raw_price, reason, int(bar["time"]))
    state["positions"] = remaining


def equity(state, prices):
    return state["balance"] + sum((prices.get(p["symbol"], p["entry"]) - p["entry"])
        * p["quantity"] * (1 if p["side"] == "LONG" else -1) for p in state["positions"])


def metrics(state, curve):
    pnl = [t["net_pnl"] for t in state["history"]]
    wins, losses = sum(p > 0 for p in pnl), sum(p <= 0 for p in pnl)
    profit, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    peak, drawdown = (curve[0] if curve else START_BALANCE), 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak > 0 else 0)
    return {"strategy": "mean_reversion_v1", "balance": state["balance"],
        "equity": curve[-1] if curve else state["balance"], "closed_trades": len(pnl),
        "wins": wins, "losses": losses, "win_rate_pct": 100 * wins / len(pnl) if pnl else None,
        "net_realized_pnl": sum(pnl), "profit_factor": profit / loss if loss else None,
        "max_drawdown_pct": drawdown * 100, "open_positions": len(state["positions"])}


def run_cycle(state, frames):
    """Replay unseen closed bars for exits; open new trades only on the newest bar."""
    state = copy.deepcopy(state)
    checkpoints = state.setdefault("processed_until", {})
    prices = dict(state.get("last_prices", {}))
    warnings, analyses, candidates, stamps = [], [], [], []
    for symbol, raw_frame in frames.items():
        frame = prepare(raw_frame)
        if len(frame) < MIN_BARS + 1 or not frame["time"].diff().dropna().eq(HOUR).all():
            warnings.append(f"{symbol}: insufficient or non-contiguous candles; skipped")
            continue
        stamp = int(frame.iloc[-1]["time"])
        last = checkpoints.get(symbol, stamp - 1)
        held = [p for p in state["positions"] if p["symbol"] == symbol]
        if held and symbol in checkpoints and last < stamp - HOUR and int(frame.iloc[0]["time"]) > last + HOUR:
            warnings.append(f"{symbol}: missing bars since checkpoint; position left untouched")
            continue
        for idx in frame[frame["time"] > last].index:
            if idx >= 1:
                manage_bar(state, symbol, frame.loc[idx])
        prices[symbol] = float(frame.iloc[-1]["close"])
        checkpoints[symbol] = stamp
        stamps.append(stamp)
        side, strength, why = analyze(frame.iloc[-1])
        analyses.append({"symbol": symbol, "side": side or "WAIT", "stretch": round(float(strength), 2), "reason": why})
        # A signal on the newest closed bar would be entered at the NEXT open, so
        # this cycle enters at the newest bar's own open using the PRIOR bar.
        sig_side, sig_strength, _ = analyze(frame.iloc[-2])
        if sig_side and last < stamp:
            candidates.append((sig_strength, symbol, frame.iloc[-2], float(frame.iloc[-1]["open"]), stamp))
    for _, symbol, signal, price, stamp in sorted(candidates, key=lambda c: -c[0]):
        open_position(state, symbol, signal, price, stamp)
        # opened trade has not yet seen its own entry bar's range; next cycle handles it
    curve = list(state.get("equity_curve", []))
    if stamps:
        mark = max(stamps) + HOUR
        if not curve or curve[-1]["time"] < mark:
            curve.append({"time": mark, "equity": equity(state, prices)})
    state["equity_curve"], state["last_prices"] = curve, prices
    report = metrics(state, [START_BALANCE] + [p["equity"] for p in curve])
    report.update({"paper_only": True, "warnings": warnings, "analysis": analyses,
        "open": state["positions"],
        "note": "Sampled hourly. Paper simulation only. No real orders. No profit guarantee."})
    return state, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--report", default=REPORT_FILE)
    parser.add_argument("--max-coins", type=int, default=MAX_COINS)
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
    frames, now_ms = {}, int(time.time() * 1000)
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
    save_state(updated, args.state)
    Path(args.report).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print("Mean-reversion paper bot. Balance %.4f, equity %.4f, closed %d, open %d, warnings %d. No real orders."
          % (report["balance"], report["equity"], report["closed_trades"], report["open_positions"], len(report["warnings"])))


if __name__ == "__main__":
    main()
