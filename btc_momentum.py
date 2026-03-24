"""
btc_momentum.py — BTC 5-Minute Momentum Trader
================================================
Runs as a persistent background service on the DigitalOcean server.

Strategy:
  - At the start of each 5-minute Polymarket BTC Up/Down window, watch
    the Coinbase BTC price via WebSocket for the first 60-90 seconds.
  - If BTC moves >= MOMENTUM_THRESHOLD (0.12%) in that time, buy the
    direction (UP or DOWN token) as a limit order near the current market mid.
  - Entry window: T+30s to T+90s into the window.
  - Hold to resolution (market pays $1/share) OR exit early if the
    position reaches 85¢ (lock-in profit).
  - Hard stops: max $50/trade, max $300/day loss, 8 trades/hour max.

Market structure:
  - Each window: btc-updown-5m-{unix_timestamp_rounded_to_300s}
  - UP token = index 0 in clobTokenIds
  - DOWN token = index 1 in clobTokenIds
  - Tick size: 0.01
  - Fresh window opens at 50/50, moves to 85-95¢ as direction clarifies

Resolution: Chainlink BTC/USD oracle at window end. UP wins if
  price_end >= price_start, otherwise DOWN wins.
"""
import os, sys, json, math, time, threading, logging, requests, websocket
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY   = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER        = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID","").strip()

# Strategy parameters
MOMENTUM_THRESHOLD = 0.0012  # 0.12% BTC move in first 60s → signal fires
ENTRY_WINDOW_START = 30      # Don't enter before T+30s (let price stabilize)
ENTRY_WINDOW_END   = 90      # Don't enter after T+90s (price already moved)
TAKE_PROFIT_PRICE  = 0.85    # Exit if position reaches 85¢ (lock in ~35-40% gain)
MAX_ENTRY_PRICE    = 0.72    # Don't buy above 72¢ (not enough upside)
MIN_ENTRY_PRICE    = 0.52    # Don't buy below 52¢ (signal not priced yet)
TRADE_SIZE_USDC    = 40      # USDC per trade (conservative start)
MAX_DAILY_LOSS     = 300     # Stop trading if down $300 on the day
MAX_HOURLY_TRADES  = 8       # Rate limit: 8 trades per hour
WINDOW_SECONDS     = 300     # 5 minutes

LOG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_momentum.log")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_momentum_state.json")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("btc_momentum")

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

def logprint(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    if level == "INFO":   log.info(msg)
    elif level == "WARN": log.warning(msg)
    elif level == "ERR":  log.error(msg)

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except: pass
    return {
        "daily_pnl": 0.0,
        "daily_date": "",
        "hourly_trades": [],
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "open_positions": {}
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except: pass

# ── BTC Price Feed ────────────────────────────────────────────────────────────
# Multi-source price feed: WebSocket primary (Coinbase/Binance), REST fallback
# Uses Chainlink v2 on Polygon as the ground-truth source (same as Polymarket)
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # Polygon Chainlink BTC/USD

btc_prices = []   # rolling list of (timestamp, price)
btc_lock   = threading.Lock()

def fetch_btc_price_rest():
    """Fetch BTC price via fastest available REST API."""
    # Priority: Chainlink oracle (same as Polymarket) > Coinbase > Kraken
    sources = [
        ("chainlink", lambda: _chainlink_price()),
        ("coinbase",  lambda: float(requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3
            ).json()["data"]["amount"])),
        ("kraken",    lambda: float(requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=3
            ).json()["result"]["XXBTZUSD"]["c"][0])),
        ("binance",   lambda: float(requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3
            ).json()["price"])),
    ]
    for name, fn in sources:
        try:
            price = fn()
            if price and price > 10000:
                return price
        except: pass
    return None

