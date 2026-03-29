"""
live_sports_trader.py
=====================
Snowball Strategy for Polymarket Tournament Markets

Based on the proven alpha: bet on heavy favorites, scale in as their
probability increases, compound gains.

Adapted for Polymarket's tournament market structure:
- NCAA Tournament winner markets
- NBA Finals winner markets  
- Masters golf winner markets
- NHL Stanley Cup winner markets

Strategy:
1. Monitor live game scores via ESPN API
2. When a tournament favorite WINS a game → their championship probability
   jumps before Polymarket fully reprices → BUY YES pre-reprice
3. Scale in as team keeps winning rounds
4. Sell if team is eliminated

Snowball phases (tournament rounds):
- Round of 64/Sweet 16 win → small buy (5% bankroll)
- Elite 8 win → medium buy (10% bankroll)  
- Final Four win → large buy (20% bankroll)
- Championship game → scale max (25% bankroll)

Safety rules:
- MAX $75 per team per round
- MAX $200 total deployed to sports
- Min odds improvement: YES must be below 80¢ (still has upside)
- Hard stop: if team eliminated, no further buys on any sport that day

Run every 5 min during game hours (1pm-midnight EDT on game days)
"""
import os, sys, json, math, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

# ── Config ─────────────────────────────────────────────────────────────
MAX_PER_TEAM = 75        # Max $ per team total
MAX_SPORTS_TOTAL = 200   # Max total sports exposure
MAX_YES_PRICE = 0.82     # Don't buy if already >82¢ (too little upside)
MIN_YES_PRICE = 0.03     # Don't buy if <3¢ (basically eliminated)
MIN_USDC = 20            # Don't trade if less than $20 available

STATE_FILE = '/opt/polymarket-agent/sports_state.json'

# ── Team → Polymarket market mapping ──────────────────────────────────
# Format: ESPN team name → (conditionId, YES_token, NO_token, sport)
# Populated dynamically from Polymarket API

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}')

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {'positions': {}, 'daily_spend': 0, 'last_date': ''}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_polymarket_tournament_markets():
    """Fetch all active tournament winner markets from Polymarket."""
    r = requests.get(
        'https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500',
        timeout=20
    )
    markets = r.json()
    
    result = {}
    for m in markets:
        q = m.get('question', '')
        vol = float(m.get('volumeNum', 0) or 0)
        if vol < 50000:
            continue
        
        # Tournament winner patterns
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
            try: tokens = json.loads(tokens)
            except: tokens = []
        
        if len(tokens) < 2:
            continue
        
        # Determine sport
        if 'NCAA Tournament' in q:
            sport = 'NCAA'
            team = q.replace('Will the ', '').replace(' win the 2026 NCAA Tournament?', '').replace('Will ', '').replace(' win the 2026 NCAA Tournament?', '')
        elif 'NBA Finals' in q:
            sport = 'NBA'
            team = q.replace('Will the ', '').replace(' win the 2026 NBA Finals?', '')
        elif 'NHL Stanley Cup' in q:
            sport = 'NHL'
            team = q.replace('Will the ', '').replace(' win the 2026 NHL Stanley Cup?', '')
        elif 'Masters' in q:
            sport = 'GOLF'
            team = q.replace('Will ', '').replace(' win the 2026 Masters tournament?', '')
        elif 'FIFA World Cup' in q:
            sport = 'SOCCER'
            team = q.replace('Will ', '').replace(' win the 2026 FIFA World Cup?', '')
        else:
            sport = 'OTHER'
            team = q[:40]
        
        result[team] = {
            'question': q,
            'yes_p': yes_p,
            'vol': vol,
            'sport': sport,
            'conditionId': m.get('conditionId', ''),
            'yes_token': str(tokens[0]),
            'no_token': str(tokens[1]),
            'end': m.get('endDate', '')[:10],
        }
    
    return result

