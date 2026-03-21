#!/usr/bin/env python3
"""
One-shot manual trade placer — Iran war NO positions
Must run on DigitalOcean server (geo-restriction).
Usage: python3 /tmp/place_iran_trades.py
"""
import os, sys, time, json, requests

# ── Load env ───────────────────────────────────────────────────────────────────
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

PRIVATE_KEY = env.get("POLYMARKET_PRIVATE_KEY","")
FUNDER      = env.get("POLYMARKET_FUNDER_ADDRESS","")
TG_TOKEN    = env.get("TELEGRAM_TOKEN","")
TG_CHAT     = env.get("TELEGRAM_CHAT_ID","")

if not PRIVATE_KEY or not FUNDER:
    print("ERROR: Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER_ADDRESS")
    sys.exit(1)

pk = "0x" + PRIVATE_KEY if not PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
print(f"FUNDER: {FUNDER}")

# ── CLOB client ────────────────────────────────────────────────────────────────
venv = "/opt/polymarket-agent/venv/bin/python3"
if os.path.exists(venv):
    sys.path.insert(0, "/opt/polymarket-agent")

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

# ── Trades ─────────────────────────────────────────────────────────────────────
# 3 high-conviction NO positions on Iran war ending by March 31.
# Thesis: war actively escalating (week 3), strikes ramping up, no ceasefire talks.
# Total deployment: ~$375, leaving ~$155 cash for bot cycles.
TRADES = [
    {
        "desc": "US x Iran ceasefire by Mar 31 — NO",
        "token_id": "51938013536033607392847872760095315790110510345353215258271180769721415981927",
        "price": 0.94,
        "usdc": 150.0,
        "tick": "0.01",
        "reason": "War in week 3, strikes ramping up, NO formal talks open",
    },
    {
        "desc": "Iran x Israel/US conflict ends by Mar 31 — NO",
        "token_id": "60020156117014074723286818065417148342963800223162713452237763775488251828713",
        "price": 0.938,
        "usdc": 125.0,
        "tick": "0.001",
        "reason": "Conflict resolution requires both sides to announce — impossible in 10 days",
    },
    {
        "desc": "Will US invade Iran by Mar 31 — NO",
        "token_id": "38962164355021662310131812809985604710051173272585034985412007147499892280346",
        "price": 0.853,
        "usdc": 100.0,
        "tick": "0.001",
        "reason": "Trump said no invasion; market pricing 15% YES which is too high",
    },
]

report = ["<b>📊 Manual Iran War NO Trades</b>"]
placed = 0
total_deployed = 0.0

for t in TRADES:
    desc   = t["desc"]
    price  = t["price"]
    usdc   = min(t["usdc"], cash - 50)  # never leave less than $50
    tick   = t["tick"]
    shares = round(usdc / price, 2)

    if usdc < 10:
        print(f"Skipping {desc} — not enough cash remaining")
        continue

    print(f"\n→ {desc}")
    print(f"  ${usdc:.2f} @ {price} = {shares} shares")

    try:
        order = client.create_order(
            OrderArgs(price=price, size=shares, side=BUY, token_id=t["token_id"]),
            PartialCreateOrderOptions(tick_size=tick)
        )
        resp = client.post_order(order, OrderType.GTC)

        if isinstance(resp, dict):
            status = resp.get("status","?")
            err    = resp.get("errorMsg","") or ""
            oid    = (resp.get("orderID","") or "")[:20]
            if err:
                print(f"  ❌ {status}: {err}")
                report.append(f"❌ {desc}: {err[:60]}")
            else:
                print(f"  ✅ {status} | {oid}")
                report.append(f"✅ {desc[:50]} | ${usdc:.0f} @ {price:.3f}")
                placed += 1
                total_deployed += usdc
        else:
            print(f"  ?: {resp}")
            report.append(f"? {desc}: {str(resp)[:60]}")

    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        report.append(f"❌ {desc}: {str(e)[:80]}")

    time.sleep(2)

# ── Final balance ──────────────────────────────────────────────────────────────
time.sleep(3)
bal2 = client.get_balance_allowance(
    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
cash2 = float(bal2["balance"]) / 1e6
deployed_actual = round(cash - cash2, 2)

report.append(f"\nDeployed: ${deployed_actual:.2f} | Cash remaining: ${cash2:.2f}")
print(f"\n{'='*50}")
print(f"Placed: {placed}/{len(TRADES)} trades")
print(f"Deployed: ${deployed_actual:.2f} | Remaining: ${cash2:.2f}")

# ── Telegram ───────────────────────────────────────────────────────────────────
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
