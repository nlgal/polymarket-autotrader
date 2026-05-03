import requests, time, json, hmac, hashlib, re, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

FUNDER = '0xc2c1892653C175113c65961C7F4227c18D09b52a'
TOKEN  = '8619264163:AAFB8t_tAcUHcZxMH-O10OeAorcIh9Nk0tc'
CHAT   = '1613579523'
SERVER = 'http://167.71.68.143:8888'
SECRET = 'REDACTED_SECRET'


# ─── Helpers ────────────────────────────────────────────────────────────────

def send_tg(msg):
    for i in range(0, len(msg), 4090):
        requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            json={'chat_id': CHAT, 'text': msg[i:i+4090]}, timeout=10)


def exec_server(cmd, **kw):
    payload = {'command': cmd, **kw}
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(f'{SERVER}/exec', data=body,
        headers={'Content-Type': 'application/json', 'X-Signature': sig}, timeout=120)
    return r.json()


def pnl_icon(val):
    if val > 0.50:  return '🟢'
    if val < -0.50: return '🔴'
    return '➖'


def fmt_pnl(val):
    sign = '+' if val >= 0 else ''
    return f'{pnl_icon(val)} {sign}${val:.2f}'


def fmt_pct(val):
    sign = '+' if val >= 0 else ''
    return f'{pnl_icon(val)} {sign}{val:.2f}%'


def est_fee(title, usdc):
    t = title.lower()
    if any(k in t for k in ['iran','israel','ukraine','china','taiwan','hungary','peru',
                              'invade','ceasefire','diplomatic','regime','peace']):
        return 0.0
    elif any(k in t for k in ['nba','nfl','nhl','mlb','soccer','vs.','pistons','cavaliers',
                               'magic','spurs','lakers','yankee','red sox','boston']):
        return usdc * 0.0075
    return usdc * 0.01


_CAL_RE = re.compile(
    r'(extended by|by (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|ceasefire.*by|deal.*by)',
    re.I
)
_SPORTS_KW = [
    'nba','nfl','mlb','nhl','vs.','fc ','soccer','ufc','cavaliers','pistons','lakers',
    'celtics','yankees','red sox','dodgers','76ers','pacers','spurs','nuggets','warriors',
    'thunder','maverick','knicks','bucks','nets','bulls','hawks','hornets','pelicans',
    'grizzlies','timberwolves','blazers','raptors','arsenal','manchester'
]
_GEO_KW = [
    'iran','ukraine','russia','china','taiwan','israel','hungary','peru','pakistan',
    'ceasefire','invasion','nuclear','peace','regime','diplomatic'
]


def classify_bucket(title):
    t = title.lower()
    if _CAL_RE.search(t):               return 'CALENDAR'
    if any(k in t for k in _SPORTS_KW): return 'SPORTS'
    if any(k in t for k in _GEO_KW):    return 'GEO'
    return 'GEO'


def fetch_all_activity(limit=500):
    all_acts = []
    offset = 0
    while len(all_acts) < limit:
        r = requests.get(
            f'https://data-api.polymarket.com/activity?user={FUNDER}&limit=100&offset={offset}',
            timeout=15
        )
        if not r.ok: break
        page = r.json()
        if not page: break
        all_acts.extend(page)
        if len(page) < 100: break
        offset += 100
        time.sleep(0.15)
    return all_acts


def compute_closed_positions(all_acts):
    """Group by (title, outcome), compute P&L for closed positions.
    Returns (closed_list, open_list)."""
    market_acts = defaultdict(list)
    for a in all_acts:
        if a.get('type') in ('TRADE', 'REDEEM'):
            key = (a.get('title', ''), a.get('outcome', ''))
            market_acts[key].append(a)

    closed, open_pos = [], []
    for (title, outcome), acts in market_acts.items():
        if not title: continue
        buys    = [a for a in acts if a.get('type') == 'TRADE' and a.get('side') == 'BUY']
        sells   = [a for a in acts if a.get('type') == 'TRADE' and a.get('side') == 'SELL']
        redeems = [a for a in acts if a.get('type') == 'REDEEM']

        total_cost     = sum(float(a.get('usdcSize', 0)) for a in buys)
        total_proceeds = sum(float(a.get('usdcSize', 0)) for a in sells + redeems)
        shares_in  = sum(float(a.get('size', 0)) for a in buys)
        shares_out = sum(float(a.get('size', 0)) for a in sells + redeems)

        if total_cost < 1: continue

        coverage     = shares_out / shares_in if shares_in > 0 else 0
        is_redeemed  = len(redeems) > 0
        pnl          = total_proceeds - total_cost
        bucket       = classify_bucket(title)

        rec = {
            'title': title, 'outcome': outcome, 'bucket': bucket,
            'pnl': round(pnl, 2),
            'total_cost': round(total_cost, 2),
            'total_proceeds': round(total_proceeds, 2),
            'is_redeemed': is_redeemed,
        }
        if is_redeemed or coverage >= 0.85:
            closed.append(rec)
        else:
            rec['unrealized_pnl'] = 0
            open_pos.append(rec)
    return closed, open_pos


# ─── Helper: fetch open positions from API ──────────────────────────────────

def fetch_open_positions():
    """Return list of position dicts from the positions API."""
    try:
        r = requests.get(
            f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50',
            timeout=15
        )
        if r.ok:
            return r.json() or []
    except Exception:
        pass
    return []


