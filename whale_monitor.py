"""
whale_monitor.py
================
Hourly monitor: checks tracked whale wallets for new trades.

For each wallet in whale_watchlist.json:
  1. Fetch recent activity
  2. Compare to last-seen timestamp stored in state
  3. If new trade found → check if it's a significant entry (>$500)
  4. Look up the market they're trading
  5. Alert via Telegram with full context

Alert includes:
  - Whale name + PnL track record
  - Market they just entered
  - Direction (YES/NO), size, price
  - Current market price + our existing position (if any)
  - Direct Polymarket link

State: whale_monitor_state.json (last-seen timestamps per wallet)
"""
import os, sys, json, time, requests, datetime
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
FUNDER   = os.environ.get('POLYMARKET_FUNDER_ADDRESS', '')

WATCHLIST_FILE   = '/opt/polymarket-agent/whale_watchlist.json'
STATE_FILE       = '/opt/polymarket-agent/whale_monitor_state.json'

MIN_TRADE_SIZE   = 500    # Only alert on trades >= $500
EXIT_WATCH_FILE  = '/opt/polymarket-agent/whale_exit_watch.json'  # wallets to monitor for exits
LOOKBACK_HOURS   = 2      # Check last 2 hours of activity

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg[:4000], 'parse_mode': 'HTML',
                      'disable_web_page_preview': True}, timeout=10)
        except: pass

def log(msg):
    print(f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}")

def load_watchlist():
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
            return data.get('wallets', [])
    except:
        return []

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def load_exit_watch():
    """Load list of whale wallets we're watching for exits."""
    try:
        with open(EXIT_WATCH_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_exit_watch(data):
    with open(EXIT_WATCH_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_recent_trades(wallet, since_ts):
    """Fetch trades for wallet since since_ts. Returns list of trade dicts."""
    try:
        r = requests.get(
            f'https://data-api.polymarket.com/activity?user={wallet}&limit=50',
            timeout=10
        )
        if r.status_code != 200:
            return []
        acts = r.json()
        trades = []
        for a in acts:
            if a.get('type') != 'TRADE':
                continue
            ts = int(a.get('timestamp', 0))
            if ts <= since_ts:
                break
            size = float(a.get('usdcSize', 0) or 0)
            if size < MIN_TRADE_SIZE:
                continue
            trades.append(a)
        return trades
    except:
        return []

def get_market_price(condition_id):
    """Get current YES price for a market."""
    try:
        r = requests.get(
            f'https://gamma-api.polymarket.com/markets?conditionId={condition_id}',
            timeout=8
        )
        if r.status_code == 200:
            markets = r.json()
            if markets:
                m = markets[0]
                prices = m.get('outcomePrices', '')
                if isinstance(prices, str):
                    prices = [float(x.strip().strip('"'))
                              for x in prices.strip('[]').split(',')]
                return float(prices[0]) if prices else None
    except:
        pass
    return None

def get_our_position(condition_id):
    """Check if we have a position in this market."""
    try:
        r = requests.get(
            f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50',
            timeout=8
        )
        if r.status_code == 200:
            positions = r.json()
            for p in positions:
                if p.get('conditionId', '') == condition_id:
                    return p
    except:
        pass
    return None

def format_alert(whale, trade):
    """Build Telegram alert for a whale trade."""
    name       = whale.get('name', whale['wallet'][:10])
    pnl        = whale.get('pnl', 0)
    pnl_ratio  = whale.get('pnl_ratio', 0)
    wallet     = whale['wallet']

    title      = trade.get('title', trade.get('slug', ''))[:60]
    outcome    = trade.get('outcome', '')
    size       = float(trade.get('usdcSize', 0) or 0)
    price      = float(trade.get('price', 0) or 0)
    side       = trade.get('side', '')
    condition  = trade.get('conditionId', '')
    slug       = trade.get('eventSlug', trade.get('slug', ''))

    # Current market price
    cur_price = get_market_price(condition)
    price_str = f'{cur_price:.2f}' if cur_price else 'unknown'

    # Our position
    our_pos = get_our_position(condition)
    our_str = ''
    if our_pos:
        our_val = float(our_pos.get('currentValue', 0) or 0)
        our_outcome = our_pos.get('outcome', '')
        our_str = f'\n<i>We hold: {our_outcome} ${our_val:.0f}</i>'

    direction = f'{outcome} {side}'.strip().upper()

    # Check if this wallet is being exit-watched (we followed their entry)
    exit_watch = load_exit_watch()
    exit_flag = ''
    if wallet in exit_watch:
        watched = exit_watch[wallet]
        followed_market = watched.get('market_title', '')[:40]
        followed_dir = watched.get('direction', '')
        exit_flag = f'\n⚠️ <b>EXIT WATCH</b>: We followed this wallet into {followed_dir} on {followed_market}'
        # If they're now trading the SAME market in OPPOSITE direction → exit signal
        if condition == watched.get('condition_id', '') and outcome != watched.get('outcome', ''):
            exit_flag = f'\n🚨 <b>WHALE EXITING</b>: Selling {watched.get("direction","")} on {followed_market} — consider unwinding our position'

    msg = (
        f'<b>🐳 Whale Alert</b>\n'
        f'<b>{name}</b> just entered\n\n'
        f'<b>Market:</b> {title}\n'
        f'<b>Direction:</b> {direction}\n'
        f'<b>Size:</b> ${size:,.0f} @ {price:.2f}\n'
        f'<b>Current price:</b> {price_str}\n'
        f'{our_str}\n'
        f'<b>Whale stats:</b> ${pnl:,.0f} all-time PnL ({pnl_ratio:.0%} efficiency)\n'
    )
    if exit_flag:
        msg += exit_flag
    if slug:
        msg += f'\nhttps://polymarket.com/event/{slug}'

    return msg

def main():
    log('=== Whale Monitor Starting ===')

    watchlist = load_watchlist()
    if not watchlist:
        log('No watchlist found — run whale_scanner.py first')
        return

    log(f'Monitoring {len(watchlist)} wallets')
    state    = load_state()
    now_ts   = int(time.time())
    cutoff   = now_ts - (LOOKBACK_HOURS * 3600)
    alerts   = 0

    for whale in watchlist:
        wallet   = whale['wallet']
        name     = whale.get('name', wallet[:10])
        last_seen = state.get(wallet, cutoff)

        trades = get_recent_trades(wallet, since_ts=last_seen)
        if not trades:
            continue

        log(f'  {name}: {len(trades)} new trade(s)')

        for trade in trades:
            ts = int(trade.get('timestamp', 0))
            # Update last seen
            if ts > state.get(wallet, 0):
                state[wallet] = ts

            alert_msg = format_alert(whale, trade)
            tg(alert_msg)
            log(f'    Alert sent: {trade.get("title","")[:50]} '
                f'{trade.get("outcome","")} ${float(trade.get("usdcSize",0)):,.0f}')
            alerts += 1
            time.sleep(0.5)

    save_state(state)
    log(f'=== Done. {alerts} alert(s) sent ===')

if __name__ == '__main__':
    main()
