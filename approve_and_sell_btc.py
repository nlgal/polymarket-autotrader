"""
approve_and_sell_btc.py — Approve CTF Exchange for BTC $80k NO token then sell
Uses web3 to call setApprovalForAll on the CTF Exchange contract for ERC-1155 tokens.
"""

import os, sys, requests
from dotenv import load_dotenv

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_env)

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIG_TYPE    = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not PRIVATE_KEY: print("ERROR"); sys.exit(1)

# BTC $80k NO token
TOKEN_ID    = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY_PRICE = 0.5656
SHARES      = 337.24

# Polymarket contracts on Polygon
CTF_EXCHANGE     = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
# The Funder is a Gnosis Safe — conditional token IS the ERC-1155 position token
# The CTF Exchange needs setApprovalForAll on the ConditionalTokens contract

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import SELL as SELL_SIDE

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception: pass

def get_client():
    creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                     api_secret=os.environ.get("CLOB_API_SECRET",""),
                     api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
    return ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137,
                      creds=creds, signature_type=SIG_TYPE, funder=FUNDER)

def approve_conditional_via_web3(token_id: str):
    """
    Use web3 to call setApprovalForAll on the ConditionalTokens contract.
    This approves the CTF Exchange to transfer position tokens on behalf of the Funder.
    """
    from web3 import Web3
    
    rpcs = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic",
        "https://polygon.drpc.org",
    ]
    w3 = None
    for rpc in rpcs:
        try:
            _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if _w3.is_connected():
                w3 = _w3
                print(f"  Connected: {rpc[-30:]}")
                break
        except Exception:
            continue
    
    if not w3:
        print("  All RPCs failed")
        return False

    # The private key is the SIGNER's key (EOA)
    pk_hex = PRIVATE_KEY
    if len(pk_hex) == 64:
        account = w3.eth.account.from_key(pk_hex)
    else:
        print("  Invalid private key")
        return False
    
    signer = account.address
    print(f"  Signer: {signer}")
    print(f"  Funder (Safe): {FUNDER}")
    
    # The Funder is the Gnosis Safe. We need to call setApprovalForAll ON the Safe
    # which requires calling it as an owner via the Safe's execTransaction.
    # This is complex. Instead, use the CLOB client's update_balance_allowance
    # which handles the Safe signature internally.
    
    # Actually: the py_clob_client handles the Safe interaction.
    # update_balance_allowance(CONDITIONAL, token_id) signs a message that 
    # authorizes the exchange. Let's see what it actually does.
    
    # Check current approval status
    CTF_CONDITIONAL = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Polymarket CTF contract
    
    ERC1155_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],
                    "name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],
                    "type":"function","stateMutability":"view"}]
    
    try:
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONDITIONAL), abi=ERC1155_ABI)
        approved = ctf.functions.isApprovedForAll(
            Web3.to_checksum_address(FUNDER),
            Web3.to_checksum_address(CTF_EXCHANGE)
        ).call()
        print(f"  CTF Exchange approved for Funder: {approved}")
        
        neg_approved = ctf.functions.isApprovedForAll(
            Web3.to_checksum_address(FUNDER),
            Web3.to_checksum_address(NEG_RISK_EXCHANGE)
        ).call()
        print(f"  Neg Risk Exchange approved for Funder: {neg_approved}")
        
        return approved or neg_approved
    except Exception as e:
        print(f"  Approval check error: {e}")
        return False

def main():
    client = get_client()
    if not os.environ.get("CLOB_API_KEY"):
        try:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
        except Exception as e:
            print(f"Creds: {e}")

    # Get current mid
    r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN_ID}", timeout=8)
    mid = float(r.json().get("mid", 0.895)) if r.status_code == 200 else 0.895
    pnl = (mid - ENTRY_PRICE) * SHARES
    pnl_pct = pnl / (ENTRY_PRICE * SHARES) * 100

    print(f"\n{'='*55}")
    print(f"  BTC $80k NO — SELL & LOCK GAIN")
    print(f"  Mid: {mid:.4f} | Entry: {ENTRY_PRICE:.4f} | Gain: {pnl_pct:+.1f}%")
    print(f"{'='*55}")

    # Step 1: Check on-chain approval status
    print("\nStep 1: Checking on-chain approval...")
    is_approved = approve_conditional_via_web3(TOKEN_ID)
    
    # Step 2: Try CLOB allowance update
    print("\nStep 2: Updating CLOB allowances...")
    for _at in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
        try:
            _kw = {"asset_type": _at, "signature_type": 2}
            if _at == AssetType.CONDITIONAL: _kw["token_id"] = TOKEN_ID
            result = client.update_balance_allowance(params=BalanceAllowanceParams(**_kw))
            print(f"  ✓ {_at}: {str(result)[:80]}")
        except Exception as ae:
            print(f"  {_at}: {ae}")

    # Step 3: Place sell order
    print("\nStep 3: Placing sell order...")
    try:
        tick     = client.get_tick_size(TOKEN_ID)
        neg_risk = client.get_neg_risk(TOKEN_ID)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        shares = round(SHARES, 2)

        print(f"  Neg risk: {neg_risk} | Tick: {tick}")
        print(f"  Sell price: {sell_price:.4f} | Shares: {shares}")

        args    = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=shares, side=SELL_SIDE)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            proceeds = sell_price * shares
            profit = proceeds - ENTRY_PRICE * shares
            print(f"\n  ✓✓✓ SOLD {shares} @ {sell_price:.4f} | ${proceeds:.2f} proceeds | ${profit:+.2f} profit")
            tg(f"💰 <b>BTC $80k NO SOLD</b>\n{shares} shares @ {sell_price:.4f}\nProceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
        else:
            print(f"\n  ✗ {receipt.get('errorMsg','')}")
            tg(f"⚠️ BTC sell failed: {receipt.get('errorMsg','')[:100]}")
    except Exception as e:
        print(f"\n  ✗ Error: {e}")
        import traceback; traceback.print_exc()

if __name__ == "__main__":
    main()
