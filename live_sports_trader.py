"""
live_sports_trader.py
=====================
Snowball + Limit Ladder Strategy for Polymarket Tournament Markets

Two-layer strategy:
  Layer 1 — Snowball entry
    Buy YES immediately after a top seed wins a game (pre-reprice window).
    Also buys when a top seed is leading big late in a game.

  Layer 2 — Limit ladder (avg-down + rebalance sell)
    After every market buy, place 3 resting GTC limit bids at:
      -15%, -25%, -35% below entry price
    These fill automatically during in-game momentum swings
    (e.g. opponent goes on a run, YES drops from 80¢ → 50¢).
    Once ANY ladder bid fills, recalculate weighted avg cost and
    place a single limit sell at avg_cost + TARGET_PROFIT_PCT.
    On bounce back up, the sell hits → full position exits at profit.

  Cleanup
    On each run, cancel any open ladder orders for eliminated teams
    (YES price < 5¢) so USDC isn't locked up in dead positions.

State file: /opt/polymarket-agent/sports_state.json
  positions[key] = {
    team, yes_token, entry_price, entry_shares, entry_cost,
    ladder_orders: [{order_id, price, shares, status}],
    sell_order_id, avg_cost, total_shares, total_cost,
    filled_ladder_count
  }

Run hourly during game hours (1pm–midnight EDT).
"""
import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID', '')

# ── Strategy config ────────────────────────────────────────────────────
# DK_MODE: when True, the scanner ONLY manages existing ladder positions.
# New entries are ONLY placed when a pick is written to DK_PICKS_FILE.
# Set DK_MODE=True permanently — manual picks via dk_picks.json only.
#
# To signal a new DK pick:
#   Write to /opt/polymarket-agent/dk_picks.json:
#   [{"team": "Phoenix Suns", "market_hint": "mavericks vs suns", "size": 200}]
#   The trader finds the Polymarket market, buys, clears the file.
DK_MODE       = True
DK_PICKS_FILE = '/opt/polymarket-agent/dk_picks.json'

MAX_PER_TEAM      = 75      # Max total $ deployed per team
MAX_SPORTS_TOTAL  = 200     # Max total sports exposure today
MAX_YES_PRICE     = 0.82    # Don't enter if already >82¢
MIN_YES_PRICE     = 0.03    # Treat as eliminated if <3¢
MIN_USDC          = 20      # Skip run if less than $20 cash

# Ladder: place bids this far below entry price
LADDER_DROPS      = [0.15, 0.25, 0.35]   # -15%, -25%, -35%
LADDER_SIZE_MULT  = [1.0,  1.5,  2.0]    # scale size at each rung (deeper = bigger)

# Exit: sell the whole position at avg_cost + this margin
TARGET_PROFIT_PCT = 0.15    # 15% above avg cost → limit sell

STATE_FILE = '/opt/polymarket-agent/sports_state.json'

# ── Helpers ────────────────────────────────────────────────────────────

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'},
                timeout=10
            )
        except:
            pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}')

def load_dk_picks():
    """Read pending DK picks from dk_picks.json. Returns list of picks, clears file after read."""
    if not os.path.exists(DK_PICKS_FILE):
        return []
    try:
        with open(DK_PICKS_FILE) as f:
            picks = json.load(f)
        os.remove(DK_PICKS_FILE)  # consume the file
        log(f'  [DK] Loaded {len(picks)} pick(s) from dk_picks.json')
        return picks if isinstance(picks, list) else [picks]
    except Exception as e:
        log(f'  [DK] Error reading dk_picks.json: {e}')
        return []

def find_market_for_pick(pick, markets):
    """Find the Polymarket market matching a DK pick by team name or hint."""
    team_hint  = pick.get('team', '').lower()
    mkt_hint   = pick.get('market_hint', '').lower()
    for team_name, mkt in markets.items():
        name_lower = team_name.lower()
        slug_lower = mkt.get('slug', '').lower()
        if team_hint and (team_hint in name_lower or name_lower in team_hint):
            return team_name, mkt
        if mkt_hint and (mkt_hint in slug_lower or any(
                word in slug_lower for word in mkt_hint.split() if len(word) > 3)):
            return team_name, mkt
    return None, None

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {'positions': {}, 'daily_spend': 0, 'last_date': ''}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def snap_to_tick(price, tick_f, tick_dec):
    return round(round(price / tick_f) * tick_f, tick_dec)

