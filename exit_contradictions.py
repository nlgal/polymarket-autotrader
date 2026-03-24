"""
exit_contradictions.py
Exits positions that are now contradicted by current news (82nd Airborne deployment):
1. US x Iran ceasefire by March 31 YES — wrong, escalation kills ceasefire chances
2. Trump announces end of military ops YES — wrong, escalation is the opposite
Also exits December 31 NO which contradicts the April 30 YES position.
"""
import os, sys, math, requests
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID","").strip()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (ApiCreds, OrderArgs, OrderType,
    PartialCreateOrderOptions, BalanceAllowanceParams, AssetType)
from py_clob_client.order_builder.constants import SELL

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
        except: pass

# Positions to exit with reason
EXITS = [
    {
        "token_id": "57085616606014598055921893388381283501413534547940462025649380023698027217143",
        "label": "US x Iran ceasefire by March 31 YES",
        "reason": "82nd Airborne deployed — escalation kills ceasefire odds",
        "entry": 0.12
    },
    {
        "token_id": "10840458361241201790490827406015519977440793547609316462773765455975888048906",
        "label": "Trump end of military ops YES",
        "reason": "82nd Airborne deploying = operations expanding, not ending",
        "entry": 0.16
    },
    {
        "token_id": "99943138608691565280041516990386399866706337568819714714225455823019553600640",
        "label": "US forces enter Iran by Dec 31 NO",
        "reason": "Contradicts April 30 YES — same underlying bet, opposing sides",
        "entry": 0.33
    },
]

def get_client():
    creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                     api_secret=os.environ.get("CLOB_API_SECRET",""),
                     api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
    return ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                      chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

def sell_position(client, token_id, label, reason, entry):
    print(f"\nSelling: {label}")
    print(f"  Reason: {reason}")
    
    try:
        # Get exact CLOB balance
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        raw = int(bal.get("balance", 0))
        if raw < 1000:  # < 0.001 shares
            print(f"  Position already closed (balance={raw})")
            return True
        
        exact_shares = raw / 1e6
        sell_size = math.floor(exact_shares * 100) / 100
        
        # Get current mid
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        mid = float(r.json().get("mid", 0.5))
        
        tick = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        
        proceeds = sell_price * sell_size
        pnl = (sell_price - entry) * sell_size
        
        print(f"  Balance: {exact_shares:.4f} shares | Mid: {mid:.3f} | Sell: {sell_price:.3f}")
        print(f"  Proceeds: ${proceeds:.2f} | P&L vs entry: ${pnl:+.2f}")
        
        args = OrderArgs(token_id=token_id, price=sell_price, size=sell_size, side=SELL)
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get("success"):
            print(f"  ✓ SOLD | Order: {receipt.get('orderID','')[:16]}")
            tg(f"📤 <b>Position exited</b>\n{label}\n"
               f"Proceeds: ${proceeds:.2f} | P&L: ${pnl:+.2f}\n"
               f"Reason: {reason}")
            return True
        else:
            err = receipt.get('errorMsg','')
            print(f"  ✗ Failed: {err}")
            return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        import traceback; traceback.print_exc()
        return False

def main():
    print("=== Exiting Contradictory/Wrong-Direction Positions ===")
    print("Context: 82nd Airborne ordered to deploy to Middle East (Mar 24)")
    
    client = get_client()
    
    # Re-derive fresh creds
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("Creds refreshed OK")
    except Exception as e:
        print(f"Cred refresh: {e}")
    
    results = []
    for exit_info in EXITS:
        ok = sell_position(client, **exit_info)
        results.append((exit_info['label'], ok))
    
    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'✓' if ok else '✗'} {label}")
    
    tg(f"📊 <b>Portfolio Rebalance Complete</b>\n"
       f"Exited {sum(1 for _,ok in results if ok)}/{len(results)} contradictory positions\n"
       f"Trigger: 82nd Airborne deployment order (escalation signal)")

if __name__ == "__main__":
    main()
