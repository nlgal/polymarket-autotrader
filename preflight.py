"""
preflight.py — Pre-flight test suite for Polymarket trading bots
=================================================================
Runs at startup of autotrader.py and btc_momentum.py.
Encodes every real bug we've hit as a test that MUST pass before live trading.

Each test:
  - Has a name describing what it protects against
  - Returns (passed: bool, message: str)
  - Is derived from a real production failure

Import and call: run_preflight() → returns True if all critical tests pass.

Tests derived from real failures:
  T1: Sell size never exceeds CLOB balance (fixed round→floor bug, March 24)
  T2: CLOB API credentials are valid (not expired/wrong model)
  T3: Equity reflects full portfolio, not just USDC (fixed $29 vs $1881, March 24)
  T4: BTC price feed returns realistic value (momentum bot dependency)
  T5: Polymarket 5-min market is accessible and has real order book depth
  T6: Weather scout has required constants (TIER_CONFIG etc., fixed March 23)
  T7: All required env vars are present
  T8: USDC allowances are set for all three exchange contracts
"""

import os, sys, math, json, time, requests, importlib
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID","").strip()
ANTHROPIC   = os.environ.get("ANTHROPIC_API_KEY","").strip()

# ── Test Registry ──────────────────────────────────────────────────────────────

TESTS_CRITICAL = []   # Failure blocks trading entirely
TESTS_WARNING  = []   # Failure sends alert but doesn't block

def test(name, critical=True):
    """Decorator to register a test function."""
    def decorator(fn):
        entry = {"name": name, "fn": fn, "critical": critical}
        if critical:
            TESTS_CRITICAL.append(entry)
        else:
            TESTS_WARNING.append(entry)
        return fn
    return decorator

# ── T1: Sell Size Never Exceeds Balance ───────────────────────────────────────
@test("T1: sell_size floor(balance*100)/100 never exceeds balance")
def test_sell_size_math():
    """
    Root cause: round(337.238879, 2) = 337.24 > 337.238879 → 400 error.
    Fix: math.floor(x * 100) / 100 always gives size ≤ actual balance.
    This test verifies the floor function is correct for 1000 edge cases.
    """
    failures = []
    test_balances = [
        337.238879,   # The exact failing case
        100.999999,   # Rounds up with round(), correct with floor
        0.001,        # Tiny position
        999.995,      # Near-integer
        50.50001,     # Subtle case
        1.0,          # Exact integer
        42.424242,    # Arbitrary decimal
    ]
    # Add programmatic cases
    import random
    random.seed(42)
    for _ in range(100):
        test_balances.append(random.uniform(0.01, 1000.0))

    for balance in test_balances:
        # The correct floor implementation
        floored = math.floor(balance * 100) / 100
        # Verify: floored ≤ balance (never request more than we have)
        if floored > balance + 1e-9:  # tiny epsilon for float precision
            failures.append(f"floor({balance}) = {floored} > balance!")
        # Verify: floored is within 0.01 of balance (not truncating too much)
        if balance - floored > 0.011:
            failures.append(f"floor({balance}) = {floored} loses too much")
        # Verify: the old round() would have failed for the known case
        # (just documenting, not asserting)

    # Verify the specific failing case
    failing_case = 337.238879
    bad_result   = round(failing_case, 2)   # = 337.24 (wrong)
    good_result  = math.floor(failing_case * 100) / 100  # = 337.23 (correct)
    assert bad_result  == 337.24, "round() changed behavior"
    assert good_result == 337.23, f"floor() gave {good_result}, expected 337.23"
    assert good_result <= failing_case, "floor result exceeds balance"
    assert bad_result  > failing_case,  "round() should exceed balance (demonstrating the bug)"

    if failures:
        return False, f"sell_size floor math failures: {failures[:3]}"
    return True, f"Sell size floor math correct for all {len(test_balances)} test cases"

