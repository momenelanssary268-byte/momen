
import requests
import pandas as pd
import json
import os
import time

BASE = "https://api.bitget.com"
START_BALANCE = 20.0
RISK = 0.01
FEE = 0.0006
MAX_COINS = 30
MAX_OPEN = 3
STATE_FILE = "paper_state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {
        "balance": START_BALANCE,
        "positions": [],
        "history": [],
        "last_signal": {}
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_symbols():
    url = BASE + "/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    data.sort(
        key=lambda x: float(x.get("quoteVolume", 0)),
        reverse=True
    )
    return [
        x["symbol"] for x in data
        if x["symbol"].endswith("USDT")
    ][:MAX_COINS]

def get_candles(symbol):
    url = BASE + "/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "granularity": "1H",
        "limit": "100",
        "productType": "USDT-FUTURES"
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    rows = r.json().get("data", [])

    if len(rows) < 60:
        return None

    df = pd.DataFrame(rows)
    df = df.iloc[:, :7]
    df.columns = [
        "time", "open", "high", "low",
        "close", "volume", "quote"
    ]

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col])

    df["time"] = pd.to_numeric(df["time"])
    return df.sort_values("time").reset_index(drop=True)

def prepare(df):
    close = df["close"]
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df

def close_position(state, pos, price, reason, candle_time):
    direction = 1 if pos["side"] == "LONG" else -1
    gross = (
        (price - pos["entry"]) *
        pos["quantity"] * direction
    )
    exit_fee = price * pos["quantity"] * FEE
    net = gross - exit_fee

    state["balance"] += gross - exit_fee

    state["history"].append({
        "symbol": pos["symbol"],
        "side": pos["side"],
        "entry": pos["entry"],
        "exit": price,
        "net_pnl": round(net, 5),
        "reason": reason,
        "entry_time": pos["entry_time"],
        "exit_time": candle_time
    })

    print(
        f"CLOSED {pos['symbol']} {pos['side']} "
        f"{reason} NET=${net:.4f}"
    )

def manage_positions(state, symbol, df):
    remaining = []

    for pos in state["positions"]:
        if pos["symbol"] != symbol:
            remaining.append(pos)
            continue

        closed = False

        new_candles = df[df["time"] > pos["entry_time"]]

        for _, candle in new_candles.iterrows():
            high = float(candle["high"])
            low = float(candle["low"])
            exit_price = None
            reason = None

            if pos["side"] == "LONG":
                if low <= pos["stop"]:
                    exit_price = pos["stop"]
                    reason = "STOP LOSS"
                elif high >= pos["target"]:
                    exit_price = pos["target"]
                    reason = "TAKE PROFIT"
            else:
                if high >= pos["stop"]:
                    exit_price = pos["stop"]
                    reason = "STOP LOSS"
                elif low <= pos["target"]:
                    exit_price = pos["target"]
                    reason = "TAKE PROFIT"

            if exit_price is not None:
                close_position(
                    state, pos, exit_price,
                    reason, int(candle["time"])
                )
                closed = True
                break

        if not closed:
            remaining.append(pos)

    state["positions"] = remaining

def open_signal(state, symbol, df):
    row = df.iloc[-2]
    candle_time = int(row["time"])

    if state["last_signal"].get(symbol) == candle_time:
        return

    if any(p["symbol"] == symbol for p in state["positions"]):
        return

    if len(state["positions"]) >= MAX_OPEN:
        return

    fast = float(row["ema20"])
    slow = float(row["ema50"])
    rsi = float(row["rsi"])
    price = float(row["close"])

    if fast > slow and 50 < rsi < 68:
        side = "LONG"
    elif fast < slow and 32 < rsi < 50:
        side = "SHORT"
    else:
        return

    risk_cash = state["balance"] * RISK
    stop = price * (0.99 if side == "LONG" else 1.01)
    target = price * (1.02 if side == "LONG" else 0.98)

    quantity = risk_cash / abs(price - stop)
    entry_fee = price * quantity * FEE

    if entry_fee >= state["balance"]:
        return

    state["balance"] -= entry_fee

    state["positions"].append({
        "symbol": symbol,
        "side": side,
        "entry": price,
        "stop": stop,
        "target": target,
        "quantity": quantity,
        "entry_fee": entry_fee,
        "entry_time": candle_time
    })

    state["last_signal"][symbol] = candle_time

    print(
        f"OPEN {symbol} {side} "
        f"ENTRY={price:.8f} "
        f"STOP={stop:.8f} TARGET={target:.8f}"
    )

def main():
    state = load_state()

    print("===== MOMEN AI TRADER PAPER V4 =====")
    print("REAL MONEY: DISABLED")
    print(f"Balance: ${state['balance']:.4f}")

    symbols = get_symbols()
    print(f"Watching {len(symbols)} coins")

    for symbol in symbols:
        try:
            df = get_candles(symbol)
            if df is None:
                continue

            df = prepare(df)

            # Use completed candles only
            manage_positions(state, symbol, df)
            open_signal(state, symbol, df)

        except Exception as e:
            print(f"ERROR {symbol}: {e}")

        time.sleep(0.2)

    save_state(state)

    wins = sum(
        1 for t in state["history"]
        if t["net_pnl"] > 0
    )
    losses = sum(
        1 for t in state["history"]
        if t["net_pnl"] <= 0
    )

    print("\n===== PAPER REPORT =====")
    print(f"Balance: ${state['balance']:.4f}")
    print(f"Closed trades: {len(state['history'])}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Open positions: {len(state['positions'])}")
    print("State saved. No real orders placed.")

if __name__ == "__main__":
    main()
