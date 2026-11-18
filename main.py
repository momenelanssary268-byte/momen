
import requests
import pandas as pd
import time

BASE = "https://api.bitget.com"
START_BALANCE = 20.0
RISK = 0.01
MAX_COINS = 30
FEE_RATE = 0.001

def get_symbols():
    url = BASE + "/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    data.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    return [
        x["symbol"] for x in data
        if x["symbol"].endswith("USDT")
    ][:MAX_COINS]

def get_candles(symbol):
    url = BASE + "/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "granularity": "1H",
        "limit": "200",
        "productType": "USDT-FUTURES"
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    rows = r.json().get("data", [])

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.iloc[:, :7]
    df.columns = [
        "time", "open", "high", "low",
        "close", "volume", "quote"
    ]

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col])

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

def simulate_coin(symbol):
    df = get_candles(symbol)
    if df is None or len(df) < 60:
        return []

    df = prepare(df)
    trades = []
    i = 55

    while i < len(df) - 1:
        row = df.iloc[i]

        direction = None
        if row["ema20"] > row["ema50"] and 50 < row["rsi"] < 68:
            direction = "LONG"
        elif row["ema20"] < row["ema50"] and 32 < row["rsi"] < 50:
            direction = "SHORT"

        if direction is None:
            i += 1
            continue

        entry = float(row["close"])
        stop = entry * (0.99 if direction == "LONG" else 1.01)
        target = entry * (1.02 if direction == "LONG" else 0.98)

        outcome = None
        exit_time = None

        for j in range(i + 1, len(df)):
            candle = df.iloc[j]
            high = float(candle["high"])
            low = float(candle["low"])

            if direction == "LONG":
                hit_stop = low <= stop
                hit_target = high >= target
            else:
                hit_stop = high >= stop
                hit_target = low <= target

            # Conservative assumption if both levels are hit
            if hit_stop:
                outcome = -1
                exit_time = int(candle["time"])
                i = j + 1
                break
            if hit_target:
                outcome = 2
                exit_time = int(candle["time"])
                i = j + 1
                break

        if outcome is not None:
            trades.append({
                "symbol": symbol,
                "direction": direction,
                "entry_time": int(row["time"]),
                "exit_time": exit_time,
                "entry": entry,
                "result_r": outcome
            })
        else:
            break

    return trades

def main():
    print("===== MOMEN AI TRADER V3 =====")
    print("HISTORICAL PAPER TRADING ONLY")
    print(f"Starting balance: ${START_BALANCE:.2f}")

    symbols = get_symbols()
    all_trades = []

    print(f"Scanning {len(symbols)} coins...")

    for symbol in symbols:
        try:
            trades = simulate_coin(symbol)
            all_trades.extend(trades)
            print(f"{symbol}: {len(trades)} simulated trades")
        except Exception as e:
            print(f"{symbol}: ERROR - {e}")
        time.sleep(0.15)

    all_trades.sort(key=lambda x: x["entry_time"])

    balance = START_BALANCE
    last_exit = 0
    wins = 0
    losses = 0

    print("\n===== TRADE RESULTS =====")

    for trade in all_trades:
        if trade["entry_time"] < last_exit:
            continue

        risk_amount = balance * RISK
        pnl = risk_amount * trade["result_r"]
        fee = balance * 0  # Fees are not included in this simulation
        balance += pnl - fee
        last_exit = trade["exit_time"]

        if pnl > 0:
            wins += 1
            status = "WIN"
        else:
            losses += 1
            status = "LOSS"

        print(
            f"{trade['symbol']} | {trade['direction']} | "
            f"{status} | P/L: ${pnl:.3f} | "
            f"Balance: ${balance:.3f}"
        )

    total = wins + losses
    print("\n===== FINAL REPORT =====")
    print(f"Starting balance: ${START_BALANCE:.2f}")
    print(f"Final simulated balance: ${balance:.3f}")
    print(f"Trades: {total}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Win rate: {(wins / total * 100):.1f}%" if total else "Win rate: N/A")
    print("No real orders were placed.")

if __name__ == "__main__":
    main()
