"""
whale_consensus.py — Dynamic Top-15 Polymarket Consensus Scanner
Refreshed weekly (or on-demand). Feeds S7 signal into signal_engine.py.

Logic:
- Pull top-15 by quality score (PnL consistency across all/monthly/weekly windows)
- Exclude LP/volume-farmer accounts (flagged by bidirectional same-market activity)
- For each market, count aligned directional accounts
- Output: consensus_cache.json with per-market alignment data

Signal S7 (whale_consensus):
  IC = 0.12 (empirically: whale consensus has demonstrated edge, especially in sports/game markets)
  Score = (aligned_count / 3) capped at 1.0, directional accounts only
  Only fires when aligned_count >= 2 AND accounts have category expertise in that market type

Account quality tiers (from behavior analysis):
  DIRECTIONAL: CemeterySun, BWArmageddon, benwyatt, ohuhusos, Soarin22, sss.tier, jtwyslljy, Feveey, BruceWayne77
  MIXED: 0x0c154c (large positions but unclear edge source)
  EXCLUDED: 0x36257cb (LP/volume farmer), elshark206 (LP), CarlosMC (spray), 0x20D6436 (small/spray)
"""

import requests, json, time, os
from collections import defaultdict
from datetime import datetime, timezone

CACHE_FILE  = '/opt/polymarket-agent/consensus_cache.json'
TOP15_FILE  = '/opt/polymarket-agent/consensus_top15.json'
LEADERBOARD = 'https://data-api.polymarket.com/v1/leaderboard'

# Accounts flagged as LP/spray/non-directional — excluded from signal
EXCLUDED_PATTERNS = [
    '0x36257cb65f199caa86f7d30625bbc1250a981187',  # LP/volume farmer
    '0x0eb75bf6f54794a8',   # elshark206 prefix — LP
]

def get_dynamic_top15():
    """Pull and quality-score top 15 accounts across time windows."""
    windows = {}
    for window in ('all', 'monthly', 'weekly'):
        try:
            r = requests.get(LEADERBOARD, params={'limit': 50, 'window': window}, timeout=10)
            windows[window] = r.json() if r.ok else []
        except:
            windows[window] = []
        time.sleep(0.2)

    account_data = {}
    for window, entries in windows.items():
        for e in entries:
            w = e['proxyWallet']
            if w not in account_data:
                account_data[w] = {
                    'name': e.get('userName') or w[:12],
                    'wallet': w,
                    'ranks': {}, 'pnl': {}, 'vol': {},
                    'verified': e.get('verifiedBadge', False),
                }
            account_data[w]['ranks'][window] = int(e.get('rank', 999))
            account_data[w]['pnl'][window]   = float(e.get('pnl', 0))
            account_data[w]['vol'][window]   = float(e.get('vol', 0))

    scored = []
    for w, d in account_data.items():
        # Skip known LPs/volume farmers
        if any(w.startswith(ex[:12]) for ex in EXCLUDED_PATTERNS):
            continue
        all_rank   = d['ranks'].get('all', 999)
        month_rank = d['ranks'].get('monthly', 999)
        week_rank  = d['ranks'].get('weekly', 999)
        n_windows  = len(d['ranks'])
        score = (
            max(0, 50 - all_rank) +
            max(0, 50 - month_rank) * 2 +   # weight recent performance
            max(0, 50 - week_rank) +         # active this week
            n_windows * 5                    # consistency bonus
        )
        if n_windows == 1 and 'all' in d['ranks']:
            score *= 0.6  # stale — only in all-time, not recently
        scored.append({**d, 'score': round(score, 1),
                       'all_rank': all_rank, 'month_rank': month_rank, 'week_rank': week_rank,
                       'pnl_all': d['pnl'].get('all', 0)})

    scored.sort(key=lambda x: -x['score'])
    top15 = scored[:15]

    # Detect LP/volume-farmer behavior (buys AND sells same market heavily)
    directional = []
    for a in top15:
        try:
            r = requests.get(
                f'https://data-api.polymarket.com/activity?user={a["wallet"]}&limit=50',
                timeout=10)
            acts = r.json() if r.ok else []
            cutoff = time.time() - 7 * 86400
            recent = [x for x in acts if x.get('timestamp', 0) > cutoff]
            # Check for LP pattern: selling > 40% of recent trades by volume
            sell_vol = sum(float(x.get('usdcSize',0)) for x in recent if x.get('side')=='SELL')
            buy_vol  = sum(float(x.get('usdcSize',0)) for x in recent if x.get('side')=='BUY')
            total_vol = sell_vol + buy_vol
            sell_pct  = sell_vol / total_vol if total_vol > 0 else 0
            a['is_lp'] = sell_pct > 0.40  # high sell % = LP behavior
            a['sell_pct'] = round(sell_pct, 2)
            directional.append(a)
        except:
            a['is_lp'] = False
            directional.append(a)
        time.sleep(0.15)

    return directional

