"""
sell_btc_80k_no.py — Sell BTC $80,000 NO position (v5 - FIXED)

ROOT CAUSE of "not enough balance / allowance":
  Hardcoded SHARES = 337.24 but actual balance = 337.238879 shares.
  The CLOB rounds size DOWN to 2 decimals → 337.23 needed (not 337.24).
  337.24 → 337240000 units > 337238879 actual balance → REJECTED.

Fix: query exact balance from CLOB, then floor to 2 decimal places.
"""
import os, sys, re, json, math, requests
from dotenv import load_dotenv

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env)

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not PRIVATE_KEY: print("ERROR: no PRIVATE_KEY"); sys.exit(1)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (ApiCreds, OrderArgs, OrderType,
    PartialCreateOrderOptions, BalanceAllowanceParams, AssetType)
from py_clob_client.order_builder.constants import SELL

HOST     = "https://clob.polymarket.com"
CHAIN    = 137
TOKEN_ID = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY    = 0.5656

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

def save_creds(creds):
    with open(_env) as f:
        env = f.read()
    for key, val in [("CLOB_API_KEY", creds.api_key),
                     ("CLOB_API_SECRET", creds.api_secret),
                     ("CLOB_API_PASSPHRASE", creds.api_passphrase)]:
        if re.search(f"^{key}=", env, re.MULTILINE):
            env = re.sub(f"^{key}=.*$", f"{key}={val}", env, flags=re.MULTILINE)
        else:
            env += f"\n{key}={val}"
    with open(_env, "w") as f:
        f.write(env)

def floor2(x):
    """Floor to 2 decimal places (same as CLOB's round_down with size=2)"""
    return math.floor(x * 100) / 100

def main():
    print("=" * 60)
    print("  BTC $80k NO — SELL v5 (exact balance fix)")
    print("=" * 60)

    # Always re-derive creds
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN,
                        signature_type=SIG_TYPE, funder=FUNDER)
    print("\n[1] Deriving fresh CLOB API creds...")
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        save_creds(creds)
        print(f"  OK: {creds.api_key[:20]}...")
    except Exception as e:
        print(f"  Warning: {e}")
        env_creds = ApiCreds(
            api_key=os.environ.get("CLOB_API_KEY",""),
            api_secret=os.environ.get("CLOB_API_SECRET",""),
            api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client.set_api_creds(env_creds)

    # Get exact balance from CLOB
    print("\n[2] Getting exact balance from CLOB...")
    try:
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID,
            signature_type=SIG_TYPE))
        print(f"  Raw response: {bal}")
        raw_units = int(bal.get("balance", 0))
        exact_shares = raw_units / 1e6
        # CLOB internally does round_down(size, 2) so floor to 2dp
        safe_shares = floor2(exact_shares)
        print(f"  Raw units: {raw_units}")
        print(f"  Exact shares: {exact_shares:.6f}")
        print(f"  Safe size (floor to 2dp): {safe_shares}")
    except Exception as e:
        print(f"  Balance query failed: {e}")
        exact_shares = 337.238879
        safe_shares = floor2(exact_shares)  # = 337.23
        print(f"  Fallback: {safe_shares}")

    if safe_shares < 1:
        print(f"\n  ERROR: safe_shares={safe_shares} — position may already be closed")
        tg(f"⚠️ BTC sell: only {safe_shares} shares. Already sold?")
        return

    # Update allowances
    print("\n[3] Updating allowances...")
    try:
        client.update_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=SIG_TYPE))
        print("  COLLATERAL: OK")
    except Exception as e:
        print(f"  COLLATERAL: {e}")
    try:
        client.update_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID,
            signature_type=SIG_TYPE))
        print("  CONDITIONAL: OK")
    except Exception as e:
        print(f"  CONDITIONAL: {e}")

    # Get market data
    print("\n[4] Getting market data...")
    r = requests.get(f"{HOST}/midpoint?token_id={TOKEN_ID}", timeout=8)
    mid = float(r.json().get("mid", 0.89)) if r.status_code == 200 else 0.89

    tick     = client.get_tick_size(TOKEN_ID)
    neg_risk = client.get_neg_risk(TOKEN_ID)
    tick_f   = float(tick)
    tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
    sell_price = max(0.01, min(0.99, sell_price))

    pnl = (mid - ENTRY) * safe_shares
    print(f"  Mid: {mid:.4f} | Sell price: {sell_price:.4f}")
    print(f"  Shares: {safe_shares} | P&L: ${pnl:+.2f}")
    print(f"  tick={tick} neg_risk={neg_risk}")

    # Place sell order
    print("\n[5] Placing sell order...")
    args = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=safe_shares, side=SELL)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)

    try:
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        print(f"  Receipt: {json.dumps(receipt, indent=2)}")

        if receipt.get("success"):
            proceeds = sell_price * safe_shares
            profit   = proceeds - ENTRY * safe_shares
            print(f"\n  ✅ SOLD {safe_shares} @ {sell_price:.4f}")
            print(f"  ✅ Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
            tg(f"💰 <b>BTC $80k NO — SOLD ✅</b>\n"
               f"{safe_shares} shares @ {sell_price:.4f}\n"
               f"Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
        else:
            err = receipt.get("errorMsg", str(receipt))
            print(f"\n  ✗ Failed: {err}")

            # Last resort: try 1 share less
            if "balance" in err.lower() or "allowance" in err.lower():
                fallback_size = floor2(safe_shares - 0.01)
                print(f"\n  Retrying with {fallback_size} shares...")
                args2   = OrderArgs(token_id=TOKEN_ID, price=sell_price,
                                    size=fallback_size, side=SELL)
                signed2 = client.create_order(args2, opts)
                r2      = client.post_order(signed2, OrderType.GTC)
                print(f"  Retry receipt: {json.dumps(r2, indent=2)}")
                if r2.get("success"):
                    p = sell_price * fallback_size
                    pr = p - ENTRY * fallback_size
                    print(f"  ✅ SOLD {fallback_size} @ {sell_price:.4f} | ${p:.2f} | ${pr:+.2f}")
                    tg(f"💰 <b>BTC $80k NO SOLD (fallback)</b>\n"
                       f"{fallback_size} @ {sell_price:.4f} | Profit: ${pr:+.2f}")
                else:
                    print(f"  ✗ Retry also failed: {r2.get('errorMsg','')}")
                    tg(f"⚠️ BTC sell all attempts failed.\nBot will handle it automatically on next cycle.")
    except Exception as e:
        print(f"\n  ✗ Exception: {e}")
        import traceback; traceback.print_exc()

    print("\nDone")

if __name__ == "__main__":
    main()