def get_tick_info(client, token_id):
    tick   = client.get_tick_size(token_id)
    neg    = client.get_neg_risk(token_id)
    tick_f = float(tick)
    tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
    return tick, neg, tick_f, tick_dec

# ── Order book helpers ─────────────────────────────────────────────────

def get_best_ask(yes_token):
    try:
        r = requests.get(
            f'https://clob.polymarket.com/book?token_id={yes_token}', timeout=8
        )
        asks = r.json().get('asks', [])
        return float(asks[0]['price']) if asks else None
    except:
        return None

def get_open_orders(client, yes_token):
    """Return list of open orders for this token."""
    try:
        orders = client.get_orders(market=yes_token)
        return orders if isinstance(orders, list) else []
    except:
        return []

def cancel_order(client, order_id):
    try:
        client.cancel(order_id=order_id)
        return True
    except:
        return False

# ── Core order functions ───────────────────────────────────────────────

def place_market_buy(client, market, size_usdc):
    """
    Buy YES at best ask (market order via GTC at ask price).
    Returns (ok, entry_price, shares_bought, order_id, msg).
    """
    yes_token = market['yes_token']
    best_ask = get_best_ask(yes_token)
    if best_ask is None:
        return False, 0, 0, '', 'No asks in book'
    if best_ask > MAX_YES_PRICE:
        return False, 0, 0, '', f'Ask {best_ask:.3f} > max {MAX_YES_PRICE}'

    try:
        tick, neg, tick_f, tick_dec = get_tick_info(client, yes_token)
        buy_price = snap_to_tick(best_ask, tick_f, tick_dec)
        shares    = round(size_usdc / buy_price, 2)
        if shares < 1:
            return False, 0, 0, '', f'Too few shares ({shares:.2f})'

        args    = OrderArgs(token_id=yes_token, price=buy_price, size=shares, side=BUY)
        opts    = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            oid = receipt.get('orderID', '')
            return True, buy_price, shares, oid, f'{shares:.1f} shares @ {buy_price:.3f}'
        return False, 0, 0, '', receipt.get('errorMsg', 'unknown')
    except Exception as e:
        return False, 0, 0, '', str(e)[:80]

def place_limit_bid(client, market, price, shares):
    """
    Place a resting GTC limit buy at `price` for `shares`.
    Returns (ok, order_id, msg).
    """
    yes_token = market['yes_token']
    try:
        tick, neg, tick_f, tick_dec = get_tick_info(client, yes_token)
        snapped = snap_to_tick(price, tick_f, tick_dec)
        snapped = max(snapped, tick_f)   # floor at 1 tick

        args    = OrderArgs(token_id=yes_token, price=snapped, size=round(shares, 2), side=BUY)
        opts    = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            oid = receipt.get('orderID', '')
            return True, oid, f'ladder bid {shares:.1f}sh @ {snapped:.3f}'
        return False, '', receipt.get('errorMsg', 'unknown')
    except Exception as e:
        return False, '', str(e)[:80]

def place_limit_sell(client, market, price, shares):
    """
    Place a resting GTC limit sell at `price` for `shares`.
    Returns (ok, order_id, msg).
    """
    yes_token = market['yes_token']
    try:
        tick, neg, tick_f, tick_dec = get_tick_info(client, yes_token)
        snapped = snap_to_tick(price, tick_f, tick_dec)
        snapped = min(snapped, 0.99)   # cap at 99¢

        args    = OrderArgs(token_id=yes_token, price=snapped, size=round(shares, 2), side=SELL)
        opts    = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            oid = receipt.get('orderID', '')
            return True, oid, f'sell {shares:.1f}sh @ {snapped:.3f}'
        return False, '', receipt.get('errorMsg', 'unknown')
    except Exception as e:
        return False, '', str(e)[:80]

# ── Ladder placement ───────────────────────────────────────────────────

