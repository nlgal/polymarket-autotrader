#!/usr/bin/env python3
"""
Polymarket Auto-Trader  —  v2 with Rule-Based Control Plane
============================================================
Control plane sits above all trade signals and adjusts risk budgets,
position limits, and allowed strategies based on equity state.

Modes (driven by drawdown from peak equity):
  NORMAL     — default, balanced risk
  RECOVERY   — drawdown >= 10%, tighter limits, no speculative trades
  EXPANSION  — new equity high >= checkpoint * 1.10, larger limits
  PAUSED     — drawdown >= 20% OR daily hard stop hit; no new trades

State persists to state.json so it survives reboots.
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone, date

import requests
from colorama import Fore, Style, init
from dotenv import load_dotenv

# ── Load env from explicit path so launchd can find it ────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)
init()

# ── Telegram Notifications ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def tg(msg: str, silent: bool = False):
    """Send a Telegram message. Fails silently so it never breaks the agent."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_notification": silent,
            },
            timeout=5,
        )
    except Exception:
        pass

# ── Static Config ─────────────────────────────────────────────────────────────

# ── Intelligence System ───────────────────────────────────────────────────────
INTEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intelligence")
# ── Market assessment cache ───────────────────────────────────────────────────
MARKET_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intelligence", "market_cache.json")
MARKET_CACHE_TTL  = 7200   # seconds — reuse cached score if price unchanged for 2h
_market_cache: dict = {}

def _load_market_cache():
    """Load market cache from disk into memory."""
    global _market_cache
    try:
        if os.path.exists(MARKET_CACHE_FILE):
            _market_cache = json.load(open(MARKET_CACHE_FILE))
    except Exception:
        _market_cache = {}

def _save_market_cache():
    """Persist market cache to disk."""
    try:
        os.makedirs(os.path.dirname(MARKET_CACHE_FILE), exist_ok=True)
        # Prune entries older than 24h to keep file small
        cutoff = time.time() - 86400
        pruned = {k: v for k, v in _market_cache.items() if v.get("ts", 0) > cutoff}
        _market_cache.clear()
        _market_cache.update(pruned)
        json.dump(_market_cache, open(MARKET_CACHE_FILE, "w"))
    except Exception:
        pass

def _get_cached_score(market):
    """Return cached score if valid (same price, within TTL). None otherwise."""
    key = market.get("condition_id") or market.get("question", "")[:80]
    entry = _market_cache.get(key)
    if not entry:
        return None
    age = time.time() - entry.get("ts", 0)
    cached_price = entry.get("yes_price")
    current_price = market.get("yes_price")
    if age < MARKET_CACHE_TTL and abs((cached_price or 0) - (current_price or 0)) < 0.005:
        return entry.get("result")
    return None

def _set_cached_score(market, result):
    """Store a market score in the cache."""
    key = market.get("condition_id") or market.get("question", "")[:80]
    _market_cache[key] = {
        "ts": time.time(),
        "yes_price": market.get("yes_price"),
        "result": result,
    }



_INTEL_CACHE: dict = {"soul": None, "lessons": None, "hard_rules": None}

def _read_intel_files():
    """Read soul.md + lessons.md from disk into cache. Call once per cycle."""
    try:
        p = os.path.join(INTEL_DIR, "soul.md")
        txt = open(p).read() if os.path.exists(p) else ""
        lines = [l.strip() for l in txt.splitlines() if l.strip() and l.strip()[0].isdigit()]
        _INTEL_CACHE["soul"] = "\n".join(lines[:10])
    except Exception:
        _INTEL_CACHE["soul"] = ""
    try:
        p = os.path.join(INTEL_DIR, "lessons.md")
        txt = open(p).read() if os.path.exists(p) else ""
        lines = [l.strip() for l in txt.splitlines()
                 if l.strip() and not l.strip().startswith("#") and len(l.strip()) > 20]
        _INTEL_CACHE["lessons"] = "\n".join(lines[:12])
    except Exception:
        _INTEL_CACHE["lessons"] = ""
    try:
        _hr_path = os.path.join(INTEL_DIR, "HARD_RULES.md")
        _INTEL_CACHE["hard_rules"] = open(_hr_path).read()[:4000] if os.path.exists(_hr_path) else ""
    except Exception:
        _INTEL_CACHE["hard_rules"] = ""


def load_hard_rules() -> str:
    """Load HARD_RULES.md — machine-readable guardrails that override all signals."""
    if _INTEL_CACHE["hard_rules"] is None: _read_intel_files()
    return _INTEL_CACHE["hard_rules"] or ""


def load_lessons() -> str:
    if _INTEL_CACHE["lessons"] is None: _read_intel_files()
    return _INTEL_CACHE["lessons"] or ""

def load_soul() -> str:
    if _INTEL_CACHE["soul"] is None: _read_intel_files()
    return _INTEL_CACHE["soul"] or ""

def log_mistake(category: str, what: str, why: str, rule: str):
    """Append a mistake to intelligence/mistakes.md and notify via Telegram."""
    try:
        os.makedirs(INTEL_DIR, exist_ok=True)
        path = os.path.join(INTEL_DIR, "mistakes.md")
        from datetime import datetime as _dt
        ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        existing = open(path).read() if os.path.exists(path) else ""
        mid = existing.count("## M") + 1
        entry = (
            "\n## M{:03d} — {}\n"
            "**Time:** {}\n"
            "**What:** {}\n"
            "**Why:** {}\n"
            "**Rule:** {}\n\n---\n"
        ).format(mid, category, ts, what[:200], why[:200], rule[:200])
        with open(path, "a") as fh:
            fh.write(entry)
        tg("\U0001f4dd Mistake logged M{:03d}: {}".format(mid, category), silent=True)
    except Exception:
        pass

def review_patterns():
    """Scan mistakes.md for recurring patterns and update lessons.md."""
    try:
        mistakes_path = os.path.join(INTEL_DIR, "mistakes.md")
        lessons_path  = os.path.join(INTEL_DIR, "lessons.md")
        if not os.path.exists(mistakes_path):
            return
        mistakes_txt = open(mistakes_path).read()
        lessons_txt  = open(lessons_path).read() if os.path.exists(lessons_path) else ""
        pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        if not pplx_key:
            return
        import requests as _req
        from datetime import datetime as _dt
        resp = _req.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": "sonar",
                "messages": [{
                    "role": "user",
                    "content": (
                        "Analyze this trading agent mistake log. Find recurring patterns "
                        "(same root cause 2+ times). Suggest 1-3 new lessons not already "
                        "in the current list. Format: NEW LESSON: [title] | [rule]\n\n"
                        "MISTAKES:\n" + mistakes_txt[-3000:] +
                        "\n\nCURRENT LESSONS:\n" + lessons_txt[-1000:]
                    )
                }],
                "max_tokens": 400,
                "temperature": 0.2,
            },
            headers={"Authorization": "Bearer " + pplx_key, "Content-Type": "application/json"},
            timeout=30,
        )
        analysis = resp.json()["choices"][0]["message"]["content"]
        if "NEW LESSON:" in analysis:
            with open(lessons_path, "a") as fh:
                fh.write("\n\n---\n## Auto-generated — {}\n{}\n".format(
                    _dt.utcnow().strftime("%Y-%m-%d"), analysis))
            log("[INTELLIGENCE] Pattern review — new lessons added", Fore.MAGENTA)
            tg("\U0001f9e0 Intelligence update: new lessons added\n" + analysis[:200])
    except Exception as e:
        log("[INTELLIGENCE] Pattern review failed: {}".format(e), Fore.YELLOW)


# =============================================================================
#  SELF-LEARNING SYSTEM — autonomous research loop
#  Architecture: log outcomes -> record resolutions -> Claude reflects -> tune
# =============================================================================

def _load_results() -> list:
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_results(results: list):
    try:
        os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2)
    except Exception as e:
        log(f"[LEARN] Could not save results: {e}", Fore.YELLOW)

def _classify_market(question: str) -> str:
    q = question.lower()
    if any(k in q for k in ["bitcoin", "ethereum", "crypto", "btc", "eth"]):
        return "crypto"
    if any(k in q for k in ["oil", "gold", "silver", "commodity"]):
        return "commodity"
    if any(k in q for k in ["iran", "war", "military", "nato", "russia", "ukraine", "israel", "china"]):
        return "geopolitical"
    if any(k in q for k in ["trump", "election", "president", "congress", "senate", "fed", "policy"]):
        return "politics"
    if any(k in q for k in ["greenland", "annex", "acquire"]):
        return "geopolitical"
    return "other"

def log_trade_outcome(market: dict, action: str, size_usdc: float, edge: float, receipt: dict):
    """Record a placed trade with full reasoning chain for self-learning."""
    try:
        results = _load_results()
        src_count = market.get("_source_count", 1)
        entry = {
            # ── Identity ──────────────────────────────────────────────────────
            "ts_placed":    datetime.now(timezone.utc).isoformat(),
            "question":     market.get("question", "")[:120],
            "action":       action,
            "market_type":  _classify_market(market.get("question", "")),
            "order_id":     receipt.get("orderID", ""),
            "token_id":     market.get("yes_token_id", "") if action == "BUY_YES" else market.get("no_token_id", ""),
            # ── Pricing at entry ───────────────────────────────────────────────
            "edge":         round(edge, 4),
            "size_usdc":    round(size_usdc, 2),
            "yes_price":    market.get("yes_price", 0.5),
            "no_price":     market.get("no_price", 0.5),
            "true_prob":    market.get("true_probability", None),
            "confidence":   market.get("confidence", ""),
            # ── Reasoning chain (the trading-as-git commit message) ────────────
            "reasoning":    market.get("reasoning", "")[:300],
            "source_count": src_count,
            "sources_used": {
                "perplexity": market.get("_has_pplx", False),
                "rss":        market.get("_has_rss", False),
                "uw":         market.get("_has_uw", False),
            },
            "uw_boost":     market.get("_uw_boosted", False),  # did UW boost the edge?
            "volume_24h":   market.get("volume", 0),
            "end_date":     market.get("end_date", ""),
            "mode":         market.get("_mode", "NORMAL"),
            # ── Outcome (filled in later) ──────────────────────────────────────
            "status":       "open",
            "pnl":          None,
            "resolved_at":  None,
            "return_pct":   None,
        }
        results.append(entry)
        _save_results(results)
        log(f"[LEARN] Trade logged: {action} {src_count}-src edge={edge:+.3f} conf={entry['confidence']} ${size_usdc:.0f}", Fore.MAGENTA)
    except Exception as e:
        log(f"[LEARN] log_trade_outcome error: {e}", Fore.YELLOW)

def record_resolved_trades():
    """Match recent REDEEMs against open trades, record final P&L."""
    try:
        results = _load_results()
        open_trades = [r for r in results if r.get("status") == "open"]
        if not open_trades:
            return
        resp = requests.get(
            f"https://data-api.polymarket.com/activity?user={FUNDER}&limit=50",
            timeout=10
        )
        if resp.status_code != 200:
            return
        activity = resp.json()
        redeems = [a for a in activity if a.get("type") in ("REDEEM", "SELL")]
        changed = False
        for trade in results:
            if trade.get("status") != "open":
                continue
            q_lower = trade["question"].lower()
            for r in redeems:
                r_title = r.get("title", "").lower()
                if len(q_lower) > 10 and (q_lower[:40] in r_title or r_title[:40] in q_lower):
                    usdc = r.get("usdcSize", 0) or 0
                    cost = trade["size_usdc"]
                    pnl  = round(usdc - cost, 2)
                    trade["status"]      = "resolved"
                    trade["pnl"]         = pnl
                    trade["resolved_at"] = datetime.fromtimestamp(
                        r["timestamp"], tz=timezone.utc).isoformat()
                    trade["return_pct"]  = round(pnl / cost * 100, 1) if cost > 0 else 0
                    changed = True
                    _q = trade["question"]; _rp = trade.get("return_pct", 0)
                    log(f"[LEARN] Resolved: {_q[:50]} | P&L ${pnl:+.2f} ({_rp:+.1f}%)", Fore.MAGENTA)
                    break
        if changed:
            _save_results(results)
    except Exception as e:
        log(f"[LEARN] record_resolved_trades error: {e}", Fore.YELLOW)

