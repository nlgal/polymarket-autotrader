"""
weather_scout.py — Polymarket Weather Market Scout
====================================================
Strategy: Temperature bucket markets (e.g. "Will the highest temp in Chicago be
between 40-41°F on March 23?") are priced algorithmically by the market maker at
roughly equal probability across all buckets. NWS/Open-Meteo point forecasts allow
identifying the correct bucket with high confidence, creating a persistent edge.

Edge source: weather forecast API vs flat-priced buckets.
No LLM involved — pure data arbitrage.

Integration: Called once per cycle from autotrader.py run_cycle().
Separate from the main LLM scoring path.

Limits (enforced here):
  - Max $20 per weather trade
  - Max $100 total weather exposure at any time
  - Only trade if: mid price < 0.35 AND forecast probability > 0.50
  - Minimum liquidity: $500
  - Market must close > 6 hours from now (not stale)
  - City must have NWS or Open-Meteo support
  - No duplicate trades for same city+date+bucket
"""

import os, json, re, time, requests
from datetime import datetime, timezone, timedelta
from colorama import Fore, Style

# ── Constants ────────────────────────────────────────────────────────────────
WEATHER_MAX_PER_TRADE   = 20.0    # Max USDC per single weather trade
WEATHER_MAX_EXPOSURE    = 100.0   # Max total open weather exposure
WEATHER_MIN_LIQUIDITY   = 500.0   # Min market liquidity to trade
WEATHER_MAX_MID         = 0.35    # Don't buy above 35¢ (asymmetric bet only)
WEATHER_MIN_CONFIDENCE  = 0.50    # Forecast must assign ≥50% prob to this bucket
WEATHER_MIN_HOURS_LEFT  = 6.0     # Market must have ≥6h before close

# ── Ensemble collapse guard (March 23 lesson) ─────────────────────────────────
# When all ensemble members agree (std < threshold), they share a systematic bias.
# Add extra buffer to account for unresolved model error.
WEATHER_ENSEMBLE_COLLAPSE_STD = 0.5   # °C — below this, ensemble is "collapsed"
WEATHER_COLLAPSE_BUFFER       = 1.0   # °C extra buffer to add when collapsed
WEATHER_STALE_RUN_HOURS       = 3.0   # Hours into ECMWF run window before applying stale penalty

# ── City reliability tiers ────────────────────────────────────────────────────
# TIER A: Reliable interiors, low convective uncertainty (dense obs network)
# TIER B: Semi-coastal or seasonal uncertainty (moderate buffer needed)
# TIER C: Coastal/tropical cities — high convective uncertainty
TIER_CONFIG = {
    "A": {"min_prob": 0.70, "max_size": 20.0, "extra_buffer_c": 0.0},
    "B": {"min_prob": 0.75, "max_size": 15.0, "extra_buffer_c": 1.0},
    "C": {"min_prob": 0.80, "max_size": 10.0, "extra_buffer_c": 2.0},
}

# City tier assignments
_CITY_TIER_MAP = {
    # Tier A — reliable interiors
    "Chicago": "A", "New York City": "A", "Los Angeles": "A",
    "Berlin": "A", "Moscow": "A", "Toronto": "A", "Denver": "A",
    # Tier B — semi-coastal / seasonal
    "Paris": "B", "Beijing": "B", "Milan": "B", "Istanbul": "B",
    "London": "B", "Tokyo": "B", "Seoul": "B", "Amsterdam": "B",
    # Tier C — coastal/tropical high uncertainty
    "Buenos Aires": "C", "Singapore": "C", "Mumbai": "C",
    "São Paulo": "C", "Sao Paulo": "C", "Lagos": "C", "Jakarta": "C",
}

def get_city_tier(city: str) -> str:
    """Return reliability tier for a city (A/B/C). Default B if unknown."""
    return _CITY_TIER_MAP.get(city, "B")

WEATHER_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "intelligence", "weather_trades.json")