def place_ladder(client, market, entry_price, entry_shares, base_size_usdc):
    """
    After an entry buy, place 3 descending limit bids.
    Returns list of ladder order dicts.
    """
    ladder = []
    for drop, size_mult in zip(LADDER_DROPS, LADDER_SIZE_MULT):
        bid_price  = entry_price * (1 - drop)
        bid_shares = round((base_size_usdc * size_mult) / bid_price, 2)
        bid_shares = max(bid_shares, 1.0)

        ok, oid, msg = place_limit_bid(client, market, bid_price, bid_shares)
        ladder.append({
            'order_id': oid,
            'price':    round(bid_price, 4),
            'shares':   bid_shares,
            'status':   'open' if ok else 'failed',
            'drop_pct': drop,
        })
        status = 'placed' if ok else 'FAILED'
        log(f'  Ladder rung {drop:.0%}: {status} — {msg}')
        time.sleep(0.3)  # avoid rate limit

    return ladder

# ── Ladder monitoring & rebalance sell ────────────────────────────────

def check_and_rebalance(client, state):
    """
    For every tracked position:
    1. Check if any ladder orders have filled (by polling open orders).
    2. If new fills detected → recalc avg cost → cancel stale sell → place new sell.
    3. If team eliminated (YES < 5¢) → cancel all open orders for that position.
    """
    positions = state.get('positions', {})
    if not positions:
        return

    log(f'Checking {len(positions)} tracked position(s) for ladder fills / rebalance...')

    for pos_key, pos in list(positions.items()):
        yes_token    = pos.get('yes_token', '')
        ladder_orders = pos.get('ladder_orders', [])
        if not yes_token or not ladder_orders:
            continue

        # Get current market price
        best_ask = get_best_ask(yes_token)
        if best_ask is None:
            continue

        # ── Eliminated team cleanup ──────────────────────────────────
        if best_ask < 0.05:
            log(f'  {pos_key}: team eliminated (YES={best_ask:.3f}), cancelling all orders')
            for lo in ladder_orders:
                if lo['status'] == 'open' and lo.get('order_id'):
                    cancel_order(client, lo['order_id'])
                    lo['status'] = 'cancelled'
            sell_oid = pos.get('sell_order_id')
            if sell_oid:
                cancel_order(client, sell_oid)
                pos['sell_order_id'] = ''
            tg(f'<b>🏀 Snowball cleanup</b>\n{pos_key}: team eliminated\nCancelled all open ladder/sell orders')
            continue

        # ── Check open orders to detect fills ───────────────────────
        open_order_ids = {o['id'] for o in get_open_orders(client, yes_token)}

        new_fills = False
        for lo in ladder_orders:
            if lo['status'] == 'open' and lo.get('order_id'):
                if lo['order_id'] not in open_order_ids:
                    # Order is gone from open orders → filled (or cancelled)
                    # Assume filled if market price is at or below the bid price + buffer
                    if best_ask <= lo['price'] * 1.05:
                        lo['status'] = 'filled'
                        new_fills = True
                        log(f'  Ladder fill detected: {lo["shares"]:.1f}sh @ {lo["price"]:.3f}')

        if not new_fills:
            log(f'  {pos_key}: no new ladder fills (YES={best_ask:.3f})')
            continue

        # ── Recalculate weighted avg cost ────────────────────────────
        total_cost   = pos.get('entry_cost', pos.get('entry_price', 0) * pos.get('entry_shares', 0))
        total_shares = pos.get('entry_shares', 0)

        for lo in ladder_orders:
            if lo['status'] == 'filled':
                total_cost   += lo['price'] * lo['shares']
                total_shares += lo['shares']

        avg_cost = total_cost / total_shares if total_shares > 0 else 0
        pos['avg_cost']     = round(avg_cost, 4)
        pos['total_shares'] = round(total_shares, 2)
        pos['total_cost']   = round(total_cost, 2)

        log(f'  New avg cost: {avg_cost:.3f} | total shares: {total_shares:.1f}')

        # ── Cancel existing sell order ───────────────────────────────
        old_sell = pos.get('sell_order_id', '')
        if old_sell:
            cancel_order(client, old_sell)
            log(f'  Cancelled old sell order {old_sell[:8]}')
            pos['sell_order_id'] = ''

        # ── Cancel remaining open ladder bids (don't avg down further) ─
        for lo in ladder_orders:
            if lo['status'] == 'open' and lo.get('order_id'):
                cancel_order(client, lo['order_id'])
                lo['status'] = 'cancelled'
                log(f'  Cancelled unfilled ladder bid @ {lo["price"]:.3f}')

        # ── Place new limit sell at avg_cost + TARGET_PROFIT_PCT ─────
        sell_price = avg_cost * (1 + TARGET_PROFIT_PCT)
        market_stub = {'yes_token': yes_token}
        ok, sell_oid, sell_msg = place_limit_sell(client, market_stub, sell_price, total_shares)

        if ok:
            pos['sell_order_id'] = sell_oid
            log(f'  ✓ Rebalance sell placed: {sell_msg}')
            tg(
                f'<b>🏀 Snowball rebalance</b>\n'
                f'{pos_key}\n'
                f'Ladder fill(s) detected — avg cost: {avg_cost:.3f}\n'
                f'New sell: {total_shares:.1f}sh @ {sell_price:.3f} (+{TARGET_PROFIT_PCT:.0%})\n'
                f'{sell_msg}'
            )
        else:
            log(f'  ✗ Sell order failed: {sell_msg}')

