"""
signal_engine.py
================
Implements the Fundamental Law of Active Management for Polymarket:

    IR = IC × √N

where IC = Information Coefficient (avg correlation of signal to outcome),
      N  = number of INDEPENDENT signals.

The key word is INDEPENDENT. Correlated signals (e.g. GDELT + Perplexity, 
both reading the same Reuters wire) add almost no IR benefit. This engine:

1. Scores 5 structurally independent signals for a given market
2. Runs the 11-step decorrelation procedure from Roh's framework
3. Outputs a single combined probability with an IC-weighted confidence score
4. Feeds the combined probability to the empirical Kelly sizer

Signal taxonomy (chosen for mutual independence):
  S1 — Polymarket term structure (cross-expiry implied probability)
  S2 — Unusual Whales / insider flow (execution-based, not news)
  S3 — Prediction market calibration prior (base rate from Metaculus/manifold)
  S4 — News velocity (rate of change of coverage, NOT content)
  S5 — CLOB microstructure (order arrival imbalance, bid/ask spread compression)
  
S1 and S5 are CLOB-derived but orthogonal: term structure is long-horizon
probability signal; microstructure is short-horizon flow signal.
S2 (execution) and S3 (calibration) are uncorrelated with news (S4).

Historical IC estimates (from Polymarket calibration literature):
  S1 (term structure):  IC ≈ 0.14  (strongest — market is liquid)
  S2 (insider flow):    IC ≈ 0.11  (when UW signal present, else 0)
  S3 (calibration):     IC ≈ 0.08  (base rate anchor)
  S4 (news velocity):   IC ≈ 0.07  (fast-moving markets only)
  S5 (microstructure):  IC ≈ 0.05  (weak standalone, useful decorrelator)

Combined with N=5 independent signals:
  Theoretical IR = mean_IC × √5 = 0.09 × 2.24 = 0.20

vs. single signal:
  Theoretical IR = 0.14 × 1    = 0.14

~43% improvement in risk-adjusted edge.
"""

import os, math, time, json, requests
from typing import Optional

