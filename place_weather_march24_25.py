"""
place_weather_march24_25.py — Weather trades for March 24-25
Run: curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/place_weather_march24_25.py -o /opt/polymarket-agent/place_weather_march24_25.py && /opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/place_weather_march24_25.py

Source: ECMWF 50-member ensemble with city tier hardening (post-Wellington lessons)
Tier A cities (Seoul, Tokyo, Beijing): standard 50% threshold
Tier B cities (London, Paris, NYC): 55% threshold
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
    # Paris 15°C — March 24 | 100% ensemble | mid=0.033 | edge=+0.968 | liq=$1532 | 32h left
    {"label": "Paris 15°C — March 24", "question": "Will the highest temperature in Paris be 15°C on March 24?", "token_id": "109818895064779900871343664188545747031132056977844952761470395420245159752409", "prob": 1.00, "size": 20.0},
    # Tokyo 15°C — March 24 | 94% ensemble | mid=0.175 | edge=+0.765 | liq=$1019 | 32h left
    {"label": "Tokyo 15°C — March 24", "question": "Will the highest temperature in Tokyo be 15°C on March 24?", "token_id": "12544544194414797934569824574436366105255053215654084516979270148603255933446", "prob": 0.94, "size": 20.0},
    # London 8°C — March 25 | 82% ensemble | mid=0.180 | edge=+0.640 | liq=$1250 | 56h left
    {"label": "London 8°C — March 25", "question": "Will the highest temperature in London be 8°C on March 25?", "token_id": "5896347067127129244648078116886178214718880875047889426557333615911194117448", "prob": 0.82, "size": 15.0},
    # Beijing 23°C — March 25 | 78% ensemble | mid=0.120 | edge=+0.660 | liq=$556 | 56h left
    {"label": "Beijing 23°C — March 25", "question": "Will the highest temperature in Beijing be 23°C on March 25?", "token_id": "111984092979175072249406901366049639596802963809557117125631013999065197799313", "prob": 0.78, "size": 15.0},
    # NYC 44-45°F — March 24 | 38% ensemble | mid=0.190 | edge=+0.190 | liq=$1125 | 32h left
    {"label": "NYC 44-45°F — March 24", "question": "Will the highest temperature in New York City be between 44-45°F on March 24?", "token_id": "11264762238868470392969284368589596929044651568997101186373153312841457562692", "prob": 0.38, "size": 10.0},
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
    token_id  = trade["token_id"]
    size_usdc = trade["size"]
    prob      = trade["prob"]
    label     = trade["label"]

    print(f"\n── {label} ──")
    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=8)
    mid = float(r.json().get("mid", 0.10)) if r.status_code == 200 else 0.10
    edge = prob - mid
    print(f"   Ensemble: {prob:.0%} | Mid: {mid:.3f} | Edge: {edge:+.3f} | Size: ${size_usdc:.0f}")

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
            print(f"   ✓ BUY {shares} shares @ {price:.3f} | payout if YES: ${payout:.0f} | order={receipt.get('orderID','')[:12]}")
            tg(f"🌡️ <b>Weather</b> {label}\nEnsemble {prob:.0%} | mid {mid:.3f} | edge {edge:+.3f}\n${size_usdc:.0f} → ${payout:.0f} if correct")
            return True
        else:
            print(f"   ✗ Failed: {receipt.get('errorMsg','')}")
            return False
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback; traceback.print_exc()
        return False

def main():
    total_size = sum(t["size"] for t in TRADES)
    print("=" * 60)
    print("  WEATHER TRADES — March 24-25 | ECMWF Ensemble")
    print(f"  {len(TRADES)} trades | ${total_size:.0f} total USDC")
    print("=" * 60)

    client = get_client()
    if not os.environ.get("CLOB_API_KEY"):
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"Creds: {e}")

    placed = 0
    total_deployed = 0
    for trade in TRADES:
        if place_trade(client, trade):
            placed += 1
            total_deployed += trade["size"]

    print(f"\n{'='*60}")
    print(f"  Done: {placed}/{len(TRADES)} placed | ${total_deployed:.0f} deployed")
    print("=" * 60)

    if placed > 0:
        tg(f"✅ <b>Weather batch: {placed}/{len(TRADES)} placed (${total_deployed:.0f})</b>\n"
           f"Paris 15°C (100%) + Tokyo 15°C (94%) + London 8°C (82%) + Beijing 23°C (78%) + NYC 44°F (38%)")

if __name__ == "__main__":
    main()