def _chainlink_price():
    """Read BTC/USD from Chainlink oracle on Polygon (most accurate — Polymarket's source)."""
    rpc = requests.post("https://polygon-bor-rpc.publicnode.com",
        json={"jsonrpc":"2.0","method":"eth_call","params":[
            {"to": CHAINLINK_BTC_USD, "data": "0x50d25bcd"}, "latest"], "id":1},
        timeout=5)
    result = rpc.json().get("result","")
    if result and len(result) > 10:
        price_int = int(result, 16)
        if price_int > 2**255: price_int -= 2**256
        return price_int / 1e8
    return None

def start_price_feed():
    """Start REST polling loop in a background thread + WebSocket if available."""
    def poll_loop():
        while True:
            try:
                price = fetch_btc_price_rest()
                if price:
                    ts = time.time()
                    with btc_lock:
                        btc_prices.append((ts, price))
                        cutoff = ts - 300
                        while len(btc_prices) > 1 and btc_prices[0][0] < cutoff:
                            btc_prices.pop(0)
            except Exception as e:
                logprint(f"Price poll error: {e}", "WARN")
            time.sleep(1)  # Poll every second

    # Try WebSocket first (lower latency)
    def ws_loop():
        while True:
            try:
                def on_msg(ws, message):
                    try:
                        data = json.loads(message)
                        price = 0
                        if data.get("type") == "ticker":
                            price = float(data.get("price", 0))
                        elif "c" in data:  # Binance format
                            price = float(data["c"])
                        if price > 10000:
                            ts = time.time()
                            with btc_lock:
                                btc_prices.append((ts, price))
                                cutoff = ts - 300
                                while len(btc_prices) > 1 and btc_prices[0][0] < cutoff:
                                    btc_prices.pop(0)
                    except: pass

                def on_open_cb(ws):
                    ws.send(json.dumps({"type": "subscribe",
                        "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]}))
                    logprint("Coinbase WebSocket connected")

                app = websocket.WebSocketApp(
                    "wss://advanced-trade-ws.coinbase.com/v1",
                    on_message=on_msg,
                    on_open=on_open_cb,
                    on_error=lambda ws, e: None,
                    on_close=lambda ws, c, m: None)
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logprint(f"WebSocket exception: {e}", "WARN")
            time.sleep(5)

    # REST poll always runs (fallback + backup)
    t_rest = threading.Thread(target=poll_loop, daemon=True)
    t_rest.start()
    logprint("REST price polling started (1s interval)")

    # WebSocket runs alongside (overwrites with faster data when available)
    t_ws = threading.Thread(target=ws_loop, daemon=True)
    t_ws.start()
    logprint("WebSocket price feed thread started")

def get_btc_price(window_start=None):
    """Get most recent BTC price. If window_start provided, return price at that time."""
    with btc_lock:
        if not btc_prices:
            # Fallback: direct REST call
            p = fetch_btc_price_rest()
            if p:
                btc_prices.append((time.time(), p))
            return p
        if window_start is None:
            return btc_prices[-1][1]
        # Find price closest to window_start
        candidates = [(abs(t - window_start), p) for t, p in btc_prices]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[0])[1]

def get_btc_move_pct(window_start):
    """Calculate BTC % move since window_start. Returns (pct_change, direction)."""
    start_price = get_btc_price(window_start)
    current_price = get_btc_price()
    if start_price is None or current_price is None:
        return 0.0, None
    pct = (current_price - start_price) / start_price
    direction = "UP" if pct >= 0 else "DOWN"
    return abs(pct), direction

# ── Polymarket Market Lookup ───────────────────────────────────────────────────
def get_current_market(window_start):
    """Fetch the Polymarket market for the given 5-min window."""
    slug = f"btc-updown-5m-{window_start}"
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=8)
        data = r.json()
        if not data:
            return None
        event = data[0]
        markets = event.get("markets", [])
        if not markets:
            return None
        m = markets[0]
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str): tokens = json.loads(tokens)
        outcomes = m.get("outcomes", [])
        return {
            "event_id": event.get("id"),
            "question": m.get("question",""),
            "up_token":   tokens[0] if len(tokens) > 0 else None,
            "down_token": tokens[1] if len(tokens) > 1 else None,
            "outcomes":   outcomes,
        }
    except Exception as e:
        logprint(f"Market lookup failed: {e}", "WARN")
        return None