def reflect_and_improve():
    """Reflect on resolved trades, rewrite lessons.md, auto-tune MIN_EDGE."""
    try:
        results  = _load_results()
        resolved = [r for r in results if r.get("status") == "resolved" and r.get("pnl") is not None]
        if len(resolved) < 10:
            return
        state = load_state()
        last_count = state.get("last_reflect_count", 0)
        if len(resolved) < last_count + 5:
            return
        log(f"[LEARN] Reflecting on {len(resolved)} resolved trades...", Fore.MAGENTA)

        by_cat  = {}
        by_edge = {}
        wins = 0
        for r in resolved:
            pnl  = r.get("pnl", 0) or 0
            won  = pnl > 0
            wins += 1 if won else 0
            cat  = r.get("market_type", "other")
            edge = abs(r.get("edge", 0))
            ebuck = str(int(edge * 100 // 5) * 5) + "pct"
            by_cat.setdefault(cat,  {"w": 0, "l": 0, "pnl": 0.0})
            by_edge.setdefault(ebuck, {"w": 0, "l": 0, "pnl": 0.0, "min_edge": int(edge * 100 // 5) * 5})
            key = "w" if won else "l"
            by_cat[cat][key]   += 1
            by_cat[cat]["pnl"] += pnl
            by_edge[ebuck][key]   += 1
            by_edge[ebuck]["pnl"] += pnl

        total_pnl = sum(r.get("pnl", 0) or 0 for r in resolved)
        win_rate  = wins / len(resolved) * 100
        recent_10 = sorted(resolved, key=lambda x: x.get("resolved_at", ""), reverse=True)[:10]

        # Build source conviction breakdown
        by_src = {1: {"w": 0, "l": 0, "pnl": 0.0}, 2: {"w": 0, "l": 0, "pnl": 0.0}, 3: {"w": 0, "l": 0, "pnl": 0.0}}
        by_conf = {}
        for r in resolved:
            pnl_r = r.get("pnl", 0) or 0
            sc = min(r.get("source_count", 1), 3)
            key_r = "w" if pnl_r > 0 else "l"
            by_src[sc][key_r] += 1
            by_src[sc]["pnl"] += pnl_r
            conf = r.get("confidence", "unknown")
            by_conf.setdefault(conf, {"w": 0, "l": 0, "pnl": 0.0})
            by_conf[conf][key_r] += 1
            by_conf[conf]["pnl"] += pnl_r

        lines = [
            f"TRADE PERFORMANCE SUMMARY ({len(resolved)} resolved trades)",
            f"Win rate: {win_rate:.1f}% | Total P&L: ${total_pnl:+.2f}",
            "",
            "BY SOURCE COUNT (conviction):",
        ]
        for sc_k in [1, 2, 3]:
            s = by_src[sc_k]
            total_sc = s["w"] + s["l"]
            wr_sc = s["w"] / total_sc * 100 if total_sc else 0
            lines.append(f"  {sc_k}-source: {wr_sc:.0f}% win ({s['w']}W/{s['l']}L) P&L ${s['pnl']:+.2f}")
        lines.append("")
        lines.append("BY CONFIDENCE LEVEL:")
        for conf_k, s in by_conf.items():
            total_ck = s["w"] + s["l"]
            wr_ck = s["w"] / total_ck * 100 if total_ck else 0
            lines.append(f"  {conf_k}: {wr_ck:.0f}% win ({s['w']}W/{s['l']}L) P&L ${s['pnl']:+.2f}")
        lines.append("")
        lines.append("BY MARKET TYPE:")
        for cat, s in by_cat.items():
            total_c = s["w"] + s["l"]
            wr_c = s["w"] / total_c * 100 if total_c else 0
            w_c = s["w"]; l_c = s["l"]; pnl_c = s["pnl"]
            lines.append(f"  {cat}: {wr_c:.0f}% win ({w_c}W/{l_c}L) P&L ${pnl_c:+.2f}")
        lines.append("")
        lines.append("BY EDGE BUCKET:")
        for bk, s in sorted(by_edge.items()):
            total_b = s["w"] + s["l"]
            wr_b = s["w"] / total_b * 100 if total_b else 0
            me = s["min_edge"]; w_b = s["w"]; l_b = s["l"]; pnl_b = s["pnl"]
            lines.append(f"  edge ~{me}%: {wr_b:.0f}% win ({w_b}W/{l_b}L) P&L ${pnl_b:+.2f}")
        lines.append("")
        lines.append("RECENT 10 TRADES:")
        for r in recent_10:
            mt = r.get("market_type", "?"); ed = r.get("edge", 0)
            sz = r.get("size_usdc", 0); pl = r.get("pnl", 0); qq = r.get("question", "")[:60]
            lines.append(f"  [{mt}] edge={ed:+.3f} ${sz:.0f} P&L ${pl:+.2f} | {qq}")
        summary = "\n".join(lines)

        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return
        lessons_path    = os.path.join(INTEL_DIR, "lessons.md")
        current_lessons = open(lessons_path).read() if os.path.exists(lessons_path) else ""
        prompt = (
            "You are a quant strategist reviewing a prediction market bot performance.\n"
            "Analyze the data and produce updated lessons.md content.\n\n"
            + summary + "\n\nCURRENT LESSONS:\n" + current_lessons[-2000:] + "\n\n"
            "Write NEW lessons.md. Be specific and data-driven. Sections:\n"
            "## WHAT TO BUY\n## WHAT TO AVOID\n## EDGE CALIBRATION\n"
            "## SIZING RULES\n## PATTERN ALERTS\n"
            "If win rate <40% for a type, say AVOID. Max 600 words."
        )
        if not hasattr(score_market, "_ac") or score_market._ac is None:
            score_market._ac = anthropic.Anthropic(api_key=api_key)
        resp = score_market._ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        _track_usage(resp)
        new_lessons = resp.content[0].text.strip()
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(lessons_path, "w") as fh:
            fh.write(f"# Learned Lessons\n*Auto-generated {ts_now} from {len(resolved)} resolved trades*\n\n")
            fh.write(new_lessons)
        log(f"[LEARN] lessons.md rewritten from {len(resolved)} trades ({win_rate:.0f}% win rate)", Fore.MAGENTA)
        tg(f"\U0001f9e0 <b>Self-learning update</b>\n{len(resolved)} trades analyzed ({win_rate:.0f}% win rate, ${total_pnl:+.2f} P&L)\nlessons.md updated.")
        _auto_tune_min_edge(by_edge)
        state["last_reflect_count"] = len(resolved)
        save_state(state)
    except Exception as e:
        log(f"[LEARN] reflect_and_improve error: {e}", Fore.YELLOW)

def _auto_tune_min_edge(by_edge: dict):
    """Auto-tune MIN_EDGE based on which edge buckets are empirically profitable."""
    global MIN_EDGE
    try:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        profitable = []
        for bk, s in by_edge.items():
            total = s["w"] + s["l"]
            if total < 3:
                continue
            wr = s["w"] / total
            if wr >= 0.55 and s["pnl"] > 0:
                profitable.append(s["min_edge"] / 100)
        if not profitable:
            log("[LEARN] Not enough data to tune MIN_EDGE", Fore.MAGENTA)
            return
        optimal = max(0.06, min(0.15, min(profitable)))
        current = MIN_EDGE
        if abs(optimal - current) < 0.005:
            return
        if os.path.exists(env_path):
            env_content = open(env_path).read()
            if f"MIN_EDGE={current}" in env_content:
                env_content = env_content.replace(f"MIN_EDGE={current}", f"MIN_EDGE={optimal}")
                with open(env_path, "w") as f:
                    f.write(env_content)
        MIN_EDGE = optimal
        log(f"[LEARN] MIN_EDGE auto-tuned: {current:.3f} -> {optimal:.3f}", Fore.MAGENTA)
        tg(f"\u2699\ufe0f <b>Edge threshold tuned</b>: MIN_EDGE {current:.3f} \u2192 {optimal:.3f}")
    except Exception as e:
        log(f"[LEARN] _auto_tune_min_edge error: {e}", Fore.YELLOW)

SCAN_INTERVAL_SECONDS  = 15 * 60
NEWS_SCAN_INTERVAL     = 5 * 60   # News arb check every 5 minutes
NEWS_ARB_MIN_EDGE      = 0.12     # Higher bar for news arb trades (more confident)
NEWS_ARB_SIZE_MULT     = 1.5      # Size up news arb trades vs normal
MIN_EDGE              = 0.07
MIN_EDGE_NET_FEE      = 0.09  # Net-of-fee minimum for fee-enabled markets (crypto/sports)
MIN_CONFIDENCE        = "high"
MARKETS_FETCH_LIMIT   = 200   # Total markets to pull per scan (by volume)
TOP_MARKETS_TO_SCORE  = 30   # How many top-volume to score with AI
ORDER_TTL_MINUTES     = 20
PROFIT_TARGET         = 0.80
STOP_LOSS             = 0.35
NEAR_RESOLUTION_THRESHOLD = 0.99  # Only sell when essentially resolved (was 0.94 — sold Spain/Duke at 92c)
PROFIT_LOCK_GAIN      = 0.40    # Sell half when unrealized gain on NO position ≥ 40%

# ── Three-Bucket Operating Model ──────────────────────────────────────────────────
#   SPORTS       = grind edge        scale slowly, repeatable, DK picks drive entry
#   GEO_POLITICS = asymmetric edge   stay small, allow fat-tail upside, don't oversize
#   CALENDAR     = timing edge       HARD CAP on loss — most dangerous bucket

BUCKET_GEO_MAX_USDC        = 150  # max single geo/politics position
BUCKET_CALENDAR_MAX_LOSS   = 200  # auto-sell if calendar spread position unrealized loss > this
BUCKET_SPORTS_SCALE_FACTOR = 1.0  # relative to normal Kelly (1.0 = no boost; grow slowly)

# Calendar spread detection: title contains a date + "extended by" or "by April/May/June"
import re as _re_bucket
_CAL_PATTERN = _re_bucket.compile(
    r'(extended by|by (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|ceasefire.*by|deal.*by)',
    _re_bucket.IGNORECASE
)

def classify_bucket(title: str) -> str:
    """Classify a market title into SPORTS | GEO | CALENDAR."""
    t = title.lower()
    if _CAL_PATTERN.search(t):
        return 'CALENDAR'
    sports_kw = ['nba','nfl','mlb','nhl','vs.','fc','soccer','ufc','mma',
                 'cavaliers','pistons','lakers','celtics','yankees','red sox',
                 'dodgers','76ers','pacers','spurs','nuggets','warriors','thunder',
                 'maverick','knicks','bucks','nets','bulls','hawks','hornets',
                 'pelicans','grizzlies','timberwolves','blazers','raptors']
    if any(k in t for k in sports_kw):
        return 'SPORTS'
    geo_kw = ['iran','ukraine','russia','china','taiwan','israel','hungary',
              'peru','pakistan','ceasefire','invasion','nuclear','peace deal',
              'regime','election','invasion','diplomatic']
    if any(k in t for k in geo_kw):
        return 'GEO'
    return 'GEO'  # default: treat unknown as geo (smaller size)
PROFIT_LOCK_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profit_locks.json")
MAX_PER_MARKET_USDC   = 200   # Never put more than $200 into a single market
MIN_FREE_BALANCE      = 20    # Always keep $20 free (Polymarket minimum)

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com/markets"
FUNDER     = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
UW_API_KEY  = os.environ.get("UW_API_KEY", "").strip()
UW_API_BASE = "https://api.unusualwhales.com/api"

LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.log")
STATE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intelligence", "results.json")

# ── Control Plane Parameters (Balanced profile) ───────────────────────────────
#
#   Mode        Risk/trade   Max open risk   Max orders  Trade size range
#   NORMAL       1.0% E       3.0% E           8          $50–$150
#   RECOVERY     0.5% E       1.5% E           4          $25–$75
#   EXPANSION    1.25% E      4.0% E           10         $75–$200
#   PAUSED       —            —                0          no new trades
#
RISK_TRADE_PCT  = {"NORMAL": 0.010, "RECOVERY": 0.005, "EXPANSION": 0.0125, "PAUSED": 0.0}
RISK_OPEN_PCT   = {"NORMAL": 0.030, "RECOVERY": 0.015, "EXPANSION": 0.040,  "PAUSED": 0.0}
MAX_ORDERS      = {"NORMAL": 8,     "RECOVERY": 4,     "EXPANSION": 10,     "PAUSED": 0}
# ── Market blacklist — skip markets with no real model edge ──────────────────
MARKET_BLACKLIST_KEYWORDS = [
    # Social media noise — no real model edge
    "tweets", "tweet", "retweet", "elon musk post",
    "how many times will elon", "posts from march", "# of tweets",
    "number of tweets", "followers", "subscribers",
    "youtube views", "tiktok", "instagram posts",
    # In-play / game-day markets — always blocked regardless of sports policy
    "o/u ", "over/under", "spread", "anytime goalscorer",
    "win on 2026-", "win on 2025-",   # game-day win markets
    # Token launch / FDV markets — NEVER trade these.
    # Lesson: Backpack FDV $200M NO cost $122. Token launched at $2B FDV,
    # market showed 46¢ with ghost order book (bids 0.001/asks 0.999).
    # These markets resolve instantly on launch day with no real order flow.
    "fdv above", "fdv below", "fdv over", "fdv under",
    "fully diluted valuation", "tge", "token generation event",
    "day after launch", "on launch day", "market cap on launch",
    "price on launch", "token price",
]  # Social/noise blacklist — sports handled separately by is_sports_market()

# Sports keyword detector — used to route markets through sports policy checks
SPORTS_KEYWORDS = [
    "nba", "nhl", "mlb", "ncaa", "nfl", "mls",
    "march madness", "college basketball", "college football",
    "world baseball classic", "stanley cup", "super bowl", "world series",
    "nba finals", "nhl playoffs", "nba playoffs",
    "premier league", "la liga", "serie a", "ligue 1", "bundesliga",
    "wimbledon", "french open", "us open tennis", "australian open",
    # Soccer clubs (Premier League, Champions League, international)
    "manchester city", "manchester united", "liverpool fc", "chelsea fc",
    "arsenal fc", "tottenham", "newcastle", "aston villa", "west ham",
    "barcelona", "real madrid", "atletico madrid", "sevilla",
    "juventus", "inter milan", "ac milan", "napoli", "roma",
    "psg", "paris saint-germain", "lyon", "marseille",
    "bayern munich", "dortmund", "rb leipzig", "bayer leverkusen",
    "ajax", "benfica", "porto", "celtic", "rangers",
    "champions league", "europa league", "conference league",
    "fa cup", "carabao cup", "copa del rey", "dfb-pokal",
    # FIFA World Cup 2026 national teams (all 48 qualified nations + likely qualifiers)
    "ivory coast", "côte d'ivoire", "cote d'ivoire",
    "brazil", "argentina", "france", "germany", "england",
    "portugal", "spain", "netherlands", "italy", "belgium",
    "senegal", "morocco", "japan", "south korea", "australia",
    "mexico", "usa", "canada", "colombia", "ecuador", "uruguay",
    "nigeria", "ghana", "cameroon", "egypt", "algeria", "tunisia",
    "saudi arabia", "iran", "qatar", "south africa",
    "new zealand", "costa rica", "jamaica", "panama",
    "peru", "chile", "bolivia", "venezuela", "paraguay",
    "denmark", "austria", "switzerland", "poland", "croatia",
    "serbia", "ukraine", "hungary", "czech republic", "slovakia",
    "scotland", "wales", "turkey", "greece", "norway", "sweden",
    "win the 2026 fifa", "win the 2026 world cup",
    "world cup winner", "world cup champion",
    # Generic match patterns — catches "Will X FC win on YYYY-MM-DD?"
    " fc win", "win on 202", "vs.", " o/u ", "over/under",
    "match winner", "both teams to score", "clean sheet",
    # Tennis / golf / cycling / formula 1
    "formula 1", "f1 grand prix", "tour de france", "giro d'italia",
    "pga tour", "lpga", "ryder cup", "masters tournament",
    "atp ", "wta ",
    # European soccer — clubs missed in first pass (Braga, Sassuolo, Córdoba, etc.)
    "braga", "sassuolo", "calcio", "córdoba", "cordoba",
    "inter miami", "lafc", "seattle sounders", "portland timbers",
    "sporting cp", "benfica", "porto", "vitoria",
    "fiorentina", "lazio", "torino", "genoa", "monza", "lecce",
    "nantes", "strasbourg", "lens", "rennes", "nice", "monaco",
    "werder", "hoffenheim", "wolfsburg", "freiburg", "union berlin",
    "galatasaray", "fenerbahce", "besiktas", "trabzonspor",
    "anderlecht", "club brugge", "psv", "feyenoord",
    "celtic", "rangers", "hearts", "hibernian",
    "shakhtar", "dynamo kyiv", "rapid vienna", "sturm graz",
    "red star", "dinamo zagreb", "slavia prague", "sparta prague",
    "europa league", "conference league", "champions league",
    "copa del rey", "dfb-pokal", "fa cup", "carabao cup", "coupe de france",
    "coppa italia", "supercoppa",
    # Esports — LoL, CSGO, Valorant, Dota (fee market, must be caught)
    "natus vincere", "team vitality", "team liquid", "cloud9", "fnatic",
    "faze clan", "navi", "g2 esports", "astralis", "ninjas in pyjamas",
    "league of legends", "lec", "lcs", "lck", "lpl",
    "counter-strike", "cs2", "csgo", "valorant", "dota 2",
    "bo3", "bo5", "map winner", "round winner",
    # Box office / entertainment (fee market, not geopolitical)
    "opening weekend", "box office", "rotten tomatoes", "golden globe",
    "academy award", "oscar", "emmy", "grammy", "billboard",
    "album sales", "streaming", "box office",
    # Reality TV / social media (already in blacklist but add here too)
    "bachelor", "survivor", "big brother", "the voice", "idol",
    "youtube", "tiktok", "instagram", "twitter followers",
    "subscribers", "views",
    # Crypto price markets (fee 1.8% — must route through fee guard)
    "bitcoin", "ethereum", "btc", "eth", "sol", "solana",
    "xrp", "bnb", "doge", "dogecoin", "shib", "avax", "ada",
    "coinbase", "binance", "crypto market cap",
    "will btc", "will eth", "will bitcoin", "will ethereum",
    # Finance / macro (fee market)
    "s&p 500", "nasdaq", "dow jones", "sp500", "fed rate",
    "interest rate", "cpi ", "inflation rate", "gdp growth",
    "recession", "yield curve", "10-year treasury",
    "oil price", "wti", "brent crude", "gold price",
    # Weather
    "hurricane", "tornado", "earthquake", "wildfire",
    "inches of snow", "temperature record",
    # NBA teams
    "timberwolves", "warriors", "lakers", "celtics", "bulls", "clippers",
    "knicks", "pacers", "bucks", "cavaliers", "76ers", "nuggets", "spurs",
    "kings", "heat", "hornets", "thunder", "blazers", "grizzlies",
    "suns", "mavericks", "pistons", "wizards", "hawks", "magic", "raptors",
    "jazz", "pelicans", "rockets",
    # NHL teams
    "lightning", "kraken", "penguins", "hurricanes", "rangers", "bruins",
    "maple leafs", "canadiens", "blackhawks", "flames", "oilers", "canucks",
    "avalanche", "golden knights", "predators", "blues", "wild", "jets",
    "flyers", "sabres", "senators", "coyotes", "ducks",
    # MLB teams
    "yankees", "red sox", "dodgers", "cubs", "mets", "braves", "astros",
    # NFL teams
    "chiefs", "eagles", "cowboys", "patriots", "49ers", "bills", "ravens",
    # Soccer clubs
    "man city", "manchester city", "manchester united", "arsenal",
    "chelsea", "liverpool", "tottenham", "barcelona", "real madrid",
    "galatasaray", "atletico madrid", "augsburg", "stuttgart", "mainz",
    "eintracht",
    # Tennis players
    "zverev", "djokovic", "alcaraz", "sinner",
    # College / other
    "howard bison",
]

def is_blacklisted(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in MARKET_BLACKLIST_KEYWORDS)

# ── Category Whitelist ──────────────────────────────────────────────────────────
# Only trade markets in these approved categories. Everything else is PASS.
# This replaces the catch-all blacklist approach. 108 "OTHER" trades analyzed:
# Elon tweet counts, random soccer, one-off events = -$800 net.
# Explicit allow-list ensures every trade fits our proven edge categories.
CATEGORY_WHITELIST_KEYWORDS = [
    # Geopolitical / conflict
    "iran", "ceasefire", "conflict ends", "forces enter", "regime", "invasion",
    "ukraine", "russia", "nato", "war", "peace deal", "sanctions",
    "north korea", "china", "taiwan", "middle east", "nuclear",
    # Commodity / macro price
    "crude oil", "brent", "wti", "oil price",
    "gold", "silver", "copper",
    "natural gas", "lng",
    # Macro / economic
    "fed ", "federal reserve", "interest rate", "fomc", "rate cut", "rate hike",
    "inflation", "cpi", "gdp", "recession", "unemployment",
    "ecb", "bank of england", "boj",
    # Crypto with macro catalyst
    "bitcoin", "btc", "ethereum", "eth",
    # Weather / temperature (handled by weather scout but allow scoring too)
    "highest temperature", "lowest temperature", "temperature in",
    # Political resolution (US + major)
    "president", "election", "congress", "senate", "supreme court",
    "trump", "harris", "biden", "zelensky", "putin", "netanyahu",
    # Regulatory / major corporate
    "sec ", "doj ", "fda ", "antitrust", "merger", "ipo",
    "tariff", "trade war",
]

def is_approved_category(question: str) -> bool:
    """
    Returns True if the market belongs to an approved trading category.
    Markets that don't match any whitelist keyword are skipped (PASS).
    Sports markets are handled separately by is_sports_market() and the
    sports policy gate — they do NOT need to pass this whitelist.
    """
    q = question.lower()
    return any(kw in q for kw in CATEGORY_WHITELIST_KEYWORDS)

def is_sports_market(question: str) -> bool:
    """Returns True if the market is a sports market (routes through sports policy)."""
    q = question.lower()
    return any(kw in q for kw in SPORTS_KEYWORDS)

# Sports circuit-breaker state (persisted inside main state.json via key 'sports')
# Structure: {
#   "daily_pnl": float,       # sports PnL today
#   "daily_date": str,        # YYYY-MM-DD of daily_pnl
#   "weekly_pnl": float,      # sports PnL this week
#   "week_start": str,        # ISO date of week start
#   "consec_losses": int,     # consecutive sports losses
#   "disabled_until": float,  # unix timestamp — 0 = not disabled
#   "halved_until": float,    # unix timestamp — 0 = not halved
#   "per_sport": {},          # {sport_name: exposure_usdc}
#   "exposure_total": float,  # total open sports exposure
#   "daily_spend": float,     # USDC spent on sports today (hard cap: $300/day)
# }

# Hard daily spend cap for sports — autotrader cannot spend more than this per day
# regardless of equity or edge. DK picks via live_sports_trader bypass this.
SPORTS_DAILY_SPEND_HARD_CAP = 300.0

def get_sports_state(state: dict) -> dict:
    """Get (or initialize) sports sub-state from main state dict."""
    if "sports" not in state:
        state["sports"] = {
            "daily_pnl": 0.0,
            "daily_date": "",
            "weekly_pnl": 0.0,
            "week_start": "",
            "consec_losses": 0,
            "disabled_until": 0.0,
            "halved_until": 0.0,
            "per_sport": {},
            "exposure_total": 0.0,
            "daily_spend": 0.0,
        }
    return state["sports"]

def check_sports_eligibility(market: dict, state: dict, equity: float) -> tuple:
    """
    Enforce sports policy rules S1-S17 before scoring.
    Returns (allowed: bool, reason: str).
    Market dict must have: volume (24h), yes_price, no_price, end_date.
    """
    import time as _t
    from datetime import datetime as _dt, timezone as _tz

    ss = get_sports_state(state)
    now = _t.time()

    # Reset daily PnL and daily spend if new day
    today = _dt.utcnow().date().isoformat()
    if ss.get("daily_date") != today:
        ss["daily_pnl"] = 0.0
        ss["daily_spend"] = 0.0
        ss["daily_date"] = today

    # Reset weekly PnL if new week (Monday)
    from datetime import date as _date
    today_d = _date.today()
    week_start = (today_d - __import__('datetime').timedelta(days=today_d.weekday())).isoformat()
    if ss.get("week_start") != week_start:
        ss["weekly_pnl"] = 0.0
        ss["week_start"] = week_start

    # [S14/S15/S16/S17] Circuit breakers: is sports disabled?
    if ss.get("disabled_until", 0) > now:
        remaining_h = (ss["disabled_until"] - now) / 3600
        return False, f"Sports disabled for {remaining_h:.1f}h more (circuit breaker)"

    # Check daily DD thresholds
    if equity > 0:
        daily_dd_pct = abs(min(0, ss.get("daily_pnl", 0))) / equity
        if daily_dd_pct >= 0.035:
            # Disable for 24h
            ss["disabled_until"] = now + 86400
            return False, f"Sports daily DD {daily_dd_pct*100:.1f}% >= 3.5% — disabled 24h"

    # Check weekly DD
    if equity > 0:
        weekly_dd_pct = abs(min(0, ss.get("weekly_pnl", 0))) / equity
        if weekly_dd_pct >= 0.06:
            ss["disabled_until"] = now + 7 * 86400
            return False, f"Sports weekly DD {weekly_dd_pct*100:.1f}% >= 6% — disabled 7 days"

    # [S17] Consecutive losses
    if ss.get("consec_losses", 0) >= 5:
        if ss.get("disabled_until", 0) == 0:  # not already set
            ss["disabled_until"] = now + 2 * 86400
        return False, f"Sports: 5 consecutive losses — 48h cooldown"

    # [S2] Liquidity check
    volume = float(market.get("volume", 0) or 0)
    if volume < 5000:
        return False, f"Sports liquidity too low: ${volume:,.0f} 24h vol (min $5,000)"

    # [S1] Timing: market end_date as proxy — require end_date > 24h from now
    # (Polymarket doesn't expose game_start; end_date is close enough for the filter)
    end_date_str = market.get("end_date", "")
    if end_date_str:
        try:
            end_dt = _dt.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_to_end = (end_dt - _dt.now(_tz.utc)).total_seconds() / 3600
            if hours_to_end < 24:
                return False, f"Sports market ends in {hours_to_end:.0f}h — too close to game time"
        except Exception:
            pass  # If we can't parse, don't block

    # [S5] YES price floor
    yes_price = float(market.get("yes_price", 0) or 0)
    no_price  = float(market.get("no_price", 0) or 0)
    if yes_price < 0.25 and yes_price > 0:  # potential BUY_YES
        return False, f"Sports YES price {yes_price:.3f} < 0.25 — lottery ticket, no edge"

    # [S6] NO price ceiling (tighter than global 0.82 guard)
    if no_price > 0.75:
        return False, f"Sports NO price {no_price:.3f} > 0.75 — market already decided favorite"

    # [S10] Total sports exposure cap: 10% of equity
    max_sports_total = equity * 0.10
    if ss.get("exposure_total", 0) >= max_sports_total:
        return False, f"Sports total exposure ${ss['exposure_total']:.0f} >= 10% of equity (${max_sports_total:.0f})"

    # [S18] Hard daily spend cap — autotrader cannot spend > $300/day on sports
    # This protects against runaway sports buys from scoreboard scanning.
    # DK picks via live_sports_trader.py are NOT subject to this cap.
    daily_spend = ss.get("daily_spend", 0.0)
    if daily_spend >= SPORTS_DAILY_SPEND_HARD_CAP:
        return False, f"Sports daily spend cap reached (${daily_spend:.0f} >= ${SPORTS_DAILY_SPEND_HARD_CAP:.0f}) — use DK picks for more"


    return True, "ok"

SIZE_MIN        = {"NORMAL": 75,    "RECOVERY": 50,    "EXPANSION": 100,    "PAUSED": 0}
SIZE_MAX        = {"NORMAL": 200,   "RECOVERY": 100,   "EXPANSION": 300,    "PAUSED": 0}

# Drawdown thresholds
DD_RECOVERY   = 0.10   # enter Recovery if drawdown from peak >= 10%
DD_RESUME     = 0.05   # exit Recovery when drawdown drops back to <= 5%
DD_HARD_PAUSE = 0.20   # full pause at 20% drawdown

# Daily stop thresholds (% of start-of-day equity)
DAILY_SOFT_STOP = 0.02  # block new trades, allow closing only
DAILY_HARD_STOP = 0.03  # flatten + block until next day

# Expansion: new peak must be >= checkpoint * 1.10 AND held for N cycles
EXPANSION_STEP       = 0.10
EXPANSION_HOLD_CYCLES = 3   # ~45 min at 15-min interval


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, color=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"{color}{line}{Style.RESET_ALL}" if color else line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── State Persistence ─────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "mode": "NORMAL",
    "equity_peak_eod": None,       # high-watermark (end-of-day basis)
    "equity_sod": None,            # start-of-day equity snapshot
    "sod_date": None,              # date string for daily reset
    "expansion_checkpoint": None,  # equity level that unlocked Expansion
    "expansion_cycles_held": 0,    # consecutive cycles above checkpoint * 1.10
    "starting_bankroll": 1000.0,   # reference for absolute sizing floor
    "last_approval_date": None,    # date of last USDC allowance approval
}


# ── Auto-Approve USDC Allowances ──────────────────────────────────────────────

MAX_UINT256 = 2**256 - 1

def ensure_allowances(state):
    """
    Re-approve USDC allowances once per day at startup.
    Prevents 'not enough balance / allowance' errors.
    """
    today = date.today().isoformat()
    if state.get("last_approval_date") == today:
        return state  # Already approved today

    log("Checking/refreshing USDC allowances...", Fore.CYAN)
    try:
        from web3 import Web3

        # Try multiple RPC endpoints in case one is rate-limited
        rpc_urls = [
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://1rpc.io/matic",
            "https://polygon-rpc.com",
        ]
        w3 = None
        for rpc in rpc_urls:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue
        if w3 is None:
            log("All Polygon RPCs failed — skipping allowance check", Fore.YELLOW)
            return state

        private_key_hex = PRIVATE_KEY
        if len(private_key_hex) == 64:
            account = w3.eth.account.from_key(private_key_hex)
        else:
            log("Invalid private key format for allowance check", Fore.YELLOW)
            return state

        signer = account.address

        # USDC contract on Polygon
        USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        # Polymarket CTF Exchange
        CTF_EXCHANGE = Web3.to_checksum_address(
    os.environ.get("CTF_EXCHANGE_ADDRESS", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
)  # Override in .env for V2 migration
        # Polymarket Neg Risk CTF Exchange  
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        # Neg Risk Adapter
        NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

        ERC20_ABI = [
            {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
             "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
             "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
             "name": "approve", "outputs": [{"name": "", "type": "bool"}],
             "stateMutability": "nonpayable", "type": "function"},
        ]

        usdc = w3.eth.contract(address=USDC, abi=ERC20_ABI)
        funder_addr = Web3.to_checksum_address(FUNDER) if FUNDER else signer
        THRESHOLD = MAX_UINT256 // 2  # Re-approve if below half of max

        approved_any = False
        for name, spender in [("CTF Exchange", CTF_EXCHANGE),
                               ("Neg Risk Exchange", NEG_RISK_EXCHANGE),
                               ("Neg Risk Adapter", NEG_RISK_ADAPTER)]:
            try:
                allowance = usdc.functions.allowance(funder_addr, spender).call()
                if allowance < THRESHOLD:
                    log(f"  Re-approving {name}...", Fore.YELLOW)
                    nonce = w3.eth.get_transaction_count(signer)
                    gas_price = w3.eth.gas_price
                    txn = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
                        "from": signer,
                        "nonce": nonce,
                        "gas": 100000,
                        "gasPrice": gas_price,
                        "chainId": 137,
                    })
                    signed_txn = w3.eth.account.sign_transaction(txn, private_key=private_key_hex)
                    raw = signed_txn.raw_transaction if hasattr(signed_txn, 'raw_transaction') else signed_txn.rawTransaction
                    tx_hash = w3.eth.send_raw_transaction(raw)
                    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    log(f"  ✓ {name} approved (tx: {tx_hash.hex()[:16]}...)", Fore.GREEN)
                    approved_any = True
                else:
                    log(f"  ✓ {name} allowance OK", Fore.WHITE)
            except Exception as e:
                log(f"  Could not check/approve {name}: {e}", Fore.YELLOW)

        state["last_approval_date"] = today
        if approved_any:
            log("USDC allowances refreshed.", Fore.GREEN)
        else:
            log("All USDC allowances already set.", Fore.WHITE)

        # ── Approve conditional token allowances for all open positions ────────────
        # This is required to SELL position tokens back to the market.
        # Without this, sells fail with 'not enough balance / allowance'.
        # The CLOB client handles this via update_balance_allowance(CONDITIONAL, token_id).
        try:
            pos_resp = requests.get(
                f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=25",
                timeout=10
            )
            if pos_resp.status_code == 200:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams as _BAP, AssetType as _AT
                # Need a CLOB client to call update_balance_allowance
                # Build one from env
                from py_clob_client_v2.client import ClobClient as _CC
                from py_clob_client_v2.clob_types import ApiCreds as _AC
                _pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
                _fu = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
                _st = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
                _cl = _CC("https://clob.polymarket.com", key=_pk, chain_id=137,
                          creds=_AC(api_key=os.environ.get("CLOB_API_KEY",""),
                                    api_secret=os.environ.get("CLOB_API_SECRET",""),
                                    api_passphrase=os.environ.get("CLOB_API_PASSPHRASE","")),
                          signature_type=_st, funder=_fu)
                for _p in pos_resp.json():
                    _tok = _p.get("asset", "")
                    _val = float(_p.get("currentValue", 0) or 0)
                    if _tok and _val > 1.0:
                        try:
                            _cl.update_balance_allowance(
                                params=_BAP(asset_type=_AT.CONDITIONAL,
                                            token_id=_tok, signature_type=2))
                            log(f"  ✓ Conditional token approved: {_tok[:16]}...", Fore.WHITE)
                        except Exception as _te:
                            log(f"  Conditional token {_tok[:16]}: {_te}", Fore.YELLOW)
        except Exception as _ce:
            log(f"Conditional token approvals: {_ce}", Fore.YELLOW)

    except ImportError as e:
        log(f"web3 import error — {e}", Fore.YELLOW)
    except Exception as e:
        log(f"Allowance check failed: {e}", Fore.YELLOW)
        import traceback; traceback.print_exc()

    return state

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            state = {**DEFAULT_STATE, **saved}
            return state
        except Exception as e:
            log(f"State load error (using defaults): {e}", Fore.YELLOW)
    return dict(DEFAULT_STATE)

def save_state(state):
    """Atomic state write — crash mid-write cannot corrupt state.json.
    Writes to a temp file first, then renames atomically (POSIX guarantee).
    Circuit breakers and protective stops are never lost on crash.
    """
    import tempfile
    try:
        dir_name = os.path.dirname(STATE_FILE)
        with tempfile.NamedTemporaryFile(
            mode='w', dir=dir_name, suffix='.tmp', delete=False
        ) as tmp:
            json.dump(state, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, STATE_FILE)  # atomic on POSIX
    except Exception as e:
        log(f"State save error: {e}", Fore.YELLOW)
        # Clean up temp file if rename failed
        try:
            if 'tmp_path' in dir() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


# ── CLOB Client ───────────────────────────────────────────────────────────────

def get_client():
    from py_clob_client_v2.client import ClobClient
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    client = ClobClient(
        host=CLOB_HOST,
        key=PRIVATE_KEY,
        chain_id=137,
        funder=FUNDER or None,
        signature_type=sig_type,
    )
    try:
        creds = client.create_or_derive_api_key()
    except AttributeError:
        creds = client.derive_api_key()
    client.set_api_creds(creds)
    return client


# ── Equity Estimation ─────────────────────────────────────────────────────────

def get_equity(client):
    """
    Equity = positions market value + CLOB free cash.
    The Polymarket data-api /value endpoint returns ONLY position mark-to-market,
    NOT CLOB free cash. We must add CLOB balance separately for true equity.
    """
    positions_value = 0.0
    clob_cash = 0.0
    try:
        import requests as _req
        # Positions value from data API
        _r = _req.get(f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=8)
        if _r.status_code == 200:
            _data = _r.json()
            if isinstance(_data, list) and _data:
                positions_value = float(_data[0].get("value", 0))
            elif isinstance(_data, dict):
                positions_value = float(_data.get("value", 0))
    except Exception as e:
        log(f"Equity value API failed: {e}", Fore.YELLOW)
    try:
        # CLOB free cash (USDC available for trading)
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        balance_info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
        clob_cash = float(balance_info.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"CLOB balance check failed: {e}", Fore.YELLOW)
    total = positions_value + clob_cash
    if total > 0:
        if clob_cash > 1.0:
            log(f"  Equity: positions=\${positions_value:.2f} + CLOB_cash=\${clob_cash:.2f} = \${total:.2f}", Fore.CYAN)
        return total
    # If both failed, return None
    log(f"Equity check failed: positions={positions_value}, clob={clob_cash}", Fore.YELLOW)
    return None


def get_portfolio_stats(client):
    try:
        # V2 SDK ClobClient does not expose get_orders — use REST API directly
        import os as _os
        _funder = _os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
        _r = requests.get(
            f"https://clob.polymarket.com/orders?maker={_funder}&status=OPEN&limit=50",
            timeout=10
        )
        orders = _r.json() if _r.ok else []
        if isinstance(orders, dict):
            orders = orders.get("data", [])
        open_count = len(orders)
        deployed = sum(
            float(o.get("original_size", 0)) * float(o.get("price", 0))
            for o in orders
        )
        return {"open_orders": open_count, "deployed": deployed}
    except Exception as e:
        log(f"Portfolio check failed: {e}", Fore.YELLOW)
        return {"open_orders": 0, "deployed": 0}


# ── Control Plane ─────────────────────────────────────────────────────────────

def update_control_plane(state, equity_now):
    """
    Core state machine. Updates mode based on equity, drawdown, and daily P&L.
    Returns updated state + whether new trades are allowed.
    """
    today = date.today().isoformat()

    # ── Daily reset ───────────────────────────────────────────────────────────
    if state.get("sod_date") != today:
        log(f"New trading day — snapshotting start-of-day equity: ${equity_now:.2f}", Fore.CYAN)
        state["sod_date"] = today
        state["equity_sod"] = equity_now
        # Force re-approval on new day
        state["last_approval_date"] = None
        # Update EOD peak at start of new day
        if state["equity_peak_eod"] is None or equity_now > state["equity_peak_eod"]:
            state["equity_peak_eod"] = equity_now
            log(f"New all-time peak: ${equity_now:.2f}", Fore.GREEN)

    # ── Initialize peak if first run ──────────────────────────────────────────
    if state["equity_peak_eod"] is None:
        state["equity_peak_eod"] = equity_now
    if state["equity_sod"] is None:
        state["equity_sod"] = equity_now
    if state["expansion_checkpoint"] is None:
        state["expansion_checkpoint"] = equity_now
    if state["starting_bankroll"] is None:
        state["starting_bankroll"] = equity_now

    peak      = state["equity_peak_eod"]
    sod       = state["equity_sod"]
    checkpoint = state["expansion_checkpoint"]

    # ── Auto-repair inflated peak ─────────────────────────────────────────────
    # If peak is >25% above current equity, it's likely stale or inflated
    # (e.g. set during a session where positions were temporarily overvalued,
    # or after a manual deposit that skewed the numbers). Reset to current.
    if peak and equity_now > 0 and (peak / equity_now) > 1.25:
        log(f"⚙ AUTO-REPAIR: peak ${peak:.2f} is {((peak/equity_now)-1):.0%} above current equity ${equity_now:.2f} — resetting peak to current.", Fore.CYAN)
        state["equity_peak_eod"] = equity_now
        if state.get("peak_equity", 0) > equity_now * 1.25:
            state["peak_equity"] = equity_now
        if state.get("expansion_checkpoint", 0) > equity_now * 1.25:
            state["expansion_checkpoint"] = equity_now
        peak = equity_now
        checkpoint = equity_now

    # ── Sports position exemption ─────────────────────────────────────────────
    # Same-day sports positions priced at 99¢+ are resolved but pending oracle.
    # Their value is real cash-equivalent — they should NOT be counted as losses.
    # Without this, Suns/Thunder winning at 99¢ then dropping to 0¢ post-oracle
    # looks like an 88% drawdown and triggers HARD_PAUSE spuriously.
    _sports_pending_value = 0.0
    try:
        import requests as _rq_sports
        _pos_r = _rq_sports.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER_ADDRESS}&limit=50",
            timeout=8
        )
        if _pos_r.ok:
            _SPORTS_KW = ["nba","nfl","nhl","mlb","ufc","suns","thunder","clippers",
                           "lakers","celtics","warriors","nuggets","cavaliers","hawks",
                           "mavericks","knicks","timberwolves","rockets","pacers","nets",
                           "premier league","champions league","tennis","f1","ufc","golf"]
            for _p in _pos_r.json():
                _cv  = float(_p.get("currentValue", 0))
                _sh  = float(_p.get("size", 0))
                _ttl = _p.get("title","").lower()
                if _cv < 1 or _sh < 1:
                    continue
                _mid = _cv / _sh
                # Near-resolved sports position (99¢+) pending oracle
                if _mid > 0.97 and any(_kw in _ttl for _kw in _SPORTS_KW):
                    _cost = _sh * float(_p.get("avgPrice", _mid))
                    _sports_pending_value += _cost  # add back cost basis
                    log(f"  [SPORTS-EXEMPT] {_p.get('title','')[:45]}: "
                        f"${_cv:.0f} pending oracle — exempt from drawdown calc")
    except Exception as _se:
        pass

    # Adjust equity_now upward by sports pending value to avoid false hard-pause
    equity_adjusted = equity_now + _sports_pending_value
    if _sports_pending_value > 0:
        log(f"  [SPORTS-EXEMPT] equity_now ${equity_now:.2f} → adjusted ${equity_adjusted:.2f} "
            f"(+${_sports_pending_value:.2f} pending oracle)")

    drawdown   = 1.0 - (equity_adjusted / peak) if peak > 0 else 0.0
    daily_pnl  = equity_adjusted - sod
    daily_pnl_pct = daily_pnl / sod if sod > 0 else 0.0

    log(f"Equity: ${equity_now:.2f} (adj ${equity_adjusted:.2f}) | Peak: ${peak:.2f} | DD: {drawdown:.1%} | Daily P&L: ${daily_pnl:+.2f} ({daily_pnl_pct:+.1%})")

    allow_new_trades = True
    prev_mode = state["mode"]

    # ── Hard pause (20% drawdown) ─────────────────────────────────────────────
    if drawdown >= DD_HARD_PAUSE:
        state["mode"] = "PAUSED"
        allow_new_trades = False
        log(f"⛔ HARD PAUSE: drawdown {drawdown:.1%} >= {DD_HARD_PAUSE:.0%}. No new trades.", Fore.RED)

    # ── Daily hard stop (lost 3%+ today) ─────────────────────────────────────
    elif daily_pnl_pct <= -DAILY_HARD_STOP:
        state["mode"] = "PAUSED"
        allow_new_trades = False
        log(f"⛔ DAILY HARD STOP: lost {daily_pnl_pct:.1%} today (>${abs(daily_pnl):.2f}). No trades until tomorrow.", Fore.RED)

    # ── Daily soft stop (lost 2%+ today) — close only ─────────────────────────
    elif daily_pnl_pct <= -DAILY_SOFT_STOP:
        allow_new_trades = False
        log(f"⚠ DAILY SOFT STOP: lost {daily_pnl_pct:.1%} today. Closing only — no new entries.", Fore.YELLOW)
        # Keep current mode, just block new trades

    # ── Unblock from PAUSED if we're in a new day with recovered drawdown ─────
    elif state["mode"] == "PAUSED" and drawdown < DD_RECOVERY:
        state["mode"] = "NORMAL"
        log(f"✓ Resuming from PAUSED — drawdown recovered to {drawdown:.1%}", Fore.GREEN)

    # ── Recovery mode ─────────────────────────────────────────────────────────
    elif drawdown >= DD_RECOVERY:
        state["mode"] = "RECOVERY"
        state["expansion_cycles_held"] = 0
        log(f"⚠ RECOVERY MODE: drawdown {drawdown:.1%}", Fore.YELLOW)

    # ── Exit Recovery when drawdown drops back to <= 5% ───────────────────────
    elif state["mode"] == "RECOVERY" and drawdown <= DD_RESUME:
        state["mode"] = "NORMAL"
        log(f"✓ Exiting Recovery — drawdown recovered to {drawdown:.1%}", Fore.GREEN)

    # ── Expansion mode (new peak >= checkpoint * 1.10) ────────────────────────
    elif equity_now >= checkpoint * (1.0 + EXPANSION_STEP) and state["mode"] != "RECOVERY":
        state["expansion_cycles_held"] = state.get("expansion_cycles_held", 0) + 1
        if state["expansion_cycles_held"] >= EXPANSION_HOLD_CYCLES:
            state["mode"] = "EXPANSION"
            state["expansion_checkpoint"] = equity_now
            log(f"🚀 EXPANSION MODE: equity ${equity_now:.2f} is {((equity_now/checkpoint)-1):.0%} above checkpoint. Unlocking larger sizes.", Fore.GREEN)
        else:
            log(f"  Near expansion (cycle {state['expansion_cycles_held']}/{EXPANSION_HOLD_CYCLES})…", Fore.CYAN)

    # ── Revert Expansion to Normal if peak not extended ───────────────────────
    elif state["mode"] == "EXPANSION" and equity_now < checkpoint:
        state["mode"] = "NORMAL"
        state["expansion_cycles_held"] = 0
        log(f"Stepped back from Expansion to Normal (equity below checkpoint).", Fore.YELLOW)

    # ── Normal ────────────────────────────────────────────────────────────────
    else:
        if state["mode"] not in ("RECOVERY", "EXPANSION"):
            state["mode"] = "NORMAL"
        state["expansion_cycles_held"] = state.get("expansion_cycles_held", 0)

    # Update EOD peak intraday if equity rose
    if equity_now > state["equity_peak_eod"]:
        state["equity_peak_eod"] = equity_now

    if state["mode"] != prev_mode:
        log(f"MODE CHANGE: {prev_mode} → {state['mode']}", Fore.CYAN)

    return state, allow_new_trades


# ── Scanner ───────────────────────────────────────────────────────────────────

def fetch_markets_batch(offset=0, limit=100):
    """Fetch active markets sorted by 24h volume, no tag filter."""
    try:
        r = requests.get(GAMMA_API, params={
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
            "active": "true",
            "closed": "false",
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def normalize(raw):
    try:
        q = raw.get("question", "").strip()
        if not q:
            return None
        ids = raw.get("clobTokenIds", [])
        if isinstance(ids, str):
            ids = json.loads(ids)
        prices = raw.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)
        yes_price = float(prices[0]) if prices else None
        no_price  = float(prices[1]) if len(prices) > 1 else None
        if yes_price is None or yes_price <= 0.03 or yes_price >= 0.97:
            return None
        return {
            "id":           raw.get("id", ""),
            "question":     q,
            "yes_price":    yes_price,
            "no_price":     no_price,
            "yes_token_id": ids[0] if ids else None,
            "no_token_id":  ids[1] if len(ids) > 1 else None,
            "volume":       float(raw.get("volume24hr", 0) or 0),
            "end_date":     raw.get("endDate", ""),
            "description":  raw.get("description", "")[:500],
            "market_slug":  raw.get("slug", ""),
            "fees_enabled": bool(raw.get("feesEnabled", False)),
        }
    except Exception:
        return None


def scan_markets():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # Fetch in two parallel batches of 100 to get top 200 markets by volume
    batch_size = 100
    offsets = [i * batch_size for i in range(MARKETS_FETCH_LIMIT // batch_size)]
    seen = {}
    with ThreadPoolExecutor(max_workers=len(offsets)) as pool:
        futures = {pool.submit(fetch_markets_batch, off, batch_size): off for off in offsets}
        for f in as_completed(futures):
            raw_list = f.result()
            for raw in raw_list:
                m = normalize(raw)
                if m and (m["id"] not in seen or m["volume"] > seen[m["id"]]["volume"]):
                    seen[m["id"]] = m
    return sorted(seen.values(), key=lambda x: x["volume"], reverse=True)


# ── Scorer ────────────────────────────────────────────────────────────────────

# ── Unusual Whales Signal ─────────────────────────────────────────────────────

_uw_cache = {"unusual": [], "smart": [], "insiders": [], "ts": 0}

def fetch_uw_signals():
    """Fetch all three UW prediction endpoints, cached 10 minutes."""
    global _uw_cache
    if not UW_API_KEY:
        return _uw_cache
    now = time.time()
    if now - _uw_cache["ts"] < 600 and _uw_cache["unusual"]:
        return _uw_cache
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        for key, ep in [("unusual","/predictions/unusual"),("smart","/predictions/smart-money"),("insiders","/predictions/insiders")]:
            try:
                r = requests.get(f"{UW_API_BASE}{ep}", headers=headers, timeout=15)
                r.raise_for_status()
                _uw_cache[key] = r.json().get("data", {}).get("data", [])
            except Exception as e:
                log(f"[UW] {key} fetch failed: {e}", Fore.YELLOW)
        _uw_cache["ts"] = now
        log(f"[UW] {len(_uw_cache['unusual'])} unusual | {len(_uw_cache['smart'])} smart-money | {len(_uw_cache['insiders'])} insiders", Fore.MAGENTA)
    except Exception as e:
        log(f"[UW] fetch error: {e}", Fore.YELLOW)
    return _uw_cache

def _uw_words(s):
    stop = {"will","the","a","an","be","by","in","on","or","of","to","vs","vs.","?","and"}
    return set(s.lower().split()) - stop

def _uw_overlap(a, b):
    wa, wb = _uw_words(a), _uw_words(b)
    if not wa or not wb: return 0
    return len(wa & wb) / max(len(wa), len(wb))

def match_uw_unusual(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    best, best_s = None, 0
    for s in signals:
        if s.get("asset_id") == tok: return s
        if s.get("outcome","") != target: continue
        ov = _uw_overlap(market.get("question",""), s.get("market",""))
        if ov > best_s and ov >= 0.5: best_s, best = ov, s
    return best

def match_uw_smart(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    for s in signals:
        for o in s.get("outcomes", []):
            if o.get("asset_id") == tok:
                return {"mkt": s, "out": o}
        ov = _uw_overlap(market.get("question",""), s.get("title",""))
        if ov >= 0.6:
            for o in s.get("outcomes", []):
                if o.get("label","") == target:
                    return {"mkt": s, "out": o}
    return None

def match_uw_insiders(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    out = []
    for s in signals:
        if s.get("asset_id") == tok: out.append(s); continue
        if s.get("outcome","") != target: continue
        if _uw_overlap(market.get("question",""), s.get("question","")) >= 0.6:
            out.append(s)
    return out

def build_uw_context(market, uw_cache, action):
    """Returns (prompt_text, signal_dict) for a market+action."""
    sig = {"unusual_score":0,"smart_money":0,"insider_trades":0,"contrarian_whales":0,
           "smart_gap":0,"smart_score":0,"insider_count":0,"insider_zscore":0,
           "insider_pnl":0,"tags_score":0}
    lines = []

    u = match_uw_unusual(market, uw_cache.get("unusual",[]), action)
    if u:
        tags = {t["tag"]: float(t["value"]) for t in u.get("tags",[])}
        sig.update({"unusual_score":float(u.get("unusual_score",0)),
                    "smart_money":tags.get("smart_money",0),
                    "insider_trades":tags.get("insider_trades",0),
                    "contrarian_whales":tags.get("contrarian_whales",0)})
        lines.append(f"unusual_score={sig['unusual_score']:.1f} smart_money={sig['smart_money']:.0f} insider_trades={sig['insider_trades']:.0f} contrarian_whales={sig['contrarian_whales']:.0f}")

    sm = match_uw_smart(market, uw_cache.get("smart",[]), action)
    if sm:
        gap   = float(sm["mkt"].get("smart_gap", 0))
        score = float(sm["out"].get("smart_score", 0))
        sig.update({"smart_gap":gap, "smart_score":score})
        lines.append(f"smart_gap={gap:.2f} smart_score={score:.2f}  (smart_gap=divergence smart vs retail, higher=stronger)")

    ins_list = match_uw_insiders(market, uw_cache.get("insiders",[]), action)
    if ins_list:
        invested = sum(float(i.get("total_invested_usd",0)) for i in ins_list)
        zsc = sum(float(i.get("invested_zscore",0)) for i in ins_list) / len(ins_list)
        pnl = sum(float(i.get("pnl_percent",0)) for i in ins_list) / len(ins_list)
        tsc = sum(float(i.get("tags_score",0)) for i in ins_list) / len(ins_list)
        sig.update({"insider_count":len(ins_list),"insider_zscore":zsc,"insider_pnl":pnl,"tags_score":tsc})
        lines.append(f"insider_wallets={len(ins_list)} invested=${invested:,.0f} zscore={zsc:.1f} pnl={pnl:.1%} tags_score={tsc:.2f}")

    text = ("UNUSUAL WHALES SMART MONEY DATA:\n" + "\n".join(lines) + "\n") if lines else ""
    return text, sig


def compute_polymarket_fee(price: float, fee_rate: float = 0.25, exponent: float = 2.0) -> float:
    """
    Compute Polymarket taker fee as a fraction of trade value.
    Formula: fee_fraction = fee_rate * (p * (1-p))^exponent
    This gives the fee as a fraction of (price * shares), i.e. as a % of USDC spent.

    For fee-enabled markets (crypto, NCAAB, Serie A):
      fee_rate=0.25, exponent=2 for crypto
      fee_rate=0.0175, exponent=1 for sports
    For most markets: returns 0.0

    The effective rate peaks at 1.56% at p=0.50 and decreases toward extremes.
    At p=0.10: ~0.20% | p=0.25: ~0.88% | p=0.50: ~1.56% | p=0.75: ~0.88% | p=0.90: ~0.20%
    """
    p = max(0.01, min(0.99, price))
    return fee_rate * (p * (1.0 - p)) ** exponent


def is_fee_enabled_market(market: dict) -> bool:
    """Check if this market has fees enabled.
    Updated March 30 2026: fees expanded to crypto, sports, politics, finance,
    economics, culture, weather, tech, other/general, mentions.
    ONLY geopolitical/world-events markets remain permanently fee-free.
    """
    if market.get("fees_enabled", False):
        return True
    q = market.get("question", "").lower() + " " + market.get("title", "").lower()
    # Geopolitical / world events — permanently fee-free
    _geo_keywords = [
        "iran", "ceasefire", "forces enter", "regime fall", "kharg", "nuclear deal",
        "war by", "conflict", "invasion", "military", "coup", "regime", "sanctions",
        "peace deal", "prime minister", "president", "election", "government",
        "treaty", "nato", "un security", "ukraine", "russia", "china invade",
    ]
    if any(kw in q for kw in _geo_keywords):
        return False  # geopolitical — fee-free
    # All other categories now have fees
    return True


def score_market(market, mode="NORMAL"):
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    # In Recovery, only score high-volume liquid markets
    if mode == "RECOVERY" and market.get("volume", 0) < 50000:
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": "Recovery mode: low-volume market skipped"}
    # Skip blacklisted market types (social noise, in-play markers)
    if is_blacklisted(market.get("question", "") + " " + market.get("title", "")):
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": "Blacklisted market type — no model edge"}

    # ── CONDITION ID BLACKLIST — never trade these markets again ──────────────
    # Lessons 9, 10, 11: crude oil at trigger, BTC near trigger → blocked permanently
    _AUTOTRADER_BLACKLIST = {
        # Sports game markets — auto-sold too early, let these resolve naturally
        "0x87254ca39f82f1fdef981066710fb49904e86358bcf1ed9a4e05e4558d665329": "Duke vs UConn ML — hold until resolution",
        "0xc5300759dc2089042380795fe7384010a6b6ebdf9e6da7ed3f786d9a5f61c563":
            "Lesson 9+10: Crude Oil $100 HIGH — bought NO 4x when WTI was at trigger",
        "0x36912c9832f0fd104d734b579fb9b3a1b31bbdc946a67356723407e3bdc96dbc":
            "Lesson 11: BTC $65k dip NO — bought when BTC was 1.8% above trigger",
        "0x4290a4aa43a0707f0f1193c73667074f2ef5ce8ab5d6fcdd4ca645bfe1528f03":
            "Lesson 11: BTC $60k dip YES — needs 10% drop in 4 days",
        # Sports match-day single game markets — blocked permanently after Man City loss
        # "win on YYYY-MM-DD" markets are game-day binary bets with 0.75% fee
        # Man City FC YES at 0.12 → $150 loss. Never again.
    }
    _mkt_condition = market.get("condition_id", market.get("conditionId", ""))
    if _mkt_condition in _AUTOTRADER_BLACKLIST:
        reason = _AUTOTRADER_BLACKLIST[_mkt_condition]
        log(f"  [BLACKLISTED] {market.get('question','')[:55]} — {reason[:50]}", Fore.RED)
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": f"BLACKLISTED: {reason}"}

    # ── FEE-MARKET PRICE GUARD — catch any sports/crypto that bypassed keyword gate ─
    # Man City FC win on 2026-04-04 slipped through because "manchester" wasn't in
    # SPORTS_KEYWORDS. This guard catches any fee market at price extremes.
    if is_fee_enabled_market(market):
        try:
            _prices_raw = market.get("outcomePrices", "[0.5,0.5]")
            if isinstance(_prices_raw, str):
                _prices_list = [float(x.strip().strip('"')) for x in _prices_raw.strip("[]").split(",")]
            else:
                _prices_list = [float(x) for x in _prices_raw]
            _yes_guard = _prices_list[0] if _prices_list else 0.5
        except Exception:
            _yes_guard = 0.5
        if _yes_guard < 0.20:
            log(f"  [FEE-GUARD] {market.get('question','')[:50]} YES={_yes_guard:.3f} < 0.20 — lottery ticket blocked", Fore.YELLOW)
            return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                    "reasoning": f"Fee-market price guard: YES={_yes_guard:.3f} < 0.20 — lottery ticket. Constitution Rule 4: no local optimization that harms total system. Man City lesson."}
        if _yes_guard > 0.83:
            log(f"  [FEE-GUARD] {market.get('question','')[:50]} YES={_yes_guard:.3f} > 0.83 — market decided, no edge", Fore.YELLOW)
            return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                    "reasoning": f"Fee-market price guard: YES={_yes_guard:.3f} > 0.83 — market already decided. No edge at extremes."}

    # ── COMMODITY PRICE REALITY CHECK — no trading near the trigger ────────────
    def _get_live_price(symbol):
        try:
            import requests as _r
            rv = _r.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
                timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            return float(rv.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except: return None

    _q_lower = (market.get("question", "") + " " + market.get("title", "")).lower()
    import re as _re2

    if ("crude oil" in _q_lower or "wti" in _q_lower or "cl)" in _q_lower) and "high" in _q_lower:
        _targets = _re2.findall(r"\$([0-9,]+)", market.get("question", ""))
        if _targets:
            _target = float(_targets[0].replace(",", ""))
            _wti = _get_live_price("CL=F")
            if _wti is not None and abs(_wti - _target) <= 5.0:
                log(f"  [COMMODITY BLOCK] WTI ${_wti:.2f} within $5 of ${_target:.0f} — skip", Fore.YELLOW)
                return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                        "reasoning": f"WTI ${_wti:.2f} within $5 of ${_target:.0f} trigger — no edge"}

    if ("bitcoin" in _q_lower or "btc" in _q_lower) and ("dip" in _q_lower or "low" in _q_lower):
        _targets = _re2.findall(r"\$([0-9,]+)", market.get("question", ""))
        if _targets:
            _target = float(_targets[0].replace(",", ""))
            _btc = _get_live_price("BTC-USD")
            if _btc is not None:
                _gap_pct = (_btc - _target) / _target
                if abs(_gap_pct) <= 0.05:
                    log(f"  [COMMODITY BLOCK] BTC ${_btc:,.0f} within 5% of ${_target:,.0f} — skip", Fore.YELLOW)
                    return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                            "reasoning": f"BTC ${_btc:,.0f} within 5% of ${_target:,.0f} trigger — no edge"}

    # Category whitelist: only score markets in approved categories.
    # Sports are exempt here — they go through the sports policy gate separately.
    q_full = market.get("question", "") + " " + market.get("title", "")
    if not is_sports_market(q_full) and not is_approved_category(q_full):
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": "Not in approved category whitelist — no proven edge"}

    # Check market assessment cache — skip API calls if price unchanged within TTL
    cached = _get_cached_score(market)
    if cached is not None:
        log(f"  [CACHE HIT] {market.get('question','')[:55]} edge={cached.get('edge',0):+.3f}", Fore.CYAN)
        return {**market, **cached}

    # ── OPT-3: Fast-reject gate — 1 cheap Haiku call to skip obvious PASSes ──
    # Saves the Perplexity news call + full scoring for markets with no real edge.
    # Gate fires ONLY when no UW signal present (UW signals always worth full scoring).
    # Pre-check UW cache (already populated by prior cycles, fetch is cached 10min)
    _uw_pre = fetch_uw_signals()
    _tok_yes = market.get("yes_token_id", "")
    _tok_no  = market.get("no_token_id", "")
    _has_uw = any(
        s.get("asset_id") in (_tok_yes, _tok_no)
        for pool in [_uw_pre.get("unusual", []), _uw_pre.get("insiders", [])]
        for s in pool
    )
    if not _has_uw:
        try:
            import anthropic as _anth
            _ac_gate = getattr(score_market, "_ac", None)
            if _ac_gate is None:
                _ac_gate = _anth.Anthropic(api_key=api_key)
                score_market._ac = _ac_gate
            _q = market.get("question", "")
            _yp = market.get("yes_price", 0.5)
            _gate_resp = _ac_gate.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": (
                    f"Prediction market: '{_q}'\nYES price: {_yp:.2f}\n"
                    "Is there ANY genuine mispricing edge (≥15pp) a well-informed trader would act on?\n"
                    "Respond ONLY: YES or NO."
                )}],
                temperature=0.0,
            )
            _count_api_call()
            _track_usage(_gate_resp)
            _gate_answer = _gate_resp.content[0].text.strip().upper()
            if _gate_answer == "NO" or _gate_answer.startswith("NO"):
                log(f"  [GATE-SKIP] {market.get('question','')[:55]}", Fore.CYAN)
                result = {**market, "action": "PASS", "edge": 0,
                          "confidence": "low", "reasoning": "fast-reject gate: no edge"}
                _set_cached_score(market, result)
                return result
        except Exception as _ge:
            pass  # gate failure — fall through to full scoring

    news = ""
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if pplx_key:
        try:
            r = requests.post(
                "https://api.perplexity.ai/chat/completions",
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "Find recent facts about this prediction market question. Be brief and factual."},
                        {"role": "user", "content": market["question"]},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
                headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
                timeout=20,
            )
            news = r.json()["choices"][0]["message"]["content"]
            _count_api_call()  # track Perplexity call
        except Exception:
            pass

    soul       = load_soul()
    lessons    = load_lessons()
    hard_rules = load_hard_rules()
    intel   = ""
    if hard_rules: intel += "HARD RULES (read first — these override everything else):\n" + hard_rules + "\n\n"
    if soul:       intel += "CORE PRINCIPLES:\n" + soul + "\n"
    if lessons:    intel += "LEARNED LESSONS:\n" + lessons + "\n"

    # ── Unusual Whales signal injection ──────────────────────────────────────
    uw_cache = fetch_uw_signals()
    uw_yes_text, uw_yes_sig = build_uw_context(market, uw_cache, "BUY_YES")
    uw_no_text,  uw_no_sig  = build_uw_context(market, uw_cache, "BUY_NO")
    uw_text = ""
    if uw_yes_text: uw_text += "IF BUY_YES: " + uw_yes_text
    if uw_no_text:  uw_text += "IF BUY_NO: "  + uw_no_text
    market["_uw_yes_sig"] = uw_yes_sig
    market["_uw_no_sig"]  = uw_no_sig

    # ── Multi-source signal: match RSS headlines to this market ─────────────
    rss_signal = ""
    try:
        cached_headlines = getattr(score_market, "_rss_cache", [])
        if cached_headlines:
            q_words = set(market.get("question", "").lower().split())
            q_words -= {"will", "the", "a", "an", "be", "by", "in", "on", "is", "of", "to", "for", "and", "or"}
            matches = [h for h in cached_headlines if sum(1 for w in q_words if w in h.lower()) >= 2]
            if matches:
                rss_signal = "BREAKING NEWS MATCHES:\n" + "\n".join(matches[:5])
    except Exception:
        pass

    # ── Whale-flow signal: Polymarket activity for large recent trades ──────────
    # If wallets (not us) placed $500+ on this market in the last 2 cycles (30 min),
    # that's directional signal: someone with real information is acting.
    whale_signal = ""
    try:
        _condition_id = market.get("condition_id", "")
        if _condition_id:
            _wa_resp = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"market": _condition_id, "limit": 50},
                timeout=8
            )
            if _wa_resp.status_code == 200:
                _wa_acts = _wa_resp.json()
                import time as _wt
                _now_ts = _wt.time()
                _whale_buys_yes = []
                _whale_buys_no  = []
                for _wa in _wa_acts:
                    _ts = _wa.get("timestamp", 0)
                    if _ts > 1e10: _ts /= 1000
                    if _now_ts - _ts > 1800:  # Only last 30 min
                        continue
                    _sz = float(_wa.get("usdcSize", 0) or 0)
                    _side = _wa.get("side", "")
                    _user = _wa.get("proxyWallet", _wa.get("trader", ""))
                    if _sz >= 300 and _user.lower() != FUNDER.lower():  # $300+ whale
                        if _side == "BUY":
                            outcome = _wa.get("outcome", _wa.get("title", ""))
                            if "yes" in str(outcome).lower():
                                _whale_buys_yes.append(_sz)
                            else:
                                _whale_buys_no.append(_sz)
                if _whale_buys_yes:
                    whale_signal += f"WHALE FLOW YES: {len(_whale_buys_yes)} large buy(s) totaling ${sum(_whale_buys_yes):,.0f} in last 30min. "
                if _whale_buys_no:
                    whale_signal += f"WHALE FLOW NO: {len(_whale_buys_no)} large buy(s) totaling ${sum(_whale_buys_no):,.0f} in last 30min. "
                if whale_signal:
                    log(f"  [WHALE] {whale_signal[:80]}", Fore.MAGENTA)
    except Exception:
        pass

    # ── GDELT geopolitical signal (SOURCE 5) ────────────────────────────────────
    # GDELT monitors 435+ news sources in real-time. For geopolitical markets
    # (Iran, Ukraine, conflict) it provides direct headline evidence of escalation
    # or de-escalation before Perplexity can summarize. Free, no API key.
    gdelt_signal = ""
    _geo_kw = ["iran", "ceasefire", "ukraine", "russia", "north korea",
               "forces enter", "invasion", "regime", "conflict ends", "war",
               "china", "taiwan", "nato"]
    _q_lower = market.get("question", "").lower()
    if any(kw in _q_lower for kw in _geo_kw):
        try:
            import time as _gtime
            # Build a focused query from the market question
            _geo_words = [w for w in market.get("question", "").replace("?","").split()
                          if len(w) > 3 and w.lower() not in
                          {"will", "the", "a", "an", "be", "by", "in", "on", "is",
                           "of", "to", "for", "and", "or", "march", "april",
                           "2026", "2025", "june", "july"}]
            _geo_query = " ".join(_geo_words[:6])
            _gtime.sleep(5)  # GDELT rate limit: 1 req / 5 sec
            _gr = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": _geo_query,
                    "mode": "artlist",
                    "maxrecords": "5",
                    "format": "json",
                    "timespan": "48h",
                    "sort": "date",
                },
                timeout=12,
                headers={"User-Agent": "polymarket-scout/1.0"},
            )
            if _gr.status_code == 200:
                _articles = _gr.json().get("articles", [])
                if _articles:
                    _headlines = []
                    for _a in _articles[:4]:
                        _t = _a.get("title", "")[:90]
                        _d = _a.get("seendate", "")[:8]
                        if _t:
                            _headlines.append(f"[{_d}] {_t}")
                    gdelt_signal = "BREAKING ({} articles):\n".format(len(_articles)) + "\n".join(_headlines)
                    log(f"  [GDELT] {len(_articles)} articles: {_headlines[0][:60]}", Fore.MAGENTA)
        except Exception as _ge:
            log(f"  [GDELT] Error: {_ge}", Fore.YELLOW)
            gdelt_signal = None  # ensure it's None so source_count stays accurate
            _gdelt_failed = True

    # Build consensus block for prompt
    # If GDELT timed out AND it was the only geopolitical source, cap confidence
    _gdelt_failed = locals().get("_gdelt_failed", False)
    consensus = ""
    source_count = 0
    if news:          consensus += f"SOURCE 1 (Perplexity/web): {news[:300]}\n"; source_count += 1
    if rss_signal:    consensus += f"SOURCE 2 (Live RSS feeds): {rss_signal[:300]}\n"; source_count += 1
    if uw_text:       consensus += f"SOURCE 3 (Unusual Whales smart money): {uw_text[:200]}\n"; source_count += 1
    if whale_signal:  consensus += f"SOURCE 4 (Polymarket whale flow): {whale_signal[:200]}\n"; source_count += 1
    if gdelt_signal:  consensus += f"SOURCE 5 (GDELT real-time global news): {gdelt_signal[:400]}\n"; source_count += 1
    # If GDELT timed out, note it in consensus so Claude knows data is stale
    if _gdelt_failed:
        consensus += "NOTE: GDELT timed out this cycle — real-time news signal unavailable. Treat geopolitical edge claims with extra skepticism.\n"
        # Cap effective source count: a GDELT timeout on a geo market means
        # we're missing the most time-sensitive source. Reduce by 1.
        source_count = max(0, source_count - 1)
    if source_count >= 2:
        consensus += f"MULTI-SOURCE CONFIDENCE: {source_count}/5 sources have data — weight accordingly.\n"

    prompt = f"""MARKET: {market['question']}
YES price: ${market['yes_price']:.3f} | NO price: ${market['no_price']:.3f}
Volume 24h: ${market['volume']:,.0f}
Description: {market.get('description','N/A')[:300]}
End date: {market.get('end_date','N/A')}
{consensus if consensus else ("News: " + (news[:400] if news else "No real-time data"))}
{intel}
Respond with ONLY valid JSON:
{{"true_probability": <float>, "confidence": "<high|medium|low>", "reasoning": "<max 150 chars>", "edge": <true_prob minus yes_price>, "action": "<BUY_YES|BUY_NO|PASS>"}}

Rules: BUY_YES if edge>0.07 and confidence=high. BUY_NO if edge<-0.07 and confidence=high. PASS otherwise.
If 2+ sources agree on direction, upgrade confidence. If sources conflict, downgrade to PASS.
If UW smart_money or insider_trades are high (>3) and align with your direction, increase confidence. If they contradict your direction, lower confidence or PASS.

CRITICAL — Information Asymmetry Test: Before returning BUY_YES or BUY_NO, ask: 'What specific information do I have that the current price does NOT already reflect?' If your answer is 'I agree with the market consensus' or 'I can imagine this scenario' — return PASS. Only trade if you have a specific breaking catalyst the market has not yet repriced.

HARD PASS rules (return PASS immediately, no exceptions):
- YES price < 0.08 without a confirmed breaking catalyst directly enabling the outcome
- Market you already assessed as BUY in the last 24 hours (avoid fragmented re-entry)
- SPORTS markets: PASS unless you can name a SPECIFIC typed catalyst — confirmed injury/lineup change, OR line movement ≥5pp in 24h, OR named statistical model (FiveThirtyEight/ESPN BPI). 'Better team' and 'stronger record' are NOT catalysts. If you cannot name one, return PASS."""

    sys_text = (
        "You are a quantitative prediction market trader. Output ONLY valid JSON.\n"
        "Keys: true_probability, confidence, reasoning, edge, action.\n"
    )
    _soul = load_soul()
    _less = load_lessons()
    if _soul: sys_text += "\nCORE PRINCIPLES:\n" + _soul + "\n"
    if _less: sys_text += "\nLEARNED LESSONS:\n" + _less + "\n"

    try:
        if not hasattr(score_market, "_ac") or score_market._ac is None:
            score_market._ac = anthropic.Anthropic(api_key=api_key)
        resp = score_market._ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=[{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        _count_api_call()  # track Anthropic call
        _track_usage(resp)
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        # ── LLM response validation — never act on malformed/hallucinated output ──
        # Validate required fields exist and are correct types before trusting them.
        required = {"true_probability": (int, float), "confidence": str,
                    "action": str, "reasoning": str}
        for field, expected_type in required.items():
            if field not in result:
                raise ValueError(f"LLM response missing field: {field}")
            if not isinstance(result[field], expected_type):
                raise ValueError(f"LLM field '{field}' wrong type: {type(result[field]).__name__} (expected {expected_type.__name__})")
        tp = float(result["true_probability"])
        if not (0.0 <= tp <= 1.0):
            raise ValueError(f"LLM true_probability out of range: {tp}")
        if result["action"] not in ("BUY_YES", "BUY_NO", "PASS"):
            raise ValueError(f"LLM action invalid: {result['action']}")
        if result["confidence"] not in ("high", "medium", "low"):
            result["confidence"] = "low"  # demote unknown confidence to low
        # ── End validation ─────────────────────────────────────────────────────
        result["edge"] = round(float(result["true_probability"]) - market["yes_price"], 4)
        # ── Fee-aware edge threshold ────────────────────────────────────────────
        # For fee-enabled markets (crypto, NCAAB, Serie A), apply the fee cost
        # to the edge before deciding. Use a higher net-edge threshold.
        _trade_price = market["yes_price"] if result.get("action") == "BUY_YES" else market.get("no_price", 0.5)
        _fee_enabled = is_fee_enabled_market(market)
        _fee_fraction = compute_polymarket_fee(_trade_price) if _fee_enabled else 0.0
        _net_edge = abs(result["edge"]) - _fee_fraction
        _min_edge_threshold = MIN_EDGE_NET_FEE if _fee_enabled else MIN_EDGE
        result["_fee_fraction"] = round(_fee_fraction, 4)
        result["_fee_enabled"]  = _fee_enabled
        result["_net_edge"]     = round(_net_edge, 4)
        if _fee_enabled and _fee_fraction > 0:
            log(f"  [FEE] {market.get('question','')[:40]} fee={_fee_fraction:.3f} net_edge={_net_edge:+.3f}", Fore.YELLOW)
        if _net_edge <= _min_edge_threshold or result["confidence"] != "high":
            result["action"] = "PASS"
        # Reject BUY_NO when no_price > 0.82 — collecting tiny premium on tail risk
        # e.g. NO at 83¢ yields only 17¢ upside with fat blow-up tail; not worth it
        if result.get("action") == "BUY_NO" and market.get("no_price", 0) > 0.82:
            result["action"] = "PASS"
            result["reasoning"] = "BUY_NO skipped: no_price > 0.82 (tail risk > reward)"
        # Attach source count so run_cycle can use it for conviction sizing
        result["_source_count"] = source_count
        result["_has_rss"]      = bool(rss_signal)
        result["_has_uw"]       = bool(uw_text)
        result["_has_pplx"]     = bool(news)
        _set_cached_score(market, result)  # cache this assessment
        return {**market, **result}
    except Exception as e:
        return {**market, "action": "PASS", "edge": 0, "confidence": "low", "reasoning": str(e)[:100]}


# API call budget guard — alert if a cycle makes an unusual number of LLM calls
_API_CALL_COUNT  = 0    # reset each cycle
MAX_API_CALLS_PER_CYCLE = 80  # 30 markets * 2 APIs + buffer; >80 = runaway

# ── Token usage tracking ─────────────────────────────────────────────────────
# Haiku 4.5 pricing (2025): $0.80/M input, $4.00/M output
# Cache read tokens (prompt caching): $0.08/M
_HAIKU_IN_PRICE  = 0.80 / 1_000_000   # $ per input token
_HAIKU_OUT_PRICE = 4.00 / 1_000_000   # $ per output token
_HAIKU_CACHE_PRICE = 0.08 / 1_000_000 # $ per cache-read token

_TOK: dict = {"in": 0, "out": 0, "cache_read": 0, "calls": 0, "calls_gated": 0}

def _track_usage(resp):
    """Record token usage from an Anthropic SDK response object."""
    try:
        u = resp.usage
        _TOK["in"]   += getattr(u, "input_tokens", 0)
        _TOK["out"]  += getattr(u, "output_tokens", 0)
        _TOK["cache_read"] += getattr(u, "cache_read_input_tokens", 0)
        _TOK["calls"] += 1
    except Exception:
        pass

def _tok_cost() -> float:
    return (_TOK["in"] * _HAIKU_IN_PRICE
          + _TOK["out"] * _HAIKU_OUT_PRICE
          + _TOK["cache_read"] * _HAIKU_CACHE_PRICE)

def _tok_summary() -> str:
    cost = _tok_cost()
    return (f"[TOKENS] {_TOK['calls']} calls | "
            f"in={_TOK['in']:,} out={_TOK['out']:,} cache_rd={_TOK['cache_read']:,} | "
            f"est cost ${cost:.4f} | daily@{96}cyc ~${cost*96:.3f}/day")

def _reset_tok():
    for k in _TOK: _TOK[k] = 0

def _count_api_call():
    """Increment the per-cycle API call counter."""
    global _API_CALL_COUNT
    _API_CALL_COUNT += 1
    if _API_CALL_COUNT == MAX_API_CALLS_PER_CYCLE:
        msg = (f"\u26a0\ufe0f <b>API BUDGET WARNING</b>\n"
               f"Cycle made {_API_CALL_COUNT} LLM calls \u2014 exceeds expected maximum ({MAX_API_CALLS_PER_CYCLE}).\n"
               f"Check for runaway scoring loops.")
        tg(msg)
        log(f"[API BUDGET] WARNING: {_API_CALL_COUNT} API calls this cycle", Fore.RED)


def score_batch(markets, mode="NORMAL"):
    global _API_CALL_COUNT
    _API_CALL_COUNT = 0   # reset counter at start of each batch
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(score_market, m, mode): m for m in markets}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    log(f"  [API BUDGET] {_API_CALL_COUNT} LLM calls this scoring batch", Fore.CYAN)
    return sorted(results, key=lambda x: abs(x.get("edge", 0)), reverse=True)


# ── Executor ──────────────────────────────────────────────────────────────────

def _pre_trade_checklist(market, action, source="unknown"):
    """
    Single chokepoint called by place_trade() before any order touches the wire.
    Runs 5 cross-checks. Returns (ok: bool, reason: str).

    The Man City lesson: individual gates (sports keyword, category whitelist,
    fee guard) can each be bypassed by different code paths. This runs all
    checks in one place regardless of which path called place_trade().
    """
    q        = market.get("question", "")
    yes_p    = float(market.get("yes_price", 0.5) or 0.5)
    no_p     = float(market.get("no_price", 0.5) or 0.5)
    trade_p  = yes_p if action == "BUY_YES" else no_p
    is_fee   = is_fee_enabled_market(market)

    # ── Check 1: Fee market at extreme price ──────────────────────────────────
    # Any fee market with YES < 0.20 or YES > 0.83 → lottery ticket or decided.
    # Man City was YES=0.12, fee market → blocked here.
    if is_fee and (yes_p < 0.20 or yes_p > 0.83):
        return False, (f"[PREFLIGHT] Fee market at extreme price: YES={yes_p:.3f}. "
                       f"Lottery ticket or already decided. Source={source}")

    # ── Check 2: Fee market not in approved category — require catalyst ─────────
    # If it's a fee market and neither sports nor approved geo/macro, we need
    # a reason to be there: a whale signal or news catalyst in the market data.
    # Without one, there is no edge reason vs the market price.
    # With one, pass to scorer and let it decide.
    # Man City: is_fee=True, is_sports=False, is_approved=False, no catalyst → BLOCKED.
    # Ivory Coast at 28¢ + whale signal: is_fee=True, not approved, BUT has catalyst → ALLOWED.
    if is_fee and not is_sports_market(q) and not is_approved_category(q):
        has_whale    = bool(market.get("_whale_signal") or market.get("_has_whale"))
        has_news     = bool(market.get("_has_rss") or market.get("_has_pplx") or
                           market.get("news_catalyst") or market.get("_uw_yes_sig") or
                           market.get("_uw_no_sig"))
        has_catalyst = has_whale or has_news
        if not has_catalyst:
            return False, (f"[PREFLIGHT] Fee market not in approved category and no catalyst: "
                           f"'{q[:55]}'. Need whale signal or news to trade here. Source={source}")
        # Has catalyst — log it and allow through to scorer
        log(f"  [PREFLIGHT] Fee/uncategorized market with catalyst — allowing scorer: {q[:50]}", Fore.CYAN)

    # ── Check 3: Block finance/commodity price-level markets ─────────────────
    # WTI $120, ETH $2100, BTC $90K etc. are fee markets with 1.0-1.8% fees
    # and no informational edge for us — we have no commodity price model.
    # These are exactly the "Fee market not in approved category" type, but the
    # catalyst check (UW signal) was passing them through. Block explicitly.
    PRICE_LEVEL_KEYWORDS = [
        "hit (high)", "hit (low)", "reach $", "above $", "below $",
        "crude oil", "wti", "brent", "natural gas", "gold price",
        "silver price", "copper price",
    ]
    if is_fee and any(kw in q.lower() for kw in PRICE_LEVEL_KEYWORDS):
        return False, (f"[PREFLIGHT] Finance/commodity price-level market blocked: "
                       f"'{q[:60]}'. No edge without price model. Source={source}")

    # ── Check 4: Sports market price sanity ──────────────────────────────────
    # Sports market buying YES < 0.25 = lottery ticket even if keywords matched.
    if is_sports_market(q) and action == "BUY_YES" and yes_p < 0.25:
        return False, (f"[PREFLIGHT] Sports BUY_YES at {yes_p:.3f} < 0.25. "
                       f"Lottery ticket. Source={source}")
    if is_sports_market(q) and action == "BUY_NO" and no_p < 0.25:
        return False, (f"[PREFLIGHT] Sports BUY_NO at {no_p:.3f} < 0.25. "
                       f"Market already decided. Source={source}")

    # ── Check 4: Match-day single game market ────────────────────────────────
    # "win on YYYY-MM-DD" = game-day binary, toxic regardless of keyword match.
    import re as _re
    if _re.search(r"win on \d{4}-\d{2}-\d{2}", q.lower()):
        return False, (f"[PREFLIGHT] Match-day single game: '{q[:55]}'. "
                       f"Game-day binary blocked. Source={source}")

    # ── Check 5: Trade price too extreme on any market ───────────────────────
    # Buying YES < 0.05 or NO < 0.05 means paying 5¢ for a near-zero payout.
    # No market has edge at these extremes — it's either resolved or impossible.
    if trade_p < 0.05:
        return False, (f"[PREFLIGHT] Trade price {trade_p:.3f} < 0.05. "
                       f"Near-zero payout. Source={source}")

    return True, "ok"


def place_trade(client, market, action, size_usdc):
    from py_clob_client_v2.order_builder.constants import BUY
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    # ── Pre-trade checklist: runs regardless of which code path called us ─────
    _ok, _reason = _pre_trade_checklist(market, action, source="place_trade")
    if not _ok:
        log(_reason, Fore.YELLOW)
        return None

    if action == "BUY_YES":
        token_id = market.get("yes_token_id")
        price    = market["yes_price"]
    else:
        token_id = market.get("no_token_id")
        price    = market["no_price"] or round(1.0 - market["yes_price"], 4)

    if not token_id:
        log(f"No token ID for {action} on {market['question'][:50]}", Fore.RED)
        return None

    try:
        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price    = round(round(price / tick_f) * tick_f, tick_dec)
        price    = max(0.01, min(0.99, price))
        num_shares = round(size_usdc / price, 2)

        args    = OrderArgs(token_id=token_id, price=price, size=num_shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            log(f"✓ ORDER PLACED | {action} ${size_usdc} | {market['question'][:50]} | ID: {receipt.get('orderID','N/A')[:20]}...", Fore.GREEN)
            tg(f"✅ <b>TRADE PLACED</b>\n{action} ${size_usdc:.0f} | {market['question'][:60]}\nEdge: {market.get('edge', 0):+.3f}")
            # Record for self-learning outcome tracking
            log_trade_outcome(market, action, size_usdc, market.get("edge", 0), receipt)
            # Pre-approve conditional token allowance so we can sell this position later
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=2
                    )
                )
                log(f"  ✓ Conditional token allowance set for future sells", Fore.WHITE)
            except Exception as ae:
                log(f"  Allowance pre-approval warning: {ae}", Fore.YELLOW)
            return receipt
        else:
            log(f"✗ Order rejected: {receipt.get('errorMsg')}", Fore.RED)
            return None
    except Exception as e:
        log(f"✗ Trade failed: {e}", Fore.RED)
        return None


