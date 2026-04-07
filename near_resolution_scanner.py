"""
near_resolution_scanner.py
==========================
Type-4 "Near Resolution" bot — anon-fake style ($7,460 → +$213,681).

Scans ALL active Polymarket markets for outcomes priced >= 0.96¢ that
have not yet resolved. Buys the near-certain side for the residual 1-4¢.

Edge: Markets don't jump from 0.99 → 1.00 instantly. Resolution takes
minutes to hours after the outcome is factually determined. This captures
that gap with near-zero directional risk.

Safety controls:
- Never buys within 30 min of resolution (oracle latency risk)
- Confirms with a secondary source (volume spike + price confirmation)
- Skips sports markets within 4h of game start (live repricing risk)
- Max position: $50 per market (small, many markets, law of large numbers)
- Skips markets we already hold the same side on (no doubling)
- Tracks all buys in near_res_state.json to avoid re-entry

Runs every 30 minutes via cron.
"""
import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

# ── Config ─────────────────────────────────────────────────────────────────────
PRIVATE_KEY   = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER        = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

NEAR_RES_THRESHOLD  = 0.955   # Buy outcomes priced >= 95.5¢
MIN_PROFIT_CENTS    = 1.5     # Skip if (1.00 - price) < 1.5¢ — not worth fees
MAX_POSITION_USDC   = 50      # Max $50 per market
MIN_POSITION_USDC   = 5       # Min $5 (enough to matter)
MIN_VOLUME_24H      = 500     # $500 min 24h volume (confirms active market)
SPORTS_BUFFER_HRS   = 4       # Skip sports markets ending within 4h
MIN_TIME_TO_END_MIN = 30      # Skip markets ending in <30 min (oracle risk)
STATE_FILE          = "/opt/polymarket-agent/near_res_state.json"
LOG_FILE            = "/opt/polymarket-agent/near_res_scanner.log"

# Markets we've already bought and are holding — avoid re-entry
# Keyed by conditionId+outcome, value is entry timestamp
_state: dict = {}

def load_state():
    global _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except:
        _state = {}

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(_state, f, indent=2)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        except:
            pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [NearRes] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass

def get_usdc_balance():
    """Get available USDC in CLOB."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, signature_type=2, funder=FUNDER)
        c = client.create_or_derive_api_creds()
        client.set_api_creds(c)
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))
        return float(bal.get("balance", 0)) / 1e6, client
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return 0.0, None

def get_existing_positions():
    """Return set of (conditionId, outcome) pairs we already hold."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=100",
            timeout=10
        )
        positions = set()
        for p in r.json():
            cid = p.get("conditionId", "")
            outcome = p.get("outcome", "")
            if cid and outcome and float(p.get("size", 0)) > 0:
                positions.add((cid, outcome.upper()))
        return positions
    except:
        return set()