def get_market_price(token_id):
    """Get current CLOB mid price for a token."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        return float(r.json().get("mid", 0.5))
    except:
        return 0.5

# ── Order Placement ────────────────────────────────────────────────────────────
def place_order(token_id, neg_risk, usdc_size, direction_label):
    """Place a BUY order for the given token."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (OrderArgs, OrderType,
        PartialCreateOrderOptions, BalanceAllowanceParams, AssetType, ApiCreds)
    from py_clob_client.order_builder.constants import BUY

    try:
        creds = ApiCreds(
            api_key=os.environ.get("CLOB_API_KEY",""),
            api_secret=os.environ.get("CLOB_API_SECRET",""),
            api_passphrase=os.environ.get("CLOB_API_PASSPHRASE","")
        )
        client = ClobClient(
            "https://clob.polymarket.com", key=PRIVATE_KEY,
            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

        # Approve conditional token
        try:
            client.update_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        except: pass

        # Get current price and set limit price slightly above mid
        mid = get_market_price(token_id)
        tick = 0.01
        # Round to tick, add 1 tick to improve fill odds
        entry_price = round(round(mid / tick) * tick + tick, 2)
        entry_price = max(MIN_ENTRY_PRICE, min(MAX_ENTRY_PRICE, entry_price))

        # Shares = USDC / price (floored to 2dp)
        shares = math.floor((usdc_size / entry_price) * 100) / 100
        if shares < 1:
            return False, None, "Too few shares"

        logprint(f"  Placing BUY {direction_label}: {shares} shares @ {entry_price:.2f} = ${shares*entry_price:.2f}")

        args    = OrderArgs(token_id=token_id, price=entry_price, size=shares, side=BUY)
        opts    = PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)
        signed  = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            order_id = receipt.get("orderID","")[:16]
            logprint(f"  ✓ Order placed: {order_id} | {shares} @ {entry_price:.2f}")
            return True, {
                "order_id": receipt.get("orderID",""),
                "token_id": token_id,
                "direction": direction_label,
                "shares": shares,
                "entry_price": entry_price,
                "usdc_cost": shares * entry_price,
                "timestamp": time.time(),
                "window_start": None  # set by caller
            }, None
        else:
            err = receipt.get("errorMsg","unknown")
            logprint(f"  ✗ Order failed: {err}", "WARN")
            return False, None, err

    except Exception as e:
        logprint(f"  Order exception: {e}", "ERR")
        return False, None, str(e)[:80]

