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

# Channels to monitor
CHANNELS_DKXBT = {
    "predictions":  "1445706612961447996",   # dkxbt's primary call channel
    "member-calls": "1425216163914453164",   # dkxbt also posts here
}

CHANNELS_ALERTS = {
    "fresh-wallets": "1466839042908881109",  # Onsight Alerts: fresh wallets $10k+
    "whales":        "1466838481459478610",  # Onsight Alerts: ranked whale trades
    "sports":        "1466839276737138761",  # sports picks
    "sports-fresh-wallet": "1466839152095133749",  # sports fresh wallet alerts
}

# The one human caller we trust
PRIMARY_CALLER = "dkxbt"

# Bot that posts structured embeds
FRESH_WALLET_BOT  = "onsight alerts"  # lowercase for comparison
MIN_FRESH_WALLET_SIZE = 5000          # Alert on fresh wallet trades >= $5k
MIN_WHALE_SIZE        = 10000         # Alert on whale trades >= $10k
MIN_WHALE_PNL_RANK    = 2000          # Only alert if trader rank <= 2000 (proven track record)

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
    if r.status_code in (401, 403):
        # Token expired or invalid — alert user with refresh instructions
        tg(
            '<b>⚠️ Discord token expired</b>\n'
            'The dkxbt call monitor has stopped working.\n\n'
            '<b>To refresh the token:</b>\n'
            '1. Open discord.com in Chrome\n'
            '2. Press F12 to open DevTools\n'
            '3. Click the <b>Console</b> tab\n'
            '4. Paste this and press Enter:\n'
            '<code>window.webpackChunkdiscord_app.push([[Math.random()],{},e=>{Object.keys(e.c).forEach(k=>{const m=e.c[k]?.exports;if(m?.default?.getToken)console.log(m.default.getToken())})}])</code>\n'
            '5. Copy the token that appears\n'
            '6. Send it to Computer to update'
        )
        log(f'TOKEN EXPIRED (HTTP {r.status_code}) — Telegram alert sent')
        return None  # None signals token failure to caller
    log(f'Discord API {r.status_code} on channel {channel_id}')
    return []

def format_dkxbt_alert(channel_name, content, ts, attachments=None):
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        time_str = dt.strftime('%b %d %H:%M UTC')
    except:
        time_str = ts[:16]

    text = (
        f'<b>📡 dkxbt — #{channel_name}</b>\n'
        f'{time_str}\n\n'
        f'{content}'
    )
    if attachments:
        for a in attachments:
            url = a.get('url', '')
            if url:
                text += f'\n{url}'
    return text

def parse_fresh_wallet_embed(embed):
    """
    Parse Onsight Alerts embed into structured dict.
    Returns None if too small or unparseable.
    """
    desc  = embed.get('description', '')
    fields = {f['name']: f['value'] for f in embed.get('fields', [])}
    title = embed.get('title', '')

    # Extract size from description
    import re
    size_match = re.search(r'Transaction Value: \$([\d,]+(?:\.\d+)?)', desc)
    size = float(size_match.group(1).replace(',', '')) if size_match else 0

    if size < MIN_FRESH_WALLET_SIZE:
        return None

    # Extract direction + market
    bought_match = re.search(r'\*\*Bought "(.+?)"\*\* in (.+)', desc)
    direction = bought_match.group(1) if bought_match else ''
    market    = bought_match.group(2).split('\n')[0].strip() if bought_match else ''

    # Extract price
    price_match = re.search(r'Price: ([\d.]+)%', desc)
    price = float(price_match.group(1)) / 100 if price_match else 0

    # Extract wallet link
    wallet_match = re.search(r'\(https://polymarket.com/profile/(0x[a-fA-F0-9]+)\)', desc)
    wallet = wallet_match.group(1) if wallet_match else ''

    return {
        'size': size, 'direction': direction, 'market': market,
        'price': price, 'wallet': wallet, 'title': title
    }

