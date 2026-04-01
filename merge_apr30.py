#!/usr/bin/env python3
"""
merge_apr30.py — Merge matched YES+NO ceasefire Apr30 positions
================================================================
Converts 1,217 matched YES+NO pairs → ~$1,217 USDC by calling
Safe.execTransaction → CTF.mergePositions() on Polygon.

Wallet architecture:
  FUNDER = Safe 1.3.0 proxy  (holds the conditional tokens)
  SIGNER = EOA owner of Safe  (private key signs Safe tx)

Safe signature fix (GS026 root cause):
  Previous code used encode_defunct() which prepends the Ethereum personal-sign
  prefix "\x19Ethereum Signed Message:\n32", causing the recovered address to
  differ from the Safe owner → GS026 "Invalid owner provided".

  Correct approach: sign the raw SafeTxHash bytes directly using
  eth_keys.PrivateKey.sign_msg_hash() — no prefix, v = 27 or 28.
  Safe 1.3.0 validates this as a standard EIP-712 signature.

Market: US x Iran ceasefire by April 30?
conditionId: 0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from web3 import Web3
from eth_account import Account
from eth_keys import keys as eth_keys

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']  # Safe proxy
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID', '')

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",  # most reliable from server
    "https://polygon-rpc.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]

USDC         = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
CONDITION_ID = "0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5"
ZERO_ADDR    = "0x0000000000000000000000000000000000000000"
CHAIN_ID     = 137

CTF_ABI = [{"inputs":[
    {"name":"collateralToken","type":"address"},
    {"name":"parentCollectionId","type":"bytes32"},
    {"name":"conditionId","type":"bytes32"},
    {"name":"partition","type":"uint256[]"},
    {"name":"amount","type":"uint256"}
],"name":"mergePositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]

SAFE_ABI = [
    {"inputs":[],"name":"nonce",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[
        {"name":"to","type":"address"},{"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}
    ],"name":"getTransactionHash","outputs":[{"name":"","type":"bytes32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[
        {"name":"to","type":"address"},{"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}
    ],"name":"execTransaction","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def rpc_post(method, params, rpc_url):
    r = requests.post(rpc_url,
        json={"jsonrpc":"2.0","method":method,"params":params,"id":1},
        headers={"Content-Type":"application/json"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data["result"]

def find_rpc():
    for rpc in RPCS:
        try:
            block = int(rpc_post("eth_blockNumber", [], rpc), 16)
            log(f"RPC: {rpc.split('//')[-1]} (block {block})")
            return rpc
        except Exception as e:
            log(f"RPC fail: {rpc.split('//')[-1]}: {str(e)[:50]}")
    return None

def get_positions():
    r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50", timeout=15)
    return r.json() if r.status_code == 200 else []

def get_clob_balance():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = ClobClient(host="https://clob.polymarket.com", key=PRIVATE_KEY,
            chain_id=137, funder=FUNDER or None,
            signature_type=int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2")))
        try:    creds = client.create_or_derive_api_creds()
        except: creds = client.derive_api_key()
        client.set_api_creds(creds)
        info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
        return float(info.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"CLOB balance error: {e}")
        return None

# ── Signing ───────────────────────────────────────────────────────────────────

def sign_safe_hash(safe_tx_hash_bytes: bytes) -> bytes:
    """
    Sign a Safe transaction hash using raw EIP-712 signing (no personal_sign prefix).

    Safe 1.3.0 signature validation:
      - v = 27 → standard secp256k1 (ethers/web3 convention)
      - v = 28 → standard secp256k1 (odd y-parity)
      - v >= 31 → eth_sign (adds personal_sign prefix) — NOT what we want
      - v = 0/1 → contract signature

    The fix: eth_keys.sign_msg_hash() signs the raw bytes32 without any prefix,
    producing v=0 or v=1 (parity). Adding 27 gives v=27 or v=28, which Safe
    interprets as a standard EIP-712 signature and recovers correctly.

    The old code used Account.sign_message(encode_defunct(primitive=hash_bytes))
    which prepends "\x19Ethereum Signed Message:\n32", recovering to a different
    address → GS026 "Invalid owner provided".
    """
    pk_obj  = eth_keys.PrivateKey(bytes.fromhex(PRIVATE_KEY.replace("0x", "")))
    sig_obj = pk_obj.sign_msg_hash(safe_tx_hash_bytes)   # raw hash, no prefix
    r_bytes = sig_obj.r.to_bytes(32, 'big')
    s_bytes = sig_obj.s.to_bytes(32, 'big')
    v_byte  = sig_obj.v + 27   # 0→27 or 1→28 (EIP-712 convention)
    return r_bytes + s_bytes + bytes([v_byte])

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 55)
    log("MERGE APR30 CEASEFIRE — v2 (Safe execTransaction)")
    log("=" * 55)

    # 1. Verify positions
    log("Fetching positions...")
    positions = get_positions()
    yes_pos = next((p for p in positions if "ceasefire" in p.get('title','').lower()
                    and "30" in p.get('title','') and p.get('outcome') in ('Yes','YES')), None)
    no_pos  = next((p for p in positions if "ceasefire" in p.get('title','').lower()
                    and "30" in p.get('title','') and p.get('outcome') in ('No','NO')), None)

    if not yes_pos or not no_pos:
        log("ERROR: Cannot find both YES and NO for Ceasefire Apr30")
        tg("❌ <b>Merge failed</b> — positions not found")
        return

    yes_shares  = float(yes_pos['size'])
    no_shares   = float(no_pos['size'])
    merge_count = int(min(yes_shares, no_shares))

    log(f"YES: {yes_shares:.2f} sh | NO: {no_shares:.2f} sh")
    log(f"Merging: {merge_count} pairs → ~${merge_count:.2f} USDC")

    if merge_count < 10:
        log("Too few pairs to merge — aborting"); return

    # 2. Find RPC
    rpc = find_rpc()
    if not rpc:
        log("ERROR: No Polygon RPC reachable")
        tg("❌ <b>Merge failed</b> — no RPC reachable"); return

    # 3. ABI helpers (local encoding only)
    w3   = Web3()
    ctf  = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    safe = w3.eth.contract(address=Web3.to_checksum_address(FUNDER), abi=SAFE_ABI)

    signer_addr = Web3.to_checksum_address(Account.from_key(PRIVATE_KEY).address)
    funder_addr = Web3.to_checksum_address(FUNDER)
    log(f"Signer (EOA):  {signer_addr}")
    log(f"Funder (Safe): {funder_addr}")

    # 4. Build mergePositions calldata (inner call from Safe → CTF)
    merge_data = ctf.encode_abi("mergePositions", args=[
        USDC,
        b'\x00' * 32,                                    # parentCollectionId = 0
        bytes.fromhex(CONDITION_ID.replace("0x", "")),   # conditionId
        [1, 2],                                          # partition: YES=1, NO=2
        merge_count * 10**6,                             # amount in USDC decimals
    ])
    log(f"mergePositions calldata: {len(merge_data)} bytes")

    # 5. Get Safe nonce
    nonce_raw = rpc_post("eth_call",
        [{"to": funder_addr, "data": safe.encode_abi("nonce", [])}, "latest"], rpc)
    safe_nonce = int(nonce_raw, 16)
    log(f"Safe nonce: {safe_nonce}")

    # 6. Compute SafeTxHash via getTransactionHash()
    tx_hash_data = safe.encode_abi("getTransactionHash", args=[
        CTF_ADDRESS, 0, merge_data, 0,   # to, value, data, operation(CALL=0)
        0, 0, 0,                         # safeTxGas, baseGas, gasPrice
        ZERO_ADDR, ZERO_ADDR,            # gasToken, refundReceiver
        safe_nonce,
    ])
    safe_tx_hash_hex = rpc_post("eth_call",
        [{"to": funder_addr, "data": tx_hash_data}, "latest"], rpc)
    safe_tx_hash_bytes = bytes.fromhex(safe_tx_hash_hex.replace("0x", ""))
    log(f"SafeTxHash: {safe_tx_hash_hex}")

    # 7. Sign raw hash (EIP-712, no personal_sign prefix) — fixes GS026
    signature = sign_safe_hash(safe_tx_hash_bytes)
    log(f"Signature: v={signature[64]} r={signature[:4].hex()}... (65 bytes)")

    # 8. Build execTransaction calldata (outer call SIGNER → Safe)
    exec_data = safe.encode_abi("execTransaction", args=[
        CTF_ADDRESS, 0, merge_data, 0,
        0, 0, 0,
        ZERO_ADDR, ZERO_ADDR,
        signature,
    ])
    log(f"execTransaction calldata: {len(exec_data)} bytes")

    # 9. Get SIGNER nonce (EOA sends the outer tx)
    nonces = {}
    for _rpc in RPCS:
        try:
            n = int(requests.post(_rpc, json={"jsonrpc":"2.0","method":"eth_getTransactionCount",
                "params":[signer_addr,"pending"],"id":1}, timeout=8).json()["result"], 16)
            nonces[_rpc] = n
            log(f"  Nonce @ {_rpc.split('//')[-1][:28]}: {n}")
        except Exception as e:
            log(f"  Nonce fail @ {_rpc.split('//')[-1][:20]}: {str(e)[:40]}")

    if not nonces:
        log("ERROR: Cannot get SIGNER nonce"); return
    signer_nonce = max(nonces.values())
    log(f"Using SIGNER nonce: {signer_nonce}")

    # 10. Get gas price
    gas_price = int(rpc_post("eth_gasPrice", [], rpc), 16)
    gas_boosted = int(gas_price * 1.3)
    log(f"Gas: {gas_boosted/1e9:.1f} gwei")

    # 11. Build, sign, send outer tx (SIGNER → Safe.execTransaction)
    outer_tx = {
        'from':     signer_addr,
        'nonce':    signer_nonce,
        'to':       funder_addr,         # Safe proxy address
        'value':    0,
        'gas':      400000,
        'gasPrice': gas_boosted,
        'data':     exec_data,
        'chainId':  CHAIN_ID,
    }
    signed    = Account.sign_transaction(outer_tx, PRIVATE_KEY)
    raw_hex   = "0x" + signed.raw_transaction.hex().lstrip("0x")

    log("Sending execTransaction...")
    try:
        tx_hash = rpc_post("eth_sendRawTransaction", [raw_hex], rpc)
        log(f"Tx sent: {tx_hash}")
    except Exception as e:
        log(f"ERROR sending: {e}")
        tg(f"❌ <b>Merge send failed</b>: {str(e)[:150]}"); return

    # 12. Wait for receipt
    log("Waiting for confirmation...")
    receipt = None
    for attempt in range(24):   # up to 72 seconds
        time.sleep(3)
        try:
            result = rpc_post("eth_getTransactionReceipt", [tx_hash], rpc)
            if result:
                receipt = result
                break
        except Exception:
            pass

    if receipt is None:
        log("Timeout — check tx manually")
        tg(f"⚠️ <b>Merge tx sent</b>, receipt pending\nTx: <code>{tx_hash[:22]}...</code>")
        return

    status = int(receipt.get('status', '0x0'), 16)

    if status == 1:
        gas_used = int(receipt.get('gasUsed','0x0'), 16)
        block_no = int(receipt.get('blockNumber','0x0'), 16)
        log(f"✓ MERGE SUCCESS — {merge_count} pairs → ~${merge_count:.2f} USDC")
        log(f"  Block {block_no} | Gas used {gas_used:,}")

        time.sleep(5)
        new_bal = get_clob_balance()
        bal_str = f"${new_bal:.2f}" if new_bal else "syncing..."

        msg = (
            f"✅ <b>Merge complete</b> — Ceasefire Apr30\n\n"
            f"Merged <b>{merge_count}</b> YES+NO pairs\n"
            f"USDC recovered: <b>~${merge_count:.2f}</b>\n"
            f"CLOB balance: {bal_str}\n\n"
            f"Remaining: {yes_shares - merge_count:.0f} YES shares\n"
            f"LP quoter re-activates next run ✅\n\n"
            f"Tx: <code>{tx_hash[:26]}...</code>"
        )
        log(msg.replace("<b>","").replace("</b>","").replace("<code>","").replace("</code>",""))
        tg(msg)

    else:
        log(f"✗ TX REVERTED (status=0)")
        log(f"  Tx: {tx_hash}")
        tg(f"❌ <b>Merge reverted</b>\nTx: <code>{tx_hash[:22]}...</code>")


if __name__ == "__main__":
    main()
