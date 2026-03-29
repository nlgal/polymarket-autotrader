"""
deposit_safe.py
===============
Deposits USDC from Gnosis Safe (funder wallet) to Polymarket CLOB.
Safe is 1-of-1, signer is the EOA with the private key.

Flow:
1. Check current USDC balance in Safe
2. Approve USDC to Polymarket CTF Exchange contract
3. Deposit via Safe's execTransaction

This is done via py-gnosis-safe or direct EIP-712 signing.
"""
import os, sys, json, time
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
import requests

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']  # Safe address
SIGNER_ADDR = "0x7C67b2e2082Fa089E1B703aA248eE17B9E56bBF6"

# Polygon RPC
RPC = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(RPC))
account: LocalAccount = Account.from_key(PRIVATE_KEY)
print(f"Signer: {account.address}")
print(f"Safe (funder): {FUNDER}")

# Contract addresses on Polygon
USDC_E = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_BRIDGED = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
SAFE_ADDR = Web3.to_checksum_address(FUNDER)

# ERC20 ABI (minimal)
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "inputs": [{"name": "_owner", "type": "address"}], 
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "approve", "type": "function", 
     "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "allowance", "type": "function",
     "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

# Check which USDC the Safe holds
usdc_e = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
usdc_b = w3.eth.contract(address=USDC_BRIDGED, abi=ERC20_ABI)

bal_e = usdc_e.functions.balanceOf(SAFE_ADDR).call()
bal_b = usdc_b.functions.balanceOf(SAFE_ADDR).call()

print(f"\nUSDC (native) in Safe: ${bal_e/1e6:.2f}")
print(f"USDC.e (bridged) in Safe: ${bal_b/1e6:.2f}")

# Use whichever has balance
if bal_e >= bal_b:
    usdc = usdc_e
    usdc_addr = USDC_E
    balance = bal_e
    token_name = "USDC (native)"
else:
    usdc = usdc_b
    usdc_addr = USDC_BRIDGED
    balance = bal_b
    token_name = "USDC.e (bridged)"

print(f"\nUsing {token_name}: ${balance/1e6:.2f}")

if balance < 1_000_000:  # Less than $1
    print("Balance too low to deposit")
    sys.exit(0)

# Keep $5 buffer in Safe for gas
deposit_amount = balance - 5_000_000  # keep $5 buffer
print(f"Depositing: ${deposit_amount/1e6:.2f} (keeping $5 buffer)")

# Check current allowance
allowance = usdc.functions.allowance(SAFE_ADDR, CTF_EXCHANGE).call()
print(f"Current allowance to CTF Exchange: ${allowance/1e6:.2f}")

# Safe Gnosis API for executing transactions
SAFE_API = f"https://safe-transaction-polygon.safe.global/api/v1/safes/{FUNDER}"

def safe_nonce():
    r = requests.get(f"{SAFE_API}/", timeout=10)
    return r.json()["nonce"]

def encode_approve(spender, amount):
    """Encode ERC20.approve(spender, amount) calldata"""
    return usdc.encode_abi(fn_name="approve", args=[spender, amount])

def build_safe_tx(to, data, value=0, nonce=None):
    """Build a Safe transaction dict"""
    if nonce is None:
        nonce = safe_nonce()
    return {
        "to": to,
        "value": str(value),
        "data": data.hex() if isinstance(data, bytes) else data,
        "operation": 0,  # CALL
        "safeTxGas": "0",
        "baseGas": "0", 
        "gasPrice": "0",
        "gasToken": "0x0000000000000000000000000000000000000000",
        "refundReceiver": "0x0000000000000000000000000000000000000000",
        "nonce": nonce,
    }

def sign_and_execute_safe_tx(tx_dict):
    """Sign and execute a Safe transaction using EIP-712"""
    from eth_account.messages import encode_defunct
    from eth_abi import encode as abi_encode
    import hashlib
    
    SAFE_TX_TYPEHASH = bytes.fromhex(
        "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
    )
    
    # Encode the Safe transaction
    encoded = abi_encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8", 
         "uint256", "uint256", "uint256", "address", "address", "uint256"],
        [
            SAFE_TX_TYPEHASH,
            Web3.to_checksum_address(tx_dict["to"]),
            int(tx_dict["value"]),
            w3.keccak(hexstr=tx_dict["data"] if tx_dict["data"].startswith("0x") else "0x" + tx_dict["data"]),
            int(tx_dict["operation"]),
            int(tx_dict["safeTxGas"]),
            int(tx_dict["baseGas"]),
            int(tx_dict["gasPrice"]),
            Web3.to_checksum_address(tx_dict["gasToken"]),
            Web3.to_checksum_address(tx_dict["refundReceiver"]),
            int(tx_dict["nonce"]),
        ]
    )
    tx_hash = w3.keccak(encoded)
    
    # EIP-712 domain
    DOMAIN_SEPARATOR_TYPEHASH = bytes.fromhex(
        "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
    )
    chain_id = 137  # Polygon
    domain_encoded = abi_encode(
        ["bytes32", "uint256", "address"],
        [DOMAIN_SEPARATOR_TYPEHASH, chain_id, SAFE_ADDR]
    )
    domain_separator = w3.keccak(domain_encoded)
    
    final_hash = w3.keccak(b"\x19\x01" + domain_separator + tx_hash)
    
    # Sign
    signed = account.sign_message(encode_defunct(hexstr=final_hash.hex()))
    sig = signed.signature.hex()
    
    # Post to Safe API
    payload = {
        **tx_dict,
        "contractTransactionHash": final_hash.hex(),
        "sender": account.address,
        "signature": sig,
    }
    
    r = requests.post(f"{SAFE_API}/multisig-transactions/", json=payload, timeout=30)
    print(f"Safe API response: {r.status_code} | {r.text[:200]}")
    return r.status_code in [200, 201], r.text

# Step 1: Approve USDC to CTF Exchange if needed
if allowance < deposit_amount:
    print(f"\nApproving USDC to CTF Exchange...")
    approve_data = encode_approve(CTF_EXCHANGE, deposit_amount)
    nonce = safe_nonce()
    tx = build_safe_tx(usdc_addr, approve_data, nonce=nonce)
    ok, resp = sign_and_execute_safe_tx(tx)
    if ok:
        print(f"✓ Approval submitted to Safe")
        time.sleep(5)
    else:
        print(f"✗ Approval failed: {resp}")
        sys.exit(1)
else:
    print(f"✓ Allowance already sufficient (${allowance/1e6:.2f})")

# Step 2: Deposit via py-clob-client
# The deposit is done through the CLOB client's deposit method
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=POLYGON,
    signature_type=2,
    funder=FUNDER,
)
client.set_api_creds(client.create_or_derive_api_creds())

print(f"\nDepositing ${deposit_amount/1e6:.2f} USDC to Polymarket...")
try:
    result = client.deposit(deposit_amount)
    print(f"✓ Deposit result: {result}")
except Exception as e:
    print(f"Deposit error: {e}")
    print("Note: May need separate approve tx through Safe first")

print("\nDone. Check Polymarket balance in ~30 seconds.")
