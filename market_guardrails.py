#!/usr/bin/env python3
"""
market_guardrails.py — Protective risk-off rules for specific open positions.
===========================================================================
Currently implements:
  - Hungary PM Magyar NO guardrail: if YES mid >= 0.72 for 2 consecutive
    checks, enter risk-off mode (reduce or exit NO position).

Architectural role:
  - Called by position_monitor.py every 4h AND by autotrader.py each cycle.
  - All state written to market_guardrails_state.json (restart-safe).
  - Never requires manual intervention for standard exits.
  - Execution uses the same sell path as autotrader.py.

State file: /opt/polymarket-agent/market_guardrails_state.json
Schema: see GUARDRAIL_STATE_DEFAULT below.
"""

import os, sys, json, time, math, datetime, requests

sys.path.insert(0, "/opt/polymarket-agent")
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_DIR   = "/opt/polymarket-agent"
STATE_FILE  = os.path.join(AGENT_DIR, "market_guardrails_state.json")
LOG_FILE    = os.path.join(AGENT_DIR, "guardrails.log")

FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY",
              os.environ.get("PRIVATE_KEY", "")).strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Hungary PM Magyar — market identifiers
# NO token id: from position data
HUNGARY_CONDITION_ID = ""  # filled at runtime from positions API
HUNGARY_TITLE        = "Will the next Prime Minister of Hungary be Péter Magyar?"

# Guardrail trigger parameters
HUNGARY_YES_TRIGGER  = 0.72   # YES price at or above this → risk-off consideration
HUNGARY_CONSEC_REQ   = 2      # consecutive checks at or above trigger before acting
HUNGARY_DRY_RUN      = False  # set True to log/alert without placing real orders

GUARDRAIL_STATE_DEFAULT = {
    # Hungary guardrail
    "hungary": {
        "trigger_count":      0,       # consecutive checks above threshold
        "last_check_ts":      0,       # unix timestamp of last price check
        "last_yes_price":     0.0,     # last observed YES price
        "triggered":          False,   # True once 2-consecutive threshold met
        "triggered_at":       None,    # ISO timestamp of first trigger
        "action_taken":       None,    # "reduced" / "exited" / "blocked" / None
        "action_ts":          None,    # ISO timestamp of action
        "shares_before":      0.0,     # shares held when action was taken
        "shares_sold":        0.0,     # shares sold in guardrail action
        "exit_price":         0.0,     # realized exit price
        "realized_pnl":       0.0,     # PnL from guardrail exit
        "avg_entry":          0.0,     # our avg entry price (for PnL calc)
        "dry_run":            False,   # whether this was a dry-run
    }
}

# ── I/O helpers ───────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] [GUARDRAIL] {msg}"
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

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            state = {}
            for k, default_v in GUARDRAIL_STATE_DEFAULT.items():
                state[k] = {**default_v, **(saved.get(k, {}))}
            return state
    except Exception as e:
        log(f"State load error: {e} — using defaults")
    return {k: dict(v) for k, v in GUARDRAIL_STATE_DEFAULT.items()}

def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)  # atomic write — safe on Linux
    except Exception as e:
        log(f"State save error: {e}")

# ── Market data helpers ───────────────────────────────────────────────────────

def get_yes_mid(condition_id, yes_token_id=None):
    """
    Return the current YES midpoint for a market.
    Tries midpoint API first, falls back to positions API.
    Returns None on failure.
    """
    # Method 1: CLOB midpoint by token_id
    if yes_token_id:
        try:
            r = requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={yes_token_id}",
                timeout=8
            )
            if r.status_code == 200:
                mid = float(r.json().get("mid", 0))
                if 0 < mid < 1:
                    return mid
        except Exception:
            pass

    # Method 2: infer from NO position mid (YES = 1 - NO_mid)
    # We'll look up the position data and get the token from there
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
            timeout=10
        )
        if r.status_code != 200:
            return None
        for p in r.json():
            if p.get("conditionId", "") == condition_id:
                tok = p.get("asset", p.get("tokenId", ""))
                outcome = p.get("outcome", "").upper()
                if tok:
                    mid_r = requests.get(
                        f"https://clob.polymarket.com/midpoint?token_id={tok}",
                        timeout=8
                    )
                    if mid_r.status_code == 200:
                        raw_mid = float(mid_r.json().get("mid", 0))
                        if outcome == "NO":
                            return round(1.0 - raw_mid, 4)
                        elif outcome == "YES":
                            return round(raw_mid, 4)
    except Exception:
        pass

    return None

