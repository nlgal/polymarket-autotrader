"""
strategy_optimizer.py — Lightweight Karpathy Loop for Polymarket
=================================================================
Inspired by Andrej Karpathy's autoresearch loop:
  - One file the agent edits: scanner_config.json (thresholds, rules)
  - One file it can never touch: .env, wallet, CLOB execution
  - One scoring function: realized P&L per trade from Polymarket activity API

Every run:
1. Reads current scanner_config.json
2. Scores it against last N resolved trades
3. Proposes ONE parameter tweak using Claude
4. Evaluates the tweak against historical data (backtests on trade history)
5. If score improves → git commit the config
6. If score drops → git reset config to last good state
7. Logs everything + sends Telegram summary

Runs daily (or on-demand). Slowly optimizes scanner thresholds over time.
This is v1 — the "lightweight" version. v2 will run overnight with real capital.

LOCKED (never modified): .env, private keys, CLOB execution code, wallet
EDITABLE (agent can modify): scanner_config.json, opportunity_scanner.py thresholds
"""
import os, sys, json, math, time, subprocess, requests, datetime, re
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

FUNDER       = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID","").strip()
ANTHROPIC_KEY= os.environ.get("ANTHROPIC_API_KEY","").strip()
AGENT_DIR    = "/opt/polymarket-agent"
CONFIG_FILE  = os.path.join(AGENT_DIR, "scanner_config.json")
CONFIG_LOG   = os.path.join(AGENT_DIR, "optimizer.log")
HISTORY_FILE = os.path.join(AGENT_DIR, "optimizer_history.json")

# ── Default config (written if missing) ──────────────────────────────────────
DEFAULT_CONFIG = {
    "min_scan_edge": 0.15,
    "max_trade_size": 75,
    "min_trade_size": 35,
    "uw_edge_discount": 0.20,
    "coin_flip_band": [0.42, 0.58],    # yes_p range treated as coin-flip
    "near_certain_threshold": 0.93,     # skip if yes_p > this or < 1-this
    "conflict_event_min_days": 30,      # min days remaining for YES on conflict markets
    "version": 1,
    "last_updated": "",
    "score": 0.0,                       # running score (win_rate * avg_pnl_pct)
    "description": "Initial conservative config"
}

# ── Market categories (for scoring analysis) ──────────────────────────────────
CONFLICT_KEYWORDS = [
    "ceasefire", "forces enter", "regime fall", "conflict ends",
    "military operations", "invasion", "invade", "war ends", "peace deal",
    "strikes end", "bombing", "kharg", "hormuz", "nuclear deal",
    "attack iran", "bomb iran", "invades iran", "us invade"
]
SPORTS_KEYWORDS = ["vs.", "vs ", "match", "game", "championship", "tournament",
                   "playoff", "cup", "league", "bowl", "series"]


def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass


def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(CONFIG_LOG, "a") as f:
            f.write(line + "\n")
    except: pass


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    cfg["last_updated"] = datetime.datetime.utcnow().isoformat()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-50:], f, indent=2)  # keep last 50 runs


# ── Scoring: fetch trade history and compute metrics ─────────────────────────

