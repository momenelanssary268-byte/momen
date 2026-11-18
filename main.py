
import json
import urllib.request
import urllib.parse
import time

# =================================
# MOMEN AI TRADER - VERSION 1.0
# Public market analysis - Bitget
# =================================

BASE_URL = "https://api.bitget.com"

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT"
]

TIMEFRAME = "1H"


def get_market_data(symbol):
    params = {
        "productType": "USDT-FUTURES",
        "symbol": symbol,
        "granularity": TIMEFRAME,
        "limit": "100"
    }

    url = BASE_URL + "/api/v2/mix/market/candles?"
    url += urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Momen-AI-Trader"}
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode())

    if result.get("code") != "00000":
        raise Exception(result.get("msg", "API error"))

    candles = result.get("data", [])

    candles.sort(key=lambda candle: int(candle[0]))

    return [float(candle[4]) for candle in candles]


def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = prices[0]

    for price in prices[1:]:
        ema = (price * multiplier) + (
            ema * (1 - multiplier)
        )

    return ema


def calculate_rsi(prices, period=14):
    changes = [
        prices[i] - prices[i - 1]
        for i in range(1, len(prices))
    ]

    recent = changes[-period:]

    gains = [max(change, 0) for change in recent]
    losses = [max(-change, 0) for change in recent]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def analyze_coin(symbol):
    prices = get_market_data(symbol)

    if len(prices) < 60:
        raise Exception("Not enough market data")

    ema20 = calculate_ema(prices[-60:], 20)
    ema50 = calculate_ema(prices[-60:], 50)

    rsi = calculate_rsi(prices)

    current_price = prices[-1]

    if current_price > ema20 > ema50 and 50 < rsi < 70:
        signal = "LONG"

    elif current_price < ema20 < ema50 and 30 < rsi < 50:
        signal = "SHORT"

    else:
        signal = "WAIT"

    return {
        "coin": symbol,
        "price": round(current_price, 6),
        "EMA20": round(ema20, 6),
        "EMA50": round(ema50, 6),
        "RSI": round(rsi, 2),
        "signal": signal
    }


def main():
    print("=" * 40)
    print("       MOMEN AI TRADER")
    print("       MARKET ANALYZER 1.0")
    print("=" * 40)

    for coin in COINS:
        try:
            result = analyze_coin(coin)

            print("\nCoin:", result["coin"])
            print("Price:", result["price"])
            print("EMA20:", result["EMA20"])
            print("EMA50:", result["EMA50"])
            print("RSI:", result["RSI"])
            print("SIGNAL:", result["signal"])

            print("-" * 30)

        except Exception as error:
            print("\n", coin, "ERROR:", str(error))

    print("\nAnalysis completed.")
    print("No real trades were executed.")


if __name__ == "__main__":
    main()
