
content = """\"\"\"
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
\"\"\"
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
RPC_URL = \"https://polygon-rpc.com\"

# CTF contract (Gnosis Conditional Token Framework) on Polygon
CTF_ADDRESS  = Web3.to_checksum_address(\"0x4D97DCd97eC945f40cF65F87097ACe5EA0476045\")
USDC_ADDRESS = Web3.to_checksum_address(\"0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174\")

# Minimal CTF ABI — just redeemPositions
CTF_ABI = [
    {
        \"inputs\": [
            {\"name\": \"collateralToken\", \"type\": \"address\"},
            {\"name\": \"parentCollectionId\", \"type\": \"bytes32\"},
            {\"name\": \"conditionId\", \"type\": \"bytes32\"},
            {\"name\": \"indexSets\", \"type\": \"uint256[]\"}
        ],
        \"name\": \"redeemPositions\",
        \"outputs\": [],
        \"stateMutability\": \"nonpayable\",
        \"type\": \"function\"
    },
    {
        \"inputs\": [
            {\"name\": \"account\", \"type\": \"address\"},
            {\"name\": \"id\", \"type\": \"uint256\"}
        ],
        \"name\": \"balanceOf\",
        \"outputs\": [{\"name\": \"\", \"type\": \"uint256\"}],
        \"stateMutability\": \"view\",
        \"type\": \"function\"
    },
    {
        \"inputs\": [
            {\"name\": \"conditionId\", \"type\": \"bytes32\"}
        ],
        \"name\": \"payoutNumerators\",
        \"outputs\": [{\"name\": \"\", \"type\": \"uint256[]\"}],
        \"stateMutability\": \"view\",
        \"type\": \"function\"
    },
    {
        \"inputs\": [
            {\"name\": \"conditionId\", \"type\": \"bytes32\"}
        ],
        \"name\": \"payoutDenominator\",
        \"outputs\": [{\"name\": \"\", \"type\": \"uint256\"}],
        \"stateMutability\": \"view\",
        \"type\": \"function\"
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
    \"\"\"
    Returns (is_resolved, winning_outcome_index) or (False, None).
    winning_outcome_index: 0=YES won, 1=NO won
    \"\"\"
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
                prices = [float(x.strip().strip('\"')) for x in prices.strip('[]').split(',')]
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
    \"\"\"
    Call redeemPositions() on the CTF contract.
    outcome_index: 0=YES, 1=NO
    indexSet: bitmask — 1 for YES (bit 0), 2 for NO (bit 1)
    \"\"\"
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
        msg = f'<b>💰 Auto-redemption complete</b>\\n'
        msg += f'Redeemed {len(redeemed)} position(s) → ~${total:.2f} USDC recovered\\n\\n'
        for r in redeemed:
            msg += f'• {r[\"q\"]}: ${r[\"size\"]:.2f}\\n'
        tg(msg)
        log(f'Total redeemed: ~${total:.2f} USDC')

if __name__ == '__main__':
    main()
"""
# Actually use base64 to avoid escaping issues
import base64
data = base64.b64decode("IiIiCmF1dG9fcmVkZWVtLnB5Cj09PT09PT09PT09PT09CkF1dG9tYXRpY2FsbHkgcmVkZWVtcyByZXNvbHZlZCB3aW5uaW5nIFBvbHltYXJrZXQgcG9zaXRpb25zLgoKSG93IFBvbHltYXJrZXQgcmVkZW1wdGlvbiB3b3JrczoKLSBXaGVuIGEgbWFya2V0IHJlc29sdmVzLCB3aW5uaW5nIGNvbmRpdGlvbmFsIHRva2VucyAoWUVTIG9yIE5PKSBhcmUgd29ydGggJDEgZWFjaAotIFlvdSBtdXN0IGNhbGwgcmVkZWVtUG9zaXRpb25zKCkgb24gdGhlIENURiBjb250cmFjdCB0byBjb252ZXJ0IHRoZW0gdG8gVVNEQwotIFBvbHltYXJrZXQncyBVSSBkb2VzIHRoaXMgYXV0b21hdGljYWxseSBidXQgdGhlIGJvdCB3YWxsZXQgbmVlZHMgZXhwbGljaXQgcmVkZW1wdGlvbgoKVGhpcyBzY3JpcHQ6CjEuIEZldGNoZXMgYWxsIHBvc2l0aW9ucyBmb3IgdGhlIGZ1bmRlciB3YWxsZXQKMi4gQ2hlY2tzIGVhY2ggbWFya2V0J3MgcmVzb2x1dGlvbiBzdGF0dXMgdmlhIGdhbW1hIEFQSQozLiBGb3IgcmVzb2x2ZWQgbWFya2V0cyB3aGVyZSB3ZSBob2xkIHRoZSBXSU5OSU5HIHRva2VuOgogICAtIENhbGxzIHJlZGVlbVBvc2l0aW9ucygpIG9uIHRoZSBDVEYgY29udHJhY3QgdmlhIHdlYjMucHkKNC4gU2VuZHMgVGVsZWdyYW0gYWxlcnQgd2l0aCB0b3RhbCBVU0RDIHJlY292ZXJlZAoKUnVuIGFmdGVyIGFueSBtYXJrZXQgZXhwaXJ5IChjcnVkZSBvaWwgTWFyY2ggMzEsIElyYW4gQXByaWwgMzAsIGV0Yy4pCkFsc28gcnVuIGJ5IGhlYWx0aCBtb25pdG9yIGF1dG9tYXRpY2FsbHkuCiIiIgppbXBvcnQgb3MsIHN5cywganNvbiwgdGltZSwgcmVxdWVzdHMsIGRhdGV0aW1lCnN5cy5wYXRoLmluc2VydCgwLCAnL29wdC9wb2x5bWFya2V0LWFnZW50JykKZnJvbSBkb3RlbnYgaW1wb3J0IGxvYWRfZG90ZW52CmxvYWRfZG90ZW52KCcvb3B0L3BvbHltYXJrZXQtYWdlbnQvLmVudicpCgpmcm9tIHdlYjMgaW1wb3J0IFdlYjMKX3BvYV9tdyA9IE5vbmUgICMgUE9BIG1pZGRsZXdhcmUgbm90IHJlcXVpcmVkIG9uIFBvbHlnb24gZm9yIHJlYWQvd3JpdGUgb3BzCgpQUklWQVRFX0tFWSA9IG9zLmVudmlyb25bJ1BPTFlNQVJLRVRfUFJJVkFURV9LRVknXQpGVU5ERVIgICAgICA9IG9zLmVudmlyb25bJ1BPTFlNQVJLRVRfRlVOREVSX0FERFJFU1MnXQpUR19UT0tFTiAgICA9IG9zLmVudmlyb24uZ2V0KCdURUxFR1JBTV9UT0tFTicsICcnKQpUR19DSEFUICAgICA9IG9zLmVudmlyb24uZ2V0KCdURUxFR1JBTV9DSEFUX0lEJywgJycpCgojIFBvbHlnb24gUlBDClJQQ19VUkwgPSAiaHR0cHM6Ly9wb2x5Z29uLXJwYy5jb20iCgojIENURiBjb250cmFjdCAoR25vc2lzIENvbmRpdGlvbmFsIFRva2VuIEZyYW1ld29yaykgb24gUG9seWdvbgpDVEZfQUREUkVTUyAgPSBXZWIzLnRvX2NoZWNrc3VtX2FkZHJlc3MoIjB4NEQ5N0RDZDk3ZUM5NDVmNDBjRjY1Rjg3MDk3QUNlNUVBMDQ3NjA0NSIpClVTRENfQUREUkVTUyA9IFdlYjMudG9fY2hlY2tzdW1fYWRkcmVzcygiMHgyNzkxQmNhMWYyZGU0NjYxRUQ4OEEzMEM5OUE3YTk0NDlBYTg0MTc0IikKCiMgTWluaW1hbCBDVEYgQUJJIOKAlCBqdXN0IHJlZGVlbVBvc2l0aW9ucwpDVEZfQUJJID0gWwogICAgewogICAgICAgICJpbnB1dHMiOiBbCiAgICAgICAgICAgIHsibmFtZSI6ICJjb2xsYXRlcmFsVG9rZW4iLCAidHlwZSI6ICJhZGRyZXNzIn0sCiAgICAgICAgICAgIHsibmFtZSI6ICJwYXJlbnRDb2xsZWN0aW9uSWQiLCAidHlwZSI6ICJieXRlczMyIn0sCiAgICAgICAgICAgIHsibmFtZSI6ICJjb25kaXRpb25JZCIsICJ0eXBlIjogImJ5dGVzMzIifSwKICAgICAgICAgICAgeyJuYW1lIjogImluZGV4U2V0cyIsICJ0eXBlIjogInVpbnQyNTZbXSJ9CiAgICAgICAgXSwKICAgICAgICAibmFtZSI6ICJyZWRlZW1Qb3NpdGlvbnMiLAogICAgICAgICJvdXRwdXRzIjogW10sCiAgICAgICAgInN0YXRlTXV0YWJpbGl0eSI6ICJub25wYXlhYmxlIiwKICAgICAgICAidHlwZSI6ICJmdW5jdGlvbiIKICAgIH0sCiAgICB7CiAgICAgICAgImlucHV0cyI6IFsKICAgICAgICAgICAgeyJuYW1lIjogImFjY291bnQiLCAidHlwZSI6ICJhZGRyZXNzIn0sCiAgICAgICAgICAgIHsibmFtZSI6ICJpZCIsICJ0eXBlIjogInVpbnQyNTYifQogICAgICAgIF0sCiAgICAgICAgIm5hbWUiOiAiYmFsYW5jZU9mIiwKICAgICAgICAib3V0cHV0cyI6IFt7Im5hbWUiOiAiIiwgInR5cGUiOiAidWludDI1NiJ9XSwKICAgICAgICAic3RhdGVNdXRhYmlsaXR5IjogInZpZXciLAogICAgICAgICJ0eXBlIjogImZ1bmN0aW9uIgogICAgfSwKICAgIHsKICAgICAgICAiaW5wdXRzIjogWwogICAgICAgICAgICB7Im5hbWUiOiAiY29uZGl0aW9uSWQiLCAidHlwZSI6ICJieXRlczMyIn0KICAgICAgICBdLAogICAgICAgICJuYW1lIjogInBheW91dE51bWVyYXRvcnMiLAogICAgICAgICJvdXRwdXRzIjogW3sibmFtZSI6ICIiLCAidHlwZSI6ICJ1aW50MjU2W10ifV0sCiAgICAgICAgInN0YXRlTXV0YWJpbGl0eSI6ICJ2aWV3IiwKICAgICAgICAidHlwZSI6ICJmdW5jdGlvbiIKICAgIH0sCiAgICB7CiAgICAgICAgImlucHV0cyI6IFsKICAgICAgICAgICAgeyJuYW1lIjogImNvbmRpdGlvbklkIiwgInR5cGUiOiAiYnl0ZXMzMiJ9CiAgICAgICAgXSwKICAgICAgICAibmFtZSI6ICJwYXlvdXREZW5vbWluYXRvciIsCiAgICAgICAgIm91dHB1dHMiOiBbeyJuYW1lIjogIiIsICJ0eXBlIjogInVpbnQyNTYifV0sCiAgICAgICAgInN0YXRlTXV0YWJpbGl0eSI6ICJ2aWV3IiwKICAgICAgICAidHlwZSI6ICJmdW5jdGlvbiIKICAgIH0KXQoKZGVmIHRnKG1zZyk6CiAgICBpZiBUR19UT0tFTiBhbmQgVEdfQ0hBVDoKICAgICAgICB0cnk6CiAgICAgICAgICAgIHJlcXVlc3RzLnBvc3QoZidodHRwczovL2FwaS50ZWxlZ3JhbS5vcmcvYm90e1RHX1RPS0VOfS9zZW5kTWVzc2FnZScsCiAgICAgICAgICAgICAgICBqc29uPXsnY2hhdF9pZCc6IFRHX0NIQVQsICd0ZXh0JzogbXNnWzo0MDAwXSwgJ3BhcnNlX21vZGUnOiAnSFRNTCd9LCB0aW1lb3V0PTEwKQogICAgICAgIGV4Y2VwdDogcGFzcwoKZGVmIGxvZyhtc2cpOgogICAgdHMgPSBkYXRldGltZS5kYXRldGltZS51dGNub3coKS5zdHJmdGltZSgnJUg6JU06JVMnKQogICAgcHJpbnQoZidbe3RzfV0ge21zZ30nKQoKZGVmIGdldF9wb3NpdGlvbnMoKToKICAgIHIgPSByZXF1ZXN0cy5nZXQoCiAgICAgICAgZidodHRwczovL2RhdGEtYXBpLnBvbHltYXJrZXQuY29tL3Bvc2l0aW9ucz91c2VyPXtGVU5ERVJ9JmxpbWl0PTEwMCcsCiAgICAgICAgdGltZW91dD0xNQogICAgKQogICAgcmV0dXJuIHIuanNvbigpIGlmIHIuc3RhdHVzX2NvZGUgPT0gMjAwIGVsc2UgW10KCmRlZiBjaGVja19tYXJrZXRfcmVzb2x2ZWQoY29uZGl0aW9uX2lkKToKICAgICIiIgogICAgUmV0dXJucyAoaXNfcmVzb2x2ZWQsIHdpbm5pbmdfb3V0Y29tZV9pbmRleCkgb3IgKEZhbHNlLCBOb25lKS4KICAgIHdpbm5pbmdfb3V0Y29tZV9pbmRleDogMD1ZRVMgd29uLCAxPU5PIHdvbgogICAgIiIiCiAgICB0cnk6CiAgICAgICAgciA9IHJlcXVlc3RzLmdldCgKICAgICAgICAgICAgZidodHRwczovL2dhbW1hLWFwaS5wb2x5bWFya2V0LmNvbS9tYXJrZXRzP2NvbmRpdGlvbklkPXtjb25kaXRpb25faWR9JywKICAgICAgICAgICAgdGltZW91dD0xMAogICAgICAgICkKICAgICAgICBpZiByLnN0YXR1c19jb2RlICE9IDIwMDoKICAgICAgICAgICAgcmV0dXJuIEZhbHNlLCBOb25lCiAgICAgICAgbWFya2V0cyA9IHIuanNvbigpCiAgICAgICAgaWYgbm90IG1hcmtldHM6CiAgICAgICAgICAgIHJldHVybiBGYWxzZSwgTm9uZQogICAgICAgIG0gPSBtYXJrZXRzWzBdCgogICAgICAgICMgQ2hlY2sgaWYgcmVzb2x2ZWQgdmlhIG91dGNvbWVQcmljZXMg4oCUIHJlc29sdmVkIG1hcmtldCBoYXMgMC8xIG9yIDEvMAogICAgICAgIHByaWNlcyA9IG0uZ2V0KCdvdXRjb21lUHJpY2VzJywgJycpCiAgICAgICAgaWYgaXNpbnN0YW5jZShwcmljZXMsIHN0cik6CiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHByaWNlcyA9IFtmbG9hdCh4LnN0cmlwKCkuc3RyaXAoJyInKSkgZm9yIHggaW4gcHJpY2VzLnN0cmlwKCdbXScpLnNwbGl0KCcsJyldCiAgICAgICAgICAgIGV4Y2VwdDoKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZSwgTm9uZQoKICAgICAgICBpZiBub3QgbS5nZXQoJ2Nsb3NlZCcsIEZhbHNlKSBhbmQgbm90IG0uZ2V0KCdyZXNvbHZlZCcsIEZhbHNlKToKICAgICAgICAgICAgcmV0dXJuIEZhbHNlLCBOb25lCgogICAgICAgICMgRmluZCB3aW5uaW5nIG91dGNvbWUgKHByaWNlID0gMS4wKQogICAgICAgIGZvciBpLCBwIGluIGVudW1lcmF0ZShwcmljZXMpOgogICAgICAgICAgICBpZiBmbG9hdChwKSA+PSAwLjk5OgogICAgICAgICAgICAgICAgcmV0dXJuIFRydWUsIGkKCiAgICAgICAgcmV0dXJuIEZhbHNlLCBOb25lCiAgICBleGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgbG9nKGYnICBjaGVja19yZXNvbHZlZCBlcnJvcjoge2V9JykKICAgICAgICByZXR1cm4gRmFsc2UsIE5vbmUKCmRlZiByZWRlZW1fcG9zaXRpb24odzMsIGN0ZiwgY29uZGl0aW9uX2lkX2hleCwgb3V0Y29tZV9pbmRleCwgcG9zaXRpb24pOgogICAgIiIiCiAgICBDYWxsIHJlZGVlbVBvc2l0aW9ucygpIG9uIHRoZSBDVEYgY29udHJhY3QuCiAgICBvdXRjb21lX2luZGV4OiAwPVlFUywgMT1OTwogICAgaW5kZXhTZXQ6IGJpdG1hc2sg4oCUIDEgZm9yIFlFUyAoYml0IDApLCAyIGZvciBOTyAoYml0IDEpCiAgICAiIiIKICAgIHRyeToKICAgICAgICBjb25kaXRpb25faWRfYnl0ZXMgPSBieXRlcy5mcm9taGV4KGNvbmRpdGlvbl9pZF9oZXgucmVwbGFjZSgnMHgnLCAnJykpCiAgICAgICAgcGFyZW50X2NvbGxlY3Rpb24gPSBiJ1x4MDAnICogMzIKICAgICAgICBpbmRleF9zZXRzID0gWzEgPDwgb3V0Y29tZV9pbmRleF0gICMgMSBmb3IgWUVTLCAyIGZvciBOTwoKICAgICAgICBhY2NvdW50ID0gV2ViMy50b19jaGVja3N1bV9hZGRyZXNzKEZVTkRFUikKICAgICAgICBub25jZSA9IHczLmV0aC5nZXRfdHJhbnNhY3Rpb25fY291bnQoYWNjb3VudCkKICAgICAgICBnYXNfcHJpY2UgPSB3My5ldGguZ2FzX3ByaWNlCgogICAgICAgIHR4ID0gY3RmLmZ1bmN0aW9ucy5yZWRlZW1Qb3NpdGlvbnMoCiAgICAgICAgICAgIFVTRENfQUREUkVTUywKICAgICAgICAgICAgcGFyZW50X2NvbGxlY3Rpb24sCiAgICAgICAgICAgIGNvbmRpdGlvbl9pZF9ieXRlcywKICAgICAgICAgICAgaW5kZXhfc2V0cwogICAgICAgICkuYnVpbGRfdHJhbnNhY3Rpb24oewogICAgICAgICAgICAnZnJvbSc6ICAgICBhY2NvdW50LAogICAgICAgICAgICAnbm9uY2UnOiAgICBub25jZSwKICAgICAgICAgICAgJ2dhcyc6ICAgICAgMjAwMDAwLAogICAgICAgICAgICAnZ2FzUHJpY2UnOiBpbnQoZ2FzX3ByaWNlICogMS4yKSwKICAgICAgICB9KQoKICAgICAgICBzaWduZWQgPSB3My5ldGguYWNjb3VudC5zaWduX3RyYW5zYWN0aW9uKHR4LCBQUklWQVRFX0tFWSkKICAgICAgICB0eF9oYXNoID0gdzMuZXRoLnNlbmRfcmF3X3RyYW5zYWN0aW9uKHNpZ25lZC5yYXdUcmFuc2FjdGlvbikKICAgICAgICByZWNlaXB0ID0gdzMuZXRoLndhaXRfZm9yX3RyYW5zYWN0aW9uX3JlY2VpcHQodHhfaGFzaCwgdGltZW91dD02MCkKCiAgICAgICAgcmV0dXJuIHJlY2VpcHQuc3RhdHVzID09IDEsIHR4X2hhc2guaGV4KCkKICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZToKICAgICAgICByZXR1cm4gRmFsc2UsIHN0cihlKVs6ODBdCgpkZWYgbWFpbigpOgogICAgbG9nKCc9PT0gQXV0byBSZWRlbXB0aW9uIFN0YXJ0aW5nID09PScpCgogICAgcG9zaXRpb25zID0gZ2V0X3Bvc2l0aW9ucygpCiAgICBsb2coZidGb3VuZCB7bGVuKHBvc2l0aW9ucyl9IHBvc2l0aW9ucycpCgogICAgaWYgbm90IHBvc2l0aW9uczoKICAgICAgICBsb2coJ05vIHBvc2l0aW9ucyB0byBjaGVjaycpCiAgICAgICAgcmV0dXJuCgogICAgIyBDb25uZWN0IHRvIFBvbHlnb24KICAgIHczID0gV2ViMyhXZWIzLkhUVFBQcm92aWRlcihSUENfVVJMKSkKICAgIGlmIF9wb2FfbXc6CiAgICAgICAgdHJ5OgogICAgICAgICAgICBpZiBfUE9BX1Y2OgogICAgICAgICAgICAgICAgdzMubWlkZGxld2FyZV9vbmlvbi5pbmplY3QoX3BvYV9tdygpLCBsYXllcj0wKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgdzMubWlkZGxld2FyZV9vbmlvbi5pbmplY3QoX3BvYV9tdywgbGF5ZXI9MCkKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzICAjIFBPQSBtaWRkbGV3YXJlIG9wdGlvbmFsIG9uIFBvbHlnb24KICAgIGlmIG5vdCB3My5pc19jb25uZWN0ZWQoKToKICAgICAgICBsb2coJ0VSUk9SOiBDYW5ub3QgY29ubmVjdCB0byBQb2x5Z29uIFJQQycpCiAgICAgICAgcmV0dXJuCiAgICBsb2coZidDb25uZWN0ZWQgdG8gUG9seWdvbiAoYmxvY2sge3czLmV0aC5ibG9ja19udW1iZXJ9KScpCgogICAgY3RmID0gdzMuZXRoLmNvbnRyYWN0KGFkZHJlc3M9Q1RGX0FERFJFU1MsIGFiaT1DVEZfQUJJKQoKICAgIHJlZGVlbWVkID0gW10KICAgIHNraXBwZWQgID0gW10KCiAgICBmb3IgcG9zIGluIHBvc2l0aW9uczoKICAgICAgICBxICAgICAgICAgICA9IHBvcy5nZXQoJ3RpdGxlJywgcG9zLmdldCgnbWFya2V0JywgJycpKVs6NjBdCiAgICAgICAgY29uZGl0aW9uX2lkID0gcG9zLmdldCgnY29uZGl0aW9uSWQnLCAnJykKICAgICAgICBvdXRjb21lICAgICA9IHBvcy5nZXQoJ291dGNvbWUnLCAnJykubG93ZXIoKQogICAgICAgIHNpemUgICAgICAgID0gZmxvYXQocG9zLmdldCgnc2l6ZScsIDApIG9yIDApCiAgICAgICAgY3VyX3ZhbCAgICAgPSBmbG9hdChwb3MuZ2V0KCdjdXJyZW50VmFsdWUnLCAwKSBvciAwKQoKICAgICAgICBpZiBub3QgY29uZGl0aW9uX2lkIG9yIHNpemUgPCAwLjE6CiAgICAgICAgICAgIGNvbnRpbnVlCgogICAgICAgIGxvZyhmJ0NoZWNraW5nOiB7cX0gKHtvdXRjb21lfSwge3NpemU6LjFmfSBzaGFyZXMsICR7Y3VyX3ZhbDouMmZ9KScpCgogICAgICAgIGlzX3Jlc29sdmVkLCB3aW5uZXJfaWR4ID0gY2hlY2tfbWFya2V0X3Jlc29sdmVkKGNvbmRpdGlvbl9pZCkKICAgICAgICBpZiBub3QgaXNfcmVzb2x2ZWQ6CiAgICAgICAgICAgIGxvZyhmJyAgTm90IHJlc29sdmVkIHlldCDigJQgc2tpcCcpCiAgICAgICAgICAgIHNraXBwZWQuYXBwZW5kKHEpCiAgICAgICAgICAgIGNvbnRpbnVlCgogICAgICAgICMgQ2hlY2sgaWYgd2UgaG9sZCB0aGUgd2lubmluZyBvdXRjb21lCiAgICAgICAgb3VyX291dGNvbWVfaWR4ID0gMCBpZiBvdXRjb21lIGluICgneWVzJywgJ2NsaXBwZXJzJywgJ21pY2hpZ2FuJywgJ2R1a2UnKSBlbHNlIDEKICAgICAgICBpZiBvdXJfb3V0Y29tZV9pZHggIT0gd2lubmVyX2lkeDoKICAgICAgICAgICAgbG9nKGYnICBXZSBob2xkIExPU0lORyBzaWRlICh3aW5uZXI9e3dpbm5lcl9pZHh9LCBvdXJzPXtvdXJfb3V0Y29tZV9pZHh9KSDigJQgc2tpcCcpCiAgICAgICAgICAgIHNraXBwZWQuYXBwZW5kKHEpCiAgICAgICAgICAgIGNvbnRpbnVlCgogICAgICAgIGxvZyhmJyAg4pyTIFJlc29sdmVkICsgd2UgV0lOIOKAlCByZWRlZW1pbmcge3NpemU6LjFmfSBzaGFyZXMgKH4ke3NpemU6LjJmfSBVU0RDKScpCiAgICAgICAgb2ssIHR4ID0gcmVkZWVtX3Bvc2l0aW9uKHczLCBjdGYsIGNvbmRpdGlvbl9pZCwgb3VyX291dGNvbWVfaWR4LCBwb3MpCgogICAgICAgIGlmIG9rOgogICAgICAgICAgICBsb2coZicgIOKckyBSZWRlZW1lZCEgdHg9e3R4WzoxNl19Li4uJykKICAgICAgICAgICAgcmVkZWVtZWQuYXBwZW5kKHsncSc6IHEsICdzaXplJzogc2l6ZSwgJ3R4JzogdHh9KQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGxvZyhmJyAg4pyXIFJlZGVtcHRpb24gZmFpbGVkOiB7dHh9JykKCiAgICAgICAgdGltZS5zbGVlcCgyKQoKICAgIGxvZyhmJz09PSBEb25lLiBSZWRlZW1lZDoge2xlbihyZWRlZW1lZCl9IHwgU2tpcHBlZDoge2xlbihza2lwcGVkKX0gPT09JykKCiAgICBpZiByZWRlZW1lZDoKICAgICAgICB0b3RhbCA9IHN1bShyWydzaXplJ10gZm9yIHIgaW4gcmVkZWVtZWQpCiAgICAgICAgbXNnID0gZic8Yj7wn5KwIEF1dG8tcmVkZW1wdGlvbiBjb21wbGV0ZTwvYj5cbicKICAgICAgICBtc2cgKz0gZidSZWRlZW1lZCB7bGVuKHJlZGVlbWVkKX0gcG9zaXRpb24ocykg4oaSIH4ke3RvdGFsOi4yZn0gVVNEQyByZWNvdmVyZWRcblxuJwogICAgICAgIGZvciByIGluIHJlZGVlbWVkOgogICAgICAgICAgICBtc2cgKz0gZifigKIge3JbInEiXX06ICR7clsic2l6ZSJdOi4yZn1cbicKICAgICAgICB0Zyhtc2cpCiAgICAgICAgbG9nKGYnVG90YWwgcmVkZWVtZWQ6IH4ke3RvdGFsOi4yZn0gVVNEQycpCgppZiBfX25hbWVfXyA9PSAnX19tYWluX18nOgogICAgbWFpbigpCg==")
with open("/opt/polymarket-agent/auto_redeem.py", "wb") as f:
    f.write(data)
# Verify
with open("/opt/polymarket-agent/auto_redeem.py") as f:
    c = f.read()
print(f"Written {len(c)} chars")
print(f"Line 27: {c.split(chr(10))[26]}")
ok = 'geth_poa_middleware' not in c
print(f"Clean import: {ok}")