# City → NWS grid or lat/lon for Open-Meteo
# NWS only covers US cities. Everything else uses Open-Meteo.
CITY_CONFIGS = {
    # US cities — use NWS (most accurate, official)
    "Chicago": {
        "nws_grid": ("LOT", 76, 73),
        "unit": "F",
        "tz": "America/Chicago",
    },
    "New York City": {
        "nws_grid": ("OKX", 33, 37),
        "unit": "F",
        "tz": "America/New_York",
    },
    "Dallas": {
        "nws_grid": ("FWD", 70, 67),
        "unit": "F",
        "tz": "America/Chicago",
    },
    "Seattle": {
        "nws_grid": ("SEW", 124, 67),
        "unit": "F",
        "tz": "America/Los_Angeles",
    },
    "Houston": {
        "nws_grid": ("HGX", 61, 98),
        "unit": "F",
        "tz": "America/Chicago",
    },
    "Los Angeles": {
        "nws_grid": ("LOX", 150, 47),
        "unit": "F",
        "tz": "America/Los_Angeles",
    },
    # International cities — use Open-Meteo
    "Seoul": {
        "lat": 37.5665, "lon": 126.9780,
        "unit": "C",
        "tz": "Asia/Seoul",
    },
    "Tokyo": {
        "lat": 35.6762, "lon": 139.6503,
        "unit": "C",
        "tz": "Asia/Tokyo",
    },
    "London": {
        "lat": 51.5074, "lon": -0.1278,
        "unit": "C",
        "tz": "Europe/London",
    },
    "Paris": {
        "lat": 48.8566, "lon": 2.3522,
        "unit": "C",
        "tz": "Europe/Paris",
    },
    "Munich": {
        "lat": 48.1351, "lon": 11.5820,
        "unit": "C",
        "tz": "Europe/Berlin",
    },
    "Singapore": {
        "lat": 1.3521, "lon": 103.8198,
        "unit": "C",
        "tz": "Asia/Singapore",
    },
    "Buenos Aires": {
        "lat": -34.6037, "lon": -58.3816,
        "unit": "C",
        "tz": "America/Argentina/Buenos_Aires",
    },
    "Wellington": {
        "lat": -41.2866, "lon": 174.7756,
        "unit": "C",
        "tz": "Pacific/Auckland",
    },
    "Warsaw": {
        "lat": 52.2297, "lon": 21.0122,
        "unit": "C",
        "tz": "Europe/Warsaw",
    },
    "Hong Kong": {
        "lat": 22.3193, "lon": 114.1694,
        "unit": "C",
        "tz": "Asia/Hong_Kong",
    },
    "Beijing": {
        "lat": 39.9042, "lon": 116.4074,
        "unit": "C",
        "tz": "Asia/Shanghai",
    },
    "Milan": {
        "lat": 45.4642, "lon": 9.1900,
        "unit": "C",
        "tz": "Europe/Rome",
    },
    "Sao Paulo": {
        "lat": -23.5505, "lon": -46.6333,
        "unit": "C",
        "tz": "America/Sao_Paulo",
    },
    "Shenzhen": {
        "lat": 22.5431, "lon": 114.0579,
        "unit": "C",
        "tz": "Asia/Shanghai",
    },
    "Chengdu": {
        "lat": 30.5728, "lon": 104.0668,
        "unit": "C",
        "tz": "Asia/Shanghai",
    },
    "Chongqing": {
        "lat": 29.5630, "lon": 106.5516,
        "unit": "C",
        "tz": "Asia/Shanghai",
    },
}


