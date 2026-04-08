"""
opportunity_scanner.py
======================
Autonomous opportunity scanner — runs every 2 hours via cron.
Scans Polymarket for mispriced markets using news + UW whale/insider signals.
Places trades directly when edge > threshold, sends Telegram summary.

Strategy:
- Pulls top 50 markets by liquidity/volume
- Augments with UW whale/unusual/insider signals from Unusual Whales API
- For each candidate: fetches news, UW signal, scores edge with Claude
- Places BUY_NO or BUY_YES when edge >= threshold (lower if UW insider flag)
- Reports to Telegram regardless (good or no-trade)

Runs on server via executor. Uses same .env as autotrader.
"""
import os, sys, re, json, math, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
try:
    from signal_engine import run_signal_engine
    _SIGNAL_ENGINE_AVAILABLE = True
except ImportError:
    _SIGNAL_ENGINE_AVAILABLE = False
    def run_signal_engine(*args, **kwargs):
        return {"signal_summary": "", "kelly_size": 0, "combined_prob": kwargs.get("yes_p", 0.5),
                "kelly": {"reason": "signal_engine not installed"}, "label": "PASS", "ir": 0}

try:
    from very_hot_forward_test import record_very_hot_signal
except ImportError:
    def record_very_hot_signal(*a, **kw): pass

load_dotenv('/opt/polymarket-agent/.env')

# ── Hormuz / Iran Conflict Proxy Signal ───────────────────────────────────────
# No dedicated Hormuz market exists on Polymarket. We use the ceasefire term
# structure as a real-time proxy:
#   ceasefire_prob   = P(deal reached) → Hormuz reopens → oil price falls
#   conflict_ongoing = 1 - ceasefire_prob → Hormuz closed → war premium persists
#
# The NEAREST active ceasefire market is the best signal:
# - Apr15 at ~96¢ YES: deal essentially done for near-term
# - Jun30 at ~85¢ YES: market pricing >85% ceasefire by June
#
# Used by: oil price market scoring, news gate veto logic, OPERATING_CONSTITUTION

CEASEFIRE_TOKENS = {
    "Apr15": "85191934649046129480174964255278880752271767733539167443243111973456166096127",
    "Apr30": "44149007410374101286260953227333745102128417138356632089802983317837574022801",
    "Jun30": "57478765869949888455459956095684929476027637398040453022498617640396695289645",
}

def fetch_hormuz_proxy() -> dict:
    """
    Fetch live ceasefire market prices and derive a Hormuz reopening probability signal.
    Returns a dict with:
      - hormuz_reopen_prob: float  (best estimate of P(Hormuz reopens) = nearest ceasefire YES)
      - conflict_ongoing_prob: float  (1 - hormuz_reopen_prob)
      - war_premium_multiplier: float  (1.0 = no premium, 1.5 = 50% premium expected)
      - signal_label: str  (human-readable summary for Claude prompt injection)
      - prices: dict  (raw ceasefire YES prices by expiry)
    """
    prices = {}
    for label, token in CEASEFIRE_TOKENS.items():
        try:
            r = requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={token}",
                timeout=5
            )
            if r.ok:
                prices[label] = round(float(r.json().get("mid", 0.5)), 3)
        except:
            pass

    if not prices:
        return {
            "hormuz_reopen_prob": 0.5,
            "conflict_ongoing_prob": 0.5,
            "war_premium_multiplier": 1.25,
            "signal_label": "Hormuz proxy: ceasefire data unavailable — using neutral 50%",
            "prices": {},
        }

    # Use nearest available expiry as primary signal
    nearest_prob = prices.get("Apr15") or prices.get("Apr30") or prices.get("Jun30") or 0.5
    conflict_prob = round(1.0 - nearest_prob, 3)

    # War premium multiplier: at 0% ceasefire (full war) → 1.5x premium
    # At 100% ceasefire (peace) → 1.0x (no premium)
    # Linear: multiplier = 1.0 + 0.5 * conflict_prob
    war_premium = round(1.0 + 0.5 * conflict_prob, 3)

    # Build label
    prices_str = " | ".join(f"{k} YES={v:.0%}" for k, v in sorted(prices.items()))
    if nearest_prob > 0.85:
        interpretation = "CEASEFIRE LIKELY — Hormuz reopening priced in, oil war premium collapsing"
    elif nearest_prob > 0.60:
        interpretation = "CEASEFIRE PROBABLE — partial Hormuz premium still priced"
    elif nearest_prob > 0.35:
        interpretation = "UNCERTAIN — Hormuz status unclear, mixed signals"
    else:
        interpretation = "CONFLICT ONGOING — Hormuz war premium elevated"

    label = (
        f"Hormuz Proxy (via Polymarket ceasefire markets): {interpretation}\n"
        f"  Ceasefire prob: {nearest_prob:.0%} | Conflict prob: {conflict_prob:.0%} | "
        f"War premium multiplier: {war_premium:.2f}x\n"
        f"  Raw prices: {prices_str}"
    )

    return {
        "hormuz_reopen_prob":   nearest_prob,
        "conflict_ongoing_prob": conflict_prob,
        "war_premium_multiplier": war_premium,
        "signal_label":         label,
        "prices":               prices,
    }



# ── Price Velocity Signal ─────────────────────────────────────────────────────
# Detects fast-moving markets that need immediate re-scoring.
# Rule 3 from quant framework: "60%→75% in 2h = new info; 75% for 3 weeks = consensus."
#
# Thresholds (tuned for Polymarket geopolitical markets):
#   VELOCITY_HOT:  |Δp| > 12% in 1h  → "breaking news repricing" — force rescore + upgrade to bull/bear
#   VELOCITY_WARM: |Δp| > 6%  in 1h  → "notable move" — bypass scan cache, rescore fresh
#   VELOCITY_COOL: |Δp| > 4%  in 4h  → "slow drift" — log only, rescore if cache expired
#
# Apr15 YES example: 32% → 94% in 4h = +62% → HOT signal, would have triggered at first 8% move

VELOCITY_VERY_HOT_1H = 0.25  # 25% in 1h → 86% win rate (backtest) → STRONG_BUY label
VELOCITY_HOT_1H  = 0.12   # 12% in 1h → 60% win rate → force bull/bear debate
VELOCITY_WARM_1H = 0.06   # 6%  in 1h → bypass cache, rescore
VELOCITY_COOL_4H = 0.04   # 4%  in 4h → log, rescore if cache stale

def fetch_price_velocity(token_id: str) -> dict:
    """
    Fetch recent price history for a token and compute velocity metrics.
    Returns dict with:
      - delta_1h: float  (signed price change over last 1h)
      - delta_4h: float  (signed price change over last 4h)
      - abs_1h:   float  (absolute 1h change)
      - abs_4h:   float  (absolute 4h change)
      - tier:     str    ("HOT" | "WARM" | "COOL" | "FLAT")
      - direction: str   ("UP" | "DOWN" | "FLAT")
      - label:    str    (human-readable summary)
      - current_p: float
    """
    _empty = {"delta_1h": 0, "delta_4h": 0, "abs_1h": 0, "abs_4h": 0,
               "tier": "FLAT", "direction": "FLAT", "label": "", "current_p": 0}
    if not token_id:
        return _empty
    try:
        r = requests.get(
            f"https://clob.polymarket.com/prices-history?market={token_id}&interval=max&fidelity=60",
            timeout=6
        )
        if not r.ok:
            return _empty
        history = r.json().get("history", [])
        if len(history) < 5:
            return _empty

        now_ts   = history[-1]["t"]
        now_p    = history[-1]["p"]

        def price_at_offset(offset_secs):
            target_ts = now_ts - offset_secs
            for h in reversed(history):
                if h["t"] <= target_ts:
                    return h["p"]
            return history[0]["p"]

        p_1h_ago = price_at_offset(3600)
        p_4h_ago = price_at_offset(14400)

        delta_1h = now_p - p_1h_ago
        delta_4h = now_p - p_4h_ago
        abs_1h   = abs(delta_1h)
        abs_4h   = abs(delta_4h)

        if abs_1h >= VELOCITY_VERY_HOT_1H:
            tier = "VERY_HOT"
        elif abs_1h >= VELOCITY_HOT_1H:
            tier = "HOT"
        elif abs_1h >= VELOCITY_WARM_1H:
            tier = "WARM"
        elif abs_4h >= VELOCITY_COOL_4H:
            tier = "COOL"
        else:
            tier = "FLAT"

        direction = "UP" if delta_1h > 0.01 else ("DOWN" if delta_1h < -0.01 else "FLAT")

        if tier != "FLAT":
            tier_badge = "🔥🔥 VERY HOT" if tier == "VERY_HOT" else f"[VELOCITY {tier}]"
            label = (
                f"{tier_badge} {direction} {abs_1h*100:.1f}% in 1h "
                f"({p_1h_ago:.3f}→{now_p:.3f}) | 4h: {delta_4h*100:+.1f}%"
            )
        else:
            label = ""

        return {
            "delta_1h":  round(delta_1h, 4),
            "delta_4h":  round(delta_4h, 4),
            "abs_1h":    round(abs_1h, 4),
            "abs_4h":    round(abs_4h, 4),
            "tier":      tier,
            "direction": direction,
            "label":     label,
            "current_p": round(now_p, 4),
        }
    except Exception as _e:
        return _empty

