#!/usr/bin/env python3
"""
sell_ceasefire_apr30_yes.py
============================
Sells all YES shares on US x Iran ceasefire by April 30?
These are LP-bot-acquired shares creating a losing contradiction
(YES 94.67sh + NO 500sh, total cost $349, min payout $95 = lose $254).

Selling all YES shares collapses the contradiction and locks in the NO position
which is our actual directional thesis (no ceasefire by Apr30).

Token: 44149007410374101286260953227333745102128417138356632089802983317837574022801
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

YES_TOKEN = "44149007410374101286260953227333745102128417138356632089802983317837574022801"
CLOB_HOST = "https://clob.polymarket.com"

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def get_position():
    r = requests.get(
        f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
        timeout=15
    )
    for p in r.json():
        if ("ceasefire" in p.get('title','').lower()
                and "30" in p.get('title','')
                and p.get('outcome','').upper() == "YES"):
            return float(p.get('size', 0)), float(p.get('curPrice', 0))
    return 0, 0

def get_client():
    from py_clob_client.client import ClobClient
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=137,
                        funder=FUNDER or None, signature_type=sig_type)
    try:    creds = client.create_or_derive_api_creds()
    except: creds = client.derive_api_key()
    client.set_api_creds(creds)
    return client

def main():
    log("=" * 50)
    log("SELL: Ceasefire Apr30 YES (contradiction fix)")
    log("=" * 50)

    shares, cur_price = get_position()
    if shares < 1:
        log("No YES position found — already sold or empty")
        return

    log(f"YES shares: {shares:.2f} @ {cur_price:.3f} = ${shares*cur_price:.2f}")
    log(f"Selling all to close contradiction with NO position")

    client = get_client()

    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    try:
        tick     = client.get_tick_size(YES_TOKEN)
        neg_risk = client.get_neg_risk(YES_TOKEN)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0

        # Get midpoint for sell price
        mid_resp = client.get_midpoint(YES_TOKEN)
        sell_price = float(mid_resp.get("mid", cur_price))
        # Pull back 1 tick to ensure fill
        sell_price = max(0.01, sell_price - tick_f)
        sell_price = round(round(sell_price / tick_f) * tick_f, tick_dec)

        sell_shares = round(shares, 2)

        log(f"Placing SELL {sell_shares}sh @ {sell_price:.4f}")

        args    = OrderArgs(token_id=YES_TOKEN, price=sell_price,
                            size=sell_shares, side=SELL)
        opts    = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            oid = receipt.get("orderID", "?")
            proceeds = sell_shares * sell_price
            log(f"✓ SELL order placed | {sell_shares}sh @ {sell_price:.4f} = ~${proceeds:.2f}")
            log(f"  Order ID: {oid[:20]}...")
            tg(
                f"✅ <b>Contradiction fixed</b> — Ceasefire Apr30\n\n"
                f"Sold {sell_shares:.0f} YES shares @ {sell_price:.3f}\n"
                f"Proceeds: ~${proceeds:.2f} USDC\n\n"
                f"Remaining: NO position only (500sh @ 0.625)\n"
                f"Thesis: No ceasefire by April 30 ✓"
            )
        else:
            err = receipt.get("errorMsg", receipt.get("error", "unknown"))
            log(f"✗ Sell failed: {err}")
            tg(f"❌ <b>Sell failed</b>: ceasefire Apr30 YES\nError: {err[:100]}")

    except Exception as e:
        log(f"ERROR: {e}")
        tg(f"❌ <b>Sell error</b>: {str(e)[:150]}")

if __name__ == "__main__":
    main()
