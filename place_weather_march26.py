"""
place_weather_march26.py — Weather trades for March 26-27
Run via executor: run_script → place_weather_march26.py

Source: ECMWF 50-member ensemble with confidence cap (90%) + seasonal tiers
"""

import os, sys, requests
from dotenv import load_dotenv

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env)

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set"); sys.exit(1)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY

HOST  = "https://clob.polymarket.com"
CHAIN = 137

TRADES = [
    # Buenos Aires 23°C Mar 26 — Tier C (coastal), ensemble 92% capped, 6.7¢, 15x
    {"label": "Buenos Aires 23°C — March 26", "token_id": "96732298163753490945367561469159745509025369263725007166898844600694018005352",
     "question": "Will the highest temperature in Buenos Aires be 23°C on March 26?", "prob": 0.92, "size": 10.0},
    # Sao Paulo 30°C Mar 27 — Tier C (tropical), ensemble 68%, 22¢
    {"label": "Sao Paulo 30°C — March 27", "token_id": "11198052698663631362250024462703754970929829390294083191313471913426292248950",
     "question": "Will the highest temperature in Sao Paulo be 30°C on March 27?", "prob": 0.68, "size": 10.0},
    # Chicago 36-37°F Mar 27 — Tier A (inland), ensemble 44%, 8¢ — asymmetric
    {"label": "Chicago 36-37°F — March 27", "token_id": "25854574802648179882434204018357252542324428036104557237434935399316618241979",
     "question": "Will the highest temperature in Chicago be between 36-37°F on March 27?", "prob": 0.44, "size": 10.0},
]

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def get_client():
    creds = ApiCreds(
        api_key=os.environ.get("CLOB_API_KEY",""),
        api_secret=os.environ.get("CLOB_API_SECRET",""),
        api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""),
    )
    return ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN,
                      creds=creds, signature_type=SIG_TYPE, funder=FUNDER)

def approve_allowance(client):
    try:
        client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    except Exception as e:
        print(f"  Allowance: {e}")

def place_trade(client, trade):
    token_id = trade["token_id"]
    size_usdc = trade["size"]
    prob = trade["prob"]
    label = trade["label"]

    print(f"\n── {label} ──")
    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=8)
    mid = float(r.json().get("mid", 0.10)) if r.status_code == 200 else 0.10
    edge = prob - mid
    print(f"   Ensemble: {prob:.0%} | Mid: {mid:.3f} | Edge: {edge:+.3f} | Size: ${size_usdc:.0f}")
    if edge < 0.10:
        print(f"   SKIP: edge too low")
        return False

    try:
        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price    = round(round(mid / tick_f) * tick_f, tick_dec)
        price    = max(0.01, min(0.99, price))
        shares   = round(size_usdc / price, 2)

        args    = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            payout = shares * 1.0
            print(f"   ✓ BUY {shares} shares @ {price:.3f} | payout ${payout:.0f} | {receipt.get('orderID','')[:12]}")
            tg(f"🌡️ <b>Weather</b> {label}\n{prob:.0%} | {mid:.3f} → {edge:+.3f}\n${size_usdc:.0f} → ${payout:.0f}")
            return True
        else:
            err = receipt.get('errorMsg','')
            print(f"   ✗ {err}")
            if 'allowance' in err.lower():
                approve_allowance(client)
                signed2 = client.create_order(args, options)
                receipt2 = client.post_order(signed2, OrderType.GTC)
                if receipt2.get("success"):
                    print(f"   ✓ Retry succeeded")
                    return True
            return False
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False

def main():
    total = sum(t["size"] for t in TRADES)
    print(f"{'='*55}\n  WEATHER TRADES Mar 26-27 | ${total:.0f} total\n{'='*55}")

    client = get_client()
    if not os.environ.get("CLOB_API_KEY"):
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"Creds: {e}")

    approve_allowance(client)

    placed = 0
    deployed = 0.0
    for trade in TRADES:
        if place_trade(client, trade):
            placed += 1
            deployed += trade["size"]

    print(f"\n{'='*55}\n  Done: {placed}/{len(TRADES)} | ${deployed:.0f} deployed\n{'='*55}")
    if placed > 0:
        tg(f"✅ <b>Weather {placed}/{len(TRADES)} placed (${deployed:.0f})</b>")

if __name__ == "__main__":
    main()
