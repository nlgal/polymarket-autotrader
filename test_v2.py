"""
Test py-clob-client-v2 against clob-v2.polymarket.com (pre-cutover staging).
Checks: auth, balance, order creation (dry-run only).
"""
import os, sys
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    BalanceAllowanceParams, AssetType, OrderArgs, PartialCreateOrderOptions
)
from py_clob_client_v2.order_builder.constants import BUY

KEY    = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))

# Test against V2 staging URL
HOST_V2 = "https://clob-v2.polymarket.com"

client = ClobClient(HOST_V2, key=KEY, chain_id=137, signature_type=SIG, funder=FUNDER or None)
try:
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    print("V2 API auth: ✓")
except Exception as e:
    print(f"V2 API auth failed: {e}")
    sys.exit(1)

# Check balance on V2
try:
    bal = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=SIG)
    )
    usdc = float(bal.get("balance", 0)) / 1e6
    print(f"V2 CLOB balance: ${usdc:.4f}")
    print(f"V2 balance raw: {bal}")
except Exception as e:
    print(f"V2 balance check failed: {e}")

# Test order creation (no posting — just signing)
try:
    order = client.create_order(
        OrderArgs(
            token_id="55115078421062885512539156303747803058407616201213034911037320915726138659123",
            price=0.30,
            size=10.0,
            side=BUY
        )
    )
    print(f"V2 create_order: ✓ (order signed, not posted)")
    print(f"  order type: {type(order).__name__}")
except Exception as e:
    print(f"V2 create_order failed: {e}")

print("\nV2 migration test complete.")