def parse_whale_embed(embed):
    """
    Parse Onsight Alerts whale embed.
    Returns dict or None if below size/rank thresholds.
    """
    import re
    desc  = embed.get('description', '')
    title = embed.get('title', '')

    # Extract PnL and rank
    pnl_match   = re.search(r'\*\*\+\$([\d,]+)', desc)
    rank_match  = re.search(r'Rank #(\d+)', desc)
    pnl  = float(pnl_match.group(1).replace(',', '')) if pnl_match else 0
    rank = int(rank_match.group(1)) if rank_match else 99999

    if rank > MIN_WHALE_PNL_RANK:
        return None

    # Extract trade details (same format as fresh wallets)
    size_match   = re.search(r'Transaction Value: \$([\d,]+(?:\.\d+)?)', desc)
    bought_match = re.search(r'\*\*Bought "(.+?)"\*\* in (.+)', desc)
    price_match  = re.search(r'Price: ([\d.]+)%', desc)
    link_match   = re.search(r'\[Polymarket Alert\]\((https://polymarket\.com/market/[^)]+)\)', desc)
    profile_match = re.search(r'\[Rank #\d+\]\((https://polymarket\.com/@[^)]+)\)', desc)

    size      = float(size_match.group(1).replace(',', '')) if size_match else 0
    direction = bought_match.group(1) if bought_match else ''
    market    = bought_match.group(2).split('\n')[0].strip() if bought_match else ''
    price     = float(price_match.group(1)) / 100 if price_match else 0
    link      = link_match.group(1) if link_match else ''
    profile   = profile_match.group(1) if profile_match else ''

    if size < MIN_WHALE_SIZE:
        return None

    return {
        'pnl': pnl, 'rank': rank, 'size': size,
        'direction': direction, 'market': market,
        'price': price, 'link': link, 'profile': profile
    }

def format_whale_alert(channel_name, parsed, ts):
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        time_str = dt.strftime('%b %d %H:%M UTC')
    except:
        time_str = ts[:16]

    pnl_str = f'${parsed["pnl"]:,.0f}' if parsed['pnl'] else 'unknown'
    link_part = f'\n<a href="{parsed["link"]}">View market</a>' if parsed.get('link') else ''
    profile_part = f' · <a href="{parsed["profile"]}">Profile</a>' if parsed.get('profile') else ''

    return (
        f'<b>🐋 Whale Alert — #{channel_name}</b>\n'
        f'{time_str}\n\n'
        f'<b>Rank #{parsed["rank"]}</b> ({pnl_str} PnL){profile_part}\n\n'
        f'<b>Market:</b> {parsed["market"]}\n'
        f'<b>Direction:</b> {parsed["direction"]}\n'
        f'<b>Size:</b> ${parsed["size"]:,.0f} @ {parsed["price"]:.1%}'
        f'{link_part}'
    )

def format_fresh_wallet_alert(channel_name, parsed, ts):
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        time_str = dt.strftime('%b %d %H:%M UTC')
    except:
        time_str = ts[:16]

    direction_emoji = '🟢' if parsed['direction'].lower() in ('yes', 'no') else '📊'
    return (
        f'<b>🆕 Fresh Wallet — #{channel_name}</b>\n'
        f'{time_str}\n\n'
        f'<b>Market:</b> {parsed["market"]}\n'
        f'<b>Direction:</b> {direction_emoji} {parsed["direction"]}\n'
        f'<b>Size:</b> ${parsed["size"]:,.0f} @ {parsed["price"]:.1%}\n'
        f'<b>Wallet:</b> <a href="https://polymarket.com/profile/{parsed["wallet"]}">View on Polymarket</a>'
    )