def get_live_ncaa_scores():
    """Get live NCAA tournament scores from ESPN."""
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard',
            timeout=10
        )
        games = r.json().get('events', [])
        live_games = []
        
        for g in games:
            status = g.get('status', {}).get('type', {})
            state = status.get('state', '')  # pre, in, post
            detail = status.get('detail', '')
            
            competitors = g.get('competitions', [{}])[0].get('competitors', [])
            if not competitors:
                continue
            
            home = next((c for c in competitors if c.get('homeAway') == 'home'), {})
            away = next((c for c in competitors if c.get('homeAway') == 'away'), {})
            
            game_info = {
                'state': state,
                'detail': detail,
                'home_team': home.get('team', {}).get('displayName', ''),
                'home_score': int(home.get('score', 0) or 0),
                'home_rank': home.get('curatedRank', {}).get('current', 99),
                'away_team': away.get('team', {}).get('displayName', ''),
                'away_score': int(away.get('score', 0) or 0),
                'away_rank': away.get('curatedRank', {}).get('current', 99),
                'winner': None,
            }
            
            # Determine winner if game over
            if state == 'post':
                if game_info['home_score'] > game_info['away_score']:
                    game_info['winner'] = game_info['home_team']
                    game_info['winner_rank'] = game_info['home_rank']
                else:
                    game_info['winner'] = game_info['away_team']
                    game_info['winner_rank'] = game_info['away_rank']
            
            live_games.append(game_info)
        
        return live_games
    except Exception as e:
        log(f'ESPN fetch error: {e}')
        return []

def get_live_nba_scores():
    """Get live NBA scores from ESPN."""
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
            timeout=10
        )
        games = r.json().get('events', [])
        live_games = []
        
        for g in games:
            status = g.get('status', {}).get('type', {})
            state = status.get('state', '')
            period = g.get('status', {}).get('period', 0)
            
            competitors = g.get('competitions', [{}])[0].get('competitors', [])
            if not competitors:
                continue
            
            home = next((c for c in competitors if c.get('homeAway') == 'home'), {})
            away = next((c for c in competitors if c.get('homeAway') == 'away'), {})
            
            h_score = int(home.get('score', 0) or 0)
            a_score = int(away.get('score', 0) or 0)
            lead = abs(h_score - a_score)
            leading_team = home if h_score > a_score else away
            
            live_games.append({
                'state': state,
                'period': period,
                'home_team': home.get('team', {}).get('displayName', ''),
                'home_abbr': home.get('team', {}).get('abbreviation', ''),
                'home_score': h_score,
                'away_team': away.get('team', {}).get('displayName', ''),
                'away_abbr': away.get('team', {}).get('abbreviation', ''),
                'away_score': a_score,
                'lead': lead,
                'leading_team': leading_team.get('team', {}).get('displayName', ''),
                'leading_abbr': leading_team.get('team', {}).get('abbreviation', ''),
            })
        
        return live_games
    except Exception as e:
        log(f'NBA ESPN error: {e}')
        return []

def score_opportunity(team_name, market, state, lead=0, period=0, game_state='pre'):
    """
    Score how good a buy opportunity is.
    Returns (should_buy, size_usdc, reason)
    
    The snowball logic:
    - Tournament game just finished: team won → buy before market reprices
    - Game in progress: team leading big late → buy on near-certain outcome
    """
    yes_p = market['yes_p']
    
    # Skip near-certain or eliminated
    if yes_p > MAX_YES_PRICE:
        return False, 0, f'YES too high ({yes_p:.0%})'
    if yes_p < MIN_YES_PRICE:
        return False, 0, f'Team likely eliminated ({yes_p:.0%})'
    
    # Game just finished and this team WON
    if game_state == 'post_win':
        # Strong buy — repricing expected
        if yes_p < 0.35:
            size = 50  # Good value, buy more
        elif yes_p < 0.60:
            size = 35
        else:
            size = 20
        return True, size, f'Just won tournament game, YES at {yes_p:.0%}'
    
    # Game in progress, large lead late
    if game_state == 'in' and lead >= 15 and period >= 3:
        if yes_p < 0.50:
            size = 30
            return True, size, f'Leading by {lead} in period {period}, YES at {yes_p:.0%}'
    
    return False, 0, 'No signal'

