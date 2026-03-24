"""
sell_btc_80k_no.py — Sell BTC $80,000 NO position
Lock in +57% gain before potential BTC rally to $80k.
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

if not PRIVATE_KEY: print("ERROR: no key"); sys.exit(1)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import SELL

HOST  = "https://clob.polymarket.com"
CHAIN = 137

# BTC $80k NO token — selling the entire position
TOKEN_ID   = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY_PRICE = 0.5656   # Our average entry
SHARES     = 337.24

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def get_client():
    creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                     api_secret=os.environ.get("CLOB_API_SECRET",""),
                     api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
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

    # Get current mid
    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN_ID}", timeout=8)
    mid = float(r.json().get("mid", 0.885)) if r.status_code == 200 else 0.885
    pnl = (mid - ENTRY_PRICE) * SHARES
    pnl_pct = (mid - ENTRY_PRICE) / ENTRY_PRICE * 100

    print(f"{'='*55}")
    print(f"  SELL BTC $80k NO — Lock gain")
    print(f"  Entry: {ENTRY_PRICE:.4f} | Current: {mid:.4f}")
    print(f"  Gain: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
    print(f"  Selling {SHARES} shares → ${SHARES * mid:.2f}")
    print(f"{'='*55}")

    try:
        # Approve conditional token allowance for this specific token
        print("  Approving conditional token allowance...")
        for _at in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
            try:
                _kwargs = {"asset_type": _at, "signature_type": 2}
                if _at == AssetType.CONDITIONAL:
                    _kwargs["token_id"] = TOKEN_ID
                client.update_balance_allowance(params=BalanceAllowanceParams(**_kwargs))
                print(f"  ✓ {_at} approved")
            except Exception as ae:
                # May already be approved or not needed
                print(f"  {_at}: {ae}")

        tick     = client.get_tick_size(TOKEN_ID)
        neg_risk = client.get_neg_risk(TOKEN_ID)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        shares_to_sell = round(SHARES, 2)

        args    = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=shares_to_sell, side=SELL)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            proceeds = sell_price * shares_to_sell
            profit = proceeds - ENTRY_PRICE * shares_to_sell
            print(f"\n  ✓ SOLD {shares_to_sell} shares @ {sell_price:.4f}")
            print(f"  ✓ Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
            print(f"  ✓ Order: {receipt.get('orderID','')[:16]}")
            tg(f"💰 <b>BTC $80k NO — SOLD</b>\n{shares_to_sell} shares @ {sell_price:.4f}\nProceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}\nLocked in gain before BTC rally risk")
        else:
            print(f"\n  ✗ Failed: {receipt.get('errorMsg','')}")
    except Exception as e:
        print(f"\n  ✗ Error: {e}")
        import traceback; traceback.print_exc()

if __name__ == "__main__":
    main()