# ── Size Calculator ───────────────────────────────────────────────────────────

def calculate_size(edge, mode, equity_now, deployed, market_price=0.5, source_count=1):
    """
    Kelly-inspired sizing within mode-defined min/max bounds.
    Conviction multiplier: source_count (1/2/3) scales size up to SIZE_MAX.
      1 source  → base edge sizing (no bonus)
      2 sources → +25% of (SIZE_MAX - base)
      3 sources → +60% of (SIZE_MAX - base)  [near SIZE_MAX]
    The RISK_OPEN_PCT budget scales with equity at higher balances (e.g. $5k+).
    At small balances (<$5k) we use fixed SIZE_MIN/MAX directly so trades always fire.
    """
    # Hard cap: never deploy more than MAX_PORTFOLIO_EXPOSURE total
    MAX_PORTFOLIO_EXPOSURE = int(__import__('os').environ.get('MAX_PORTFOLIO_EXPOSURE', '4000'))
    remaining_capacity = MAX_PORTFOLIO_EXPOSURE - deployed
    if remaining_capacity < SIZE_MIN[mode]:
        return 0

    # Base sizing: scale between mode min and max based on edge strength
    edge_strength = min(abs(edge) / 0.30, 1.0)
    size = SIZE_MIN[mode] + (SIZE_MAX[mode] - SIZE_MIN[mode]) * edge_strength

    # Conviction multiplier: more sources = more confidence = bigger size
    # source_count=1: no bonus | 2: +25% | 3: +60% | 4 (whale): +80%
    conv_bonus = {1: 0.0, 2: 0.25, 3: 0.60, 4: 0.80, 5: 0.95}.get(min(source_count, 5), 0.0)
    if conv_bonus > 0:
        headroom = SIZE_MAX[mode] - size
        size = size + headroom * conv_bonus
        log(f"  [CONVICTION] {source_count}-source bet — size boosted to ${size:.0f}", Fore.MAGENTA)

    # At higher equity levels, also respect the percentage-based open risk budget
    if equity_now > 5000:
        open_budget = RISK_OPEN_PCT[mode] * equity_now
        remaining_budget = open_budget - deployed
        size = min(size, remaining_budget)

    # Final caps
    size = min(size, remaining_capacity, SIZE_MAX[mode])
    size = max(size, SIZE_MIN[mode]) if size >= SIZE_MIN[mode] else 0

    return round(size, 2)


