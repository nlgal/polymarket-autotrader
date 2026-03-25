"""
Place 5 trades using the correct py_clob_client API (OrderArgs + create_order + post_order).
"""
import os, sys, math, time, json, requests

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN','')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, PartialCreateOrderOptions,
    BalanceAllowanceParams, AssetType
)
from py_clob_client.order_builder.constants import BUY

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

def get_client():
    client = ClobClient('https://clob.polymarket.com', key=PRIVATE_KEY,
                        chain_id=137, signature_type=2, funder=FUNDER)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client

# Trade definitions: [YES_token_id, NO_token_id]
TRADES = [
    {
        'name': 'Iranian regime fall by April 30 — NO',
        'action': 'BUY_NO',
        'yes_token': '48764428286656921488851644351774667118009263342042758531252625616470924946480',
        'no_token':  '45752951190517118746418545365916139233368614665273368123939609626397431866529',
        'size_usdc': 150,
        'max_price': 0.94,  # don't pay more than this for NO
    },
    {
        'name': 'US x Iran ceasefire by April 15 — YES',
        'action': 'BUY_YES',
        'yes_token': '85191934649046129480174964255278880752271767733539167443243111973456166096127',
        'no_token':  '8442709013751543525223072638303914942960068246422295030411662679470140144155',
        'size_usdc': 100,
        'max_price': 0.42,  # don't pay more than 42¢ for YES
    },
    {
        'name': 'Bitcoin dip to $65,000 in March — NO',
        'action': 'BUY_NO',
        'yes_token': '112493481455469093769281852159558847572704253342416714876781522096078968514094',
        'no_token':  '64087619211543545431479218048939484178441767712621033463416084593776314629222',
        'size_usdc': 100,
        'max_price': 0.87,
    },
    {
        'name': 'Crude Oil LOW $80 by March 31 — NO',
        'action': 'BUY_NO',
        'yes_token': '114929598274366971131336205799393924832857816779736104698632388348104809344836',
        'no_token':  '36145084663453267666801423060957500580695518567612602414112493781080641634240',
        'size_usdc': 75,
        'max_price': 0.84,
    },
    {
        'name': 'US forces enter Iran by March 31 — NO',
        'action': 'BUY_NO',
        'yes_token': '42750054381142639205639663180818682570869285140532640407891991570656047928885',
        'no_token':  '81697486240392901899167649997008736380137911909662773455994395620863894931973',
        'size_usdc': 75,
        'max_price': 0.93,
    },
]

print('Connecting to CLOB...')
client = get_client()

# Check USDC cash
try:
    bal_info = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    usdc_cash = float(bal_info.get('balance', 0)) / 1e6
    print(f'USDC available: ${usdc_cash:.2f}')
except Exception as e:
    print(f'Balance check error: {e}')
    usdc_cash = 999

results = []

for trade in TRADES:
    action = trade['action']
    token_id = trade['no_token'] if action == 'BUY_NO' else trade['yes_token']
    size_usdc = trade['size_usdc']
    name = trade['name']
    max_price = trade['max_price']

    print(f'\n--- {name} ---')

    try:
        # Get tick size and neg_risk flag
        tick = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

        # Get order book for best ask
        ob = client.get_order_book(token_id)
        asks = ob.asks if hasattr(ob, 'asks') and ob.asks else []
        bids = ob.bids if hasattr(ob, 'bids') and ob.bids else []

        if asks:
            raw_price = float(asks[-1].price)
        else:
            print(f'  No asks available — skipping')
            results.append({'name': name, 'status': 'SKIPPED', 'reason': 'no asks'})
            continue

        # Round to tick
        price = round(round(raw_price / tick_f) * tick_f, tick_dec)
        price = max(0.01, min(0.99, price))

        print(f'  Best ask: {raw_price:.4f} → rounded: {price:.4f} (tick={tick})')
        print(f'  neg_risk: {neg_risk}')

        # Price guard
        if price > max_price:
            print(f'  SKIP: price {price:.3f} > max {max_price:.3f}')
            results.append({'name': name, 'status': 'SKIPPED', 'reason': f'price {price:.3f} > max {max_price:.3f}'})
            continue

        # Calculate shares
        num_shares = round(size_usdc / price, 2)
        print(f'  Buying {num_shares} shares @ {price} = ${num_shares * price:.2f}')

        # Place GTC limit order at best ask
        args = OrderArgs(token_id=token_id, price=price, size=num_shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        print(f'  Receipt: {receipt}')

        if receipt.get('success') or receipt.get('orderID'):
            print(f'  ✅ SUCCESS — orderID: {receipt.get("orderID","N/A")[:24]}...')
            # Approve conditional token for future sells
            try:
                client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=2
                    )
                )
                print(f'  Conditional token approved for selling')
            except Exception as ae:
                print(f'  Approval warning: {ae}')
            results.append({
                'name': name, 'status': 'OK',
                'shares': num_shares, 'price': price, 'usdc': size_usdc,
                'order_id': receipt.get('orderID', '')
            })
        else:
            err = receipt.get('errorMsg', str(receipt))
            print(f'  ❌ Rejected: {err}')
            results.append({'name': name, 'status': 'REJECTED', 'error': err})

        time.sleep(1.5)

    except Exception as e:
        import traceback
        print(f'  ERROR: {e}')
        traceback.print_exc()
        results.append({'name': name, 'status': 'ERROR', 'error': str(e)[:120]})

# Summary
print('\n' + '='*60)
print('TRADE SUMMARY')
ok = [r for r in results if r['status'] == 'OK']
total_usdc = sum(r.get('usdc', 0) for r in ok)

tg_lines = ['<b>📊 5-Trade Batch — Results</b>\n']
for r in results:
    s = r['status']
    if s == 'OK':
        tg_lines.append(f"✅ {r['name']}\n   {r['shares']} shares @ {r['price']:.3f} (${r['usdc']})")
        print(f"✅ {r['name']} — {r['shares']} @ {r['price']:.3f}")
    elif s == 'SKIPPED':
        tg_lines.append(f"⏭️ {r['name']}\n   {r.get('reason','')}")
        print(f"⏭️  SKIP: {r['name']} — {r.get('reason','')}")
    else:
        tg_lines.append(f"❌ {r['name']}\n   {r.get('error','')[:80]}")
        print(f"❌ FAIL: {r['name']} — {r.get('error','')[:80]}")

tg_lines.append(f'\n<b>Deployed: ${total_usdc} USDC</b>')
tg('\n\n'.join(tg_lines))
print(f'\nTotal deployed: ${total_usdc}')