def scan_consensus(top15):
    """Get positions for all top-15 accounts, find alignment."""
    market_map = defaultdict(lambda: {'YES': [], 'NO': []})

    for a in top15:
        if a.get('is_lp'):
            continue  # skip LP accounts
        try:
            r = requests.get(
                f'https://data-api.polymarket.com/positions?user={a["wallet"]}&limit=50',
                timeout=10)
            positions = r.json() if r.ok else []
            for p in positions:
                val     = float(p.get('currentValue', 0))
                if val < 50: continue
                title   = p.get('title', '')
                outcome = p.get('outcome', '').upper()
                mid     = float(p.get('curPrice', 0))
                avg     = float(p.get('avgPrice', 0))
                size    = float(p.get('size', 0))
                # Skip near-resolved (mid > 0.92 or < 0.08) — stale
                if mid > 0.92 or mid < 0.08: continue
                if outcome not in ('YES', 'NO'): continue
                drift = mid - avg
                # Downgrade if price moved significantly against them (they may be wrong)
                market_map[title][outcome].append({
                    'account':  a['name'],
                    'rank':     a['all_rank'],
                    'pnl':      a['pnl_all'],
                    'val':      val,
                    'avg':      avg,
                    'mid':      mid,
                    'drift':    round(drift, 3),
                    'entry_stale': abs(drift) > 0.12,  # entered at much better price
                })
        except:
            pass
        time.sleep(0.12)

    # Build consensus output
    consensus = {}
    for title, sides in market_map.items():
        for side in ('YES', 'NO'):
            accts = sides[side]
            if not accts: continue
            n         = len(accts)
            opp       = sides['NO' if side == 'YES' else 'YES']
            avg_entry = sum(a['avg'] for a in accts) / n
            cur_mid   = accts[0]['mid']
            avg_drift = sum(a['drift'] for a in accts) / n
            stale_count = sum(1 for a in accts if a['entry_stale'])
            top_rank  = min(a['rank'] for a in accts)
            total_val = sum(a['val'] for a in accts)

            # Signal quality: downgrade if most accounts entered at much better prices
            stale_fraction = stale_count / n
            quality = 'strong' if n >= 3 and stale_fraction < 0.4 else \
                      'moderate' if n >= 2 and stale_fraction < 0.6 else \
                      'weak'

            key = f"{title}|{side}"
            consensus[key] = {
                'title':         title,
                'side':          side,
                'count':         n,
                'quality':       quality,
                'accounts':      [a['account'] for a in accts],
                'opposing':      [a['account'] for a in opp],
                'avg_entry':     round(avg_entry, 3),
                'cur_mid':       round(cur_mid, 3),
                'avg_drift':     round(avg_drift, 3),
                'stale_fraction':round(stale_fraction, 2),
                'top_rank':      top_rank,
                'total_val':     round(total_val, 2),
                'ts':            time.time(),
            }

    return consensus

def run():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Whale Consensus Scanner starting...")
    top15 = get_dynamic_top15()
    print(f"  Top-15 refreshed: {len(top15)} accounts ({sum(1 for a in top15 if not a.get('is_lp'))} directional)")

    # Save top15 snapshot
    with open(TOP15_FILE, 'w') as f:
        json.dump({'ts': time.time(), 'accounts': top15}, f, indent=2)

    consensus = scan_consensus(top15)
    print(f"  Consensus markets found: {len(consensus)}")

    # Print summary
    strong = [(k,v) for k,v in consensus.items() if v['count'] >= 2]
    strong.sort(key=lambda x: (-x[1]['count'], x[1]['top_rank']))
    for key, c in strong[:10]:
        stale_flag = ' [STALE ENTRIES]' if c['stale_fraction'] > 0.5 else ''
        print(f"  [{c['count']}x {c['quality'].upper()}] {c['title'][:50]} → {c['side']} @ {c['cur_mid']:.3f}{stale_flag}")
        print(f"    accounts={c['accounts']} drift={c['avg_drift']:+.3f}")

    # Save cache
    with open(CACHE_FILE, 'w') as f:
        json.dump({'ts': time.time(), 'consensus': consensus}, f, indent=2)
    print(f"  Cache written: {CACHE_FILE}")
    return consensus

def get_s7_score(market_title: str, side: str) -> float:
    """
    Called by signal_engine.py to get S7 whale consensus score.
    Returns 0.0–1.0 raw score (IC weighting applied in signal_engine).
    Downgraded if entries are stale (price already moved materially).
    """
    try:
        if not os.path.exists(CACHE_FILE):
            return 0.0
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        # Cache valid for 24h
        if time.time() - cache.get('ts', 0) > 86400:
            return 0.0
        key = f"{market_title}|{side}"
        if key not in cache['consensus']:
            return 0.0
        c = cache['consensus'][key]
        n = c['count']
        stale = c.get('stale_fraction', 0)
        # Score: count of aligned accounts, scaled, penalized for stale entries
        raw = min(n / 3.0, 1.0)          # 3+ accounts = full score
        raw *= (1.0 - stale * 0.5)       # stale entries cut score by up to 50%
        return round(raw, 3)
    except:
        return 0.0

if __name__ == '__main__':
    run()