# ── Order Management ─────────────────────────────────────────────────────────

def cancel_and_resubmit_stale_orders(client, current_markets_by_token):
    freed_usdc   = 0.0
    resubmitted  = 0

    try:
        # V2 SDK ClobClient does not expose get_orders — use REST API directly
        _funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
        _r = requests.get(
            f"https://clob.polymarket.com/orders?maker={_funder}&status=OPEN&limit=100",
            timeout=10
        )
        orders = _r.json() if _r.ok else []
        if isinstance(orders, dict):
            orders = orders.get("data", [])
    except Exception as e:
        log(f"Could not fetch open orders: {e}", Fore.YELLOW)
        return freed_usdc

    now = datetime.now(timezone.utc).timestamp()

    for order in orders:
        try:
            created_at   = float(order.get("created_at", now))
            age_minutes  = (now - created_at) / 60

            if age_minutes < ORDER_TTL_MINUTES:
                continue

            order_id       = order["id"]
            token_id       = order.get("asset_id", "")
            side           = order.get("side", "BUY")
            original_size  = float(order.get("original_size", 0))
            old_price      = float(order.get("price", 0))
            size_matched   = float(order.get("size_matched", 0))
            remaining      = original_size - size_matched
            usdc_locked    = remaining * old_price

            log(f"Stale order ({age_minutes:.0f} min old): {side} {remaining:.2f} shares @ ${old_price:.3f}", Fore.YELLOW)

            try:
                client.cancel(order_id)
                log(f"  Cancelled order {order_id[:16]}...", Fore.YELLOW)
                freed_usdc += usdc_locked
            except Exception as e:
                log(f"  Cancel failed: {e}", Fore.RED)
                continue

            market = current_markets_by_token.get(token_id)
            if not market:
                continue

            if order.get("outcome", "").lower() in ("yes", ""):
                current_price = market.get("yes_price", 0)
                action = "BUY_YES"
            else:
                current_price = market.get("no_price", 0)
                action = "BUY_NO"

            if not current_price or current_price <= 0.03 or current_price >= 0.97:
                continue

            edge = market.get("edge", 0)
            if abs(edge) < MIN_EDGE:
                log(f"  Edge gone (now {edge:+.3f}) — not resubmitting", Fore.WHITE)
                continue

            resubmit_size = max(round(usdc_locked, 2), 5.0)
            log(f"  Edge still strong ({edge:+.3f}) — resubmitting at ${current_price:.3f}", Fore.CYAN)
            receipt = place_trade(client, {**market, "yes_price": current_price if action == "BUY_YES" else market["yes_price"]}, action, resubmit_size)
            if receipt:
                freed_usdc -= resubmit_size
                resubmitted += 1

        except Exception as e:
            log(f"Error processing order: {e}", Fore.RED)
            continue

    if freed_usdc > 0 or resubmitted > 0:
        log(f"Order cleanup: freed ${freed_usdc:.2f}, resubmitted {resubmitted}", Fore.CYAN)

    return max(freed_usdc, 0.0)


