"""
trade_grader.py — Post-Trade Grade Engine (Karpathy Loop v2)
=============================================================
For every newly-closed market, asks Claude to grade the trade on 11 dimensions:

  1.  Thesis quality       — was the reasoning correct?
  2.  Entry quality        — good price, chased, or too early?
  3.  Exit quality         — took profit / held correctly / sold too early / decayed?
  4.  Position sizing      — appropriate for confidence, liquidity, time, downside?
  5.  Market type          — repeatable | asymmetric | calendar | headline | noise
  6.  Closing lesson       — one sentence the bot should remember
  7.  Thesis source        — BOT_INDEPENDENT | DK_ALPHA | MANUAL | HYBRID | HEADLINE | NOISE
  8.  Bot contribution     — what did the bot actually add?
  9.  Process quality      — was it a good trade regardless of outcome?
  10. Closing-line value   — did price move in our favor after entry?
  11. Confidence bucket    — what confidence grade should have been assigned at entry?

Grades are written to:
  /opt/polymarket-agent/intelligence/post_trade_reviews.jsonl  (structured, read by optimizer)
  /opt/polymarket-agent/intelligence/lessons.md                (human-readable, read by scanner)

The strategy_optimizer already reads post_trade_reviews.jsonl via load_failure_summary().
The scanner/autotrader reads intelligence/lessons.md via system prompt injection.

Detection: tracks which markets have already been graded via a state file
  /opt/polymarket-agent/intelligence/graded_markets.json
so duplicate grading never happens.

Usage:
  Runs daily (after market close) as part of the strategy optimizer cron.
  Can also be run standalone: python3 trade_grader.py
"""
import os, sys, json, time, re, requests, datetime, math
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

FUNDER        = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

INTEL_DIR     = "/opt/polymarket-agent/intelligence"
REVIEW_FILE   = os.path.join(INTEL_DIR, "post_trade_reviews.jsonl")
LESSONS_FILE  = os.path.join(INTEL_DIR, "lessons.md")
GRADED_FILE   = os.path.join(INTEL_DIR, "graded_markets.json")

os.makedirs(INTEL_DIR, exist_ok=True)

# ── Bucket classifier (mirrors autotrader.py classify_bucket) ─────────────────
_CAL_RE = re.compile(
    r'(extended by|by (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|ceasefire.*by|deal.*by)',
    re.IGNORECASE
)
_SPORTS_KW = ['nba','nfl','mlb','nhl','vs.','fc ','soccer','ufc','mma',
              'cavaliers','pistons','lakers','celtics','yankees','red sox',
              'dodgers','76ers','pacers','spurs','nuggets','warriors','thunder',
              'maverick','knicks','bucks','nets','bulls','hawks','hornets',
              'pelicans','grizzlies','timberwolves','blazers','raptors',
              'arsenal','manchester','chelsea','barcelona','celtic','rangers',
              'nhl','stanley cup','super bowl','world series','nba finals',
              'championship','grand slam','wimbledon','french open','us open']
_GEO_KW = ['iran','ukraine','russia','china','taiwan','israel','hungary',
            'peru','pakistan','india','ceasefire','invasion','nuclear',
            'peace deal','regime','election','diplomatic','sanctions',
            'nato','g7','g20','un security','coup','civil war']

def classify_bucket(title: str) -> str:
    t = title.lower()
    if _CAL_RE.search(t):              return 'CALENDAR'
    if any(k in t for k in _SPORTS_KW): return 'SPORTS'
    if any(k in t for k in _GEO_KW):   return 'GEO'
    return 'GEO'  # default: treat unknown as geo

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.datetime.utcnow().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass

def load_graded() -> set:
    if not os.path.exists(GRADED_FILE):
        return set()
    try:
        with open(GRADED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_graded(graded: set):
    with open(GRADED_FILE, 'w') as f:
        json.dump(sorted(graded), f, indent=2)

def append_review(record: dict):
    with open(REVIEW_FILE, 'a') as f:
        f.write(json.dumps(record) + '\n')

def append_lesson(title: str, lesson: str, bucket: str, pnl: float,
                  grade_overall: str, signal_source: str, clv: str):
    """Append one lesson line to lessons.md (read by autotrader/scanner)."""
    date = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    sign = '+' if pnl >= 0 else ''
    with open(LESSONS_FILE, 'a') as f:
        f.write(f"\n## [{date}] [{bucket}] [{signal_source}] [{grade_overall}] [{clv}] ${sign}{pnl:.0f}\n")
        f.write(f"**Market:** {title}\n")
        f.write(f"**Lesson:** {lesson}\n")

# ── Fetch closed markets ──────────────────────────────────────────────────────
def fetch_closed_markets(limit: int = 500) -> list:
    """
    Pull activity, group by market, return markets that are fully closed
    (have REDEEM or SELL covering > 90% of bought shares).
    Returns list of dicts with full trade context.
    """
    all_acts = []
    offset = 0
    while len(all_acts) < limit:
        r = requests.get(
            f"https://data-api.polymarket.com/activity?user={FUNDER}&limit=100&offset={offset}",
            timeout=15
        )
        if not r.ok:
            break
        page = r.json()
        if not page:
            break
        all_acts.extend(page)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(0.2)

    # Group by (title, outcome)
    from collections import defaultdict
    market_acts = defaultdict(list)
    for a in all_acts:
        if a.get('type') in ('TRADE', 'REDEEM'):
            key = (a.get('title', ''), a.get('outcome', ''))
            market_acts[key].append(a)

    closed = []
    for (title, outcome), acts in market_acts.items():
        if not title:
            continue

        buys     = [a for a in acts if a.get('type') == 'TRADE' and a.get('side') == 'BUY']
        sells    = [a for a in acts if a.get('type') == 'TRADE' and a.get('side') == 'SELL']
        redeems  = [a for a in acts if a.get('type') == 'REDEEM']

        total_bought_usdc = sum(float(a.get('usdcSize', 0)) for a in buys)
        total_sold_usdc   = sum(float(a.get('usdcSize', 0)) for a in sells + redeems)
        total_shares_in   = sum(float(a.get('size', 0)) for a in buys)
        total_shares_out  = sum(float(a.get('size', 0)) for a in sells + redeems)

        if total_bought_usdc < 5:  # skip dust
            continue

        # Market is "closed" if redeemed OR shares out ≥ 85% of shares in
        is_redeemed = len(redeems) > 0
        coverage    = total_shares_out / total_shares_in if total_shares_in > 0 else 0

        if not is_redeemed and coverage < 0.85:
            continue  # still open

        realized_pnl = total_sold_usdc - total_bought_usdc

        # Entry price stats
        avg_entry = total_bought_usdc / total_shares_in if total_shares_in > 0 else 0
        entry_prices = [float(a.get('price', 0)) for a in buys if float(a.get('price', 0)) > 0]
        first_entry  = min(entry_prices) if entry_prices else avg_entry
        last_entry   = max(entry_prices) if entry_prices else avg_entry
        entry_drift  = last_entry - first_entry  # positive = chased up

        # Exit price
        exit_prices = [float(a.get('price', 0)) for a in sells + redeems if float(a.get('price', 0)) > 0]
        avg_exit = sum(exit_prices) / len(exit_prices) if exit_prices else (1.0 if is_redeemed else avg_entry)
        if is_redeemed:
            avg_exit = 1.0  # full resolution

        # Time in trade
        all_ts   = [a.get('timestamp', 0) for a in acts if a.get('timestamp', 0) > 0]
        first_ts = min(all_ts) if all_ts else 0
        last_ts  = max(all_ts) if all_ts else 0
        days_held = (last_ts - first_ts) / 86400

        closed.append({
            'title':           title,
            'outcome':         outcome,
            'bucket':          classify_bucket(title),
            'realized_pnl':    round(realized_pnl, 2),
            'pnl_pct':         round(realized_pnl / total_bought_usdc * 100, 1) if total_bought_usdc else 0,
            'total_cost':      round(total_bought_usdc, 2),
            'total_proceeds':  round(total_sold_usdc, 2),
            'avg_entry':       round(avg_entry, 4),
            'avg_exit':        round(avg_exit, 4),
            'first_entry':     round(first_entry, 4),
            'last_entry':      round(last_entry, 4),
            'entry_drift':     round(entry_drift, 4),
            'buy_count':       len(buys),
            'sell_count':      len(sells),
            'redeem_count':    len(redeems),
            'is_redeemed':     is_redeemed,
            'days_held':       round(days_held, 1),
            'first_ts':        first_ts,
            'last_ts':         last_ts,
            'market_key':      f"{title}||{outcome}",
        })

    return sorted(closed, key=lambda x: -abs(x['realized_pnl']))


# ── Claude grader ─────────────────────────────────────────────────────────────
def grade_trade_claude(trade: dict) -> dict | None:
    """
    Ask Claude to grade a single closed trade on 11 dimensions.
    Returns structured grade dict or None on failure.
    """
    if not ANTHROPIC_KEY:
        return None

    bucket   = trade['bucket']
    pnl      = trade['realized_pnl']
    pnl_pct  = trade['pnl_pct']
    outcome  = 'WIN' if pnl > 0 else ('LOSS' if pnl < -0.50 else 'BREAKEVEN')
    redeemed = trade['is_redeemed']

    # Entry quality signal
    entry_note = ""
    if trade['entry_drift'] > 0.05:
        entry_note = f"Entry chased up: first entry {trade['first_entry']:.3f} → last entry {trade['last_entry']:.3f} (drift +{trade['entry_drift']:.3f})"
    elif trade['buy_count'] > 3:
        entry_note = f"Multiple entries ({trade['buy_count']} buys) — averaged in or DCA'd"
    else:
        entry_note = f"Clean entry: avg {trade['avg_entry']:.3f}, {trade['buy_count']} buy(s)"

    # Exit quality signal
    if redeemed:
        exit_note = f"Held to full resolution (redeemed at 1.000) — {'captured full upside' if pnl > 0 else 'held a loser to zero'}"
    elif trade['avg_exit'] > 0.95:
        exit_note = f"Sold near resolution {trade['avg_exit']:.3f} — good timing"
    elif pnl > 0 and trade['avg_exit'] < trade['avg_entry'] * 1.15:
        exit_note = f"Sold at {trade['avg_exit']:.3f} vs entry {trade['avg_entry']:.3f} — possible premature exit (only +{pnl_pct:.0f}%)"
    elif pnl < 0:
        exit_note = f"Sold at {trade['avg_exit']:.3f} vs entry {trade['avg_entry']:.3f} — stopped out or let runner decay"
    else:
        exit_note = f"Exit at {trade['avg_exit']:.3f} vs entry {trade['avg_entry']:.3f}"

    prompt = f"""You are grading a closed Polymarket prediction market trade. Grade it across 11 dimensions. Be blunt and precise — this feeds a learning loop.

TRADE DETAILS:
  Market: {trade['title']}
  Outcome bet: {trade['outcome']}
  Bucket: {bucket} (SPORTS=grind edge | GEO=asymmetric thesis | CALENDAR=timing spread)
  Result: {outcome}
  Cost: ${trade['total_cost']:.2f}  →  Proceeds: ${trade['total_proceeds']:.2f}
  Realized P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)
  Days held: {trade['days_held']:.1f}
  Buy count: {trade['buy_count']}
  {entry_note}
  {exit_note}
  Redeemed at resolution: {redeemed}

SIGNAL SOURCE CLASSIFICATION RULES:
- BOT_INDEPENDENT: bot found the opportunity via opportunity_scanner.py with no DK or user input
- DK_ALPHA: trade was triggered by a DraftKings signal (dk_picks.json or live_sports_trader.py)
- MANUAL: user explicitly directed the trade in conversation
- HYBRID: combination of bot analysis + DK signal OR bot + user direction
- HEADLINE: reaction to breaking news/event without deeper thesis
- NOISE: no identifiable thesis

SIGNAL SOURCE HEURISTICS (use as guidance, apply judgment):
- If bucket == SPORTS and days_held < 1: likely DK_ALPHA (same-day sports resolution)
- If bucket == SPORTS and days_held > 3: likely BOT_INDEPENDENT or MANUAL
- If bucket == CALENDAR: likely BOT_INDEPENDENT thesis
- If bucket == GEO and total_cost < 150: likely BOT_INDEPENDENT (under geo cap)
- If buy_count == 1 and total_cost > 200: possibly MANUAL (single large entry)

CLOSING-LINE VALUE (CLV) ESTIMATION — use these rules since we lack intraday price history:
- If redeemed and pnl > 0: CLV is POSITIVE (price moved to 1.0)
- If redeemed and pnl < 0: CLV is NEGATIVE (held to zero)
- If avg_exit > avg_entry and pnl > 0: CLV likely POSITIVE
- If avg_exit < avg_entry: CLV is NEGATIVE
- If pnl_pct > 20% in < 3 days: CLV likely POSITIVE
- Otherwise: UNKNOWN

CONFIDENCE BUCKET — retroactively assign the confidence bucket that SHOULD have been used at entry (not what was used):
- A+: clear market mispricing, strong thesis, good liquidity, time to resolution > 7 days, repeatable edge
- B: reasonable thesis with some uncertainty
- C: speculative, thin liquidity, or timing-dependent
- D: watchlist only — no trade should have been made
- F: noise trade — should never have been entered

GRADING RUBRIC:
Return ONLY a JSON object with exactly these fields (no prose, no markdown):

{{
  "thesis_quality": "CORRECT | PARTIALLY_CORRECT | WRONG | UNKNOWN",
  "thesis_note": "1 sentence explaining why (what the thesis was and whether it played out)",
  "entry_quality": "GOOD | CHASED | EARLY | AVERAGED_IN",
  "entry_note": "1 sentence on price and timing",
  "exit_quality": "OPTIMAL | GOOD | PREMATURE | HELD_CORRECTLY | DECAYED | STOPPED_OUT",
  "exit_note": "1 sentence on whether exit was correct",
  "sizing_quality": "APPROPRIATE | OVERSIZED | UNDERSIZED | CORRECT_FOR_BUCKET",
  "sizing_note": "1 sentence on whether size matched confidence, liquidity, and time to resolution",
  "market_type": "REPEATABLE_EDGE | ASYMMETRIC_THESIS | CALENDAR_SPREAD | HEADLINE_CHASE | PURE_NOISE",
  "primary_failure_mode": "none | late_entry | position_too_large | position_too_small | early_exit | held_loser | wrong_thesis | calendar_timing | headline_chase | spread_mismatch",
  "closing_lesson": "ONE sentence starting with signal source tag e.g. [BOT_INDEPENDENT] followed by an action verb and lesson the bot should remember for the next similar trade.",
  "grade_overall": "A | B | C | D | F",
  "thesis_source": "BOT_INDEPENDENT | DK_ALPHA | MANUAL | HYBRID | HEADLINE | NOISE",
  "thesis_source_note": "1 sentence explaining how this was classified",
  "bot_contribution": "GENERATED_THESIS | IMPROVED_ENTRY | IMPROVED_EXIT | IMPROVED_SIZING | FOUND_CORRELATED | AVOIDED_WORSE | FOLLOWED_SIGNAL | UNCLEAR",
  "bot_contribution_note": "1 sentence on what the bot actually added",
  "process_quality": "GOOD | ACCEPTABLE | POOR | VERY_POOR",
  "process_quality_note": "1 sentence — was it a good trade regardless of outcome?",
  "outcome_quality": "WIN | BREAKEVEN | LOSS",
  "closing_line_value": "POSITIVE | NEGATIVE | NEUTRAL | UNKNOWN",
  "closing_line_note": "Did price move in our favor after entry? If unknown, say UNKNOWN.",
  "confidence_bucket": "A_PLUS | B | C | D | F",
  "confidence_note": "What confidence grade should have been assigned at entry?"
}}

Grade A = profitable AND process was correct (right thesis, good entry, correct exit, right size)
Grade B = profitable OR process was mostly correct  
Grade C = breakeven or small loss, process was partially correct
Grade D = meaningful loss, one dimension was badly wrong
Grade F = large loss AND process was wrong on multiple dimensions

For CALENDAR bucket: be extra harsh on sizing — calendar spreads should be small.
For GEO bucket: if thesis was correct and pnl positive, credit the asymmetry.
For SPORTS bucket: focus on whether the entry price was +EV relative to the closing line.

The closing_lesson field MUST start with the signal source tag in brackets, e.g.:
  "[BOT_INDEPENDENT] Avoid entering SPORTS markets after line movement exceeds 5 cents."
  "[DK_ALPHA] Follow DK signals only when Polymarket price lags by > 8 cents."

Return ONLY the JSON. No explanation outside the JSON."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 900,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if not resp.ok:
            log(f"  Claude API error {resp.status_code}: {resp.text[:200]}")
            return None

        content = resp.json()["content"][0]["text"].strip()
        # Strip markdown fences if present
        content = re.sub(r'^```json?\n?', '', content)
        content = re.sub(r'\n?```$', '', content)
        grade = json.loads(content)
        return grade

    except json.JSONDecodeError as e:
        log(f"  JSON parse error: {e} | raw: {content[:200]}")
        return None
    except Exception as e:
        log(f"  Claude error: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def run_grader(max_new: int = 10, send_summary: bool = True) -> int:
    """
    Grade up to max_new newly-closed markets.
    Returns number of markets graded this run.
    """
    log("=== Trade Grader Starting ===")

    graded = load_graded()
    log(f"Already graded: {len(graded)} markets")

    closed = fetch_closed_markets(limit=500)
    log(f"Total closed markets found: {len(closed)}")

    new_markets = [m for m in closed if m['market_key'] not in graded]
    log(f"New to grade: {len(new_markets)}")

    if not new_markets:
        log("Nothing new to grade.")
        return 0

    # Prioritize: grade largest P&L swings first (most informative)
    new_markets = sorted(new_markets, key=lambda x: -abs(x['realized_pnl']))[:max_new]

    graded_this_run = []
    failures = 0

    for trade in new_markets:
        title   = trade['title'][:60]
        pnl     = trade['realized_pnl']
        bucket  = trade['bucket']
        log(f"Grading: [{bucket}] {title} | P&L: ${pnl:+.2f}")

        grade = grade_trade_claude(trade)

        if not grade:
            failures += 1
            log(f"  ⚠ Grade failed — skipping (will retry next run)")
            continue

        # Merge trade data + grade into one record
        record = {
            **trade,
            **grade,
            "graded_at": datetime.datetime.utcnow().isoformat() + "Z",
            "source_module": bucket.lower(),
        }

        # Write to post_trade_reviews.jsonl
        append_review(record)

        # Write lesson to lessons.md
        append_lesson(
            title=trade['title'],
            lesson=grade.get('closing_lesson', '(no lesson)'),
            bucket=bucket,
            pnl=pnl,
            grade_overall=grade.get('grade_overall', '?'),
            signal_source=grade.get('thesis_source', 'UNKNOWN'),
            clv=grade.get('closing_line_value', 'UNKNOWN'),
        )

        # Mark as graded
        graded.add(trade['market_key'])
        save_graded(graded)

        graded_this_run.append(record)
        log(f"  ✓ Grade={grade.get('grade_overall','?')} | {grade.get('primary_failure_mode','none')} | {grade.get('closing_lesson','')[:80]}")

        time.sleep(1)  # rate limit

    # ── Summary ──────────────────────────────────────────────────────────────
    n = len(graded_this_run)
    log(f"Graded {n} new trade(s) | {failures} failure(s)")

    if n == 0:
        return 0

    if send_summary and TG_TOKEN:
        # Build compact Telegram summary
        grade_map = {'A': '🟢', 'B': '🟡', 'C': '🟠', 'D': '🔴', 'F': '⛔'}
        lines = [f"📝 <b>Trade Grades — {n} new close(s)</b>\n"]

        for r in graded_this_run:
            g    = r.get('grade_overall', '?')
            icon = grade_map.get(g, '◻')
            pnl  = r['realized_pnl']
            sign = '+' if pnl >= 0 else ''
            fm   = r.get('primary_failure_mode', 'none')
            src  = r.get('thesis_source', '?')
            clv  = r.get('closing_line_value', '?')
            lines.append(
                f"{icon} <b>[{r['bucket']}] [{src}] {g}</b> {sign}${pnl:.0f}\n"
                f"  {r['title'][:50]}\n"
                f"  {r.get('closing_lesson','')[:100]}"
            )
            if fm not in ('none', ''):
                lines.append(f"  ⚠ Failure mode: {fm}")
            lines.append("")

        # Aggregate failure mode summary
        fm_counts = {}
        for r in graded_this_run:
            fm = r.get('primary_failure_mode', 'none')
            if fm and fm != 'none':
                fm_counts[fm] = fm_counts.get(fm, 0) + 1

        if fm_counts:
            lines.append("🔍 <b>Failure modes this batch:</b>")
            for fm, cnt in sorted(fm_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {fm}: {cnt}x")
            lines.append("")

        # Signal source breakdown
        src_counts = {}
        for r in graded_this_run:
            src = r.get('thesis_source', 'UNKNOWN')
            src_counts[src] = src_counts.get(src, 0) + 1

        if src_counts:
            lines.append("📡 <b>Signal sources this batch:</b>")
            for src, cnt in sorted(src_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {src}: {cnt}x")
            lines.append("")

        # Process quality summary
        pq_counts = {}
        for r in graded_this_run:
            pq = r.get('process_quality', 'UNKNOWN')
            pq_counts[pq] = pq_counts.get(pq, 0) + 1

        if pq_counts:
            lines.append("⚙️ <b>Process quality this batch:</b>")
            for pq, cnt in sorted(pq_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {pq}: {cnt}x")
            lines.append("")

        # Closing-line value summary
        clv_counts = {}
        for r in graded_this_run:
            clv = r.get('closing_line_value', 'UNKNOWN')
            clv_counts[clv] = clv_counts.get(clv, 0) + 1

        if clv_counts:
            lines.append("📈 <b>Closing-line value this batch:</b>")
            for clv, cnt in sorted(clv_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  • {clv}: {cnt}x")

        tg("\n".join(lines))

    return n


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--max', type=int, default=10, help='Max new trades to grade')
    parser.add_argument('--no-tg', action='store_true', help='Skip Telegram summary')
    args = parser.parse_args()
    run_grader(max_new=args.max, send_summary=not args.no_tg)
    print("Done.")