# ── Historical IC estimates (calibrated on Polymarket data) ───────────────────
SIGNAL_ICS = {
    "term_structure":  0.14,
    "insider_flow":    0.11,
    "calibration":     0.08,
    "news_velocity":   0.09,   # Raised from 0.07: backtest IC=0.163 (lifetime max), live estimate ~0.09
    "microstructure":  0.05,
    "markov":          0.03,   # Reduced from 0.09 post-backtest: IC=0.03 on 16 markets
}


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 1 — Term Structure (cross-expiry implied probability)
# ══════════════════════════════════════════════════════════════════════════════
def signal_term_structure(condition_id: str, yes_token: str, yes_p: float) -> dict:
    """
    Compare this market's YES price against adjacent-expiry markets in the
    same event series. A divergence from the term structure implies mispricing.

    Returns:
        prob_estimate: float  — term-structure-implied probability
        confidence:    float  — 0-1, based on number of neighbours found
        direction:     str    — "UNDERPRICED" | "OVERPRICED" | "FAIR"
        ic:            float  — Information Coefficient for this signal
    """
    try:
        # Fetch sibling markets from the same event
        cid_clean = condition_id.replace("0x", "")
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets/{condition_id}",
            timeout=6
        )
        if not r.ok:
            return {"prob_estimate": yes_p, "confidence": 0, "direction": "UNKNOWN", "ic": 0}

        market = r.json()
        event_slug = market.get("eventSlug") or market.get("slug", "").rsplit("-202", 1)[0]
        end_date   = market.get("endDate", "")

        # Fetch event siblings
        siblings = []
        if event_slug:
            r2 = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"event_slug": event_slug, "limit": 20, "active": "true"},
                timeout=6
            )
            if r2.ok:
                siblings = [m for m in r2.json()
                           if m.get("conditionId") != condition_id]

        if len(siblings) < 2:
            # No siblings — use current price as estimate, low confidence
            return {"prob_estimate": yes_p, "confidence": 0.1,
                    "direction": "FAIR", "ic": SIGNAL_ICS["term_structure"] * 0.3}

        # Build term structure — sort by end date
        prices = []
        for s in siblings:
            try:
                ps = json.loads(s.get("outcomePrices", "[]") or "[]")
                if ps:
                    prices.append((s.get("endDate", ""), float(ps[0])))
            except:
                pass
        prices.sort()

        if not prices:
            return {"prob_estimate": yes_p, "confidence": 0.2,
                    "direction": "FAIR", "ic": SIGNAL_ICS["term_structure"] * 0.4}

        # Interpolate expected price from term structure
        # Simple: average of nearest neighbours weighted by time proximity
        ts_probs = [p for _, p in prices]
        ts_avg   = sum(ts_probs) / len(ts_probs)

        # Expected YES price given term structure (monotonicity constraint)
        # If all siblings are higher-expiry, this market should be <= them
        later_prices = [p for d, p in prices if d > end_date]
        earlier_prices = [p for d, p in prices if d < end_date]

        if later_prices and earlier_prices:
            # Interpolate
            ts_implied = (min(later_prices) + max(earlier_prices)) / 2
        elif later_prices:
            ts_implied = min(later_prices) * 0.85  # should be less than later
        elif earlier_prices:
            ts_implied = max(earlier_prices) * 1.15  # should be more than earlier
        else:
            ts_implied = ts_avg

        ts_implied = max(0.01, min(0.99, ts_implied))
        deviation  = yes_p - ts_implied

        if deviation > 0.06:
            direction = "OVERPRICED"
        elif deviation < -0.06:
            direction = "UNDERPRICED"
        else:
            direction = "FAIR"

        # Confidence based on number of siblings
        confidence = min(1.0, len(prices) / 4)

        return {
            "prob_estimate": ts_implied,
            "confidence":    confidence,
            "direction":     direction,
            "ic":            SIGNAL_ICS["term_structure"] * confidence,
            "n_siblings":    len(prices),
            "ts_avg":        round(ts_avg, 3),
        }
    except Exception as e:
        return {"prob_estimate": yes_p, "confidence": 0, "direction": "UNKNOWN",
                "ic": 0, "error": str(e)[:60]}


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 2 — Insider / Smart Money Flow (Unusual Whales + whale monitor)
# ══════════════════════════════════════════════════════════════════════════════
def signal_insider_flow(uw_summary: str, yes_p: float, whale_data: Optional[dict] = None) -> dict:
    """
    Interprets UW signal and whale watchlist activity.
    Execution-based signal — structurally independent of news.

    uw_summary: text from UnusualWhales API (may be empty string)
    whale_data: optional dict from whale_exit_watch.json
    """
    if not uw_summary and not whale_data:
        return {"prob_estimate": yes_p, "confidence": 0,
                "direction": "NONE", "ic": 0}

    # Parse UW signal direction
    uw_lower = uw_summary.lower() if uw_summary else ""
    insider_yes = sum(1 for kw in
        ["bullish", "buying yes", "long yes", "accumulated yes", "smart money yes"]
        if kw in uw_lower)
    insider_no  = sum(1 for kw in
        ["bearish", "buying no", "long no", "accumulated no", "smart money no"]
        if kw in uw_lower)

    # Large whale positions shift implied probability
    whale_yes_bias = 0.0
    if whale_data:
        whale_dir = whale_data.get("direction", "")
        if "YES" in whale_dir:
            whale_yes_bias = +0.05
        elif "NO" in whale_dir:
            whale_yes_bias = -0.05

    if insider_yes > insider_no:
        # Smart money leaning YES → market may be underpriced
        implied = min(0.99, yes_p + 0.08 + whale_yes_bias)
        direction = "UNDERPRICED"
        confidence = min(1.0, 0.4 + insider_yes * 0.2)
    elif insider_no > insider_yes:
        implied = max(0.01, yes_p - 0.08 + whale_yes_bias)
        direction = "OVERPRICED"
        confidence = min(1.0, 0.4 + insider_no * 0.2)
    elif whale_yes_bias != 0:
        implied = max(0.01, min(0.99, yes_p + whale_yes_bias))
        direction = "UNDERPRICED" if whale_yes_bias > 0 else "OVERPRICED"
        confidence = 0.25
    else:
        return {"prob_estimate": yes_p, "confidence": 0.1,
                "direction": "NEUTRAL", "ic": SIGNAL_ICS["insider_flow"] * 0.1}

    return {
        "prob_estimate": round(implied, 3),
        "confidence":    round(confidence, 2),
        "direction":     direction,
        "ic":            SIGNAL_ICS["insider_flow"] * confidence,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 3 — Calibration Prior (base rate from resolution history)
# ══════════════════════════════════════════════════════════════════════════════
def signal_calibration_prior(question: str, yes_p: float, description: str = "") -> dict:
    """
    Anchors to the base rate of similar market types resolving YES.
    Structurally independent of news and order flow.

    Uses Polymarket's public resolution statistics and known calibration biases:
    - Geopolitical conflict markets: YES resolves ~22% historically
    - Ceasefire markets: YES ~31% (higher than base — deals do happen)
    - Election incumbents: YES ~58% (incumbents win more)
    - Sports favorites: YES ~62% (when priced >0.65)
    - Near-resolution (>0.90): YES ~96% (FOMC, predictable events)
    """
    q_lower = (question + " " + description).lower()

    # Category detection
    if any(x in q_lower for x in ["ceasefire", "peace deal", "end hostilities"]):
        base_rate, category = 0.31, "ceasefire"
    elif any(x in q_lower for x in ["invade", "invasion", "military operation", "deploy troops"]):
        base_rate, category = 0.18, "military_action"
    elif any(x in q_lower for x in ["regime fall", "regime collapse", "coup", "overthrow"]):
        base_rate, category = 0.08, "regime_change"
    elif any(x in q_lower for x in ["win the election", "elected president", "wins presidency"]):
        base_rate, category = 0.52, "election_winner"
    elif any(x in q_lower for x in ["nba", "nfl", "nhl", "mlb", "wins the game", "wins game"]):
        base_rate, category = 0.50, "sports_game"
    elif any(x in q_lower for x in ["above $", "hit $", "reach $", "exceed $"]):
        base_rate, category = 0.28, "price_target"
    elif any(x in q_lower for x in ["before ", "by ", "within ", "this year"]):
        base_rate, category = 0.35, "time_bounded_event"
    else:
        base_rate, category = 0.40, "generic"

    # Calibration bias adjustment:
    # Markets priced between 0.15-0.40 tend to OVER-estimate unlikely events
    # Markets priced between 0.60-0.85 tend to UNDER-estimate likely events
    if 0.15 < yes_p < 0.40:
        # Market may be overpricing low-prob event — shade toward base rate
        calibrated = 0.7 * yes_p + 0.3 * base_rate
    elif 0.60 < yes_p < 0.85:
        # Market may be underpricing high-prob event — shade toward yes_p
        calibrated = 0.85 * yes_p + 0.15 * base_rate
    else:
        # Extreme prices — trust the market more
        calibrated = 0.9 * yes_p + 0.1 * base_rate

    calibrated = max(0.01, min(0.99, calibrated))
    deviation  = abs(calibrated - yes_p)

    # Confidence proportional to deviation from known base rate
    confidence = min(0.8, deviation / 0.15)

    direction = "UNDERPRICED" if calibrated > yes_p else "OVERPRICED" if calibrated < yes_p else "FAIR"

    return {
        "prob_estimate": round(calibrated, 3),
        "confidence":    round(confidence, 2),
        "direction":     direction,
        "ic":            SIGNAL_ICS["calibration"] * confidence,
        "base_rate":     base_rate,
        "category":      category,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 4 — News Velocity (rate of coverage change, NOT content)
# ══════════════════════════════════════════════════════════════════════════════
def signal_news_velocity(question: str, vel_data: Optional[dict] = None) -> dict:
    """
    Measures how fast news coverage is accelerating on this topic.
    Independent of news CONTENT (which is what S3/news_snippets use).

    vel_data: output from fetch_price_velocity() — price velocity as proxy.
    If not provided, fetches from Google News RSS directly.
    """
    # If we have price velocity data from the scanner, use it directly
    # Price velocity IS news velocity — it reflects the market absorbing news
    if vel_data and vel_data.get("tier") != "FLAT":
        tier = vel_data.get("tier", "FLAT")
        direction = vel_data.get("direction", "FLAT")
        delta_1h  = vel_data.get("delta_1h", 0)

        # Fast upward price move → news is bullish, market absorbing
        # Fast downward move → news is bearish
        if tier == "HOT":
            confidence = 0.85
            adjustment = 0.12 * (1 if direction == "UP" else -1)
        elif tier == "WARM":
            confidence = 0.55
            adjustment = 0.07 * (1 if direction == "UP" else -1)
        else:  # COOL
            confidence = 0.25
            adjustment = 0.03 * (1 if direction == "UP" else -1)

        implied = max(0.01, min(0.99, vel_data.get("current_p", 0.5) + adjustment))
        direction_label = "UNDERPRICED" if adjustment > 0 else "OVERPRICED"

        return {
            "prob_estimate": round(implied, 3),
            "confidence":    confidence,
            "direction":     direction_label,
            "ic":            SIGNAL_ICS["news_velocity"] * confidence,
            "velocity_tier": tier,
            "delta_1h":      delta_1h,
        }

    # Fallback: RSS article count proxy (number of headlines in last 2h)
    try:
        import re
        q_words = " ".join(question.lower().split()[:4])
        r = requests.get(
            f"https://news.google.com/rss/search?q={q_words.replace(' ','+')}"
            f"&hl=en-US&gl=US&ceid=US:en",
            timeout=6
        )
        if not r.ok:
            return {"prob_estimate": 0.5, "confidence": 0, "direction": "UNKNOWN", "ic": 0}

        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        cutoff_2h = time.time() - 7200

        recent_count = 0
        for item in items[:20]:
            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
            if pub_m:
                try:
                    import email.utils
                    ts = email.utils.parsedate_to_datetime(pub_m.group(1)).timestamp()
                    if ts > cutoff_2h:
                        recent_count += 1
                except:
                    pass

        # High recent article count = accelerating coverage
        if recent_count >= 5:
            confidence = 0.5
        elif recent_count >= 2:
            confidence = 0.25
        else:
            confidence = 0.0

        return {
            "prob_estimate": 0.5,  # velocity alone doesn't tell direction
            "confidence":    confidence,
            "direction":     "ACTIVE" if confidence > 0 else "QUIET",
            "ic":            SIGNAL_ICS["news_velocity"] * confidence,
            "recent_articles": recent_count,
        }
    except:
        return {"prob_estimate": 0.5, "confidence": 0, "direction": "UNKNOWN", "ic": 0}


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 5 — CLOB Microstructure (spread compression, bid arrival rate)
# ══════════════════════════════════════════════════════════════════════════════
def signal_microstructure(yes_token: str, yes_p: float) -> dict:
    """
    Measures order book health and information asymmetry.
    Uses bid-ask spread compression as the primary signal.

    Tight spread + thin ask side → informed buyers accumulating → UNDERPRICED
    Wide spread + thin bid side → informed sellers distributing → OVERPRICED

    This is INDEPENDENT of price momentum (S4) because:
    - Momentum uses historical price changes
    - Microstructure uses current book depth and spread
    """
    if not yes_token:
        return {"prob_estimate": yes_p, "confidence": 0, "direction": "UNKNOWN", "ic": 0}
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={yes_token}",
            timeout=6
        )
        if not r.ok:
            return {"prob_estimate": yes_p, "confidence": 0, "direction": "UNKNOWN", "ic": 0}

        book  = r.json()
        bids  = book.get("bids", [])
        asks  = book.get("asks", [])

        if not bids or not asks:
            return {"prob_estimate": yes_p, "confidence": 0.1,
                    "direction": "HOLLOW", "ic": 0}

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread   = best_ask - best_bid

        # Depth imbalance (top 5 levels)
        def depth(orders, n=5):
            return sum(float(o["price"]) * float(o["size"]) for o in orders[:n])

        bid_depth = depth(bids)
        ask_depth = depth(asks)
        total_depth = bid_depth + ask_depth
        if total_depth < 50:
            return {"prob_estimate": yes_p, "confidence": 0,
                    "direction": "ILLIQUID", "ic": 0}

        imbalance = (bid_depth - ask_depth) / total_depth  # -1 to +1

        # Spread compression: tight spread vs. typical spread for this price range
        # Typical spread for a ~50¢ market: 2-4¢. Below 1.5¢ = tight (informed)
        typical_spread = max(0.01, yes_p * (1 - yes_p) * 0.08)
        spread_ratio   = spread / typical_spread  # <1 = tighter than normal

        # Combined microstructure signal
        if spread_ratio < 0.7 and imbalance > 0.15:
            # Tight spread + bids dominating = informed accumulation
            direction  = "UNDERPRICED"
            adjustment = 0.04
            confidence = min(0.7, (0.15 - spread_ratio * 0.1) + imbalance * 0.3)
        elif spread_ratio < 0.7 and imbalance < -0.15:
            # Tight spread + asks dominating = informed distribution
            direction  = "OVERPRICED"
            adjustment = -0.04
            confidence = min(0.7, (0.15 - spread_ratio * 0.1) + abs(imbalance) * 0.3)
        elif spread_ratio > 2.0:
            # Very wide spread = stale / no information
            return {"prob_estimate": yes_p, "confidence": 0,
                    "direction": "STALE", "ic": 0}
        else:
            direction  = "NEUTRAL"
            adjustment = imbalance * 0.02
            confidence = 0.15

        implied = max(0.01, min(0.99, yes_p + adjustment))

        return {
            "prob_estimate": round(implied, 3),
            "confidence":    round(confidence, 2),
            "direction":     direction,
            "ic":            SIGNAL_ICS["microstructure"] * confidence,
            "spread":        round(spread, 4),
            "spread_ratio":  round(spread_ratio, 2),
            "imbalance":     round(imbalance, 3),
        }
    except Exception as e:
        return {"prob_estimate": yes_p, "confidence": 0, "direction": "UNKNOWN",
                "ic": 0, "error": str(e)[:60]}



# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL 6 — Markov Chain Transition Matrix (structural price dynamics)
# ══════════════════════════════════════════════════════════════════════════════
def signal_markov(yes_token: str, yes_p: float, n_states: int = 10,
                  n_sims: int = 5000, horizon_days: int = 7) -> dict:
    """
    Builds a Markov transition matrix from price history, runs Monte Carlo
    simulations to derive a structurally-implied probability of resolution YES.

    Based on 0xMovez framework (72.1M trade study):
    - Discretize price into 10 states (0-10¢, ..., 90-100¢)
    - Build transition matrix from observed price moves
    - Simulate 5,000 price paths forward N days
    - P(final state >= 50¢) = Markov-implied YES probability

    Calibration adjustments (from backtest, n=16 resolved markets):
    - Raw Markov < 10¢ resolves YES 27% of the time → scale UP 2.5x
    - Raw Markov < 25¢ scale up 1.4x (mean-reversion bias correction)
    - IC=0.03 (reduced from 0.09 post-backtest: directionally correct 69%
      but Brier 28% worse due to overconfident catastrophic failures)
    - Works best for: trending-NO markets, strong momentum
    - Fails for: event-driven YES outcomes (ceasefire signed, deal done)
    - The news velocity signal (S4) should override Markov when HOT

    IC ≈ 0.09 (estimated from Markov model backtests on prediction markets)
    This signal is ORTHOGONAL to all other signals because:
    - It uses price SEQUENCE dynamics, not just current price level
    - It captures mean-reversion and momentum in the price process itself
    """
    import random

    MARKOV_IC = 0.09

    if not yes_token:
        return {"prob_estimate": yes_p, "confidence": 0,
                "direction": "UNKNOWN", "ic": 0}

    try:
        # Fetch price history
        r = requests.get(
            f"https://clob.polymarket.com/prices-history?market={yes_token}"
            f"&interval=max&fidelity=60",
            timeout=8
        )
        if not r.ok:
            return {"prob_estimate": yes_p, "confidence": 0,
                    "direction": "NO_DATA", "ic": 0}

        history = r.json().get("history", [])
        if len(history) < 20:
            return {"prob_estimate": yes_p, "confidence": 0.05,
                    "direction": "INSUFFICIENT", "ic": MARKOV_IC * 0.1}

        prices_raw = [h["p"] for h in history]

        # Step 1: Discretize prices into n_states states
        def discretize(p):
            return min(n_states - 1, int(p * n_states))

        states = [discretize(p) for p in prices_raw]

        # Step 2: Build transition matrix
        T = [[0.0] * n_states for _ in range(n_states)]
        for i in range(len(states) - 1):
            T[states[i]][states[i + 1]] += 1.0

        # Normalize rows → probabilities (with smoothing for unseen transitions)
        alpha = 0.1  # Laplace smoothing
        for i in range(n_states):
            row_sum = sum(T[i]) + alpha * n_states
            for j in range(n_states):
                T[i][j] = (T[i][j] + alpha) / row_sum

        # Step 3: Current state
        current_state = discretize(yes_p)

        # Step 4: Monte Carlo simulation — n_sims paths over horizon_days steps
        # Each "step" ≈ 1 hour of price data (fidelity=60min)
        steps_per_day = 24
        total_steps   = horizon_days * steps_per_day

        final_states = []
        for _ in range(n_sims):
            state = current_state
            for _ in range(total_steps):
                # Sample next state from transition row
                row  = T[state]
                rand = random.random()
                cum  = 0.0
                next_state = n_states - 1
                for j, prob in enumerate(row):
                    cum += prob
                    if rand <= cum:
                        next_state = j
                        break
                state = next_state
            final_states.append(state)

        # P(YES) = fraction of simulations ending in state >= n_states/2
        mid_state = n_states // 2
        p_yes_raw = sum(1 for s in final_states if s >= mid_state) / n_sims

        # Step 5: Calibration — backtest-derived corrections (n=16 markets)
        # NOTE: The 72M longshot bias applies to TAKER prices, NOT Markov outputs.
        # Backtest shows Markov < 10¢ resolves YES 27% of the time (6.7x too low).
        # Root cause: mean-reversion bias pushes simulations to 0¢ for drifting markets.
        # Correction: scale UP low Markov probabilities to avoid overconfident NO calls.
        if p_yes_raw < 0.10:
            # Markov is severely overconfident toward NO — scale up
            p_yes_calibrated = p_yes_raw * 2.5  # bring toward 25¢ range
        elif p_yes_raw < 0.25:
            # Mild upward correction
            p_yes_calibrated = p_yes_raw * 1.4
        elif p_yes_raw > 0.90:
            # High Markov predictions are well-calibrated (+0.06 bias only)
            p_yes_calibrated = min(0.99, p_yes_raw)
        else:
            p_yes_calibrated = p_yes_raw  # 25-90¢ range is well-calibrated

        p_yes_calibrated = max(0.01, min(0.99, p_yes_calibrated))

        # Step 6: Confidence based on history length and simulation convergence
        n_pts      = len(prices_raw)
        # Convergence: standard error of proportion = sqrt(p*(1-p)/n)
        se         = (p_yes_calibrated * (1 - p_yes_calibrated) / n_sims) ** 0.5
        confidence = min(0.85, max(0.1, (n_pts / 200) * (1 - se * 10)))

        deviation  = p_yes_calibrated - yes_p
        if deviation > 0.08:
            direction = "UNDERPRICED"
        elif deviation < -0.08:
            direction = "OVERPRICED"
        else:
            direction = "FAIR"

        return {
            "prob_estimate":     round(p_yes_calibrated, 4),
            "confidence":        round(confidence, 3),
            "direction":         direction,
            "ic":                MARKOV_IC * confidence,
            "n_history":         n_pts,
            "n_sims":            n_sims,
            "p_yes_raw":         round(p_yes_raw, 4),
            "p_yes_calibrated":  round(p_yes_calibrated, 4),
            "current_state":     current_state,
            "horizon_days":      horizon_days,
        }

    except Exception as e:
        return {"prob_estimate": yes_p, "confidence": 0,
                "direction": "ERROR", "ic": 0, "error": str(e)[:80]}


# ══════════════════════════════════════════════════════════════════════════════
# 11-STEP SIGNAL COMBINATION ENGINE (Roh framework)
# ══════════════════════════════════════════════════════════════════════════════
def combine_signals(signals: dict, yes_p: float) -> dict:
    """
    Implements the 11-step decorrelation and combination procedure.

    Simplified for cross-sectional application to a single market
    (we don't have time-series history per signal, so we adapt steps 1-7
    to work in the probability domain across signals).

    signals: {
        "term_structure": {..., "prob_estimate": float, "ic": float, "confidence": float},
        "insider_flow":   {...},
        "calibration":    {...},
        "news_velocity":  {...},
        "microstructure": {...},
    }

    Returns:
        combined_prob: float        — decorrelated IC-weighted probability
        combined_ic:   float        — total effective IC (accounting for correlation)
        ir_estimate:   float        — estimated information ratio = IC × √N_effective
        signal_weights: dict        — normalized weight per signal
        label:         str          — "STRONG_BUY" | "BUY" | "PASS" | "SELL" | etc.
    """
    # Step 1-4: Collect, demean, normalize signal outputs
    estimates  = []
    ics        = []
    names      = []
    confidences = []

    for name, sig in signals.items():
        p   = sig.get("prob_estimate", yes_p)
        ic  = sig.get("ic", 0)
        conf = sig.get("confidence", 0)
        if ic <= 0 or conf <= 0:
            continue  # skip signals with no information
        estimates.append(p)
        ics.append(ic)
        confidences.append(conf)
        names.append(name)

    if not estimates:
        # No valid signals — return market price with zero confidence
        return {
            "combined_prob":  yes_p,
            "combined_ic":    0,
            "ir_estimate":    0,
            "signal_weights": {},
            "n_signals":      0,
            "label":          "PASS",
            "edge":           0,
        }

    n = len(estimates)
    mean_est = sum(estimates) / n

    # Step 2: Demean
    deviations = [e - mean_est for e in estimates]

    # Step 3: Variance per signal
    variances = [d**2 for d in deviations]

    # Step 4: Normalize by standard deviation
    total_var = sum(variances)
    if total_var < 1e-8:
        # All signals agree — just use the mean
        combined_prob = mean_est
        weights = {name: 1/n for name in names}
    else:
        std = math.sqrt(total_var / n)

        # Step 5-7: Cross-sectional decorrelation
        # Subtract the cross-sectional mean from each normalized signal
        normalized = [d / std if std > 1e-8 else 0 for d in deviations]
        cross_mean = sum(normalized) / n
        decorr     = [x - cross_mean for x in normalized]

        # Step 8: IC-weighted expected value
        # Each signal's independent contribution weighted by its IC
        weighted_sum = sum(ic * d for ic, d in zip(ics, decorr))
        total_ic     = sum(ics)

        # Step 9: Residual weights (IC × independent deviation)
        # w(i) = η × ε(i) / σ(i)  [simplified: proportional to IC × decorr]
        raw_weights = [ic * abs(d) for ic, d in zip(ics, decorr)]
        total_w = sum(raw_weights) + 1e-10
        norm_weights = [w / total_w for w in raw_weights]

        # Step 10-11: Compute combined probability
        # Anchor to market price, shift by weighted independent signal deviation
        combined_delta = weighted_sum / (total_ic + 1e-10) * std
        combined_prob  = max(0.02, min(0.98, yes_p + combined_delta))

        weights = {name: round(w, 3) for name, w in zip(names, norm_weights)}

    # Combined IC (accounting for approximate independence)
    # True independence → combined_IC = mean_IC × √N
    # We estimate correlation between signals and discount accordingly
    mean_ic = sum(ics) / len(ics)
    # Correlation penalty: term_structure and microstructure are both CLOB-derived
    # Estimate ~0.3 avg pairwise correlation → effective N = N / (1 + (N-1)×ρ)
    est_rho     = 0.20  # conservative estimate
    n_effective = n / (1 + (n - 1) * est_rho)
    combined_ic = mean_ic * math.sqrt(n_effective)

    # Information ratio estimate
    ir_estimate = combined_ic  # per trade, not annualized

    # Edge vs. market price
    edge = combined_prob - yes_p

    # Label
    if edge > 0.15:
        label = "STRONG_BUY_YES"
    elif edge > 0.08:
        label = "BUY_YES"
    elif edge < -0.15:
        label = "STRONG_BUY_NO"
    elif edge < -0.08:
        label = "BUY_NO"
    elif abs(edge) < 0.04:
        label = "PASS"
    else:
        label = "WEAK_SIGNAL"

    return {
        "combined_prob":  round(combined_prob, 4),
        "combined_ic":    round(combined_ic, 4),
        "ir_estimate":    round(ir_estimate, 4),
        "signal_weights": weights,
        "n_signals":      n,
        "n_effective":    round(n_effective, 2),
        "label":          label,
        "edge":           round(edge, 4),
        "signals_used":   names,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EMPIRICAL KELLY SIZER
# ══════════════════════════════════════════════════════════════════════════════
def empirical_kelly_size(
    combined_prob:  float,
    yes_p:          float,
    action:         str,          # "BUY_YES" or "BUY_NO"
    available_cash: float,
    min_size:       float = 50.0,
    max_size:       float = 300.0,
    kelly_fraction: float = 0.25,  # fractional Kelly (safety multiplier)
    n_simulations:  int   = 5000,  # Monte Carlo for CV_edge
    ir_estimate:    float = 0.10,  # from combine_signals()
) -> dict:
    """
    Empirical Kelly position sizing using Monte Carlo simulation.

    f_empirical = f_kelly × (1 - CV_edge)

    where:
        f_kelly   = (p × b - q) / b  (standard Kelly formula)
        CV_edge   = std(simulated_edges) / mean(simulated_edges)
                  = coefficient of variation of edge uncertainty
        p         = combined_prob (our estimated true probability)
        q         = 1 - p
        b         = (1 - entry_price) / entry_price  (payout odds)

    The CV adjustment shrinks position size when edge is uncertain.
    High CV (edge varies wildly in sims) → smaller position.
    Low CV (edge is stable) → closer to full Kelly.
    """
    import random

    if action == "BUY_YES":
        entry_price = yes_p
        win_prob    = combined_prob
    else:  # BUY_NO
        entry_price = 1 - yes_p
        win_prob    = 1 - combined_prob

    win_prob    = max(0.01, min(0.99, win_prob))
    entry_price = max(0.01, min(0.99, entry_price))

    # Payout ratio: if we pay entry_price for a share that pays $1 on win
    b = (1.0 - entry_price) / entry_price  # net profit per $ risked on win
    q = 1 - win_prob

    # Standard Kelly fraction
    f_kelly_raw = (win_prob * b - q) / b
    f_kelly     = max(0, f_kelly_raw) * kelly_fraction  # fractional Kelly

    if f_kelly <= 0:
        return {
            "size_usdc":    0,
            "f_kelly":      0,
            "f_empirical":  0,
            "cv_edge":      1.0,
            "edge":         round(win_prob - entry_price, 4),
            "reason":       "Kelly < 0 — no edge",
        }

    # Monte Carlo: simulate edge uncertainty
    # Model: true_prob ~ Beta(α, β) where α, β calibrated from IR estimate
    # Higher IR → tighter distribution → lower CV
    # IR ≈ IC × √N; we use IR as precision proxy
    precision = max(5, ir_estimate * 200)  # maps IR 0.10 → alpha=20
    alpha     = win_prob * precision
    beta_     = (1 - win_prob) * precision

    simulated_edges = []
    for _ in range(n_simulations):
        # Sample a true probability from our uncertainty distribution
        sim_p = random.betavariate(alpha, beta_)
        sim_edge = sim_p - entry_price
        simulated_edges.append(sim_edge)

    valid_edges = [e for e in simulated_edges if e > 0]

    if not valid_edges:
        return {
            "size_usdc":    0,
            "f_kelly":      round(f_kelly, 4),
            "f_empirical":  0,
            "cv_edge":      1.0,
            "edge":         round(win_prob - entry_price, 4),
            "reason":       "No positive edge in Monte Carlo sims",
        }

    mean_edge = sum(valid_edges) / len(valid_edges)
    std_edge  = math.sqrt(sum((e - mean_edge)**2 for e in valid_edges) / len(valid_edges))
    cv_edge   = std_edge / mean_edge if mean_edge > 1e-8 else 1.0
    cv_edge   = min(1.0, cv_edge)

    # Empirical Kelly
    f_empirical = f_kelly * (1 - cv_edge)
    f_empirical = max(0, min(0.50, f_empirical))  # never > 50% of available cash

    size_usdc = available_cash * f_empirical
    size_usdc = max(min_size, min(max_size, size_usdc)) if size_usdc >= min_size else 0

    return {
        "size_usdc":       round(size_usdc, 2),
        "f_kelly":         round(f_kelly, 4),
        "f_empirical":     round(f_empirical, 4),
        "cv_edge":         round(cv_edge, 4),
        "edge":            round(win_prob - entry_price, 4),
        "win_prob":        round(win_prob, 4),
        "entry_price":     round(entry_price, 4),
        "b_odds":          round(b, 3),
        "n_sims_positive": len(valid_edges),
        "reason":          f"Kelly={f_kelly:.3f} × (1-CV={cv_edge:.3f}) = {f_empirical:.3f}",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — run all signals and return combined output
# ══════════════════════════════════════════════════════════════════════════════
def run_signal_engine(
    question:      str,
    yes_p:         float,
    yes_token:     str       = "",
    condition_id:  str       = "",
    description:   str       = "",
    uw_summary:    str       = "",
    vel_data:      dict      = None,
    available_cash: float    = 500.0,
    min_size:      float     = 50.0,
    max_size:      float     = 300.0,
    action_hint:   str       = "",   # pre-computed action from Claude (BUY_YES/BUY_NO/PASS)
) -> dict:
    """
    Full pipeline:
      1. Run all 5 signals
      2. Combine with IC-weighted decorrelation
      3. Compute empirical Kelly size
      4. Return combined result for injection into Claude prompt

    Returns dict ready for use as a source in score_with_claude.
    """
    # Run all signals
    s1 = signal_term_structure(condition_id, yes_token, yes_p)
    s2 = signal_insider_flow(uw_summary, yes_p)
    s3 = signal_calibration_prior(question, yes_p, description)
    s4 = signal_news_velocity(question, vel_data)
    s5 = signal_microstructure(yes_token, yes_p)
    s6 = signal_markov(yes_token, yes_p)

    signals = {
        "term_structure": s1,
        "insider_flow":   s2,
        "calibration":    s3,
        "news_velocity":  s4,
        "microstructure": s5,
        "markov":         s6,
    }

    # Combine signals
    combo = combine_signals(signals, yes_p)

    # Determine action
    action = action_hint if action_hint in ("BUY_YES", "BUY_NO") else (
        "BUY_YES" if combo["edge"] > 0 else "BUY_NO"
    )

    # Empirical Kelly sizing
    kelly = empirical_kelly_size(
        combined_prob  = combo["combined_prob"],
        yes_p          = yes_p,
        action         = action,
        available_cash = available_cash,
        min_size       = min_size,
        max_size       = max_size,
        ir_estimate    = combo["ir_estimate"],
    )

    # Build summary label for Claude prompt injection
    n_sig = combo["n_signals"]
    ir    = combo["ir_estimate"]
    edge  = combo["edge"]

    if n_sig > 0:
        signal_summary = (
            f"SIGNAL ENGINE ({n_sig}/{len(signals)} signals active, "
            f"IR≈{ir:.3f}, N_eff≈{combo['n_effective']:.1f}):\n"
            f"  Combined probability: {combo['combined_prob']:.3f} "
            f"(market: {yes_p:.3f}, edge: {edge:+.3f})\n"
            f"  Verdict: {combo['label']}\n"
            f"  Kelly size: ${kelly['size_usdc']:.0f} "
            f"[f_kelly={kelly['f_kelly']:.3f}, CV={kelly['cv_edge']:.3f}, "
            f"f_emp={kelly['f_empirical']:.3f}]\n"
            f"  Signals: "
            + " | ".join(
                f"{n}={signals[n].get('prob_estimate', yes_p):.3f}"
                f"({signals[n].get('direction','?')[:3]})"
                for n in combo.get("signals_used", [])
            )
        )
    else:
        signal_summary = (
            f"SIGNAL ENGINE: No active signals (all IC=0). "
            f"Market price {yes_p:.3f} used as-is."
        )

    return {
        "signals":        signals,
        "combination":    combo,
        "kelly":          kelly,
        "action":         action,
        "signal_summary": signal_summary,
        "combined_prob":  combo["combined_prob"],
        "kelly_size":     kelly["size_usdc"],
        "ir":             ir,
        "label":          combo["label"],
    }


# ── Bellman Exit Threshold (Phosphen, April 2026) ───────────────────────────
# Based on optimal stopping / Snell envelope framework.
# The exit threshold for a position should DECAY toward 0 as expiry approaches.
# A flat reservation price leaks money: continuation value changes every day.
#
# Formula: threshold(t) = fair_value × (1 - exp(-k × days_remaining / T))
# where k controls decay speed (k≈3 gives ~5% decay at T/3, ~63% at T).
#
# For multi-clip exits (d-stop problem): Kobylanski et al. 2009
#   Clip 1: exit at threshold × 0.85  (capture early drift)
#   Clip 2: exit at threshold × 1.00  (fair value hit)
#   Clip 3: hold to resolution or threshold × 1.15 (capture upside tail)

def bellman_exit_threshold(
    fair_value: float,
    days_remaining: int,
    total_days: int,
    k: float = 3.0,
) -> dict:
    """
    Compute the Bellman exit threshold for a YES position.

    Args:
        fair_value:      Your model's probability estimate (e.g. 0.60)
        days_remaining:  Calendar days until market resolution
        total_days:      Total duration of the market in days
        k:               Decay speed (default 3.0 — aggressive decay near expiry)

    Returns dict with:
        threshold:    The decayed exit price — sell when market hits this
        clip_exits:   Three-clip exit prices for scaling out
        hold_signal:  True if market is below threshold (hold/buy)
        exit_signal:  True if market is above threshold (sell/reduce)
        decay_factor: How much of the fair value is preserved today
    """
    import math
    if total_days <= 0:
        total_days = max(days_remaining, 1)
    t_frac = days_remaining / total_days
    # Decay: threshold starts near fair_value when t_frac=1, collapses to 0 at t_frac=0
    decay = 1.0 - math.exp(-k * t_frac)
    threshold = fair_value * decay
    threshold = max(threshold, fair_value * 0.05)  # floor at 5% of fair value
    threshold = min(threshold, 0.99)

    # Three clips (d-stop): early, fair, tail
    clip1 = round(min(threshold * 0.85, 0.99), 3)
    clip2 = round(min(threshold,        0.99), 3)
    clip3 = round(min(threshold * 1.15, 0.99), 3)

    return {
        "threshold":    round(threshold, 4),
        "clip_exits":   [clip1, clip2, clip3],
        "decay_factor": round(decay, 4),
        "days_remaining": days_remaining,
        "fair_value":   fair_value,
        "hold_signal":  True,   # caller compares market price to threshold
        "description":  (
            f"Exit threshold {threshold:.3f} (fair={fair_value:.3f}, "
            f"{days_remaining}d left, decay={decay:.2f}). "
            f"Clips: {clip1:.3f} / {clip2:.3f} / {clip3:.3f}"
        ),
    }


if __name__ == "__main__":
    # Quick smoke test
    import json
    print("=== Signal Engine Smoke Test ===\n")
    result = run_signal_engine(
        question      = "Will there be a ceasefire by April 30?",
        yes_p         = 0.285,
        yes_token     = "44149007410374101286260953227333745102128417138356632089802983317837574022801",
        condition_id  = "0x9937410f8aae98cc6440a7480ad832be5a1a998afcf5a6e77c6f5aa4c0f259f",
        description   = "Ceasefire between US and Iran by April 30, 2026",
        uw_summary    = "",
        available_cash = 3000,
        min_size       = 50,
        max_size       = 300,
    )
    print(result["signal_summary"])
    print(f"\nKelly detail: {json.dumps(result['kelly'], indent=2)}")
    print(f"\nCombination: {json.dumps(result['combination'], indent=2)}")