def get_hungary_position():
    """
    Returns (size, avg_price, token_id, condition_id) for our Hungary NO position.
    Returns (0, 0, None, None) if not held.
    """
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
            timeout=10
        )
        if r.status_code != 200:
            return 0, 0, None, None
        for p in r.json():
            title = p.get("title", "")
            outcome = p.get("outcome", "").upper()
            if ("magyar" in title.lower() or "hungary" in title.lower()) and outcome == "NO":
                size    = float(p.get("size", 0) or 0)
                avg     = float(p.get("avgPrice", p.get("averagePrice", 0)) or 0)
                tok     = p.get("asset", p.get("tokenId", ""))
                cid     = p.get("conditionId", p.get("market", ""))
                return size, avg, tok, cid
    except Exception as e:
        log(f"get_hungary_position error: {e}")
    return 0, 0, None, None

def get_best_bid(token_id):
    """Return best bid price for a token. Returns None if illiquid (< 0.05)."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8
        )
        if r.status_code == 200:
            bids = r.json().get("bids", [])
            if bids:
                best = float(bids[0]["price"])
                return best if best >= 0.05 else None
    except Exception:
        pass
    return None

# ── Execution helper ──────────────────────────────────────────────────────────

def place_sell_order(token_id, size, price, dry_run=False):
    """
    Place a GTC limit sell order via py_clob_client.
    Returns (success: bool, fill_price: float, error: str).
    In dry_run mode, logs but doesn't send order.
    """
    if dry_run:
        log(f"[DRY RUN] Would sell {size:.1f}sh @ {price:.3f} token={token_id[:16]}...")
        return True, price, None

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import LimitOrderArgs, OrderType
        HOST  = "https://clob.polymarket.com"
        client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=137)
        client.set_api_creds(client.create_or_derive_api_creds())

        # Floor size to 2 decimal places (never round up — avoids balance errors)
        safe_size = math.floor(size * 100) / 100.0
        if safe_size < 1.0:
            return False, 0, f"size {safe_size} too small after floor"

        # Tick-align price
        tick = float(client.get_tick_size(token_id))
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        aligned_price = round(round(price / tick) * tick, tick_dec)
        aligned_price = max(0.01, min(0.99, aligned_price))

        from py_clob_client.order_builder.constants import SELL
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        args   = OrderArgs(token_id=token_id, price=aligned_price, size=safe_size, side=SELL)
        opts   = PartialCreateOrderOptions(
            tick_size=tick, neg_risk=client.get_neg_risk(token_id)
        )
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success") or receipt.get("orderID"):
            return True, aligned_price, None
        else:
            err = receipt.get("errorMsg", str(receipt))
            return False, 0, err

    except Exception as e:
        return False, 0, str(e)

# ── Hungary guardrail core logic ──────────────────────────────────────────────

def check_hungary_guardrail(dry_run=HUNGARY_DRY_RUN):
    """
    Main entry point. Call this every monitor cycle.

    Logic:
      1. Load state.
      2. Fetch current YES price for Hungary market.
      3. If YES >= HUNGARY_YES_TRIGGER: increment consecutive counter.
         Else: reset counter to 0.
      4. If counter >= HUNGARY_CONSEC_REQ AND action not already taken: trigger.
      5. On trigger: evaluate execution mode, place sell, send Telegram alert.
      6. Save state.

    Returns: (action_taken: str or None, details: dict)
    """
    state = load_state()
    g     = state["hungary"]

    # ── 1. Get current position ───────────────────────────────────────────────
    size, avg_entry, no_token_id, condition_id = get_hungary_position()

    if size < 10:
        log("Hungary: no meaningful NO position held — skip")
        return None, {"reason": "no_position"}

    # ── 2. Get YES price ──────────────────────────────────────────────────────
    # For a NO position, our token is the NO token. YES mid = 1 - NO mid.
    yes_mid = None
    if no_token_id:
        try:
            no_mid_r = requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={no_token_id}",
                timeout=8
            )
            if no_mid_r.status_code == 200:
                no_mid = float(no_mid_r.json().get("mid", 0))
                if 0 < no_mid < 1:
                    yes_mid = round(1.0 - no_mid, 4)
        except Exception:
            pass

    if yes_mid is None:
        log("Hungary: could not fetch YES price — skip this cycle")
        return None, {"reason": "price_unavailable"}

    now_ts   = int(time.time())
    prev_cnt = g["trigger_count"]

    log(f"Hungary: YES mid={yes_mid:.4f} | trigger_threshold={HUNGARY_YES_TRIGGER} | consecutive={prev_cnt}")

    # ── 3. Update consecutive counter ────────────────────────────────────────
    g["last_check_ts"]    = now_ts
    g["last_yes_price"]   = yes_mid
    g["avg_entry"]        = avg_entry
    g["shares_before"]    = size

    if yes_mid >= HUNGARY_YES_TRIGGER:
        g["trigger_count"] = prev_cnt + 1
        log(f"Hungary: YES >= {HUNGARY_YES_TRIGGER} — consecutive count now {g['trigger_count']}")
    else:
        if prev_cnt > 0:
            log(f"Hungary: YES below threshold — resetting count from {prev_cnt} to 0")
        g["trigger_count"] = 0
        save_state(state)
        return None, {"reason": "below_threshold", "yes_mid": yes_mid}

    # ── 4. Check if already actioned ─────────────────────────────────────────
    if g["action_taken"] in ("exited", "reduced"):
        log(f"Hungary: action already taken ({g['action_taken']}) — skip")
        save_state(state)
        return g["action_taken"], {"reason": "already_actioned"}

    # ── 5. Check if consecutive threshold reached ─────────────────────────────
    if g["trigger_count"] < HUNGARY_CONSEC_REQ:
        log(f"Hungary: {g['trigger_count']}/{HUNGARY_CONSEC_REQ} consecutive — not yet triggered")
        if not g["triggered"]:
            # First trigger: send early warning
            tg(
                f"⚠️ <b>Hungary Guardrail — Early Warning</b>\n\n"
                f"YES mid={yes_mid:.3f} ≥ {HUNGARY_YES_TRIGGER} trigger\n"
                f"Count: {g['trigger_count']}/{HUNGARY_CONSEC_REQ} (need {HUNGARY_CONSEC_REQ} consecutive)\n"
                f"Position: {size:.0f}sh NO @ avg {avg_entry:.3f}\n\n"
                f"No action yet. Will act on next check if YES stays ≥ {HUNGARY_YES_TRIGGER}."
            )
        save_state(state)
        return None, {"reason": "warning_only", "count": g["trigger_count"]}

    # ── 6. TRIGGERED — determine execution mode ───────────────────────────────
    if not g["triggered"]:
        g["triggered"]    = True
        g["triggered_at"] = datetime.datetime.utcnow().isoformat()
        log(f"Hungary guardrail TRIGGERED — YES={yes_mid:.4f} for {g['trigger_count']} consecutive checks")

    # Determine: full exit or partial?
    # Rule: 
    #   - If YES >= 0.80 OR time-to-expiry < 5 days: FULL EXIT (cross spread aggressively)
    #   - If YES 0.72-0.80: reduce by 50% with passive limit at best bid
    days_left = _get_hungary_days_left()
    full_exit = (yes_mid >= 0.80) or (days_left is not None and days_left <= 5)

    if full_exit:
        shares_to_sell = size         # exit all
        exec_mode      = "full_exit"
    else:
        shares_to_sell = math.floor(size * 0.5 * 100) / 100.0  # reduce 50%
        exec_mode      = "partial_50pct"

    # ── 7. Get best bid for execution ─────────────────────────────────────────
    best_bid = get_best_bid(no_token_id)  # We're selling NO shares
    if best_bid is None or best_bid < 0.05:
        # Book is illiquid — log the blocked state, alert, and do NOT force sell
        log(f"Hungary: BLOCKED — best bid illiquid ({best_bid}) — cannot exit safely")
        g["action_taken"] = "blocked_illiquid"
        g["action_ts"]    = datetime.datetime.utcnow().isoformat()
        save_state(state)

        tg(
            f"🚨 <b>Hungary Guardrail TRIGGERED — BLOCKED (illiquid book)</b>\n\n"
            f"YES mid={yes_mid:.3f} ≥ {HUNGARY_YES_TRIGGER}\n"
            f"Position: {size:.0f}sh NO @ avg {avg_entry:.3f}\n"
            f"Best bid for NO: {best_bid} — TOO ILLIQUID to exit safely\n\n"
            f"⚠️ Manual review required. Cannot reduce without destroying exit price.\n"
            f"Est. loss if forced exit at bid: ${ (avg_entry - (best_bid or 0)) * size:.0f}"
        )
        return "blocked_illiquid", {"reason": "illiquid_book", "best_bid": best_bid}

    # ── 8. Place sell order ───────────────────────────────────────────────────
    sell_price = best_bid  # sell at best bid (passive GTC — aggressive = cross spread)
    if full_exit and yes_mid >= 0.85:
        # Genuinely urgent — use best_bid - 1 tick to ensure fill
        sell_price = max(0.01, round(best_bid - 0.01, 2))

    success, filled_price, err = place_sell_order(
        token_id=no_token_id,
        size=shares_to_sell,
        price=sell_price,
        dry_run=dry_run
    )

    # ── 9. Record outcome ─────────────────────────────────────────────────────
    estimated_pnl = round((filled_price - avg_entry) * shares_to_sell, 2) if success else 0

    if success:
        g["action_taken"]  = exec_mode
        g["action_ts"]     = datetime.datetime.utcnow().isoformat()
        g["shares_sold"]   = shares_to_sell
        g["exit_price"]    = filled_price
        g["realized_pnl"]  = estimated_pnl
        g["dry_run"]       = dry_run
        action_str = "FULL EXIT" if full_exit else "PARTIAL (50%)"
        log(f"Hungary guardrail executed: {action_str} | {shares_to_sell:.1f}sh @ {filled_price:.3f} | PnL ${estimated_pnl:+.2f}")
    else:
        g["action_taken"] = "sell_failed"
        g["action_ts"]    = datetime.datetime.utcnow().isoformat()
        log(f"Hungary guardrail SELL FAILED: {err}")

    save_state(state)

    # ── 10. Telegram alert ────────────────────────────────────────────────────
    dry_tag  = " [DRY RUN]" if dry_run else ""
    loss_est = round((avg_entry - sell_price) * shares_to_sell, 2)

    if success:
        tg(
            f"🛡️ <b>Hungary Guardrail Executed{dry_tag}</b>\n\n"
            f"<b>Trigger:</b> YES mid={yes_mid:.3f} ≥ {HUNGARY_YES_TRIGGER} "
            f"for {g['trigger_count']} consecutive checks\n\n"
            f"<b>Position:</b> {size:.0f}sh NO @ avg entry {avg_entry:.3f}\n"
            f"<b>Action:</b> {'Full exit' if full_exit else '50% reduction'} "
            f"({shares_to_sell:.0f}sh)\n"
            f"<b>Execution:</b> GTC limit @ {sell_price:.3f} (best bid)\n"
            f"<b>Est. realized loss:</b> ${loss_est:+.2f}\n"
            f"<b>Days to expiry:</b> {days_left if days_left is not None else 'unknown'}\n\n"
            f"<i>Guardrail defensive action — not a thesis reversal</i>"
        )
    else:
        tg(
            f"🚨 <b>Hungary Guardrail TRIGGERED but SELL FAILED{dry_tag}</b>\n\n"
            f"YES mid={yes_mid:.3f} | Position: {size:.0f}sh NO\n"
            f"Error: {err}\n\n"
            f"Manual intervention may be required."
        )

    # ── 11. Log to post_trade_review if available ─────────────────────────────
    _log_to_review(
        market_title=HUNGARY_TITLE,
        action=exec_mode,
        side="NO",
        size=shares_to_sell,
        entry_price=avg_entry,
        exit_price=filled_price if success else 0,
        realized_pnl=estimated_pnl if success else 0,
        trigger_source="guardrail_yes_price",
        failure_mode="bad_exit_discipline" if not success else None,
        notes=f"Guardrail triggered at YES={yes_mid:.3f}. Days left: {days_left}. "
              f"{'Full exit' if full_exit else '50% reduction'}."
              f"{' SELL FAILED: ' + str(err) if not success else ''}"
    )

    return exec_mode if success else "sell_failed", {
        "yes_mid": yes_mid,
        "shares_sold": shares_to_sell if success else 0,
        "exit_price": filled_price if success else 0,
        "realized_pnl": estimated_pnl if success else 0,
        "exec_mode": exec_mode,
        "days_left": days_left,
        "error": err,
    }

# ── Helper: days to expiry ─────────────────────────────────────────────────────

def _get_hungary_days_left():
    """Fetch Hungary market end date from Gamma API."""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets?"
            "search=Prime+Minister+Hungary+Magyar&limit=5",
            timeout=8
        )
        if r.status_code == 200:
            markets = r.json() if isinstance(r.json(), list) else r.json().get("markets", [])
            for m in markets:
                if "magyar" in m.get("question", "").lower() or \
                   "hungary" in m.get("question", "").lower():
                    end = m.get("endDate") or m.get("end_date_iso")
                    if end:
                        end_dt = datetime.datetime.fromisoformat(
                            end.replace("Z", "+00:00")
                        )
                        delta = (end_dt - datetime.datetime.now(
                            datetime.timezone.utc
                        )).days
                        return delta
    except Exception:
        pass
    return None

# ── Bridge to post_trade_review ───────────────────────────────────────────────

def _log_to_review(market_title, action, side, size, entry_price,
                   exit_price, realized_pnl, trigger_source,
                   failure_mode=None, notes=""):
    """Write a guardrail event to post_trade_review.jsonl if the module exists."""
    try:
        review_path = os.path.join(AGENT_DIR, "post_trade_review.py")
        if not os.path.exists(review_path):
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("post_trade_review", review_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.write_review(
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
            primary_failure_mode=failure_mode,
            notes=notes,
        )
    except Exception as e:
        log(f"_log_to_review error: {e}")

# ── Standalone run ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Log and alert without placing orders")
    parser.add_argument("--reset", action="store_true",
                        help="Reset guardrail state (e.g. after market resolves)")
    args = parser.parse_args()

    if args.reset:
        state = load_state()
        state["hungary"] = dict(GUARDRAIL_STATE_DEFAULT["hungary"])
        save_state(state)
        print("Hungary guardrail state reset.")
        sys.exit(0)

    action, details = check_hungary_guardrail(dry_run=args.dry_run)
    print(f"Action: {action} | Details: {json.dumps(details, indent=2)}")