def _biggest_cluster(positions):
    """Return (cluster_name, cluster_value) for the most common theme keyword."""
    theme_words = [
        'iran','ukraine','russia','china','taiwan','israel','ceasefire','peace',
        'nba','nfl','mlb','nhl','lakers','celtics','yankees',
        'trump','biden','election','fed','rate',
    ]
    counts = defaultdict(float)
    for p in positions:
        title = (p.get('title') or p.get('market', '')).lower()
        val   = float(p.get('currentValue') or p.get('value') or 0)
        for w in theme_words:
            if w in title:
                counts[w] += val
    if not counts:
        return ('N/A', 0.0)
    best = max(counts, key=lambda k: counts[k])
    return (best.upper(), counts[best])


def _days_to_expiry(pos):
    """Return days until end-date, or None if unavailable."""
    end = pos.get('endDate') or pos.get('end_date_iso')
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        delta = dt - datetime.now(tz=timezone.utc)
        return delta.days
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FUNCTION 1: daily_report
# ═══════════════════════════════════════════════════════════════════════════

def daily_report():
    now_utc   = datetime.now(tz=timezone.utc)
    yesterday = now_utc - timedelta(days=1)
    date_str  = yesterday.strftime('%Y-%m-%d')

    # Window: yesterday midnight → today midnight
    day_start = int(datetime(yesterday.year, yesterday.month, yesterday.day,
                             tzinfo=timezone.utc).timestamp())
    day_end   = day_start + 86400

    # ── 1. Fetch yesterday's activity ────────────────────────────────────
    all_acts   = fetch_all_activity(limit=1000)
    day_acts   = [
        a for a in all_acts
        if day_start <= int(a.get('timestamp', 0)) < day_end
    ]

    buys    = [a for a in day_acts if a.get('type') == 'TRADE' and a.get('side') == 'BUY']
    sells   = [a for a in day_acts if a.get('type') == 'TRADE' and a.get('side') == 'SELL']
    redeems = [a for a in day_acts if a.get('type') == 'REDEEM']

    deployed  = sum(float(a.get('usdcSize', 0)) for a in buys)
    returned  = sum(float(a.get('usdcSize', 0)) for a in sells + redeems)
    realized_pnl = returned - deployed
    total_costs  = sum(est_fee(a.get('title', ''), float(a.get('usdcSize', 0))) for a in buys)
    net_pnl      = realized_pnl - total_costs

    # ── 2. Fetch open positions ───────────────────────────────────────────
    open_positions = fetch_open_positions()
    active_pos     = [p for p in open_positions if float(p.get('currentValue') or 0) > 0.50]

    open_cost_basis = sum(float(p.get('initialValue') or p.get('value') or 0) for p in active_pos)
    unrealized_pnl  = sum(
        float(p.get('currentValue') or 0) - float(p.get('initialValue') or p.get('value') or 0)
        for p in active_pos
    )

    geo_exposure    = sum(
        float(p.get('currentValue') or 0) for p in active_pos
        if classify_bucket(p.get('title') or p.get('market', '')) == 'GEO'
    )
    sports_exposure = sum(
        float(p.get('currentValue') or 0) for p in active_pos
        if classify_bucket(p.get('title') or p.get('market', '')) == 'SPORTS'
    )
    cal_exposure    = sum(
        float(p.get('currentValue') or 0) for p in active_pos
        if classify_bucket(p.get('title') or p.get('market', '')) == 'CALENDAR'
    )

    biggest_cluster_name, biggest_cluster_val = _biggest_cluster(active_pos)

    # ── 3. LP quoter / server ─────────────────────────────────────────────
    clob_cash    = 0.0
    n_lp_orders  = 0
    try:
        lp_result = exec_server('python lp_quoter.py --report')
        clob_cash   = float(lp_result.get('cash', 0))
        n_lp_orders = int(lp_result.get('orders', 0))
    except Exception:
        pass

    # ── 4. Tail log ───────────────────────────────────────────────────────
    log_lines      = []
    checklist_lines = []
    skip_lines     = []
    hot_lines      = []
    try:
        log_result  = exec_server('tail_log', lines=500)
        raw_log     = log_result.get('output', '') or ''
        for line in raw_log.splitlines():
            if date_str in line or yesterday.strftime('%Y/%m/%d') in line:
                log_lines.append(line)
            if 'CHECKLIST' in line:
                checklist_lines.append(line)
            if 'T3-SKIP' in line or 'SKIP' in line:
                skip_lines.append(line)
            if 'VERY_HOT' in line or 'HOT' in line:
                hot_lines.append(line)
    except Exception:
        pass

    # ── 5. Build top-5 open risks ─────────────────────────────────────────
    top5_risk = sorted(
        active_pos,
        key=lambda p: float(p.get('initialValue') or p.get('value') or 0),
        reverse=True
    )[:5]

    # ── 6. Top-10 open positions by currentValue ──────────────────────────
    top10_pos = sorted(
        active_pos,
        key=lambda p: float(p.get('currentValue') or 0),
        reverse=True
    )[:10]

    # ── 7. Risk notes helpers ─────────────────────────────────────────────
    biggest_winner = max(
        active_pos,
        key=lambda p: float(p.get('currentValue') or 0) - float(p.get('initialValue') or p.get('value') or 0),
        default=None
    )
    biggest_loser = min(
        active_pos,
        key=lambda p: float(p.get('currentValue') or 0) - float(p.get('initialValue') or p.get('value') or 0),
        default=None
    )
    largest_by_cost = max(
        active_pos,
        key=lambda p: float(p.get('initialValue') or p.get('value') or 0),
        default=None
    )
    near_expiry  = [p for p in active_pos if (_days_to_expiry(p) or 999) < 3]
    redeemable   = [p for p in active_pos if float(p.get('currentValue') or 0) > float(p.get('initialValue') or p.get('value') or 1) * 0.9]

    # ── 8. DO NOT TRADE logic ─────────────────────────────────────────────
    headline_trades = [
        a for a in buys
        if not any(k in (a.get('title', '') or '').lower()
                   for k in _SPORTS_KW + _GEO_KW)
    ]
    concentration_flags = []
    total_open = geo_exposure + sports_exposure + cal_exposure or 1
    for bucket_name, bucket_val in [('GEO', geo_exposure), ('SPORTS', sports_exposure), ('CALENDAR', cal_exposure)]:
        if bucket_val / total_open > 0.25:
            concentration_flags.append(f'{bucket_name} ({bucket_val/total_open*100:.0f}% of open risk)')

    # ── Assemble message ──────────────────────────────────────────────────
    lines = []

    # S1
    lines.append(f'📊 DAILY REPORT — {date_str}')
    lines.append('')

    # S2
    lines.append('🧾 SUMMARY')
    lines.append(f'  Trades: {len(day_acts)} ({len(buys)}B / {len(sells)}S / {len(redeems)}R)')
    lines.append(f'  Deployed: ${deployed:.2f}  Returned: ${returned:.2f}')
    lines.append(f'  Realized P/L: {fmt_pnl(realized_pnl)}')
    lines.append(f'  Estimated costs: ${total_costs:.2f}')
    lines.append(f'  Net P/L after costs: {fmt_pnl(net_pnl)}')
    lines.append('')

    # S3
    lines.append('💼 OPEN EXPOSURE')
    lines.append(f'  Total open positions: {len(active_pos)} | Cost basis: ${open_cost_basis:.0f}')
    lines.append(f'  Worst-case loss (all expire worthless): -${open_cost_basis:.0f}')
    lines.append(f'  Unrealized P/L: {fmt_pnl(unrealized_pnl)}')
    lines.append(f'  By category: GEO ${geo_exposure:.0f} | SPORTS ${sports_exposure:.0f} | CALENDAR ${cal_exposure:.0f}')
    lines.append(f'  ⚠️ Biggest risk cluster: {biggest_cluster_name} ${biggest_cluster_val:.0f}')
    lines.append('')

    # S4
    lines.append('🚨 TOP 5 OPEN RISKS (by cost basis)')
    for i, p in enumerate(top5_risk, 1):
        title   = (p.get('title') or p.get('market') or 'Unknown')[:60]
        outcome = p.get('outcome') or p.get('side') or '?'
        cost    = float(p.get('initialValue') or p.get('value') or 0)
        cur     = float(p.get('currentValue') or 0)
        upnl    = cur - cost
        lines.append(f'  {i}. {title} [{outcome}]')
        lines.append(f'     Cost: ${cost:.2f} | Current: ${cur:.2f} | uPNL: {fmt_pnl(upnl)}')
    lines.append('')

    # S5
    lines.append('🟢 BOUGHT')
    if buys:
        for b in buys:
            title  = (b.get('title') or 'Unknown')[:55]
            shares = float(b.get('size') or 0)
            usdc   = float(b.get('usdcSize') or 0)
            price  = usdc / shares if shares else 0
            fee    = est_fee(b.get('title', ''), usdc)
            ts     = datetime.fromtimestamp(int(b.get('timestamp', 0)), tz=timezone.utc).strftime('%H:%M')
            lines.append(f'  • {title}')
            lines.append(f'    {shares:.0f} sh @ ${price:.3f} | Cost: ${usdc:.2f} | Fee: ${fee:.2f} | {ts}')
    else:
        lines.append('  None')
    lines.append('')

    # S6
    lines.append('🔴 SOLD')
    if sells:
        for s in sells:
            title   = (s.get('title') or 'Unknown')[:55]
            shares  = float(s.get('size') or 0)
            usdc    = float(s.get('usdcSize') or 0)
            price   = usdc / shares if shares else 0
            ts      = datetime.fromtimestamp(int(s.get('timestamp', 0)), tz=timezone.utc).strftime('%H:%M')
            # best-effort P&L: not reliable without matching buys, so label as proceeds only
            lines.append(f'  • {title}')
            lines.append(f'    {shares:.0f} sh @ ${price:.3f} | Proceeds: ${usdc:.2f} | {ts}')
    else:
        lines.append('  None')
    lines.append('')

    # S7
    lines.append('📌 OPEN POSITIONS (top 10 by value)')
    if top10_pos:
        for p in top10_pos:
            title   = (p.get('title') or p.get('market') or 'Unknown')[:55]
            outcome = p.get('outcome') or p.get('side') or '?'
            cost    = float(p.get('initialValue') or p.get('value') or 0)
            cur     = float(p.get('currentValue') or 0)
            entry_p = float(p.get('avgPrice') or p.get('entryPrice') or (cost / max(float(p.get('size') or 1), 1)))
            cur_p   = float(p.get('curPrice') or p.get('currentPrice') or 0)
            upnl    = cur - cost
            dte     = _days_to_expiry(p)
            flags   = []
            if dte is not None and dte < 3: flags.append(f'⚠️ {dte}d left')
            if cur_p and cur_p < 0.05:      flags.append('⚠️ LOW PROB')
            flag_str = '  ' + ' '.join(flags) if flags else ''
            lines.append(f'  • {title} [{outcome}]{flag_str}')
            lines.append(f'    Entry: ${entry_p:.3f} | Cur: ${cur_p:.3f} | Cost: ${cost:.2f} | uPNL: {fmt_pnl(upnl)}')
    else:
        lines.append('  No active positions')
    lines.append('')

    # S8
    lines.append(f'📋 LP ORDERS: {n_lp_orders} orders | Cash: ${clob_cash:.2f}')
    lines.append('')

    # S9
    lines.append('🏷️ SIGNAL SOURCES TODAY')
    if checklist_lines:
        for cl in checklist_lines[-10:]:
            lines.append(f'  {cl.strip()}')
    else:
        lines.append('  Signal tagging not available for today')
    lines.append('')

    # S10
    lines.append('🎯 CLOSING-LINE VALUE')
    lines.append('  Not available for same-day report — review in weekly')
    lines.append('')

    # S11
    lines.append('⏭ SKIPPED TODAY')
    if skip_lines:
        for sl in skip_lines[-10:]:
            lines.append(f'  {sl.strip()}')
    else:
        lines.append('  No T3-SKIP lines found in log')
    lines.append('')

    # S12
    lines.append('⚠️ RISK NOTES')
    if biggest_winner:
        wt  = (biggest_winner.get('title') or biggest_winner.get('market') or 'Unknown')[:50]
        wv  = float(biggest_winner.get('currentValue') or 0) - float(biggest_winner.get('initialValue') or biggest_winner.get('value') or 0)
        lines.append(f'  Biggest winner (unrealized): {wt} {fmt_pnl(wv)}')
    if biggest_loser:
        lt  = (biggest_loser.get('title') or biggest_loser.get('market') or 'Unknown')[:50]
        lv  = float(biggest_loser.get('currentValue') or 0) - float(biggest_loser.get('initialValue') or biggest_loser.get('value') or 0)
        lines.append(f'  Biggest loser (unrealized): {lt} {fmt_pnl(lv)}')
    if largest_by_cost:
        lct = (largest_by_cost.get('title') or largest_by_cost.get('market') or 'Unknown')[:50]
        lcc = float(largest_by_cost.get('initialValue') or largest_by_cost.get('value') or 0)
        lines.append(f'  Largest position by cost: {lct} (${lcc:.2f})')
    if near_expiry:
        lines.append(f'  ⚠️ Positions near expiry (<3d): {len(near_expiry)}')
        for p in near_expiry:
            nt  = (p.get('title') or p.get('market') or 'Unknown')[:50]
            dte = _days_to_expiry(p)
            lines.append(f'    - {nt} ({dte}d)')
    else:
        lines.append('  No positions near expiry (<3d)')
    if redeemable:
        lines.append(f'  REDEEMABLE: {len(redeemable)} position(s)')
        for p in redeemable[:3]:
            rt = (p.get('title') or p.get('market') or 'Unknown')[:50]
            lines.append(f'    - {rt}')
    lines.append('')

    # S13
    lines.append('🚫 DO NOT TRADE TOMORROW')
    if headline_trades:
        for h in headline_trades[:5]:
            lines.append(f'  ❌ HEADLINE: {(h.get("title") or "Unknown")[:60]}')
    else:
        lines.append('  No HEADLINE trades today')
    if concentration_flags:
        for cf in concentration_flags:
            lines.append(f'  ❌ HIGH CONCENTRATION: {cf}')
    else:
        lines.append('  No concentration kill-switch triggered')
    try:
        ks_result = exec_server('cat kill_switches.json')
        ks_data   = ks_result.get('output', '')
        if ks_data:
            ks = json.loads(ks_data)
            for bucket, status in ks.items():
                if status:
                    lines.append(f'  🚫 KILL SWITCH ACTIVE: {bucket}')
    except Exception:
        pass
    lines.append('')

    # S14
    lines.append('🔭 WATCH TOMORROW')
    if hot_lines:
        for hl in hot_lines[-10:]:
            lines.append(f'  {hl.strip()}')
    else:
        lines.append('  No VERY_HOT/HOT signals in log today')
    lines.append('')

    # S15
    lines.append('💰 COSTS & BURN RATE')
    lines.append(f'  Today: ${total_costs:.2f}')
    lines.append('  Burn rate: $8.84/day (ops $1.70 + UW $7.14)')
    lines.append('  Monthly projection: ~$265')

    send_tg('\n'.join(lines))
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCTION 2: weekly_edge_report
# ═══════════════════════════════════════════════════════════════════════════

