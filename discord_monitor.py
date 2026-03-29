"""
discord_monitor.py
==================
Monitors DeityFNF Discord for dkxbt trade calls.

Priority: dkxbt in #predictions (proven caller, unknown win rate of others)
Secondary: dkxbt posts in any watched channel

Every run:
  1. Fetch last 20 messages from #predictions + #member-calls
  2. Only alert on dkxbt messages (all others skipped)
  3. Skip messages already seen (tracked by message ID)
  4. Send Telegram alert for each new dkxbt call

State: /opt/polymarket-agent/discord_state.json
"""
import os, sys, json, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '')
TG_TOKEN      = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT       = os.environ.get('TELEGRAM_CHAT_ID', '')

STATE_FILE = '/opt/polymarket-agent/discord_state.json'

# Only these channels, only dkxbt
CHANNELS = {
    "predictions":  "1445706612961447996",   # dkxbt's primary call channel
    "member-calls": "1425216163914453164",   # dkxbt also posts here
}

# The one caller we trust
PRIMARY_CALLER = "dkxbt"

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={
                    'chat_id': TG_CHAT,
                    'text': msg[:4000],
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': True
                },
                timeout=10
            )
        except:
            pass

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
    log(f'Discord API {r.status_code} on channel {channel_id}')
    return []

def format_alert(channel_name, content, ts, attachments=None):
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        time_str = dt.strftime('%b %d %H:%M UTC')
    except:
        time_str = ts[:16]

    text = (
        f'<b>📡 dkxbt call — #{channel_name}</b>\n'
        f'{time_str}\n\n'
        f'{content}'
    )

    # Attach any image URLs
    if attachments:
        for a in attachments:
            url = a.get('url', '')
            if url:
                text += f'\n{url}'

    return text

def main():
    log('=== Discord Monitor (dkxbt only) Starting ===')

    if not DISCORD_TOKEN:
        log('ERROR: DISCORD_TOKEN not set')
        return

    state     = load_state()
    new_calls = 0

    for channel_name, channel_id in CHANNELS.items():
        log(f'Checking #{channel_name}...')
        messages = fetch_messages(channel_id, limit=20)
        if not messages:
            continue

        last_seen_id = state['last_seen'].get(channel_id, '0')
        new_last_id  = last_seen_id

        new_msgs = []
        for msg in messages:
            msg_id  = msg.get('id', '0')
            author  = msg.get('author', {}).get('username', '').lower()
            content = msg.get('content', '').strip()

            # Track newest ID regardless
            if msg_id > new_last_id:
                new_last_id = msg_id

            # Skip already-seen
            if msg_id <= last_seen_id:
                continue

            # Skip bots
            if msg.get('author', {}).get('bot', False):
                continue

            # dkxbt ONLY
            if author != PRIMARY_CALLER:
                continue

            # Skip empty messages (just reactions/embeds with no text)
            if not content:
                continue

            attachments = msg.get('attachments', [])
            ts = msg.get('timestamp', '')
            new_msgs.append((content, ts, attachments, msg_id))

        # Alert oldest first
        for content, ts, attachments, msg_id in reversed(new_msgs):
            alert = format_alert(channel_name, content, ts, attachments)
            tg(alert)
            log(f'  ALERT: {content[:100]}')
            new_calls += 1

        if new_last_id > last_seen_id:
            state['last_seen'][channel_id] = new_last_id

        if not new_msgs:
            log(f'  No new dkxbt messages')

    save_state(state)
    log(f'=== Done. {new_calls} new dkxbt call(s) ===')

if __name__ == '__main__':
    main()