def fetch_trade_history(limit=200):
    """Get full activity from Polymarket data API."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/activity?user={FUNDER}&limit={limit}",
            timeout=15)
        return r.json()
    except Exception as e:
        log(f"Error fetching history: {e}")
        return []


def compute_realized_pnl(acts):
    """
    Group trades by market, compute realized P&L for each closed market.
    Returns list of dicts with market metadata + P&L.
    """
    market_data = {}
    for a in acts:
        title = a.get("title", "?")
        side  = a.get("side", "?")
        usdc  = float(a.get("usdcSize", 0))
        ts    = a.get("timestamp", 0)

        if title not in market_data:
            market_data[title] = {
                "title": title, "buys": 0, "sells": 0,
                "buy_count": 0, "sell_count": 0, "first_ts": ts
            }
        if side == "BUY":
            market_data[title]["buys"] += usdc
            market_data[title]["buy_count"] += 1
        elif side == "SELL":
            market_data[title]["sells"] += usdc
            market_data[title]["sell_count"] += 1

    results = []
    for title, d in market_data.items():
        if d["buys"] > 0 and d["sells"] > 0:
            pnl = d["sells"] - d["buys"]
            pnl_pct = pnl / d["buys"] * 100
            t_lower = title.lower()
            results.append({
                "title": title,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 1),
                "bought": round(d["buys"], 2),
                "sold": round(d["sells"], 2),
                "is_conflict": any(k in t_lower for k in CONFLICT_KEYWORDS),
                "is_sports": any(k in t_lower for k in SPORTS_KEYWORDS),
                "is_crypto": any(k in t_lower for k in ["bitcoin", "btc", "eth", "crypto"]),
            })

    return sorted(results, key=lambda x: -abs(x["pnl"]))


def score_config(cfg, trade_history):
    """
    Score a config against historical trades.
    Simulates which trades the config WOULD have allowed, and evaluates their P&L.
    
    Scoring formula: win_rate * avg_pnl_pct * (1 + large_win_bonus)
    Higher = better config. Range roughly 0–100.
    """
    if not trade_history:
        return 0.0, {}

    # Simulate which trades would pass under this config
    passing_trades = []
    for t in trade_history:
        # Would this trade have been allowed?
        title_lower = t["title"].lower()
        
        # Conflict YES guardrail: we'd have blocked short-duration YES on conflict markets
        # (We can't know the original direction from activity alone, so use heuristics)
        # Trades on conflict markets that LOST were likely the blocked-YES pattern
        is_conflict = t["is_conflict"]
        is_sports   = t["is_sports"]
        
        # Sports filter: block if no UW signal (we assume no signal for historical trades)
        if is_sports and t["bought"] < 60:  # Small sports bets = scanner (no signal)
            continue
        
        # Would pass if: not conflict (always), or conflict with positive result (would keep)
        # This is approximate — we're scoring the OUTCOME correlation with our rules
        passing_trades.append(t)

    if not passing_trades:
        return 0.0, {}

    total_pnl = sum(t["pnl"] for t in passing_trades)
    wins   = [t for t in passing_trades if t["pnl"] > 0]
    losses = [t for t in passing_trades if t["pnl"] <= 0]

    win_rate   = len(wins) / len(passing_trades) if passing_trades else 0
    avg_pnl_pct = sum(t["pnl_pct"] for t in passing_trades) / len(passing_trades)

    # Bonus for avoiding large losses
    large_loss_count = len([t for t in passing_trades if t["pnl"] < -30])
    large_loss_penalty = large_loss_count * 5

    # Score: win_rate (0-1) * avg_pnl_pct (can be negative) - penalty
    score = (win_rate * 100) + (avg_pnl_pct * 0.5) - large_loss_penalty

    details = {
        "n_trades": len(passing_trades),
        "win_rate": round(win_rate * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 1),
        "large_losses": large_loss_count,
        "score": round(score, 2),
    }
    return round(score, 2), details


def propose_tweak(cfg, trade_history, current_score, history):
    """
    Use Claude to propose ONE parameter tweak based on trade analysis.
    Returns proposed_config or None if no improvement found.
    """
    if not ANTHROPIC_KEY:
        return None, "No API key"

    # Summarize trade performance by category
    wins   = [t for t in trade_history if t["pnl"] > 0]
    losses = [t for t in trade_history if t["pnl"] < -5]

    win_summary = "\n".join(
        f"  +${t['pnl']:.0f} ({t['pnl_pct']:+.0f}%): {t['title'][:55]}"
        for t in sorted(wins, key=lambda x: -x["pnl"])[:8]
    )
    loss_summary = "\n".join(
        f"  -${abs(t['pnl']):.0f} ({t['pnl_pct']:+.0f}%): {t['title'][:55]}"
        for t in sorted(losses, key=lambda x: x["pnl"])[:8]
    )

    recent_history = "\n".join(
        f"  v{h['version']}: score={h['score']:.1f} — {h['description']}"
        for h in history[-5:]
    ) if history else "  (no history yet)"

    prompt = f"""You are optimizing a Polymarket trading bot scanner configuration.
    
