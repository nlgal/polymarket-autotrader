"""
whale_scanner.py
================
Scans Polymarket leaderboard to build a watchlist of elite wallets.

Criteria (configurable):
  - All-time PnL >= MIN_PNL
  - PnL/Volume ratio >= MIN_PNL_RATIO (profit efficiency, not just volume)
  - Active in last 30 days (recent activity check)

Pulls top 200 wallets from the leaderboard (paging through ALL-time PnL),
scores each one, saves the top candidates to whale_watchlist.json.

Run manually or weekly to refresh the watchlist.
"""
import os, sys, json, time, requests, datetime
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')

WATCHLIST_FILE = '/opt/polymarket-agent/whale_watchlist.json'

# ── Scoring criteria ────────────────────────────────────────────────────
MIN_PNL        = 100_000   # At least $100k all-time profit
MIN_PNL_RATIO  = 0.10      # At least 10% profit on volume (efficiency filter)
MAX_WATCHLIST  = 20        # Keep top 20 wallets
ACTIVITY_WINDOW_DAYS = 14  # Must have traded in last 14 days (was 30 — tighter for freshness)
STALE_DAYS     = 21        # Remove from watchlist if no trades for 21 days

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg[:4000], 'parse_mode': 'HTML'}, timeout=10)
        except: pass

def log(msg):
    print(f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}")

def fetch_leaderboard(limit=50, offset=0):
    r = requests.get(
        f'https://data-api.polymarket.com/v1/leaderboard'
        f'?timePeriod=ALL&orderBy=PNL&limit={limit}&offset={offset}',
        timeout=15
    )
    if r.status_code == 200:
        return r.json()
    log(f"Leaderboard error {r.status_code}")
    return []

def get_last_activity(wallet):
    """Return timestamp of most recent trade, or 0 if none."""
    try:
        r = requests.get(
            f'https://data-api.polymarket.com/activity?user={wallet}&limit=1',
            timeout=10
        )
        if r.status_code == 200:
            acts = r.json()
            if acts:
                return int(acts[0].get('timestamp', 0))
    except:
        pass
    return 0

def score_wallet(entry):
    """
    Score a leaderboard entry.
    Returns dict with score and metadata, or None if doesn't qualify.
    """
    wallet = entry.get('proxyWallet', '')
    pnl    = float(entry.get('pnl', 0) or 0)
    vol    = float(entry.get('vol', 0) or 0)
    name   = entry.get('userName', '') or entry.get('pseudonym', '') or wallet[:10]
    rank   = entry.get('rank', '?')

    if pnl < MIN_PNL:
        return None
    if vol <= 0:
        return None

    pnl_ratio = pnl / vol

    if pnl_ratio < MIN_PNL_RATIO:
        return None

    # Check recency — must have traded in last ACTIVITY_WINDOW_DAYS days
    last_ts = get_last_activity(wallet)
    days_ago = (time.time() - last_ts) / 86400 if last_ts else 999
    if days_ago > ACTIVITY_WINDOW_DAYS:
        return None

    # Composite score: weight PnL heavily, bonus for efficiency
    score = pnl * (1 + pnl_ratio)

    return {
        'wallet':      wallet,
        'name':        name,
        'rank':        rank,
        'pnl':         round(pnl, 2),
        'volume':      round(vol, 2),
        'pnl_ratio':   round(pnl_ratio, 4),
        'days_since_trade': round(days_ago, 1),
        'score':       round(score, 2),
        'last_seen':   {},   # populated by whale_monitor
        'added_at':    datetime.datetime.utcnow().isoformat(),
    }

def main():
    log('=== Whale Scanner Starting ===')

    candidates = []
    pages = 4  # Pull top 200 wallets (4 × 50)

    for page in range(pages):
        offset = page * 50
        log(f'Fetching leaderboard page {page+1} (offset={offset})...')
        entries = fetch_leaderboard(limit=50, offset=offset)
        if not entries:
            break

        for entry in entries:
            pnl = float(entry.get('pnl', 0) or 0)
            if pnl < MIN_PNL:
                break  # Sorted by PnL, so we can stop early

            scored = score_wallet(entry)
            if scored:
                candidates.append(scored)
                log(f'  ✓ {scored["name"]} | PnL=${scored["pnl"]:,.0f} '
                    f'| ratio={scored["pnl_ratio"]:.1%} | {scored["days_since_trade"]:.0f}d ago')

        time.sleep(1)  # Rate limit

    # Sort by composite score and take top N
    candidates.sort(key=lambda x: x['score'], reverse=True)
    new_wallets = candidates[:MAX_WATCHLIST]

    # ── Smart merge: preserve last_seen state, drop stale wallets ─────────
    existing_last_seen = {}
    try:
        with open(WATCHLIST_FILE) as f:
            old_data = json.load(f)
            for w in old_data.get('wallets', []):
                existing_last_seen[w['wallet']] = w.get('last_seen', {})
    except:
        pass

    # Restore last_seen timestamps for wallets that carried over
    for w in new_wallets:
        if w['wallet'] in existing_last_seen:
            w['last_seen'] = existing_last_seen[w['wallet']]

    watchlist = new_wallets
    dropped = [a[:10] for a in existing_last_seen if a not in {w['wallet'] for w in watchlist}]

    log(f'\nFound {len(candidates)} qualifying → keeping top {len(watchlist)}')
    if dropped:
        log(f'Rotated out (stale/dropped rank): {dropped}')

    # Save watchlist
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump({
            'updated_at': datetime.datetime.utcnow().isoformat(),
            'criteria': {'min_pnl': MIN_PNL, 'min_pnl_ratio': MIN_PNL_RATIO,
                         'activity_window_days': ACTIVITY_WINDOW_DAYS},
            'wallets': watchlist
        }, f, indent=2)

    log(f'Watchlist saved to {WATCHLIST_FILE}')

    # Summary telegram
    if watchlist:
        msg = f'<b>🐳 Whale Watchlist Updated</b>\n'
        msg += f'{len(watchlist)} elite wallets tracked\n\n'
        for w in watchlist[:10]:
            msg += f'• <b>{w["name"]}</b>: ${w["pnl"]:,.0f} PnL ({w["pnl_ratio"]:.0%} efficiency)\n'
        tg(msg)

    log('=== Done ===')

if __name__ == '__main__':
    main()