# ── Position Manager ─────────────────────────────────────────────────────────


# =============================================================================
# =============================================================================
#  ORACLE CHECK (FINAL 24H)
#  In the last 24 hours before market resolution, bypass LLM and query the
#  actual settlement source directly:
#    - Weather markets → Weather Underground station data
#    - Geopolitical/macro → Perplexity forced "what happened today" query
#  Result directly updates the position hold/sell decision.
# =============================================================================

ORACLE_CHECK_WINDOW_H = 24.0   # Only run oracle check inside this window

def oracle_check_weather(position: dict) -> dict | None:
    """
    For weather temperature markets expiring in <24h, fetch the actual
    recorded temperature from Weather Underground (the settlement source).
    Returns {"verdict": "HOLD"|"SELL"|"UNKNOWN", "reason": str, "data": str}
    """
    title = position.get("title", "")
    resolution_src = position.get("resolutionSource", "")

    # Only handle temperature markets
    if "temperature" not in title.lower() and "temperature" not in resolution_src.lower():
        return None

    try:
        import re as _re
        from datetime import datetime as _dt, timezone as _tz

        # Extract city from title ("highest temperature in CITY on DATE")
        city_match = _re.search(r'temperature in ([\w\s]+?) (?:be|on)', title, _re.IGNORECASE)
        city = city_match.group(1).strip() if city_match else ""

        # Parse bucket from position outcome/title  
        outcome = position.get("outcome", "")  # e.g. "Yes" means we hold YES
        avg_price = float(position.get("avgPrice", 0) or 0)

        # Extract target temp from title
        temp_match = _re.search(r'be (\d+)(?:\s*[\u00b0\u2103\u2109]|\s*[CF]|\s*degrees)', title)
        range_match = _re.search(r'between (\d+)[\s\-]+(\d+)', title)
        gte_match   = _re.search(r'(\d+)[\s\u00b0CF]* or higher', title, _re.IGNORECASE)
        lte_match   = _re.search(r'(\d+)[\s\u00b0CF]* or (?:below|lower)', title, _re.IGNORECASE)

        if range_match:
            bucket_low  = float(range_match.group(1))
            bucket_high = float(range_match.group(2))
            is_gte = False
        elif gte_match:
            bucket_low  = float(gte_match.group(1))
            bucket_high = 9999.0
            is_gte = True
        elif lte_match:
            bucket_low  = -999.0
            bucket_high = float(lte_match.group(1))
            is_gte = False
        elif temp_match:
            bucket_low  = float(temp_match.group(1))
            bucket_high = bucket_low + 1.0
            is_gte = False
        else:
            return {"verdict": "UNKNOWN", "reason": "Could not parse bucket from title", "data": ""}

        # Get forecast from weather scout cities
        from weather_scout import get_city_forecast_high, CITY_CONFIGS
        unit = "C"
        for _city, _cfg in CITY_CONFIGS.items():
            if _city.lower() in city.lower() or city.lower() in _city.lower():
                unit = _cfg.get("unit", "C")
                # Use today's date for oracle (market expiring today/tomorrow)
                today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
                forecast_high, _ = get_city_forecast_high(_city, today)
                if forecast_high is not None:
                    # Does the forecast land in our bucket?
                    in_bucket = False
                    if is_gte:
                        in_bucket = forecast_high >= bucket_low
                    elif bucket_low == -999.0:
                        in_bucket = forecast_high <= bucket_high
                    else:
                        in_bucket = bucket_low <= forecast_high < bucket_high + 1.0

                    verdict = "HOLD" if in_bucket else "SELL"
                    reason  = (f"Oracle: {_city} forecast high = {forecast_high:.1f}°{unit}. "
                               f"Bucket [{bucket_low},{bucket_high}] → {'IN BUCKET' if in_bucket else 'NOT in bucket'}")
                    log(f"[ORACLE] {title[:55]} → {verdict}: {reason}", Fore.MAGENTA)
                    return {"verdict": verdict, "reason": reason,
                            "data": f"{forecast_high:.1f}°{unit}"}
                break

        return {"verdict": "UNKNOWN", "reason": f"No forecast for {city}", "data": ""}

    except Exception as e:
        log(f"[ORACLE] Weather check error: {e}", Fore.YELLOW)
        return {"verdict": "UNKNOWN", "reason": str(e), "data": ""}


def oracle_check_geopolitical(position: dict, pplx_key: str) -> dict | None:
    """
    For geopolitical/macro markets in final 24h, query Perplexity with a
    forced real-time prompt: "Has [outcome] actually happened today?"
    Returns {"verdict": "HOLD"|"SELL"|"UNKNOWN", "reason": str}
    """
    title   = position.get("title", "")
    outcome = position.get("outcome", "")  # "Yes" or "No"
    avg_p   = float(position.get("avgPrice", 0) or 0)

    geo_kw = ["iran", "ceasefire", "regime", "ukraine", "russia", "war", "invasion",
               "nato", "north korea", "china", "taiwan", "forces enter", "conflict ends"]
    if not any(kw in title.lower() for kw in geo_kw):
        return None  # Not a geopolitical market

    if not pplx_key:
        return None

    try:
        prompt = (
            f"Market question: '{title}'\n"
            f"We hold a {outcome.upper()} position (entry price {avg_p:.3f}).\n"
            f"This market resolves TODAY or TOMORROW. \n\n"
            f"Search for the very latest news (last 24 hours) and answer:\n"
            f"Has the '{outcome.upper()}' outcome already happened or is it clearly about to happen?\n"
            f"Or has something happened that makes '{outcome.upper()}' impossible or very unlikely?\n\n"
            f"Respond in ONE line: HOLDS | SELL | UNCERTAIN, then one sentence explaining why."
        )
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": "sonar",
                "messages": [
                    {"role": "system", "content": "You are a prediction market analyst. Use real-time search. Be direct and specific."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 120,
                "temperature": 0.0,
            },
            headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
            timeout=20,
        )
        resp_text = r.json()["choices"][0]["message"]["content"].strip()
        first_line = resp_text.split("\n")[0].upper()
        if "SELL" in first_line:
            verdict = "SELL"
        elif "HOLDS" in first_line or "HOLD" in first_line:
            verdict = "HOLD"
        else:
            verdict = "UNCERTAIN"
        log(f"[ORACLE] Geo check {title[:45]} → {verdict}: {resp_text[:80]}", Fore.MAGENTA)
        return {"verdict": verdict, "reason": resp_text[:200]}
    except Exception as e:
        log(f"[ORACLE] Geo check error: {e}", Fore.YELLOW)
        return None


def run_oracle_checks(client):
    """
    Run oracle checks on all open positions expiring in <24h.
    If oracle returns SELL with high confidence, executes the exit.
    """
    from datetime import datetime as _dt, timezone as _tz
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    now = _dt.now(_tz.utc)

    try:
        pos_resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=20",
            timeout=10
        )
        if pos_resp.status_code != 200:
            return
        positions = pos_resp.json()
    except Exception as e:
        log(f"[ORACLE] Positions fetch failed: {e}", Fore.YELLOW)
        return

    for p in positions:
        cur_val  = float(p.get("currentValue", 0) or 0)
        if cur_val < 20:
            continue

        # Check time to expiry
        end_date_str = p.get("endDate", "")
        if not end_date_str:
            continue
        try:
            end_dt = _dt.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
        except Exception:
            continue

        if hours_left > ORACLE_CHECK_WINDOW_H or hours_left < 0:
            continue  # Outside oracle window

        title   = p.get("title", "")
        outcome = p.get("outcome", "")
        token_id = p.get("asset", "")
        size     = float(p.get("size", 0) or 0)

        log(f"[ORACLE] Checking {title[:50]} ({hours_left:.1f}h left)", Fore.MAGENTA)

        # Try weather oracle first
        oracle_result = oracle_check_weather(p)

        # Then geopolitical if not weather
        if oracle_result is None:
            oracle_result = oracle_check_geopolitical(p, pplx_key)

        if oracle_result is None or oracle_result["verdict"] == "UNKNOWN":
            log(f"[ORACLE] No oracle available for {title[:40]}", Fore.YELLOW)
            continue

        verdict = oracle_result["verdict"]
        reason  = oracle_result["reason"]

        if verdict == "SELL":
            log(f"[ORACLE] SELL signal — exiting {title[:45]}: {reason[:80]}", Fore.RED)
            tg(f"⚠️ <b>Oracle exit (final 24h)</b>\n{title[:60]}\nReason: {reason[:150]}\nSelling ${cur_val:.0f} position")
            # Execute the sell
            try:
                from py_clob_client_v2.order_builder.constants import SELL as _SELL
                from py_clob_client_v2.clob_types import OrderArgs as _OA, OrderType as _OT
                _book = client.get_midpoint(token_id)
                _mid  = float(_book.get("mid", 0.5))
                _tick     = client.get_tick_size(token_id)
                _tick_f   = float(_tick)
                _tick_dec = len(str(_tick).rstrip("0").split(".")[-1]) if "." in str(_tick) else 0
                _sp = round(round(_mid / _tick_f) * _tick_f, _tick_dec)
                _sp = max(0.01, min(0.99, _sp))
                _args = _OA(token_id=token_id, price=_sp, size=round(size, 2), side=_SELL)
                _signed  = client.create_order(_args)  # plain — no neg_risk options (avoids allowance error)
                _receipt = client.post_order(_signed, _OT.GTC)
                if _receipt.get("success"):
                    avg_p = float(p.get("avgPrice", 0) or 0)
                    pnl = (_sp - avg_p) * size
                    log(f"[ORACLE] ✓ SOLD @ {_sp:.3f} | P&L ${pnl:+.2f}", Fore.GREEN)
                    tg(f"✅ <b>Oracle exit executed</b>\n{title[:55]}\nSold @ {_sp:.3f} | P&L ${pnl:+.2f}")
                else:
                    log(f"[ORACLE] Sell failed: {_receipt.get('errorMsg','')}", Fore.RED)
            except Exception as se:
                log(f"[ORACLE] Sell error: {se}", Fore.YELLOW)

        elif verdict == "HOLD":
            log(f"[ORACLE] HOLD confirmed — {title[:45]}: {reason[:80]}", Fore.GREEN)


# =============================================================================
#  THESIS INVALIDATION EXIT
#  Every cycle, re-checks news on open positions valued > $50.
#  If Perplexity + Claude both say the original thesis is broken,
#  auto-sells the position and logs the reason.
# =============================================================================

# Cache: {token_id: {"thesis": str, "checked_at": float, "bought_at": float}}
# Persisted to disk so it survives service restarts (prevents buy-then-sell churn)
THESIS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thesis_cache.json")
THESIS_RECHECK_INTERVAL = 3600   # re-check each position at most once per hour
THESIS_MIN_VALUE = 50.0          # only check positions worth >$50
THESIS_NEW_POSITION_GRACE = 4 * 3600  # never thesis-exit within 4h of buying