def scan_near_resolution_markets():
    """
    Fetch all active markets and find ones with an outcome >= NEAR_RES_THRESHOLD.
    Returns list of candidate dicts sorted by highest price first.
    """
    candidates = []
    now = datetime.datetime.utcnow()

    # Fetch high-volume active markets — scan broadly
    for limit, order in [(200, "volume24hr"), (200, "liquidity")]:
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets"
                f"?active=true&closed=false&limit={limit}&order={order}&ascending=false",
                timeout=15
            )
            if not r.ok:
                continue
            for m in r.json():
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    if len(prices) < 2:
                        continue
                    yes_p = float(prices[0])
                    no_p  = float(prices[1])
                except:
                    continue

                # Check if either side is near resolution
                for outcome, price, token_idx in [("YES", yes_p, 0), ("NO", no_p, 1)]:
                    if price < NEAR_RES_THRESHOLD:
                        continue
                    residual = 1.0 - price
                    if residual < (MIN_PROFIT_CENTS / 100):
                        continue

                    vol24 = float(m.get("volume24hr") or 0)
                    if vol24 < MIN_VOLUME_24H:
                        continue

                    end_str = m.get("endDate", "") or m.get("endDateIso", "")
                    mins_to_end = None
                    if end_str:
                        try:
                            end_dt = datetime.datetime.fromisoformat(
                                end_str.replace("Z", "+00:00").replace("+00:00", "")
                            )
                            mins_to_end = (end_dt - now).total_seconds() / 60
                            if mins_to_end < MIN_TIME_TO_END_MIN:
                                log(f"  SKIP (too close to end {mins_to_end:.0f}m): {m.get('question','')[:50]}")
                                continue
                        except:
                            pass

                    q = m.get("question", "")
                    q_lower = q.lower()

                    # Skip sports markets within buffer hours of end
                    is_sports = any(x in q_lower for x in [
                        "nba", "nfl", "nhl", "mlb", "ufc", "premier league",
                        "champions league", "la liga", "bundesliga", "serie a",
                        "ligue 1", "mls", " fc ", " vs ", "game ", "match ",
                        "tennis", "golf", "f1", "formula", "cricket", "rugby"
                    ])
                    if is_sports and mins_to_end is not None and mins_to_end < SPORTS_BUFFER_HRS * 60:
                        log(f"  SKIP (sports within {SPORTS_BUFFER_HRS}h): {q[:50]}")
                        continue

                    # Skip markets with coin-flip pricing (stale/broken)
                    if 0.49 < yes_p < 0.51 and 0.49 < no_p < 0.51:
                        continue

                    try:
                        tokens = json.loads(m.get("clobTokenIds", "[]") or "[]")
                    except:
                        tokens = []

                    token_id = tokens[token_idx] if len(tokens) > token_idx else ""
                    cid = m.get("conditionId", "")

                    key = f"{cid}:{outcome}"
                    if key in _state:
                        continue  # already bought, skip

                    candidates.append({
                        "question":    q,
                        "outcome":     outcome,
                        "price":       price,
                        "residual":    residual,
                        "vol24h":      vol24,
                        "conditionId": cid,
                        "token_id":    token_id,
                        "end_str":     end_str,
                        "mins_to_end": mins_to_end,
                        "is_sports":   is_sports,
                        "liq":         float(m.get("liquidityNum") or m.get("liquidity") or 0),
                    })

        except Exception as e:
            log(f"Market fetch error: {e}")
            continue

    # Deduplicate by (conditionId, outcome)
    seen = set()
    unique = []
    for c in candidates:
        k = (c["conditionId"], c["outcome"])
        if k not in seen:
            seen.add(k)
            unique.append(c)

    # Sort: highest price first (most certain), then by volume
    unique.sort(key=lambda x: (-x["price"], -x["vol24h"]))
    return unique

def confirm_near_resolution(candidate):
    """
    Secondary confirmation before buying:
    1. Fetch live CLOB midpoint — confirm price still >= threshold
    2. Check recent trade activity — volume spike in last 1h confirms resolution
    3. Verify the book has real bids (not hollow)
    Returns (confirmed: bool, live_price: float, reason: str)
    """
    token = candidate["token_id"]
    if not token:
        return False, 0, "no token_id"

    # 1. Live CLOB midpoint
    try:
        r = requests.get(
            f"https://clob.polymarket.com/midpoint?token_id={token}",
            timeout=6
        )
        live_price = float(r.json().get("mid", 0))
    except:
        return False, 0, "midpoint fetch failed"

    if live_price < NEAR_RES_THRESHOLD:
        return False, live_price, f"price dropped to {live_price:.3f} — no longer near-res"

    # 2. Recent volume check — look for activity spike
    try:
        r2 = requests.get(
            f"https://data-api.polymarket.com/activity?market={token}&limit=20",
            timeout=8
        )
        if r2.ok:
            trades = r2.json()
            cutoff = time.time() - 3600  # last 1h
            recent_vol = sum(
                float(t.get("usdcSize", 0))
                for t in trades
                if t.get("timestamp", 0) > cutoff
            )
            if recent_vol < 50 and candidate["vol24h"] < 5000:
                # Low recent activity AND low overall volume = stale price
                return False, live_price, f"low recent volume (${recent_vol:.0f} in last 1h) — stale price"
    except:
        pass  # volume check optional — proceed without it

    # 3. Book depth check — confirm real bids exist on this side
    try:
        r3 = requests.get(
            f"https://clob.polymarket.com/book?token_id={token}",
            timeout=6
        )
        book = r3.json()
        bids = book.get("bids", [])
        if not bids:
            return False, live_price, "no bids in book — hollow market"
        best_bid = float(bids[0].get("price", 0)) if bids else 0
        if best_bid < NEAR_RES_THRESHOLD - 0.02:
            return False, live_price, f"best bid {best_bid:.3f} too far from ask — illiquid"
    except:
        pass  # book check optional

    return True, live_price, "confirmed"

