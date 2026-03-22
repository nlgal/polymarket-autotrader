"""
place_wellington_seoul.py — Manual execution: Wellington 20°C + Seoul 16°C+
Run: curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/place_wellington_seoul.py -o /opt/polymarket-agent/place_wellington_seoul.py && /opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/place_wellington_seoul.py

Trades (ECMWF 50-member ensemble probabilities):
  1. Wellington 20°C on March 23  — 98% ensemble prob, 20.5¢ market → edge +77.5%
  2. Seoul 16°C or higher Mar 23  — 96% ensemble prob, 56.5¢ market → edge +39.5%
     (Seoul exceeds normal 35¢ ceiling — justified because ensemble physics, not LLM opinion)
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
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

HOST  = "https://clob.polymarket.com"
CHAIN = 137

TRADES = [
    {
        "label":    "Wellington 20°C — March 23",
        "question": "Will the highest temperature in Wellington be 20°C on March 23?",
        "token_id": "67598468196090897645259680326620715196763593984988917789795471505377321055369",
        "ensemble_prob": 0.98,
        "members": 50,
        "size": 20.0,
    },
    {
        "label":    "Seoul 16°C or higher — March 23",
        "question": "Will the highest temperature in Seoul be 16°C or higher on March 23?",
        "token_id": "57933182851524937060966492638917849074380898417400869784511300773896677074905",
        "ensemble_prob": 0.96,
        "members": 50,
        "size": 20.0,
    },
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

def place_trade(client, trade):
    token_id = trade["token_id"]
    size_usdc = trade["size"]
    prob = trade["ensemble_prob"]
    label = trade["label"]

    print(f"\n── {label} ──")

    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=8)
    mid = float(r.json().get("mid", 0.20)) if r.status_code == 200 else 0.20
    edge = prob - mid
    print(f"   Ensemble: {prob:.0%} | Market mid: {mid:.3f} | Edge: {edge:+.3f} | Size: ${size_usdc:.0f}")

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
            print(f"   ✓ BUY {shares} shares @ {price:.3f} = ${size_usdc:.0f} | order={receipt.get('orderID','')[:12]}")
            tg(f"🌡️ <b>Weather trade</b>\n{trade['question']}\nEnsemble: {prob:.0%} | Mid: {mid:.3f} | Edge: {edge:+.3f}\n${size_usdc:.0f} @ {price:.3f}")
            return True
        else:
            print(f"   ✗ Failed: {receipt.get('errorMsg','')}")
            return False
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback; traceback.print_exc()
        return False

def main():
    print("=" * 55)
    print("  WEATHER TRADES: Wellington + Seoul")
    print("  Source: ECMWF 50-member ensemble")
    print("=" * 55)

    client = get_client()
    if not os.environ.get("CLOB_API_KEY"):
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"Creds: {e}")

    placed = 0
    for trade in TRADES:
        if place_trade(client, trade):
            placed += 1

    print(f"\n{'='*55}")
    print(f"  Done: {placed}/{len(TRADES)} placed | ${placed*20:.0f} deployed")
    print("=" * 55)

    if placed > 0:
        tg(f"✅ <b>Weather trades: {placed}/2 placed</b>\nWellington 20°C (98% prob) + Seoul 16°C+ (96% prob)\n${placed*20} deployed via ECMWF 50-member ensemble")

if __name__ == "__main__":
    main()
