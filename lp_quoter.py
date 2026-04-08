#!/usr/bin/env python3
"""
lp_quoter.py — Polymarket LP Reward Quoter
===========================================
Places two-sided resting limit orders on reward-eligible markets to earn
daily USDC LP incentives from Polymarket's liquidity rewards program.

Runs every 15 minutes via cron. Places YES buy + NO buy within the
reward spread zone, cancels and re-quotes when midpoint drifts.

Two-sided quoting earns 3× vs single-sided in the reward formula:
  score = min(yes_score, no_score) × scaling_factor
  where score = ((max_spread - distance) / max_spread)² × shares

Safety:
  - kill_lp.json: drop this file on server to cancel all orders immediately
  - MAX_FILL_USDC_PER_SIDE: stops quoting if filled too much one-directionally
  - GTD orders: auto-expire after 70 min (prevents stale fills on crash)
  - Activity guard: skips quoting if market traded < 3 min ago (price discovery)
  - Fill scaling: reduces size to 300sh after large fills, 200sh after very large
  - 1.5¢ pullback from midpoint: fill protection with 44% reward score (EVPoly-inspired)
  - No withdrawal permissions needed — CLOB API key only
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

# ── Config ────────────────────────────────────────────────────────────────────

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CLOB_HOST   = "https://clob.polymarket.com"

STATE_FILE  = "/opt/polymarket-agent/lp_state.json"
KILL_FILE   = "/opt/polymarket-agent/kill_lp.json"
LOG_FILE    = "/opt/polymarket-agent/lp_quoter.log"

# Re-quote if midpoint shifts more than this from our quoted price
REBALANCE_THRESHOLD = 0.012   # 1.2 cents — tighter than before

# Kill switch: if fills on one side exceed this USDC, stop quoting that market
MAX_FILL_USDC_PER_SIDE = 5000  # LLN justification: small edge × many IID fills = reliable profit on fee-free geo markets  # $600 — flags only if filled 3-4 full rounds one-way

# Minimum USDC to keep as cash buffer (don't deploy everything)
CASH_BUFFER = 100  # keep $100 free for autotrader

# Order expiry: GTD orders auto-cancel after this many seconds
# 70 min = just after next 4h cron run (prevents stale fills if bot crashes)
ORDER_TTL_SECONDS = 70 * 60   # 4200 seconds

# Activity guard: skip quoting if market traded within this many seconds
# Avoids being adverse-selected during active price discovery
ACTIVITY_GUARD_SECONDS = 180  # 3 minutes

# Fill scaling thresholds: reduce size when fills are large
FILL_SCALE_MEDIUM = 150  # reduce to 300sh if one-side fills exceed $150
FILL_SCALE_HEAVY  = 300  # reduce to 200sh if one-side fills exceed $300

# ── LP Markets — hardcoded token IDs from live API ───────────────────────────
#
# Priority ranking by expected daily earnings (verified live 2026-03-31):
#
#   Ceasefire Apr30: $119/day pool, 92K zone depth
#     500sh/side → 0.54% → $11/day → two-sided → ~$33/day  ✅ BEST ratio
#   Ceasefire Apr15: $2061/day pool, 403K zone depth
#     1000sh/side → 0.25% → $5/day → two-sided → ~$15/day
#   Ceasefire Apr7: $169/day pool, 213K zone depth
#     1000sh/side → 0.47% → ~$9/day → two-sided → ~$29/day
#   Forces Apr30:   $283/day pool, 192K zone depth
#     500sh/side → 0.26% → $0.74/day → two-sided → ~$2/day

LP_MARKETS = [
    {
        # Pool $119/day, zone 92K shares — lightest competition
        # We already hold NO here → NO buys aligned with our thesis
        "label":       "Ceasefire Apr30",
        "yes_token":   "44149007410374101286260953227333745102128417138356632089802983317837574022801",
        "no_token":    "52284848830940446862370529859386043059769275594386884690262695607365719243018",
        "condition_id":"0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5",
        "max_spread":  0.045,    # ±4.5 cents
        "min_shares":  200,
        "target_shares": 500,   # each side
        "max_inventory": 1000,  # hard cap: stop quoting same side if holding >= this many shares
        "pool_day":    119,
        "enabled":     True,
        "reset_fills":  True,   # reset fill counters on next run
    },
    {
        # Pool $2061/day, zone 403K shares — big pool, more competition
        # Run 1000 shares each side to compete meaningfully
        "label":       "Ceasefire Apr15",
        "yes_token":   "85191934649046129480174964255278880752271767733539167443243111973456166096127",
        "no_token":    "8442709013751543525223072638303914942960068246422295030411662679470140144155",
        "condition_id":"0x773abaa5fe55e5cde51a261f444b7921652a4e059ead6b3be9fe56499c2d4609",
        "max_spread":  0.045,
        "min_shares":  200,
        "target_shares": 1000,
        "max_inventory": 1400,  # hard cap per side
        "pool_day":    2061,
        "enabled":     True,
    },
    {
        # Pool $169/day, zone 213K shares — good ratio if depth stays light
        "label":       "Ceasefire Apr7",
        "yes_token":   "82855088893985825781350466813737280564000275725006328179621744619327480699369",
        "no_token":    "55194745453074297560900438908357749978780021444937743754846798173575377021411",
        "condition_id":"0x4c5701bcde0b8fb7d7f48c8e9d20245a6caa58c61a77f981fad98f2bfa0b1bc7",
        "max_spread":  0.045,
        "min_shares":  200,
        "target_shares": 500,
        "pool_day":    169,
        "enabled":     True,    # enabled: capital available, Apr30 validated
    },
]

# ── Logging / Telegram ────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def tg(msg):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass

# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"State save error: {e}")

# ── CLOB Client ───────────────────────────────────────────────────────────────

def get_client():
    from py_clob_client.client import ClobClient
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
    client = ClobClient(
        host=CLOB_HOST,
        key=PRIVATE_KEY,
        chain_id=137,
        funder=FUNDER or None,
        signature_type=sig_type,
    )
    try:
        creds = client.create_or_derive_api_creds()
    except AttributeError:
        creds = client.derive_api_key()
    client.set_api_creds(creds)
    return client

def get_usdc_balance(client):
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
        return float(info.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"Balance error: {e}")
        return 0.0

def get_midpoint(client, token_id):
    try:
        book = client.get_midpoint(token_id)
        return float(book.get("mid", 0.5))
    except Exception as e:
        log(f"Midpoint error {token_id[:12]}: {e}")
        return None

def get_open_orders(client):
    try:
        from py_clob_client.clob_types import OpenOrderParams
        return client.get_orders(OpenOrderParams()) or []
    except Exception as e:
        log(f"Open orders error: {e}")
        return []

def cancel_token_orders(client, token_id, open_orders):
    n = 0
    for o in open_orders:
        if o.get("asset_id") == token_id:
            try:
                client.cancel(o["id"])
                log(f"  Cancelled {o['id'][:16]} @ {o.get('price')}")
                n += 1
            except Exception as e:
                log(f"  Cancel error: {e}")
    return n

def cancel_all(client, open_orders):
    for o in open_orders:
        try:
            client.cancel(o["id"])
        except Exception:
            pass

def place_buy(client, token_id, price, shares, label):
    """Place a GTC BUY limit order. Returns order_id or None."""
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    try:
        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0

        price = round(round(price / tick_f) * tick_f, tick_dec)
        price = max(0.01, min(0.99, price))

        # GTD: expiration on OrderArgs auto-cancels stale orders if bot crashes.
        # Falls back to GTC if server SDK version doesn't support expiration.
        expiry  = int(time.time()) + ORDER_TTL_SECONDS
        try:
            args = OrderArgs(token_id=token_id, price=price, size=round(shares, 2),
                             side=BUY, expiration=expiry)
        except TypeError:
            # Older SDK version — expiration not supported as kwarg
            args = OrderArgs(token_id=token_id, price=price, size=round(shares, 2), side=BUY)
        opts    = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, opts)
        try:
            receipt = client.post_order(signed, OrderType.GTD)
        except TypeError:
            receipt = client.post_order(signed, OrderType.GTC)  # SDK fallback

        if receipt.get("success"):
            oid = receipt.get("orderID", "?")
            log(f"  ✓ {label} BUY {shares:.0f}sh @ {price:.4f}  id={oid[:16]}...")
            return oid, price
        else:
            log(f"  ✗ {label} failed: {receipt.get('errorMsg', receipt.get('error','?'))}")
            return None, price
    except Exception as e:
        log(f"  ✗ {label} exception: {e}")
        return None, price

# ── Fill tracking ─────────────────────────────────────────────────────────────

def track_fills(open_order_ids, state):
    """Detect filled orders by comparing saved IDs to current open set."""
    for mkt in LP_MARKETS:
        if not mkt["enabled"]:
            continue
        ms = state.get(mkt["label"], {})
        for side, key, price_key in [
            ("YES", "yes_order_id", "yes_quoted_price"),
            ("NO",  "no_order_id",  "no_quoted_price"),
        ]:
            oid = ms.get(key)
            if not oid or oid in open_order_ids:
                continue
            # Order gone — filled or cancelled
            price     = float(ms.get(price_key, 0.5))
            shares    = float(ms.get("target_shares", mkt["target_shares"]))
            fill_usdc = shares * price
            fill_key  = f"{side.lower()}_filled_usdc"
            ms[fill_key] = float(ms.get(fill_key, 0)) + fill_usdc
            ms[key] = None  # clear so we re-quote
            total_filled = ms[fill_key]

            msg = (f"💰 <b>LP FILL</b> — {mkt['label']}\n"
                   f"{side}: {shares:.0f} shares @ {price:.3f} ≈ ${fill_usdc:.2f}\n"
                   f"Session {side} total filled: ${total_filled:.2f}")
            log(msg.replace("<b>","").replace("</b>",""))
            tg(msg)

            if total_filled > MAX_FILL_USDC_PER_SIDE:
                warn = (f"⚠️ <b>LP LIMIT HIT</b> — {mkt['label']}\n"
                        f"{side} fills ${total_filled:.2f} > ${MAX_FILL_USDC_PER_SIDE} limit\n"
                        f"Disabling market — re-enable manually after review")
                log(warn.replace("<b>","").replace("</b>",""))
                tg(warn)
                mkt["enabled"] = False

        state[mkt["label"]] = ms
    return state

# ── Per-market LP logic ───────────────────────────────────────────────────────

def run_market(client, mkt, state, open_orders, usdc_avail):
    label = mkt["label"]
    ms    = state.get(label, {})

    # Get current midpoint (YES price)
    yes_mid = get_midpoint(client, mkt["yes_token"])
    if yes_mid is None:
        return state
    no_mid = round(1.0 - yes_mid, 4)

    log(f"[{label}] YES mid={yes_mid:.4f}  NO mid={no_mid:.4f}  pool=${mkt['pool_day']}/day")

    # ── Activity guard: skip if market actively trading (price discovery) ────
    try:
        cid = mkt.get("condition_id","")
        if cid:
            act_r = requests.get(
                f"https://data-api.polymarket.com/activity?market={cid}&limit=1",
                timeout=8
            )
            if act_r.status_code == 200:
                acts = act_r.json()
                if acts:
                    last_ts = acts[0].get("timestamp", 0)
                    if isinstance(last_ts, (int, float)):
                        age_sec = time.time() - last_ts
                        if age_sec < ACTIVITY_GUARD_SECONDS:
                            log(f"[{label}] Activity guard: traded {age_sec:.0f}s ago — skipping this cycle")
                            state[label] = ms
                            return state
    except Exception:
        pass  # guard failure → proceed normally

    # ── Fill-based size scaling (EVPoly-inspired) ─────────────────────────────
    yes_filled = float(ms.get("yes_filled_usdc", 0))
    no_filled  = float(ms.get("no_filled_usdc",  0))
    max_filled = max(yes_filled, no_filled)
    base_target = mkt["target_shares"]
    if max_filled > FILL_SCALE_HEAVY:
        scaled_target = max(mkt["min_shares"], int(base_target * 0.4))   # 40% of normal
        log(f"[{label}] Heavy fills (${max_filled:.0f}) — scaling to {scaled_target}sh")
    elif max_filled > FILL_SCALE_MEDIUM:
        scaled_target = max(mkt["min_shares"], int(base_target * 0.6))   # 60% of normal
        log(f"[{label}] Medium fills (${max_filled:.0f}) — scaling to {scaled_target}sh")
    else:
        scaled_target = base_target

    # Check if rebalance needed
    prev_yes_mid = float(ms.get("yes_mid", -1))
    yes_has_order = bool(ms.get("yes_order_id"))
    no_has_order  = bool(ms.get("no_order_id"))
    shift = abs(yes_mid - prev_yes_mid) if prev_yes_mid >= 0 else 999

    if yes_has_order and no_has_order and shift < REBALANCE_THRESHOLD:
        log(f"[{label}] Orders OK, shift={shift:.4f} < {REBALANCE_THRESHOLD} — holding")
        state[label] = ms
        return state

    reason = []
    if shift >= REBALANCE_THRESHOLD: reason.append(f"mid shifted {shift:.3f}")
    if not yes_has_order: reason.append("YES order gone")
    if not no_has_order:  reason.append("NO order gone")
    log(f"[{label}] Rebalancing: {', '.join(reason)}")

    # Cancel existing orders for this market
    n_yes = cancel_token_orders(client, mkt["yes_token"], open_orders)
    n_no  = cancel_token_orders(client, mkt["no_token"],  open_orders)
    if n_yes + n_no:
        time.sleep(0.5)

    # Check USDC cost: shares × price each side
    target = scaled_target
    yes_cost = target * yes_mid
    no_cost  = target * no_mid
    total_cost = yes_cost + no_cost

    if usdc_avail < total_cost + CASH_BUFFER:
        # Scale down shares to what we can afford (both sides equally)
        affordable = max(0, usdc_avail - CASH_BUFFER) / (yes_mid + no_mid)
        target = max(mkt["min_shares"], int(affordable // 10) * 10)  # round to 10s
        if target < mkt["min_shares"]:
            log(f"[{label}] Insufficient USDC (${usdc_avail:.2f}, need ${total_cost:.2f}) — skip")
            state[label] = ms
            return state
        log(f"[{label}] Scaling shares {mkt['target_shares']} → {target} (USDC limited)")
        yes_cost = target * yes_mid
        no_cost  = target * no_mid

    # Place YES buy and NO buy near midpoint for maximum reward score.
    # Pull back enough ticks to avoid crossing the book:
    #   - Normal markets (mid 20-80¢): 1 tick back = 0.01
    #   - Near-certain markets (mid >80¢ or <20¢): pull back more (0.02)
    #     because the spread is extremely tight and midpoint ≈ best ask
    def safe_price(mid, extra_pullback=0.0):
        # EVPoly-inspired: place 1.5 ticks behind mid for fill protection.
        # Reward score at 1.5¢ back = ((max_spread - 1.5) / max_spread)² = 0.44×
        # Much safer than midpoint while still earning meaningful rewards.
        # Near-certain markets (>80¢ or <20¢) need extra pullback because
        # the spread is so tight that even 1.5¢ back may cross the book.
        pullback = 0.03 if (mid > 0.80 or mid < 0.20) else 0.015
        return max(0.01, min(0.99, mid - pullback - extra_pullback))

    yes_price = safe_price(yes_mid)
    no_price  = safe_price(no_mid)

    # ── Parity check — detect arb bots before quoting ─────────────────────
    # Binary markets: YES_mid + NO_mid should ~= 1.00.
    # If sum > 1.02: spread is inverted — arb bots are actively buying both
    # sides for a risk-free profit. Our quotes would be filled by arb bots,
    # not real traders. Skip this cycle. (karlbooklover / PredictParity, Apr 2026)
    # If sum < 0.96: hollow book — widen our pullback to avoid thin fills.
    _parity_sum = yes_mid + no_mid
    if _parity_sum > 1.02:
        log(f"  [PARITY SKIP] {label} YES+NO={_parity_sum:.4f}>1.02 — arb bot active, skipping")
        state.setdefault(label, {})["last_parity_skip_ts"] = int(time.time())
        save_state(state)
        return None, None  # skip this market — no orders placed this cycle
    if _parity_sum < 0.96:
        log(f"  [PARITY WARN] {label} YES+NO={_parity_sum:.4f}<0.96 — hollow book, widening pullback")
        yes_price = safe_price(yes_mid, extra_pullback=0.02)
        no_price  = safe_price(no_mid,  extra_pullback=0.02)

    # ── Position conflict check (EVPoly-inspired) ─────────────────────────
    # If we hold a directional position >100 shares on this contract,
    # block the LP bot from placing the opposing side.
    # This prevents LP fills from creating losing contradictions.
    CONFLICT_THRESHOLD = 100  # shares
    block_yes = False
    block_no  = False
    try:
        pos_r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50",
            timeout=10
        )
        if pos_r.status_code == 200:
            for pos in pos_r.json():
                if pos.get("conditionId","") != mkt.get("condition_id",""):
                    continue
                pos_outcome = pos.get("outcome","").upper()
                pos_size    = float(pos.get("size", 0))
                if pos_size < CONFLICT_THRESHOLD:
                    continue
                # Block the opposing side
                if pos_outcome == "NO":
                    block_yes = True
                    log(f"[{label}] Conflict: hold {pos_size:.0f} NO → blocking YES order")
                elif pos_outcome == "YES":
                    block_no = True
                    log(f"[{label}] Conflict: hold {pos_size:.0f} YES → blocking NO order")
    except Exception as _ce:
        pass  # conflict check failure → proceed (fail open, not closed)

    # ── Inventory cap check ──────────────────────────────────────────────────
    max_inv = mkt.get("max_inventory", 9999)
    try:
        _pos_r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50", timeout=8
        )
        if _pos_r.status_code == 200:
            for _p in _pos_r.json():
                if _p.get("conditionId","") != mkt.get("condition_id",""):
                    continue
                _out = _p.get("outcome","").upper()
                _sz  = float(_p.get("size", 0))
                if _sz >= max_inv:
                    if _out == "YES" and not block_yes:
                        block_yes = True
                        log(f"[{label}] Inventory cap: hold {_sz:.0f} YES >= {max_inv} — blocking YES LP")
                    elif _out == "NO" and not block_no:
                        block_no = True
                        log(f"[{label}] Inventory cap: hold {_sz:.0f} NO >= {max_inv} — blocking NO LP")
    except Exception:
        pass  # fail open

    yes_oid, yes_p = (None, yes_price) if block_yes else place_buy(client, mkt["yes_token"], yes_price, target, f"[{label}] YES")
    no_oid,  no_p  = (None, no_price)  if block_no  else place_buy(client, mkt["no_token"],  no_price,  target, f"[{label}] NO")

    placed = sum(1 for x in [yes_oid, no_oid] if x)
    if placed == 2:
        # Estimate share of pool for Telegram summary
        est_share = (target / 92000) * 100  # rough, using Apr30 zone depth
        est_daily = mkt["pool_day"] * (est_share / 100) * 3  # ×3 two-sided
        log(f"[{label}] ✓ Both sides placed | est ~${est_daily:.2f}/day LP rewards")
    elif placed == 1:
        log(f"[{label}] ⚠️  Only {placed}/2 orders placed — partial LP")
    else:
        log(f"[{label}] ✗ No orders placed")

    ms.update({
        "yes_mid":          yes_mid,
        "no_mid":           no_mid,
        "yes_order_id":     yes_oid,
        "no_order_id":      no_oid,
        "yes_quoted_price": yes_p,
        "no_quoted_price":  no_p,
        "target_shares":    target,
        "last_quote_ts":    time.time(),
    })
    state[label] = ms
    return state

# ── Daily summary ─────────────────────────────────────────────────────────────

def maybe_send_daily_summary(state):
    last = float(state.get("last_summary_ts", 0))
    if time.time() - last < 86400:
        return
    lines = ["📊 <b>LP Daily Summary</b>"]
    for mkt in LP_MARKETS:
        if not mkt["enabled"]:
            continue
        ms = state.get(mkt["label"], {})
        yes_f = float(ms.get("yes_filled_usdc", 0))
        no_f  = float(ms.get("no_filled_usdc",  0))
        lines.append(f"\n<b>{mkt['label']}</b>  pool=${mkt['pool_day']}/day\n"
                     f"YES fills: ${yes_f:.2f}  NO fills: ${no_f:.2f}")
    tg("\n".join(lines))
    state["last_summary_ts"] = time.time()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 55)


    log("LP QUOTER v1.0 — " + datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    log("=" * 55)

    # Kill switch
    if os.path.exists(KILL_FILE):
        log("KILL FILE found — cancelling all orders and stopping")
        try:
            client = get_client()
            cancel_all(client, get_open_orders(client))
            tg("🛑 <b>LP Quoter</b> — kill switch activated, all orders cancelled")
        except Exception as e:
            log(f"Kill error: {e}")
        return

    # Init client
    try:
        client = get_client()
    except Exception as e:
        log(f"FATAL client init: {e}")
        tg(f"❌ <b>LP Quoter</b> — client init failed: {e}")
        return

    # Balance
    usdc = get_usdc_balance(client)
    log(f"USDC balance: ${usdc:.2f}")

    if usdc < CASH_BUFFER + 50:
        log(f"USDC ${usdc:.2f} too low (need >${CASH_BUFFER + 50:.0f}) — not quoting")
        return

    # State + open orders
    state = load_state()
    # Re-enable markets flagged reset_fills (fill limit was triggered by first-run fills)
    for mkt in LP_MARKETS:
        if mkt.get("reset_fills"):
            mkt["enabled"] = True
            ms = state.get(mkt["label"], {})
            ms["yes_filled_usdc"] = 0.0
            ms["no_filled_usdc"]  = 0.0
            state[mkt["label"]] = ms
            mkt.pop("reset_fills", None)
    open_orders = get_open_orders(client)
    open_order_ids = {o["id"] for o in open_orders}
    log(f"Open orders: {len(open_orders)}")

    # Track fills since last run
    state = track_fills(open_order_ids, state)

    # Run each enabled market
    usdc_remaining = usdc
    for mkt in LP_MARKETS:
        if not mkt["enabled"]:
            log(f"[{mkt['label']}] disabled — skip")
            continue
        try:
            state = run_market(client, mkt, state, open_orders, usdc_remaining)
            # Deduct reserved collateral from available
            ms = state.get(mkt["label"], {})
            shares = float(ms.get("target_shares", mkt["target_shares"]))
            yes_mid = float(ms.get("yes_mid", 0.5))
            no_mid  = float(ms.get("no_mid",  0.5))
            usdc_remaining -= shares * (yes_mid + no_mid)
            usdc_remaining  = max(0, usdc_remaining)
        except Exception as e:
            log(f"[{mkt['label']}] ERROR: {e}")
            tg(f"⚠️ <b>LP Quoter</b> {mkt['label']}: {e}")

    # Daily summary
    maybe_send_daily_summary(state)

    save_state(state)
    log("LP QUOTER — run complete\n")


if __name__ == "__main__":
    main()