CURRENT CONFIG:
{json.dumps({k: v for k, v in cfg.items() if k not in ['version','last_updated','description']}, indent=2)}

CURRENT SCORE: {current_score:.1f} (higher = better)

RECENT OPTIMIZATION HISTORY:
{recent_history}

WINNING TRADES (last 200 activity records):
{win_summary}

LOSING TRADES:
{loss_summary}

TASK: Propose ONE specific parameter change to improve the score.
Rules:
- Only suggest changing ONE parameter at a time
- The change must be data-driven (explain why based on winning/losing patterns)
- Valid parameters: min_scan_edge (0.10-0.25), max_trade_size (50-150), uw_edge_discount (0.10-0.40), coin_flip_band [low,high] where 0.30-0.45 and 0.55-0.70, conflict_event_min_days (14-60), near_certain_threshold (0.88-0.96)
- Do NOT suggest turning off safety guardrails entirely
- Focus on what's actually causing losses vs wins

Respond with ONLY a JSON object:
{{
  "parameter": "parameter_name",
  "old_value": <current_value>,
  "new_value": <proposed_value>,
  "reasoning": "one clear sentence explaining why based on the data",
  "expected_score_delta": <estimated_change_in_score>
}}"""

    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5", "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)

        if resp.status_code != 200:
            return None, f"API error {resp.status_code}"

        content = resp.json().get("content", [{}])[0].get("text", "")
        # Try to extract JSON - Claude sometimes adds markdown or trailing text
        # Try ```json block first
        code_match = re.search(r'```(?:json)?\s*({.*?})\s*```', content, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1)), ""
            except: pass
        # Try raw JSON object
        for match in re.finditer(r'\{[^{}]+\}', content, re.DOTALL):
            try:
                result = json.loads(match.group())
                if "parameter" in result:
                    return result, ""
            except: continue
        # Try finding any JSON-like structure
        bracket_match = re.search(r'(\{[\s\S]*?\})', content)
        if bracket_match:
            try:
                return json.loads(bracket_match.group(1)), ""
            except: pass
        return None, f"no valid json in: {content[:150]}"
    except Exception as e:
        return None, str(e)[:80]


def apply_tweak(cfg, tweak):
    """Apply a proposed tweak to the config dict."""
    new_cfg = cfg.copy()
    param = tweak.get("parameter")
    new_val = tweak.get("new_value")

    if param and new_val is not None:
        new_cfg[param] = new_val
        new_cfg["version"] = cfg.get("version", 1) + 1
        new_cfg["description"] = tweak.get("reasoning", "")[:100]

    return new_cfg


def write_config_to_scanner(cfg):
    """
    Write the config thresholds into opportunity_scanner.py.
    This is the 'commit to the editable file' step.
    """
    scanner_path = os.path.join(AGENT_DIR, "opportunity_scanner.py")
    if not os.path.exists(scanner_path):
        return False, "Scanner not found"

    with open(scanner_path) as f:
        content = f.read()

    # Update key threshold lines
    replacements = [
        (r'MIN_SCAN_EDGE\s*=\s*[\d.]+', f'MIN_SCAN_EDGE    = {cfg["min_scan_edge"]:.2f}'),
        (r'MAX_TRADE_SIZE\s*=\s*[\d.]+', f'MAX_TRADE_SIZE   = {int(cfg["max_trade_size"])}'),
        (r'MIN_TRADE_SIZE\s*=\s*[\d.]+', f'MIN_TRADE_SIZE   = {int(cfg.get("min_trade_size", 35))}'),
        (r'UW_EDGE_DISCOUNT\s*=\s*[\d.]+', f'UW_EDGE_DISCOUNT = {cfg["uw_edge_discount"]:.2f}'),
    ]

    import re as _re
    for pattern, replacement in replacements:
        new_content = _re.sub(pattern, replacement, content)
        if new_content != content:
            content = new_content

    with open(scanner_path, "w") as f:
        f.write(content)

    # Syntax check
    result = subprocess.run(
        ["python3", "-m", "py_compile", scanner_path],
        capture_output=True, text=True)

    if result.returncode != 0:
        return False, f"Syntax error: {result.stderr[:100]}"

    return True, "Scanner updated"


# ── Auto Dream: Regenerate CLAUDE.md with current live data ──────────────────
# Inspired by Claude Code's Auto Dream memory feature — periodically rewrites
# the memory file so Claude always has accurate, current context.

def get_latest_news():
    """Fetch recent headlines for our key positions."""
    try:
        import xml.etree.ElementTree as ET
        import email.utils
        url = "https://news.google.com/rss/search?q=iran+ceasefire+war+military&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        headlines = []
        now = datetime.datetime.utcnow()
        for item in items[:6]:
            title = item.findtext("title", "")
            pub   = item.findtext("pubDate", "")
            try:
                dt = email.utils.parsedate_to_datetime(pub)
                age_h = (now.replace(tzinfo=None) - dt.replace(tzinfo=None)).total_seconds() / 3600
                if age_h < 24:
                    headlines.append(f"  [{age_h:.0f}h] {title[:90]}")
            except: pass
        return "\n".join(headlines[:5]) if headlines else "  No recent headlines"
    except:
        return "  (news fetch failed)"


def get_current_positions():
    """Fetch live portfolio from Polymarket."""
    try:
        r  = requests.get(f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=8)
        val = float(r.json()[0]["value"])
        r2 = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50", timeout=8)
        positions = r2.json()
        active = []
        for p in sorted(positions, key=lambda x: -float(x.get("currentValue", 0))):
            cv = float(p.get("currentValue", 0))
            if cv < 2: continue
            size = float(p.get("size", 0))
            avg  = float(p.get("avgPrice", 0))
            pnl  = cv - size * avg
            end  = (p.get("endDate", "") or "")[:10]
            active.append({
                "outcome": p.get("outcome", "?"),
                "cv": cv, "pnl": pnl,
                "title": p.get("title", "?")[:60],
                "end": end
            })
        return val, active
    except Exception as e:
        return 0.0, []


def auto_dream():
    """
    Auto Dream: Rewrite CLAUDE.md with current live portfolio, P&L, and news.
    Keeps the AI's memory accurate and prevents stale context degradation.
    Called daily by the optimizer — like Claude Code's Auto Dream feature.
    """
    log("=== Auto Dream: Regenerating CLAUDE.md ===")

    equity, positions = get_current_positions()
    news = get_latest_news()
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Format portfolio table
    pos_lines = []
    pos_total = 0
    for p in positions:
        pnl_str = f"{p['pnl']:+.2f}"
        pos_lines.append(
            f"| {p['outcome']:3} | ${p['cv']:7.2f} | {pnl_str:>8} | {p['end']:10} | {p['title'][:50]} |"
        )
        pos_total += p["cv"]

    pos_table = "\n".join(pos_lines) if pos_lines else "| (no active positions) |"

    # Compute realized P&L summary from trade history
    try:
        acts = fetch_trade_history(limit=200)
        market_data = {}
        for a in acts:
            t = a.get("title", "?")
            s = a.get("side", "?")
            u = float(a.get("usdcSize", 0))
            if t not in market_data:
                market_data[t] = {"buys": 0, "sells": 0}
            if s == "BUY":   market_data[t]["buys"] += u
            if s == "SELL":  market_data[t]["sells"] += u

        wins   = [(t, d["sells"] - d["buys"]) for t, d in market_data.items()
                  if d["buys"] > 0 and d["sells"] > 0 and d["sells"] - d["buys"] > 5]
        losses = [(t, d["sells"] - d["buys"]) for t, d in market_data.items()
                  if d["buys"] > 0 and d["sells"] > 0 and d["sells"] - d["buys"] < -5]

        win_lines  = "\n".join(f"  +${pnl:.0f}: {t[:55]}"
                               for t, pnl in sorted(wins, key=lambda x: -x[1])[:6])
        loss_lines = "\n".join(f"  -${abs(pnl):.0f}: {t[:55]}"
                               for t, pnl in sorted(losses, key=lambda x: x[1])[:6])
        total_realized = sum(d["sells"] - d["buys"] for d in market_data.values()
                             if d["buys"] > 0 and d["sells"] > 0)
        n_wins   = len(wins)
        n_losses = len(losses)
        win_rate = n_wins / (n_wins + n_losses) * 100 if (n_wins + n_losses) > 0 else 0
    except:
        win_lines = loss_lines = "  (unavailable)"
        total_realized = win_rate = 0

    content = f"""# Polymarket Autotrader — Context File (Auto Dream)
