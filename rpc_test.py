
import sys
sys.path.insert(0, '/opt/polymarket-agent')
from web3 import Web3
import requests as req

rpcs = [
    "https://polygon-rpc.com",
    "https://1rpc.io/matic", 
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
    "https://bor.hindenburg.com",
    "https://endpoints.omniatech.io/v1/matic/mainnet/public",
]

# Also try JSON-RPC directly
for rpc in rpcs:
    try:
        # Direct JSON-RPC call (faster than web3)
        resp = req.post(rpc, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=6)
        if resp.status_code == 200 and resp.json().get("result"):
            block = int(resp.json()["result"], 16)
            print(f"WORKING: {rpc} block={block}")
        else:
            print(f"FAIL: {rpc} status={resp.status_code}")
    except Exception as e:
        print(f"ERROR: {rpc} {str(e)[:40]}")
