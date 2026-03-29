"""
discord_monitor.py
==================
Monitors DeityFNF Discord for sports/Polymarket trade calls.

Channels watched:
  #predictions        — dkxbt's direct calls (high conviction)
  #member-calls       — lexiconical, dkxbt deeper analysis
  #member-calls-2     — Shadow (PM Era) — direct Polymarket calls
  #bet-calls          — Cooker + others — game-specific moneyline calls

Every run:
  1. Fetch last 20 messages from each channel
  2. Filter to messages newer than last seen timestamp (stored in state)
  3. Parse for actionable signals (team names, YES/NO/buy/bet keywords)
  4. Send Telegram alert for each new call with context
  5. Save last-seen message IDs so we never double-alert

State file: /opt/polymarket-agent/discord_state.json
"""
import os, sys, json, re, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '')
TG_TOKEN      = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT       = os.environ.get('TELEGRAM_CHAT_ID', '')

STATE_FILE = '/opt/polymarket-agent/discord_state.json'

CHANNELS = {
    "predictions":    "1445706612961447996",
    "member-calls":   "1425216163914453164",
    "member-calls-2": "1486484462316290251",
    "bet-calls":      "1486484798217130055",
}

# Keywords that suggest a Polymarket / sports trade call
SIGNAL_KEYWORDS = [
    # direct action
    'buying', 'bought', 'buy', 'bet', 'taking', 'take', 'rolling', 'going',
    'fading', 'fade', 'selling', 'sold', 'adding', 'add',
    # market direction
    'yes', 'no', 'ml', 'moneyline', 'cover', 'over', 'under',
    # sports
    'ncaa', 'nba', 'nfl', 'nhl', 'tennis', 'soccer', 'esports', 'cs2', 'dota',
    'tournament', 'finals', 'championship',
    # polymarket specific
    'polymarket', 'poly', 'market', 'position', 'shares',
    # confidence words
    'ez', 'easy', 'lock', 'conviction', 'strong', 'confident', 'guaranteed',
]

# Authors known to post actionable calls
ALPHA_AUTHORS = {'dkxbt', 'lexiconical', 'shadow (pm era)', 'cooker', 'picofromportugal'}

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg[:4000], 'parse_mode': 'HTML',
                      'disable_web_page_preview': True},
                timeout=10
            )
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}')

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {'last_seen': {}}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def fetch_messages(channel_id, limit=20):
    headers = {
        'Authorization': DISCORD_TOKEN,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    r = requests.get(
        f'https://discord.com/api/v9/channels/{channel_id}/messages?limit={limit}',
        headers=headers, timeout=15
    )
    if r.status_code == 200:
        return r.json()
    log(f'Discord API error {r.status_code} for channel {channel_id}')
    return []

def is_signal(content, author):
    """Returns True if this message looks like an actionable call."""
    if not content:
        return False
    c_lower = content.lower()
    a_lower = author.lower()

    # Always flag known alpha authors
    if a_lower in ALPHA_AUTHORS:
        # Check for any signal keyword
        for kw in SIGNAL_KEYWORDS:
            if kw in c_lower:
                return True
        # Even without keywords, flag if it mentions a team/sport
        if any(word in c_lower for word in ['wildcats', 'clippers', 'vitality', 'spain',
                                              'duke', 'michigan', 'tennessee', 'uconn',
                                              'celtics', 'thunder', 'lakers', 'knicks']):
            return True

    # For other authors, require stronger signal
    strong_keywords = ['buying', 'bet', 'taking', 'going', 'polymarket', 'yes', 'no ml',
                       'moneyline', 'conviction']
    matches = sum(1 for kw in strong_keywords if kw in c_lower)
    return matches >= 2

def format_alert(channel_name, author, content, ts, msg_id):
    """Format a Telegram alert for a new call."""
    # Parse timestamp
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        time_str = dt.strftime('%b %d %H:%M UTC')
    except:
        time_str = ts[:16]

    return (
        f'<b>📡 Discord Signal — #{channel_name}</b>\n'
        f'<b>{author}</b> · {time_str}\n\n'
        f'{content[:800]}\n\n'
        f'<i>Message ID: {msg_id}</i>'
    )

def main():
    log('=== Discord Monitor Starting ===')

    if not DISCORD_TOKEN:
        log('ERROR: DISCORD_TOKEN not set in .env')
        return

    state    = load_state()
    new_calls = 0

    for channel_name, channel_id in CHANNELS.items():
        log(f'Checking #{channel_name}...')
        messages = fetch_messages(channel_id)
        if not messages:
            continue

        last_seen_id = state['last_seen'].get(channel_id, '0')
        new_last_id  = last_seen_id
        new_in_channel = []

        for msg in messages:
            msg_id  = msg.get('id', '0')
            content = msg.get('content', '')
            author  = msg.get('author', {}).get('username', 'unknown')
            ts      = msg.get('timestamp', '')

            # Discord snowflake IDs are time-ordered — higher = newer
            if msg_id <= last_seen_id:
                continue

            # Track newest ID seen
            if msg_id > new_last_id:
                new_last_id = msg_id

            # Skip bot messages and empty content
            if msg.get('author', {}).get('bot', False):
                continue
            if not content.strip():
                continue

            if is_signal(content, author):
                new_in_channel.append((author, content, ts, msg_id))

        # Send alerts (oldest first)
        for author, content, ts, msg_id in reversed(new_in_channel):
            alert = format_alert(channel_name, author, content, ts, msg_id)
            tg(alert)
            log(f'  ALERT: [{author}] {content[:80]}')
            new_calls += 1

        # Update last seen
        if new_last_id > last_seen_id:
            state['last_seen'][channel_id] = new_last_id
            log(f'  Updated last_seen to {new_last_id}')
        else:
            log(f'  No new messages')

    save_state(state)
    log(f'=== Done. {new_calls} new call(s) found ===')

if __name__ == '__main__':
    main()