# ── Config ────────────────────────────────────────────────────────────────────

# Load scanner config (tunable params + blacklists)
# Hardcoded blacklist — markets to NEVER trade again (by conditionId)
# These are also in scanner_config.json for reference, but this is the authoritative source.
_HARDCODED_BLACKLIST = {
    "0xc5300759dc2089042380795fe7384010a6b6ebdf9e6da7ed3f786d9a5f61c563":
        "Lesson 9+10: Crude Oil $100 HIGH — bought NO twice when WTI was at trigger",
    "0x36912c9832f0fd104d734b579fb9b3a1b31bbdc946a67356723407e3bdc96dbc":
        "Lesson 11: BTC $65k dip NO — bought when BTC was 1.8% above trigger ($66,173 vs $65k)",
    "0x4290a4aa43a0707f0f1193c73667074f2ef5ce8ab5d6fcdd4ca645bfe1528f03":
        "Lesson 11: BTC $60k dip YES — BTC needs 10% drop in 4 days, unrealistic",
}

def _load_config():
    import json as _json, os as _os
    cfg_path = _os.path.join("/opt/polymarket-agent", "scanner_config.json")
    try:
        with open(cfg_path) as _f:
            return _json.load(_f)
    except:
        return {}

_SCANNER_CONFIG = _load_config()
# Merge hardcoded + config blacklists (hardcoded always wins)
_config_blacklist = _SCANNER_CONFIG.get("BLACKLISTED_CONDITION_IDS", {})
BLACKLISTED_CONDITIONS = {**_config_blacklist, **_HARDCODED_BLACKLIST}
COMMODITY_BUFFER_USD = float(_SCANNER_CONFIG.get("COMMODITY_BUFFER_USD", 5.0))

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID","").strip()
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY","").strip()
PERPLEXITY_KEY  = os.environ.get("PERPLEXITY_API_KEY","").strip()
UW_API_KEY       = os.environ.get("UW_API_KEY","").strip()
UW_BASE       = "https://api.unusualwhales.com"

MIN_SCAN_EDGE    = 0.15   # Raised from 0.12 — only very high conviction trades's 0.07 — only obvious mispricings
MIN_LIQUIDITY    = 50000  # $50k minimum liquidity
MAX_TRADE_SIZE   = 150    # Raised from $75: capital base $4,700 — 3.2% per trade
MIN_TRADE_SIZE   = 50
UW_EDGE_DISCOUNT = 0.20   # Lower edge threshold by 20% when UW insider/whale signal present

# ── Priority watchlist: markets to score regardless of yes_p filter ────────────
# These are 0%-fee geopolitical contracts where NO edge is structural.
# Bypass the yes_p < 0.06 gate — scanner would otherwise skip them entirely.
PRIORITY_WATCHLIST = [
    # US x Iran ceasefire by April 7
    {
        "condition_id": "0x4c5701bcde0b8fb7d7f48c8e9d20245a6caa58c61a77f981fad98f2bfa0b1bc7",
        "label": "Ceasefire Apr7",
        "yes_token": "82855088893985825781350466813737280564000275725006328179621744619327480699369",
        "no_token":  "55194745453074297560900438908357749978780021444937743754846798173575377021411",
    },
    # US x Iran ceasefire by April 15
    {
        "condition_id": "0x773abaa5fe55e5cde51a261f444b7921652a4e059ead6b3be9fe56499c2d4609",
        "label": "Ceasefire Apr15",
        "yes_token": "85191934649046129480174964255278880752271767733539167443243111973456166096127",
        "no_token":  "8442709013751543525223072638303914942960068246422295030411662679470140144155",
    },
    # US x Iran ceasefire by April 30
    {
        "condition_id": "0x80059ff4e694f878c0498f6f3a067ee7ca62dc2fc46251a4287b58355ce47bc5",
        "label": "Ceasefire Apr30",
        "yes_token": "44149007410374101286260953227333745102128417138356632089802983317837574022801",
        "no_token":  "52284848830940446862370529859386043059769275594386884690262695607365719243018",
    },
    # US x Iran ceasefire by May 31
    {
        "condition_id": "",
        "label": "Ceasefire May31",
        "label_match": "ceasefire by may 31",
    },
    # US x Iran ceasefire by June 30 — zerosmart bought $3,280 NO today (Apr 6)
    # Iran rejected ceasefire plan. Trump set April 7 8pm EDT deadline.
    # YES at 57¢ implies 57% probability — thesis: ~42%, edge ~+15%, 0% fee
    {
        "condition_id": "",
        "label": "Ceasefire Jun30 NO",
        "label_match": "ceasefire by june 30",
    },
    # US x Iran ceasefire by May 31 — YES at 48¢, Iran rejected plan today
    # We hold 121sh NO @ 0.62 (currently -15%). Thesis intact: ~+22% edge.
    {
        "condition_id": "",
        "label": "Ceasefire May31 NO",
        "label_match": "ceasefire by may 31",
    },
    # Trump announces end of military operations against Iran
    {
        "condition_id": "",
        "label": "Trump ends Iran ops",
        "label_match": "trump announces end of military operations against iran",
    },
    # US invade Mexico in 2026 — YES=8¢ on 0% fee, ~98% true NO probability
    # Resolution: US must establish territorial control. Cross-border ops don't qualify.
    {
        "condition_id": "",
        "label": "US invade Mexico NO",
        "label_match": "us invade mexico in 2026",
    },
    # Putin out as President of Russia by Dec 31 — SecondWindCapital bought $3.8k NO
    {
        "condition_id": "",
        "label": "Putin out Dec31 NO",
        "label_match": "putin out as president of russia by december 31",
    },
    # US forces enter Iran by April 30 (YES directional — we hold this)
    {
        "condition_id": "",
        "label": "Forces Apr30",
        "label_match": "us forces enter iran by april 30",
    },
    {
        "label": "Hormuz traffic Apr30 YES",
        "label_match": "strait of hormuz traffic returns to normal by end of april",
        "preferred_side": "BUY_YES",
        "fee_pct": 0.0,
        "notes": "Post-ceasefire de-escalation. Zerosmart $3.6K UAE NO = same thesis. YES at 29c if Hormuz reopens.",
    },
]
ALREADY_IN_FILE  = "/opt/polymarket-agent/intelligence/existing_positions.json"
SCAN_LOG         = "/opt/polymarket-agent/opportunity_scan.log"
SCAN_SCORE_CACHE_TTL = 7200  # 2 hours — skip re-scoring if price unchanged

# ── Scan score cache (keyed on conditionId, invalidated by price move ≥ 0.5¢) ──
_scan_score_cache: dict = {}  # {conditionId: {ts, yes_p, action, edge, reasoning}}

def _scan_cache_get(mkt):
    """Return cached (action, edge, reasoning) if fresh and price unchanged."""
    key = mkt.get("conditionId", "") or mkt.get("question", "")[:80]
    entry = _scan_score_cache.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > SCAN_SCORE_CACHE_TTL:
        return None
    if abs(entry["yes_p"] - mkt.get("yes_p", 0)) > 0.005:
        return None  # price moved — re-score
    return entry["action"], entry["edge"], entry["reasoning"]

def _scan_cache_set(mkt, action, edge, reasoning):
    key = mkt.get("conditionId", "") or mkt.get("question", "")[:80]
    _scan_score_cache[key] = {
        "ts": time.time(), "yes_p": mkt.get("yes_p", 0),
        "action": action, "edge": edge, "reasoning": reasoning,
    }

def tg(msg, parse_mode="HTML"):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": parse_mode}, timeout=10)
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(SCAN_LOG, "a") as f:
            f.write(line + "\n")
    except: pass


# ── Live commodity price checker ─────────────────────────────────────────────

