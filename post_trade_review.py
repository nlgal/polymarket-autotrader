#!/usr/bin/env python3
"""
post_trade_review.py — Post-trade review logging, failure mode detection,
                        and weekly summary.
===========================================================================
Implements meta-rule #9:
  "If post-trade review shows repeated losses from the same failure mode,
   create a new guardrail instead of rationalizing the loss."

Storage: JSONL at /opt/polymarket-agent/intelligence/post_trade_reviews.jsonl
  - One JSON object per line (grep-able, appendable, survives partial writes)
  - Never rewritten in full — append only (except compaction runs)
  - Each record is self-contained (no foreign keys to fragile runtime state)

Failure mode detection:
  - Runs after each new record is written
  - Checks last 10 and last 30 records for repeated modes
  - Threshold: 3+ occurrences of same failure mode in last 10 → alert + suggest guardrail

Weekly summary:
  - Called by reset_state.py / strategy_optimizer.py every Monday
  - Top failure modes, capital lost by mode, module attribution

Usage:
  from post_trade_review import write_review, run_failure_detection, weekly_summary
  or:
  python post_trade_review.py --summary
  python post_trade_review.py --detect
  python post_trade_review.py --backfill
"""

import os, sys, json, time, datetime, uuid, requests

sys.path.insert(0, "/opt/polymarket-agent")
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_DIR   = "/opt/polymarket-agent"
REVIEW_FILE = os.path.join(AGENT_DIR, "intelligence", "post_trade_reviews.jsonl")
LOG_FILE    = os.path.join(AGENT_DIR, "guardrails.log")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
FUNDER   = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()

# ── Failure mode taxonomy ─────────────────────────────────────────────────────
# Every primary_failure_mode value must be from this set.

FAILURE_MODES = {
    "stale_signal_chase":           "Entered based on information already reflected in price",
    "late_whale_copy":              "Copied whale position after price had already moved",
    "fee_negative_edge":            "Edge after fees was zero or negative",
    "slippage_trap":                "Slippage consumed the expected edge",
    "adverse_selection_lp":         "LP fills came from informed flow; positions moved against us immediately",
    "overconcentration":            "Single market >30% of portfolio at cost",
    "capital_starvation":           "No free cash to act on good opportunities",
    "expiry_compression_misread":   "Held too long; time decay destroyed edge",
    "resolution_rule_ambiguity":    "Market resolution was unclear; outcome was not what thesis predicted",
    "correlated_source_overconfidence": "Multiple sources counted as independent but were correlated",
    "bad_execution_good_thesis":    "Thesis was correct but entry/exit price destroyed PnL",
    "good_execution_bad_thesis":    "Execution was clean but fundamental thesis was wrong",
    "bad_exit_discipline":          "Held losing position past thesis invalidation",
    "contradiction_hedge_confusion":"LP or autotrader built opposing side inadvertently",
    "ops_failure":                  "Process restart, state corruption, or infrastructure issue",
    "missing_cash_reserve":         "Traded when cash buffer was below $100 floor",
    "model_overconfidence":         "LLM probability estimate was overconfident relative to market",
    "false_momentum_read":          "Entered on perceived momentum that reversed immediately",
    "none":                         "No failure — process was correct",
}

# Detection thresholds
DETECT_WINDOW_SHORT   = 10   # last N records
DETECT_WINDOW_LONG    = 30   # last M records
ALERT_THRESHOLD_SHORT = 3    # same failure mode >= 3 times in last 10 → alert
ALERT_THRESHOLD_LONG  = 5    # same failure mode >= 5 times in last 30 → alert

# ── I/O helpers ───────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] [REVIEW] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception:
            pass

def _ensure_dir():
    os.makedirs(os.path.dirname(REVIEW_FILE), exist_ok=True)