def _load_thesis_cache() -> dict:
    """Load thesis cache from disk, return empty dict on any error."""
    try:
        if os.path.exists(THESIS_CACHE_FILE):
            with open(THESIS_CACHE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_thesis_cache(cache: dict) -> None:
    """Persist thesis cache to disk."""
    try:
        with open(THESIS_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        log(f"[THESIS] Cache save failed: {e}", Fore.YELLOW)

_THESIS_CACHE: dict = _load_thesis_cache()

def check_thesis_invalidation(client):
    """
    For each open position > $50, ask Perplexity for fresh news,
    then ask Claude if the original thesis is still valid.
    If Claude says INVALID with high confidence, sell immediately.
    """
    import anthropic, time as _time
    api_key  = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key or not pplx_key:
        return

    try:
        # Get current positions from data API (faster than CLOB trades scan)
        pos_resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=20",
            timeout=10
        )
        if pos_resp.status_code != 200:
            return
        positions = pos_resp.json()
    except Exception as e:
        log(f"[THESIS] Could not fetch positions: {e}", Fore.YELLOW)
        return

    now = _time.time()
    for p in positions:
        cur_val  = float(p.get("currentValue", 0) or 0)
        if cur_val < THESIS_MIN_VALUE:
            continue

        token_id     = p.get("asset", "")
        condition_id = p.get("conditionId", p.get("condition_id", ""))
        title        = p.get("title", "")
        outcome      = p.get("outcome", "")  # "Yes" or "No"
        avg_p        = float(p.get("avgPrice", 0) or 0)
        size         = float(p.get("size", 0) or 0)

        # ── HARD SKIP 1: Short-duration markets (sports games, daily markets) ──
        # Any position expiring within 3 days is NEVER thesis-sold.
        # Sports games expire same-day. Thesis checking a live game = disaster.
        # High price (90c+) during a game means the team is WINNING, not done.
        import datetime as _thesis_dt
        _end_date_str = p.get("endDate", "") or p.get("end_date", "")
        if _end_date_str:
            try:
                _end_dt  = _thesis_dt.datetime.fromisoformat(_end_date_str.replace("Z", "+00:00"))
                _now_dt  = _thesis_dt.datetime.now(_thesis_dt.timezone.utc)
                _days_left = (_end_dt - _now_dt).total_seconds() / 86400
                if _days_left < 3:
                    log(f"[THESIS] {title[:45]} — expires in {_days_left:.1f}d, skip", Fore.MAGENTA)
                    continue
            except Exception:
                pass

        # ── HARD SKIP 2: Condition ID blacklist ───────────────────────────
        _THESIS_BLACKLIST = {
            "0x87254ca39f82f1fdef981066710fb49904e86358bcf1ed9a4e05e4558d665329",  # Duke vs UConn
            "0xb54b0241f4f33e6734a29df5d59e0b4fbb7b96cf92d2c7151622cdb294c94516",  # Clippers vs Bucks ML
        }
        if condition_id in _THESIS_BLACKLIST:
            log(f"[THESIS] {title[:45]} — condition ID blacklisted, skip", Fore.MAGENTA)
            continue

        # ── HARD SKIP 3: Sports keywords (belt+suspenders) ────────────────
        _SPORTS_GAME_KEYWORDS = [
            " vs. ", " vs ", "spread:", "o/u ", "over/under",
            "clippers", "bucks", "celtics", "lakers", "heat", "nuggets",
            "thunder", "spurs", "knicks", "pistons", "warriors", "nets",
            "duke", "uconn", "michigan", "tennessee", "connecticut huskies",
            "blue devils", "ncaa", "nba ", "nhl ", "nfl ", "atp ", "wta ",
        ]
        if any(kw in title.lower() for kw in _SPORTS_GAME_KEYWORDS):
            log(f"[THESIS] {title[:45]} — sports keyword match, skip", Fore.MAGENTA)
            continue

        # Grace period: never thesis-exit a position bought in last 4 hours.
        # This prevents buy-then-sell churn when service restarts and cache is cold.
        # Record "first_seen" the moment we first encounter this token_id.
        if token_id not in _THESIS_CACHE:
            _THESIS_CACHE[token_id] = {"checked_at": 0, "first_seen": now}
            _save_thesis_cache(_THESIS_CACHE)
        first_seen = _THESIS_CACHE[token_id].get("first_seen", now)
        if now - first_seen < THESIS_NEW_POSITION_GRACE:
            age_min = (now - first_seen) / 60
            log(f"[THESIS] {title[:45]} — grace period ({age_min:.0f}m / 240m elapsed), skip", Fore.MAGENTA)
            continue

        # Rate limit: skip if checked recently
        last_check = _THESIS_CACHE[token_id].get("checked_at", 0)
        if now - last_check < THESIS_RECHECK_INTERVAL:
            continue

        _THESIS_CACHE[token_id]["checked_at"] = now
        _save_thesis_cache(_THESIS_CACHE)

        log(f"[THESIS] Checking: {title[:55]} ({outcome} @ {avg_p:.3f})", Fore.MAGENTA)

        # Step 1: Get fresh news via Perplexity
        fresh_news = ""
        try:
            r = requests.post(
                "https://api.perplexity.ai/chat/completions",
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "Find the latest news (last 24 hours) about this topic. Focus on any events that could change the outcome. Be brief and factual."},
                        {"role": "user", "content": title},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.1,
                },
                headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
                timeout=20,
            )
            fresh_news = r.json()["choices"][0]["message"]["content"]
        except Exception as ne:
            log(f"[THESIS] Perplexity failed for {title[:40]}: {ne}", Fore.YELLOW)
            continue

        # Step 2: Ask Claude if thesis is still valid
        prompt = (
            f"We hold a prediction market position: {outcome.upper()} on '{title}'\n"
            f"Entry price: {avg_p:.3f} | Current value: ${cur_val:.2f}\n"
            f"\nFresh news (last 24h):\n{fresh_news[:500]}\n\n"
            f"Question: Is our {outcome.upper()} thesis STILL VALID, or has something happened that INVALIDATES it?\n"
            f"Respond with ONLY one of: VALID | INVALID | UNCERTAIN\n"
            f"Then on the next line, one sentence explaining why (max 100 chars)."
        )

        try:
            if not hasattr(score_market, "_ac") or score_market._ac is None:
                score_market._ac = anthropic.Anthropic(api_key=api_key)
            resp = score_market._ac.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            _track_usage(resp)
            verdict_text = resp.content[0].text.strip()
            verdict      = verdict_text.split("\n")[0].strip().upper()
            reason_line  = verdict_text.split("\n")[1].strip() if "\n" in verdict_text else ""
        except Exception as ce:
            log(f"[THESIS] Claude verdict failed: {ce}", Fore.YELLOW)
            continue

        log(f"[THESIS] {title[:45]} → {verdict}: {reason_line[:80]}", Fore.MAGENTA)

        if verdict == "INVALID":
            # Auto-sell
            log(f"[THESIS] THESIS BROKEN — auto-selling {outcome} position", Fore.RED)
            tg(f"\u26a0\ufe0f <b>Thesis Invalidated</b>\n{title[:60]}\nReason: {reason_line[:120]}\nSelling {outcome} position (${cur_val:.0f})")
            _execute_thesis_sell(client, token_id, size, reason_line)
        elif verdict == "UNCERTAIN":
            tg(f"\u26a0\ufe0f <b>Thesis Uncertain</b>\n{title[:60]}\n{reason_line[:120]}\nMonitoring closely.", silent=True)

def _execute_thesis_sell(client, token_id: str, shares: float, reason: str):
    """Execute a market sell for thesis invalidation."""
    try:
        from py_clob_client_v2.order_builder.constants import SELL
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        book = client.get_midpoint(token_id)
        cur_price = float(book.get("mid", 0.5))
        tick      = client.get_tick_size(token_id)
        tick_f    = float(tick)
        tick_dec  = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        sell_price = round(round(cur_price / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        args       = OrderArgs(token_id=token_id, price=sell_price, size=round(shares, 2), side=SELL)
        signed     = client.create_order(args)  # plain — no neg_risk options (avoids allowance error)
        receipt    = client.post_order(signed, OrderType.GTC)
        if receipt.get("success"):
            log(f"[THESIS] Sold @ {sell_price:.3f} | {reason[:80]}", Fore.GREEN)
            tg(f"\u2705 <b>Thesis exit sold</b> @ {sell_price:.3f}\n{reason[:100]}")
        else:
            log(f"[THESIS] Sell failed: {receipt.get('errorMsg','')}", Fore.RED)
    except Exception as e:
        log(f"[THESIS] _execute_thesis_sell error: {e}", Fore.YELLOW)

def manage_positions(client):
    """
    Sell filled positions at profit target, stop loss, or near resolution.
    Uses CLOB trade history + Polymarket /positions API to catch all positions
    (including ones bought outside of this bot or in previous sessions).
    """
    try:
        trades = client.get_trades()
        if not trades:
            return
    except Exception as e:
        log(f"Could not fetch trades: {e}", Fore.YELLOW)
        return

    positions = {}
    for t in trades:
        token_id = t.get("asset_id", "")
        side     = t.get("side", "BUY")
        price    = float(t.get("price", 0))
        size     = float(t.get("size", 0))
        if not token_id or price == 0:
            continue
        if token_id not in positions:
            positions[token_id] = {"side": side, "shares": 0, "cost": 0}
        if side == "BUY":
            positions[token_id]["shares"] += size
            positions[token_id]["cost"]   += size * price
        else:
            positions[token_id]["shares"] -= size

    # ── Augment with Polymarket /positions API ────────────────────────────────
    # The CLOB trade history is paged and may miss older or external trades.
    # The /positions endpoint reflects the actual on-chain token balances.
    try:
        import requests as _pr
        _pos_r = _pr.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=100",
                         timeout=10)
        if _pos_r.status_code == 200:
            for _p in _pos_r.json():
                _asset = _p.get("asset", "")
                _outcome = _p.get("outcome", "NO")
                _size = float(_p.get("size", 0))
                _avg_price = float(_p.get("avgPrice") or _p.get("averagePrice") or 0)
                if not _asset or _size < 0.1:
                    continue
                if _asset not in positions:
                    # Position exists on-chain but not in CLOB trade history — add it
                    # Use side="BUY" always: we hold the token (YES or NO) and profit_target
                    # check is: current_price >= PROFIT_TARGET (token price appreciation)
                    _fallback_entry = _avg_price if _avg_price > 0 else 0.50
                    positions[_asset] = {"side": "BUY", "shares": _size, "cost": _size * _fallback_entry}
                    log(f"  [POSITIONS API] Added {_asset[:16]}... {_size:.2f} {_outcome} shares @ entry {_fallback_entry:.3f}", Fore.CYAN)
                elif abs(positions[_asset]["shares"] - _size) > 1.0:
                    # Significant discrepancy — trust the on-chain balance
                    _old = positions[_asset]["shares"]
                    positions[_asset]["shares"] = _size
                    # Also correct cost and side using positions API data
                    # curPrice from positions API is the actual token price (not YES inverse)
                    # Use side="BUY" + avgPrice for correct profit check direction
                    if _avg_price > 0:
                        positions[_asset]["cost"] = _size * _avg_price
                        positions[_asset]["side"] = "BUY"  # profit check: current >= PROFIT_TARGET
                    log(f"  [POSITIONS API] Corrected {_asset[:16]}... shares {_old:.2f} → {_size:.2f}, side=BUY, entry={_avg_price:.3f}", Fore.CYAN)
    except Exception as _pe:
        log(f"  positions API augmentation failed: {_pe}", Fore.YELLOW)

    # ── SPORTS BLACKLIST: read from JSON file every cycle (bypasses .pyc cache) ──
    # This is the one protection that works even if bytecode is stale.
    # Add any sports market conditionId to /opt/polymarket-agent/sports_blacklist.json
    _sports_bl_cids  = set()
    _sports_bl_tokens = set()
    try:
        import json as _jmod
        _bl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sports_blacklist.json")
        if os.path.exists(_bl_path):
            _bl_data = _jmod.load(open(_bl_path))
            _sports_bl_cids   = set(_bl_data.get("condition_ids", []))
            _sports_bl_tokens = set(_bl_data.get("yes_tokens", []))
    except Exception:
        pass

    # Build title lookup from positions API so we can skip sports markets
    _title_lookup = {}
    try:
        import requests as _pr2
        _pos_r2 = _pr2.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=100", timeout=8)
        if _pos_r2.status_code == 200:
            for _p2 in _pos_r2.json():
                _asset2 = _p2.get("asset", "")
                _title2 = _p2.get("title", "") or _p2.get("market", "")
                if _asset2 and _title2:
                    _title_lookup[_asset2] = _title2
    except Exception:
        pass

    # Sports keywords — positions with these titles are NEVER auto-sold by profit checker
    _PROFIT_SPORTS_KEYWORDS = [
        " vs. ", " vs ", "spread:", "will ", "win on 2026", "win on 2025",
        "clippers", "bucks", "celtics", "lakers", "heat", "nuggets", "thunder",
        "duke", "uconn", "michigan", "tennessee", "spain", "germany", "france",
        "connecticut", "ncaa", "nba ", "nhl ", "nfl ", "atp ", "wta ",
        "miami open", "fifa", "world cup",
    ]

    for token_id, pos in positions.items():
        shares = pos["shares"]
        if shares <= 0.1:
            continue

        # Skip if in JSON sports blacklist (runtime check — bypasses .pyc)
        _pos_cid = pos.get("condition_id", pos.get("conditionId", ""))
        if token_id in _sports_bl_tokens or _pos_cid in _sports_bl_cids:
            log(f"  [PROFIT-SKIP-JSON] {token_id[:16]}... in sports_blacklist.json", Fore.CYAN)
            continue

        # Skip sports game markets — they resolve on score, not thesis
        _pos_title = _title_lookup.get(token_id, "").lower()
        if _pos_title and any(kw in _pos_title for kw in _PROFIT_SPORTS_KEYWORDS):
            log(f"  [PROFIT-SKIP] {_pos_title[:45]} — sports market, skip profit check", Fore.CYAN)
            continue

        avg_entry  = pos["cost"] / (pos["shares"] + 1e-9)
        trade_side = pos["side"]

        try:
            book          = client.get_midpoint(token_id)
            current_price = float(book.get("mid", avg_entry))
        except Exception:
            continue

        should_sell = False
        reason      = ""

        # ── Calendar Spread Hard Loss Cap ──────────────────────────────────────
        # Calendar spreads ("by April X", "extended by", etc.) are timing bets.
        # They lose fast when the event resolution date passes. Hard cut at $200 loss.
        _bucket = classify_bucket(title)
        if _bucket == 'CALENDAR':
            _cost     = avg_entry * shares if trade_side == 'BUY' else (1.0 - avg_entry) * shares
            _cur_val  = current_price * shares if trade_side == 'BUY' else (1.0 - current_price) * shares
            _unreal   = _cur_val - _cost
            if _unreal < -BUCKET_CALENDAR_MAX_LOSS:
                should_sell = True
                reason = f"CALENDAR hard loss cap: unrealized ${_unreal:.2f} < -${BUCKET_CALENDAR_MAX_LOSS}"
                log(f"[CALENDAR-CAP] {title[:50]} | loss ${_unreal:.2f} — hard exit", Fore.RED)

        if trade_side == "BUY":
            if current_price >= PROFIT_TARGET:
                should_sell = True
                reason = f"Profit target hit (entry ${avg_entry:.3f} → now ${current_price:.3f})"
            elif current_price <= avg_entry * (1 - STOP_LOSS):
                should_sell = True
                reason = f"Stop loss hit (entry ${avg_entry:.3f} → now ${current_price:.3f})"
            elif current_price >= NEAR_RESOLUTION_THRESHOLD:
                should_sell = True
                reason = f"Near resolution at ${current_price:.3f} — locking in gain"
        else:
            no_entry   = 1.0 - avg_entry
            no_current = 1.0 - current_price
            if no_current >= PROFIT_TARGET:
                should_sell = True
                reason = f"NO profit target hit (NO entry ${no_entry:.3f} → now ${no_current:.3f})"
            elif current_price >= avg_entry + STOP_LOSS:
                should_sell = True
                reason = f"Stop loss hit on NO position"
            elif no_current >= NEAR_RESOLUTION_THRESHOLD:
                should_sell = True
                reason = f"NO near resolution at ${no_current:.3f} — locking in gain"

        # ── Profit-lock: sell HALF when unrealized gain ≥ 40% (NO positions) ──────
        # Locking in half preserves capital while keeping upside on the remainder.
        # Persisted to profit_locks.json so we don't re-trigger after a restart.
        if not should_sell and trade_side == "SELL" and shares > 5:
            no_entry   = 1.0 - avg_entry
            no_current = 1.0 - current_price
            gain_pct   = (no_current - no_entry) / no_entry if no_entry > 0 else 0
            try:
                pl_data = json.load(open(PROFIT_LOCK_FILE)) if os.path.exists(PROFIT_LOCK_FILE) else {}
            except Exception:
                pl_data = {}
            if gain_pct >= PROFIT_LOCK_GAIN and token_id not in pl_data:
                import math as _math2
                half_shares = _math2.floor(shares / 2 * 100) / 100  # floor to 2dp
                log(f"[PROFIT-LOCK] NO position +{gain_pct*100:.0f}% gain — selling half ({half_shares} shares)", Fore.GREEN)
                tg(f"🔒 <b>Profit-lock: selling half</b>\nNO gain {gain_pct*100:.0f}% (entry {no_entry:.3f} → now {no_current:.3f})\nSelling {half_shares} of {shares:.0f} shares to lock gain")
                try:
                    from py_clob_client_v2.order_builder.constants import SELL as _SELL
                    from py_clob_client_v2.clob_types import OrderArgs as _OA, OrderType as _OT
                    _tick     = client.get_tick_size(token_id)
                    _tick_f   = float(_tick)
                    _tick_dec = len(str(_tick).rstrip("0").split(".")[-1]) if "." in str(_tick) else 0
                    _sp = round(round(current_price / _tick_f) * _tick_f, _tick_dec)
                    _sp = max(0.01, min(0.99, _sp))
                    _args = _OA(token_id=token_id, price=_sp, size=half_shares, side=_SELL)
                    _signed  = client.create_order(_args)  # plain — no neg_risk options (avoids allowance error)
                    _receipt = client.post_order(_signed, _OT.GTC)
                    if _receipt.get("success"):
                        _pnl = (_sp - avg_entry) * half_shares
                        log(f"[PROFIT-LOCK] ✓ Sold {half_shares} @ {_sp:.3f} | locked P&L ${_pnl:+.2f}", Fore.GREEN)
                        pl_data[token_id] = {"fired_at": time.time(), "shares_sold": half_shares, "price": _sp}
                        try:
                            with open(PROFIT_LOCK_FILE, "w") as _f:
                                json.dump(pl_data, _f)
                        except Exception:
                            pass
                    else:
                        log(f"[PROFIT-LOCK] Sell failed: {_receipt.get('errorMsg','')}", Fore.RED)
                except Exception as _e:
                    log(f"[PROFIT-LOCK] Error: {_e}", Fore.YELLOW)

        if should_sell:
            log(f"SELL SIGNAL: {reason}", Fore.CYAN)
            try:
                from py_clob_client_v2.order_builder.constants import SELL
                from py_clob_client_v2.clob_types import OrderArgs, OrderType
                tick     = client.get_tick_size(token_id)
                tick_f   = float(tick)
                tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
                sell_price = round(round(current_price / tick_f) * tick_f, tick_dec)
                sell_price = max(0.01, min(0.99, sell_price))
                # CRITICAL: Use CLOB-reported balance (floor to 2dp) — never round up
                # round(337.238879, 2) = 337.24 which EXCEEDS 337.238879 → 400 error
                import math as _math
                _clob_bal = shares
                try:
                    from py_clob_client_v2.clob_types import BalanceAllowanceParams as _BAP2, AssetType as _AT2
                    _bal_resp = client.get_balance_allowance(params=_BAP2(
                        asset_type=_AT2.CONDITIONAL, token_id=token_id, signature_type=2))
                    _raw = int(_bal_resp.get("balance", 0))
                    if _raw > 0:
                        _clob_bal = _raw / 1e6
                        log(f"  CLOB balance: {_clob_bal:.6f} shares", Fore.WHITE)
                except Exception as _be:
                    log(f"  Balance check: {_be} — using trade-calc", Fore.YELLOW)
                sell_size = _math.floor(_clob_bal * 100) / 100  # floor to 2dp, never exceed balance
                if sell_size < 0.01:
                    log(f"  Sell size {sell_size} too small — skipping (token may be expired)", Fore.YELLOW)
                    continue
                args    = OrderArgs(token_id=token_id, price=sell_price, size=sell_size, side=SELL)
                signed  = client.create_order(args)  # plain — no neg_risk options (avoids allowance error)
                receipt = client.post_order(signed, OrderType.GTC)
                if receipt.get("success"):
                    pnl = (sell_price - avg_entry) * sell_size
                    log(f"✓ SOLD {sell_size:.2f} shares @ ${sell_price:.3f} | PnL: ${pnl:+.2f} | {reason}", Fore.GREEN)
                    tg(f"💰 <b>SOLD</b> {sell_size:.1f} shares @ ${sell_price:.3f}\nP&L: ${pnl:+.2f} | {reason[:80]}")
                    # ── Post-trade review hook ─────────────────────────────────────────────
                    try:
                        import importlib.util as _ptr_ilu
                        _ptr_spec = _ptr_ilu.spec_from_file_location(
                            "post_trade_review",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "post_trade_review.py")
                        )
                        if _ptr_spec:
                            _ptr_mod = _ptr_ilu.module_from_spec(_ptr_spec)
                            _ptr_spec.loader.exec_module(_ptr_mod)
                            _market_type = _classify_market(market.get("question", ""))
                            _ptr_mod.write_review(
                                market_title=market.get("question", "")[:120],
                                strategy_type="directional",
                                category=_market_type,
                                side="YES" if "YES" in reason.upper() or action == "BUY_YES" else "NO",
                                entry_price=avg_entry,
                                exit_price=sell_price,
                                size=sell_size,
                                realized_pnl=pnl,
                                trigger_source=reason[:80],
                                trade_type="automated",
                                token_id=token_id,
                                primary_failure_mode=(
                                    "bad_exit_discipline" if pnl < -10
                                    else "expiry_compression_misread" if "expir" in reason.lower()
                                    else "none"
                                ),
                                process_quality="good",
                                source_module="autotrader",
                                notes=f"reason={reason[:100]}",
                            )
                    except Exception as _ptr_e:
                        log(f"[LEARN] post_trade_review hook error: {_ptr_e}", Fore.YELLOW)
                else:
                    err_msg = receipt.get('errorMsg', '')
                    if "not enough balance" in err_msg or "allowance" in err_msg:
                        log(f"  Balance/allowance error — token may have expired or already sold. Skipping.", Fore.YELLOW)
                    else:
                        log(f"Sell order failed: {err_msg}", Fore.RED)
            except Exception as e:
                err = str(e)
                if "not enough balance" in err or "allowance" in err:
                    # Balance error on exception path — token likely expired/resolved
                    log(f"  Balance/allowance error (exception): {err[:100]}", Fore.YELLOW)
                    log(f"  Token {token_id[:20]}... may be expired or already sold — skipping.", Fore.YELLOW)
                else:
                    log(f"Sell failed: {e}", Fore.RED)
                    log_mistake("Sell failed", f"token {token_id[:20]}", str(e)[:150], "Check CLOB balance before sell")