def _log(msg, color=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [WEATHER] {msg}"
    print(f"{color}{line}{Style.RESET_ALL}" if color else line)


def _load_weather_trades() -> dict:
    """Load previously placed weather trades to avoid duplicates."""
    try:
        if os.path.exists(WEATHER_LOG_FILE):
            with open(WEATHER_LOG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_weather_trades(trades: dict):
    try:
        os.makedirs(os.path.dirname(WEATHER_LOG_FILE), exist_ok=True)
        with open(WEATHER_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        _log(f"Failed to save weather trades: {e}", Fore.YELLOW)


def fetch_ensemble_members(lat: float, lon: float, tz: str,
                           target_date: str, unit: str) -> list[float]:
    """
    Fetch ECMWF 50-member ensemble from Open-Meteo and return
    the daily high temperature for each member on target_date.

    Using the ensemble endpoint instead of a single point forecast gives us
    a real probability distribution over outcomes rather than a Gaussian
    approximation. This is the gold standard for weather probability estimation.

    Returns list of floats (one per ensemble member). Empty list on failure.
    """
    try:
        temp_unit = "fahrenheit" if unit == "F" else "celsius"
        r = requests.get(
            "https://ensemble-api.open-meteo.com/v1/ensemble",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "temperature_unit": temp_unit,
                "forecast_days": 10,
                "timezone": tz,
                "models": "ecmwf_ifs025",   # 50-member ECMWF ensemble (best available free)
            },
            timeout=20,
        )
        if r.status_code != 200:
            return []
        hourly = r.json().get("hourly", {})
        member_keys = [k for k in hourly if k.startswith("temperature_2m_member")]
        times = hourly.get("time", [])

        member_highs = []
        for mk in member_keys:
            vals = hourly[mk]
            day_temps = [v for t, v in zip(times, vals)
                         if t.startswith(target_date) and v is not None]
            if day_temps:
                member_highs.append(float(max(day_temps)))
        return member_highs
    except Exception as e:
        _log(f"Ensemble fetch error ({lat},{lon}): {e}", Fore.YELLOW)
        return []


def fetch_nws_members(grid_id: str, grid_x: int, grid_y: int,
                      target_date: str) -> list[float]:
    """
    For US cities: fetch NWS deterministic forecast as a single point,
    then also get ECMWF ensemble (lat/lon needed).
    Falls back to single NWS value if ensemble unavailable.
    Returns list with either 1 value (NWS only) or 50 values (ensemble).
    """
    try:
        url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "polymarket-weather-scout/1.0"})
        if r.status_code != 200:
            return []
        periods = r.json()["properties"]["periods"]
        day_temps = [p["temperature"] for p in periods if p["startTime"][:10] == target_date]
        if not day_temps:
            return []
        return [float(max(day_temps))]  # Single NWS deterministic point
    except Exception as e:
        _log(f"NWS fetch error {grid_id}/{grid_x},{grid_y}: {e}", Fore.YELLOW)
        return []


def get_city_ensemble(city: str, target_date: str) -> tuple[list[float], str]:
    """
    Returns (ensemble_members, unit) for a city on a given date.
    For international cities: 50-member ECMWF ensemble (highest quality).
    For US cities: ECMWF ensemble via lat/lon (same quality).
    Falls back to NWS single point if ensemble unavailable.
    Returns (members_list, unit). Members list has 50 values (or 1 for NWS fallback).
    """
    cfg = CITY_CONFIGS.get(city)
    if not cfg:
        return [], "?"

    unit = cfg.get("unit", "C")
    tz   = cfg.get("tz", "UTC")

    # Always try ensemble first (works for all cities via lat/lon)
    if "lat" in cfg:
        lat, lon = cfg["lat"], cfg["lon"]
    elif "nws_grid" in cfg:
        # For NWS cities, get lat/lon from the NWS grid metadata
        _NWS_LATLON = {
            "Chicago":       (41.8827, -87.6233),
            "New York City":  (40.7128, -74.0060),
            "Dallas":         (32.7767, -96.7970),
            "Seattle":        (47.6062, -122.3321),
            "Houston":        (29.7604, -95.3698),
            "Los Angeles":    (34.0522, -118.2437),
        }
        lat, lon = _NWS_LATLON.get(city, (0, 0))
        if lat == 0:
            # Fallback to NWS single point
            grid_id, grid_x, grid_y = cfg["nws_grid"]
            members = fetch_nws_members(grid_id, grid_x, grid_y, target_date)
            return members, unit
    else:
        return [], unit

    # Fetch ECMWF ensemble
    members = fetch_ensemble_members(lat, lon, tz, target_date, unit)
    if members:
        return members, unit

    # Fallback to NWS if ensemble failed and this is a US city
    if "nws_grid" in cfg:
        grid_id, grid_x, grid_y = cfg["nws_grid"]
        nws_members = fetch_nws_members(grid_id, grid_x, grid_y, target_date)
        return nws_members, unit

    return [], unit


