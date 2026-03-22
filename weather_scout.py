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


def fetch_nws_high(grid_id: str, grid_x: int, grid_y: int, target_date: str) -> float | None:
    """
    Fetch NWS hourly forecast and return expected high temp (°F) for target_date.
    target_date: 'YYYY-MM-DD'
    """
    try:
        url = f"https://api.weather.gov/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "polymarket-weather-scout/1.0"})
        if r.status_code != 200:
            return None
        periods = r.json()["properties"]["periods"]
        day_temps = [p["temperature"] for p in periods if p["startTime"][:10] == target_date]
        if not day_temps:
            return None
        return float(max(day_temps))
    except Exception as e:
        _log(f"NWS fetch error for {grid_id}/{grid_x},{grid_y}: {e}", Fore.YELLOW)
        return None


def fetch_openmeteo_high(lat: float, lon: float, tz: str, target_date: str, unit: str) -> float | None:
    """
    Fetch Open-Meteo hourly forecast and return expected high for target_date.
    unit: 'C' or 'F'
    """
    try:
        temp_unit = "fahrenheit" if unit == "F" else "celsius"
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "temperature_unit": temp_unit,
            "forecast_days": 10,
            "timezone": tz,
        }, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        day_temps = [temp for t, temp in zip(times, temps)
                     if t[:10] == target_date and temp is not None]
        if not day_temps:
            return None
        return float(max(day_temps))
    except Exception as e:
        _log(f"Open-Meteo error ({lat},{lon}): {e}", Fore.YELLOW)
        return None


def get_city_forecast_high(city: str, target_date: str) -> tuple[float | None, str]:
    """
    Returns (forecast_high, unit) for a city on a given date.
    """
    cfg = CITY_CONFIGS.get(city)
    if not cfg:
        return None, "?"

    unit = cfg.get("unit", "C")

    if "nws_grid" in cfg:
        grid_id, grid_x, grid_y = cfg["nws_grid"]
        high = fetch_nws_high(grid_id, grid_x, grid_y, target_date)
        return high, unit
    else:
        lat = cfg["lat"]
        lon = cfg["lon"]
        tz  = cfg.get("tz", "UTC")
        high = fetch_openmeteo_high(lat, lon, tz, target_date, unit)
        return high, unit


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


def forecast_prob_for_bucket(forecast_high: float, low: float, high: float, is_gte: bool,
                              uncertainty_f: float = 2.0) -> float:
    """
    Estimate probability that actual high falls in the bucket [low, high).
    Uses a Gaussian model centered on forecast_high with std=uncertainty_f (in °F or °C).

    uncertainty_f: forecast standard deviation. 2°F ≈ 1.1°C is appropriate for
    24-48h NWS/Open-Meteo forecasts.
    """
    import math

    def normal_cdf(x: float, mu: float, sigma: float) -> float:
        return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

    sigma = uncertainty_f

    if is_gte:
        # P(X >= low)
        prob = 1.0 - normal_cdf(low, forecast_high, sigma)
    elif low == -999.0:
        # P(X <= high)
        prob = normal_cdf(high + 0.5, forecast_high, sigma)
    else:
        # P(low <= X < high+1) — bucket covers [low, high+0.999]
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

        # ── Get forecast (cached per city+date) ─────────────────────────────
        cache_key = f"{city}:{target_date}"
        if cache_key not in city_date_forecasts:
            forecast_high, _ = get_city_forecast_high(city, target_date)
            city_date_forecasts[cache_key] = forecast_high
            if forecast_high is not None:
                _log(f"Forecast {city} {target_date}: {forecast_high:.1f}°{unit}", Fore.CYAN)
        forecast_high = city_date_forecasts.get(cache_key)

        if forecast_high is None:
            continue

        # ── Estimate probability for this bucket ─────────────────────────────
        # Use 2°F / 1.1°C uncertainty for 24-48h forecasts
        uncertainty = 2.0 if unit == "F" else 1.1
        prob = forecast_prob_for_bucket(forecast_high, low, high, is_gte, uncertainty)

        if prob < WEATHER_MIN_CONFIDENCE:
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
        if edge < 0.15:  # Need at least 15pp edge after uncertainty
            continue

        # ── Duplicate check ──────────────────────────────────────────────────
        trade_key = f"{city}:{target_date}:{low}-{high}"
        if trade_key in weather_trades:
            _log(f"Already traded {trade_key}, skipping", Fore.YELLOW)
            continue

        # ── Size calculation ─────────────────────────────────────────────────
        remaining_budget = WEATHER_MAX_EXPOSURE - weather_exposure
        size = min(WEATHER_MAX_PER_TRADE, remaining_budget)
        if size < 5.0:
            _log("Weather budget exhausted", Fore.YELLOW)
            break

        # Higher edge = slightly larger size (but still capped)
        if edge > 0.40:
            size = min(size, WEATHER_MAX_PER_TRADE)
        elif edge > 0.30:
            size = min(size, 15.0)
        else:
            size = min(size, 10.0)

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
            forecast_high, _ = get_city_forecast_high(city, target_date)
            city_date_forecasts[cache_key] = forecast_high

        forecast_high = city_date_forecasts.get(cache_key)
        if forecast_high is None:
            continue

        uncertainty = 2.0 if unit == "F" else 1.1
        prob = forecast_prob_for_bucket(forecast_high, low, high, is_gte, uncertainty)

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