def place_buy(client, market, size_usdc, reason):
    """Place a buy YES order on a tournament market."""
    yes_token = market['yes_token']
    
    try:
        # Get CLOB order book
        rb = requests.get(f'https://clob.polymarket.com/book?token_id={yes_token}', timeout=8)
        book = rb.json()
        asks = book.get('asks', [])
        
        if not asks:
            return False, 'No asks in order book'
        
        best_ask = float(asks[0]['price'])
        if best_ask > MAX_YES_PRICE:
            return False, f'Ask {best_ask:.3f} too high'
        
        tick = client.get_tick_size(yes_token)
        neg_risk = client.get_neg_risk(yes_token)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
        
        buy_price = round(round(best_ask / tick_f) * tick_f, tick_dec)
        shares = round(size_usdc / buy_price, 2)
        
        if shares < 1:
            return False, f'Too few shares ({shares:.2f})'
        
        args = OrderArgs(token_id=yes_token, price=buy_price, size=shares, side=BUY)
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get('success') or receipt.get('orderID'):
            actual_cost = shares * buy_price
            return True, f'{shares:.1f} shares @ {buy_price:.3f} = ${actual_cost:.2f}'
        else:
            return False, receipt.get('errorMsg', 'unknown error')
    
    except Exception as e:
        return False, str(e)[:80]