def get_city_forecast_high(city: str, target_date: str) -> tuple[float | None, str]:
    """
    Legacy compatibility shim — returns (mean_ensemble_high, unit).
    Used by oracle_check_weather in autotrader.py.
    """
    members, unit = get_city_ensemble(city, target_date)
    if not members:
        return None, unit
    import statistics
    return statistics.mean(members), unit


def parse_bucket(question: str, unit: str) -> tuple[float | None, float | None, bool]:
    """
    Parse the temperature bucket from a market question.
    Returns (low, high, is_gte) where:
      is_gte=True means the bucket is "X°C or higher" (open-ended upper bound)
      is_gte=False means it's a specific range [low, high)

    Examples:
      "between 48-49°F" → (48, 49, False)
      "between 40-41°F" → (40, 41, False)
      "16°C or higher"  → (16, inf, True)
      "37°F or below"   → (-inf, 37, False) — treated as low=-999
      "13°C"            → (13, 14, False)   — single degree
    """
    q = question.lower()

    # "X or below" pattern
    m = re.search(r'(\d+(?:\.\d+)?)[°]?(?:c|f)?\s+or\s+below', q)
    if m:
        val = float(m.group(1))
        return (-999.0, val, False)

    # "X or higher" / "X or more" pattern
    m = re.search(r'(\d+(?:\.\d+)?)[°]?(?:c|f)?\s+or\s+(?:higher|more|above)', q)
    if m:
        val = float(m.group(1))
        return (val, 9999.0, True)

    # "between X-Y" or "X-Y°F" pattern
    m = re.search(r'between\s+(\d+(?:\.\d+)?)[°\s-]+(\d+(?:\.\d+)?)', q)
    if not m:
        m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°', q)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        return (low, high, False)

    # Single value "X°C" (no range specified)
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*°', q)
    if m:
        val = float(m.group(1))
        return (val, val + 1.0, False)

    return (None, None, False)


def ensemble_prob_for_bucket(members: list[float], low: float, high: float,
                              is_gte: bool) -> float:
    """
    Compute bucket probability directly from ECMWF ensemble members.
    Each member is one physics simulation of the atmosphere — the fraction
    of members landing in the bucket IS the probability (frequentist).

    This replaces the Gaussian approximation with actual model output.
    50 members gives ~2% resolution (each member = 2pp).

    Bucket definitions:
      is_gte=True:  P(X >= low) — "X°C or higher"
      low==-999:    P(X <= high) — "X°C or below"
      else:         P(low <= X < high+1) — "between X-Y°"
    """
    if not members:
        return 0.0
    n = len(members)
    if is_gte:
        count = sum(1 for h in members if h >= low)
    elif low == -999.0:
        count = sum(1 for h in members if h <= high)
    else:
        count = sum(1 for h in members if low <= h < high + 1.0)
    return count / n


def forecast_prob_for_bucket(forecast_high: float, low: float, high: float, is_gte: bool,
                              uncertainty_f: float = 2.0) -> float:
    """
    Gaussian fallback when ensemble members are unavailable (e.g. NWS single point).
    Uses a normal distribution centered on forecast_high with given std.
    """
    import math

    def normal_cdf(x: float, mu: float, sigma: float) -> float:
        return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

    sigma = uncertainty_f
    if is_gte:
        prob = 1.0 - normal_cdf(low, forecast_high, sigma)
    elif low == -999.0:
        prob = normal_cdf(high + 0.5, forecast_high, sigma)
    else:
        prob = normal_cdf(high + 0.999, forecast_high, sigma) - normal_cdf(low, forecast_high, sigma)
    return max(0.0, min(1.0, prob))