def load_reviews(limit=None):
    """Load all review records. Returns list of dicts, oldest first."""
    _ensure_dir()
    records = []
    if not os.path.exists(REVIEW_FILE):
        return records
    try:
        with open(REVIEW_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip corrupt lines silently
    except Exception as e:
        log(f"load_reviews error: {e}")
    if limit:
        return records[-limit:]
    return records

def _append_review(record):
    """Append a single review record as one JSON line. Atomic-safe."""
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with open(REVIEW_FILE, "a") as f:
            f.write(line)
        return True
    except Exception as e:
        log(f"_append_review error: {e}")
        return False

# ── Record schema + write ─────────────────────────────────────────────────────

def write_review(
    market_title,
    strategy_type,          # "directional" | "lp" | "hybrid"
    category,               # "geopolitical" | "sports" | "politics" | "finance" | "crypto"
    side,                   # "YES" | "NO"
    entry_price,
    exit_price,
    size,                   # shares
    realized_pnl,
    trigger_source,         # "stop_loss" | "profit_target" | "guardrail_yes_price" |
                            # "expiry_compression" | "contradiction_fix" | "manual" |
                            # "lp_fill" | "resolution"
    trade_type,             # "automated" | "discretionary" | "defensive"
    # optional enrichment
    market_slug=None,
    condition_id=None,
    token_id=None,
    entry_ts=None,          # ISO string
    exit_ts=None,           # ISO string — defaults to now
    fees=0.0,
    slippage_estimate=0.0,
    thesis_summary="",
    market_state_entry="",  # free text: state classifier output at entry
    market_state_exit="",   # free text: state classifier output at exit
    whale_signal=False,
    discord_signal=False,
    news_catalyst=False,
    guardrail_fired=False,
    primary_failure_mode="none",  # must be a key in FAILURE_MODES
    secondary_factors=None,       # list of failure mode keys
    process_quality="",           # "good" | "acceptable" | "poor"
    outcome_quality="",           # "won" | "lost" | "partial" | "neutral"
    recommended_guardrail="",
    notes="",
    source_module="",             # "autotrader" | "position_monitor" | "lp_quoter" |
                                  # "market_guardrails" | "manual"
):
    """
    Write a post-trade review record.
    Called after any meaningful trade event: resolution, forced exit,
    guardrail fire, LP episode, or partial exit.
    """
    now_iso  = datetime.datetime.utcnow().isoformat()
    review_id = f"ptr_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"

    # Validate failure mode
    if primary_failure_mode not in FAILURE_MODES:
        log(f"Unknown failure mode '{primary_failure_mode}' — defaulting to 'none'")
        primary_failure_mode = "none"

    pnl_pct = round(realized_pnl / (entry_price * size) * 100, 1) \
              if entry_price > 0 and size > 0 else 0.0

    record = {
        # ── Identity ──────────────────────────────────────────────────────────
        "review_id":              review_id,
        "ts":                     now_iso,
        "market_title":           market_title[:120],
        "market_slug":            market_slug or "",
        "condition_id":           condition_id or "",
        "token_id":               token_id or "",
        # ── Strategy ──────────────────────────────────────────────────────────
        "strategy_type":          strategy_type,
        "category":               category,
        "side":                   side.upper(),
        "trade_type":             trade_type,
        "source_module":          source_module,
        # ── Timing ────────────────────────────────────────────────────────────
        "entry_ts":               entry_ts or "",
        "exit_ts":                exit_ts or now_iso,
        # ── Pricing ───────────────────────────────────────────────────────────
        "entry_price":            round(float(entry_price), 4),
        "exit_price":             round(float(exit_price), 4),
        "size":                   round(float(size), 2),
        "fees":                   round(float(fees), 4),
        "slippage_estimate":      round(float(slippage_estimate), 4),
        # ── PnL ───────────────────────────────────────────────────────────────
        "realized_pnl":           round(float(realized_pnl), 2),
        "realized_pnl_pct":       pnl_pct,
        # ── Context ───────────────────────────────────────────────────────────
        "trigger_source":         trigger_source,
        "thesis_summary":         thesis_summary[:300],
        "market_state_entry":     market_state_entry[:100],
        "market_state_exit":      market_state_exit[:100],
        # ── Signals ───────────────────────────────────────────────────────────
        "whale_signal":           bool(whale_signal),
        "discord_signal":         bool(discord_signal),
        "news_catalyst":          bool(news_catalyst),
        "guardrail_fired":        bool(guardrail_fired),
        # ── Quality assessment ────────────────────────────────────────────────
        "process_quality":        process_quality,
        "outcome_quality":        outcome_quality or ("won" if realized_pnl > 0 else
                                                       "lost" if realized_pnl < 0 else "neutral"),
        # ── Failure analysis ──────────────────────────────────────────────────
        "primary_failure_mode":   primary_failure_mode,
        "secondary_factors":      secondary_factors or [],
        "recommended_guardrail":  recommended_guardrail,
        "notes":                  notes[:500],
    }

    ok = _append_review(record)
    if ok:
        log(f"Review written: {review_id} | {market_title[:40]} | PnL ${realized_pnl:+.2f} "
            f"| mode={primary_failure_mode}")
        # Run detection after every write
        run_failure_detection(silent=True)
    return review_id

# ── Failure mode detection ────────────────────────────────────────────────────

def run_failure_detection(silent=False):
    """
    Check for repeated failure modes over last 10 and last 30 records.
    If a mode breaches threshold: emit Telegram alert + suggested guardrail.
    Returns list of detected issues.
    """
    all_records = load_reviews()
    issues      = []

    windows = [
        (DETECT_WINDOW_SHORT, ALERT_THRESHOLD_SHORT, "last_10"),
        (DETECT_WINDOW_LONG,  ALERT_THRESHOLD_LONG,  "last_30"),
    ]

    for window_size, threshold, label in windows:
        recent = all_records[-window_size:]
        # Filter to actual losses with a failure mode
        loss_records = [
            r for r in recent
            if r.get("primary_failure_mode", "none") != "none"
            and r.get("realized_pnl", 0) < 0
        ]

        # Count by failure mode
        counts = {}
        for r in loss_records:
            mode = r.get("primary_failure_mode", "none")
            counts[mode] = counts.get(mode, 0) + 1

        for mode, count in counts.items():
            if count >= threshold:
                capital_lost = sum(
                    abs(r["realized_pnl"])
                    for r in loss_records
                    if r.get("primary_failure_mode") == mode
                )
                issue = {
                    "window":        label,
                    "failure_mode":  mode,
                    "occurrences":   count,
                    "capital_lost":  round(capital_lost, 2),
                    "description":   FAILURE_MODES.get(mode, ""),
                    "suggested_guardrail": _suggest_guardrail(mode),
                }
                issues.append(issue)

                if not silent:
                    _alert_repeated_failure(issue)
                else:
                    # Only alert if this is a new detection (not re-alerting known issues)
                    _alert_if_new(issue)

    return issues

def _alert_if_new(issue):
    """Alert only if this is the first time we're seeing this failure mode at this count."""
    # Simple heuristic: alert only when count is exactly at threshold (first breach)
    if issue["occurrences"] == (ALERT_THRESHOLD_SHORT
                                if issue["window"] == "last_10"
                                else ALERT_THRESHOLD_LONG):
        _alert_repeated_failure(issue)

def _alert_repeated_failure(issue):
    mode = issue["failure_mode"]
    tg(
        f"🔁 <b>Repeated Failure Mode Detected</b>\n\n"
        f"<b>Mode:</b> <code>{mode}</code>\n"
        f"<b>Window:</b> {issue['window']}\n"
        f"<b>Occurrences:</b> {issue['occurrences']}\n"
        f"<b>Capital lost:</b> ${issue['capital_lost']:.2f}\n"
        f"<b>Description:</b> {issue['description']}\n\n"
        f"<b>Suggested guardrail:</b>\n{issue['suggested_guardrail']}\n\n"
        f"<i>Per meta-rule #9: add a new guardrail instead of rationalizing.</i>"
    )
    log(f"REPEATED FAILURE ALERT: {mode} x{issue['occurrences']} in {issue['window']} "
        f"— capital lost ${issue['capital_lost']:.2f}")

def _suggest_guardrail(mode):
    """Return a concrete suggested guardrail for each failure mode."""
    suggestions = {
        "stale_signal_chase": (
            "Add freshness check: if market mid hasn't moved >2% in last 2h "
            "despite the claimed catalyst, block entry."
        ),
        "late_whale_copy": (
            "Add whale signal lag check: if whale trade is >4h old AND price "
            "already moved >5% toward the whale's direction, classify as 'late' "
            "and skip or halve size."
        ),
        "fee_negative_edge": (
            "Add pre-trade fee gate: compute edge after fee. "
            "If fee_adjusted_edge < 0.03, skip. "
            "Currently 0% on geopolitical, 0.75-1.8% on others."
        ),
        "slippage_trap": (
            "Add spread check: if CLOB spread > 0.05 (5 ticks), "
            "use passive limit only. Reject marketable orders when spread > 0.08."
        ),
        "adverse_selection_lp": (
            "Reduce target_shares on markets with rapid fill rate. "
            "If fill rate > 50% of quotes in a cycle, raise pullback from 1.5 to 3 ticks."
        ),
        "overconcentration": (
            "Hard cap: any single market > 30% of portfolio cost basis → "
            "block further LP accumulation AND new directional entries. "
            "Currently LP max_inventory helps, but needs portfolio-level check."
        ),
        "capital_starvation": (
            "Require $150 free cash before any new entry. "
            "LP max_inventory should already limit fill-driven depletion. "
            "Add cash check at top of autotrader main loop."
        ),
        "expiry_compression_misread": (
            "Add time-decay scaling: for markets < 7 days to expiry, "
            "require mid to be >= 0.90 for YES or <= 0.10 for NO before holding. "
            "Alert if a position is within 5 days and still near 50%."
        ),
        "resolution_rule_ambiguity": (
            "Before entry: check gamma API market description for ambiguous "
            "resolution language. If description contains 'may', 'at discretion', "
            "or 'subject to review' → size down 50% or skip."
        ),
        "correlated_source_overconfidence": (
            "Require sources to be from different outlets/wire services. "
            "Reuters + AP = 2 independent. Reuters + NYT citing Reuters = 1. "
            "Do not scale size beyond 1x until sources are genuinely independent."
        ),
        "bad_execution_good_thesis": (
            "Force passive limit orders on illiquid markets (spread > 0.04). "
            "Log all instances where fill price differed from mid by > 0.03."
        ),
        "good_execution_bad_thesis": (
            "Add mandatory thesis review at 24h mark. "
            "If mid moved > 10% against position without news catalyst, "
            "require Haiku re-assessment before holding further."
        ),
        "bad_exit_discipline": (
            "Implement explicit guardrail triggers for each held position. "
            "Market-specific YES/NO price thresholds that auto-reduce."
        ),
        "contradiction_hedge_confusion": (
            "The existing LP conflict check (CONFLICT_THRESHOLD=100, max_inventory) "
            "should prevent this. If it fires again: tighten CONFLICT_THRESHOLD to 50."
        ),
        "ops_failure": (
            "Add startup state validation to autotrader: "
            "if peak_equity > 2x current equity, reset to current before trading. "
            "(This already exists — check if it ran.)"
        ),
        "missing_cash_reserve": (
            "Add hard pre-trade cash check: "
            "if (total_equity - positions_total) < 100, skip all new entries."
        ),
        "model_overconfidence": (
            "Add calibration flag: if true_prob > 0.80 AND edge > 0.15, "
            "apply 50% size haircut — extreme estimates are usually wrong."
        ),
        "false_momentum_read": (
            "Require evidence of fundamental driver, not just price movement. "
            "Momentum alone (no news, no whale) → classify as 'no_trade'."
        ),
    }
    return suggestions.get(mode, "Review this failure mode manually and add a specific guardrail.")

# ── Weekly summary ────────────────────────────────────────────────────────────

def weekly_summary(send_telegram=True):
    """
    Generate weekly post-trade review summary.
    Called by strategy_optimizer.py / reset_state.py on Mondays.
    """
    all_records = load_reviews()
    if not all_records:
        log("No review records found for weekly summary.")
        return {}

    # Last 7 days
    cutoff = time.time() - 7 * 86400
    recent = [
        r for r in all_records
        if datetime.datetime.fromisoformat(
            r["ts"].replace("Z", "")
        ).timestamp() > cutoff
    ]

    if not recent:
        log("No records in last 7 days for weekly summary.")
        return {}

    # Aggregate by failure mode
    mode_stats = {}
    for r in recent:
        mode   = r.get("primary_failure_mode", "none")
        pnl    = float(r.get("realized_pnl", 0))
        module = r.get("source_module", "unknown")
        if mode not in mode_stats:
            mode_stats[mode] = {"count": 0, "capital_lost": 0.0, "modules": set()}
        mode_stats[mode]["count"]        += 1
        mode_stats[mode]["capital_lost"] += min(pnl, 0)  # only count losses
        mode_stats[mode]["modules"].add(module)

    # Total stats
    total_pnl      = sum(r.get("realized_pnl", 0) for r in recent)
    total_trades   = len(recent)
    wins           = [r for r in recent if r.get("realized_pnl", 0) > 0]
    losses         = [r for r in recent if r.get("realized_pnl", 0) < 0]
    win_rate       = len(wins) / total_trades * 100 if total_trades > 0 else 0

    # Module attribution (which module generated the most losses)
    module_pnl = {}
    for r in recent:
        m   = r.get("source_module", "unknown")
        pnl = float(r.get("realized_pnl", 0))
        module_pnl[m] = module_pnl.get(m, 0.0) + pnl

    # Top failure modes (by capital lost)
    sorted_modes = sorted(
        [(m, s) for m, s in mode_stats.items() if m != "none"],
        key=lambda x: x[1]["capital_lost"]
    )

    # "Do nothing" counterfactual: what if we had held everything to expiry?
    # Not computable from sells alone — note it as a caveat
    do_nothing_note = "Cannot compute without resolution data — see results.json for comparison."

    summary = {
        "period":         "last_7_days",
        "total_trades":   total_trades,
        "total_pnl":      round(total_pnl, 2),
        "win_rate_pct":   round(win_rate, 1),
        "n_wins":         len(wins),
        "n_losses":       len(losses),
        "top_failure_modes": [
            {
                "mode": m,
                "count": s["count"],
                "capital_lost": round(s["capital_lost"], 2),
                "modules": list(s["modules"]),
            }
            for m, s in sorted_modes[:5]
        ],
        "module_pnl":     {k: round(v, 2) for k, v in module_pnl.items()},
        "do_nothing_note": do_nothing_note,
        "ts_generated":   datetime.datetime.utcnow().isoformat(),
    }

    if send_telegram:
        lines_modes = "\n".join(
            f"  • <code>{item['mode']}</code>: "
            f"{item['count']}x, -${abs(item['capital_lost']):.0f}"
            for item in summary["top_failure_modes"][:5]
        ) or "  (none)"

        lines_modules = "\n".join(
            f"  • {mod}: ${pnl:+.0f}"
            for mod, pnl in sorted(module_pnl.items(), key=lambda x: x[1])
        ) or "  (none)"

        tg(
            f"📊 <b>Weekly Post-Trade Review</b>\n\n"
            f"<b>Period:</b> Last 7 days ({total_trades} trades)\n"
            f"<b>Total PnL:</b> ${total_pnl:+.2f} | Win rate: {win_rate:.0f}%\n\n"
            f"<b>Top Failure Modes:</b>\n{lines_modes}\n\n"
            f"<b>Module Attribution (PnL):</b>\n{lines_modules}\n\n"
            f"<i>Full log: intelligence/post_trade_reviews.jsonl</i>"
        )

    log(f"Weekly summary: {total_trades} trades, ${total_pnl:+.2f} PnL, "
        f"{len(sorted_modes)} failure modes")
    return summary

# ── Backfill from existing results.json ──────────────────────────────────────

def backfill_from_results():
    """
    One-time import of resolved trades from intelligence/results.json into JSONL.
    Marks all as source_module="autotrader" with failure_mode inference.
    Safe to call multiple times — skips already-imported records.
    """
    results_path = os.path.join(AGENT_DIR, "intelligence", "results.json")
    if not os.path.exists(results_path):
        log("No results.json found — skipping backfill")
        return 0

    existing_reviews = load_reviews()
    existing_ids = {
        r.get("notes", "")
        for r in existing_reviews
        if r.get("notes", "").startswith("backfill:")
    }

    try:
        with open(results_path) as f:
            results = json.load(f)
    except Exception as e:
        log(f"backfill_from_results load error: {e}")
        return 0

    count = 0
    for trade in results:
        status = trade.get("status", "")
        if status not in ("resolved", "closed"):
            continue

        q = trade.get("question", "")
        backfill_key = f"backfill:{q[:60]}"
        if backfill_key in existing_ids:
            continue  # already imported

        pnl        = float(trade.get("pnl", 0) or 0)
        cost       = float(trade.get("size_usdc", 0) or 0)
        action     = trade.get("action", "")
        yes_price  = float(trade.get("yes_price", 0.5) or 0.5)
        no_price   = float(trade.get("no_price", 0.5) or 0.5)
        side       = "YES" if "YES" in action else "NO"
        entry_price = yes_price if side == "YES" else no_price
        mtype       = trade.get("market_type", "unknown")

        # Infer rough failure mode from outcome and context
        failure_mode = "none"
        if pnl < -30:
            if "ceasefire" in q.lower() or "forces" in q.lower():
                failure_mode = "good_execution_bad_thesis"
            elif "crude" in q.lower() or "btc" in q.lower():
                failure_mode = "stale_signal_chase"
        elif pnl < -10:
            failure_mode = "good_execution_bad_thesis"

        write_review(
            market_title=q,
            strategy_type="directional",
            category=mtype,
            side=side,
            entry_price=entry_price,
            exit_price=0,         # unknown for historical records
            size=cost / entry_price if entry_price > 0 else 0,
            realized_pnl=pnl,
            trigger_source="resolution",
            trade_type="automated",
            entry_ts=trade.get("ts_placed", ""),
            exit_ts=trade.get("resolved_at", ""),
            thesis_summary=trade.get("reasoning", "")[:200],
            primary_failure_mode=failure_mode,
            process_quality="unknown",
            source_module="autotrader",
            notes=backfill_key,
        )
        count += 1

    log(f"Backfill complete: {count} records imported from results.json")
    return count

# ── Autotrader hook: wrap existing log_trade_outcome ─────────────────────────

def log_guardrail_event(
    market_title, side, size, entry_price, exit_price,
    realized_pnl, trigger_source, notes="", source_module="market_guardrails"
):
    """Convenience wrapper called from market_guardrails.py."""
    return write_review(
        market_title=market_title,
        strategy_type="directional",
        category="geopolitical",
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        size=size,
        realized_pnl=realized_pnl,
        trigger_source=trigger_source,
        trade_type="defensive",
        guardrail_fired=True,
        primary_failure_mode="bad_exit_discipline" if realized_pnl < 0 else "none",
        process_quality="good",
        source_module=source_module,
        notes=notes,
    )

# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Post-trade review manager")
    parser.add_argument("--summary",  action="store_true", help="Print weekly summary")
    parser.add_argument("--detect",   action="store_true", help="Run failure detection")
    parser.add_argument("--backfill", action="store_true", help="Import from results.json")
    parser.add_argument("--list",     type=int, default=0, help="List last N reviews")
    parser.add_argument("--no-tg",    action="store_true", help="Suppress Telegram")
    args = parser.parse_args()

    if args.backfill:
        n = backfill_from_results()
        print(f"Backfilled {n} records.")

    if args.detect:
        issues = run_failure_detection(silent=args.no_tg)
        print(f"Detected {len(issues)} issue(s):")
        for issue in issues:
            print(f"  [{issue['window']}] {issue['failure_mode']} x{issue['occurrences']} "
                  f"— ${issue['capital_lost']:.2f} lost")
            print(f"    Suggested: {issue['suggested_guardrail'][:80]}")

    if args.summary:
        summary = weekly_summary(send_telegram=not args.no_tg)
        print(json.dumps(summary, indent=2))

    if args.list:
        records = load_reviews(limit=args.list)
        for r in records:
            pnl = r.get("realized_pnl", 0)
            print(f"  {r['ts'][:16]} | {r['market_title'][:45]:45s} | "
                  f"{r['side']:3s} | ${pnl:+7.2f} | {r['primary_failure_mode']}")
