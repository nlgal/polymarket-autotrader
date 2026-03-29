"""
deposit_safe.py
===============
Deposits USDC from Gnosis Safe to Polymarket CLOB.
Uses Safe REST API + py-clob-client — no direct RPC/Web3 needed.

The py-clob-client handles the actual on-chain transaction using the 
private key directly. The Safe's signer (EOA) just needs to call deposit().
"""
import os, sys, time, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN','')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

print(f"Funder (Safe): {FUNDER}")

# Check Safe USDC balance via Safe API
r = requests.get(
    f"https://safe-transaction-polygon.safe.global/api/v1/safes/{FUNDER}/balances/",
    timeout=15
)
balances = r.json()

usdc_balance_raw = 0
usdc_symbol = ""
for b in balances:
    token = b.get("token")
    if token and token.get("symbol","") in ["USDC", "USDC.E", "USDC.e"]:
        usdc_balance_raw = int(b.get("balance","0"))
        usdc_symbol = token.get("symbol","USDC")
        break

usdc_balance = usdc_balance_raw / 1e6
print(f"Safe {usdc_symbol} balance: ${usdc_balance:.2f}")

if usdc_balance < 10:
    print("Balance too low — nothing to deposit")
    sys.exit(0)

# Keep $5 buffer in Safe
deposit_usdc = usdc_balance - 5.0
deposit_raw = int(deposit_usdc * 1e6)
print(f"Depositing: ${deposit_usdc:.2f} USDC (keeping $5 buffer)")

# Use py-clob-client to deposit
# The client handles the Safe's proxy wallet mechanics
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

try:
    client = ClobClient(
        "https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2,
        funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    
    # Check current Polymarket balance before deposit
    before_r = requests.get(
        f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=10
    )
    before_val = before_r.json()
    before_equity = float(before_val[0]["value"]) if isinstance(before_val, list) else float(before_val["value"])
    print(f"Current Polymarket equity: ${before_equity:.2f}")
    
    print(f"\nCalling client.deposit({deposit_raw})...")
    result = client.deposit(deposit_raw)
    print(f"Result: {result}")
    
    time.sleep(15)  # Wait for on-chain confirmation
    
    # Check new balance
    after_r = requests.get(
        f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=10
    )
    after_val = after_r.json()
    after_equity = float(after_val[0]["value"]) if isinstance(after_val, list) else float(after_val["value"])
    print(f"New Polymarket equity: ${after_equity:.2f}")
    print(f"Deposited: ${after_equity - before_equity:.2f}")
    
    tg(f"""<b>💰 Safe Deposit Complete</b>
Deposited: ${deposit_usdc:.2f} USDC
New equity: ${after_equity:.2f}
Old equity: ${before_equity:.2f}""")
    
except Exception as e:
    print(f"Deposit error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\nDone!")
