#!/usr/bin/env python3
"""
One-shot manual trade placer — Gold LOW NO + ETH dip NO
Must run on DigitalOcean server (geo-restriction).
Usage: curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/place_gold_eth_trades.py | /opt/polymarket-agent/venv/bin/python3
"""
import os, sys, time, requests

# ── Load env ────────────────────────────────────────────────────────────────
env = {}
for path in ["/opt/polymarket-agent/.env", os.path.expanduser("~/.env"), ".env"]:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        break
    except FileNotFoundError:
        continue

PRIVATE_KEY = env.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER      = env.get("POLYMARKET_FUNDER_ADDRESS", "")
TG_TOKEN    = env.get("TELEGRAM_TOKEN", "")
TG_CHAT     = env.get("TELEGRAM_CHAT_ID", "")

if not PRIVATE_KEY or not FUNDER:
    print("ERROR: Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER_ADDRESS")
    sys.exit(1)

pk = "0x" + PRIVATE_KEY if not PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
print(f"FUNDER: {FUNDER}")

# ── CLOB client ─────────────────────────────────────────────────────────────
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams, AssetType, OrderArgs, OrderType,
    PartialCreateOrderOptions
)
from py_clob_client.order_builder.constants import BUY

client = ClobClient(host="https://clob.polymarket.com", key=pk,
                    chain_id=137, funder=FUNDER, signature_type=2)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

bal = client.get_balance_allowance(
    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
cash = float(bal["balance"]) / 1e6
print(f"Cash available: ${cash:.2f}")

if cash < 50:
    print("Not enough cash. Exiting.")
    sys.exit(0)

# ── Trades ───────────────────────────────────────────────────────────────────
# 1. Gold (GC) hit LOW $4,200 by end of June — NO
#    Thesis: Gold at ~$4,490 now (ATH $5,608). Every major bank (JPM, GS, WF)
#    forecasts $5k-$6.3k by year-end. Iran war escalating = geopolitical bid.
#    Market pricing 40% chance of a $4,200 LOW (a ~7% further drop) by June.
#    That is far too pessimistic. NO at 39.5c = ~2.53x payout.
#
# 2. Ethereum dip to $1,800 in March — NO
#    Thesis: ETH at ~$2,155, needs 16% crash in 10 days. Market at 82.5c NO.
#    Decent premium (17.5c) for a move that would require a major black swan.
#    Under the 88c hard cap — acceptable risk/reward.

TRADES = [
    {
        "desc": "Gold (GC) hit LOW $4,200 by end of June — NO",
        "token_id": "107903187097064440993665243047237103234187359771702998268137476819081108572677",
        "price": 0.40,   # midpoint 0.395, buying at 0.40 to ensure fill
        "usdc": 150.0,
        "tick": "0.01",
        "reason": "Gold at $4,490, ATH $5,608, JPM/GS/WF all forecast $5k-6.3k by year-end. "
                  "Iran war geopolitical bid strong. 40% chance of $4,200 low is too pessimistic.",
    },
    {
        "desc": "Ethereum dip to $1,800 in March — NO",
        "token_id": "54381288899729059336893303549703041321201308892041262917796512226880109904254",
        "price": 0.83,   # midpoint 0.825, buying at 0.83 to ensure fill
        "usdc": 100.0,
        "tick": "0.01",
        "reason": "ETH at ~$2,155, needs 16% crash in 10 days. "
                  "Below 88c hard cap, decent premium for tail risk.",
    },
]

report = ["<b>📊 Manual Trades: Gold LOW NO + ETH Dip NO</b>"]
placed = 0
total_deployed = 0.0

for t in TRADES:
    desc   = t["desc"]
    price  = t["price"]
    usdc   = min(t["usdc"], cash - 50)   # never leave less than $50
    shares = round(usdc / price, 2)

    if usdc < 10:
        print(f"Skipping {desc} — not enough cash remaining")
        continue

    print(f"\n→ {desc}")
    print(f"  ${usdc:.2f} @ {price} = {shares} shares")
    print(f"  Reason: {t['reason'][:80]}")

    try:
        order = client.create_order(
            OrderArgs(price=price, size=shares, side=BUY, token_id=t["token_id"]),
            PartialCreateOrderOptions(tick_size=t["tick"])
        )
        resp = client.post_order(order, OrderType.GTC)

        if isinstance(resp, dict):
            status = resp.get("status", "?")
            err    = resp.get("errorMsg", "") or ""
            oid    = (resp.get("orderID", "") or "")[:20]
            if err:
                print(f"  ❌ {status}: {err}")
                report.append(f"❌ {desc[:50]}: {err[:60]}")
            else:
                print(f"  ✅ {status} | {oid}")
                report.append(f"✅ {desc[:55]} | ${usdc:.0f} @ {price:.2f}")
                placed += 1
                total_deployed += usdc
        else:
            print(f"  ?: {resp}")
            report.append(f"? {desc[:50]}: {str(resp)[:60]}")

    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        report.append(f"❌ {desc[:50]}: {str(e)[:80]}")

    time.sleep(2)

# ── Final balance ────────────────────────────────────────────────────────────
time.sleep(3)
bal2 = client.get_balance_allowance(
    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
cash2 = float(bal2["balance"]) / 1e6
deployed_actual = round(cash - cash2, 2)

report.append(f"\nDeployed: ${deployed_actual:.2f} | Cash remaining: ${cash2:.2f}")
print(f"\n{'='*50}")
print(f"Placed: {placed}/{len(TRADES)} trades")
print(f"Deployed: ${deployed_actual:.2f} | Remaining: ${cash2:.2f}")

# ── Telegram ─────────────────────────────────────────────────────────────────
if TG_TOKEN and TG_CHAT and placed > 0:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": "\n".join(report), "parse_mode": "HTML"},
            timeout=10
        )
        print("Telegram sent.")
    except Exception as e:
        print(f"Telegram error: {e}")

print("Done.")
