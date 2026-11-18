
import requests
import pandas as pd
import time

BASE_URL = "https://api.bitget.com"
START_BALANCE = 20.0
RISK_PERCENT = 0.01
TIMEFRAME = "1H"
MAX_COINS = 30

def get_symbols():
    url = BASE_URL + "/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    data = requests.get(url, params=params, timeout=15).json()
    symbols = data.get("data", [])
    return [
        item["symbol"] for item in symbols
        if item.get("symbol", "").endswith("USDT")
    ][:MAX_COINS]

def get_candles(symbol):
    url = BASE_URL + "/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "granularity": TIMEFRAME,
        "limit": "100",
        "productType": "USDT-FUTURES"
    }
    response = requests.get(url, params=params, timeout=15).json()
    rows = response.get("data", [])

    if not rows:
        return None

    df = pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "volume", "quote"]
    )
    df["close"] = pd.to_numeric(df["close"])
    return df.sort_values("time").reset_index(drop=True)

def analyze(symbol):
    try:
        df = get_candles(symbol)
        if df is None or len(df) < 55:
            return None

        close = df["close"]
        ema20 = close.ewm(span=20).mean()
        ema50 = close.ewm(span=50).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        price = float(close.iloc[-1])
        fast = float(ema20.iloc[-1])
        slow = float(ema50.iloc[-1])
        current_rsi = float(rsi.iloc[-1])

        if fast > slow and 50 < current_rsi < 68:
            direction = "LONG"
        elif fast < slow and 32 < current_rsi < 50:
            direction = "SHORT"
        else:
            return None

        stop_distance = 0.01
        stop = price * (1 - stop_distance) if direction == "LONG" else price * (1 + stop_distance)
        target = price * (1 + 0.02) if direction == "LONG" else price * (1 - 0.02)

        risk_amount = START_BALANCE * RISK_PERCENT
        position_size = risk_amount / abs(price - stop)

        return {
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "stop": stop,
            "target": target,
            "quantity": position_size,
            "risk": risk_amount,
            "rsi": current_rsi
        }

    except Exception as error:
        print(f"Error {symbol}: {error}")
        return None

def main():
    print("===== MOMEN AI TRADER V2 =====")
    print("Mode: PAPER TRADING - NO REAL ORDERS")
    print(f"Virtual balance: ${START_BALANCE:.2f}")
    print("Scanning market...\n")

    symbols = get_symbols()
    print(f"Watching {len(symbols)} symbols\n")

    opportunities = 0

    for symbol in symbols:
        result = analyze(symbol)
        if result:
            opportunities += 1
            print("-" * 35)
            print(f"COIN: {result['symbol']}")
            print(f"SIGNAL: {result['direction']}")
            print(f"PRICE: {result['price']:.8f}")
            print(f"STOP LOSS: {result['stop']:.8f}")
            print(f"TAKE PROFIT: {result['target']:.8f}")
            print(f"POSITION SIZE: {result['quantity']:.6f}")
            print(f"RISK: ${result['risk']:.2f}")
            print(f"RSI: {result['rsi']:.2f}")
        time.sleep(0.2)

    print("\n===== SCAN COMPLETE =====")
    print(f"Qualified opportunities: {opportunities}")
    print("No real trades were placed.")

if __name__ == "__main__":
    main()