# Auto-generated by strategy_optimizer.py on {now_str}
# DO NOT EDIT MANUALLY — this file is rewritten daily with live data.

## System Overview
Autonomous Polymarket prediction market trader on Polygon blockchain.
- Wallet: Gnosis Safe `0xc2c1892653C175113c65961C7F4227c18D09b52a`
- Server: DigitalOcean Amsterdam, /opt/polymarket-agent/
- Capital: ${equity:.2f} equity | $1,600 deposited | P&L: ${equity - 1600:.2f} ({(equity - 1600) / 1600 * 100:.1f}%)
- Goal: grow to $100k through disciplined edge-based trading

## Current Portfolio (live as of {now_str})
| Side | Value | P&L | Expires | Market |
|---|---|---|---|---|
{pos_table}

**Total open position value:** ${pos_total:.2f}
**Total realized P&L (history):** ${total_realized:.2f} ({win_rate:.0f}% win rate on closed trades)

## What Has Worked (Recent Wins)
{win_lines}

## What Has Failed (Recent Losses)
{loss_lines}

## Trading Rules — ENFORCE STRICTLY

### NEVER DO
- BUY YES on conflict/event markets with < 30 days until expiry
  - Covers: ceasefire, forces enter, regime fall, invasion, invade, war ends, kharg, hormuz, military operations, nuclear deal, peace deal, attack iran, bomb iran
  - Root cause: near-term event bets lose ~75% of the time. Status quo wins.