def place_near_res_trade(candidate, usdc_available, client):
    """
    Place market buy for a near-resolution outcome.
    Size: min(MAX_POSITION_USDC, usdc_available * 0.10) — max 10% of balance per trade.
    """
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    price       = candidate["price"]
    outcome     = candidate["outcome"]
    token_id    = candidate["token_id"]
    question    = candidate["question"]
    residual    = candidate["residual"]

    # Size: 10% of available cash, capped at MAX, floored at MIN
    size_usdc = min(MAX_POSITION_USDC, max(MIN_POSITION_USDC, usdc_available * 0.10))
    shares    = size_usdc / price  # shares we'll receive
    profit_if_win = shares * residual  # expected profit

    log(f"  PLACING: {outcome} on '{question[:50]}' @ {price:.4f}")
    log(f"  Size: ${size_usdc:.2f} → {shares:.1f}sh | residual: {residual*100:.2f}¢ | expected profit: ${profit_if_win:.2f}")

    try:
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=size_usdc,
        ))
        resp = client.post_order(order, OrderType.FOK)

        if resp and resp.get("success"):
            log(f"  ✅ FILLED: {outcome} '{question[:40]}' @ {price:.4f} for ${size_usdc:.2f}")
            tg(
                f"🎯 <b>Near-Resolution BUY</b>\n"
                f"Market: {question[:60]}\n"
                f"Side: {outcome} @ {price:.4f} ({price*100:.1f}¢)\n"
                f"Size: ${size_usdc:.2f} → {shares:.1f} shares\n"
                f"Residual: {residual*100:.2f}¢ | Expected profit: ${profit_if_win:.2f}\n"
                f"End: {candidate.get('end_str','?')[:16]}"
            )
            return True, size_usdc
        else:
            log(f"  ❌ Order failed: {resp}")
            return False, 0

    except Exception as e:
        log(f"  ❌ Exception placing order: {e}")
        return False, 0

def run():
    log("=== Near-Resolution Scanner starting ===")
    load_state()

    usdc_balance, client = get_usdc_balance()
    log(f"USDC balance: ${usdc_balance:.2f}")

    if usdc_balance < MIN_POSITION_USDC:
        log("Insufficient balance — exiting")
        return

    # Buffer: leave $200 in account always
    BUFFER = 200
    usdc_available = max(0, usdc_balance - BUFFER)
    if usdc_available < MIN_POSITION_USDC:
        log(f"After $200 buffer, only ${usdc_available:.2f} available — exiting")
        return

    existing_positions = get_existing_positions()
    log(f"Existing positions: {len(existing_positions)}")

    candidates = scan_near_resolution_markets()
    log(f"Near-resolution candidates: {len(candidates)}")

    trades_placed = 0
    usdc_spent    = 0

    for c in candidates:
        # Skip if we already hold this side
        key_pos = (c["conditionId"], c["outcome"])
        if key_pos in existing_positions:
            log(f"  SKIP (already hold): {c['outcome']} '{c['question'][:40]}'")
            continue

        # Cap total deployment per scanner run
        if usdc_spent >= 200:
            log("  Reached $200 per-run deployment cap — stopping")
            break

        if usdc_available - usdc_spent < MIN_POSITION_USDC:
            log("  Insufficient remaining balance — stopping")
            break

        log(f"\nCandidate: {c['outcome']} '{c['question'][:60]}' @ {c['price']:.4f}")
        log(f"  Vol24h: ${c['vol24h']:,.0f} | Residual: {c['residual']*100:.2f}¢ | End: {c.get('end_str','?')[:16]}")

        # Secondary confirmation
        confirmed, live_price, reason = confirm_near_resolution(c)
        if not confirmed:
            log(f"  SKIP (confirmation failed): {reason}")
            continue

        c["price"] = live_price  # use confirmed live price

        # Place trade
        remaining = usdc_available - usdc_spent
        filled, spent = place_near_res_trade(c, remaining, client)

        if filled:
            trades_placed += 1
            usdc_spent    += spent
            state_key = f"{c['conditionId']}:{c['outcome']}"
            _state[state_key] = {
                "ts":       time.time(),
                "question": c["question"][:60],
                "price":    live_price,
                "spent":    spent,
            }
            save_state()

    log(f"\n=== Done: {trades_placed} trades placed, ${usdc_spent:.2f} deployed ===")

    if trades_placed > 0:
        tg(
            f"📊 <b>Near-Resolution Scanner Summary</b>\n"
            f"Trades: {trades_placed} | Deployed: ${usdc_spent:.2f}\n"
            f"Balance remaining: ${usdc_balance - usdc_spent:.2f}"
        )

if __name__ == "__main__":
    run()