# ── Market data ────────────────────────────────────────────────────────

def get_polymarket_tournament_markets():
    r = requests.get(
        'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500',
        timeout=20
    )
    markets = r.json()
    result = {}
    for m in markets:
        q   = m.get('question', '')
        vol = float(m.get('volumeNum', 0) or 0)
        if vol < 50000:
            continue
        is_tournament = any(phrase in q for phrase in [
            'win the 2026 NCAA Tournament',
            'win the 2026 NBA Finals',
            'win the 2026 NHL Stanley Cup',
            'win the 2026 Masters',
            'win the 2026 FIFA World Cup',
            'win the 2026 NBA',
        ])
        if not is_tournament:
            continue
        try:
            prices = m.get('outcomePrices', '')
            if isinstance(prices, str):
                prices = [float(x.strip().strip('"')) for x in prices.strip('[]').split(',')]
            yes_p = float(prices[0])
        except:
            continue
        tokens = m.get('clobTokenIds', '[]')
        if isinstance(tokens, str):
            try:    tokens = json.loads(tokens)
            except: tokens = []
        if len(tokens) < 2:
            continue
        if 'NCAA Tournament' in q:
            sport = 'NCAA'
            team  = q.replace('Will the ', '').replace(' win the 2026 NCAA Tournament?', '').replace('Will ', '')
        elif 'NBA Finals' in q:
            sport = 'NBA'
            team  = q.replace('Will the ', '').replace(' win the 2026 NBA Finals?', '')
        elif 'NHL Stanley Cup' in q:
            sport = 'NHL'
            team  = q.replace('Will the ', '').replace(' win the 2026 NHL Stanley Cup?', '')
        elif 'Masters' in q:
            sport = 'GOLF'
            team  = q.replace('Will ', '').replace(' win the 2026 Masters tournament?', '')
        elif 'FIFA World Cup' in q:
            sport = 'SOCCER'
            team  = q.replace('Will ', '').replace(' win the 2026 FIFA World Cup?', '')
        else:
            sport = 'OTHER'
            team  = q[:40]
        result[team] = {
            'question':    q,
            'yes_p':       yes_p,
            'vol':         vol,
            'sport':       sport,
            'conditionId': m.get('conditionId', ''),
            'yes_token':   str(tokens[0]),
            'no_token':    str(tokens[1]),
            'end':         m.get('endDate', '')[:10],
        }
    return result