def get_market_mid(token_id: str) -> float | None:
    """Get current midpoint price from CLOB order book."""
    try:
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}",
                         timeout=8)
        if r.status_code == 200:
            mid = r.json().get("mid")
            return float(mid) if mid is not None else None
    except Exception:
        pass
    return None


def fetch_weather_markets() -> list[dict]:
    """
    Fetch active temperature bucket markets from Polymarket gamma API.
    Returns list of market dicts with token IDs and prices.
    """
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets", params={
            "closed": "false",
            "limit": 500,
            "offset": 0,
            "order": "volume",
            "ascending": "false",
        }, timeout=20)
        if r.status_code != 200:
            _log(f"Gamma API error: {r.status_code}", Fore.YELLOW)
            return []
        markets = r.json()
        temp_kw = ["highest temperature", "lowest temperature"]
        return [m for m in markets if any(kw in m.get("question", "") for kw in temp_kw)]
    except Exception as e:
        _log(f"fetch_weather_markets error: {e}", Fore.YELLOW)
        return []


def run_weather_scout(client, state: dict, equity: float) -> list[dict]:
    """
    Main entry point. Scans temperature markets, finds mispriced buckets,
    places trades when edge is clear.

    Returns list of trades placed: [{"question": ..., "size": ..., "mid": ..., "edge": ...}]
    """
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    placed = []
    now = datetime.now(timezone.utc)

    # Load state to track exposure and avoid duplicates
    weather_trades = _load_weather_trades()
    
    # Compute current open weather exposure from state
    weather_exposure = state.get("weather_exposure", 0.0)

    if weather_exposure >= WEATHER_MAX_EXPOSURE:
        _log(f"Weather exposure cap reached (${weather_exposure:.0f}), skipping", Fore.YELLOW)
        return []

    _log("Scanning temperature markets...", Fore.CYAN)
    markets = fetch_weather_markets()
    _log(f"Found {len(markets)} temperature markets", Fore.CYAN)

    if not markets:
        return []

    # Group by city+date to fetch forecasts efficiently
    city_date_forecasts = {}

    for m in markets:
        q = m.get("question", "")
        end_date_str = m.get("endDate", "")
        liq = float(m.get("liquidity", 0) or 0)
        clob_ids_raw = m.get("clobTokenIds", [])
        # clobTokenIds is returned as a JSON string by the gamma API — parse it
        if isinstance(clob_ids_raw, str):
            try:
                clob_ids = json.loads(clob_ids_raw)
            except Exception:
                clob_ids = []
        else:
            clob_ids = clob_ids_raw
        if not clob_ids:
            continue

        # ── Eligibility pre-checks ──────────────────────────────────────────
        # Liquidity
        if liq < WEATHER_MIN_LIQUIDITY:
            continue

        # Time to close
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
            if hours_left < WEATHER_MIN_HOURS_LEFT:
                continue
            target_date = end_dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        # ── Identify city ───────────────────────────────────────────────────
        city = None
        for c in CITY_CONFIGS:
            if c in q:
                city = c
                break
        if not city:
            continue

        # Determine temperature unit from city config
        unit = CITY_CONFIGS[city].get("unit", "C")

        # ── Parse bucket ────────────────────────────────────────────────────
        low, high, is_gte = parse_bucket(q, unit)
        if low is None:
            continue

        # ── Get ensemble forecast (cached per city+date) ──────────────────────
        cache_key = f"{city}:{target_date}"
        if cache_key not in city_date_forecasts:
            members, _ = get_city_ensemble(city, target_date)
            city_date_forecasts[cache_key] = members
            if members:
                import statistics as _stat
                _log(f"Ensemble {city} {target_date}: mean={_stat.mean(members):.1f}°{unit} "
                     f"std={(_stat.stdev(members) if len(members)>1 else 0):.2f}°{unit} "
                     f"({len(members)} members)", Fore.CYAN)
        members = city_date_forecasts.get(cache_key, [])

        if not members:
            continue

        import statistics as _stat
        forecast_high = _stat.mean(members)
        ensemble_std  = _stat.stdev(members) if len(members) > 1 else 2.0

        # ── City tier: per-city thresholds and bucket buffer ──────────────────
        # ── Seasonal tier adjustment ─────────────────────────────────────────
        # Some cities are harder to forecast in specific seasons:
        #   Paris Mar-May: warm surge events from Atlantic → empirically Tier B
        #   Beijing Mar-May: Gobi Desert warm advection → up to +5°C in 24h
        #   Buenos Aires Mar-Apr: late-summer convection → Tier C
        _month = datetime.now(timezone.utc).month
        _seasonal_upgrades = {}
        if 3 <= _month <= 5:
            _seasonal_upgrades = {"Paris": "B", "Beijing": "B", "Milan": "B"}
        tier = _seasonal_upgrades.get(city) or get_city_tier(city)
        tcfg         = TIER_CONFIG[tier]
        min_prob     = tcfg["min_prob"]
        max_size_city = tcfg["max_size"]
        buf_c        = tcfg["extra_buffer_c"]

        # ── Ensemble collapse detection ───────────────────────────────────────
        # If std < 0.5°C, all members share the same systematic bias (overconfident).
        # March 23 lesson: Wellington std=0.36°C but actual miss was 2.8°C.
        if ensemble_std < WEATHER_ENSEMBLE_COLLAPSE_STD:
            buf_c += WEATHER_COLLAPSE_BUFFER
            _log(f"  [COLLAPSE] {city} std={ensemble_std:.2f}°{unit} — adding buffer (total={buf_c:.1f}°C)", Fore.YELLOW)

        # Widen bucket by buffer before computing probability
        buf = buf_c if unit == "C" else buf_c * 1.8
        buf_low  = low  - buf if low  != -999.0 else low
        buf_high = high + buf if high != 9999.0 else high

        if len(members) > 1:
            prob = ensemble_prob_for_bucket(members, buf_low, buf_high, is_gte)
        else:
            uncertainty = (2.0 if unit == "F" else 1.1) + buf
            prob = forecast_prob_for_bucket(members[0], buf_low, buf_high, is_gte, uncertainty)

        # ── Overconfidence cap (90% ceiling) ───────────────────────────────
        # When all 50 members agree (prob >= 90%), model has systematic bias.
        # Empirically: Paris 100% → missed by +3.8°C; Wellington 98% → missed 2°C.
        ENSEMBLE_CONFIDENCE_CAP = 0.90
        if prob > ENSEMBLE_CONFIDENCE_CAP:
            _log(f"  [CAP] {city} {prob:.0%} → capped at {ENSEMBLE_CONFIDENCE_CAP:.0%}", Fore.YELLOW)
            prob = ENSEMBLE_CONFIDENCE_CAP

        # ── Model run freshness penalty ───────────────────────────────────────
        # ECMWF runs at 00:00 and 12:00 UTC. If >3h into a 12h window, apply -5pp.
        _utc_hour = datetime.now(timezone.utc).hour
        _run_age_h = _utc_hour % 12
        if _run_age_h > WEATHER_STALE_RUN_HOURS:
            prob = max(0.0, prob - 0.05)
            _log(f"  [STALE RUN] ECMWF ~{_run_age_h:.0f}h old — prob reduced by 5pp", Fore.YELLOW)

        if prob < min_prob:
            continue

        # ── Get current market mid price ─────────────────────────────────────
        yes_token_id = clob_ids[0] if isinstance(clob_ids[0], str) else str(clob_ids[0])
        mid = get_market_mid(yes_token_id)
        if mid is None:
            continue

        # Must be below our price ceiling (asymmetric bet)
        if mid > WEATHER_MAX_MID:
            continue

        # Edge: how mispriced is it?
        edge = prob - mid
        if edge < 0.18:  # Need at least 18pp edge (15pp base + ~3pp fee buffer for weather markets)
            continue

        # ── Duplicate check ──────────────────────────────────────────────────
        trade_key = f"{city}:{target_date}:{low}-{high}"
        if trade_key in weather_trades:
            _log(f"Already traded {trade_key}, skipping", Fore.YELLOW)
            continue

        # ── Size calculation ─────────────────────────────────────────────────
        remaining_budget = WEATHER_MAX_EXPOSURE - weather_exposure
        # Cap by tier: Tier C cities get max $10 (coastal uncertainty)
        tier_cap = max_size_city  # from TIER_CONFIG
        size = min(WEATHER_MAX_PER_TRADE, tier_cap, remaining_budget)
        if size < 5.0:
            _log("Weather budget exhausted", Fore.YELLOW)
            break

        # Higher edge = slightly larger size (but still capped)
        if edge > 0.40:
            size = min(size, tier_cap)
        elif edge > 0.30:
            size = min(size, min(15.0, tier_cap))
        else:
            size = min(size, min(10.0, tier_cap))

        # ── Place the trade ──────────────────────────────────────────────────
        _log(
            f"WEATHER TRADE: {q[:60]}",
            Fore.GREEN
        )
        _log(
            f"  Forecast={forecast_high:.1f}°{unit} bucket=[{low},{high}] prob={prob:.0%} mid={mid:.3f} edge={edge:+.3f} size=${size:.0f}",
            Fore.GREEN
        )

        try:
            tick = client.get_tick_size(yes_token_id)
            neg_risk = client.get_neg_risk(yes_token_id)
            tick_f = float(tick)
            tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0

            # Use mid as limit price (slight improvement on market)
            buy_price = round(round(mid / tick_f) * tick_f, tick_dec)
            buy_price = max(0.01, min(0.99, buy_price))

            args = OrderArgs(
                token_id=yes_token_id,
                price=buy_price,
                size=round(size / buy_price, 2),  # shares = size / price
                side=BUY,
            )
            options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
            signed  = client.create_order(args, options)
            receipt = client.post_order(signed, OrderType.GTC)

            if receipt.get("success"):
                _log(f"  ✓ Placed @ {buy_price:.3f} | {receipt.get('orderID', '')[:12]}", Fore.GREEN)

                # Record trade
                weather_trades[trade_key] = {
                    "question": q,
                    "city": city,
                    "date": target_date,
                    "bucket": [low, high, is_gte],
                    "forecast_high": forecast_high,
                    "unit": unit,
                    "prob": round(prob, 3),
                    "mid_at_entry": mid,
                    "edge": round(edge, 3),
                    "size_usdc": round(size, 2),
                    "price": buy_price,
                    "token_id": yes_token_id,
                    "placed_at": now.isoformat(),
                    "order_id": receipt.get("orderID", ""),
                }
                _save_weather_trades(weather_trades)

                weather_exposure += size
                state["weather_exposure"] = weather_exposure

                placed.append(weather_trades[trade_key])

                # Telegram notification
                try:
                    from autotrader import tg
                    tg(
                        f"🌡️ <b>Weather trade placed</b>\n"
                        f"{q[:70]}\n"
                        f"Forecast: {forecast_high:.1f}°{unit} | Bucket prob: {prob:.0%}\n"
                        f"Market price: {mid:.3f} → Edge: {edge:+.3f}\n"
                        f"Size: ${size:.0f} @ {buy_price:.3f}"
                    )
                except Exception:
                    pass

            else:
                _log(f"  Order failed: {receipt.get('errorMsg', '')}", Fore.RED)

        except Exception as e:
            _log(f"  Trade error: {e}", Fore.RED)

    if placed:
        _log(f"Weather scout placed {len(placed)} trades", Fore.GREEN)
    else:
        _log("No weather trades this cycle (no sufficient edge found)", Fore.WHITE)

    return placed


