"""
sell_btc_80k_no.py — Sell BTC $80,000 NO position (comprehensive v4)

Fixes "not enough balance / allowance" 400 error by:
1. Re-deriving fresh API creds every time
2. Querying exact balance from CLOB (not hardcoded)
3. For SELL orders: size = shares (not USDC)
4. Trying multiple price levels if first attempt fails (bid, mid, mid-0.01)
5. Saving fresh creds to .env so autotrader uses them too
"""
import os, sys, re, json, requests
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

HOST  = "https://clob.polymarket.com"
CHAIN = 137
TOKEN_ID    = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY_PRICE = 0.5656

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception: pass

def save_creds(creds):
    """Persist fresh CLOB creds to .env"""
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
    print(f"  ✓ Saved fresh creds to .env (key: {creds.api_key[:20]}...)")

def main():
    # Step 1: Create client and ALWAYS re-derive creds
    print("=" * 60)
    print("  BTC $80k NO — SELL (v4)")
    print("=" * 60)
    
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN,
                        signature_type=SIG_TYPE, funder=FUNDER)
    
    print("\n[1] Deriving fresh CLOB API credentials...")
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        save_creds(creds)
    except Exception as e:
        print(f"  Warning: cred derivation failed: {e}")
        # Fall back to env creds
        creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                         api_secret=os.environ.get("CLOB_API_SECRET",""),
                         api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client.set_api_creds(creds)

    # Step 2: Get exact balance from CLOB
    print("\n[2] Querying exact balance from CLOB...")
    try:
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID, signature_type=SIG_TYPE))
        print(f"  Balance response: {bal}")
        raw = int(bal.get("balance", 0))
        # CLOB balance is in units of 1e6
        exact_shares = raw / 1e6
        print(f"  Raw: {raw} → {exact_shares:.6f} shares")
    except Exception as e:
        print(f"  Warning: balance query failed: {e} — using 337.24")
        exact_shares = 337.24

    if exact_shares < 1:
        print(f"\n  ERROR: Only {exact_shares} shares found. Position may already be sold or CLOB out of sync.")
        tg(f"⚠️ <b>BTC sell aborted</b>: only {exact_shares:.4f} shares in CLOB. Already sold?")
        return

    # Step 3: Update allowances
    print("\n[3] Updating allowances...")
    for asset_type, kwargs in [
        (AssetType.COLLATERAL, {"asset_type": AssetType.COLLATERAL, "signature_type": SIG_TYPE}),
        (AssetType.CONDITIONAL, {"asset_type": AssetType.CONDITIONAL, "token_id": TOKEN_ID, "signature_type": SIG_TYPE}),
    ]:
        try:
            r = client.update_balance_allowance(params=BalanceAllowanceParams(**kwargs))
            print(f"  ✓ {asset_type}: {r}")
        except Exception as e:
            print(f"  {asset_type}: {e}")

    # Step 4: Get market data
    print("\n[4] Getting market data...")
    r = requests.get(f"{HOST}/midpoint?token_id={TOKEN_ID}", timeout=8)
    mid = float(r.json().get("mid", 0.89)) if r.status_code == 200 else 0.89
    
    # Also get the order book to find best bid
    try:
        ob_r = requests.get(f"{HOST}/book?token_id={TOKEN_ID}", timeout=8)
        ob = ob_r.json()
        bids = ob.get("bids", [])
        best_bid = float(bids[0]["price"]) if bids else mid - 0.01
        print(f"  Mid: {mid:.4f} | Best bid: {best_bid:.4f}")
    except:
        best_bid = mid - 0.01
        print(f"  Mid: {mid:.4f} (couldn't get order book)")

    tick     = client.get_tick_size(TOKEN_ID)
    neg_risk = client.get_neg_risk(TOKEN_ID)
    tick_f   = float(tick)
    tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    print(f"  tick={tick} neg_risk={neg_risk}")

    # Step 5: Try to sell at multiple price levels
    print("\n[5] Attempting sell orders...")
    
    pnl = (mid - ENTRY_PRICE) * exact_shares
    pnl_pct = (mid - ENTRY_PRICE) / ENTRY_PRICE * 100
    print(f"  Entry: {ENTRY_PRICE:.4f} | Current: {mid:.4f} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
    
    # Try sizes: exact (6dp), truncated to 4dp, truncated to 2dp, minus a tiny bit
    # Polymarket CLOB needs size in shares (not USDC) for conditional tokens
    size_candidates = [
        round(exact_shares, 6),
        round(exact_shares, 4),
        round(exact_shares, 2),
        round(exact_shares - 0.01, 2),
    ]
    # Remove duplicates while preserving order
    seen = set()
    sizes = []
    for s in size_candidates:
        if s > 0 and s not in seen:
            seen.add(s)
            sizes.append(s)

    # Try price levels: mid (rounded to tick), mid-1tick, best_bid
    price_candidates = []
    for base in [mid, mid - tick_f, best_bid]:
        p = round(round(base / tick_f) * tick_f, tick_dec)
        p = max(0.01, min(0.99, p))
        if p not in price_candidates:
            price_candidates.append(p)

    for price in price_candidates:
        for size in sizes:
            print(f"\n  Trying: {size} shares @ {price:.4f}...")
            try:
                args   = OrderArgs(token_id=TOKEN_ID, price=price, size=size, side=SELL)
                opts   = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
                signed = client.create_order(args, opts)
                receipt = client.post_order(signed, OrderType.GTC)
                
                print(f"  Receipt: {json.dumps(receipt, indent=2)}")
                
                if receipt.get("success"):
                    proceeds = price * size
                    profit   = proceeds - ENTRY_PRICE * size
                    print(f"\n  ✅ SOLD {size} @ {price:.4f}")
                    print(f"  ✅ Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
                    tg(f"💰 <b>BTC $80k NO — SOLD ✅</b>\n{size} shares @ {price:.4f}\n"
                       f"Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}\n"
                       f"Locked in gain before BTC rally risk")
                    return  # SUCCESS — done
                else:
                    err = receipt.get("errorMsg", str(receipt))
                    print(f"  ✗ Failed: {err}")
                    if "not enough balance" not in err.lower() and "allowance" not in err.lower():
                        # Different error — stop trying this price
                        break
            except Exception as e:
                print(f"  ✗ Exception: {e}")
                import traceback; traceback.print_exc()

    # All attempts failed
    print("\n" + "=" * 60)
    print("  All sell attempts failed.")
    print("  The bot's manage_positions() should handle this automatically")
    print("  since current price (0.895) > PROFIT_TARGET (0.80).")
    print("  It will sell on the next trading cycle if CLOB creds are valid.")
    print("=" * 60)
    
    tg(f"⚠️ <b>BTC $80k NO sell failed</b>\n"
       f"All {len(price_candidates)*len(sizes)} attempts returned 'not enough balance'.\n"
       f"Bot will attempt automatic sell on next cycle.\n"
       f"Balance: {exact_shares:.4f} shares | Mid: {mid:.4f}")

if __name__ == "__main__":
    main()