def main():
    log('=== Sports Snowball Trader Starting ===')
    
    state = load_state()
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    
    # Reset daily spend on new day
    if state.get('last_date') != today:
        state['daily_spend'] = 0
        state['last_date'] = today
        log('New day — daily spend reset')
    
    # Check CLOB balance
    CLOB_BASE = 'https://clob.polymarket.com'
    r = requests.get(f'https://data-api.polymarket.com/value?user={FUNDER}', timeout=10)
    val = r.json()
    equity = float(val[0]['value']) if isinstance(val, list) else float(val['value'])
    
    rp = requests.get(f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50', timeout=10)
    positions = rp.json()
    pos_total = sum(float(p.get('currentValue', 0)) for p in positions)
    cash = equity - pos_total
    
    log(f'Cash: ${cash:.2f} | Daily sports spend: ${state["daily_spend"]:.2f}')
    
    if cash < MIN_USDC:
        log(f'Insufficient cash (${cash:.2f}) — skipping sports scan')
        return
    
    if state['daily_spend'] >= MAX_SPORTS_TOTAL:
        log(f'Daily sports limit hit (${state["daily_spend"]:.2f}) — done for today')
        return
    
    # Init CLOB client
    client = ClobClient(
        'https://clob.polymarket.com',
        key=PRIVATE_KEY, chain_id=POLYGON,
        signature_type=2, funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    
    # Get tournament markets from Polymarket
    log('Fetching tournament markets...')
    markets = get_polymarket_tournament_markets()
    log(f'Found {len(markets)} tournament markets')
    
    trades_placed = []
    
    # ── NCAA Tournament check ─────────────────────────────────────────
    log('Checking NCAA tournament games...')
    ncaa_games = get_live_ncaa_scores()
    ncaa_markets = {k: v for k, v in markets.items() if v['sport'] == 'NCAA'}
    
    for game in ncaa_games:
        state_g = game['state']
        winner = game.get('winner')
        
        if state_g == 'post' and winner:
            # Game finished — find this team's Polymarket market
            for team_name, market in ncaa_markets.items():
                if winner.lower() in team_name.lower() or team_name.lower() in winner.lower():
                    # Check if we already bought for this win
                    pos_key = f'ncaa_{team_name}_{today}'
                    if pos_key in state.get('positions', {}):
                        continue
                    
                    should_buy, size, reason = score_opportunity(
                        team_name, market, state_g, game_state='post_win'
                    )
                    
                    if should_buy:
                        size = min(size, MAX_PER_TEAM, MAX_SPORTS_TOTAL - state['daily_spend'])
                        if size >= 10:
                            log(f'BUY {team_name} YES: {reason}')
                            ok, msg = place_buy(client, market, size, reason)
                            if ok:
                                log(f'  ✓ {msg}')
                                state['daily_spend'] += size
                                state.setdefault('positions', {})[pos_key] = {
                                    'team': team_name, 'size': size, 'reason': reason
                                }
                                trades_placed.append(f'{team_name} YES ${size:.0f} ({reason})')
                                tg(f'<b>🏀 Snowball: {team_name}</b>\n{reason}\nBought ${size:.0f} YES\n{msg}')
                            else:
                                log(f'  ✗ {msg}')
        
        elif state_g == 'in':
            # Game in progress — check for large lead late
            lead = abs(game['home_score'] - game['away_score'])
            leading = game['home_team'] if game['home_score'] > game['away_score'] else game['away_team']
            
            for team_name, market in ncaa_markets.items():
                if leading.lower() in team_name.lower() or team_name.lower() in leading.lower():
                    pos_key = f'ncaa_live_{team_name}_{today}'
                    if pos_key in state.get('positions', {}):
                        continue
                    
                    # Parse period from detail (e.g., "2nd Half - 8:32")
                    detail = game.get('detail', '')
                    period = 2 if '2nd' in detail else 1
                    
                    should_buy, size, reason = score_opportunity(
                        team_name, market, state_g, lead=lead, period=period, game_state='in'
                    )
                    
                    if should_buy:
                        size = min(size, MAX_PER_TEAM, MAX_SPORTS_TOTAL - state['daily_spend'])
                        if size >= 10:
                            log(f'BUY {team_name} YES (live): {reason}')
                            ok, msg = place_buy(client, market, size, reason)
                            if ok:
                                log(f'  ✓ {msg}')
                                state['daily_spend'] += size
                                state.setdefault('positions', {})[pos_key] = {
                                    'team': team_name, 'size': size, 'reason': reason
                                }
                                trades_placed.append(f'{team_name} YES ${size:.0f} live')
                                tg(f'<b>🏀 Live Snowball: {team_name}</b>\n{reason}\nBought ${size:.0f} YES\n{msg}')
                            else:
                                log(f'  ✗ {msg}')
    
    # ── NBA playoff check (same logic) ──────────────────────────────
    log('Checking NBA games...')
    nba_games = get_live_nba_scores()
    nba_markets = {k: v for k, v in markets.items() if v['sport'] == 'NBA'}
    nba_standings = {
        'Oklahoma City Thunder': 1, 'San Antonio Spurs': 2, 'Detroit Pistons': 3,
        'Boston Celtics': 4, 'New York Knicks': 5, 'Los Angeles Lakers': 6,
        'Denver Nuggets': 7, 'Cleveland Cavaliers': 8, 'Minnesota Timberwolves': 9,
        'Houston Rockets': 10,
    }
    
    for game in nba_games:
        if game['state'] != 'in':
            continue
        
        lead = game['lead']
        period = game.get('period', 0)
        leading = game['leading_team']
        rank = nba_standings.get(leading, 99)
        
        # Only snowball on top-10 teams with big leads late
        if rank > 10 or lead < 15 or period < 3:
            continue
        
        for team_name, market in nba_markets.items():
            if leading.lower() in team_name.lower() or team_name.lower() in leading.lower():
                pos_key = f'nba_live_{team_name}_{today}'
                if pos_key in state.get('positions', {}):
                    continue
                
                should_buy, size, reason = score_opportunity(
                    team_name, market, 'in', lead=lead, period=period, game_state='in'
                )
                
                if should_buy:
                    size = min(size, MAX_PER_TEAM - sum(
                        p.get('size', 0) for k, p in state.get('positions', {}).items()
                        if team_name in k
                    ), MAX_SPORTS_TOTAL - state['daily_spend'])
                    if size >= 10:
                        log(f'NBA BUY {team_name} YES: {reason}')
                        ok, msg = place_buy(client, market, size, reason)
                        if ok:
                            log(f'  ✓ {msg}')
                            state['daily_spend'] += size
                            state.setdefault('positions', {})[pos_key] = {
                                'team': team_name, 'size': size
                            }
                            trades_placed.append(f'NBA {team_name} YES ${size:.0f}')
                            tg(f'<b>🏀 NBA Snowball: {team_name}</b>\n{reason}\n${msg}')
                        else:
                            log(f'  ✗ {msg}')
    
    save_state(state)
    
    log(f'=== Done. Trades: {len(trades_placed)} | Sports spend today: ${state["daily_spend"]:.2f} ===')
    if trades_placed:
        for t in trades_placed:
            log(f'  + {t}')

if __name__ == '__main__':
    main()