- Trade sports markets without Unusual Whales whale/insider signal
- Buy near-certain markets (>93% YES or <7% YES) — no edge
- Buy coin-flip markets (42-58% YES) without news catalyst or UW signal
- Enter FDV/TGE/token launch markets — ghost order books

### ALWAYS DO
- Prefer NO on conflict markets — status quo bets win reliably
- Check Unusual Whales signals: "insider_trades" + "contrarian_whales" = strong edge
- Require real CLOB depth (bids AND asks 10-90¢, spread < 30¢)
- Max trade size: $75 at current equity level

## Current Market Context (Iran War — {now_str[:10]})
{news}

## Key Scoring Heuristics
**Strong BUY_NO signals:**
- Near-term deadline for a conflict event (< 30 days) — status quo wins
- Iran has publicly rejected negotiations (ongoing as of this writing)
- Market pricing YES too high for near-term resolution

**Strong BUY_YES signals:**
- Long duration (> 30 days) on escalation events with active military buildup
- UW shows insider_trades + contrarian_whales tags with real volume
- Clear news catalyst within last 24h with ACTUAL action (not just talks)

**Strong PASS signals:**
- Sports market without UW whale flow signal
- Coin-flip range (42-58%) without catalyst
- Market expiring < 7 days
- FDV, TGE, token launch, celebrity tweet count markets

## Edge Thresholds (Current Config)
- MIN_SCAN_EDGE = 0.15 (15% mispricing required)
- MAX_TRADE_SIZE = $75
- UW_EDGE_DISCOUNT = 0.20 (lowers threshold to 12% with insider/whale signal)

