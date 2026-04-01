#!/usr/bin/env python3
"""
merge_apr30.py — Merge matched YES+NO ceasefire Apr30 positions
================================================================
Converts 1,217 matched YES+NO pairs → $1,217 USDC via mergePositions()
on the Polymarket CTF contract. Uses raw HTTP JSON-RPC (no web3 connection
check) for reliability on restricted network environments.

Market: US x Iran ceasefire by April 30?
conditionId: 0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5
negativeRisk: False → standard CTF contract mergePositions()
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from web3 import Web3
from eth_account import Account

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID', '')

# Use raw HTTP JSON-RPC — avoids web3 connection check failures
RPCS = [
    "https://polygon-rpc.com",
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.meowrpc.com",
]

USDC        = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
CONDITION_ID = "0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5"
CHAIN_ID    = 137  # Polygon

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "partition",          "type": "uint256[]"},
            {"name": "amount",             "type": "uint256"}
        ],
        "name": "mergePositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception:
            pass

def rpc_call(method, params, rpc_url):
    """Raw JSON-RPC call. Returns result or raises."""
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers={"Content-Type": "application/json"},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data["result"]

def find_working_rpc():
    """Try each RPC until one responds to eth_blockNumber."""
    for rpc in RPCS:
        try:
            result = rpc_call("eth_blockNumber", [], rpc)
            block = int(result, 16)
            log(f"RPC working: {rpc} (block {block})")
            return rpc
        except Exception as e:
            log(f"RPC failed: {rpc} — {str(e)[:50]}")
    return None

def get_nonce(rpc, address):
    result = rpc_call("eth_getTransactionCount", [address, "pending"], rpc)
    return int(result, 16)

def get_gas_price(rpc):
    result = rpc_call("eth_gasPrice", [], rpc)
    return int(result, 16)

def send_raw_tx(rpc, raw_tx_hex):
    return rpc_call("eth_sendRawTransaction", [raw_tx_hex], rpc)

def get_tx_receipt(rpc, tx_hash, retries=20, delay=3):
    for _ in range(retries):
        try:
            result = rpc_call("eth_getTransactionReceipt", [tx_hash], rpc)
            if result is not None:
                return result
        except Exception:
            pass
        time.sleep(delay)
    return None

def get_positions():
    r = requests.get(
        f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
        timeout=15
    )
    return r.json() if r.status_code == 200 else []

def get_usdc_balance_clob():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY, chain_id=137,
            funder=FUNDER or None, signature_type=sig_type,
        )
        try:
            creds = client.create_or_derive_api_creds()
        except AttributeError:
            creds = client.derive_api_key()
        client.set_api_creds(creds)
        info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
        return float(info.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"CLOB balance error: {e}")
        return None

def main():
    log("=" * 55)
    log("MERGE APR30 CEASEFIRE POSITIONS")
    log("=" * 55)

    # --- Step 1: Verify positions ---
    log("Fetching positions...")
    positions = get_positions()

    yes_pos = next((p for p in positions
                    if "ceasefire" in p.get('title','').lower()
                    and "30" in p.get('title','')
                    and p.get('outcome') in ('Yes','YES')), None)
    no_pos  = next((p for p in positions
                    if "ceasefire" in p.get('title','').lower()
                    and "30" in p.get('title','')
                    and p.get('outcome') in ('No','NO')), None)

    if not yes_pos or not no_pos:
        log("ERROR: Could not find both YES and NO for Ceasefire Apr30")
        return

    yes_shares  = float(yes_pos['size'])
    no_shares   = float(no_pos['size'])
    merge_count = int(min(yes_shares, no_shares))

    log(f"YES: {yes_shares:.2f} shares")
    log(f"NO:  {no_shares:.2f} shares")
    log(f"Merging: {merge_count} pairs → ${merge_count:.2f} USDC")

    if merge_count < 10:
        log("Too few pairs — aborting")
        return

    # --- Step 2: Find working RPC ---
    rpc = find_working_rpc()
    if not rpc:
        log("ERROR: No working Polygon RPC found")
        tg("❌ <b>Merge failed</b> — no Polygon RPC reachable from server")
        return

    # --- Step 3: Build transaction ---
    w3 = Web3()  # local-only instance for ABI encoding — no network needed
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    condition_bytes  = bytes.fromhex(CONDITION_ID.replace("0x", ""))
    parent_collection = b'\x00' * 32
    partition        = [1, 2]           # YES=bit0, NO=bit1
    amount_raw       = merge_count * 10**6  # USDC has 6 decimals

    # Wallet architecture: SIGNER EOA (private key) is the actual on-chain sender.
    # FUNDER is a Safe proxy; SIGNER has isApprovedForAll on CTF on behalf of FUNDER.
    # On-chain txs originate from SIGNER (nonce 9), not FUNDER (nonce 1).
    signer_address = Web3.to_checksum_address(Account.from_key(PRIVATE_KEY).address)
    funder_address = Web3.to_checksum_address(FUNDER)
    log(f"Signer: {signer_address} — tx sender")
    log(f"Funder: {funder_address} — token holder")

    nonce     = get_nonce(rpc, signer_address)  # SIGNER nonce = 9
    gas_price = get_gas_price(rpc)
    gas_price_boosted = int(gas_price * 1.3)

    log(f"Nonce: {nonce} | Gas: {gas_price_boosted/1e9:.1f} gwei")

    # Encode the function call
    data = ctf.encode_abi(
        "mergePositions",
        args=[USDC, parent_collection, condition_bytes, partition, amount_raw]
    )

    tx = {
        'from':     signer_address,   # SIGNER EOA sends the tx
        'nonce':    nonce,
        'to':       CTF_ADDRESS,
        'value':    0,
        'gas':      300000,
        'gasPrice': gas_price_boosted,
        'data':     data,
        'chainId':  CHAIN_ID,
    }

    # --- Step 4: Sign and send ---
    signed   = Account.sign_transaction(tx, PRIVATE_KEY)
    raw_hex  = signed.raw_transaction.hex()
    if not raw_hex.startswith('0x'):
        raw_hex = '0x' + raw_hex

    log(f"Sending transaction...")
    try:
        tx_hash = send_raw_tx(rpc, raw_hex)
        log(f"Tx hash: {tx_hash}")
    except Exception as e:
        log(f"ERROR sending tx: {e}")
        tg(f"❌ <b>Merge tx failed</b>: {str(e)[:150]}")
        return

    # --- Step 5: Wait for receipt ---
    log("Waiting for confirmation...")
    receipt = get_tx_receipt(rpc, tx_hash)

    if receipt is None:
        log("Timeout waiting for receipt — check tx manually")
        tg(f"⚠️ <b>Merge tx sent</b> but receipt not confirmed\nTx: <code>{tx_hash[:20]}...</code>")
        return

    status = int(receipt.get('status', '0x0'), 16)

    if status == 1:
        log(f"✓ MERGE SUCCESS — {merge_count} pairs merged")
        log(f"  Block: {int(receipt.get('blockNumber','0x0'), 16)}")
        log(f"  Gas used: {int(receipt.get('gasUsed','0x0'), 16)}")

        time.sleep(4)
        new_bal = get_usdc_balance_clob()
        bal_str = f"${new_bal:.2f}" if new_bal else "updating..."

        msg = (
            f"✅ <b>Merge complete</b> — Ceasefire Apr30\n\n"
            f"Merged {merge_count} YES+NO pairs\n"
            f"<b>USDC recovered: ~${merge_count:.2f}</b>\n"
            f"CLOB balance now: {bal_str}\n\n"
            f"Remaining: {yes_shares - merge_count:.0f} YES shares\n"
            f"LP quoter re-activates on next hourly run ✅\n\n"
            f"Tx: <code>{tx_hash[:24]}...</code>"
        )
        log(msg.replace('<b>','').replace('</b>','').replace('<code>','').replace('</code>',''))
        tg(msg)

    else:
        log(f"✗ TX REVERTED — status={status}")
        log(f"  Tx: {tx_hash}")
        tg(f"❌ <b>Merge reverted</b>\nTx: <code>{tx_hash[:20]}...</code>")


if __name__ == "__main__":
    main()
