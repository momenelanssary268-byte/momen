"""Bybit DEMO-ONLY executor. Virtual funds. Never touches real money.

This module mirrors the paper simulator's signals and places equivalent
orders on Bybit's *demo trading* account (virtual funds, real prices).
It exists so order execution can be rehearsed without risk.

Hard safety rules enforced here, in code:

1. The ONLY host this module will ever contact is https://api-demo.bybit.com
   (Bybit mainnet demo trading). There is no setting, environment variable
   or argument that can point it anywhere else. Any other URL raises before
   a request is sent.
2. Bybit demo API keys are created inside Bybit's Demo Trading mode and are
   rejected by the real trading API. Even a leaked demo key cannot reach
   real funds.
3. It reads its credentials only from the BYBIT_DEMO_API_KEY and
   BYBIT_DEMO_API_SECRET environment variables (GitHub Actions secrets).
   Keys must never be committed or pasted into chat.
4. It keeps its own state file (demo_state.json). It never opens, imports
   or modifies the paper simulator's paper_state.json.
5. Without demo keys it exits successfully with a "skipped" report so the
   workflow stays green until the owner adds secrets.

Demo trading docs: https://bybit-exchange.github.io/docs/v5/demo
This is research software, not financial advice. No profit is promised.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import requests
import pandas as pd

from main import analyze, prepare

DEMO_HOST = "https://api-demo.bybit.com"
RECV_WINDOW = "5000"
STATE_FILE = "demo_state.json"
REPORT_FILE = "demo_report.json"
MAX_COINS = 60
LEVERAGE = Decimal("5")
RISK_PER_TRADE = Decimal("0.005")
MAX_PORTFOLIO_RISK = Decimal("0.04")
MAX_MARGIN_USE = Decimal("0.60")
MIN_NOTIONAL_USDT = Decimal("10")
SYMBOL_SUFFIX = "USDT"


class DemoSafetyError(RuntimeError):
    """Raised before any request that is not provably demo-only."""


def assert_demo_url(url):
    """Reject anything that is not exactly Bybit's demo trading host."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api-demo.bybit.com":
        raise DemoSafetyError(
            f"Refusing non-demo URL: {url!r}. This executor only talks to "
            "https://api-demo.bybit.com (Bybit demo trading, virtual funds).")
    return url


def require_demo_keys(env=None):
    """Return (api_key, api_secret) or raise SystemExit with a skip message."""
    env = os.environ if env is None else env
    key = (env.get("BYBIT_DEMO_API_KEY") or "").strip()
    secret = (env.get("BYBIT_DEMO_API_SECRET") or "").strip()
    if not key or not secret:
        raise SystemExit(
            "No demo keys found. Set GitHub secrets BYBIT_DEMO_API_KEY and "
            "BYBIT_DEMO_API_SECRET from a Bybit DEMO TRADING account "
            "(avatar > API inside Demo Trading mode). Skipping; nothing ran.")
    return key, secret