# ── Standalone test (run directly to verify without placing trades) ───────────
if __name__ == "__main__":
    print("=== WEATHER SCOUT DRY RUN ===\n")
    now = datetime.now(timezone.utc)

    markets = fetch_weather_markets()
    print(f"Active temperature markets: {len(markets)}\n")

    city_date_forecasts = {}
    opportunities = []

    for m in markets:
        q = m.get("question", "")
        end_date_str = m.get("endDate", "")
        liq = float(m.get("liquidity", 0) or 0)
        clob_ids_raw = m.get("clobTokenIds", [])
        # clobTokenIds is returned as a JSON string by the gamma API — parse it
        if isinstance(clob_ids_raw, str):
            try:
                clob_ids = json.loads(clob_ids_raw)
            except Exception:
                clob_ids = []
        else:
            clob_ids = clob_ids_raw

        if liq < WEATHER_MIN_LIQUIDITY or not clob_ids:
            continue

        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end_dt - now).total_seconds() / 3600
            if hours_left < WEATHER_MIN_HOURS_LEFT:
                continue
            target_date = end_dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        city = None
        for c in CITY_CONFIGS:
            if c in q:
                city = c
                break
        if not city:
            continue

        unit = CITY_CONFIGS[city].get("unit", "C")
        low, high, is_gte = parse_bucket(q, unit)
        if low is None:
            continue

        cache_key = f"{city}:{target_date}"
        if cache_key not in city_date_forecasts:
            members, _ = get_city_ensemble(city, target_date)
            city_date_forecasts[cache_key] = members

        members = city_date_forecasts.get(cache_key, [])
        if not members:
            continue

        import statistics as _stat
        forecast_high = _stat.mean(members)
        if len(members) > 1:
            prob = ensemble_prob_for_bucket(members, low, high, is_gte)
        else:
            uncertainty = 2.0 if unit == "F" else 1.1
            prob = forecast_prob_for_bucket(members[0], low, high, is_gte, uncertainty)

        yes_token_id = clob_ids[0] if isinstance(clob_ids[0], str) else str(clob_ids[0])
        mid = get_market_mid(yes_token_id)
        if mid is None:
            continue

        edge = prob - mid
        bucket_str = f"[{low},{high}]" if not is_gte else f"≥{low}"

        if prob > 0.20:  # Show anything with >20% forecast prob
            opportunities.append({
                "city": city, "date": target_date, "unit": unit,
                "forecast": forecast_high, "bucket": bucket_str,
                "prob": prob, "mid": mid, "edge": edge,
                "liq": liq, "q": q[:70],
            })

    # Sort by edge descending
    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    print(f"{'City':<18} {'Date':<11} {'Forecast':<10} {'Bucket':<12} {'Prob':>6} {'Mid':>6} {'Edge':>7} {'Liq':>7}")
    print("-" * 90)
    for o in opportunities:
        flag = " ← TRADE" if o["edge"] >= 0.15 and o["mid"] <= WEATHER_MAX_MID else ""
        print(f"{o['city']:<18} {o['date']:<11} {o['forecast']:.1f}°{o['unit']:<7} "
              f"{o['bucket']:<12} {o['prob']:>5.0%} {o['mid']:>6.3f} {o['edge']:>+7.3f} "
              f"${o['liq']:>6,.0f}{flag}")

    print(f"\nTrade opportunities (edge ≥ 0.15, mid ≤ {WEATHER_MAX_MID}): "
          f"{sum(1 for o in opportunities if o['edge'] >= 0.15 and o['mid'] <= WEATHER_MAX_MID)}")