def get_commodity_price(symbol):
    """Fetch live commodity/stock price from Yahoo Finance."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except:
        return None


def check_commodity_reality(question, yes_p):
    """
    Reality check for commodity price markets.
    
    LESSON 9+10 FIX: Original code used `yes_p < 0.5` as proxy for "we are
    buying NO", but when market is at 57% YES the check never fires — yet
    the scanner still picks it as a NO trade. The fix: block the trade when
    price is within $5 of the trigger, regardless of yes_p direction.
    
    Rule: If the commodity is within $5 (~5%) of the trigger price, the market
    is too close to call and the NO has no edge. Skip it entirely.
    
    Returns (should_skip, reason) tuple.
    """
    q_lower = question.lower()
    
    # Crude oil HIGH target checks
    if ("crude oil" in q_lower or "wti" in q_lower or "cl)" in q_lower) and "high" in q_lower:
        import re as _re
        targets = _re.findall(r"\$([0-9,]+)", question)
        if targets:
            target = float(targets[0].replace(",", ""))
            wti = get_commodity_price("CL=F")
            if wti is not None:
                gap = target - wti  # positive = not yet hit; negative = already exceeded
                # HARD BLOCK: price within $5 of trigger in either direction
                if abs(gap) <= COMMODITY_BUFFER_USD:
                    return True, f"WTI ${wti:.2f} within $5 of ${target:.0f} HIGH trigger — too risky (gap=${gap:+.2f})"
                # HARD BLOCK: price already exceeded trigger → YES is certain, NO is worthless
                if gap < 0:
                    return True, f"WTI ${wti:.2f} already exceeded ${target:.0f} HIGH trigger — NO worthless"
                # Soft block: buying YES when price is >8% away with short time
                if yes_p > 0.5 and gap > target * 0.08:
                    return True, f"WTI ${wti:.2f} too far from ${target:.0f} HIGH target — YES unlikely (gap=${gap:.2f})"
    
    # Crude oil LOW target checks
    if ("crude oil" in q_lower or "wti" in q_lower or "cl)" in q_lower) and "low" in q_lower:
        import re as _re
        targets = _re.findall(r"\$([0-9,]+)", question)
        if targets:
            target = float(targets[0].replace(",", ""))
            wti = get_commodity_price("CL=F")
            if wti is not None:
                gap = wti - target  # positive = not yet hit; negative = already dropped below
                if abs(gap) <= COMMODITY_BUFFER_USD:
                    return True, f"WTI ${wti:.2f} within $5 of ${target:.0f} LOW trigger — too risky (gap={gap:+.2f})"
                if gap < 0:
                    return True, f"WTI ${wti:.2f} already below ${target:.0f} LOW trigger — NO worthless"
    
    # Bitcoin / crypto dip-to-X checks
    # "Will Bitcoin dip to $65,000" = we're betting on a LOW — same logic as crude LOW
    if ("bitcoin" in q_lower or "btc" in q_lower) and ("dip" in q_lower or "low" in q_lower or "fall" in q_lower or "drop" in q_lower):
        import re as _re
        targets = _re.findall(r"\$([0-9,]+)", question)
        if targets:
            target = float(targets[0].replace(",", ""))
            btc = get_commodity_price("BTC-USD")
            if btc is not None:
                gap_pct = (btc - target) / target  # positive = BTC above target (not yet dipped)
                gap_abs = btc - target
                # Block NO on dip market if BTC is within 5% of target (too close to call)
                if yes_p < 0.5 and gap_pct <= 0.05:  # buying NO but price within 5%
                    return True, f"BTC ${btc:,.0f} within 5% of ${target:,.0f} dip target — NO risky (gap={gap_pct*100:.1f}%)"
                # Block YES on dip market if BTC is >15% above target with <7 days left
                if yes_p > 0.5 and gap_pct > 0.15:
                    return True, f"BTC ${btc:,.0f} too far above ${target:,.0f} — dip unlikely (gap={gap_pct*100:.1f}%)"
                # Block if BTC already dipped below target (YES is guaranteed)
                if gap_pct < 0:
                    return True, f"BTC ${btc:,.0f} already below ${target:,.0f} — NO is worthless"
    
    # Gold checks
    if ("gold" in q_lower or "gc" in q_lower) and ("high" in q_lower or "low" in q_lower):
        import re as _re
        targets = _re.findall(r"\$([0-9,]+)", question)
        if targets:
            target = float(targets[0].replace(",", ""))
            gold = get_commodity_price("GC=F")
            if gold is not None:
                pct_gap = abs(gold - target) / target
                if pct_gap <= 0.015:  # within 1.5% of trigger
                    return True, f"Gold ${gold:.0f} within 1.5% of ${target:.0f} trigger — too risky"
                if "high" in q_lower and gold > target:
                    return True, f"Gold ${gold:.0f} already exceeded ${target:.0f} HIGH trigger — NO worthless"
                if "low" in q_lower and gold < target:
                    return True, f"Gold ${gold:.0f} already below ${target:.0f} LOW trigger — NO worthless"
    
    return False, "ok"


def get_usdc_balance():
    """Get available USDC cash."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                        chain_id=137, signature_type=2, funder=FUNDER)
    c = client.create_or_derive_api_creds()
    client.set_api_creds(c)
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=2))
    return float(bal.get("balance", 0)) / 1e6, client

# ── Unusual Whales Prediction Market Signal ─────────────────────────────────────

def get_uw_signals():
    """
    Fetch unusual + whale signals from Unusual Whales prediction market endpoints.
    Returns dict keyed by asset_id (CLOB token ID).
    """
    if not UW_API_KEY:
        return {}
    headers = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
    signals = {}

    # Unusual prediction markets (insider_trades, contrarian_whales tags)
    try:
        r = requests.get(f"{UW_BASE}/api/predictions/unusual", headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json().get("data", {}).get("data", []):
                asset_id = item.get("asset_id", "")
                if not asset_id:
                    continue
                tags = [t.get("tag", "") for t in item.get("tags", [])]
                signals[asset_id] = {
                    "source": "unusual",
                    "tags": tags,
                    "smart_volume": float(item.get("smart_volume", 0)),
                    "outcome": item.get("outcome", "Yes"),
                    "current_price": float(item.get("current", 0) or 0),
                    "market": item.get("market", ""),
                }
    except Exception as e:
        log(f"[UW] Unusual fetch error: {e}")

    # Whale positions ($50k+ smart money)
    try:
        r = requests.get(f"{UW_BASE}/api/predictions/whales", headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json().get("data", {}).get("data", []):
                asset_id = item.get("asset_id", "")
                if not asset_id:
                    continue
                invested = float(item.get("invested_usd", 0))
                if invested < 50000:
                    continue
                existing = signals.get(asset_id, {})
                signals[asset_id] = {
                    **existing,
                    "source": existing.get("source", "whale"),
                    "tags": list(set(existing.get("tags", []) + ["whale_position"])),
                    "whale_invested": invested,
                    "whale_amount": float(item.get("amount", 0)),
                    "outcome": item.get("outcome", existing.get("outcome", "Yes")),
                    "avg_price": float(item.get("avg_price", 0) or 0),
                    "current_price": float(item.get("current_price", existing.get("current_price", 0)) or 0),
                    "market": item.get("market", existing.get("market", "")),
                }
    except Exception as e:
        log(f"[UW] Whale fetch error: {e}")

    log(f"[UW] Loaded {len(signals)} signal(s) from Unusual Whales")
    return signals


def get_uw_signal_for_market(market, uw_signals):
    """Match market tokens against UW signals. Returns (signal or None, action_hint or None)."""
    tokens = market.get("clob_token_ids", [])
    for token in tokens:
        if token in uw_signals:
            sig = uw_signals[token]
            outcome = sig.get("outcome", "Yes").lower()
            is_yes_token = (token == tokens[0]) if len(tokens) > 1 else True
            if is_yes_token:
                action_hint = "BUY_YES" if "yes" in outcome else "BUY_NO"
            else:
                action_hint = "BUY_NO" if "yes" in outcome else "BUY_YES"
            return sig, action_hint
    return None, None


def uw_signal_summary(sig):
    """Format UW signal for Claude prompt injection."""
    if not sig:
        return ""
    parts = []
    whale_inv = sig.get("whale_invested", 0)
    smart_vol = sig.get("smart_volume", 0)
    tags = sig.get("tags", [])
    if whale_inv > 0:
        parts.append(f"Whale position: ${whale_inv:,.0f} invested at avg {sig.get('avg_price', 0):.3f}")
    if smart_vol > 0:
        parts.append(f"Smart money volume: ${smart_vol:,.0f}")
    if "insider_trades" in tags:
        parts.append("⚠️ INSIDER TRADES flagged by Unusual Whales")
    if "contrarian_whales" in tags:
        parts.append("⚠️ CONTRARIAN WHALES flagged by Unusual Whales")
    if "momentum" in tags:
        parts.append("Momentum signal active")
    return "\n".join(parts) if parts else ""


def get_existing_positions():
    """Get markets we already have positions in."""
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=100", timeout=10)
        return {p.get("conditionId","") for p in r.json()}
    except:
        return set()

def get_candidate_markets():
    """Pull top liquid markets, filter out obvious non-opportunities."""
    r = requests.get(
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
        "&order=volume24hr&ascending=false", timeout=15)
    markets = r.json()
    
    candidates = []
    for m in markets:
        liq = float(m.get("liquidity") or 0)
        if liq < MIN_LIQUIDITY:
            continue
        
        try:
            prices = json.loads(m.get("outcomePrices","[]"))
            yes_p = float(prices[0])
        except:
            continue
        
        # Skip near-certain or coin-flip with no news signal
        is_priority = any(
            (w.get("condition_id") and w["condition_id"] == m.get("conditionId","")) or
            (w.get("label_match") and w["label_match"] in m.get("question","").lower())
            for w in PRIORITY_WATCHLIST
        )
        if not is_priority and (yes_p > 0.94 or yes_p < 0.06):
            continue
        
        q = m.get("question","")
        q_lower = q.lower()
        # Skip in-play sports (spread/totals), tweet counts
        if any(x in q_lower for x in ["spread:", "o/u", "tweets", "post from", "how many times"]):
            continue
        # Skip token launches, FDV markets, TGE markets — these resolve before
        # any real order book forms and prices are stale/fake.
        # Lesson: Backpack FDV $200M NO cost $122 because the token launched at
        # $2B FDV but the market showed 46¢ YES with ghost order book depth.
        if any(x in q_lower for x in ["fdv", "tge", "token launch", "token generation",
                                       "fully diluted", "market cap on launch",
                                       "price on launch", "day after launch"]):
            continue
        # Skip same-day game results (already resolving)
        end = m.get("endDate","")
        if end:
            try:
                end_dt = datetime.datetime.fromisoformat(end.replace("Z",""))
                if (end_dt - datetime.datetime.utcnow()).total_seconds() < 3600:
                    continue  # resolves within 1h — skip
            except: pass
        
        candidates.append({
            "question": q,
            "yes_p": yes_p,
            "no_p": 1 - yes_p,
            "liquidity": liq,
            "volume24h": float(m.get("volume24hr") or 0),
            "conditionId": m.get("conditionId",""),
            "clob_token_ids": json.loads(m.get("clobTokenIds","[]") or "[]"),
            "endDate": end,
            "description": m.get("description","")[:400],
        })
    
    # ── Inject priority watchlist markets using CLOB token IDs directly ─────────
    # Gamma search doesn't support exact condition_id lookup.
    # Use known token IDs to fetch live YES price from CLOB midpoint endpoint.
    existing_cids = {c.get("conditionId","") for c in candidates}
    for wm in PRIORITY_WATCHLIST:
        wm_cid    = wm.get("condition_id","")
        yes_token = wm.get("yes_token","")
        no_token  = wm.get("no_token","")
        label     = wm.get("label", "")

        # Skip if already captured by volume sort
        if wm_cid and wm_cid in existing_cids:
            continue

        # Must have token IDs to proceed
        if not yes_token:
            continue

        # Fetch live YES price from CLOB
        try:
            mid_r = requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={yes_token}",
                timeout=6
            ).json()
            yes_p = float(mid_r.get("mid", 0))
        except Exception:
            yes_p = 0.05  # fallback

        # Skip if price is 0 (market resolved or inactive)
        if yes_p <= 0:
            continue

        # Fetch liquidity from order book
        try:
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={yes_token}",
                timeout=6
            ).json()
            bid_liq = sum(float(b.get("size",0)) * float(b.get("price",0))
                          for b in book.get("bids",[])[:10])
            ask_liq = sum(float(a.get("size",0)) * float(a.get("price",0))
                          for a in book.get("asks",[])[:10])
            liq = round(bid_liq + ask_liq, 0)
        except Exception:
            liq = 10000  # assume liquid if book fetch fails

        candidates.append({
            "question":       label,
            "yes_p":          yes_p,
            "no_p":           1 - yes_p,
            "liquidity":      liq,
            "volume24h":      0,
            "conditionId":    wm_cid,
            "clob_token_ids": [yes_token, no_token] if no_token else [yes_token],
            "endDate":        "",
            "description":    f"Priority watchlist: {label}. 0% fee geopolitical market.",
            "priority":       True,
        })

    return sorted(candidates, key=lambda x: (not x.get("priority",False), -x["volume24h"]))[:30]