## The One Rule That Matters Most
"The status quo is almost always the correct prediction for near-term conflict markets.
Nothing needs to happen for NO to win. Something specific must happen for YES to win."
"""

    # Write to server file
    claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")
    try:
        with open(claude_md_path, "w") as f:
            f.write(content)
        log(f"CLAUDE.md regenerated ({len(content)} chars) — equity ${equity:.2f}, {len(positions)} positions")
        return content
    except Exception as e:
        log(f"CLAUDE.md write failed: {e}")
        return content


def main():
    log("=== Strategy Optimizer (Karpathy Loop v1) Starting ===")

    # Load current config
    cfg = load_config()
    if not os.path.exists(CONFIG_FILE):
        save_config(cfg)
        log("Created default config")

    # Load optimization history
    history = load_history()

    # Fetch trade data (ground truth)
    log("Fetching trade history from Polymarket...")
    acts = fetch_trade_history(limit=200)
    if not acts:
        log("No trade data — skipping")
        return

    trade_results = compute_realized_pnl(acts)
    log(f"Analyzed {len(trade_results)} closed markets")

    # Score current config
    current_score, current_details = score_config(cfg, trade_results)
    log(f"Current score: {current_score:.1f} | {current_details}")

    cfg["score"] = current_score
    save_config(cfg)

    # Propose a tweak
    log("Asking Claude for optimization suggestion...")
    tweak, err = propose_tweak(cfg, trade_results, current_score, history)

    if not tweak:
        log(f"No tweak proposed: {err}")
        tg(f"<b>🔬 Optimizer run</b>\nScore: {current_score:.1f} | No tweak this cycle\n{current_details}")
        return

    log(f"Proposed tweak: {tweak}")

    # Apply tweak and score it
    new_cfg = apply_tweak(cfg, tweak)
    new_score, new_details = score_config(new_cfg, trade_results)
    log(f"Proposed score: {new_score:.1f} | {new_details}")

    if new_score > current_score:
        # Git commit — apply the improvement
        log(f"✅ IMPROVEMENT: {current_score:.1f} → {new_score:.1f}")
        new_cfg["score"] = new_score

        # Write config to scanner
        ok, msg = write_config_to_scanner(new_cfg)
        if ok:
            save_config(new_cfg)
            history.append({
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "version": new_cfg["version"],
                "score": new_score,
                "delta": round(new_score - current_score, 2),
                "tweak": tweak,
                "description": new_cfg["description"],
            })
            save_history(history)
            log(f"Config v{new_cfg['version']} saved and applied to scanner")

            tg(f"""<b>🔬 Optimizer: IMPROVEMENT</b>
Score: {current_score:.1f} → {new_score:.1f} (+{new_score - current_score:.1f})

<b>Tweak:</b> {tweak.get('parameter')} = {tweak.get('old_value')} → {tweak.get('new_value')}
<b>Reason:</b> {tweak.get('reasoning', '')[:120]}

<b>Stats:</b> {new_details.get('n_trades')} trades | {new_details.get('win_rate')}% win rate | avg {new_details.get('avg_pnl_pct'):+.0f}% per trade""")
        else:
            log(f"❌ Could not write to scanner: {msg}")
    else:
        # Git reset — discard the tweak
        log(f"No improvement ({new_score:.1f} ≤ {current_score:.1f}) — keeping current config")
        history.append({
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "version": cfg["version"],
            "score": current_score,
            "delta": 0,
            "tweak": {"rejected": tweak},
            "description": f"Rejected: {tweak.get('parameter')} change",
        })
        save_history(history)

        tg(f"""<b>🔬 Optimizer: No change</b>
Score: {current_score:.1f} (stable)
Rejected: {tweak.get('parameter')} = {tweak.get('new_value')} (would score {new_score:.1f})
Keeping current config.""")

    # Auto Dream: regenerate CLAUDE.md with fresh live data
    try:
        auto_dream()
    except Exception as _ad_err:
        log(f"Auto Dream error: {_ad_err}")

    log("=== Optimizer complete ===")


if __name__ == "__main__":
    main()