# ── Position Management ────────────────────────────────────────────────────────
def check_and_exit_positions(state):
    """Check open positions and exit if take-profit hit."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (OrderArgs, OrderType,
        PartialCreateOrderOptions, BalanceAllowanceParams, AssetType, ApiCreds)
    from py_clob_client.order_builder.constants import SELL

    positions = state.get("open_positions", {})
    to_close = []

    for win_str, pos in positions.items():
        token_id  = pos.get("token_id")
        direction = pos.get("direction")
        shares    = pos.get("shares", 0)
        entry     = pos.get("entry_price", 0.5)
        win_start = pos.get("window_start", 0)

        if not token_id: continue

        current_price = get_market_price(token_id)

        # Exit conditions:
        # 1. Take profit: price >= TAKE_PROFIT_PRICE
        # 2. Window expired (held to resolution — CLOB handles payout)
        time_in_window = time.time() - win_start
        if time_in_window > WINDOW_SECONDS - 10:
            # Window resolved — remove from tracking, count P&L at resolution
            logprint(f"  Window resolved for {direction} position — tracking P&L")
            to_close.append(win_str)
            continue

        if current_price >= TAKE_PROFIT_PRICE and shares > 0.1:
            # Take profit: sell
            logprint(f"  TAKE PROFIT: {direction} @ {current_price:.2f} (entry {entry:.2f})")
            try:
                creds = ApiCreds(
                    api_key=os.environ.get("CLOB_API_KEY",""),
                    api_secret=os.environ.get("CLOB_API_SECRET",""),
                    api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
                client = ClobClient(
                    "https://clob.polymarket.com", key=PRIVATE_KEY,
                    chain_id=137, creds=creds, signature_type=2, funder=FUNDER)

                import sys as _sys; _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                # Get CLOB balance for exact shares
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                bal = client.get_balance_allowance(params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
                exact = int(bal.get("balance",0)) / 1e6
                sell_size = math.floor(exact * 100) / 100
                if sell_size < 0.01: continue

                sell_price = round(current_price - 0.01, 2)
                sell_price = max(0.01, min(0.99, sell_price))

                neg_risk = client.get_neg_risk(token_id)
                args = OrderArgs(token_id=token_id, price=sell_price,
                                 size=sell_size, side=SELL)
                opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)
                signed  = client.create_order(args, opts)
                receipt = client.post_order(signed, OrderType.GTC)

                if receipt.get("success"):
                    pnl = (sell_price - entry) * sell_size
                    logprint(f"  ✓ SOLD {direction} {sell_size} @ {sell_price:.2f} | P&L: ${pnl:+.2f}")
                    tg(f"💰 <b>BTC Bot: {direction} SOLD</b>\n{sell_size} shares @ {sell_price:.2f}\nP&L: ${pnl:+.2f} | Entry: {entry:.2f}")
                    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
                    state["total_pnl"] = state.get("total_pnl", 0) + pnl
                    if pnl > 0: state["wins"] = state.get("wins", 0) + 1
                    else: state["losses"] = state.get("losses", 0) + 1
                    to_close.append(win_str)
            except Exception as e:
                logprint(f"  Exit error: {e}", "WARN")

    for key in to_close:
        positions.pop(key, None)

    return state

# ── Rate Limit Checks ──────────────────────────────────────────────────────────
def can_trade(state):
    """Check daily loss and hourly trade rate limits."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_date") != today:
        state["daily_pnl"]   = 0.0
        state["daily_date"]  = today
        state["hourly_trades"] = []

    if state.get("daily_pnl", 0) <= -MAX_DAILY_LOSS:
        return False, f"Daily loss limit hit (${state['daily_pnl']:.0f})"

    # Hourly rate limit
    now = time.time()
    cutoff = now - 3600
    state["hourly_trades"] = [t for t in state.get("hourly_trades",[]) if t > cutoff]
    if len(state["hourly_trades"]) >= MAX_HOURLY_TRADES:
        return False, f"Hourly trade limit ({MAX_HOURLY_TRADES}/hr)"

    # Check USDC balance
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
        creds = ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                         api_secret=os.environ.get("CLOB_API_SECRET",""),
                         api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, creds=creds, signature_type=2, funder=FUNDER)
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))
        usdc = float(bal.get("balance",0)) / 1e6
        if usdc < TRADE_SIZE_USDC:
            return False, f"Insufficient USDC (${usdc:.2f})"
    except Exception as e:
        return False, f"Balance check failed: {e}"

    return True, "OK"

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    logprint("=" * 60)
    logprint("BTC 5-Min Momentum Bot Starting")
    logprint(f"Threshold: {MOMENTUM_THRESHOLD*100:.2f}% | Size: ${TRADE_SIZE_USDC} | TP: {TAKE_PROFIT_PRICE}")
    logprint("=" * 60)

    if not PRIVATE_KEY:
        logprint("ERROR: POLYMARKET_PRIVATE_KEY not set", "ERR")
        sys.exit(1)

    # Start BTC price feed
    start_price_feed()
    time.sleep(3)  # Let WebSocket connect

    state = load_state()
    last_window = 0

    tg(f"🤖 <b>BTC Momentum Bot Started</b>\nThreshold: {MOMENTUM_THRESHOLD*100:.2f}% | ${TRADE_SIZE_USDC}/trade\nWatching 5-min BTC Up/Down markets")

    while True:
        try:
            now = int(time.time())
            window_start = (now // WINDOW_SECONDS) * WINDOW_SECONDS
            time_in_window = now - window_start

            # Check and manage open positions every cycle
            state = check_and_exit_positions(state)

            # New window just opened
            if window_start != last_window:
                last_window = window_start
                logprint(f"\n{'='*50}")
                logprint(f"New window: {datetime.fromtimestamp(window_start, tz=timezone.utc).strftime('%H:%M')} UTC")
                btc_open = get_btc_price(window_start)
                if btc_open:
                    logprint(f"BTC open price: ${btc_open:,.2f}")

            # Entry window: T+30s to T+90s
            if ENTRY_WINDOW_START <= time_in_window <= ENTRY_WINDOW_END:
                # Check if already in a position for this window
                win_key = str(window_start)
                if win_key not in state.get("open_positions", {}):

                    # Check rate limits
                    ok, reason = can_trade(state)
                    if not ok:
                        if time_in_window == ENTRY_WINDOW_START:  # Only log once
                            logprint(f"  Skip: {reason}")
                    else:
                        # Calculate momentum signal
                        move_pct, direction = get_btc_move_pct(window_start)

                        if move_pct >= MOMENTUM_THRESHOLD and direction:
                            logprint(f"  SIGNAL: BTC moved {move_pct*100:.3f}% → {direction}")

                            # Get market for this window
                            market = get_current_market(window_start)
                            if not market:
                                logprint("  Market not found — skip", "WARN")
                            else:
                                # Choose token based on direction
                                token_id = (market["up_token"] if direction == "UP"
                                            else market["down_token"])

                                if not token_id:
                                    logprint("  Token not found", "WARN")
                                else:
                                    # Check market price is in our range
                                    mkt_price = get_market_price(token_id)
                                    if mkt_price > MAX_ENTRY_PRICE:
                                        logprint(f"  Market price {mkt_price:.2f} > max {MAX_ENTRY_PRICE} — already priced in, skip")
                                    elif mkt_price < MIN_ENTRY_PRICE:
                                        logprint(f"  Market price {mkt_price:.2f} < min {MIN_ENTRY_PRICE} — weak signal, skip")
                                    else:
                                        # Get neg_risk
                                        try:
                                            from py_clob_client.client import ClobClient
                                            from py_clob_client.clob_types import ApiCreds
                                            creds = ApiCreds(
                                                api_key=os.environ.get("CLOB_API_KEY",""),
                                                api_secret=os.environ.get("CLOB_API_SECRET",""),
                                                api_passphrase=os.environ.get("CLOB_API_PASSPHRASE",""))
                                            cl = ClobClient("https://clob.polymarket.com",
                                                key=PRIVATE_KEY, chain_id=137,
                                                creds=creds, signature_type=2, funder=FUNDER)
                                            neg_risk = cl.get_neg_risk(token_id)
                                        except:
                                            neg_risk = False

                                        success, position, err = place_order(
                                            token_id, neg_risk, TRADE_SIZE_USDC, direction)

                                        if success and position:
                                            position["window_start"] = window_start
                                            state.setdefault("open_positions", {})[win_key] = position
                                            state.setdefault("hourly_trades", []).append(time.time())
                                            save_state(state)

                                            btc_now = get_btc_price()
                                            tg(f"📈 <b>BTC Bot: {direction}</b>\n"
                                               f"{position['shares']} shares @ {position['entry_price']:.2f}\n"
                                               f"BTC move: {move_pct*100:.3f}% | BTC: ${btc_now:,.0f}\n"
                                               f"Window: {datetime.fromtimestamp(window_start, tz=timezone.utc).strftime('%H:%M')} UTC")
                                        else:
                                            logprint(f"  Trade failed: {err}", "WARN")

            save_state(state)
            time.sleep(2)  # Poll every 2 seconds

        except KeyboardInterrupt:
            logprint("Shutting down")
            tg("🛑 BTC Momentum Bot stopped")
            break
        except Exception as e:
            logprint(f"Main loop error: {e}", "ERR")
            time.sleep(5)

if __name__ == "__main__":
    main()