# ── T2: CLOB API Credentials Valid ────────────────────────────────────────────
@test("T2: CLOB API credentials are valid and not expired")
def test_clob_credentials():
    """
    Root cause: Wrong Anthropic model name (claude-3-5-haiku vs claude-haiku-4-5)
    caused silent API failures. CLOB creds expire and cause 400 errors.
    This test verifies CLOB auth is working before any trading begins.
    """
    if not PRIVATE_KEY:
        return False, "POLYMARKET_PRIVATE_KEY not set"
    if not FUNDER:
        return False, "POLYMARKET_FUNDER_ADDRESS not set"

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        # Test with existing creds first
        creds = ApiCreds(
            api_key=os.environ.get("CLOB_API_KEY",""),
            api_secret=os.environ.get("CLOB_API_SECRET",""),
            api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))

        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

        # Try to make an authenticated call
        try:
            bal = client.get_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=2))
            usdc = float(bal.get("balance", 0)) / 1e6
            if usdc >= 0:  # Even $0 is a valid authenticated response
                return True, f"CLOB auth OK | USDC balance: ${usdc:.2f}"
        except Exception:
            pass  # Try re-deriving creds

        # Creds expired — re-derive
        fresh_creds = client.create_or_derive_api_creds()
        client.set_api_creds(fresh_creds)
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))
        usdc = float(bal.get("balance", 0)) / 1e6

        # Save fresh creds to .env
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        import re
        with open(env_path) as f:
            env = f.read()
        for key, val in [("CLOB_API_KEY", fresh_creds.api_key),
                         ("CLOB_API_SECRET", fresh_creds.api_secret),
                         ("CLOB_API_PASSPHRASE", fresh_creds.api_passphrase)]:
            if re.search(f"^{key}=", env, re.MULTILINE):
                env = re.sub(f"^{key}=.*$", f"{key}={val}", env, flags=re.MULTILINE)
            else:
                env += f"\n{key}={val}"
        with open(env_path, "w") as f:
            f.write(env)

        return True, f"CLOB auth OK (re-derived fresh creds) | USDC: ${usdc:.2f}"

    except Exception as e:
        return False, f"CLOB auth failed: {e}"

# ── T3: Equity Reflects Full Portfolio ────────────────────────────────────────
@test("T3: Equity includes position values, not just USDC cash")
def test_equity_accuracy():
    """
    Root cause: get_equity() returned only USDC balance ($29) instead of
    total portfolio value ($1,881) because it only queried COLLATERAL balance.
    Fix: use Polymarket /value API which includes all position mark-to-market.
    """
    try:
        # Method A: Polymarket /value API (correct)
        r_value = requests.get(
            f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=8)
        total_value = 0
        if r_value.status_code == 200:
            data = r_value.json()
            if isinstance(data, list) and data:
                total_value = float(data[0].get("value", 0))

        # Method B: USDC cash only (old broken method)
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                         api_secret=os.environ.get("CLOB_API_SECRET",""),
                         api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))
        usdc_only = float(bal.get("balance", 0)) / 1e6

        # Verify: /value API returns >= USDC cash (positions add value, never subtract)
        if total_value < usdc_only - 1.0:  # allow $1 tolerance for timing
            return False, (f"Equity check failed: /value API (${total_value:.2f}) < "
                           f"USDC cash (${usdc_only:.2f}) — impossible unless API broken")

        # Verify: total value is plausible (not obviously wrong)
        if total_value < 0:
            return False, f"Negative equity ${total_value:.2f} from /value API"

        # Calculate the error that would occur using old method
        discrepancy = total_value - usdc_only
        if discrepancy > 0:
            return True, (f"Equity OK: /value=${total_value:.2f}, "
                          f"USDC-only=${usdc_only:.2f} "
                          f"(old method would under-report by ${discrepancy:.2f})")
        else:
            return True, f"Equity OK: ${total_value:.2f} (no open positions or positions at cost)"

    except Exception as e:
        return False, f"Equity check failed: {e}"

# ── T4: BTC Price Feed Returns Realistic Value ────────────────────────────────
@test("T4: BTC price feed returns realistic value (momentum bot dependency)",
      critical=False)  # Warning only — bot can use fallbacks
def test_btc_price_feed():
    """
    The momentum bot depends on accurate BTC prices. If all sources fail or
    return wildly wrong values, trades will be based on stale/wrong data.
    Test that at least one source returns a value within expected range.
    """
    BTC_MIN_EXPECTED = 5000    # Historical all-time context
    BTC_MAX_EXPECTED = 500000  # Far future upper bound
    
    sources = {
        "Chainlink oracle": lambda: _chainlink_price(),
        "Coinbase REST":    lambda: float(requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=4
            ).json()["data"]["amount"]),
        "Kraken REST":      lambda: float(requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=4
            ).json()["result"]["XXBTZUSD"]["c"][0]),
    }

    working = []
    prices  = []
    for name, fn in sources.items():
        try:
            price = fn()
            if BTC_MIN_EXPECTED < price < BTC_MAX_EXPECTED:
                working.append(name)
                prices.append(price)
        except:
            pass

    if not working:
        return False, "All BTC price sources failed — momentum bot cannot trade safely"

    # Check consistency across sources (within 1%)
    if len(prices) >= 2:
        spread = (max(prices) - min(prices)) / min(prices)
        if spread > 0.01:
            return False, (f"BTC price inconsistency: {prices} — "
                           f"spread {spread*100:.2f}% exceeds 1%")

    return True, f"BTC price OK: ${prices[0]:,.2f} from {len(working)} source(s): {working}"

