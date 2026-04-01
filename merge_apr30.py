#!/usr/bin/env python3
"""
merge_apr30.py — Merge matched YES+NO ceasefire Apr30 positions
================================================================
Calls mergePositions() on the Polymarket CTF contract to convert
1,217 matched YES+NO pairs → $1,217 USDC, freeing cash for LP quoting.

Market: US x Iran ceasefire by April 30?
conditionId: 0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5
negativeRisk: False → standard CTF contract

mergePositions(collateralToken, parentCollectionId, conditionId, partition, amount)
  partition = [1, 2]  (YES = indexSet 1, NO = indexSet 2 — both sides)
  amount    = number of pairs to merge (in shares, scaled to 1e6 for USDC decimals)
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from web3 import Web3

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID', '')

RPC_URL     = "https://1rpc.io/matic"  # primary
RPC_BACKUPS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]
USDC        = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

# Ceasefire Apr30
CONDITION_ID = "0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5"

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
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
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

def get_positions():
    r = requests.get(
        f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
        timeout=15
    )
    return r.json() if r.status_code == 200 else []

def get_usdc_balance_clob():
    """Get USDC balance in CLOB (off-chain)."""
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
        log("ERROR: Could not find both YES and NO positions for Ceasefire Apr30")
        return

    yes_shares = float(yes_pos['size'])
    no_shares  = float(no_pos['size'])
    merge_amount = int(min(yes_shares, no_shares))  # floor to integer shares

    log(f"YES: {yes_shares:.2f} shares")
    log(f"NO:  {no_shares:.2f} shares")
    log(f"Merging: {merge_amount} pairs → ${merge_amount:.2f} USDC")

    if merge_amount < 10:
        log("Too few pairs to merge — aborting")
        return

    # --- Step 2: Connect to Polygon ---
    log("Connecting to Polygon...")
    w3 = None
    for rpc in [RPC_URL] + RPC_BACKUPS:
        try:
            _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if _w3.is_connected():
                w3 = _w3
                log(f"Connected via {rpc} — block {w3.eth.block_number}")
                break
        except Exception:
            continue
    if w3 is None:
        log("ERROR: Cannot connect to any Polygon RPC")
        tg("\u274c <b>Merge failed</b> \u2014 cannot connect to Polygon RPC")
        return
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    account = Web3.to_checksum_address(FUNDER)

    # --- Step 3: Build mergePositions tx ---
    # mergePositions params:
    #   collateralToken    = USDC address
    #   parentCollectionId = bytes32(0) — top-level market
    #   conditionId        = the market condition ID
    #   partition          = [1, 2] — YES (bit 0) and NO (bit 1) together
    #   amount             = shares to merge × 1e6 (USDC has 6 decimals)

    condition_bytes = bytes.fromhex(CONDITION_ID.replace("0x", ""))
    parent_collection = b'\x00' * 32
    partition = [1, 2]              # YES=1, NO=2 — both must be included
    amount_raw = merge_amount * 10**6  # scale to 6 decimals

    log(f"Building mergePositions tx: amount={merge_amount} ({amount_raw} raw)")

    try:
        nonce     = w3.eth.get_transaction_count(account)
        gas_price = w3.eth.gas_price
        gas_price_boosted = int(gas_price * 1.3)  # 30% tip to ensure inclusion

        tx = ctf.functions.mergePositions(
            USDC,
            parent_collection,
            condition_bytes,
            partition,
            amount_raw
        ).build_transaction({
            'from':     account,
            'nonce':    nonce,
            'gas':      250000,
            'gasPrice': gas_price_boosted,
        })

        log(f"Gas price: {gas_price_boosted / 1e9:.1f} gwei | Gas limit: 250000")

        # --- Step 4: Sign and send ---
        signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        log(f"Tx sent: 0x{tx_hash.hex()}")
        log("Waiting for confirmation (up to 60s)...")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status == 1:
            log(f"✓ MERGE SUCCESS — {merge_amount} pairs merged")
            log(f"  Tx: 0x{tx_hash.hex()}")
            log(f"  Block: {receipt.blockNumber} | Gas used: {receipt.gasUsed}")

            # Check new CLOB balance
            time.sleep(3)
            new_balance = get_usdc_balance_clob()
            balance_str = f"${new_balance:.2f}" if new_balance else "checking..."

            msg = (
                f"✅ <b>Merge complete</b> — Ceasefire Apr30\n\n"
                f"Merged {merge_amount} YES+NO pairs\n"
                f"<b>USDC recovered: ${merge_amount:.2f}</b>\n"
                f"CLOB balance now: {balance_str}\n\n"
                f"Remaining: {yes_shares - merge_amount:.0f} YES shares\n"
                f"Tx: <code>0x{tx_hash.hex()[:20]}...</code>\n"
                f"LP quoter will re-activate on next hourly run"
            )
            log(msg.replace("<b>","").replace("</b>","").replace("<code>","").replace("</code>",""))
            tg(msg)

        else:
            log(f"✗ MERGE FAILED — tx reverted")
            log(f"  Tx: 0x{tx_hash.hex()}")
            tg(f"❌ <b>Merge failed</b> — tx reverted\nTx: 0x{tx_hash.hex()[:20]}...")

    except Exception as e:
        log(f"ERROR: {e}")
        tg(f"❌ <b>Merge error</b>: {str(e)[:200]}")


if __name__ == "__main__":
    main()
