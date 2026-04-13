import os, sys
sys.path.insert(0, '/opt/polymarket-agent')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

HOST = "https://clob.polymarket.com"
KEY  = os.environ.get("PRIVATE_KEY")
CHAIN_ID = 137

try:
    client = ClobClient(HOST, key=KEY, chain_id=CHAIN_ID, signature_type=2,
                        funder=os.environ.get("FUNDER_ADDRESS"))
    bal = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
    )
    raw = float(bal.get("balance", 0))
    usdc = raw / 1e6
    print(f"CLOB USDC balance: ${usdc:.4f}")
    print(f"Raw wei: {raw}")
    print(f"Full response: {bal}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