def get_live_ncaa_scores():
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard',
            timeout=10
        )
        games = []
        for g in r.json().get('events', []):
            status = g.get('status', {}).get('type', {})
            state  = status.get('state', '')
            detail = status.get('detail', '')
            comps  = g.get('competitions', [{}])[0].get('competitors', [])
            if not comps:
                continue
            home = next((c for c in comps if c.get('homeAway') == 'home'), {})
            away = next((c for c in comps if c.get('homeAway') == 'away'), {})
            info = {
                'state':      state,
                'detail':     detail,
                'home_team':  home.get('team', {}).get('displayName', ''),
                'home_score': int(home.get('score', 0) or 0),
                'home_rank':  home.get('curatedRank', {}).get('current', 99),
                'away_team':  away.get('team', {}).get('displayName', ''),
                'away_score': int(away.get('score', 0) or 0),
                'away_rank':  away.get('curatedRank', {}).get('current', 99),
                'winner':     None,
            }
            if state == 'post':
                if info['home_score'] > info['away_score']:
                    info['winner']      = info['home_team']
                    info['winner_rank'] = info['home_rank']
                else:
                    info['winner']      = info['away_team']
                    info['winner_rank'] = info['away_rank']
            games.append(info)
        return games
    except Exception as e:
        log(f'ESPN NCAA error: {e}')
        return []

def get_live_nba_scores():
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            timeout=10
        )
        games = []
        for g in r.json().get('events', []):
            status = g.get('status', {}).get('type', {})
            state  = status.get('state', '')
            period = g.get('status', {}).get('period', 0)
            comps  = g.get('competitions', [{}])[0].get('competitors', [])
            if not comps:
                continue
            home = next((c for c in comps if c.get('homeAway') == 'home'), {})
            away = next((c for c in comps if c.get('homeAway') == 'away'), {})
            h = int(home.get('score', 0) or 0)
            a = int(away.get('score', 0) or 0)
            lead = abs(h - a)
            leader = home if h > a else away
            games.append({
                'state':        state,
                'period':       period,
                'home_team':    home.get('team', {}).get('displayName', ''),
                'home_score':   h,
                'away_team':    away.get('team', {}).get('displayName', ''),
                'away_score':   a,
                'lead':         lead,
                'leading_team': leader.get('team', {}).get('displayName', ''),
            })
        return games
    except Exception as e:
        log(f'ESPN NBA error: {e}')
        return []

# ── Entry scoring ──────────────────────────────────────────────────────

def score_entry(market, game_state, lead=0, period=0):
    """Returns (should_buy, base_size_usdc, reason)."""
    yes_p = market['yes_p']
    if yes_p > MAX_YES_PRICE:
        return False, 0, f'YES too high ({yes_p:.0%})'
    if yes_p < MIN_YES_PRICE:
        return False, 0, f'Likely eliminated ({yes_p:.0%})'

    if game_state == 'post_win':
        if yes_p < 0.35:   size = 50
        elif yes_p < 0.60: size = 35
        else:               size = 20
        return True, size, f'Won tournament game, YES={yes_p:.0%}'

    if game_state == 'in' and lead >= 15 and period >= 3:
        if yes_p < 0.50:
            return True, 30, f'Leading +{lead} in P{period}, YES={yes_p:.0%}'

    return False, 0, 'No signal'

# ── Entry + ladder placement ───────────────────────────────────────────

def enter_position(client, market, pos_key, base_size, reason, state):
    """
    1. Market-buy YES
    2. Place 3 ladder bids below entry
    3. Place initial limit sell above entry
    4. Record everything in state
    """
    team_name = market.get('_team', pos_key)

    # Cap spend
    remaining = MAX_SPORTS_TOTAL - state['daily_spend']
    base_size  = min(base_size, MAX_PER_TEAM, remaining)
    if base_size < 10:
        log(f'  Budget too small (${base_size:.0f}), skipping')
        return False

    log(f'ENTER {team_name}: {reason} | base ${base_size:.0f}')
    ok, entry_price, entry_shares, entry_oid, msg = place_market_buy(client, market, base_size)
    if not ok:
        log(f'  ✗ Market buy failed: {msg}')
        return False

    log(f'  ✓ Entry: {msg}')
    entry_cost = entry_price * entry_shares
    state['daily_spend'] += entry_cost

    # Place ladder bids
    log('  Placing limit ladder...')
    ladder = place_ladder(client, market, entry_price, entry_shares, base_size)

    # Place initial limit sell at entry + TARGET_PROFIT_PCT
    initial_sell_price = entry_price * (1 + TARGET_PROFIT_PCT)
    ok_s, sell_oid, sell_msg = place_limit_sell(
        client, market, initial_sell_price, entry_shares
    )
    if ok_s:
        log(f'  ✓ Initial sell: {sell_msg}')
    else:
        log(f'  ✗ Sell order failed: {sell_msg}')
        sell_oid = ''

    # Persist state
    state.setdefault('positions', {})[pos_key] = {
        'team':          team_name,
        'yes_token':     market['yes_token'],
        'entry_price':   entry_price,
        'entry_shares':  entry_shares,
        'entry_cost':    entry_cost,
        'entry_oid':     entry_oid,
        'avg_cost':      entry_price,
        'total_shares':  entry_shares,
        'total_cost':    entry_cost,
        'sell_order_id': sell_oid,
        'ladder_orders': ladder,
        'filled_ladder_count': 0,
        'reason':        reason,
        'ts':            datetime.datetime.utcnow().isoformat(),
    }

    tg(
        f'<b>🏀 Snowball entry: {team_name}</b>\n'
        f'{reason}\n'
        f'Bought {entry_shares:.1f}sh @ {entry_price:.3f} = ${entry_cost:.2f}\n'
        f'Ladder bids: {len([l for l in ladder if l["status"]=="open"])} placed\n'
        f'Initial sell @ {initial_sell_price:.3f} (+{TARGET_PROFIT_PCT:.0%})'
    )
    return True