def load_claude_md():
    """Load CLAUDE.md + lessons.md + HARD_RULES.md for full trading context."""
    parts = []
    agent_dir = "/opt/polymarket-agent"
    for fname, cap in [("HARD_RULES.md", 2000), ("CLAUDE.md", 1500), ("lessons.md", 1200)]:
        fpath = os.path.join(agent_dir, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    text = f.read()[:cap]
                parts.append(f"--- {fname} ---\n{text}")
            except: pass
    return "\n\n".join(parts) if parts else ""


# Cache CLAUDE.md at module load time (read once per scan run)
_CLAUDE_MD_CONTEXT = load_claude_md()



BULL_BEAR_THRESHOLD = 50   # Run full bull/bear debate on trades >= this size
CONSENSUS_THRESHOLD = 30   # Only run consensus vote on trades >= this size

BULL_BEAR_THRESHOLD = 50  # Only run full bull/bear debate on trades >= this size

# ── Token usage tracking (scanner) ───────────────────────────────────────────
_HAIKU_IN_PRICE  = 0.80 / 1_000_000
_HAIKU_OUT_PRICE = 4.00 / 1_000_000
_SC_TOK: dict = {"in": 0, "out": 0, "calls": 0}

def _sc_track(resp_json: dict):
    """Track usage from raw Anthropic HTTP response JSON."""
    u = resp_json.get("usage", {})
    _SC_TOK["in"]    += u.get("input_tokens", 0)
    _SC_TOK["out"]   += u.get("output_tokens", 0)
    _SC_TOK["calls"] += 1

def _sc_cost() -> float:
    return _SC_TOK["in"] * _HAIKU_IN_PRICE + _SC_TOK["out"] * _HAIKU_OUT_PRICE

def _sc_summary() -> str:
    runs_per_day = 6  # after schedule optimization
    cost = _sc_cost()
    return (f"[TOKENS] {_SC_TOK['calls']} Haiku calls | "
            f"in={_SC_TOK['in']:,} out={_SC_TOK['out']:,} | "
            f"est ${cost:.4f}/run | daily@{runs_per_day}x ~${cost*runs_per_day:.3f}/day")

def claude_call(prompt, max_tokens=300):
    """Raw Claude Haiku call. Returns text or empty string on error."""
    if not ANTHROPIC_KEY:
        return ""
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        if resp.status_code == 200:
            _sc_track(resp.json())
            return resp.json().get("content", [{}])[0].get("text", "")
    except:
        pass
    return ""

def bull_bear_debate(question, yes_p, description, news_snippets, uw_summary=""):
    """
    3-step adversarial deliberation before a trade.
    
    Step 1: Bull agent — argue the strongest case FOR buying this side
    Step 2: Bear agent — argue the strongest case AGAINST, poke holes in bull
    Step 3: Risk manager — reads both, makes final BUY/PASS decision with sizing
    
    Returns (action, edge, reasoning) same interface as score_with_claude.
    Falls back to score_with_claude on any error.
    """
    import concurrent.futures

    uw_note = f"\nUW SIGNAL: {uw_summary}" if uw_summary else ""
    market_ctx = f"""MARKET: {question}
YES PRICE: {yes_p:.2f} ({yes_p*100:.0f}% probability)
DESCRIPTION: {description[:250]}
NEWS: {news_snippets[:500]}{uw_note}"""

    # Step 1 + 2: Bull and Bear argue in parallel
    bull_prompt = f"""You are the BULL analyst at a prediction market trading desk.
Your job: argue the STRONGEST possible case for why this market is MISPRICED and we should trade it.
Find the best edge. Be aggressive and specific.

{market_ctx}

Give your bull case in 2-3 sentences. Focus on: why the market is wrong, what the crowd is missing, what the true probability is."""

    bear_prompt = f"""You are the BEAR analyst at a prediction market trading desk.
Your job: argue the STRONGEST possible case for why we should NOT trade this market.
Find every risk, every reason the current price might be correct or we might lose.

{market_ctx}

Give your bear case in 2-3 sentences. Focus on: tail risks, reasons the current price is fair, what could go wrong."""

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_bull = ex.submit(claude_call, bull_prompt, 200)
        f_bear = ex.submit(claude_call, bear_prompt, 200)

    bull_case = f_bull.result()
    bear_case = f_bear.result()

    if not bull_case or not bear_case:
        log("  [Bull/Bear] Fallback to single Claude (API error)")
        return score_with_claude(question, yes_p, description, news_snippets, uw_summary)

    log(f"  [Bull] {bull_case[:100]}")
    log(f"  [Bear] {bear_case[:100]}")

    # Step 3: Risk manager reads the debate and decides
    risk_prompt = f"""You are the RISK MANAGER at a prediction market trading desk.
Two analysts have debated this market. You make the final call.

{market_ctx}

BULL ANALYST SAYS:
{bull_case}

BEAR ANALYST SAYS:
{bear_case}

Your job: weigh both arguments and decide if there is a genuine edge worth trading.
Be conservative — only approve if the bull case clearly outweighs the bear case.

Respond ONLY with JSON:
{{"true_probability": 0.XX, "action": "BUY_YES"|"BUY_NO"|"PASS", "edge": 0.XX, "reasoning": "one sentence verdict", "bull_wins": true|false}}

Rules:
- action=BUY_YES if true_prob > yes_p+0.12, BUY_NO if true_prob < yes_p-0.12, else PASS
- If bull and bear are roughly equal → PASS (no edge)
- Only trade if the edge is real and the bull case is specific and factual
- For sports markets without UW signal → PASS"""

    verdict_text = claude_call(risk_prompt, 300)
    if not verdict_text:
        log("  [Bull/Bear] Risk manager failed — fallback")
        return score_with_claude(question, yes_p, description, news_snippets, uw_summary)

    try:
        match = re.search(r'\{[^{}]*"action"[^{}]*\}', verdict_text, re.DOTALL)
        if not match:
            match = re.search(r'\{.*?\}', verdict_text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            action    = result.get("action", "PASS")
            edge      = float(result.get("edge", 0))
            reasoning = result.get("reasoning", "")
            bull_wins = result.get("bull_wins", False)
            log(f"  [Risk Manager] {action} (edge={edge:.2f}, bull_wins={bull_wins}): {reasoning[:80]}")
            return action, edge, f"[bull/bear] {reasoning}"
    except Exception as e:
        log(f"  [Bull/Bear] Parse error: {e}")

    return score_with_claude(question, yes_p, description, news_snippets, uw_summary)


def ask_perplexity(prompt):
    """Ask Perplexity sonar-small for a trade signal. Returns (action, edge, reasoning)."""
    if not PERPLEXITY_KEY:
        return None, 0, "no key"
    try:
        resp = requests.post("https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {PERPLEXITY_KEY}", "Content-Type": "application/json"},
            json={"model": "sonar", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15)
        text = resp.json()["choices"][0]["message"]["content"]
        match = re.search(r'\{[^{}]*"action"[^{}]*\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result.get("action","PASS"), float(result.get("edge",0)), result.get("reasoning","")
    except Exception as e:
        pass
    return None, 0, "perplexity err"

def consensus_vote(question, yes_p, description, news_snippets, uw_summary=""):
    """
    Poll Claude + Perplexity in parallel. 
    Returns (action, edge, reasoning, votes_detail).
    
    Rules:
    - Both must agree on same action (BUY_YES or BUY_NO) → use that action
    - Disagreement or either returns PASS → return PASS
    - If Perplexity unavailable → fall back to Claude only
    """
    import concurrent.futures

    # Build a compact version of the Claude prompt for Perplexity too
    uw_section = f"\nUW SIGNAL: {uw_summary}" if uw_summary else ""
    shared_prompt = f"""You are a prediction market analyst. Evaluate this market:

MARKET: {question}
YES PRICE: {yes_p:.2f} ({yes_p*100:.0f}% implied probability)
DESCRIPTION: {description[:300]}
NEWS: {news_snippets[:600]}{uw_section}

Respond ONLY with JSON:
{{"true_probability": 0.XX, "action": "BUY_YES"|"BUY_NO"|"PASS", "edge": 0.XX, "reasoning": "one sentence"}}

Rules: action=BUY_YES if true_prob > yes_p+0.12, BUY_NO if true_prob < yes_p-0.12, else PASS. Be conservative."""

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_claude = ex.submit(score_with_claude, question, yes_p, description, news_snippets, uw_summary)
        f_perp   = ex.submit(ask_perplexity, shared_prompt)

    c_action, c_edge, c_reason = f_claude.result()
    p_action, p_edge, p_reason = f_perp.result()

    votes = {"claude": c_action, "perplexity": p_action}
    log(f"  [Consensus] Claude={c_action}({c_edge:.2f}) | Perplexity={p_action}({p_edge:.2f})")

    # If Perplexity unavailable, fall back to Claude alone
    if p_action is None:
        log("  [Consensus] Perplexity unavailable — using Claude only")
        return c_action, c_edge, c_reason, votes

    # Both agree on a trade action
    if c_action == p_action and c_action != "PASS":
        avg_edge = (c_edge + p_edge) / 2
        log(f"  [Consensus] ✓ AGREEMENT: {c_action} (avg edge {avg_edge:.2f})")
        return c_action, avg_edge, f"[2/2 agree] {c_reason}", votes

    # Disagreement or both PASS
    log(f"  [Consensus] ✗ No agreement — PASS")
    return "PASS", 0, f"[no consensus] claude={c_action} perp={p_action}", votes

def score_with_claude(question, yes_p, description, news_snippets, uw_summary=""):
    """Use Claude to score the edge given fresh news and persistent trading context."""
    if not ANTHROPIC_KEY:
        return "PASS", 0.0, "No API key"
    
    uw_section = f"\nUNUSUAL WHALES SIGNAL:\n{uw_summary}" if uw_summary else ""
    context_section = f"\n\nTRADING CONTEXT (from CLAUDE.md):\n{_CLAUDE_MD_CONTEXT[:1500]}" if _CLAUDE_MD_CONTEXT else ""

    # ── Hormuz proxy injection for oil/conflict markets ───────────────────────
    _q_lower = question.lower()
    _is_oil  = any(x in _q_lower for x in ["crude oil","wti","brent","oil price","$120","$110","$100","$90","hit (high)","hit (low)"])
    _is_conf = any(x in _q_lower for x in ["iran","ceasefire","hormuz","kharg","military op","conflict ends","war ends","nuclear deal"])
    _hormuz_section = ""
    if _is_oil or _is_conf:
        try:
            _h = fetch_hormuz_proxy()
            _hormuz_section = f"\nPOLYMARKET HORMUZ/CONFLICT PROXY SIGNAL:\n{_h['signal_label']}"
            if _is_oil and "high" in _q_lower and _h["hormuz_reopen_prob"] > 0.80:
                _hormuz_section += (
                    f"\nHARD CONSTRAINT: Ceasefire probability is {_h['hormuz_reopen_prob']:.0%}. "
                    f"Hormuz reopening is priced in. Oil supply returning to market. "
                    f"Oil war premium is COLLAPSING. DO NOT assign YES probability > 15% "
                    f"on oil price HIGH targets above current spot."
                )
            elif _is_oil and "high" in _q_lower and _h["hormuz_reopen_prob"] > 0.50:
                _hormuz_section += (
                    f"\nADJUSTMENT: Ceasefire at {_h['hormuz_reopen_prob']:.0%} probability. "
                    f"Reduce oil HIGH target YES probability by ~{int(_h['hormuz_reopen_prob']*30)}% "
                    f"from baseline — war premium partially deflating."
                )
        except Exception as _he:
            pass  # non-fatal
    # ─────────────────────────────────────────────────────────────────────────

    # ── Signal Engine (5 independent signals + empirical Kelly) ─────────────
    _yes_token = ""
    try:
        # Extract token from description context if present (opportunity_scanner passes it)
        import re as _re
        _m = _re.search(r"YES token:\s*([0-9]{20,})", description)
        if _m: _yes_token = _m.group(1)
    except: pass

    # Use the Hormuz signal already fetched if available; otherwise fetch a neutral fallback
    _vel_data = locals().get('_vel', {"tier": "FLAT"})

    _sig = run_signal_engine(
        question       = question,
        yes_p          = yes_p,
        yes_token      = _yes_token,
        condition_id   = "",  # scanner passes raw market dict elsewhere
        description    = description,
        uw_summary     = uw_summary,
        vel_data       = _vel_data,
        available_cash = 300,   # scanner-level Kelly used for guidance only
        min_size       = MIN_TRADE_SIZE,
        max_size       = MAX_TRADE_SIZE,
    )
    _signal_section = f"\n\n{_sig['signal_summary']}"
    # ─────────────────────────────────────────────────────────────────────────

    prompt = f"""You are a Nash Equilibrium Strategist and prediction market analyst. \
You model markets as multi-player strategic games and find mispricings by identifying when the \
current price deviates from the true equilibrium probability.

MARKET: {question}
CURRENT YES PRICE: {yes_p:.2f} (implies {yes_p*100:.0f}% probability)
DESCRIPTION: {description}

RECENT NEWS CONTEXT:
{news_snippets}{_hormuz_section}{uw_section}{context_section}{_signal_section}

ANALYTICAL FRAMEWORK — apply this thinking before scoring:

PLAYERS in this market:
- Retail bettors: follow news headlines, often overreact to recent events
- Whales/insiders: have better information (check UW signal above)
- Market makers: keep price near efficient probability
- Event reality: the actual outcome independent of all beliefs

EQUILIBRIUM CHECK:
1. What is each player's dominant strategy at the current price?
2. Is the current price a Nash Equilibrium (no player can profitably deviate)?
3. If retail is overweighting recent news, the price is above equilibrium → BUY_NO edge
4. If insiders are quietly accumulating (UW insider_trades signal), price is below true value → BUY_YES edge
5. Status quo bias: in conflict markets, the "nothing happens" outcome wins ~75% of the time near-term

Respond with ONLY a JSON object like this:
{{
  "true_probability": 0.XX,
  "action": "BUY_YES" | "BUY_NO" | "PASS",
  "edge": 0.XX,
  "reasoning": "one sentence including which player group is mispricing this"
}}

Rules:
- true_probability = your honest estimate of YES resolving
- edge = abs(true_probability - yes_p)
- action = BUY_YES if true_prob > yes_p + 0.12, BUY_NO if true_prob < yes_p - 0.12, else PASS
- If Unusual Whales flags INSIDER TRADES or CONTRARIAN WHALES, treat as strong signal (+weight to that side)
- For near-term conflict events (ceasefire, forces entering, invasion), default to status quo (NO) unless compelling evidence
- For sports without UW signal, PASS
- Be conservative. Only flag genuine equilibrium deviations backed by data."""
    
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5",
                  "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        resp_data = resp.json()
        if resp.status_code != 200:
            err = resp_data.get("error",{}).get("message","API error")[:60]
            return "PASS", 0, f"API {resp.status_code}: {err}"
        content = resp_data.get("content", [{}])[0].get("text","")
        # Extract JSON — handle both raw JSON and ```json blocks
        match = re.search(r'\{[^{}]*"action"[^{}]*\}', content, re.DOTALL)
        if not match:
            match = re.search(r'\{.*?\}', content, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result.get("action","PASS"), float(result.get("edge",0)), result.get("reasoning","")
        return "PASS", 0, f"no json: {content[:80]}"
    except Exception as e:
        return "PASS", 0, f"err: {str(e)[:60]}"

def fetch_news_snippets(question):
    """Fetch relevant RSS headlines for the market question."""
    try:
        # Use a broad query from the question
        keywords = " ".join(question.split()[:6])
        r = requests.get(
            f"https://news.google.com/rss/search?q={keywords.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        
        # Parse RSS simple
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        snippets = []
        for item in items[:5]:
            title = item.findtext("title","")
            pub = item.findtext("pubDate","")[:16]
            snippets.append(f"- [{pub}] {title}")
        return "\n".join(snippets) if snippets else "No recent news found"
    except Exception as e:
        return f"News fetch error: {e}"

def get_clob_price(token_id):
    """Get actual CLOB mid price."""
    try:
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        return float(r.json().get("mid", 0))
    except:
        return 0

def has_real_liquidity(token_id, side="YES", min_depth=2000):
    """
    Check if the order book has REAL depth at tradeable prices.
    Ghost order books (bids at 0.001, asks at 0.999) pass the gamma API
    liquidity check but are untradeable.
    
    Requirements:
    1. At least 3 orders in the 10¢–90¢ price range (not just extreme walls)
    2. Total depth >= min_depth USDC in that range
    3. Bid-ask spread in the tradeable zone < 30¢ (not a 0.001/0.999 ghost book)
    
    Lesson: Backpack FDV NO had $36k 'liquidity' on gamma but bids only at
    0.001–0.05 and asks only at 0.95–0.999. Nothing tradeable in the middle.
    """
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        # Filter to the tradeable zone (10¢–90¢)
        real_bids = [b for b in bids if 0.10 <= float(b["price"]) <= 0.90]
        real_asks = [a for a in asks if 0.10 <= float(a["price"]) <= 0.90]
        
        # Must have orders on BOTH sides in the tradeable zone
        if len(real_bids) < 2 or len(real_asks) < 2:
            return False
        
        # Spread between best bid and best ask in tradeable zone must be reasonable
        best_bid = max(float(b["price"]) for b in real_bids)
        best_ask = min(float(a["price"]) for a in real_asks)
        spread = best_ask - best_bid
        if spread > 0.30:  # >30¢ spread = no real market maker activity
            return False
        
        # Total depth check
        if side == "YES":
            depth = sum(float(a["size"]) * float(a["price"]) for a in real_asks)
        else:
            depth = sum(float(b["size"]) * float(b["price"]) for b in real_bids)
        
        return depth >= min_depth
    except:
        return False

def place_trade(client, token_id, side, size, neg_risk, tick, yes_price):
    """Place a market order."""
    from py_clob_client.clob_types import (OrderArgs, OrderType,
        PartialCreateOrderOptions, BalanceAllowanceParams, AssetType)
    from py_clob_client.order_builder.constants import BUY, SELL
    
    try:
        # Approve conditional token
        try:
            client.update_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        except: pass
        
        # Get current price
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        mid = float(r.json().get("mid", yes_price))
        
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price = round(round(mid / tick_f) * tick_f, tick_dec)
        price = max(0.02, min(0.98, price))
        
        # Size in shares = USDC / price
        shares = math.floor((size / price) * 100) / 100
        if shares < 1:
            return False, "Too few shares"
        
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get("success"):
            return True, f"{shares:.1f} shares @ {price:.3f} = ${shares*price:.2f}"
        else:
            return False, receipt.get("errorMsg","unknown")
    except Exception as e:
        return False, str(e)[:100]

def main():
    log("=== Opportunity Scanner Starting ===")

    # ── Fetch Hormuz proxy signal (ceasefire market prices) ──────────────────
    _hormuz = fetch_hormuz_proxy()
    log(f"[HORMUZ] {_hormuz['signal_label'].splitlines()[0]}")

    
    # Get cash
    try:
        usdc, client = get_usdc_balance()
    except Exception as e:
        log(f"Failed to get balance: {e}")
        tg(f"⚠️ <b>Scan failed</b>: balance check error — {e}")
        return
    
    log(f"Available USDC: ${usdc:.2f}")
    
    if usdc < MIN_TRADE_SIZE:
        log(f"Insufficient cash (${usdc:.2f}) — skipping scan")
        return
    
    # Load UW whale/insider signals
    uw_signals = get_uw_signals()
    
    # Get existing positions
    existing_conditions = get_existing_positions()
    log(f"Existing positions: {len(existing_conditions)}")
    
    # Get candidates
    candidates = get_candidate_markets()
    log(f"Scanning {len(candidates)} candidate markets...")
    
    # Skip markets we're already in
    candidates = [c for c in candidates if c["conditionId"] not in existing_conditions]
    log(f"After filtering existing: {len(candidates)} candidates")
    
    trades_placed = []
    skipped = []
    cash_remaining = usdc
    
    for mkt in candidates[:20]:
        q = mkt["question"]
        yes_p = mkt["yes_p"]
        
        # ════════════════════════════════════════════════════════════════
        # TIER 3 — EYES: Rule-based pre-filter (no LLM, no API calls)
        # Inspired by 3-tier model routing: cheap checks first, expensive last.
        # Skip obvious non-opportunities instantly, saving ~40% of Claude calls.
        # ════════════════════════════════════════════════════════════════
        
        _pre_pass = True
        _pre_reason = ""
        
        # Rule 0: Market-level blacklist — never trade again (learned from mistakes)
        _cond_id = mkt.get("conditionId", "")
        if _cond_id in BLACKLISTED_CONDITIONS:
            _pre_pass = False
            reason_txt = _SCANNER_CONFIG.get("BLACKLISTED_CONDITION_IDS", {}).get(_cond_id, "blacklisted market")
            _pre_reason = f"BLACKLISTED: {reason_txt[:60]}"
        
        # Rule 1: Coin-flip markets with no signal — skip (0.42–0.58 yes_p range)
        # These require genuine insight to trade; without UW signal they're noise.
        if 0.42 <= yes_p <= 0.58:
            # Only proceed if UW already has signal OR it's a high-priority category
            _has_preload_signal = any(
                t in (mkt.get("clob_token_ids") or []) 
                for t in (uw_signals or {})
            )
            if not _has_preload_signal:
                _pre_pass = False
                _pre_reason = f"coin-flip ({yes_p:.2f}) no signal"
        
        # Rule 2: Near-certain markets (>92¢ or <8¢) — skip unless in priority watchlist
        # Priority markets (0%-fee geopolitical NO plays) bypass this gate.
        _is_priority_mkt = mkt.get("priority", False) or any(
            (w.get("condition_id") and w["condition_id"] == mkt.get("conditionId","")) or
            (w.get("label_match") and w["label_match"] in q.lower())
            for w in PRIORITY_WATCHLIST
        )
        if not _is_priority_mkt and (yes_p > 0.93 or yes_p < 0.07):
            _pre_pass = False
            _pre_reason = f"near-certain ({yes_p:.2f}) — no edge"
        
        # Rule 2.5: Commodity price reality check — don't buy NO if commodity already hit target
        _skip_comm, _comm_reason = check_commodity_reality(q, yes_p)
        if _skip_comm:
            _pre_pass = False
            _pre_reason = _comm_reason
        
        # Rule 3: Short-duration YES on conflict/event markets — blocked (earlier rule)
        # Already applied below, but catch it early to avoid news fetch
        _end_pre = mkt.get("endDate", "")
        _days_pre = 999
        if _end_pre:
            try:
                _edt = datetime.datetime.fromisoformat(_end_pre.replace("Z",""))
                _days_pre = (_edt - datetime.datetime.utcnow()).days
            except: pass
        
        _q_pre = q.lower()
        _conflict_keywords = [
            "ceasefire", "forces enter", "regime fall", "conflict ends",
            "military operations", "invasion", "invade", "war ends", "peace deal",
            "strikes end", "bombing", "kharg", "hormuz", "nuclear deal",
            "attack iran", "bomb iran", "invades iran", "us invade"
        ]
        _is_conflict_pre = any(x in _q_pre for x in _conflict_keywords)
        if _days_pre < 30 and _is_conflict_pre and yes_p > 0.15:
            # YES side of a near-term conflict market — almost always loses
            # Exception: only allow if price is very low (<15¢) — cheap lottery  
            _pre_pass = False
            _pre_reason = f"short-duration conflict ({_days_pre}d) at {yes_p:.2f}"
        
        # Rule 4: Sports markets without strong data signal — skip in off-hours
        import datetime as _dt2
        _hour_utc = _dt2.datetime.utcnow().hour
        _is_sports_pre = any(x in _q_pre for x in [
            "vs.", "vs ", "match", "game", "championship", "tournament",
            "playoff", "cup", "league", "bowl", "series"
        ])
        # uw_sig is not yet loaded at pre-filter stage — check uw_signals dict directly
        _has_uw_pre = any(t in uw_signals for t in mkt.get("clob_token_ids", []))
        if _is_sports_pre and not _has_uw_pre:
            # Sports markets without whale flow are coin-flips for us
            _pre_pass = False
            _pre_reason = "sports market, no UW signal"
        
        if not _pre_pass:
            log(f"  [T3-SKIP] {q[:50]} — {_pre_reason}")
            skipped.append({"q": q[:50], "action": "PASS", "edge": 0, 
                           "skip_reason": f"pre-filter: {_pre_reason}"})
            continue
        # ════════════════════════════════════════════════════════════════
        # End Tier 3 pre-filter
        # ════════════════════════════════════════════════════════════════
        
        # Get UW signal
        uw_sig, uw_action_hint = get_uw_signal_for_market(mkt, uw_signals)
        uw_summary_text = uw_signal_summary(uw_sig)
        uw_tags = uw_sig.get("tags", []) if uw_sig else []
        has_insider = any(t in uw_tags for t in ["insider_trades", "contrarian_whales", "whale_position"])
        if uw_sig:
            log(f"  [UW] {q[:40]}: tags={uw_tags[:3]} vol=${uw_sig.get('smart_volume',0):,.0f}")
        
        # ── Price Velocity Check — bypass cache for fast-moving markets ────────
        _vel_token = (mkt.get("tokens") or [{}])[0].get("token_id", "") if mkt.get("tokens") else ""
        if not _vel_token:
            # Try clobTokenIds
            try:
                _vel_tokens = json.loads(mkt.get("clobTokenIds","[]") or "[]")
                _vel_token = _vel_tokens[0] if _vel_tokens else ""
            except:
                _vel_token = ""
        _vel = fetch_price_velocity(_vel_token) if _vel_token else {"tier": "FLAT", "label": ""}

        if _vel["tier"] == "VERY_HOT":
            log(f"  {_vel['label']} — 25%+ spike, 86% historical accuracy", Fore.RED)
            _cached_score = None  # always bypass, always bull/bear
            # Record for forward-test validation
            _vel_dir = _vel.get("direction", "FLAT")
            if _vel_dir in ("UP","DOWN"):
                record_very_hot_signal(
                    question     = q,
                    token_id     = _vel_token,
                    yes_p        = yes_p,
                    direction    = _vel_dir,
                    delta_1h     = _vel.get("delta_1h", 0),
                    condition_id = mkt.get("conditionId",""),
                )
        elif _vel["tier"] in ("HOT", "WARM"):
            log(f"  {_vel['label']}", Fore.YELLOW if _vel['tier'] == 'WARM' else Fore.RED)
            # Force cache bypass — this market needs fresh eyes
            _cached_score = None
        elif _vel["tier"] == "COOL":
            log(f"  {_vel['label']}")
            _cached_score = _scan_cache_get(mkt)  # still use cache if fresh
        else:
            _cached_score = _scan_cache_get(mkt)

        # ── OPT-1: Scan score cache — skip re-scoring if price unchanged ────
        if _cached_score is not None:
            action, edge, reasoning = _cached_score
            log(f"  [SCAN-CACHE] {q[:50]} — reusing {action} edge={edge:.2f}")
        else:
            # Get news
            snippets = fetch_news_snippets(q)
            # Prepend velocity signal to snippets so Claude sees it prominently
            if _vel["tier"] in ("HOT", "WARM") and _vel["label"]:
                snippets = f"MARKET VELOCITY ALERT: {_vel['label']}\n"                            f"This market is moving fast — weight recency heavily.\n\n" + snippets

            # ── OPT-2: Fast-reject gate — 1 cheap Haiku call before bull/bear ──
            # If there's clearly no edge, bail before spending 3 calls on debate.
            # Only gate when projected size would hit bull/bear path AND no UW signal.
            # ── Empirical Kelly sizing (signal engine) ──────────────────────
            projected_size = min(MAX_TRADE_SIZE, cash_remaining * 0.3)
            projected_size = max(MIN_TRADE_SIZE, projected_size)

            # Run signal engine for sizing guidance BEFORE gate/scoring
            _yes_token_for_kelly = ""
            try:
                _tok = json.loads(mkt.get("clobTokenIds","[]") or "[]")
                _yes_token_for_kelly = _tok[0] if _tok else ""
            except:
                pass
            _sig_engine = run_signal_engine(
                question       = q,
                yes_p          = yes_p,
                yes_token      = _yes_token_for_kelly,
                condition_id   = mkt.get("conditionId", ""),
                description    = mkt["description"],
                uw_summary     = uw_summary_text,
                vel_data       = _vel if '_vel' in locals() else {"tier": "FLAT"},
                available_cash = cash_remaining,
                min_size       = MIN_TRADE_SIZE,
                max_size       = MAX_TRADE_SIZE,
            )
            _kelly_size = _sig_engine["kelly_size"]
            if _kelly_size >= MIN_TRADE_SIZE:
                log(f"  [KELLY] {q[:45]} → ${_kelly_size:.0f} ({_sig_engine['kelly']['reason']})")
                projected_size = _kelly_size
            _skip_to_pass = False
            if projected_size >= BULL_BEAR_THRESHOLD and not has_insider:
                _gate_prompt = (
                    f"Prediction market: '{q}'\n"
                    f"YES price: {yes_p:.2f} | Description: {mkt['description'][:200]}\n"
                    f"News: {snippets[:300]}\n"
                    f"Signal engine: {_sig_engine['signal_summary'][:250]}\n\n"
                    "Is there ANY genuine mispricing edge here (≥15pp) that a well-informed trader would act on?\n"
                    "Respond with ONLY: YES or NO. Do not explain."
                )
                _gate_answer = claude_call(_gate_prompt, max_tokens=5).strip().upper()
                log(f"  [GATE] {q[:45]} → {_gate_answer}")
                if _gate_answer == "NO" or "NO" in _gate_answer:
                    if _is_priority_mkt:
                        # Priority market: gate says NO = GOOD for us (NO position)
                        # Don't skip — proceed to full bull/bear scoring
                        log(f"  [PRIORITY] {q[:45]} — gate→NO, proceeding to full score")
                        _skip_to_pass = False
                    else:
                        _skip_to_pass = True
                        action, edge, reasoning = "PASS", 0.0, "fast-reject gate: no edge signal"

            if not _skip_to_pass:
                # Tiered scoring:
                #   HOT velocity OR >= $50 + UW: full bull/bear debate (3 Claude calls)
                #   WARM velocity OR >= $30: consensus vote (Claude + Perplexity in parallel)
                #   <  $30: single Claude call
                _force_bull_bear  = _vel["tier"] in ("VERY_HOT", "HOT")
                _force_consensus  = _vel["tier"] == "WARM"
                if _force_bull_bear or projected_size >= BULL_BEAR_THRESHOLD:
                    if _force_bull_bear:
                        log(f"  [VELOCITY] HOT market → forcing full bull/bear debate", Fore.RED)
                    action, edge, reasoning = bull_bear_debate(q, yes_p, mkt["description"], snippets, uw_summary_text)
                elif (_force_consensus or projected_size >= CONSENSUS_THRESHOLD) and PERPLEXITY_KEY:
                    if _force_consensus:
                        log(f"  [VELOCITY] WARM market → forcing consensus vote", Fore.YELLOW)
                    action, edge, reasoning, _votes = consensus_vote(q, yes_p, mkt["description"], snippets, uw_summary_text)
                else:
                    action, edge, reasoning = score_with_claude(q, yes_p, mkt["description"], snippets, uw_summary_text)

            # Cache the result regardless of outcome
            _scan_cache_set(mkt, action, edge, reasoning)
        
        # Lower threshold when UW insider/whale signal present
        effective_threshold = MIN_SCAN_EDGE * (1 - UW_EDGE_DISCOUNT) if has_insider else MIN_SCAN_EDGE
        
        # UW override: if Claude PASSed but UW has strong signal and near-miss edge
        if action == "PASS" and uw_action_hint and has_insider and edge >= MIN_SCAN_EDGE * 0.5:
            log(f"  [UW override] Claude PASS → using UW hint {uw_action_hint}")
            action = uw_action_hint
            edge = max(edge, effective_threshold)
        
        # ── Risk guardrail: no BUY_YES on short-duration conflict/event markets ──
        # Lesson: near-term YES bets on Iran/ceasefire/forces events bleed money.
        # Status quo bias: "nothing happens by deadline" wins far more than "something happens."
        # Only allow BUY_YES if: duration > 30 days OR market is not an event/deadline type.
        end_date = mkt.get("endDate", "")
        days_left = 999
        if end_date:
            try:
                end_dt = datetime.datetime.fromisoformat(end_date.replace("Z",""))
                days_left = (end_dt - datetime.datetime.utcnow()).days
            except: pass
        
        q_lower_check = q.lower()
        is_event_market = any(x in q_lower_check for x in [
            "ceasefire", "forces enter", "regime fall", "conflict ends",
            "military operations", "invasion", "invade", "war ends", "peace deal",
            "strikes end", "bombing", "kharg", "hormuz", "attack iran", "bomb iran",
            "us invade", "invades iran", "occupy iran"
        ])
        
        if action == "BUY_YES" and is_event_market and days_left < 30:
            log(f"  SKIP (short-duration YES on event market, {days_left}d left): {q[:45]}")
            skipped.append({"q": q[:50], "action": action, "edge": edge, 
                           "skip_reason": f"event YES <30d ({days_left}d)"})
            continue
        
        log(f"  {q[:50]}: {action} edge={edge:.2f} (thresh={effective_threshold:.2f}) — {reasoning[:55]}")
        
        if action == "PASS" or edge < effective_threshold:
            skipped.append({"q": q[:50], "action": action, "edge": edge})
            continue
        
        # Determine which token to buy
        tokens = mkt["clob_token_ids"]
        if not tokens or len(tokens) < 2:
            log(f"  No token IDs found for {q[:40]}")
            continue
        
        yes_token, no_token = tokens[0], tokens[1]
        buy_token = yes_token if action == "BUY_YES" else no_token
        buy_price = yes_p if action == "BUY_YES" else mkt["no_p"]
        
        # Check real CLOB liquidity
        if not has_real_liquidity(buy_token, min_depth=2000):
            log(f"  Insufficient CLOB depth for {q[:40]} — skip")
            skipped.append({"q": q[:50], "action": action, "edge": edge, "skip_reason": "no_depth"})
            continue
        
        # Get tick size and neg_risk
        try:
            tick = client.get_tick_size(buy_token)
            neg_risk = client.get_neg_risk(buy_token)
        except:
            tick = "0.01"
            neg_risk = False
        
        # Determine trade size
        trade_size = min(MAX_TRADE_SIZE, cash_remaining * 0.3)  # max 30% of remaining cash
        trade_size = max(MIN_TRADE_SIZE, trade_size)
        if trade_size > cash_remaining:
            log(f"  Insufficient cash for {q[:40]}")
            break
        
        # Place the trade
        log(f"  PLACING {action} ${trade_size:.0f} on: {q[:50]}")
        success, detail = place_trade(client, buy_token, action, trade_size,
                                      neg_risk, tick, buy_price)
        
        if success:
            cash_remaining -= trade_size
            trades_placed.append({
                "q": q[:60],
                "action": action,
                "size": trade_size,
                "edge": edge,
                "reasoning": reasoning,
                "detail": detail,
                "uw_signal": bool(uw_sig),
                "uw_tags": uw_tags[:3],
            })
            log(f"  ✓ Trade placed: {detail}")
            time.sleep(2)  # brief pause between trades
        else:
            log(f"  ✗ Trade failed: {detail}")
            skipped.append({"q": q[:50], "action": action, "edge": edge, "skip_reason": detail[:50]})
        
        if cash_remaining < MIN_TRADE_SIZE:
            break
    
    # ════════════════════════════════════════════════════════════════
    # PASS 2 — Near-Resolution Scanner (Type-4 bot, anon-fake style)
    # Finds any active market with an outcome >= 95.5¢ and buys residual.
    # Zero directional risk — pure resolution timing edge.
    # ════════════════════════════════════════════════════════════════
    try:
        import near_resolution_scanner as _nr
        nr_candidates = _nr.scan_near_resolution_markets()
        log(f"[NearRes] {len(nr_candidates)} candidate(s) found")
        _nr.load_state()
        _nr_spent = 0
        for _nrc in nr_candidates[:5]:  # max 5 per scanner run
            if cash_remaining - _nr_spent < _nr.MIN_POSITION_USDC:
                break
            if (_nrc["conditionId"], _nrc["outcome"]) in [(p["conditionId"], p["outcome"])
                    for p in [{"conditionId": s, "outcome": "?"} for s in existing_conditions]]:
                continue
            _confirmed, _live_p, _reason = _nr.confirm_near_resolution(_nrc)
            if not _confirmed:
                log(f"  [NearRes] SKIP: {_reason}")
                continue
            _nrc["price"] = _live_p
            _filled, _spent_nr = _nr.place_near_res_trade(
                _nrc, cash_remaining - _nr_spent, client
            )
            if _filled:
                _nr_spent += _spent_nr
                _nr._state[f"{_nrc['conditionId']}:{_nrc['outcome']}"] = {
                    "ts": time.time(), "question": _nrc["question"][:60],
                    "price": _live_p, "spent": _spent_nr,
                }
                _nr.save_state()
                trades_placed.append({
                    "q": _nrc["question"][:60],
                    "action": f"BUY_{_nrc['outcome']} [NEAR-RES]",
                    "size": _spent_nr,
                    "edge": _nrc["residual"],
                    "reasoning": f"Near-res: {_nrc['outcome']} @ {_live_p:.4f} ({_nrc['residual']*100:.1f}¢ residual)",
                    "detail": "near_resolution",
                    "uw_signal": False,
                    "uw_tags": [],
                })
        if _nr_spent > 0:
            cash_remaining -= _nr_spent
            log(f"  [NearRes] Deployed ${_nr_spent:.2f} across near-resolution trades")
    except Exception as _nr_err:
        log(f"[NearRes] Error: {_nr_err}")

    # ════════════════════════════════════════════════════════════════
    # PASS 3 — Book Imbalance Scanner (Type-2 bot, vague-sourdough style)
    # DISABLED: Backtest (Apr 7 2026, 71K price points, 23 markets) shows
    # momentum REVERSAL at all thresholds (2x–10x). Win rate 22-35%, avg
    # return -0.25% to -2.87%. Polymarket prices efficiently reflect book
    # imbalances — no exploitable momentum edge on geopolitical/slow markets.
    # Re-enable only with L2 book snapshot history or 5-min crypto market data.
    # ════════════════════════════════════════════════════════════════
    if False:  # DISABLED — see backtest results above
     try:
        import book_imbalance_scanner as _bi
        bi_candidates = _bi.scan_only()
        log(f"[BookImb] {len(bi_candidates)} imbalanced market(s) found")
        _bi.load_state()
        _bi_spent = 0
        for _bic in bi_candidates[:3]:  # max 3 per scanner run
            if cash_remaining - _bi_spent < _bi.MIN_TRADE_SIZE:
                break
            _confirmed, _reason = _bi.claude_confirm_imbalance(
                _bic["question"], _bic["thin_side"],
                _bic["ratio"], _bic["bid_depth"], _bic["ask_depth"]
            )
            log(f"  [BookImb] {'✅' if _confirmed else '❌'} {_bic['question'][:45]} — {_reason}")
            if not _confirmed:
                continue
            _filled, _spent_bi = _bi.place_imbalance_trade(
                _bic, cash_remaining - _bi_spent, client
            )
            if _filled:
                _bi_spent += _spent_bi
                _cooldown_key = f"{_bic['conditionId']}:{_bic['action']}"
                _bi._state[_cooldown_key] = {
                    "ts": time.time(), "question": _bic["question"][:60],
                    "ratio": _bic["ratio"], "spent": _spent_bi,
                }
                _bi.save_state()
                trades_placed.append({
                    "q": _bic["question"][:60],
                    "action": f"{_bic['action']} [BOOK-IMB]",
                    "size": _spent_bi,
                    "edge": min(abs(_bic["ratio"] - 1) * 0.1, 0.30),
                    "reasoning": f"Book imbalance {_bic['ratio']:.1f}x — {_reason}",
                    "detail": "book_imbalance",
                    "uw_signal": False,
                    "uw_tags": [],
                })
        if _bi_spent > 0:
            cash_remaining -= _bi_spent
            log(f"  [BookImb] Deployed ${_bi_spent:.2f} across imbalance trades")
     except Exception as _bi_err:
        log(f"[BookImb] Error: {_bi_err}")

        # Build Telegram summary
    now = datetime.datetime.utcnow().strftime("%H:%M UTC")
    
    if trades_placed:
        trade_lines = "\n".join(
            f"• <b>{t['action']}</b> ${t['size']:.0f} | edge={t['edge']:.2f} | {t['q'][:45]}\n"
            f"  ↳ {t['reasoning'][:70]}"
            + (f"\n  🐋 UW: {','.join(t.get('uw_tags', []))[:40]}" if t.get('uw_signal') else "")
            for t in trades_placed
        )
        msg = (f"🤖 <b>Opportunity Scanner</b> [{now}]\n\n"
               f"💰 Cash deployed: ${sum(t['size'] for t in trades_placed):.0f} in {len(trades_placed)} trade(s)\n\n"
               f"{trade_lines}\n\n"
               f"Remaining cash: ${cash_remaining:.0f}")
        tg(msg)
        log(_sc_summary())
        log(f"Scan complete: {len(trades_placed)} trades placed")
    else:
        # Silent if nothing — don't spam
        log(_sc_summary())
        log(f"Scan complete: no trades. {len(skipped)} markets evaluated.")
        # Only notify if we had near-misses
        near = [s for s in skipped if s.get("edge",0) >= MIN_SCAN_EDGE * 0.7]
        if near:
            near_lines = "\n".join(f"• {s['q'][:45]} — edge {s.get('edge',0)*100:.0f}% ({s.get('skip_reason','no depth')})"
                                   for s in near[:4])
            tg(f"📊 <b>Scanner [{now}]</b>: no trades placed\n"
               f"Near-misses (blocked by depth/size):\n{near_lines}\n"
               f"Cash available: ${cash_remaining:.0f}")

if __name__ == "__main__":
    main()

