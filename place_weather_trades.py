"""
place_weather_trades.py — Manual execution for 3 weather plays
Run: curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/place_weather_trades.py | /opt/polymarket-agent/venv/bin/python3

Trades:
  1. Buenos Aires 23°C on March 25 — forecast 24.5°C, market 14¢, edge +45%
  2. Singapore 34°C on March 23  — forecast 34.8°C, market 22.5¢, edge +41%
  3. Paris 10°C on March 25       — forecast 11.8°C, market 13.5¢, edge +39%
"""

import os, sys, json, requests
from dotenv import load_dotenv

# Load .env from same directory as this script
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env)

PRIVATE_KEY   = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER        = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIG_TYPE      = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set"); sys.exit(1)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

HOST    = "https://clob.polymarket.com"
CHAIN   = 137

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def get_client():
    creds = ApiCreds(
        api_key    = os.environ.get("CLOB_API_KEY", ""),
        api_secret = os.environ.get("CLOB_API_SECRET", ""),
        api_passphrase = os.environ.get("CLOB_API_PASSPHRASE", ""),
    )
    return ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN,
                      creds=creds, signature_type=SIG_TYPE,
                      funder=FUNDER)

def get_or_derive_creds(client):
    """Get or derive API credentials."""
    if not os.environ.get("CLOB_API_KEY"):
        print("Deriving API credentials...")
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            print("Credentials derived OK")
        except Exception as e:
            print(f"Could not derive creds: {e}")
    return client

TRADES = [
    {
        "label":    "Buenos Aires 23°C — March 25",
        "question": "Will the highest temperature in Buenos Aires be 23°C on March 25?",
        "token_id": "88865497438695860917235935601931058876759327995306357873974440575667807545564",
        "forecast": "24.5°C (forecast high) vs 23°C bucket",
        "prob":     0.59,
        "size":     20.0,   # USDC
    },
    {
        "label":    "Singapore 34°C — March 23",
        "question": "Will the highest temperature in Singapore be 34°C on March 23?",
        "token_id": "20704999603093763913024394877585406781636008139754265319685830253339896389055",
        "forecast": "34.8°C (forecast high) vs 34°C bucket",
        "prob":     0.63,
        "size":     20.0,
    },
    {
        "label":    "Paris 10°C — March 25",
        "question": "Will the highest temperature in Paris be 10°C on March 25?",
        "token_id": "101861364181237654420861695566508108627277070385347345504636593793881138512887",
        "forecast": "11.8°C (forecast high) vs 10°C bucket",
        "prob":     0.52,
        "size":     20.0,
    },
]

def place_trade(client, trade):
    token_id = trade["token_id"]
    size_usdc = trade["size"]
    label     = trade["label"]

    print(f"\n── {label} ──")
    print(f"   {trade['forecast']} | prob={trade['prob']:.0%} | size=${size_usdc:.0f}")

    try:
        # Get current mid price
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=8)
        mid = float(r.json().get("mid", 0.15)) if r.status_code == 200 else 0.15
        print(f"   Market mid: {mid:.3f}")

        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0

        # Buy at mid (limit order — better than market slippage)
        price  = round(round(mid / tick_f) * tick_f, tick_dec)
        price  = max(0.01, min(0.99, price))
        shares = round(size_usdc / price, 2)

        args    = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            print(f"   ✓ BUY {shares} shares @ {price:.3f} = ${size_usdc:.0f} | order={receipt.get('orderID','')[:12]}")
            tg(f"🌡️ <b>Weather trade placed</b>\n{trade['question']}\n{trade['forecast']}\nProb: {trade['prob']:.0%} | Price: {price:.3f} | Size: ${size_usdc:.0f}")
            return True
        else:
            err = receipt.get("errorMsg", str(receipt))
            print(f"   ✗ Order failed: {err}")
            return False

    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback; traceback.print_exc()
        return False

def main():
    print("=" * 55)
    print("  WEATHER TRADE EXECUTION — 3 plays")
    print("=" * 55)

    client = get_client()
    client = get_or_derive_creds(client)

    placed = 0
    for trade in TRADES:
        if place_trade(client, trade):
            placed += 1

    print(f"\n{'='*55}")
    print(f"  Done: {placed}/{len(TRADES)} trades placed")
    print(f"  Total USDC deployed: ${placed * 20:.0f}")
    print("=" * 55)

    if placed > 0:
        tg(f"✅ <b>Weather trades complete</b>: {placed}/3 placed (${placed*20} deployed)\n"
           f"Buenos Aires 23°C Mar25 + Singapore 34°C Mar23 + Paris 10°C Mar25")

if __name__ == "__main__":
    main()