def main():
    log('=== Discord Monitor Starting (dkxbt + fresh wallets) ===')

    if not DISCORD_TOKEN:
        log('ERROR: DISCORD_TOKEN not set')
        return

    state     = load_state()
    new_calls = 0

    # ── 1. dkxbt channels ─────────────────────────────────────────────
    for channel_name, channel_id in CHANNELS_DKXBT.items():
        log(f'Checking #{channel_name} (dkxbt)...')
        messages = fetch_messages(channel_id, limit=20)
        if messages is None:
            log('Token expired — aborting run')
            save_state(state)
            return
        if not messages:
            continue

        last_seen_id = state['last_seen'].get(channel_id, '0')
        new_last_id  = last_seen_id
        new_msgs     = []

        for msg in messages:
            msg_id = msg.get('id', '0')
            author = msg.get('author', {}).get('username', '').lower()
            content = msg.get('content', '').strip()

            if msg_id > new_last_id:
                new_last_id = msg_id
            if msg_id <= last_seen_id:
                continue
            if msg.get('author', {}).get('bot', False):
                continue
            if author != PRIMARY_CALLER:
                continue
            if not content:
                continue

            new_msgs.append((content, msg.get('timestamp', ''), msg.get('attachments', []), msg_id))

        for content, ts, attachments, _ in reversed(new_msgs):
            alert = format_dkxbt_alert(channel_name, content, ts, attachments)
            tg(alert)
            log(f'  dkxbt ALERT: {content[:80]}')
            new_calls += 1

        if new_last_id > last_seen_id:
            state['last_seen'][channel_id] = new_last_id
        if not new_msgs:
            log(f'  No new dkxbt messages')

    # ── 2. Fresh wallet alert channels ───────────────────────────────
    for channel_name, channel_id in CHANNELS_ALERTS.items():
        log(f'Checking #{channel_name} (alerts)...')
        messages = fetch_messages(channel_id, limit=10)
        if messages is None:
            save_state(state)
            return
        if not messages:
            continue

        last_seen_id = state['last_seen'].get(channel_id, '0')
        new_last_id  = last_seen_id
        new_alerts   = []

        for msg in messages:
            msg_id = msg.get('id', '0')
            author = msg.get('author', {}).get('username', '').lower()
            embeds = msg.get('embeds', [])
            content = msg.get('content', '').strip()

            if msg_id > new_last_id:
                new_last_id = msg_id
            if msg_id <= last_seen_id:
                continue

            ts = msg.get('timestamp', '')

            # Handle Onsight Alerts bot embeds
            if author == FRESH_WALLET_BOT and embeds:
                for embed in embeds:
                    if channel_name == 'whales':
                        parsed = parse_whale_embed(embed)
                        kind   = 'whale'
                    else:
                        parsed = parse_fresh_wallet_embed(embed)
                        kind   = 'fresh'
                    if parsed:
                        new_alerts.append((kind, parsed, ts, msg_id))

            # Handle plain text messages from any author (sports channel etc)
            elif content and not msg.get('author', {}).get('bot', False):
                # Only dkxbt text posts matter in non-fresh-wallet channels
                if author == PRIMARY_CALLER:
                    new_alerts.append(('text', content, ts, msg_id))

        for kind, data, ts, _ in reversed(new_alerts):
            if kind == 'whale':
                alert = format_whale_alert(channel_name, data, ts)
                log(f'  Whale ALERT: Rank#{data["rank"]} {data["market"][:40]} {data["direction"]} ${data["size"]:,.0f}')
            elif kind == 'fresh':
                alert = format_fresh_wallet_alert(channel_name, data, ts)
                log(f'  Fresh wallet ALERT: {data["market"][:50]} {data["direction"]} ${data["size"]:,.0f}')
            else:
                alert = format_dkxbt_alert(channel_name, data, ts)
                log(f'  dkxbt ALERT in #{channel_name}: {data[:60]}')
            tg(alert)
            new_calls += 1

        if new_last_id > last_seen_id:
            state['last_seen'][channel_id] = new_last_id
        if not new_alerts:
            log(f'  No new alerts')

    save_state(state)
    log(f'=== Done. {new_calls} new alert(s) ===')

if __name__ == '__main__':
    main()