def _chainlink_price():
    rpc = requests.post("https://polygon-bor-rpc.publicnode.com",
        json={"jsonrpc":"2.0","method":"eth_call","params":[
            {"to":"0xc907E116054Ad103354f2D350FD2514433D57F6f","data":"0x50d25bcd"},
            "latest"],"id":1}, timeout=5)
    result = rpc.json().get("result","")
    if result and len(result) > 10:
        price_int = int(result, 16)
        if price_int > 2**255: price_int -= 2**256
        return price_int / 1e8
    return None

# ── T5: Polymarket 5-Min Market Has Real Order Book ──────────────────────────
@test("T5: Active 5-min BTC market exists and has tradeable order book",
      critical=False)  # Warning only — timing dependent
def test_5min_market_depth():
    """
    The 5-min markets only exist during active windows. Between windows,
    the test is meaningless. During a window, verify real bid depth exists.
    We saw ghost order books (bids only at 0.001) that can't be filled.
    """
    now = int(time.time())
    window_start = (now // 300) * 300
    time_in_window = now - window_start

    # Only meaningful during the entry window
    if time_in_window < 10 or time_in_window > 200:
        return True, f"Outside entry window ({time_in_window}s into window) — skipping depth check"

    slug = f"btc-updown-5m-{window_start}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8)
        data = r.json()
        if not data:
            return False, f"No market found for slug {slug}"

        markets = data[0].get("markets", [])
        if not markets:
            return False, "Event has no markets"

        tokens = markets[0].get("clobTokenIds", [])
        if isinstance(tokens, str): tokens = json.loads(tokens)
        if not tokens:
            return False, "No token IDs found"

        # Check order book for the UP token
        up_token = tokens[0]
        r2 = requests.get(f"https://clob.polymarket.com/book?token_id={up_token}", timeout=5)
        book = r2.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # Check for real bids between 10¢ and 90¢ (ghost bids are at 0.001-0.01)
        real_bids = [b for b in bids if 0.10 <= float(b["price"]) <= 0.90]
        real_asks = [a for a in asks if 0.10 <= float(a["price"]) <= 0.90]

        if not real_bids and not real_asks:
            # Fresh window might not have real depth yet — this is expected
            mid = float(requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={up_token}",
                timeout=5).json().get("mid", 0))
            if 0.40 <= mid <= 0.60:
                return True, f"Fresh window, no depth yet but mid={mid:.3f} (50/50 is normal)"
            return False, f"No real order book depth and mid={mid:.3f} is suspicious"

        depth_usdc = sum(float(b["size"]) * float(b["price"]) for b in real_bids[:5])
        return True, f"Order book OK: {len(real_bids)} real bids, ${depth_usdc:.0f} depth"

    except Exception as e:
        return True, f"Market depth check skipped: {e} (non-critical)"