def sign_payload(api_secret, timestamp, api_key, recv_window, payload):
    """Bybit v5 HMAC signature over timestamp+key+recv_window+payload."""
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def demo_request(method, path, payload=None, query=None, env=None):
    """One signed request to the demo host. The guard runs every call."""
    key, secret = require_demo_keys(env)
    url = assert_demo_url(DEMO_HOST + path)
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    timestamp = str(int(time.time() * 1000))
    signed = sign_payload(secret, timestamp, key, RECV_WINDOW, body if method == "POST" else
                          "&".join(f"{k}={v}" for k, v in (query or {}).items()))
    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": signed,
        "Content-Type": "application/json",
    }
    if method == "POST":
        response = requests.post(assert_demo_url(url), data=body, headers=headers, timeout=20)
    else:
        response = requests.get(assert_demo_url(url), params=query, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit demo rejected {path}: {data.get('retCode')} {data.get('retMsg')}")
    return data.get("result", {})


def round_down(value, step):
    """Round a Decimal down to the exchange's quantity/price step."""
    value, step = Decimal(str(value)), Decimal(str(step))
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def plain(number):
    """Format a Decimal without scientific notation for the API."""
    return format(number.normalize(), "f")



def bybit_symbols(limit=MAX_COINS):
    """Rank Bybit linear USDT contracts by 24h turnover."""
    response = requests.get(DEMO_HOST + "/v5/market/tickers",
                            params={"category": "linear"}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit tickers failed: {payload.get('retMsg')}")
    ranked = []
    for row in payload.get("result", {}).get("list", []):
        symbol = row.get("symbol", "")
        try:
            turnover = Decimal(row.get("turnover24h", "0"))
            if symbol.endswith(SYMBOL_SUFFIX) and turnover.is_finite() and turnover > 0:
                ranked.append((symbol, turnover))
        except Exception:
            continue
    return [x[0] for x in sorted(ranked, key=lambda item: item[1], reverse=True)[:limit]]


def bybit_candles(symbol, limit=500):
    """Use Bybit hourly candles for Bybit signals and execution."""
    response = requests.get(DEMO_HOST + "/v5/market/kline",
                            params={"category": "linear", "symbol": symbol,
                                    "interval": "60", "limit": str(limit)}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit candles failed: {payload.get('retMsg')}")
    rows = payload.get("result", {}).get("list", [])
    now_ms = int(time.time() * 1000)
    cleaned = []
    for row in reversed(rows):
        stamp = int(row[0])
        if stamp + 3600000 <= now_ms:
            cleaned.append({"time": stamp, "open": float(row[1]), "high": float(row[2]),
                            "low": float(row[3]), "close": float(row[4]),
                            "volume": float(row[5]), "quote": float(row[6])})
    frame = pd.DataFrame(cleaned)
    if frame.empty or not frame["time"].diff().dropna().eq(3600000).all():
        raise ValueError(f"Missing or non-contiguous completed candles for {symbol}")
    return frame


def estimated_position_risk(pos):
    entry = Decimal(str(pos["reference_price"]))
    stop = Decimal(str(pos["stop"]))
    qty = Decimal(str(pos["qty"]))
    return abs(entry - stop) * qty


def instrument_rules(symbol, env=None):
    """Lot size and tick rules for one linear symbol, or None if unlisted."""
    result = demo_request("GET", "/v5/market/instruments-info",
                          query={"category": "linear", "symbol": symbol}, env=env)
    rows = result.get("list") or []
    if not rows:
        return None
    lot, price_filter = rows[0].get("lotSizeFilter", {}), rows[0].get("priceFilter", {})
    return {
        "min_qty": Decimal(lot.get("minOrderQty", "0")),
        "qty_step": Decimal(lot.get("qtyStep", "1")),
        "tick": Decimal(price_filter.get("tickSize", "0.0001")),
    }


def wallet_equity(env=None):
    result = demo_request("GET", "/v5/account/wallet-balance",
                          query={"accountType": "UNIFIED"}, env=env)
    rows = result.get("list") or []
    return rows[0].get("totalEquity") if rows else None


def open_demo_position(symbol, env=None):
    """Live demo position size for a symbol ('0' when flat)."""
    result = demo_request("GET", "/v5/position/list",
                          query={"category": "linear", "symbol": symbol}, env=env)
    rows = result.get("list") or []
    return Decimal(rows[0].get("size", "0")) if rows else Decimal("0")


def build_order(symbol, side, qty, stop, target, link_id):
    """One market order with attached take-profit and stop-loss."""
    return {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy" if side == "LONG" else "Sell",
        "orderType": "Market",
        "qty": plain(qty),
        "positionIdx": 0,
        "orderLinkId": link_id[:36],
        "takeProfit": plain(target),
        "stopLoss": plain(stop),
        "tpslMode": "Full",
    }


def build_prices(side, reference, atr, tick):
    """(stop, target) rounded to the tick, mirroring main.open_position."""
    direction = 1 if side == "LONG" else -1
    distance = max(Decimal("1.5") * Decimal(str(atr)), Decimal(str(reference)) * Decimal("0.005"))
    reference = Decimal(str(reference))
    stop = round_down(reference - direction * distance, tick)
    target = round_down(reference + direction * 2 * distance, tick)
    return stop, target


def load_demo_state(path=STATE_FILE):
    if not Path(path).exists():
        return {"positions": [], "history": [], "last_signal": {}}
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state.get("positions"), list) or not isinstance(state.get("history"), list):
        raise ValueError("Invalid demo state; refusing to continue")
    return state


def save_demo_state(state, path=STATE_FILE):
    Path(path).write_text(json.dumps(state, indent=2, allow_nan=False), encoding="utf-8")


def run(state_path=STATE_FILE, report_path=REPORT_FILE, max_coins=MAX_COINS, env=None):
    report = {"demo_only": True, "virtual_funds": True, "host": DEMO_HOST,
              "opened": [], "closed": [], "warnings": [], "positions": []}
    try:
        require_demo_keys(env)
    except SystemExit as exc:
        report["status"] = "skipped_no_demo_keys"
        report["detail"] = str(exc)
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(report["detail"])
        return report

    state = load_demo_state(state_path)
    report["demo_wallet_equity_usdt"] = wallet_equity(env)
    if report["demo_wallet_equity_usdt"] is None:
        raise RuntimeError("Bybit demo wallet equity unavailable; refusing new orders")
    equity = Decimal(str(report["demo_wallet_equity_usdt"]))
    if not equity.is_finite() or equity <= 0:
        raise RuntimeError("Invalid Bybit demo equity; refusing new orders")

    # Reconcile: exchange-side TP/SL closes positions on its own.
    still_open = []
    for pos in state["positions"]:
        try:
            size = open_demo_position(pos["symbol"], env)
        except (requests.RequestException, RuntimeError) as exc:
            report["warnings"].append(f"{pos['symbol']}: position check failed ({type(exc).__name__})")
            still_open.append(pos)
            continue
        if size == 0:
            state["history"].append({**pos, "exit": "closed by demo exchange TP/SL",
                                     "exit_time": int(time.time() * 1000)})
            report["closed"].append(pos["symbol"])
        else:
            still_open.append(pos)
        time.sleep(0.2)
    state["positions"] = still_open

    held = [p["symbol"] for p in state["positions"]]
    for symbol in list(dict.fromkeys(held + bybit_symbols(max_coins))):
        if symbol in held:
            continue
        try:
            rules = instrument_rules(symbol, env)
        except (requests.RequestException, RuntimeError):
            rules = None  # not listed on Bybit linear (e.g. stock tokens)
        if rules is None:
            continue
        try:
            frame = prepare(bybit_candles(symbol))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            report["warnings"].append(f"{symbol}: market data unavailable ({type(exc).__name__})")
            continue
        row = frame.iloc[-1]
        side, score, reason = analyze(row)
        if not side or state["last_signal"].get(symbol, -1) >= int(row["time"]):
            continue
        reference = Decimal(str(row["close"]))
        stop, target = build_prices(side, reference, row["atr"], rules["tick"])
        stop_distance = abs(reference - stop)
        risk_budget = equity * RISK_PER_TRADE
        qty_by_risk = risk_budget / stop_distance if stop_distance > 0 else Decimal("0")
        qty_by_notional = equity * LEVERAGE * Decimal("0.20") / reference
        qty = round_down(min(qty_by_risk, qty_by_notional), rules["qty_step"])
        notional = qty * reference
        used_margin = sum((Decimal(str(p["qty"])) * Decimal(str(p["reference_price"])) / LEVERAGE)
                          for p in state["positions"])
        portfolio_risk = sum((estimated_position_risk(p) for p in state["positions"]), Decimal("0"))
        if (qty < rules["min_qty"] or notional < MIN_NOTIONAL_USDT
                or used_margin + notional / LEVERAGE > equity * MAX_MARGIN_USE
                or portfolio_risk + qty * stop_distance > equity * MAX_PORTFOLIO_RISK):
            report["warnings"].append(f"{symbol}: dynamic risk/margin/minimum filter; skipped")
            continue
        demo_request("POST", "/v5/position/set-leverage", payload={
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(int(LEVERAGE)), "sellLeverage": str(int(LEVERAGE))}, env=env)
        if side == "LONG" and not (stop < reference < target):
            report["warnings"].append(f"{symbol}: invalid stop/target; skipped")
            continue
        if side == "SHORT" and not (target < reference < stop):
            report["warnings"].append(f"{symbol}: invalid stop/target; skipped")
            continue
        link_id = f"demo-{symbol}-{int(row['time'])}"
        order = build_order(symbol, side, qty, stop, target, link_id)
        try:
            result = demo_request("POST", "/v5/order/create", payload=order, env=env)
        except (requests.RequestException, RuntimeError) as exc:
            report["warnings"].append(f"{symbol}: demo order rejected ({exc})")
            continue
        state["positions"].append({"symbol": symbol, "side": side, "qty": str(qty),
            "reference_price": str(reference), "stop": str(stop), "target": str(target),
            "order_id": result.get("orderId"), "order_link_id": link_id,
            "entry_time": int(row["time"]), "score": score, "analysis": reason})
        state["last_signal"][symbol] = int(row["time"])
        report["opened"].append(symbol)
        time.sleep(0.2)

    report["positions"] = state["positions"]
    report["status"] = "ok"
    save_demo_state(state, state_path)
    Path(report_path).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    summary = ("## Bybit DEMO executor (virtual funds, demo host only)\n\n"
               f"Opened: {report['opened']}\n\nClosed by TP/SL: {report['closed']}\n\n"
               f"Open demo positions: {len(report['positions'])}\n\n"
               f"Demo wallet equity (USDT): {report.get('demo_wallet_equity_usdt')}\n\n"
               "Virtual funds on api-demo.bybit.com. No real money. No profit guarantee.\n")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)
    return report


if __name__ == "__main__":
    run()