# ── News Arbitrage Layer ─────────────────────────────────────────────────────
#
# Strategy: scan global news every 5 min, find breaking events that haven't
# been priced into Polymarket yet, trade immediately at full size.
# This is the "obvious locally, not priced globally" edge.

NEWS_FEEDS = [
    # Global wire services
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    # Politics / geopolitics
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://feeds.reuters.com/reuters/worldNews",
    # Finance / markets
    "https://feeds.reuters.com/reuters/businessNews",
    # Sports (for game result arb)
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nfl/news",
]


def fetch_news_headlines():
    """Pull headlines from RSS feeds, return list of recent headline strings."""
    import xml.etree.ElementTree as ET
    headlines = []
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                desc  = item.findtext("description", "").strip()[:150]
                pub   = item.findtext("pubDate", "").strip()
                if title:
                    headlines.append(f"{title} | {desc} [{pub}]")
        except Exception:
            continue
    return headlines[:120]  # cap at 120 headlines


def news_arb_scan(client, state, markets_cache):
    """
    1. Fetch latest headlines from global news feeds
    2. Ask Perplexity to match headlines to open Polymarket questions
    3. For each match where the outcome is clear, score and trade immediately
    Returns number of trades placed.
    """
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not pplx_key:
        return 0

    mode = state.get("mode", "NORMAL")
    if mode == "PAUSED":
        return 0

    try:
        headlines = fetch_news_headlines()
    except Exception as e:
        log(f"[NEWS ARB] Feed fetch failed: {e}", Fore.YELLOW)
        return 0

    if not headlines:
        return 0

    # Use markets_cache (passed in from last full scan) to avoid re-fetching
    markets = markets_cache if markets_cache else []
    if not markets:
        return 0

    # Build a compact market list for the prompt
    market_list = "\n".join(
        f"- [{m['id'][:12]}] {m['question']} | YES={m['yes_price']:.2f} NO={m['no_price']:.2f}"
        for m in markets[:60]
    )
    headline_block = "\n".join(headlines[:60])

    prompt = f"""You are a prediction market arbitrage scanner. Be EXTREMELY strict.

Here are the latest global news headlines (past few hours):
{headline_block}

Here are open Polymarket questions with current prices:
{market_list}

Your task:
1. Find headlines that DIRECTLY and UNAMBIGUOUSLY resolve a listed market
2. The headline must mention the EXACT same team, person, country, or event as the market question
3. Only flag if confidence >= 0.92 (near-certain outcome)
4. Focus ONLY on: confirmed final scores, official election results, confirmed policy decisions
5. REJECT any match where the headline is about a different sport, team, league, or event
6. REJECT vague correlations, partial matches, or different competitions (e.g. rugby != football)
7. A headline about Team A does NOT imply anything about Team B

Respond with ONLY a JSON array (empty if no matches):
[{{"market_id": "first 12 chars", "action": "BUY_YES or BUY_NO", "confidence": 0.0-1.0, "headline": "the specific headline", "reasoning": "exact match explanation — name the shared entity"}}]

If uncertain, return []"""

    try:
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": "sonar",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 800,
                "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        # Extract JSON array
        if "[" in text:
            text = text[text.index("["):text.rindex("]")+1]
        matches = json.loads(text)
    except Exception as e:
        log(f"[NEWS ARB] Scoring failed: {e}", Fore.YELLOW)
        return 0

    if not matches:
        log("[NEWS ARB] No breaking news matches this cycle.", Fore.WHITE)
        return 0

    trades_placed = 0
    equity_now = get_equity(client) or state.get("equity_sod", 1000)
    stats = get_portfolio_stats(client)
    available = max(0, equity_now - stats["deployed"] - MIN_FREE_BALANCE)

    for match in matches:
        if match.get("confidence", 0) < 0.92:
            log(f"[NEWS ARB] Low confidence ({match.get('confidence',0):.2f}) — skipping: {match.get('headline','')[:60]}", Fore.YELLOW)
            continue

        mid = match.get("market_id", "")
        # Find the full market by partial ID match
        market = next((m for m in markets if m["id"].startswith(mid) or mid in m["id"]), None)
        if not market:
            continue

        # Verify keyword overlap: headline must share key words with market question
        headline_words = set(match.get("headline", "").lower().split())
        question_words = set(market["question"].lower().split())
        # Remove common stop words
        stops = {"the","a","an","is","are","was","will","to","of","in","on","at","by","for","and","or","not","be","with","as","it"}
        h_words = headline_words - stops
        q_words = question_words - stops
        overlap = h_words & q_words
        if len(overlap) < 2:
            log(f"[NEWS ARB] REJECTED — insufficient keyword overlap ({overlap}) between headline and market", Fore.YELLOW)
            continue

        action = match.get("action", "")
        if action not in ("BUY_YES", "BUY_NO"):
            continue

        # Check edge is real — price shouldn't already reflect the news
        yes_price = market["yes_price"]
        no_price  = market["no_price"]
        if action == "BUY_YES" and yes_price > 0.88:
            continue  # already priced in
        if action == "BUY_NO" and no_price > 0.82:
            continue  # already priced in

        # Size up for news arb — higher confidence = larger trade
        base_size = SIZE_MIN[mode] * NEWS_ARB_SIZE_MULT
        size = min(base_size, available, MAX_PER_MARKET_USDC)
        if size < SIZE_MIN[mode]:
            log(f"[NEWS ARB] Insufficient balance for news arb trade.", Fore.YELLOW)
            break

        log(f"[NEWS ARB] 🚨 BREAKING: {match['headline'][:80]}", Fore.MAGENTA)
        log(f"[NEWS ARB] {action} ${size:.0f} | {market['question'][:60]}", Fore.MAGENTA)
        log(f"[NEWS ARB] Reasoning: {match['reasoning'][:100]}", Fore.MAGENTA)

        result = place_trade(client, market, action, size)
        if result:
            trades_placed += 1
            available -= size

    return trades_placed


# ── Main Cycle ────────────────────────────────────────────────────────────────

