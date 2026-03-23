"""
place_iran_conflict_no.py — BUY NO on "Iran x Israel/US conflict ends by December 31?"
=========================================================================================
Thesis: Active missile exchanges (Iran fired at US-UK base, US-Israel struck Natanz),
Trump explicitly rejected ceasefire. Market says 83.5% chance conflict ENDS by Dec 31.
That's badly mispriced — with 9 months of active shooting war, conflict ending Dec 31
seems <50% likely. BUY NO at 16.5¢ = 6x payout if conflict continues past Dec 31.

Run: curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/place_iran_conflict_no.py -o /opt/polymarket-agent/place_iran_conflict_no.py && /opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/place_iran_conflict_no.py
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

# NO token for "Iran x Israel/US conflict ends by December 31?"
# YES=0.835 (market says 83.5% chance conflict ends) → NO=16.5¢
TRADE = {
    "label":    "Iran x Israel/US conflict ends by Dec 31 — NO",
    "question": "Iran x Israel/US conflict ends by December 31?",
    "token_id": "72672649490627178259809292941952979051200271014678303501403336114251236535062",
    "side":     "NO",
    "thesis":   "Active US-Iran missile exchange + Trump rejected ceasefire = conflict not ending Dec 31",
    "market_yes": 0.835,   # Market's YES probability
    "our_prob":   0.45,    # Our estimated probability of YES (conflict ends)
    "size":     100.0,     # USDC — sized for high-conviction macro thesis
}

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

def main():
    client = get_client()
    if not os.environ.get("CLOB_API_KEY"):
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"Creds: {e}")

    token_id  = TRADE["token_id"]
    size_usdc = TRADE["size"]

    # Get current mid for the NO token
    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=8)
    no_mid = float(r.json().get("mid", 0.165)) if r.status_code == 200 else 0.165
    yes_mid = round(1.0 - no_mid, 3)
    edge = TRADE["our_prob"] - yes_mid  # edge on YES side (negative = we want NO)
    no_edge = (1.0 - TRADE["our_prob"]) - no_mid  # edge on NO side

    print("=" * 60)
    print("  IRAN CONFLICT — BUY NO")
    print("=" * 60)
    print(f"  Market YES: {yes_mid:.3f} (conflict ends by Dec 31)")
    print(f"  Our est:    {TRADE['our_prob']:.3f}")
    print(f"  NO price:   {no_mid:.3f}")
    print(f"  NO edge:    {no_edge:+.3f}")
    print(f"  Payout if NO wins: ${size_usdc/no_mid:.0f} on ${size_usdc:.0f} (~{1/no_mid:.1f}x)")
    print(f"  Thesis: {TRADE['thesis']}")
    print()

    try:
        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price    = round(round(no_mid / tick_f) * tick_f, tick_dec)
        price    = max(0.01, min(0.99, price))
        shares   = round(size_usdc / price, 2)

        args    = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)

        # Approve USDC allowance before placing (required for first trade)
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            client.update_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=2
                )
            )
        except Exception as ae:
            print(f"  Allowance pre-approval: {ae}")

        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            payout = shares * 1.0
            print(f"  ✓ BUY NO: {shares} shares @ {price:.3f} = ${size_usdc:.0f}")
            print(f"  ✓ Max payout: ${payout:.0f} if conflict continues past Dec 31")
            print(f"  ✓ Order: {receipt.get('orderID','')[:16]}")
            tg(f"🇮🇷 <b>Iran Conflict NO placed</b>\n"
               f"Iran x Israel/US conflict ends by Dec 31? → NO\n"
               f"Market says 83.5% ends — we say {TRADE['our_prob']:.0%}\n"
               f"NO @ {price:.3f} | ${size_usdc:.0f} → ${payout:.0f} if correct (6x)\n"
               f"Thesis: active missile exchange + Trump rejected ceasefire")
        else:
            print(f"  ✗ Failed: {receipt.get('errorMsg','')}")

    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback; traceback.print_exc()

if __name__ == "__main__":
    main()
