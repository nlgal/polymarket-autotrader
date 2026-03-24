
import os, sys
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import SELL
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))
TOKEN_ID = "96839284769036407740491691016901048240322264125970194871307313464800669089139"

client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137,
                    creds=ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                                   api_secret=os.environ.get("CLOB_API_SECRET",""),
                                   api_passphrase=os.environ.get("CLOB_API_PASSPHRASE","")),
                    signature_type=SIG_TYPE, funder=FUNDER)

try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("Creds OK")
except Exception as e:
    print(f"Creds: {e}")

# Check current allowance
try:
    r = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID, signature_type=2))
    print(f"Conditional allowance: {r}")
except Exception as e:
    print(f"Get allowance error: {e}")

try:
    r2 = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    print(f"Collateral allowance: {r2}")
except Exception as e:
    print(f"Get collateral allowance: {e}")

# Try update
try:
    r3 = client.update_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID, signature_type=2))
    print(f"Update conditional: {r3}")
except Exception as e:
    print(f"Update conditional error: {e}")