def run_cycle(client, state):
    _read_intel_files()  # refresh soul/lessons cache once per cycle
    _load_market_cache()  # load market score cache from disk
    # ── Self-learning: record resolved trades, reflect if enough data ────
    record_resolved_trades()
    reflect_and_improve()
    # ── Market guardrails (runs every cycle, fast — price check only) ──────────
    try:
        import importlib.util as _mg_ilu
        _mg_spec = _mg_ilu.spec_from_file_location(
            "market_guardrails",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "market_guardrails.py")
        )
        if _mg_spec:
            _mg_mod = _mg_ilu.module_from_spec(_mg_spec)
            _mg_spec.loader.exec_module(_mg_mod)
            _mg_mod.check_hungary_guardrail()
    except Exception as _mg_e:
        log(f"[GUARDRAIL] check error: {_mg_e}", Fore.YELLOW)
    log("─" * 60)
    log(f"Starting scan cycle — {datetime.now().strftime('%H:%M:%S')}", Fore.CYAN)

    # ── 1. Manage existing positions (sell signals + thesis checks) ────────────
    manage_positions(client)
    # DISABLED: check_thesis_invalidation — repeatedly selling live sports bets (Clippers, Duke, Spain)
    # The thesis checker cannot distinguish 'game in progress at 85c' from 'thesis failed'
    # Re-enable only after adding a reliable way to detect sports vs prediction markets
    # check_thesis_invalidation(client)

    # ── 2. Get current equity and update control plane ─────────────────────────
    equity_now = get_equity(client)
    if equity_now is None:
        log("Could not determine equity — skipping cycle", Fore.YELLOW)
        return state

    state, allow_new_trades = update_control_plane(state, equity_now)
    mode = state["mode"]
    log(f"Mode: {mode} | Equity: ${equity_now:.2f}", Fore.CYAN)

    # ── Sell Signal Processor ─────────────────────────────────────────────────
    # Reads /opt/polymarket-agent/sell_signals.json and executes any pending sells.
    # Written by: exec_server("sell_position", token_id=..., shares=...)
    # Cleared after execution. This bypasses the executor script cache entirely.
    _SELL_SIG_FILE = "/opt/polymarket-agent/sell_signals.json"
    if os.path.exists(_SELL_SIG_FILE):
        try:
            with open(_SELL_SIG_FILE) as _sf:
                _signals = json.load(_sf)
            if _signals:
                log(f"  [SELL-SIG] Processing {len(_signals)} sell signal(s)", Fore.YELLOW)
                _completed = []
                for _sig in _signals:
                    _tok  = _sig.get("token_id", "")
                    _shr  = float(_sig.get("shares", 0))
                    _lbl  = _sig.get("label", _tok[:20])
                    if not _tok or _shr <= 0:
                        _completed.append(_sig)
                        continue
                    try:
                        import requests as _rq_ss
                        _mid_r  = _rq_ss.get(f"https://clob.polymarket.com/midpoint?token_id={_tok}", timeout=8)
                        _mid    = float(_mid_r.json().get("mid", 0))
                        _tick_r = _rq_ss.get(f"https://clob.polymarket.com/tick-size?token_id={_tok}", timeout=8)
                        _tick   = float(_tick_r.json().get("minimum_tick_size", 0.01))
                        # Floor to nearest valid tick — use round() not // to avoid float precision issues
                        _n_ticks = int(_mid / _tick)
                        _sp     = round(_n_ticks * _tick, 6)
                        _sp     = max(_tick, _sp)
                        # For near-resolved markets (mid > 0.95), use mid directly
                        if _mid > 0.95: _sp = round((_n_ticks + 1) * _tick, 6)
                        _sp     = min(0.99, max(_tick, _sp))
                        if _mid <= 0.01:
                            log(f"  [SELL-SIG] {_lbl}: mid={_mid:.4f} too low — position worthless, marking done", Fore.YELLOW)
                            _completed.append(_sig)
                            continue
                        log(f"  [SELL-SIG] {_lbl}: {_shr:.0f}sh @ {_sp:.4f} (mid={_mid:.4f})")
                        from py_clob_client_v2.order_builder.constants import SELL as _SELL_CONST
                        from py_clob_client_v2.clob_types import OrderArgs as _OAS, OrderType as _OTS
                        # Try with PartialCreateOrderOptions first, fall back to simple create_order
                        _ord = None
                        try:
                            from py_clob_client_v2.clob_types import PartialCreateOrderOptions as _PCOS
                            _ord = client.create_order(
                                _OAS(token_id=_tok, price=_sp, size=_shr, side=_SELL_CONST),
                                _PCOS(tick_size=_tick, neg_risk=False)
                            )
                        except Exception:
                            # neg_risk param not supported for this market — try without
                            try:
                                from py_clob_client_v2.clob_types import PartialCreateOrderOptions as _PCOS2
                                _ord = client.create_order(
                                    _OAS(token_id=_tok, price=_sp, size=_shr, side=_SELL_CONST),
                                    _PCOS2(tick_size=_tick)
                                )
                            except Exception:
                                # Last resort: plain create_order without options
                                _ord = client.create_order(
                                    _OAS(token_id=_tok, price=_sp, size=_shr, side=_SELL_CONST)
                                )
                        _resp = client.post_order(_ord, _OTS.GTC)
                        if _resp and _resp.get("success"):
                            log(f"  [SELL-SIG] {_lbl}: ✅ SOLD at {_sp:.4f}", Fore.GREEN)
                            _completed.append(_sig)
                        else:
                            log(f"  [SELL-SIG] {_lbl}: ❌ {_resp}", Fore.RED)
                    except Exception as _se:
                        import traceback as _tb
                        log(f"  [SELL-SIG] {_lbl}: error {_se} | {_tb.format_exc()[-200:]}", Fore.RED)
                # Remove completed signals
                remaining = [s for s in _signals if s not in _completed]
                if remaining:
                    with open(_SELL_SIG_FILE, "w") as _sf:
                        json.dump(remaining, _sf)
                else:
                    os.remove(_SELL_SIG_FILE)
                log(f"  [SELL-SIG] Done: {len(_completed)} sold, {len(remaining)} pending")
        except Exception as _sse:
            log(f"  [SELL-SIG] Error reading signals: {_sse}", Fore.RED)
    # ── End Sell Signal Processor ─────────────────────────────────────────────

    # Re-approve allowances if flagged by daily reset
    if state.get("last_approval_date") != date.today().isoformat():
        state = ensure_allowances(state)
        save_state(state)

    # ── 3. Portfolio stats ─────────────────────────────────────────────────────
    stats = get_portfolio_stats(client)
    log(f"Portfolio: {stats['open_orders']} open orders, ${stats['deployed']:.2f} deployed")

    # ── 4. Skip new trades if paused or daily stop hit ─────────────────────────
    if not allow_new_trades or mode == "PAUSED":
        log(f"No new trades this cycle (mode={mode})", Fore.YELLOW)
        if mode == "PAUSED":
            peak   = state.get("equity_peak_eod", equity_now)
            dd_pct = (peak - equity_now) / peak * 100 if peak > 0 else 0
            tg(f"⛔ <b>HARD PAUSE</b> — drawdown {dd_pct:.1f}%\n💰 Equity: ${equity_now:,.2f} | Peak: ${peak:,.2f}")
        save_state(state)
        return state

    # ── 5. Scan markets ────────────────────────────────────────────────────────
    log("Scanning markets...", Fore.CYAN)
    all_markets = scan_markets()
    log(f"Found {len(all_markets)} markets. Scoring top {TOP_MARKETS_TO_SCORE}...")

    # ── 6. Score (pass mode to restrict in Recovery) ──────────────────────────
    # Pre-fetch RSS headlines once and cache on score_market for multi-source scoring
    try:
        score_market._rss_cache = fetch_news_headlines()
        log(f"[MULTI-SRC] RSS cache: {len(score_market._rss_cache)} headlines", Fore.CYAN)
    except Exception:
        score_market._rss_cache = []
    scored = score_batch(all_markets[:TOP_MARKETS_TO_SCORE], mode=mode)
    _save_market_cache()  # persist updated scores to disk

    # Build token lookup for stale order resubmission
    markets_by_token = {}
    for m in scored:
        if m.get("yes_token_id"):
            markets_by_token[m["yes_token_id"]] = m
        if m.get("no_token_id"):
            markets_by_token[m["no_token_id"]] = m

    # ── 7. Cancel stale orders ────────────────────────────────────────────────
    freed = cancel_and_resubmit_stale_orders(client, markets_by_token)
    stats["deployed"] = max(0, stats["deployed"] - freed)
    stats = get_portfolio_stats(client)
    log(f"After cleanup: {stats['open_orders']} open orders, ${stats['deployed']:.2f} deployed")

    # ── 8. Order gate: check mode limits ──────────────────────────────────────
    max_orders = MAX_ORDERS[mode]
    # Use fixed dollar budget when equity < $5k (pct-based gives trivially small numbers)
    if equity_now < 5000:
        open_budget = MAX_ORDERS[mode] * SIZE_MAX[mode]  # e.g. 8 * $150 = $1200 max deployed
    else:
        open_budget = RISK_OPEN_PCT[mode] * equity_now

    if stats["open_orders"] >= max_orders:
        log(f"Max open orders for {mode} mode ({max_orders}) reached.", Fore.YELLOW)
        save_state(state)
        return state

    if stats["deployed"] >= open_budget:
        log(f"Max open risk for {mode} mode (${open_budget:.2f}) reached.", Fore.YELLOW)
        save_state(state)
        return state

    # ── 9. Place trades ────────────────────────────────────────────────────────
    actionable = [m for m in scored if m.get("action") in ("BUY_YES", "BUY_NO")]

    if not actionable:
        log("No actionable opportunities this cycle.", Fore.WHITE)
        save_state(state)
        return state

    # Build map of current open exposure keyed by lowercase title
    # Also track which SIDE (YES/NO) we hold to prevent buying the opposite
    existing_exposure = {}  # lowercase title -> current_value USD
    existing_side     = {}  # lowercase title -> "YES" or "NO"
    try:
        pos_resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": FUNDER, "sizeThreshold": "0.01"},
            timeout=10
        )
        if pos_resp.status_code == 200:
            for p in pos_resp.json():
                cur_value = float(p.get("currentValue", 0))
                title = p.get("title", "").strip().lower()
                asset = p.get("asset", "")  # token ID
                outcome = p.get("outcome", "")  # "Yes" or "No"
                if title and cur_value > 0:
                    existing_exposure[title] = existing_exposure.get(title, 0) + cur_value
                    existing_side[title] = outcome  # track which side we hold
                if asset and cur_value > 0:
                    existing_exposure[asset] = cur_value
    except Exception:
        pass

    # Check available balance (use USDC cash only for new trade sizing)
    # equity_now includes position values but we can only spend USDC cash
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams as _BAP3, AssetType as _AT3
        _usdc_info = client.get_balance_allowance(
            params=_BAP3(asset_type=_AT3.COLLATERAL, signature_type=2))
        _usdc_cash = float(_usdc_info.get("balance", 0)) / 1e6
    except Exception:
        _usdc_cash = equity_now  # fallback
    available = max(0, _usdc_cash - stats["deployed"] - MIN_FREE_BALANCE)
    if available < SIZE_MIN[mode]:
        log(f"Insufficient free balance (${available:.2f} USDC cash) — skipping new trades.", Fore.YELLOW)
        save_state(state)
        return state

    log(f"Found {len(actionable)} opportunities [{mode} mode] | Free: ${available:.2f}:", Fore.GREEN)

    # ── PRE-TRADE LIVE NEWS GATE ──────────────────────────────────────────────
    # Before placing ANY trade this cycle, fetch the last 15 minutes of news
    # on topics that overlap with our pending trades. If a breaking event is
    # detected that directly contradicts the trade thesis, PAUSE that trade
    # (not the whole bot) and re-score it next cycle with fresh data.
    #
    # Trigger events: ceasefire signed/rejected, invasion, regime fall,
    # deal signed/collapsed, election result called.
    # Each trade in actionable gets a _news_vetoed flag if it's invalidated.
    #
    # This prevents the Man City / WTI / ceasefire-NO recurrence:
    # the bot scored on stale data, then placed on live markets that had moved.
    def _live_news_veto(trade_list):
        import re as _re
        _BREAKING_PATTERNS = [
            # Ceasefire / peace
            (r"ceasefire (signed|reached|agreed|announced|in effect|deal)",
             ["ceasefire", "cease", "iran", "israel", "hormuz", "military op"]),
            (r"(deal|agreement|accord) (signed|reached|announced)",
             ["iran", "ceasefire", "cease", "nuclear", "hormuz"]),
            # Escalation
            (r"(invasion|invad|troops enter|forces enter|bombs|strikes begin|war declared)",
             ["iran", "taiwan", "china", "israel", "ukraine", "russia"]),
            # Political resolution
            (r"(election (result|won|called)|prime minister (named|elected|appointed))",
             ["hungary", "orbán", "magyar", "viktor"]),
            # Regime change
            (r"(regime (fall|collapse|toppled)|government (fell|collapsed|overthrown))",
             ["iran", "regime", "tehran"]),
        ]

        try:
            import requests as _rq
            # Build query from the markets in our trade list
            q_texts = " ".join(m.get("question","") for m in trade_list).lower()

            # Which RSS feeds to check
            queries = []
            if any(x in q_texts for x in ["iran","ceasefire","cease","hormuz","military"]):
                queries.append("iran ceasefire deal april 2026")
            if any(x in q_texts for x in ["hungary","orbán","magyar"]):
                queries.append("hungary election april 2026")
            if any(x in q_texts for x in ["taiwan","china"]):
                queries.append("china taiwan invasion 2026")

            if not queries:
                return {}  # no relevant markets, skip check

            breaking_headlines = []
            cutoff_ts = __import__("time").time() - 900  # last 15 min

            for query in queries[:2]:  # max 2 queries to stay fast
                try:
                    feed = _rq.get(
                        f"https://news.google.com/rss/search?q={query.replace(' ','+')}"
                        f"&hl=en-US&gl=US&ceid=US:en",
                        timeout=6
                    )
                    if not feed.ok:
                        continue
                    items = _re.findall(r"<item>(.*?)</item>", feed.text, _re.DOTALL)
                    for item in items[:8]:
                        title_m = _re.search(r"<title>(.*?)</title>", item)
                        date_m  = _re.search(r"<pubDate>(.*?)</pubDate>", item)
                        if not title_m:
                            continue
                        headline = title_m.group(1).lower()
                        # Only headlines from last 15 min get auto-veto;
                        # last 2h headlines get a soft warning (logged, not vetoed)
                        pub_str = date_m.group(1) if date_m else ""
                        try:
                            import email.utils as _eu
                            pub_ts = _eu.parsedate_to_datetime(pub_str).timestamp()
                        except:
                            pub_ts = 0
                        age_min = (__import__("time").time() - pub_ts) / 60 if pub_ts else 999

                        for pattern, keywords in _BREAKING_PATTERNS:
                            if _re.search(pattern, headline):
                                if any(kw in headline for kw in keywords):
                                    breaking_headlines.append({
                                        "headline": title_m.group(1)[:120],
                                        "age_min":  round(age_min, 1),
                                        "pattern":  pattern,
                                        "fresh":    age_min < 15,
                                    })
                                    break
                except Exception:
                    continue

            if not breaking_headlines:
                return {}

            # Sort: freshest first
            breaking_headlines.sort(key=lambda x: x["age_min"])

            # Build veto map: for each pending trade, check if a headline
            # directly contradicts its direction
            vetoes = {}
            for h in breaking_headlines:
                hl = h["headline"].lower()
                age = h["age_min"]
                fresh = h["fresh"]  # < 15 min

                for m in trade_list:
                    q = m.get("question","").lower()
                    action = m.get("action","")

                    # Ceasefire signed → veto BUY_NO on ceasefire markets
                    if _re.search(r"ceasefire (signed|reached|agreed|in effect)", hl):
                        if "ceasefire" in q and action == "BUY_NO":
                            vetoes[id(m)] = (h, "ceasefire confirmed — NO thesis invalidated")
                    # Ceasefire rejected/collapsed → veto BUY_YES on ceasefire
                    if _re.search(r"ceasefire (rejected|collapsed|failed|no deal)", hl):
                        if "ceasefire" in q and action == "BUY_YES":
                            if fresh:
                                vetoes[id(m)] = (h, "ceasefire rejected — YES thesis invalidated")
                    # Deal signed → veto NO on related market
                    if _re.search(r"(deal|agreement) (signed|reached)", hl):
                        if any(x in q for x in ["military op","hormuz","nuclear","iran"]) and action == "BUY_NO":
                            vetoes[id(m)] = (h, "deal reached — NO thesis invalidated")
                    # Invasion/escalation → veto NO on conflict-escalation markets
                    if _re.search(r"(invasion|invad|bombs|strikes begin)", hl):
                        if any(x in q for x in ["invade","invasion","forces enter"]) and action == "BUY_NO":
                            vetoes[id(m)] = (h, "invasion confirmed — NO thesis invalidated")
                    # Ceasefire / deal → veto oil HIGH targets (Hormuz reopens = supply up = price drops)
                    if _re.search(r"ceasefire (signed|reached|agreed|in effect)|strait.*open|hormuz.*open", hl):
                        if any(x in q for x in ["crude oil","wti","brent","oil price","$120","$110","$100","$90","hit (high)"]):
                            if action == "BUY_YES":
                                vetoes[id(m)] = (h, "ceasefire/Hormuz reopening → oil price spike thesis invalidated")

                    # Escalation → veto oil LOW targets (war premium = price spikes, not falls)
                    if _re.search(r"(strikes begin|invasion|bombs|hormuz closed|blockade)", hl):
                        if any(x in q for x in ["crude oil","wti","brent","oil price","hit (low)"]):
                            if action == "BUY_YES":
                                vetoes[id(m)] = (h, "escalation confirmed → oil low target invalidated")

                    # Election called → veto opposite direction
                    if _re.search(r"(prime minister (named|elected)|election (won|called))", hl):
                        if "hungary" in q or "prime minister" in q:
                            # Veto whichever candidate lost
                            if "orbán" in hl or "orban" in hl:
                                if "magyar" in q and action == "BUY_YES":
                                    vetoes[id(m)] = (h, "Orbán confirmed PM — Magyar YES invalidated")
                            elif "magyar" in hl:
                                if "orbán" in q and action == "BUY_YES":
                                    vetoes[id(m)] = (h, "Magyar confirmed PM — Orbán YES invalidated")

            return vetoes, breaking_headlines

        except Exception as _nge:
            log(f"  [NEWS GATE] check failed (non-fatal): {_nge}")
            return {}, []

    # Run the gate
    _veto_result = _live_news_veto(actionable)
    _vetoes, _breaking = (_veto_result if isinstance(_veto_result, tuple) else ({}, []))

    if _breaking:
        log(f"  [NEWS GATE] {len(_breaking)} breaking headline(s) detected:", Fore.YELLOW)
        for _h in _breaking[:3]:
            log(f"    [{_h['age_min']:.0f}m ago] {_h['headline'][:100]}", Fore.YELLOW)
        if _vetoes:
            log(f"  [NEWS GATE] {len(_vetoes)} trade(s) VETOED — re-scores next cycle", Fore.RED)
    else:
        log("  [NEWS GATE] clean — no breaking events in last 15 min", Fore.GREEN)

    trades_placed = 0

    for m in actionable:
        # ── News gate veto check ───────────────────────────────────────────────
        if id(m) in _vetoes:
            _vh, _vreason = _vetoes[id(m)]
            log(f"  [NEWS GATE VETO] {m.get('action')} '{m.get('question','')[:50]}': {_vreason}", Fore.RED)
            log(f"    Headline ({_vh['age_min']:.0f}m ago): {_vh['headline'][:90]}", Fore.RED)
            continue
        # ─────────────────────────────────────────────────────────────────────
        action = m["action"]
        edge   = m.get("edge", 0)
        market_id = m.get("id", "")

        # ── Side-conflict check: never buy opposite side of existing position ──────
        q_lower = m.get("question", "").strip().lower()
        yes_tok = m.get("yes_token_id", "")
        no_tok  = m.get("no_token_id", "")
        held_side = existing_side.get(q_lower, "")
        want_side = "Yes" if action == "BUY_YES" else "No"
        if held_side and held_side != want_side:
            log(f"  SKIP (holding {held_side}, agent wants {want_side} — conflict): {m['question'][:50]}", Fore.YELLOW)
            continue

        # ── Correlation guard: don't hold opposing bets on the SAME underlying event ──
        # Example: "US forces enter Iran by April 30 YES" vs "US forces enter Iran by Dec 31 NO"
        # These are correlated — if forces enter Iran, both positions lose/win together.
        # Detect via shared key phrases in the question.
        _corr_conflict = False
        _q_words = set(q_lower.split())
        for _held_q, _held_side in existing_side.items():
            if _held_q == q_lower:
                continue  # same question, already handled above
            _held_words = set(_held_q.split())
            # Overlap: questions sharing 4+ meaningful words are likely correlated
            _shared = _q_words & _held_words - {"will","the","by","to","a","an","in",
                                                "of","or","and","is","be","on","for",
                                                "at","it","as","from","with","that",
                                                "are","this","2026","2025","march",
                                                "april","june","december","31","30"}
            if len(_shared) >= 4 and _held_side != want_side:
                log(f"  SKIP (correlated conflict: '{_held_q[:40]}' holds {_held_side}, "
                    f"new wants {want_side}): {q_lower[:40]}", Fore.YELLOW)
                _corr_conflict = True
                break
        if _corr_conflict:
            continue

        # ── Sports policy gate ──────────────────────────────────────────────────
        if is_sports_market(m.get("question", "") + " " + m.get("title", "")):
            allowed, reason = check_sports_eligibility(m, state, equity_now)
            if not allowed:
                log(f"  SKIP [SPORTS POLICY] {reason}: {m['question'][:45]}", Fore.YELLOW)
                save_state(state)  # persist circuit-breaker state updates
                continue
            log(f"  [SPORTS] Passed eligibility gate: {m['question'][:45]}", Fore.MAGENTA)

        # ── UW signal edge boost / veto ────────────────────────────────────────
        uw_sig  = m.get("_uw_yes_sig") if action == "BUY_YES" else m.get("_uw_no_sig")
        opp_sig = m.get("_uw_no_sig")  if action == "BUY_YES" else m.get("_uw_yes_sig")
        if uw_sig:
            sm     = uw_sig.get("smart_money", 0)
            ins    = uw_sig.get("insider_trades", 0)
            gap    = uw_sig.get("smart_gap", 0)
            n_ins  = uw_sig.get("insider_count", 0)
            uscore = uw_sig.get("unusual_score", 0)
            if gap >= 5 or sm >= 5 or ins >= 5 or n_ins >= 2:
                boost = min(0.06, gap * 0.004 + (sm + ins) * 0.002 + n_ins * 0.005)
                edge  = round(edge + boost, 4)
                m["edge"] = edge
                log(f"  [UW] ✓ Boost +{boost:.3f} | gap={gap:.1f} sm={sm:.0f} ins={ins:.0f} wallets={n_ins:.0f}", Fore.MAGENTA)
            if opp_sig:
                opp_gap = opp_sig.get("smart_gap", 0)
                opp_sm  = opp_sig.get("smart_money", 0)
                if opp_gap > gap + 4 or opp_sm > sm + 4:
                    log(f"  [UW] ✗ VETO — smart money opposes (gap={opp_gap:.1f} sm={opp_sm:.0f}): {m['question'][:45]}", Fore.RED)
                    continue
            if uscore > 0 or gap > 0:
                log(f"  [UW] score={uscore:.1f} gap={gap:.1f} sm={sm:.0f} wallets={n_ins:.0f}", Fore.MAGENTA)

        # ── Short-duration YES guardrail ────────────────────────────────────────
        # Lesson from losses: near-term BUY_YES on conflict/event markets almost always
        # lose money. The status quo ("nothing happens by deadline") wins ~75% of the time.
        # Only allow BUY_YES if: duration > 30 days OR it's not a conflict/event market.
        _end_date = m.get("endDate", "") or ""
        _days_left = 999
        if _end_date:
            try:
                import datetime as _dt_mod
                _end_dt = _dt_mod.datetime.fromisoformat(_end_date.replace("Z",""))
                _days_left = (_end_dt - _dt_mod.datetime.utcnow()).days
            except: pass
        _q_lower_risk = m.get("question","").lower()
        _is_conflict_event = any(x in _q_lower_risk for x in [
            "ceasefire", "forces enter", "regime fall", "conflict ends",
            "military operations", "invasion", "invade", "war ends", "peace deal",
            "strikes end", "kharg", "hormuz", "nuclear deal", "bomb iran",
            "attack iran", "strike iran", "occupy", "capture tehran"
        ])
        if action == "BUY_YES" and _is_conflict_event and _days_left < 30:
            log(f"  SKIP [YES guardrail] Short-duration conflict YES ({_days_left}d): {m['question'][:45]}", Fore.YELLOW)
            continue

        # ── Per-market cap check ────────────────────────────────────────────────
        already_in = (
            existing_exposure.get(q_lower, 0) or
            existing_exposure.get(yes_tok, 0) or
            existing_exposure.get(no_tok, 0) or
            existing_exposure.get(market_id, 0)
        )
        if already_in >= MAX_PER_MARKET_USDC:
            log(f"  SKIP (already ${already_in:.0f} in this market, cap=${MAX_PER_MARKET_USDC}): {m['question'][:50]}", Fore.YELLOW)
            continue

        log(f"  {action} | edge={edge:+.3f} | src={m.get('_source_count',1)} | {m['question'][:50]}", Fore.GREEN)
        log(f"    Reasoning: {m.get('reasoning','')[:100]}")

        # Annotate market dict with mode + UW boost for trade log
        m["_mode"]      = mode
        m["_uw_boosted"] = bool(m.get("_uw_yes_sig") or m.get("_uw_no_sig"))

        src_cnt = m.get("_source_count", 1)
        size = calculate_size(edge, mode, equity_now, stats["deployed"], market_price=m.get("price", 0.5), source_count=src_cnt)
        size = min(size, available)  # Never exceed available free balance
        size = min(size, MAX_PER_MARKET_USDC - already_in)  # Per-market cap

        # ── Three-Bucket Sizing Caps ─────────────────────────────────────────
        _mkt_bucket = classify_bucket(m.get('question', '') + ' ' + m.get('title', ''))
        if _mkt_bucket == 'CALENDAR':
            # Calendar spreads: dangerous timing edge — hard cap $100
            size = min(size, 100)
            log(f"  [BUCKET:CALENDAR] Size capped at $100 (timing risk)", Fore.YELLOW)
        elif _mkt_bucket == 'GEO':
            # Geo/politics: asymmetric edge — keep small to avoid fat-tail overfit
            size = min(size, BUCKET_GEO_MAX_USDC)
            log(f"  [BUCKET:GEO] Size capped at ${BUCKET_GEO_MAX_USDC} (asymmetric edge)", Fore.CYAN)
        # SPORTS: no extra cap — grind edge, scale with mode/Kelly naturally
        if size < SIZE_MIN[mode]:
            log(f"  Skipping — size ${size:.2f} below mode minimum ${SIZE_MIN[mode]}", Fore.YELLOW)
            continue

        receipt = place_trade(client, m, action, size)
        if receipt:
            stats["deployed"] += size
            trades_placed += 1
            # Track sports daily spend [S18]
            if is_sports_market(m.get("question", "") + " " + m.get("title", "")):
                _ss = get_sports_state(state)
                _ss["daily_spend"] = _ss.get("daily_spend", 0.0) + size
                log(f"  [SPORTS] Daily spend: ${_ss['daily_spend']:.0f} / ${SPORTS_DAILY_SPEND_HARD_CAP:.0f}", Fore.MAGENTA)

        if stats["deployed"] >= open_budget or stats["open_orders"] + trades_placed >= max_orders:
            log(f"Mode budget reached during cycle.", Fore.YELLOW)
            break

    log(f"Cycle complete. {trades_placed} new trades placed. Mode: {mode}", Fore.CYAN)
    if _TOK["calls"] > 0:
        log(_tok_summary(), Fore.CYAN)
    _reset_tok()
    _API_CALL_COUNT = 0

    # ── 10. Weather scout (separate from LLM scoring — pure forecast arb) ────────
    if mode != "PAUSED" and allow_new_trades:
        try:
            from weather_scout import run_weather_scout
            weather_placed = run_weather_scout(client, state, equity_now)
            if weather_placed:
                log(f"[WEATHER] {len(weather_placed)} weather trade(s) placed", Fore.CYAN)
        except Exception as we:
            log(f"[WEATHER] Scout error (non-fatal): {we}", Fore.YELLOW)

    save_state(state)
    return state


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("  POLYMARKET AUTO-TRADER v2 — Control Plane Active", Fore.CYAN)
    log(f"  Scan interval: {SCAN_INTERVAL_SECONDS//60} minutes")
    log(f"  NORMAL: ${SIZE_MIN['NORMAL']}–${SIZE_MAX['NORMAL']} | RECOVERY: ${SIZE_MIN['RECOVERY']}–${SIZE_MAX['RECOVERY']} | EXPANSION: ${SIZE_MIN['EXPANSION']}–${SIZE_MAX['EXPANSION']}")
    log(f"  DD triggers: Recovery={DD_RECOVERY:.0%}, Hard Pause={DD_HARD_PAUSE:.0%}")
    log("=" * 60)

    if not PRIVATE_KEY:
        log("ERROR: POLYMARKET_PRIVATE_KEY not set in .env", Fore.RED)
        return

    # ── Pre-flight checks ────────────────────────────────────────────────────
    # Run safety checks before trading. Blocks on critical failures.
    # Skip T4/T5 (BTC momentum-specific) and T6 (weather scout re-checked inline)
    try:
        _pf_dir = os.path.dirname(os.path.abspath(__file__))
        import importlib.util as _ilu
        _pf_spec = _ilu.spec_from_file_location("preflight", os.path.join(_pf_dir, "preflight.py"))
        _pf_mod  = _ilu.module_from_spec(_pf_spec)
        _pf_spec.loader.exec_module(_pf_mod)
        _ok, _results = _pf_mod.run_preflight(
            bot_name="Autotrader",
            send_telegram=True,
            skip_tests={"T4", "T5"}  # BTC price feed / 5-min market — not needed here
        )
        if not _ok:
            critical_fails = [r for r in _results if not r["passed"] and r["critical"]]
            log(f"Pre-flight FAILED: {len(critical_fails)} critical check(s). Trading blocked.", Fore.RED)
            for f in critical_fails:
                log(f"  FAIL: {f['name']}: {f['message']}", Fore.RED)
            # Don't exit — some failures are auto-fixable (e.g. CLOB creds re-derived in T2)
            # Re-run after a brief wait to give T2 a chance to save fresh creds
            import time as _t; _t.sleep(5)
    except Exception as _pfe:
        log(f"Pre-flight check error (non-fatal): {_pfe}", Fore.YELLOW)
    # ──────────────────────────────────────────────────────────────

    # ── Executor health check — restart if hung ──────────────────────────────
    # The executor service can hang if a long-running script blocks its HTTP handler.
    # TCP accepts connections but never sends a response. We detect this and kill
    # the process to let systemd restart it cleanly.
    try:
        import requests as _req_ex, subprocess as _sp, time as _t_ex
        _ex_resp = _req_ex.get("http://127.0.0.1:8888/health", timeout=5)
        log("Executor health OK", Fore.WHITE)
    except Exception:
        # Executor is not responding — try to restart it
        try:
            log("Executor not responding — restarting service", Fore.YELLOW)
            # Kill any process holding port 8888
            _sp.run(["fuser", "-k", "8888/tcp"], capture_output=True, timeout=5)
            import time as _t_ex; _t_ex.sleep(2)
            _sp.run(["systemctl", "restart", "executor"], capture_output=True, timeout=10)
            _t_ex.sleep(3)
            log("Executor restart attempted", Fore.WHITE)
        except Exception as _exe:
            log(f"Executor restart failed: {_exe}", Fore.YELLOW)
    # ────────────────────────────────────────────────────────────

    # Load persisted state (survives reboots)
    state = load_state()
    log(f"Loaded state: mode={state['mode']}, peak=${state.get('equity_peak_eod') or '?'}", Fore.CYAN)

    # Auto-approve USDC allowances at startup
    state = ensure_allowances(state)
    save_state(state)

    cycle = 0
    markets_cache = []       # Shared market list between full scans and news arb
    last_full_scan    = 0       # Timestamp of last 15-min full scan
    last_news_scan    = 0       # Timestamp of last 5-min news arb scan
    last_review       = 0       # Timestamp of last weekly pattern review
    last_urgency_scan = 0       # Timestamp of last 30-min urgency rescore
    last_oracle_check = 0       # Timestamp of last oracle check (final-24h positions)

    # ── Crash-loop circuit breaker ────────────────────────────────────────────
    # Tracks rapid consecutive errors. If the bot crashes 5 times in <10 min,
    # it backs off exponentially (up to 30 min sleep) and alerts Telegram.
    # This prevents runaway API calls ($500 bill) in crash-loops.
    _err_times = []          # timestamps of recent errors
    _CIRCUIT_WINDOW  = 600   # 10-minute window
    _CIRCUIT_MAX     = 5     # errors within window = circuit opens
    _backoff_sleep   = 60    # current sleep after error (doubles each breach, max 1800)
    # ──────────────────────────────────────────────────────────────

    while True:
        now = time.time()
        try:
            client = get_client()

            # ── Full scan every 15 minutes ────────────────────────────────────
            if now - last_full_scan >= SCAN_INTERVAL_SECONDS:
                cycle += 1
                log(f"\nCycle #{cycle}", Fore.CYAN)
                # Refresh market cache during full scan
                fresh_markets = scan_markets()
                if fresh_markets:
                    markets_cache = fresh_markets
                state = run_cycle(client, state)
                last_full_scan = time.time()

            # ── News arb every 5 minutes ──────────────────────────────────────
            elif now - last_news_scan >= NEWS_SCAN_INTERVAL:
                log(f"\n[NEWS ARB] Scanning headlines...", Fore.MAGENTA)
                try:
                    n = news_arb_scan(client, state, markets_cache)
                    if n > 0:
                        log(f"[NEWS ARB] Placed {n} news-driven trade(s).", Fore.MAGENTA)
                except Exception as e:
                    log(f"[NEWS ARB] Error: {e}", Fore.YELLOW)
                last_news_scan = time.time()

            # ── Oracle check every 30 min (final-24h positions) ──────────────
            elif now - last_oracle_check >= 1800:
                try:
                    run_oracle_checks(client)
                except Exception as oe:
                    log(f"[ORACLE] Check error (non-fatal): {oe}", Fore.YELLOW)
                last_oracle_check = time.time()

            # ── Urgency rescore every 30 min (markets expiring <72h) ─────────
            elif now - last_urgency_scan >= 1800:
                log("[URGENCY] Scanning for near-expiry opportunities...", Fore.MAGENTA)
                try:
                    from datetime import datetime as _udt, timezone as _utz
                    _now_u = _udt.now(_utz.utc)
                    _urgent = [m for m in markets_cache if m.get("end_date") and
                               0 < (_udt.fromisoformat(m["end_date"].replace("Z","+00:00")) - _now_u
                                    ).total_seconds() / 3600 < 72]
                    if _urgent:
                        log(f"[URGENCY] {len(_urgent)} markets expiring <72h — force-rescoring", Fore.MAGENTA)
                        # Bust cache for these markets so they get a fresh LLM score
                        for _um in _urgent:
                            _key = _um.get("condition_id") or _um.get("question","")[:80]
                            if _key in _market_cache:
                                del _market_cache[_key]
                        equity_u = get_equity(client)
                        if equity_u:
                            mode_u = state.get("mode", "NORMAL")
                            _uscored = score_batch(_urgent, mode=mode_u)
                            _uact = [m for m in _uscored if m.get("action") in ("BUY_YES","BUY_NO")]
                            if _uact:
                                log(f"[URGENCY] {len(_uact)} actionable near-expiry signal(s)", Fore.GREEN)
                                for _um2 in _uact[:3]:  # cap at 3 urgency trades
                                    _ua = _um2["action"]
                                    _ue = _um2.get("edge", 0)
                                    _usize = min(50.0, max(10.0, equity_u * 0.01))  # 1% equity, cap $50
                                    log(f"  [URGENCY] {_ua} edge={_ue:+.3f}: {_um2.get('question','')[:50]}", Fore.GREEN)
                                    place_trade(client, _um2, _ua, _usize)
                except Exception as ue:
                    log(f"[URGENCY] Rescore error (non-fatal): {ue}", Fore.YELLOW)
                last_urgency_scan = time.time()

            # ── Weekly intelligence review (every 7 days) ─────────────────
            elif time.time() - last_review >= 7 * 24 * 3600:
                log("[INTELLIGENCE] Running weekly pattern review...", Fore.MAGENTA)
                review_patterns()
                last_review = time.time()

            # ── Sleep 60 seconds between checks ──────────────────────────────
            else:
                time.sleep(60)
                continue

        except KeyboardInterrupt:
            log("\nStopped by user. Goodbye.", Fore.WHITE)
            break
        except Exception as e:
            tb = traceback.format_exc()
            log(f"Cycle error: {e}", Fore.RED)
            log(tb, Fore.RED)
            short = tb.strip().splitlines()
            # Send last 15 lines of traceback to Telegram so we can diagnose
            tb_snippet = "\n".join(short[-15:])
            tg(f"\U0001f534 <b>Cycle error</b>\n<code>{str(e)[:200]}</code>\n\n<code>{tb_snippet[:800]}</code>")
            log_mistake("Cycle error", "Unhandled exception", str(e)[:150], "Add specific handling for this error type")

            # ── Circuit breaker: exponential backoff on rapid crash-loops ──
            now_err = time.time()
            _err_times.append(now_err)
            # Evict errors older than window
            _err_times[:] = [t for t in _err_times if now_err - t < _CIRCUIT_WINDOW]
            if len(_err_times) >= _CIRCUIT_MAX:
                _backoff_sleep = min(_backoff_sleep * 2, 1800)  # double up to 30 min
                tg(
                    f"\u26a0\ufe0f <b>CIRCUIT BREAKER</b> \u2014 {len(_err_times)} errors in "
                    f"{_CIRCUIT_WINDOW//60}min\n"
                    f"Backing off {_backoff_sleep//60:.0f}min to protect API budget.\n"
                    f"Last error: <code>{str(e)[:200]}</code>"
                )
                log(f"[CIRCUIT BREAKER] {len(_err_times)} errors in {_CIRCUIT_WINDOW//60}min — "
                    f"sleeping {_backoff_sleep//60:.0f}min", Fore.RED)
                time.sleep(_backoff_sleep)
            else:
                _backoff_sleep = 60  # reset backoff on isolated error
                time.sleep(60)
            # ── End circuit breaker ────────────────────────────────────────





if __name__ == "__main__":
    main()