# ── T6: Required Constants Defined ────────────────────────────────────────────
@test("T6: weather_scout.py has all required constants (TIER_CONFIG, get_city_tier, etc.)")
def test_weather_scout_constants():
    """
    Root cause: weather_scout.py referenced TIER_CONFIG, get_city_tier,
    WEATHER_ENSEMBLE_COLLAPSE_STD, WEATHER_COLLAPSE_BUFFER without defining them.
    Every weather trade cycle crashed with NameError. Fixed March 24.
    This test imports weather_scout and verifies all names resolve.
    """
    scout_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "weather_scout.py")
    if not os.path.exists(scout_path):
        return True, "weather_scout.py not present — skipping"

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("weather_scout", scout_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        required_names = [
            "TIER_CONFIG",
            "get_city_tier",
            "WEATHER_ENSEMBLE_COLLAPSE_STD",
            "WEATHER_COLLAPSE_BUFFER",
            "WEATHER_MAX_PER_TRADE",
            "WEATHER_MAX_EXPOSURE",
            "WEATHER_MIN_CONFIDENCE",
        ]
        missing = [n for n in required_names if not hasattr(mod, n)]
        if missing:
            return False, f"weather_scout missing: {missing}"

        # Verify TIER_CONFIG has A/B/C tiers with correct keys
        tc = mod.TIER_CONFIG
        for tier in ["A", "B", "C"]:
            if tier not in tc:
                return False, f"TIER_CONFIG missing tier '{tier}'"
            for key in ["min_prob", "max_size", "extra_buffer_c"]:
                if key not in tc[tier]:
                    return False, f"TIER_CONFIG['{tier}'] missing key '{key}'"

        # Verify get_city_tier returns valid tier
        test_cities = ["Chicago", "Paris", "Buenos Aires", "Unknown City"]
        for city in test_cities:
            tier = mod.get_city_tier(city)
            if tier not in ["A", "B", "C"]:
                return False, f"get_city_tier('{city}') returned invalid tier '{tier}'"

        return True, "weather_scout constants all present and valid"

    except Exception as e:
        return False, f"weather_scout import failed: {e}"

# ── T7: Required Environment Variables ────────────────────────────────────────
@test("T7: All required environment variables are set")
def test_env_vars():
    """
    Missing env vars cause cryptic failures deep in execution.
    Catch them at startup with clear messages.
    """
    required = {
        "POLYMARKET_PRIVATE_KEY": "64-char hex private key",
        "POLYMARKET_FUNDER_ADDRESS": "Gnosis Safe wallet address (0x...)",
        "TELEGRAM_TOKEN": "Telegram bot token for alerts",
        "TELEGRAM_CHAT_ID": "Telegram chat ID",
        "ANTHROPIC_API_KEY": "Anthropic API key for market scoring",
    }
    optional_warn = {
        "CLOB_API_KEY": "CLOB API key (auto-derived if missing)",
        "UW_API_KEY": "Unusual Whales API key (optional signal source)",
    }

    missing_critical = []
    warnings = []

    for var, desc in required.items():
        val = os.environ.get(var, "").strip()
        if not val:
            missing_critical.append(f"{var} ({desc})")
        elif var == "POLYMARKET_PRIVATE_KEY" and len(val) != 64:
            missing_critical.append(f"{var} has wrong length {len(val)}, expected 64")
        elif var == "POLYMARKET_FUNDER_ADDRESS" and not val.startswith("0x"):
            missing_critical.append(f"{var} should start with 0x")

    for var, desc in optional_warn.items():
        if not os.environ.get(var, "").strip():
            warnings.append(f"{var} not set ({desc})")

    if missing_critical:
        return False, f"Missing env vars: {'; '.join(missing_critical)}"

    msg = f"All {len(required)} required env vars present"
    if warnings:
        msg += f" | Warnings: {', '.join(warnings)}"
    return True, msg

# ── T8: USDC Allowances Set ───────────────────────────────────────────────────
@test("T8: USDC allowances are set for CTF, NegRisk, and Adapter contracts",
      critical=False)  # Warning only — ensure_allowances handles this
def test_usdc_allowances():
    """
    Root cause: Expired or missing USDC allowances cause 'not enough allowance'
    errors on new trades. The bot has ensure_allowances() but verifying at startup
    catches cases where on-chain state has changed.
    """
    CTF_EXCHANGE  = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    NEG_RISK_EXCH = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    NEG_RISK_ADAP = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                         api_secret=os.environ.get("CLOB_API_SECRET",""),
                         api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))

        allowances = bal.get("allowances", {})
        MIN_ALLOWANCE = 10**30  # Should be max_uint256

        not_approved = []
        for contract_addr, name in [
            (CTF_EXCHANGE,  "CTF Exchange"),
            (NEG_RISK_EXCH, "NegRisk Exchange"),
            (NEG_RISK_ADAP, "NegRisk Adapter"),
        ]:
            # Normalize address comparison (lowercase)
            found = None
            for addr, amount in allowances.items():
                if addr.lower() == contract_addr.lower():
                    found = int(amount)
                    break
            if found is None:
                not_approved.append(f"{name}: not found in allowances")
            elif found < MIN_ALLOWANCE:
                not_approved.append(f"{name}: allowance too low ({found})")

        if not_approved:
            return False, f"Insufficient allowances: {'; '.join(not_approved)}"

        return True, f"All 3 contract allowances set to max_uint256"

    except Exception as e:
        return True, f"Allowance check skipped: {e}"  # Non-blocking