def weekly_edge_report():
    now_utc  = datetime.now(tz=timezone.utc)
    date_str = now_utc.strftime('%Y-%m-%d')

    # ── 1. Full history ────────────────────────────────────────────────────
    all_acts = fetch_all_activity(limit=2000)

    # ── 2. Closed / open split ────────────────────────────────────────────
    closed, _ = compute_closed_positions(all_acts)

    # ── 3. Current open positions ─────────────────────────────────────────
    open_positions = fetch_open_positions()
    active_pos     = [p for p in open_positions if float(p.get('currentValue') or 0) > 0.50]

    open_cost_basis = sum(float(p.get('initialValue') or p.get('value') or 0) for p in active_pos)
    unrealized_total = sum(
        float(p.get('currentValue') or 0) - float(p.get('initialValue') or p.get('value') or 0)
        for p in active_pos
    )

    # ── 4. Overall performance stats ──────────────────────────────────────
    wins   = [c for c in closed if c['pnl'] > 0]
    losses = [c for c in closed if c['pnl'] <= 0]
    total_pnl    = sum(c['pnl'] for c in closed)
    total_cost   = sum(c['total_cost'] for c in closed)
    total_wins   = sum(c['pnl'] for c in wins)
    total_losses = abs(sum(c['pnl'] for c in losses))
    win_rate     = len(wins) / len(closed) * 100 if closed else 0
    profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
    avg_win  = total_wins / len(wins)   if wins   else 0
    avg_loss = total_losses / len(losses) if losses else 0
    roi      = total_pnl / total_cost * 100 if total_cost > 0 else 0

    # ── 5. Outlier-adjusted P&L ───────────────────────────────────────────
    sorted_wins = sorted(wins, key=lambda c: c['pnl'], reverse=True)
    top1_pnl = sorted_wins[0]['pnl'] if len(sorted_wins) >= 1 else 0
    top2_pnl = sorted_wins[1]['pnl'] if len(sorted_wins) >= 2 else 0
    top3_pnl = sorted_wins[2]['pnl'] if len(sorted_wins) >= 3 else 0

    ex_top2_wins   = total_wins - top1_pnl - top2_pnl
    ex_top2_losses = total_losses
    adj_pf         = ex_top2_wins / ex_top2_losses if ex_top2_losses > 0 else float('inf')

    # ── 6. By bucket ──────────────────────────────────────────────────────
    bucket_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0,
                                         'total_pnl': 0, 'win_pnl': 0, 'loss_pnl': 0})
    for c in closed:
        b = c['bucket']
        bucket_stats[b]['count']     += 1
        bucket_stats[b]['total_pnl'] += c['pnl']
        if c['pnl'] > 0:
            bucket_stats[b]['wins']    += 1
            bucket_stats[b]['win_pnl'] += c['pnl']
        else:
            bucket_stats[b]['losses']    += 1
            bucket_stats[b]['loss_pnl']  += abs(c['pnl'])

    # ── 7. Signal-source classification (2-class) ─────────────────────────
    def classify_source(rec):
        title = rec['title'].lower()
        if any(k in title for k in _SPORTS_KW):
            return 'DK_ALPHA'
        return 'BOT_INDEPENDENT'

    source_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0,
                                          'total_pnl': 0, 'win_pnl': 0, 'loss_pnl': 0})
    for c in closed:
        src = classify_source(c)
        source_stats[src]['count']     += 1
        source_stats[src]['total_pnl'] += c['pnl']
        if c['pnl'] > 0:
            source_stats[src]['wins']    += 1
            source_stats[src]['win_pnl'] += c['pnl']
        else:
            source_stats[src]['losses']    += 1
            source_stats[src]['loss_pnl']  += abs(c['pnl'])

    dk_total   = source_stats['DK_ALPHA']['total_pnl']
    bot_total  = source_stats['BOT_INDEPENDENT']['total_pnl']

    # ── 8. Top wins / losses (all time) ───────────────────────────────────
    all_sorted  = sorted(closed, key=lambda c: c['pnl'], reverse=True)
    top3_wins_all  = all_sorted[:3]
    top3_loss_all  = sorted(closed, key=lambda c: c['pnl'])[:3]

    # This-week window (last 7 days)
    week_start_ts = int((now_utc - timedelta(days=7)).timestamp())
    week_acts     = [a for a in all_acts if int(a.get('timestamp', 0)) >= week_start_ts]
    week_closed, _ = compute_closed_positions(week_acts)
    week_wins_sorted  = sorted(week_closed, key=lambda c: c['pnl'], reverse=True)[:3]
    week_losses_sorted = sorted(week_closed, key=lambda c: c['pnl'])[:3]
    week_pnl = sum(c['pnl'] for c in week_closed)

    # ── 9. P&L concentration ──────────────────────────────────────────────
    total_winning_pnl = sum(c['pnl'] for c in wins) or 1
    top3_pct          = (top1_pnl + top2_pnl + top3_pnl) / total_winning_pnl * 100

    # ── 10. Drawdown by category ──────────────────────────────────────────
    max_loss_by_bucket = {}
    for c in closed:
        b = c['bucket']
        if c['pnl'] < 0:
            if b not in max_loss_by_bucket or c['pnl'] < max_loss_by_bucket[b]:
                max_loss_by_bucket[b] = c['pnl']

    # ── 11. Kill-switch check (last 5 per bucket) ─────────────────────────
    kill_flags = {}
    for b in ['SPORTS', 'GEO', 'CALENDAR']:
        b_trades = [c for c in closed if c['bucket'] == b]
        last5    = sorted(b_trades, key=lambda c: c.get('total_cost', 0))[-5:]
        consec_loss = 0
        for c in reversed(last5):
            if c['pnl'] <= 0:
                consec_loss += 1
            else:
                break
        kill_flags[b] = consec_loss

    # ── 12. Open positions by category ────────────────────────────────────
    geo_open    = sum(float(p.get('currentValue') or 0) for p in active_pos
                      if classify_bucket(p.get('title') or p.get('market', '')) == 'GEO')
    sports_open = sum(float(p.get('currentValue') or 0) for p in active_pos
                      if classify_bucket(p.get('title') or p.get('market', '')) == 'SPORTS')
    cal_open    = sum(float(p.get('currentValue') or 0) for p in active_pos
                      if classify_bucket(p.get('title') or p.get('market', '')) == 'CALENDAR')

    top5_open_risk = sorted(
        active_pos,
        key=lambda p: float(p.get('initialValue') or p.get('value') or 0),
        reverse=True
    )[:5]

    # ── 13. Positions to cut ──────────────────────────────────────────────
    to_cut = []
    for p in active_pos:
        cost = float(p.get('initialValue') or p.get('value') or 0)
        cur  = float(p.get('currentValue') or 0)
        dte  = _days_to_expiry(p)
        cur_p = float(p.get('curPrice') or p.get('currentPrice') or 0)
        reasons = []
        if dte is not None and dte < 3 and cur_p and cur_p < 0.15:
            reasons.append(f'expires in {dte}d, prob={cur_p:.2f}')
        if cost > 0 and (cur - cost) / cost < -0.60:
            reasons.append(f'down {abs((cur-cost)/cost)*100:.0f}%')
        if cost > 200:
            reasons.append(f'concentration risk ${cost:.0f}')
        if reasons:
            to_cut.append((p.get('title') or p.get('market') or 'Unknown', reasons))

    # ── 14. Costs this week ───────────────────────────────────────────────
    week_buys       = [a for a in week_acts if a.get('type') == 'TRADE' and a.get('side') == 'BUY']
    week_costs      = sum(est_fee(a.get('title', ''), float(a.get('usdcSize', 0))) for a in week_buys)
    week_deployed   = sum(float(a.get('usdcSize', 0)) for a in week_buys)

    # ── Assemble report ───────────────────────────────────────────────────
    lines = []

    # S1
    lines.append(f'📊 WEEKLY EDGE REPORT — {date_str}')
    lines.append(f'   Week ending {date_str}')
    lines.append('')

    # S2
    lines.append('🎯 OVERALL PERFORMANCE')
    lines.append(f'  Win rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L / {len(closed)} total)')
    lines.append(f'  Profit factor: {profit_factor:.2f}')
    lines.append(f'  Avg win: ${avg_win:.2f} | Avg loss: ${avg_loss:.2f}')
    lines.append(f'  Net realized P/L: {fmt_pnl(total_pnl)}')
    lines.append(f'  Total cost deployed: ${total_cost:.2f}')
    lines.append(f'  ROI: {fmt_pct(roi)}')
    lines.append('')

    # S3
    lines.append('💼 OPEN POSITION RISK')
    lines.append(f'  Open positions: {len(active_pos)} | Total cost basis: ${open_cost_basis:.2f}')
    lines.append(f'  Worst-case loss (all expire worthless): -${open_cost_basis:.2f}')
    lines.append(f'  Top 5 by cost basis:')
    for i, p in enumerate(top5_open_risk, 1):
        title   = (p.get('title') or p.get('market') or 'Unknown')[:55]
        cost    = float(p.get('initialValue') or p.get('value') or 0)
        cur     = float(p.get('currentValue') or 0)
        upnl    = cur - cost
        lines.append(f'    {i}. {title}')
        lines.append(f'       Cost: ${cost:.2f} | Cur: ${cur:.2f} | uPNL: {fmt_pnl(upnl)}')
    lines.append(f'  Exposure by category: GEO ${geo_open:.0f} | SPORTS ${sports_open:.0f} | CALENDAR ${cal_open:.0f}')
    lines.append('')

    # S4
    lines.append('📊 OUTLIER-ADJUSTED P&L')
    lines.append(f'  Total P&L:              {fmt_pnl(total_pnl)}')
    lines.append(f'  Ex top 1 win:           {fmt_pnl(total_pnl - top1_pnl)}')
    lines.append(f'  Ex top 2 wins:          {fmt_pnl(total_pnl - top1_pnl - top2_pnl)}')
    lines.append(f'  Ex top 3 wins:          {fmt_pnl(total_pnl - top1_pnl - top2_pnl - top3_pnl)}')
    lines.append(f'  Profit factor ex top 2: {adj_pf:.2f}')
    lines.append(f'  Interpretation: BOT_INDEPENDENT profits collapse without top 2 wins — not yet proven repeatable')
    lines.append('')

    # S5
    lines.append('🗂️ BY BUCKET')
    for b in ['SPORTS', 'GEO', 'CALENDAR']:
        s  = bucket_stats[b]
        wr = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
        pf = s['win_pnl'] / s['loss_pnl'] if s['loss_pnl'] > 0 else float('inf')
        aw = s['win_pnl'] / s['wins']   if s['wins']   > 0 else 0
        al = s['loss_pnl'] / s['losses'] if s['losses'] > 0 else 0
        lines.append(f'  {b}: {s["count"]} trades | WR {wr:.1f}% | P&L {fmt_pnl(s["total_pnl"])} | PF {pf:.2f} | Avg W ${aw:.2f} / Avg L ${al:.2f}')
    lines.append('')

    # S6
    lines.append('🏷️ BY SIGNAL SOURCE')
    for src in ['DK_ALPHA', 'BOT_INDEPENDENT']:
        s  = source_stats[src]
        wr = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
        pf = s['win_pnl'] / s['loss_pnl'] if s['loss_pnl'] > 0 else float('inf')
        lines.append(f'  {src}: {s["count"]} trades | WR {wr:.1f}% | P&L {fmt_pnl(s["total_pnl"])} | PF {pf:.2f}')
    lines.append('  (Simplified 2-class split: DK_ALPHA=sports same-day, BOT_INDEPENDENT=all other closed)')
    lines.append('')

    # S7
    lines.append('🔬 EDGE CHARACTER')
    dk_wr  = source_stats['DK_ALPHA']['wins'] / source_stats['DK_ALPHA']['count'] * 100 if source_stats['DK_ALPHA']['count'] > 0 else 0
    dk_pf  = source_stats['DK_ALPHA']['win_pnl'] / source_stats['DK_ALPHA']['loss_pnl'] if source_stats['DK_ALPHA']['loss_pnl'] > 0 else float('inf')
    lines.append(f'  DK_ALPHA:         EXTERNAL_ALPHA ✅  (WR {dk_wr:.1f}%, PF {dk_pf:.2f})')
    lines.append(f'  BOT_INDEPENDENT:  FAT_TAIL_ASYMMETRIC ⚠️  (collapses without top 2 outliers)')
    lines.append(f'  BOT sports:       NOT_PROVEN ❌  (no confirmed standalone positions)')
    lines.append('')

    # S8
    lines.append('📈 TOP 3 WINS')
    lines.append('  This week:')
    for i, c in enumerate(week_wins_sorted, 1):
        lines.append(f'    {i}. {c["title"][:55]} [{c["outcome"]}] {fmt_pnl(c["pnl"])}')
    lines.append('  All time:')
    for i, c in enumerate(top3_wins_all, 1):
        lines.append(f'    {i}. {c["title"][:55]} [{c["outcome"]}] {fmt_pnl(c["pnl"])}')
    lines.append('')

    # S9
    lines.append('📉 TOP 3 LOSSES')
    lines.append('  This week:')
    for i, c in enumerate(week_losses_sorted, 1):
        lines.append(f'    {i}. {c["title"][:55]} [{c["outcome"]}] {fmt_pnl(c["pnl"])}')
    lines.append('  All time:')
    for i, c in enumerate(top3_loss_all, 1):
        lines.append(f'    {i}. {c["title"][:55]} [{c["outcome"]}] {fmt_pnl(c["pnl"])}')
    lines.append('')

    # S10
    lines.append('🎯 P&L CONCENTRATION')
    lines.append(f'  Top 3 wins as % of total winning P&L: {top3_pct:.1f}%')
    if top3_pct > 60:
        lines.append('  ⚠️ HIGH CONCENTRATION — performance driven by outliers')
    elif top3_pct > 40:
        lines.append('  ⚠️ MODERATE CONCENTRATION — some outlier dependency')
    else:
        lines.append('  ✅ DISTRIBUTED — wins spread across positions')
    lines.append('')

    # S11
    lines.append('📉 DRAWDOWN BY CATEGORY')
    for b in ['SPORTS', 'GEO', 'CALENDAR']:
        ml = max_loss_by_bucket.get(b, 0)
        lines.append(f'  {b}: Max single loss {fmt_pnl(ml)}')
    lines.append('')

    # S12
    lines.append('🔄 REPEATABLE vs FAT-TAIL')
    lines.append('  SPORTS:   DK_ALPHA execution provides repeatable edge — volume-dependent.')
    lines.append('  GEO:      Small asymmetric bets with occasional fat-tail payout — not repeatable at scale.')
    lines.append('  CALENDAR: Hard-capped; purely event-driven, no repeatable signal confirmed.')
    lines.append('')

    # S13
    dk_pct  = dk_total  / total_pnl * 100 if total_pnl != 0 else 0
    bot_adj = total_pnl - top1_pnl - top2_pnl
    bot_pct = bot_adj / total_pnl * 100 if total_pnl != 0 else 0
    lines.append('🤖 BOT vs EXTERNAL ALPHA')
    lines.append(f'  DK_ALPHA total P&L:                   {fmt_pnl(dk_total)} ({dk_pct:.1f}% of total)')
    lines.append(f'  BOT_INDEPENDENT adj P&L (ex top 2):   {fmt_pnl(bot_adj)} ({bot_pct:.1f}% of total)')
    lines.append('  Assessment: The bot has not independently generated proven repeatable edge.')
    lines.append('              It executes DK alpha and caught fat-tail geo events.')
    lines.append('')

    # S14
    lines.append('⚠️ KILL SWITCH STATUS')
    for b in ['SPORTS', 'GEO', 'CALENDAR']:
        consec = kill_flags.get(b, 0)
        if consec >= 3:
            lines.append(f'  🚨 {b}: {consec} consecutive losses — KILL SWITCH NEAR TRIGGER')
        elif consec >= 2:
            lines.append(f'  ⚠️  {b}: {consec} consecutive losses — watch closely')
        else:
            lines.append(f'  ✅ {b}: {consec} consecutive losses — OK')
    lines.append('')

    # S15
    lines.append('📏 SIZING RECOMMENDATIONS')
    lines.append('  DK_ALPHA:         Current sizing OK — scale if bot proves execution edge')
    lines.append('  BOT_INDEPENDENT:  Do NOT scale until outlier-adjusted PF proven > 1.5')
    lines.append('  HYBRID:           Reduce to max $50 per trade')
    lines.append('  HEADLINE:         No new trades')
    lines.append('  CALENDAR:         Max $100 per trade, hard stop $200 total exposure')
    lines.append('')

    # S16
    lines.append('🔪 POSITIONS TO CUT')
    if to_cut:
        for title, reasons in to_cut:
            lines.append(f'  • {title[:55]}')
            lines.append(f'    Reason: {"; ".join(reasons)}')
    else:
        lines.append('  No positions flagged for immediate reduction')
    lines.append('')

    # S17
    lines.append('💰 COSTS THIS WEEK')
    lines.append(f'  Deployed: ${week_deployed:.2f}')
    lines.append(f'  Estimated fees: ${week_costs:.2f}')
    lines.append(f'  Ops burn (7d × $8.84): ${7 * 8.84:.2f}')
    lines.append('')

    # S18
    lines.append('✅ WHAT WORKED THIS WEEK')
    if week_wins_sorted:
        for c in week_wins_sorted[:3]:
            lines.append(f'  • {c["title"][:55]} {fmt_pnl(c["pnl"])} [{c["bucket"]}]')
    else:
        lines.append('  No winning trades this week')
    lines.append('')

    # S19
    lines.append('❌ WHAT FAILED THIS WEEK')
    if week_losses_sorted:
        for c in week_losses_sorted[:3]:
            lines.append(f'  • {c["title"][:55]} {fmt_pnl(c["pnl"])} [{c["bucket"]}]')
    else:
        lines.append('  No losing trades this week')
    lines.append('')

    # S20
    lines.append('📌 ONE CHANGE TO MAKE NEXT WEEK')
    if top3_pct > 60:
        lines.append('  Reduce max position size — concentration too high in outlier wins.')
    elif any(kill_flags.get(b, 0) >= 2 for b in ['SPORTS', 'GEO', 'CALENDAR']):
        lines.append('  Review kill-switch buckets before trading — consecutive losses detected.')
    else:
        lines.append('  Improve signal tagging (CHECKLIST logging) to enable CLV measurement.')
    lines.append('')

    # S21
    lines.append('🧠 MODEL SUMMARY')
    lines.append('  Sports  = grind (small edge, volume dependent, DK-sourced)')
    lines.append('  Geo     = small + asymmetric (fat-tail hunts, not scalable)')
    lines.append('  Calendar = hard-cap (event-driven, no repeatable signal confirmed)')
    lines.append('')

    # S22
    lines.append('⚖️ OPEN RISK vs REALIZED P&L')
    lines.append(f'  Realized P&L (all time): {fmt_pnl(total_pnl)}')
    lines.append(f'  Open position unrealized: {fmt_pnl(unrealized_total)}')
    lines.append(f'  Worst-case (all expire):  -{open_cost_basis:.2f}')
    if open_cost_basis > total_pnl * 2 and total_pnl > 0:
        lines.append('  ⚠️ WARNING: Open-position downside could erase all realized gains if positions expire worthless')
    else:
        lines.append('  ✅ Open risk within acceptable range relative to realized gains')
    lines.append('')

    # S23
    lines.append('🤖 BOT CONTRIBUTION THIS WEEK')
    week_bot_closed = [c for c in week_closed if classify_source(c) == 'BOT_INDEPENDENT']
    week_dk_closed  = [c for c in week_closed if classify_source(c) == 'DK_ALPHA']
    week_bot_pnl    = sum(c['pnl'] for c in week_bot_closed)
    week_dk_pnl     = sum(c['pnl'] for c in week_dk_closed)
    lines.append(f'  DK_ALPHA P&L this week:          {fmt_pnl(week_dk_pnl)}')
    lines.append(f'  BOT_INDEPENDENT P&L this week:   {fmt_pnl(week_bot_pnl)}')
    if week_dk_pnl > week_bot_pnl and week_dk_pnl > 0:
        lines.append('  Assessment: External DK signals drove performance. Bot provided execution only.')
    elif week_bot_pnl > 0:
        lines.append('  Assessment: Bot contributed independent P&L this week — monitor for repeatability.')
    else:
        lines.append('  Assessment: Bot did not add independent value this week.')
    lines.append('')

    # S24 — Final score
    if win_rate >= 55 and profit_factor >= 1.5 and not any(kill_flags.get(b, 0) >= 3 for b in ['SPORTS', 'GEO', 'CALENDAR']):
        grade, rationale = 'A', 'Strong win rate, healthy PF, no kill switches triggered.'
    elif win_rate >= 45 and profit_factor >= 1.0:
        grade, rationale = 'B', 'Profitable but concentration risk or low volume limits confidence.'
    elif win_rate >= 35 or total_pnl > 0:
        grade, rationale = 'C', 'Marginally profitable; process needs improvement in signal quality or sizing.'
    elif total_pnl > -50:
        grade, rationale = 'D', 'Small losses — review signal sources before increasing activity.'
    else:
        grade, rationale = 'F', 'Significant losses — pause and audit before resuming.'

    lines.append('🏆 FINAL SCORE')
    lines.append(f'  Grade: {grade}')
    lines.append(f'  {rationale}')

    send_tg('\n'.join(lines))
    return '\n'.join(lines)


# ─── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    now = datetime.now(tz=timezone.utc)
    is_sunday = now.weekday() == 6

    print(f'Running daily_report() for {now.strftime("%Y-%m-%d")} ...')
    daily_report()

    if is_sunday:
        print('Sunday detected — running weekly_edge_report() ...')
        weekly_edge_report()
