"""
hormuz_rebalance.py
===========================
Response to Iran opening Strait of Hormuz (March 24, 2026):
1. BUY more Iran ceasefire March 31 YES (21¢ → likely 70-90¢ if deal announced)
2. SELL ceasefire April 15 NO (41¢ — wrong direction now, cut the loss)
3. SELL US forces enter Iran December 31 NO (hedge confusion — cut the contradiction)

Context: Iran said ships can cross Hormuz, Witkoff/Kushner ceasefire mechanism confirmed.
Trump 5-day window = March 23-28. Ceasefire by March 31 now has real probability.
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
from py_clob_client.order_builder.constants import BUY, SELL

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
        except: pass

def get_client():
    creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                     api_secret=os.environ.get("CLOB_API_SECRET",""),
                     api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
    return ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                      chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

def buy_position(client, token_id, usdc_size, label):
    print(f"\nBUY: {label} (${usdc_size})")
    try:
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        mid = float(r.json().get("mid", 0.5))
        
        # Check real order book depth
        book = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5).json()
        asks = book.get("asks", [])
        real_asks = [a for a in asks if 0.10 <= float(a["price"]) <= 0.90]
        if not real_asks:
            print(f"  No real order book depth — skipping {label}")
            return False
            
        tick = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        
        price = round(round(mid / tick_f) * tick_f + tick_f, tick_dec)
        price = max(0.02, min(0.98, price))
        shares = math.floor((usdc_size / price) * 100) / 100
        
        if shares < 1:
            print(f"  Too few shares ({shares}) — skip")
            return False
            
        print(f"  mid={mid:.3f} buy_price={price:.3f} shares={shares:.1f}")
        
        try:
            client.update_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        except: pass
        
        args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get("success"):
            cost = price * shares
            print(f"  ✓ BOUGHT {shares:.1f} @ {price:.3f} = ${cost:.2f}")
            tg(f"📈 <b>BUY {label}</b>\n{shares:.1f} shares @ {price:.3f} = ${cost:.2f}\nHormuz opening → ceasefire thesis")
            return True
        else:
            print(f"  ✗ {receipt.get('errorMsg','')}")
            return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False

def sell_position(client, token_id, label, reason):
    print(f"\nSELL: {label}")
    print(f"  Reason: {reason}")
    try:
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        raw = int(bal.get("balance", 0))
        if raw < 1000:
            print(f"  No position found (balance={raw})")
            return True
            
        exact_shares = raw / 1e6
        sell_size = math.floor(exact_shares * 100) / 100
        
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        mid = float(r.json().get("mid", 0.5))
        
        tick = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        
        print(f"  {sell_size:.2f} shares @ {sell_price:.3f} = ${sell_price*sell_size:.2f}")
        
        args = OrderArgs(token_id=token_id, price=sell_price, size=sell_size, side=SELL)
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get("success"):
            proceeds = sell_price * sell_size
            print(f"  ✓ SOLD | Proceeds: ${proceeds:.2f}")
            tg(f"📤 <b>SELL {label}</b>\nProceeds: ${proceeds:.2f}\nReason: {reason}")
            return True
        else:
            print(f"  ✗ {receipt.get('errorMsg','')}")
            return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False

# Token IDs
CEASEFIRE_MARCH31_YES = "57085616606014598055921893388381283501413534547940462025649380023698027217143"
CEASEFIRE_APR15_NO    = "22668285050948804754832949640553977261521843754082234483820700003882477527905"
FORCES_DEC31_NO       = "99943138608691565280041516990386399866706337568819714714225455823019553600640"

def main():
    print("=== Hormuz Rebalance: Iran Ceasefire Thesis ===")
    print("Context: Iran opened Strait of Hormuz, Witkoff/Kushner mechanism confirmed")
    print("Action: Add ceasefire YES, exit conflicting NO positions\n")
    
    client = get_client()
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("Creds OK")
    except Exception as e:
        print(f"Creds: {e}")
    
    # Check USDC balance
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=2))
    usdc = float(bal.get("balance",0)) / 1e6
    print(f"USDC available: ${usdc:.2f}")
    
    results = []
    
    # 1. Buy more ceasefire March 31 YES
    if usdc >= 75:
        ok = buy_position(client, CEASEFIRE_MARCH31_YES,
                         min(75, usdc * 0.4),
                         "Iran ceasefire by March 31 YES")
        results.append(("BUY ceasefire YES", ok))
    else:
        print(f"\nInsufficient USDC (${usdc:.2f}) for new buy")
    
    # 2. Sell ceasefire April 15 NO (wrong direction)
    ok2 = sell_position(client, CEASEFIRE_APR15_NO,
                        "Iran ceasefire by April 15 NO",
                        "Ceasefire now more likely — NO is wrong side")
    results.append(("SELL ceasefire Apr15 NO", ok2))
    
    # 3. Sell December 31 NO (contradicts April 30 YES, confusing book)
    ok3 = sell_position(client, FORCES_DEC31_NO,
                        "US forces enter Iran by December 31 NO",
                        "Contradicts April 30 YES position — clean up book")
    results.append(("SELL forces Dec31 NO", ok3))
    
    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'✓' if ok else '✗'} {label}")
    
    tg(f"📊 <b>Hormuz Rebalance Done</b>\n"
       f"Ceasefire thesis activated: Iran opened Strait of Hormuz\n"
       f"{sum(1 for _,ok in results if ok)}/{len(results)} actions succeeded")

if __name__ == "__main__":
    main()