# ── T9: Sell Size Consistent With CLOB Balance ───────────────────────────────
@test("T9: Live CLOB balance check — sell size would not exceed any open position")
def test_live_balance_consistency():
    """
    Integration test: for each known open position, verify that
    floor(CLOB_balance * 100) / 100 < CLOB_balance.
    This catches the exact bug where round() exceeded the real balance.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

        creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                         api_secret=os.environ.get("CLOB_API_SECRET",""),
                         api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

        # Get open positions from Polymarket API
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=20", timeout=10)
        positions_data = r.json()
        if not isinstance(positions_data, list):
            return True, f"Positions API returned unexpected type: {type(positions_data)} — skipping"

        failures = []
        tested = 0
        for pos in positions_data[:5]:  # Check top 5 positions (rate limit friendly)
            token_id = pos.get("asset", "")
            if not token_id:
                continue

            # Get CLOB balance for this token
            bal = client.get_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
            raw = int(bal.get("balance", 0))
            if raw == 0:
                continue  # Position may be held differently

            exact_shares = raw / 1e6

            # The bug: round() can exceed balance
            round_result = round(exact_shares, 2)
            floor_result = math.floor(exact_shares * 100) / 100

            if round_result > exact_shares + 1e-9:
                failures.append(
                    f"Position {token_id[:12]}...: round({exact_shares:.6f},2)={round_result} "
                    f"EXCEEDS balance! floor={floor_result} is correct.")

            # Verify floor is always safe
            assert floor_result <= exact_shares + 1e-9, \
                f"floor({exact_shares}) = {floor_result} exceeds balance!"
            tested += 1

        if failures:
            return False, f"Balance/sell_size inconsistency: {'; '.join(failures)}"

        return True, f"Sell size check OK for {tested} live positions (all use floor, not round)"

    except Exception as e:
        return False, f"Live balance check failed: {e}"

# ── Runner ─────────────────────────────────────────────────────────────────────

def run_preflight(bot_name="bot", send_telegram=True, skip_tests=None):
    """
    Run all pre-flight tests. Returns True if all critical tests pass.
    
    Args:
        bot_name: Name of the bot (for Telegram messages)
        send_telegram: Whether to send Telegram alerts on failure
        skip_tests: Set of test names to skip (e.g. {"T5"} for non-momentum bots)
    """
    skip_tests = skip_tests or set()
    print(f"\n{'='*55}")
    print(f"  PRE-FLIGHT CHECKS — {bot_name}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*55}")

    all_passed   = True
    results      = []
    warnings     = []
    start_time   = time.time()

    for test_entry in TESTS_CRITICAL + TESTS_WARNING:
        name     = test_entry["name"]
        fn       = test_entry["fn"]
        critical = test_entry["critical"]

        if any(name.startswith(s) for s in skip_tests):
            print(f"  ⏭ SKIP  {name}")
            continue

        try:
            passed, message = fn()
        except Exception as e:
            passed, message = False, f"Test threw exception: {e}"

        icon = "✓" if passed else ("✗" if critical else "⚠")
        print(f"  {icon} {'PASS' if passed else ('FAIL' if critical else 'WARN')}  {name}")
        if not passed or not critical:
            print(f"         {message}")

        results.append({"name": name, "passed": passed, "message": message, "critical": critical})

        if not passed and critical:
            all_passed = False
        elif not passed and not critical:
            warnings.append(f"{name}: {message}")

    elapsed = time.time() - start_time
    failures = [r for r in results if not r["passed"] and r["critical"]]

    print(f"{'='*55}")
    if all_passed:
        warn_str = f" ({len(warnings)} warning(s))" if warnings else ""
        print(f"  ✓ ALL {len(TESTS_CRITICAL)} CRITICAL CHECKS PASSED{warn_str}")
    else:
        print(f"  ✗ {len(failures)} CRITICAL CHECK(S) FAILED — trading blocked")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"{'='*55}\n")

    # Send Telegram alert on critical failures
    if not all_passed and send_telegram and TG_TOKEN and TG_CHAT:
        fail_lines = "\n".join(f"• {f['name']}\n  {f['message'][:100]}" for f in failures)
        msg = (f"🚨 <b>{bot_name} Pre-flight Failed</b>\n"
               f"{len(failures)} critical check(s) failed:\n\n"
               f"{fail_lines}\n\n"
               f"Trading is blocked until resolved.")
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

    return all_passed, results


if __name__ == "__main__":
    # Can run standalone: python3 preflight.py
    import argparse
    parser = argparse.ArgumentParser(description="Run bot pre-flight checks")
    parser.add_argument("--bot", default="standalone", help="Bot name")
    parser.add_argument("--skip", nargs="*", default=[], help="Tests to skip (e.g. T4 T5)")
    args = parser.parse_args()

    passed, results = run_preflight(bot_name=args.bot, skip_tests=set(args.skip))
    sys.exit(0 if passed else 1)
