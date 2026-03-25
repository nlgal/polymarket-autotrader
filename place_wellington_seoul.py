"""
5 high-conviction trades:
1. Iranian regime fall Apr 30 - BUY NO $150
2. Ceasefire by Apr 15    - BUY YES $100
3. BTC dip $65k March     - BUY NO  $100
4. Crude Oil LOW $80 Mar31- BUY NO  $75
5. US forces Mar31        - BUY NO  $75
"""
import os, sys, math, time, json, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, PartialCreateOrderOptions,
    BalanceAllowanceParams, AssetType
)
from py_clob_client.order_builder.constants import BUY

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN','')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID','')

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

client = ClobClient('https://clob.polymarket.com', key=PRIVATE_KEY,
                    chain_id=137, signature_type=2, funder=FUNDER)
client.set_api_creds(client.create_or_derive_api_creds())

try:
    bal = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    usdc = float(bal.get('balance', 0)) / 1e6
    print(f"USDC available: ${usdc:.2f}")
except Exception as e:
    print(f"Balance error: {e}")
    usdc = 999

# Check existing positions
held = set()
try:
    r = requests.get(f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50', timeout=10)
    for p in r.json():
        if float(p.get('currentValue', 0)) > 5:
            held.add(p.get('title','').lower()[:50])
except: pass

TRADES = [
    {'name': 'Iranian regime fall Apr 30 — NO',
     'action': 'BUY_NO',
     'yes_tok': '48764428286656921488851644351774667118009263342042758531252625616470924946480',
     'no_tok':  '45752951190517118746418545365916139233368614665273368123939609626397431866529',
     'size': 150, 'max_p': 0.95,
     'skip_key': 'will the iranian regime fall by april 30'},
    {'name': 'Ceasefire by April 15 — YES',
     'action': 'BUY_YES',
     'yes_tok': '85191934649046129480174964255278880752271767733539167443243111973456166096127',
     'no_tok':  '8442709013751543525223072638303914942960068246422295030411662679470140144155',
     'size': 100, 'max_p': 0.45,
     'skip_key': 'us x iran ceasefire by april 15'},
    {'name': 'BTC dip $65k March — NO',
     'action': 'BUY_NO',
     'yes_tok': '112493481455469093769281852159558847572704253342416714876781522096078968514094',
     'no_tok':  '64087619211543545431479218048939484178441767712621033463416084593776314629222',
     'size': 100, 'max_p': 0.88,
     'skip_key': 'will bitcoin dip to $65,000 in march'},
    {'name': 'Crude Oil LOW $80 Mar 31 — NO',
     'action': 'BUY_NO',
     'yes_tok': '114929598274366971131336205799393924832857816779736104698632388348104809344836',
     'no_tok':  '36145084663453267666801423060957500580695518567612602414112493781080641634240',
     'size': 75, 'max_p': 0.87,
     'skip_key': 'will crude oil (cl) hit (low) $80 by end of march'},
    {'name': 'US forces enter Iran Mar 31 — NO',
     'action': 'BUY_NO',
     'yes_tok': '42750054381142639205639663180818682570869285140532640407891991570656047928885',
     'no_tok':  '81697486240392901899167649997008736380137911909662773455994395620863894931973',
     'size': 75, 'max_p': 0.92,
     'skip_key': 'us forces enter iran by march 31'},
]

results = []
for t in TRADES:
    name   = t['name']
    action = t['action']
    tok    = t['no_tok'] if action == 'BUY_NO' else t['yes_tok']
    size   = t['size']
    max_p  = t['max_p']
    skip_k = t.get('skip_key','')

    if any(skip_k in h for h in held):
        print(f"SKIP (already holding): {name}")
        results.append({'name': name, 'status': 'SKIP', 'reason': 'already holding'})
        continue

    if usdc < size * 0.4:
        print(f"SKIP (low cash ${usdc:.0f}): {name}")
        results.append({'name': name, 'status': 'SKIP', 'reason': f'low cash'})
        continue

    print(f"\n--- {name} ---")
    try:
        tick     = client.get_tick_size(tok)
        neg_risk = client.get_neg_risk(tok)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

        ob   = client.get_order_book(tok)
        asks = ob.asks if hasattr(ob, 'asks') and ob.asks else []
        if not asks:
            print(f"  No asks — skip")
            results.append({'name': name, 'status': 'SKIP', 'reason': 'no asks'})
            continue

        raw_p = float(asks[-1].price)
        price = round(round(raw_p / tick_f) * tick_f, tick_dec)
        price = max(0.01, min(0.99, price))
        print(f"  Ask: {raw_p:.4f} → {price:.4f} (max={max_p})")

        if price > max_p:
            print(f"  SKIP: price {price:.3f} > max {max_p:.3f}")
            results.append({'name': name, 'status': 'SKIP', 'reason': f'price {price:.3f}'})
            continue

        shares = round(size / price, 2)
        print(f"  Buying {shares} shares @ {price} = ${shares*price:.2f}")

        args    = OrderArgs(token_id=tok, price=price, size=shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            oid = receipt.get('orderID', 'N/A')
            print(f"  ✅ {oid[:22]}...")
            usdc -= size
            try:
                client.update_balance_allowance(params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=tok, signature_type=2))
            except: pass
            results.append({'name': name, 'status': 'OK', 'shares': shares, 'price': price, 'usdc': size})
        else:
            err = receipt.get('errorMsg', str(receipt))
            print(f"  ❌ Rejected: {err}")
            results.append({'name': name, 'status': 'FAIL', 'error': err[:80]})

        time.sleep(1.5)
    except Exception as e:
        import traceback; traceback.print_exc()
        results.append({'name': name, 'status': 'ERROR', 'error': str(e)[:100]})

print('\n' + '='*50)
ok    = [r for r in results if r['status'] == 'OK']
total = sum(r.get('usdc',0) for r in ok)
print(f"Placed: {len(ok)}/{len(TRADES)} | Deployed: ${total}")

lines = ['<b>📊 5-Trade Batch</b>\n']
for r in results:
    if r['status'] == 'OK':
        lines.append(f"✅ {r['name']}\n   {r['shares']} @ {r['price']:.3f} (${r['usdc']})")
    elif r['status'] == 'SKIP':
        lines.append(f"⏭️ {r['name']}: {r.get('reason','')}")
    else:
        lines.append(f"❌ {r['name']}: {r.get('error','')[:60]}")
lines.append(f'\n<b>Total deployed: ${total}</b>')
tg('\n\n'.join(lines))
