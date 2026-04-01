"""
auto_redeem.py
==============
Automatically redeems resolved winning Polymarket positions.

How Polymarket redemption works:
- When a market resolves, winning conditional tokens (YES or NO) are worth $1 each
- You must call redeemPositions() on the CTF contract to convert them to USDC
- Polymarket's UI does this automatically but the bot wallet needs explicit redemption

This script:
1. Fetches all positions for the funder wallet
2. Checks each market's resolution status via gamma API
3. For resolved markets where we hold the WINNING token:
   - Calls redeemPositions() on the CTF contract via web3.py
4. Sends Telegram alert with total USDC recovered

Run after any market expiry (crude oil March 31, Iran April 30, etc.)
Also run by health monitor automatically.
"""
import os, sys, json, time, requests, datetime
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from web3 import Web3
_poa_mw = None  # POA middleware not required on Polygon for read/write ops

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID', '')

# Polygon RPC
RPC_URL = "https://polygon.drpc.org"

# CTF contract (Gnosis Conditional Token Framework) on Polygon
CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# Minimal CTF ABI — just redeemPositions
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"}
        ],
        "name": "payoutNumerators",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"}
        ],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg[:4000], 'parse_mode': 'HTML'}, timeout=10)
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}')

def get_positions():
    r = requests.get(
        f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=100',
        timeout=15
    )
    return r.json() if r.status_code == 200 else []

def check_market_resolved(condition_id):
    """
    Returns (is_resolved, winning_outcome_index) or (False, None).
    winning_outcome_index: 0=YES won, 1=NO won
    """
    try:
        r = requests.get(
            f'https://gamma-api.polymarket.com/markets?conditionId={condition_id}',
            timeout=10
        )
        if r.status_code != 200:
            return False, None
        markets = r.json()
        if not markets:
            return False, None
        m = markets[0]

        # Check if resolved via outcomePrices — resolved market has 0/1 or 1/0
        prices = m.get('outcomePrices', '')
        if isinstance(prices, str):
            try:
                prices = [float(x.strip().strip('"')) for x in prices.strip('[]').split(',')]
            except:
                return False, None

        if not m.get('closed', False) and not m.get('resolved', False):
            return False, None

        # Find winning outcome (price = 1.0)
        for i, p in enumerate(prices):
            if float(p) >= 0.99:
                return True, i

        return False, None
    except Exception as e:
        log(f'  check_resolved error: {e}')
        return False, None

def redeem_position(w3, ctf, condition_id_hex, outcome_index, position):
    """
    Call redeemPositions() on the CTF contract.
    outcome_index: 0=YES, 1=NO
    indexSet: bitmask — 1 for YES (bit 0), 2 for NO (bit 1)
    """
    try:
        condition_id_bytes = bytes.fromhex(condition_id_hex.replace('0x', ''))
        parent_collection = b'\x00' * 32
        index_sets = [1 << outcome_index]  # 1 for YES, 2 for NO

        account = Web3.to_checksum_address(FUNDER)
        nonce = w3.eth.get_transaction_count(account)
        gas_price = w3.eth.gas_price

        tx = ctf.functions.redeemPositions(
            USDC_ADDRESS,
            parent_collection,
            condition_id_bytes,
            index_sets
        ).build_transaction({
            'from':     account,
            'nonce':    nonce,
            'gas':      200000,
            'gasPrice': int(gas_price * 1.2),
        })

        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        return receipt.status == 1, tx_hash.hex()
    except Exception as e:
        return False, str(e)[:80]

def main():
    log('=== Auto Redemption Starting ===')

    positions = get_positions()
    log(f'Found {len(positions)} positions')

    if not positions:
        log('No positions to check')
        return

    # Connect to Polygon
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if _poa_mw:
        try:
            if _POA_V6:
                w3.middleware_onion.inject(_poa_mw(), layer=0)
            else:
                w3.middleware_onion.inject(_poa_mw, layer=0)
        except Exception:
            pass  # POA middleware optional on Polygon
    if not w3.is_connected():
        log('ERROR: Cannot connect to Polygon RPC')
        return
    log(f'Connected to Polygon (block {w3.eth.block_number})')

    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    redeemed = []
    skipped  = []

    for pos in positions:
        q           = pos.get('title', pos.get('market', ''))[:60]
        condition_id = pos.get('conditionId', '')
        outcome     = pos.get('outcome', '').lower()
        size        = float(pos.get('size', 0) or 0)
        cur_val     = float(pos.get('currentValue', 0) or 0)

        if not condition_id or size < 0.1:
            continue

        log(f'Checking: {q} ({outcome}, {size:.1f} shares, ${cur_val:.2f})')

        is_resolved, winner_idx = check_market_resolved(condition_id)
        if not is_resolved:
            log(f'  Not resolved yet — skip')
            skipped.append(q)
            continue

        # Check if we hold the winning outcome
        our_outcome_idx = 0 if outcome in ('yes', 'clippers', 'michigan', 'duke') else 1
        if our_outcome_idx != winner_idx:
            log(f'  We hold LOSING side (winner={winner_idx}, ours={our_outcome_idx}) — skip')
            skipped.append(q)
            continue

        log(f'  ✓ Resolved + we WIN — redeeming {size:.1f} shares (~${size:.2f} USDC)')
        ok, tx = redeem_position(w3, ctf, condition_id, our_outcome_idx, pos)

        if ok:
            log(f'  ✓ Redeemed! tx={tx[:16]}...')
            redeemed.append({'q': q, 'size': size, 'tx': tx})
        else:
            log(f'  ✗ Redemption failed: {tx}')

        time.sleep(2)

    log(f'=== Done. Redeemed: {len(redeemed)} | Skipped: {len(skipped)} ===')

    if redeemed:
        total = sum(r['size'] for r in redeemed)
        msg = f'<b>💰 Auto-redemption complete</b>\n'
        msg += f'Redeemed {len(redeemed)} position(s) → ~${total:.2f} USDC recovered\n\n'
        for r in redeemed:
            msg += f'• {r["q"]}: ${r["size"]:.2f}\n'
        tg(msg)
        log(f'Total redeemed: ~${total:.2f} USDC')

if __name__ == '__main__':
    main()