# ── Main ───────────────────────────────────────────────────────────────

def main():
    log('=== Sports Snowball Trader (v2 — ladder) Starting ===')

    state = load_state()
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')

    if state.get('last_date') != today:
        state['daily_spend'] = 0
        state['last_date']   = today
        log('New day — daily spend reset')

    # Portfolio check
    r = requests.get(f'https://data-api.polymarket.com/value?user={FUNDER}', timeout=10)
    val    = r.json()
    equity = float(val[0]['value']) if isinstance(val, list) else float(val['value'])
    rp     = requests.get(f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50', timeout=10)
    pos_total = sum(float(p.get('currentValue', 0)) for p in rp.json())
    cash   = equity - pos_total

    log(f'Cash: ${cash:.2f} | Sports spend today: ${state["daily_spend"]:.2f}')

    if cash < MIN_USDC and not state.get('positions'):
        log(f'No cash (${cash:.2f}) and no open positions — nothing to do')
        return

    # Init CLOB client
    client = ClobClient(
        'https://clob.polymarket.com',
        key=PRIVATE_KEY, chain_id=POLYGON,
        signature_type=2, funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    # ── Phase 1: Check ladder fills & rebalance sells ──────────────
    check_and_rebalance(client, state)

    # ── Phase 2: DK Discord pick entries only (DK_MODE=True) ────────
    if DK_MODE:
        dk_picks = load_dk_picks()
        if not dk_picks:
            log('[DK MODE] No pending picks — skipping new entries')
        else:
            log(f'[DK MODE] Processing {len(dk_picks)} DK pick(s)...')
            markets = get_polymarket_tournament_markets()
            for pick in dk_picks:
                team_name, mkt = find_market_for_pick(pick, markets)
                if not mkt:
                    log(f'  [DK] No market found for pick: {pick}')
                    tg(f'⚠️ DK pick not found on Polymarket: {pick.get("team",pick)}')
                    continue
                size    = min(pick.get('size', MAX_PER_TEAM), MAX_PER_TEAM)
                pos_key = f'dk_{team_name}_{today}'
                if pos_key in state.get('positions', {}):
                    log(f'  [DK] Already in {team_name} today — skip')
                    continue
                mkt['_team'] = team_name
                reason = f'DK Discord pick: {pick.get("note", team_name)}'
                enter_position(client, mkt, pos_key, size, reason, state)
    else:
        # Legacy: independent scanning (disabled — set DK_MODE=True above)
        log('[SCAN MODE] Independent scanning active (DK_MODE=False)')
        if cash >= MIN_USDC and state['daily_spend'] < MAX_SPORTS_TOTAL:
            log('Fetching tournament markets...')
            markets = get_polymarket_tournament_markets()
            log(f'Found {len(markets)} markets — scanning disabled, use DK_MODE')
        else:
            log('Skipping new entries (budget or cash exhausted)')

    save_state(state)
    log(f'=== Done | Sports spend today: ${state["daily_spend"]:.2f} ===')

if __name__ == '__main__':
    main()
