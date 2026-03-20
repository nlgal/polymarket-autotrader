#!/usr/bin/env python3
"""
Polymarket Auto-Trader  —  v2 with Rule-Based Control Plane
============================================================
Control plane sits above all trade signals and adjusts risk budgets,
position limits, and allowed strategies based on equity state.

Modes (driven by drawdown from peak equity):
  NORMAL     — default, balanced risk
  RECOVERY   — drawdown >= 10%, tighter limits, no speculative trades
  EXPANSION  — new equity high >= checkpoint * 1.10, larger limits
  PAUSED     — drawdown >= 20% OR daily hard stop hit; no new trades

State persists to state.json so it survives reboots.
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone, date

import requests
from colorama import Fore, Style, init
from dotenv import load_dotenv

# ── Load env from explicit path so launchd can find it ────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)
init()

# ── Telegram Notifications ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def tg(msg: str, silent: bool = False):
    """Send a Telegram message. Fails silently so it never breaks the agent."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_notification": silent,
            },
            timeout=5,
        )
    except Exception:
        pass

# ── Static Config ─────────────────────────────────────────────────────────────

# ── Intelligence System ───────────────────────────────────────────────────────
INTEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intelligence")

_INTEL_CACHE: dict = {"soul": None, "lessons": None}

def _read_intel_files():
    """Read soul.md + lessons.md from disk into cache. Call once per cycle."""
    try:
        p = os.path.join(INTEL_DIR, "soul.md")
        txt = open(p).read() if os.path.exists(p) else ""
        lines = [l.strip() for l in txt.splitlines() if l.strip() and l.strip()[0].isdigit()]
        _INTEL_CACHE["soul"] = "\n".join(lines[:10])
    except Exception:
        _INTEL_CACHE["soul"] = ""
    try:
        p = os.path.join(INTEL_DIR, "lessons.md")
        txt = open(p).read() if os.path.exists(p) else ""
        lines = [l.strip() for l in txt.splitlines()
                 if l.strip() and not l.strip().startswith("#") and len(l.strip()) > 20]
        _INTEL_CACHE["lessons"] = "\n".join(lines[:12])
    except Exception:
        _INTEL_CACHE["lessons"] = ""

def load_lessons() -> str:
    if _INTEL_CACHE["lessons"] is None: _read_intel_files()
    return _INTEL_CACHE["lessons"] or ""

def load_soul() -> str:
    if _INTEL_CACHE["soul"] is None: _read_intel_files()
    return _INTEL_CACHE["soul"] or ""

def log_mistake(category: str, what: str, why: str, rule: str):
    """Append a mistake to intelligence/mistakes.md and notify via Telegram."""
    try:
        os.makedirs(INTEL_DIR, exist_ok=True)
        path = os.path.join(INTEL_DIR, "mistakes.md")
        from datetime import datetime as _dt
        ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        existing = open(path).read() if os.path.exists(path) else ""
        mid = existing.count("## M") + 1
        entry = (
            "\n## M{:03d} — {}\n"
            "**Time:** {}\n"
            "**What:** {}\n"
            "**Why:** {}\n"
            "**Rule:** {}\n\n---\n"
        ).format(mid, category, ts, what[:200], why[:200], rule[:200])
        with open(path, "a") as fh:
            fh.write(entry)
        tg("\U0001f4dd Mistake logged M{:03d}: {}".format(mid, category), silent=True)
    except Exception:
        pass

def review_patterns():
    """Scan mistakes.md for recurring patterns and update lessons.md."""
    try:
        mistakes_path = os.path.join(INTEL_DIR, "mistakes.md")
        lessons_path  = os.path.join(INTEL_DIR, "lessons.md")
        if not os.path.exists(mistakes_path):
            return
        mistakes_txt = open(mistakes_path).read()
        lessons_txt  = open(lessons_path).read() if os.path.exists(lessons_path) else ""
        pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        if not pplx_key:
            return
        import requests as _req
        from datetime import datetime as _dt
        resp = _req.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": "sonar",
                "messages": [{
                    "role": "user",
                    "content": (
                        "Analyze this trading agent mistake log. Find recurring patterns "
                        "(same root cause 2+ times). Suggest 1-3 new lessons not already "
                        "in the current list. Format: NEW LESSON: [title] | [rule]\n\n"
                        "MISTAKES:\n" + mistakes_txt[-3000:] +
                        "\n\nCURRENT LESSONS:\n" + lessons_txt[-1000:]
                    )
                }],
                "max_tokens": 400,
                "temperature": 0.2,
            },
            headers={"Authorization": "Bearer " + pplx_key, "Content-Type": "application/json"},
            timeout=30,
        )
        analysis = resp.json()["choices"][0]["message"]["content"]
        if "NEW LESSON:" in analysis:
            with open(lessons_path, "a") as fh:
                fh.write("\n\n---\n## Auto-generated — {}\n{}\n".format(
                    _dt.utcnow().strftime("%Y-%m-%d"), analysis))
            log("[INTELLIGENCE] Pattern review — new lessons added", Fore.MAGENTA)
            tg("\U0001f9e0 Intelligence update: new lessons added\n" + analysis[:200])
    except Exception as e:
        log("[INTELLIGENCE] Pattern review failed: {}".format(e), Fore.YELLOW)

SCAN_INTERVAL_SECONDS  = 15 * 60
NEWS_SCAN_INTERVAL     = 5 * 60   # News arb check every 5 minutes
NEWS_ARB_MIN_EDGE      = 0.12     # Higher bar for news arb trades (more confident)
NEWS_ARB_SIZE_MULT     = 1.5      # Size up news arb trades vs normal
MIN_EDGE              = 0.07
MIN_CONFIDENCE        = "high"
MARKETS_FETCH_LIMIT   = 200   # Total markets to pull per scan (by volume)
TOP_MARKETS_TO_SCORE  = 30   # How many top-volume to score with AI
ORDER_TTL_MINUTES     = 20
PROFIT_TARGET         = 0.80
STOP_LOSS             = 0.35
NEAR_RESOLUTION_THRESHOLD = 0.94
MAX_PER_MARKET_USDC   = 200   # Never put more than $200 into a single market
MIN_FREE_BALANCE      = 20    # Always keep $20 free (Polymarket minimum)

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com/markets"
FUNDER     = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
UW_API_KEY  = os.environ.get("UW_API_KEY", "").strip()
UW_API_BASE = "https://api.unusualwhales.com/api"

LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.log")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ── Control Plane Parameters (Balanced profile) ───────────────────────────────
#
#   Mode        Risk/trade   Max open risk   Max orders  Trade size range
#   NORMAL       1.0% E       3.0% E           8          $50–$150
#   RECOVERY     0.5% E       1.5% E           4          $25–$75
#   EXPANSION    1.25% E      4.0% E           10         $75–$200
#   PAUSED       —            —                0          no new trades
#
RISK_TRADE_PCT  = {"NORMAL": 0.010, "RECOVERY": 0.005, "EXPANSION": 0.0125, "PAUSED": 0.0}
RISK_OPEN_PCT   = {"NORMAL": 0.030, "RECOVERY": 0.015, "EXPANSION": 0.040,  "PAUSED": 0.0}
MAX_ORDERS      = {"NORMAL": 8,     "RECOVERY": 4,     "EXPANSION": 10,     "PAUSED": 0}
# ── Market blacklist — skip markets with no real model edge ──────────────────
MARKET_BLACKLIST_KEYWORDS = [
    "tweets", "tweet", "retweet", "elon musk post",
    "how many times will elon", "posts from march", "# of tweets",
    "number of tweets", "followers", "subscribers",
    "youtube views", "tiktok", "instagram posts",
]

def is_blacklisted(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in MARKET_BLACKLIST_KEYWORDS)

SIZE_MIN        = {"NORMAL": 50,    "RECOVERY": 25,    "EXPANSION": 75,     "PAUSED": 0}
SIZE_MAX        = {"NORMAL": 150,   "RECOVERY": 75,    "EXPANSION": 200,    "PAUSED": 0}

# Drawdown thresholds
DD_RECOVERY   = 0.10   # enter Recovery if drawdown from peak >= 10%
DD_RESUME     = 0.05   # exit Recovery when drawdown drops back to <= 5%
DD_HARD_PAUSE = 0.20   # full pause at 20% drawdown

# Daily stop thresholds (% of start-of-day equity)
DAILY_SOFT_STOP = 0.02  # block new trades, allow closing only
DAILY_HARD_STOP = 0.03  # flatten + block until next day

# Expansion: new peak must be >= checkpoint * 1.10 AND held for N cycles
EXPANSION_STEP       = 0.10
EXPANSION_HOLD_CYCLES = 3   # ~45 min at 15-min interval


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, color=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"{color}{line}{Style.RESET_ALL}" if color else line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── State Persistence ─────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "mode": "NORMAL",
    "equity_peak_eod": None,       # high-watermark (end-of-day basis)
    "equity_sod": None,            # start-of-day equity snapshot
    "sod_date": None,              # date string for daily reset
    "expansion_checkpoint": None,  # equity level that unlocked Expansion
    "expansion_cycles_held": 0,    # consecutive cycles above checkpoint * 1.10
    "starting_bankroll": 1000.0,   # reference for absolute sizing floor
    "last_approval_date": None,    # date of last USDC allowance approval
}


# ── Auto-Approve USDC Allowances ──────────────────────────────────────────────

MAX_UINT256 = 2**256 - 1

def ensure_allowances(state):
    """
    Re-approve USDC allowances once per day at startup.
    Prevents 'not enough balance / allowance' errors.
    """
    today = date.today().isoformat()
    if state.get("last_approval_date") == today:
        return state  # Already approved today

    log("Checking/refreshing USDC allowances...", Fore.CYAN)
    try:
        from web3 import Web3

        # Try multiple RPC endpoints in case one is rate-limited
        rpc_urls = [
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://1rpc.io/matic",
            "https://polygon-rpc.com",
        ]
        w3 = None
        for rpc in rpc_urls:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue
        if w3 is None:
            log("All Polygon RPCs failed — skipping allowance check", Fore.YELLOW)
            return state

        private_key_hex = PRIVATE_KEY
        if len(private_key_hex) == 64:
            account = w3.eth.account.from_key(private_key_hex)
        else:
            log("Invalid private key format for allowance check", Fore.YELLOW)
            return state

        signer = account.address

        # USDC contract on Polygon
        USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        # Polymarket CTF Exchange
        CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
        # Polymarket Neg Risk CTF Exchange  
        NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
        # Neg Risk Adapter
        NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

        ERC20_ABI = [
            {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
             "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
             "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
             "name": "approve", "outputs": [{"name": "", "type": "bool"}],
             "stateMutability": "nonpayable", "type": "function"},
        ]

        usdc = w3.eth.contract(address=USDC, abi=ERC20_ABI)
        funder_addr = Web3.to_checksum_address(FUNDER) if FUNDER else signer
        THRESHOLD = MAX_UINT256 // 2  # Re-approve if below half of max

        approved_any = False
        for name, spender in [("CTF Exchange", CTF_EXCHANGE),
                               ("Neg Risk Exchange", NEG_RISK_EXCHANGE),
                               ("Neg Risk Adapter", NEG_RISK_ADAPTER)]:
            try:
                allowance = usdc.functions.allowance(funder_addr, spender).call()
                if allowance < THRESHOLD:
                    log(f"  Re-approving {name}...", Fore.YELLOW)
                    nonce = w3.eth.get_transaction_count(signer)
                    gas_price = w3.eth.gas_price
                    txn = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
                        "from": signer,
                        "nonce": nonce,
                        "gas": 100000,
                        "gasPrice": gas_price,
                        "chainId": 137,
                    })
                    signed_txn = w3.eth.account.sign_transaction(txn, private_key=private_key_hex)
                    raw = signed_txn.raw_transaction if hasattr(signed_txn, 'raw_transaction') else signed_txn.rawTransaction
                    tx_hash = w3.eth.send_raw_transaction(raw)
                    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    log(f"  ✓ {name} approved (tx: {tx_hash.hex()[:16]}...)", Fore.GREEN)
                    approved_any = True
                else:
                    log(f"  ✓ {name} allowance OK", Fore.WHITE)
            except Exception as e:
                log(f"  Could not check/approve {name}: {e}", Fore.YELLOW)

        state["last_approval_date"] = today
        if approved_any:
            log("USDC allowances refreshed.", Fore.GREEN)
        else:
            log("All USDC allowances already set.", Fore.WHITE)

    except ImportError as e:
        log(f"web3 import error — {e}", Fore.YELLOW)
    except Exception as e:
        log(f"Allowance check failed: {e}", Fore.YELLOW)
        import traceback; traceback.print_exc()

    return state

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            state = {**DEFAULT_STATE, **saved}
            return state
        except Exception as e:
            log(f"State load error (using defaults): {e}", Fore.YELLOW)
    return dict(DEFAULT_STATE)

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"State save error: {e}", Fore.YELLOW)


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


# ── Equity Estimation ─────────────────────────────────────────────────────────

def get_equity(client):
    """
    Equity = USDC cash balance only.
    Position values are not included because get_trades() returns all historical
    trades and cannot be reliably netted. The cash balance is the ground truth
    for how much is available to trade.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        balance_info = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
        return float(balance_info.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"Equity check failed: {e}", Fore.YELLOW)
        return None


def get_portfolio_stats(client):
    try:
        from py_clob_client.clob_types import OpenOrderParams
        orders = client.get_orders(OpenOrderParams())
        open_count = len(orders)
        deployed = sum(
            float(o["original_size"]) * float(o["price"])
            for o in orders
        )
        return {"open_orders": open_count, "deployed": deployed}
    except Exception as e:
        log(f"Portfolio check failed: {e}", Fore.YELLOW)
        return {"open_orders": 0, "deployed": 0}


# ── Control Plane ─────────────────────────────────────────────────────────────

def update_control_plane(state, equity_now):
    """
    Core state machine. Updates mode based on equity, drawdown, and daily P&L.
    Returns updated state + whether new trades are allowed.
    """
    today = date.today().isoformat()

    # ── Daily reset ───────────────────────────────────────────────────────────
    if state.get("sod_date") != today:
        log(f"New trading day — snapshotting start-of-day equity: ${equity_now:.2f}", Fore.CYAN)
        state["sod_date"] = today
        state["equity_sod"] = equity_now
        # Force re-approval on new day
        state["last_approval_date"] = None
        # Update EOD peak at start of new day
        if state["equity_peak_eod"] is None or equity_now > state["equity_peak_eod"]:
            state["equity_peak_eod"] = equity_now
            log(f"New all-time peak: ${equity_now:.2f}", Fore.GREEN)

    # ── Initialize peak if first run ──────────────────────────────────────────
    if state["equity_peak_eod"] is None:
        state["equity_peak_eod"] = equity_now
    if state["equity_sod"] is None:
        state["equity_sod"] = equity_now
    if state["expansion_checkpoint"] is None:
        state["expansion_checkpoint"] = equity_now
    if state["starting_bankroll"] is None:
        state["starting_bankroll"] = equity_now

    peak      = state["equity_peak_eod"]
    sod       = state["equity_sod"]
    checkpoint = state["expansion_checkpoint"]

    # ── Auto-repair inflated peak ─────────────────────────────────────────────
    # If peak is >25% above current equity, it's likely stale or inflated
    # (e.g. set during a session where positions were temporarily overvalued,
    # or after a manual deposit that skewed the numbers). Reset to current.
    if peak and equity_now > 0 and (peak / equity_now) > 1.25:
        log(f"⚙ AUTO-REPAIR: peak ${peak:.2f} is {((peak/equity_now)-1):.0%} above current equity ${equity_now:.2f} — resetting peak to current.", Fore.CYAN)
        state["equity_peak_eod"] = equity_now
        if state.get("peak_equity", 0) > equity_now * 1.25:
            state["peak_equity"] = equity_now
        if state.get("expansion_checkpoint", 0) > equity_now * 1.25:
            state["expansion_checkpoint"] = equity_now
        peak = equity_now
        checkpoint = equity_now

    drawdown   = 1.0 - (equity_now / peak) if peak > 0 else 0.0
    daily_pnl  = equity_now - sod
    daily_pnl_pct = daily_pnl / sod if sod > 0 else 0.0

    log(f"Equity: ${equity_now:.2f} | Peak: ${peak:.2f} | DD: {drawdown:.1%} | Daily P&L: ${daily_pnl:+.2f} ({daily_pnl_pct:+.1%})")

    allow_new_trades = True
    prev_mode = state["mode"]

    # ── Hard pause (20% drawdown) ─────────────────────────────────────────────
    if drawdown >= DD_HARD_PAUSE:
        state["mode"] = "PAUSED"
        allow_new_trades = False
        log(f"⛔ HARD PAUSE: drawdown {drawdown:.1%} >= {DD_HARD_PAUSE:.0%}. No new trades.", Fore.RED)

    # ── Daily hard stop (lost 3%+ today) ─────────────────────────────────────
    elif daily_pnl_pct <= -DAILY_HARD_STOP:
        state["mode"] = "PAUSED"
        allow_new_trades = False
        log(f"⛔ DAILY HARD STOP: lost {daily_pnl_pct:.1%} today (>${abs(daily_pnl):.2f}). No trades until tomorrow.", Fore.RED)

    # ── Daily soft stop (lost 2%+ today) — close only ─────────────────────────
    elif daily_pnl_pct <= -DAILY_SOFT_STOP:
        allow_new_trades = False
        log(f"⚠ DAILY SOFT STOP: lost {daily_pnl_pct:.1%} today. Closing only — no new entries.", Fore.YELLOW)
        # Keep current mode, just block new trades

    # ── Unblock from PAUSED if we're in a new day with recovered drawdown ─────
    elif state["mode"] == "PAUSED" and drawdown < DD_RECOVERY:
        state["mode"] = "NORMAL"
        log(f"✓ Resuming from PAUSED — drawdown recovered to {drawdown:.1%}", Fore.GREEN)

    # ── Recovery mode ─────────────────────────────────────────────────────────
    elif drawdown >= DD_RECOVERY:
        state["mode"] = "RECOVERY"
        state["expansion_cycles_held"] = 0
        log(f"⚠ RECOVERY MODE: drawdown {drawdown:.1%}", Fore.YELLOW)

    # ── Exit Recovery when drawdown drops back to <= 5% ───────────────────────
    elif state["mode"] == "RECOVERY" and drawdown <= DD_RESUME:
        state["mode"] = "NORMAL"
        log(f"✓ Exiting Recovery — drawdown recovered to {drawdown:.1%}", Fore.GREEN)

    # ── Expansion mode (new peak >= checkpoint * 1.10) ────────────────────────
    elif equity_now >= checkpoint * (1.0 + EXPANSION_STEP) and state["mode"] != "RECOVERY":
        state["expansion_cycles_held"] = state.get("expansion_cycles_held", 0) + 1
        if state["expansion_cycles_held"] >= EXPANSION_HOLD_CYCLES:
            state["mode"] = "EXPANSION"
            state["expansion_checkpoint"] = equity_now
            log(f"🚀 EXPANSION MODE: equity ${equity_now:.2f} is {((equity_now/checkpoint)-1):.0%} above checkpoint. Unlocking larger sizes.", Fore.GREEN)
        else:
            log(f"  Near expansion (cycle {state['expansion_cycles_held']}/{EXPANSION_HOLD_CYCLES})…", Fore.CYAN)

    # ── Revert Expansion to Normal if peak not extended ───────────────────────
    elif state["mode"] == "EXPANSION" and equity_now < checkpoint:
        state["mode"] = "NORMAL"
        state["expansion_cycles_held"] = 0
        log(f"Stepped back from Expansion to Normal (equity below checkpoint).", Fore.YELLOW)

    # ── Normal ────────────────────────────────────────────────────────────────
    else:
        if state["mode"] not in ("RECOVERY", "EXPANSION"):
            state["mode"] = "NORMAL"
        state["expansion_cycles_held"] = state.get("expansion_cycles_held", 0)

    # Update EOD peak intraday if equity rose
    if equity_now > state["equity_peak_eod"]:
        state["equity_peak_eod"] = equity_now

    if state["mode"] != prev_mode:
        log(f"MODE CHANGE: {prev_mode} → {state['mode']}", Fore.CYAN)

    return state, allow_new_trades


# ── Scanner ───────────────────────────────────────────────────────────────────

def fetch_markets_batch(offset=0, limit=100):
    """Fetch active markets sorted by 24h volume, no tag filter."""
    try:
        r = requests.get(GAMMA_API, params={
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
            "active": "true",
            "closed": "false",
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def normalize(raw):
    try:
        q = raw.get("question", "").strip()
        if not q:
            return None
        ids = raw.get("clobTokenIds", [])
        if isinstance(ids, str):
            ids = json.loads(ids)
        prices = raw.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)
        yes_price = float(prices[0]) if prices else None
        no_price  = float(prices[1]) if len(prices) > 1 else None
        if yes_price is None or yes_price <= 0.03 or yes_price >= 0.97:
            return None
        return {
            "id":           raw.get("id", ""),
            "question":     q,
            "yes_price":    yes_price,
            "no_price":     no_price,
            "yes_token_id": ids[0] if ids else None,
            "no_token_id":  ids[1] if len(ids) > 1 else None,
            "volume":       float(raw.get("volume24hr", 0) or 0),
            "end_date":     raw.get("endDate", ""),
            "description":  raw.get("description", "")[:500],
            "market_slug":  raw.get("slug", ""),
        }
    except Exception:
        return None


def scan_markets():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # Fetch in two parallel batches of 100 to get top 200 markets by volume
    batch_size = 100
    offsets = [i * batch_size for i in range(MARKETS_FETCH_LIMIT // batch_size)]
    seen = {}
    with ThreadPoolExecutor(max_workers=len(offsets)) as pool:
        futures = {pool.submit(fetch_markets_batch, off, batch_size): off for off in offsets}
        for f in as_completed(futures):
            raw_list = f.result()
            for raw in raw_list:
                m = normalize(raw)
                if m and (m["id"] not in seen or m["volume"] > seen[m["id"]]["volume"]):
                    seen[m["id"]] = m
    return sorted(seen.values(), key=lambda x: x["volume"], reverse=True)


# ── Scorer ────────────────────────────────────────────────────────────────────

# ── Unusual Whales Signal ─────────────────────────────────────────────────────

_uw_cache = {"unusual": [], "smart": [], "insiders": [], "ts": 0}

def fetch_uw_signals():
    """Fetch all three UW prediction endpoints, cached 10 minutes."""
    global _uw_cache
    if not UW_API_KEY:
        return _uw_cache
    now = time.time()
    if now - _uw_cache["ts"] < 600 and _uw_cache["unusual"]:
        return _uw_cache
    try:
        headers = {"Authorization": f"Bearer {UW_API_KEY}"}
        for key, ep in [("unusual","/predictions/unusual"),("smart","/predictions/smart-money"),("insiders","/predictions/insiders")]:
            try:
                r = requests.get(f"{UW_API_BASE}{ep}", headers=headers, timeout=15)
                r.raise_for_status()
                _uw_cache[key] = r.json().get("data", {}).get("data", [])
            except Exception as e:
                log(f"[UW] {key} fetch failed: {e}", Fore.YELLOW)
        _uw_cache["ts"] = now
        log(f"[UW] {len(_uw_cache['unusual'])} unusual | {len(_uw_cache['smart'])} smart-money | {len(_uw_cache['insiders'])} insiders", Fore.MAGENTA)
    except Exception as e:
        log(f"[UW] fetch error: {e}", Fore.YELLOW)
    return _uw_cache

def _uw_words(s):
    stop = {"will","the","a","an","be","by","in","on","or","of","to","vs","vs.","?","and"}
    return set(s.lower().split()) - stop

def _uw_overlap(a, b):
    wa, wb = _uw_words(a), _uw_words(b)
    if not wa or not wb: return 0
    return len(wa & wb) / max(len(wa), len(wb))

def match_uw_unusual(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    best, best_s = None, 0
    for s in signals:
        if s.get("asset_id") == tok: return s
        if s.get("outcome","") != target: continue
        ov = _uw_overlap(market.get("question",""), s.get("market",""))
        if ov > best_s and ov >= 0.5: best_s, best = ov, s
    return best

def match_uw_smart(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    for s in signals:
        for o in s.get("outcomes", []):
            if o.get("asset_id") == tok:
                return {"mkt": s, "out": o}
        ov = _uw_overlap(market.get("question",""), s.get("title",""))
        if ov >= 0.6:
            for o in s.get("outcomes", []):
                if o.get("label","") == target:
                    return {"mkt": s, "out": o}
    return None

def match_uw_insiders(market, signals, action):
    target = "Yes" if action == "BUY_YES" else "No"
    tok = market.get("yes_token_id" if action == "BUY_YES" else "no_token_id", "")
    out = []
    for s in signals:
        if s.get("asset_id") == tok: out.append(s); continue
        if s.get("outcome","") != target: continue
        if _uw_overlap(market.get("question",""), s.get("question","")) >= 0.6:
            out.append(s)
    return out

def build_uw_context(market, uw_cache, action):
    """Returns (prompt_text, signal_dict) for a market+action."""
    sig = {"unusual_score":0,"smart_money":0,"insider_trades":0,"contrarian_whales":0,
           "smart_gap":0,"smart_score":0,"insider_count":0,"insider_zscore":0,
           "insider_pnl":0,"tags_score":0}
    lines = []

    u = match_uw_unusual(market, uw_cache.get("unusual",[]), action)
    if u:
        tags = {t["tag"]: float(t["value"]) for t in u.get("tags",[])}
        sig.update({"unusual_score":float(u.get("unusual_score",0)),
                    "smart_money":tags.get("smart_money",0),
                    "insider_trades":tags.get("insider_trades",0),
                    "contrarian_whales":tags.get("contrarian_whales",0)})
        lines.append(f"unusual_score={sig['unusual_score']:.1f} smart_money={sig['smart_money']:.0f} insider_trades={sig['insider_trades']:.0f} contrarian_whales={sig['contrarian_whales']:.0f}")

    sm = match_uw_smart(market, uw_cache.get("smart",[]), action)
    if sm:
        gap   = float(sm["mkt"].get("smart_gap", 0))
        score = float(sm["out"].get("smart_score", 0))
        sig.update({"smart_gap":gap, "smart_score":score})
        lines.append(f"smart_gap={gap:.2f} smart_score={score:.2f}  (smart_gap=divergence smart vs retail, higher=stronger)")

    ins_list = match_uw_insiders(market, uw_cache.get("insiders",[]), action)
    if ins_list:
        invested = sum(float(i.get("total_invested_usd",0)) for i in ins_list)
        zsc = sum(float(i.get("invested_zscore",0)) for i in ins_list) / len(ins_list)
        pnl = sum(float(i.get("pnl_percent",0)) for i in ins_list) / len(ins_list)
        tsc = sum(float(i.get("tags_score",0)) for i in ins_list) / len(ins_list)
        sig.update({"insider_count":len(ins_list),"insider_zscore":zsc,"insider_pnl":pnl,"tags_score":tsc})
        lines.append(f"insider_wallets={len(ins_list)} invested=${invested:,.0f} zscore={zsc:.1f} pnl={pnl:.1%} tags_score={tsc:.2f}")

    text = ("UNUSUAL WHALES SMART MONEY DATA:\n" + "\n".join(lines) + "\n") if lines else ""
    return text, sig


def score_market(market, mode="NORMAL"):
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    # In Recovery, only score high-volume liquid markets
    if mode == "RECOVERY" and market.get("volume", 0) < 50000:
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": "Recovery mode: low-volume market skipped"}
    # Skip blacklisted market types — no real model edge on these
    if is_blacklisted(market.get("question", "") + " " + market.get("title", "")):
        return {**market, "action": "PASS", "edge": 0, "confidence": "low",
                "reasoning": "Blacklisted market type — no model edge"}


    news = ""
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if pplx_key:
        try:
            r = requests.post(
                "https://api.perplexity.ai/chat/completions",
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "Find recent facts about this prediction market question. Be brief and factual."},
                        {"role": "user", "content": market["question"]},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
                headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
                timeout=20,
            )
            news = r.json()["choices"][0]["message"]["content"]
        except Exception:
            pass

    soul    = load_soul()
    lessons = load_lessons()
    intel   = ""
    if soul:    intel += "CORE PRINCIPLES:\n" + soul + "\n"
    if lessons: intel += "LEARNED LESSONS:\n" + lessons + "\n"

    # ── Unusual Whales signal injection ──────────────────────────────────────
    uw_cache = fetch_uw_signals()
    uw_yes_text, uw_yes_sig = build_uw_context(market, uw_cache, "BUY_YES")
    uw_no_text,  uw_no_sig  = build_uw_context(market, uw_cache, "BUY_NO")
    uw_text = ""
    if uw_yes_text: uw_text += "IF BUY_YES: " + uw_yes_text
    if uw_no_text:  uw_text += "IF BUY_NO: "  + uw_no_text
    market["_uw_yes_sig"] = uw_yes_sig
    market["_uw_no_sig"]  = uw_no_sig

    prompt = f"""MARKET: {market['question']}
YES price: ${market['yes_price']:.3f} | NO price: ${market['no_price']:.3f}
Volume 24h: ${market['volume']:,.0f}
Description: {market.get('description','N/A')[:300]}
End date: {market.get('end_date','N/A')}
News: {news[:400] if news else 'No real-time data'}
{('UNUSUAL WHALES SMART MONEY DATA:\n' + uw_text) if uw_text else ''}
{intel}
Respond with ONLY valid JSON:
{{"true_probability": <float>, "confidence": "<high|medium|low>", "reasoning": "<max 150 chars>", "edge": <true_prob minus yes_price>, "action": "<BUY_YES|BUY_NO|PASS>"}}

Rules: BUY_YES if edge>0.07 and confidence=high. BUY_NO if edge<-0.07 and confidence=high. PASS otherwise.
If UW smart_money or insider_trades are high (>3) and align with your direction, increase confidence. If they contradict your direction, lower confidence or PASS."""

    sys_text = (
        "You are a quantitative prediction market trader. Output ONLY valid JSON.\n"
        "Keys: true_probability, confidence, reasoning, edge, action.\n"
    )
    _soul = load_soul()
    _less = load_lessons()
    if _soul: sys_text += "\nCORE PRINCIPLES:\n" + _soul + "\n"
    if _less: sys_text += "\nLEARNED LESSONS:\n" + _less + "\n"

    try:
        if not hasattr(score_market, "_ac") or score_market._ac is None:
            score_market._ac = anthropic.Anthropic(api_key=api_key)
        resp = score_market._ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=[{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        result["edge"] = round(float(result["true_probability"]) - market["yes_price"], 4)
        if abs(result["edge"]) <= MIN_EDGE or result["confidence"] != "high":
            result["action"] = "PASS"
        return {**market, **result}
    except Exception as e:
        return {**market, "action": "PASS", "edge": 0, "confidence": "low", "reasoning": str(e)[:100]}


def score_batch(markets, mode="NORMAL"):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(score_market, m, mode): m for m in markets}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    return sorted(results, key=lambda x: abs(x.get("edge", 0)), reverse=True)


# ── Executor ──────────────────────────────────────────────────────────────────

def place_trade(client, market, action, size_usdc):
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    if action == "BUY_YES":
        token_id = market.get("yes_token_id")
        price    = market["yes_price"]
    else:
        token_id = market.get("no_token_id")
        price    = market["no_price"] or round(1.0 - market["yes_price"], 4)

    if not token_id:
        log(f"No token ID for {action} on {market['question'][:50]}", Fore.RED)
        return None

    try:
        tick     = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f   = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price    = round(round(price / tick_f) * tick_f, tick_dec)
        price    = max(0.01, min(0.99, price))
        num_shares = round(size_usdc / price, 2)

        args    = OrderArgs(token_id=token_id, price=price, size=num_shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get("success"):
            log(f"✓ ORDER PLACED | {action} ${size_usdc} | {market['question'][:50]} | ID: {receipt.get('orderID','N/A')[:20]}...", Fore.GREEN)
            tg(f"✅ <b>TRADE PLACED</b>\n{action} ${size_usdc:.0f} | {market['question'][:60]}\nEdge: {market.get('edge', 0):+.3f}")
            # Pre-approve conditional token allowance so we can sell this position later
            try:
                client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=2
                    )
                )
                log(f"  ✓ Conditional token allowance set for future sells", Fore.WHITE)
            except Exception as ae:
                log(f"  Allowance pre-approval warning: {ae}", Fore.YELLOW)
            return receipt
        else:
            log(f"✗ Order rejected: {receipt.get('errorMsg')}", Fore.RED)
            return None
    except Exception as e:
        log(f"✗ Trade failed: {e}", Fore.RED)
        return None


# ── Size Calculator ───────────────────────────────────────────────────────────

def calculate_size(edge, mode, equity_now, deployed, market_price=0.5):
    """
    Kelly-inspired sizing within mode-defined min/max bounds.
    The RISK_OPEN_PCT budget scales with equity at higher balances (e.g. $5k+).
    At small balances (<$5k) we use fixed SIZE_MIN/MAX directly so trades always fire.
    """
    # Hard cap: never deploy more than MAX_PORTFOLIO_EXPOSURE total
    MAX_PORTFOLIO_EXPOSURE = int(__import__('os').environ.get('MAX_PORTFOLIO_EXPOSURE', '2500'))
    remaining_capacity = MAX_PORTFOLIO_EXPOSURE - deployed
    if remaining_capacity < SIZE_MIN[mode]:
        return 0

    # Scale between mode min and max based on edge strength
    edge_strength = min(abs(edge) / 0.30, 1.0)
    size = SIZE_MIN[mode] + (SIZE_MAX[mode] - SIZE_MIN[mode]) * edge_strength

    # At higher equity levels, also respect the percentage-based open risk budget
    if equity_now > 5000:
        open_budget = RISK_OPEN_PCT[mode] * equity_now
        remaining_budget = open_budget - deployed
        size = min(size, remaining_budget)

    # Final caps
    size = min(size, remaining_capacity, SIZE_MAX[mode])
    size = max(size, SIZE_MIN[mode]) if size >= SIZE_MIN[mode] else 0

    return round(size, 2)


# ── Order Management ─────────────────────────────────────────────────────────

def cancel_and_resubmit_stale_orders(client, current_markets_by_token):
    from py_clob_client.clob_types import OpenOrderParams

    freed_usdc   = 0.0
    resubmitted  = 0

    try:
        orders = client.get_orders(OpenOrderParams())
    except Exception as e:
        log(f"Could not fetch open orders: {e}", Fore.YELLOW)
        return freed_usdc

    now = datetime.now(timezone.utc).timestamp()

    for order in orders:
        try:
            created_at   = float(order.get("created_at", now))
            age_minutes  = (now - created_at) / 60

            if age_minutes < ORDER_TTL_MINUTES:
                continue

            order_id       = order["id"]
            token_id       = order.get("asset_id", "")
            side           = order.get("side", "BUY")
            original_size  = float(order.get("original_size", 0))
            old_price      = float(order.get("price", 0))
            size_matched   = float(order.get("size_matched", 0))
            remaining      = original_size - size_matched
            usdc_locked    = remaining * old_price

            log(f"Stale order ({age_minutes:.0f} min old): {side} {remaining:.2f} shares @ ${old_price:.3f}", Fore.YELLOW)

            try:
                client.cancel(order_id)
                log(f"  Cancelled order {order_id[:16]}...", Fore.YELLOW)
                freed_usdc += usdc_locked
            except Exception as e:
                log(f"  Cancel failed: {e}", Fore.RED)
                continue

            market = current_markets_by_token.get(token_id)
            if not market:
                continue

            if order.get("outcome", "").lower() in ("yes", ""):
                current_price = market.get("yes_price", 0)
                action = "BUY_YES"
            else:
                current_price = market.get("no_price", 0)
                action = "BUY_NO"

            if not current_price or current_price <= 0.03 or current_price >= 0.97:
                continue

            edge = market.get("edge", 0)
            if abs(edge) < MIN_EDGE:
                log(f"  Edge gone (now {edge:+.3f}) — not resubmitting", Fore.WHITE)
                continue

            resubmit_size = max(round(usdc_locked, 2), 5.0)
            log(f"  Edge still strong ({edge:+.3f}) — resubmitting at ${current_price:.3f}", Fore.CYAN)
            receipt = place_trade(client, {**market, "yes_price": current_price if action == "BUY_YES" else market["yes_price"]}, action, resubmit_size)
            if receipt:
                freed_usdc -= resubmit_size
                resubmitted += 1

        except Exception as e:
            log(f"Error processing order: {e}", Fore.RED)
            continue

    if freed_usdc > 0 or resubmitted > 0:
        log(f"Order cleanup: freed ${freed_usdc:.2f}, resubmitted {resubmitted}", Fore.CYAN)

    return max(freed_usdc, 0.0)


# ── Position Manager ─────────────────────────────────────────────────────────

def manage_positions(client):
    """
    Sell filled positions at profit target, stop loss, or near resolution.
    """
    try:
        trades = client.get_trades()
        if not trades:
            return
    except Exception as e:
        log(f"Could not fetch trades: {e}", Fore.YELLOW)
        return

    positions = {}
    for t in trades:
        token_id = t.get("asset_id", "")
        side     = t.get("side", "BUY")
        price    = float(t.get("price", 0))
        size     = float(t.get("size", 0))
        if not token_id or price == 0:
            continue
        if token_id not in positions:
            positions[token_id] = {"side": side, "shares": 0, "cost": 0}
        if side == "BUY":
            positions[token_id]["shares"] += size
            positions[token_id]["cost"]   += size * price
        else:
            positions[token_id]["shares"] -= size

    for token_id, pos in positions.items():
        shares = pos["shares"]
        if shares <= 0.1:
            continue

        avg_entry  = pos["cost"] / (pos["shares"] + 1e-9)
        trade_side = pos["side"]

        try:
            book          = client.get_midpoint(token_id)
            current_price = float(book.get("mid", avg_entry))
        except Exception:
            continue

        should_sell = False
        reason      = ""

        if trade_side == "BUY":
            if current_price >= PROFIT_TARGET:
                should_sell = True
                reason = f"Profit target hit (entry ${avg_entry:.3f} → now ${current_price:.3f})"
            elif current_price <= avg_entry * (1 - STOP_LOSS):
                should_sell = True
                reason = f"Stop loss hit (entry ${avg_entry:.3f} → now ${current_price:.3f})"
            elif current_price >= NEAR_RESOLUTION_THRESHOLD:
                should_sell = True
                reason = f"Near resolution at ${current_price:.3f} — locking in gain"
        else:
            no_entry   = 1.0 - avg_entry
            no_current = 1.0 - current_price
            if no_current >= PROFIT_TARGET:
                should_sell = True
                reason = f"NO profit target hit (NO entry ${no_entry:.3f} → now ${no_current:.3f})"
            elif current_price >= avg_entry + STOP_LOSS:
                should_sell = True
                reason = f"Stop loss hit on NO position"
            elif no_current >= NEAR_RESOLUTION_THRESHOLD:
                should_sell = True
                reason = f"NO near resolution at ${no_current:.3f} — locking in gain"

        if should_sell:
            log(f"SELL SIGNAL: {reason}", Fore.CYAN)
            try:
                from py_clob_client.order_builder.constants import SELL
                from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                tick     = client.get_tick_size(token_id)
                neg_risk = client.get_neg_risk(token_id)
                tick_f   = float(tick)
                tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
                sell_price = round(round(current_price / tick_f) * tick_f, tick_dec)
                sell_price = max(0.01, min(0.99, sell_price))
                args    = OrderArgs(token_id=token_id, price=sell_price, size=round(shares, 2), side=SELL)
                options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
                signed  = client.create_order(args, options)
                receipt = client.post_order(signed, OrderType.GTC)
                if receipt.get("success"):
                    pnl = (sell_price - avg_entry) * shares
                    log(f"✓ SOLD {shares:.2f} shares @ ${sell_price:.3f} | PnL: ${pnl:+.2f} | {reason}", Fore.GREEN)
                    tg(f"💰 <b>SOLD</b> {shares:.1f} shares @ ${sell_price:.3f}\nP&L: ${pnl:+.2f} | {reason[:80]}")
                else:
                    err_msg = receipt.get('errorMsg', '')
                    if "not enough balance" in err_msg or "allowance" in err_msg:
                        # Set conditional token allowance then retry once
                        log(f"  Setting conditional token allowance for sell, retrying...", Fore.YELLOW)
                        try:
                            client.update_balance_allowance(
                                params=BalanceAllowanceParams(
                                    asset_type=AssetType.CONDITIONAL,
                                    token_id=token_id,
                                    signature_type=2
                                )
                            )
                            signed2 = client.create_order(args, options)
                            receipt2 = client.post_order(signed2, OrderType.GTC)
                            if receipt2.get("success"):
                                pnl = (sell_price - avg_entry) * shares
                                log(f"✓ SOLD {shares:.2f} shares @ ${sell_price:.3f} | PnL: ${pnl:+.2f} | {reason}", Fore.GREEN)
                                tg(f"💰 <b>SOLD</b> {shares:.1f} shares @ ${sell_price:.3f}\nP&L: ${pnl:+.2f} | {reason[:80]}")
                            else:
                                log(f"Sell failed after allowance fix: {receipt2.get('errorMsg')}", Fore.RED)
                        except Exception as e2:
                            log(f"Sell allowance retry failed: {e2}", Fore.RED)
                    else:
                        log(f"Sell order failed: {err_msg}", Fore.RED)
            except Exception as e:
                err = str(e)
                if "not enough balance" in err or "allowance" in err:
                    # Set conditional token allowance then retry once
                    log(f"  Setting conditional token allowance for sell, retrying...", Fore.YELLOW)
                    try:
                        client.update_balance_allowance(
                            params=BalanceAllowanceParams(
                                asset_type=AssetType.CONDITIONAL,
                                token_id=token_id,
                                signature_type=2
                            )
                        )
                        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                        args2   = OrderArgs(token_id=token_id, price=sell_price, size=round(shares, 2), side=SELL)
                        signed2 = client.create_order(args2, options)
                        receipt2 = client.post_order(signed2, OrderType.GTC)
                        if receipt2.get("success"):
                            pnl = (sell_price - avg_entry) * shares
                            log(f"✓ SOLD {shares:.2f} shares @ ${sell_price:.3f} | PnL: ${pnl:+.2f} | {reason}", Fore.GREEN)
                            tg(f"💰 <b>SOLD</b> {shares:.1f} shares @ ${sell_price:.3f}\nP&L: ${pnl:+.2f} | {reason[:80]}")
                        else:
                            log(f"Sell failed after allowance fix: {receipt2.get('errorMsg')}", Fore.RED)
                    except Exception as e2:
                        log(f"Sell allowance retry failed: {e2}", Fore.RED)
                else:
                    log(f"Sell failed: {e}", Fore.RED)
                    log_mistake("Sell failed", f"token {token_id[:20]}", str(e)[:150], "Set conditional token allowance at buy time")


# ── News Arbitrage Layer ─────────────────────────────────────────────────────
#
# Strategy: scan global news every 5 min, find breaking events that haven't
# been priced into Polymarket yet, trade immediately at full size.
# This is the "obvious locally, not priced globally" edge.

NEWS_FEEDS = [
    # Global wire services
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    # Politics / geopolitics
    "https://feeds.reuters.com/reuters/politicsNews",
    "https://feeds.reuters.com/reuters/worldNews",
    # Finance / markets
    "https://feeds.reuters.com/reuters/businessNews",
    # Sports (for game result arb)
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nfl/news",
]


def fetch_news_headlines():
    """Pull headlines from RSS feeds, return list of recent headline strings."""
    import xml.etree.ElementTree as ET
    headlines = []
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                desc  = item.findtext("description", "").strip()[:150]
                pub   = item.findtext("pubDate", "").strip()
                if title:
                    headlines.append(f"{title} | {desc} [{pub}]")
        except Exception:
            continue
    return headlines[:120]  # cap at 120 headlines


def news_arb_scan(client, state, markets_cache):
    """
    1. Fetch latest headlines from global news feeds
    2. Ask Perplexity to match headlines to open Polymarket questions
    3. For each match where the outcome is clear, score and trade immediately
    Returns number of trades placed.
    """
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not pplx_key:
        return 0

    mode = state.get("mode", "NORMAL")
    if mode == "PAUSED":
        return 0

    try:
        headlines = fetch_news_headlines()
    except Exception as e:
        log(f"[NEWS ARB] Feed fetch failed: {e}", Fore.YELLOW)
        return 0

    if not headlines:
        return 0

    # Use markets_cache (passed in from last full scan) to avoid re-fetching
    markets = markets_cache if markets_cache else []
    if not markets:
        return 0

    # Build a compact market list for the prompt
    market_list = "\n".join(
        f"- [{m['id'][:12]}] {m['question']} | YES={m['yes_price']:.2f} NO={m['no_price']:.2f}"
        for m in markets[:60]
    )
    headline_block = "\n".join(headlines[:60])

    prompt = f"""You are a prediction market arbitrage scanner. Be EXTREMELY strict.

Here are the latest global news headlines (past few hours):
{headline_block}

Here are open Polymarket questions with current prices:
{market_list}

Your task:
1. Find headlines that DIRECTLY and UNAMBIGUOUSLY resolve a listed market
2. The headline must mention the EXACT same team, person, country, or event as the market question
3. Only flag if confidence >= 0.92 (near-certain outcome)
4. Focus ONLY on: confirmed final scores, official election results, confirmed policy decisions
5. REJECT any match where the headline is about a different sport, team, league, or event
6. REJECT vague correlations, partial matches, or different competitions (e.g. rugby != football)
7. A headline about Team A does NOT imply anything about Team B

Respond with ONLY a JSON array (empty if no matches):
[{{"market_id": "first 12 chars", "action": "BUY_YES or BUY_NO", "confidence": 0.0-1.0, "headline": "the specific headline", "reasoning": "exact match explanation — name the shared entity"}}]

If uncertain, return []"""

    try:
        r = requests.post(
            "https://api.perplexity.ai/chat/completions",
            json={
                "model": "sonar",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 800,
                "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        # Extract JSON array
        if "[" in text:
            text = text[text.index("["):text.rindex("]")+1]
        matches = json.loads(text)
    except Exception as e:
        log(f"[NEWS ARB] Scoring failed: {e}", Fore.YELLOW)
        return 0

    if not matches:
        log("[NEWS ARB] No breaking news matches this cycle.", Fore.WHITE)
        return 0

    trades_placed = 0
    equity_now = get_equity(client) or state.get("equity_sod", 1000)
    stats = get_portfolio_stats(client)
    available = max(0, equity_now - stats["deployed"] - MIN_FREE_BALANCE)

    for match in matches:
        if match.get("confidence", 0) < 0.92:
            log(f"[NEWS ARB] Low confidence ({match.get('confidence',0):.2f}) — skipping: {match.get('headline','')[:60]}", Fore.YELLOW)
            continue

        mid = match.get("market_id", "")
        # Find the full market by partial ID match
        market = next((m for m in markets if m["id"].startswith(mid) or mid in m["id"]), None)
        if not market:
            continue

        # Verify keyword overlap: headline must share key words with market question
        headline_words = set(match.get("headline", "").lower().split())
        question_words = set(market["question"].lower().split())
        # Remove common stop words
        stops = {"the","a","an","is","are","was","will","to","of","in","on","at","by","for","and","or","not","be","with","as","it"}
        h_words = headline_words - stops
        q_words = question_words - stops
        overlap = h_words & q_words
        if len(overlap) < 2:
            log(f"[NEWS ARB] REJECTED — insufficient keyword overlap ({overlap}) between headline and market", Fore.YELLOW)
            continue

        action = match.get("action", "")
        if action not in ("BUY_YES", "BUY_NO"):
            continue

        # Check edge is real — price shouldn't already reflect the news
        yes_price = market["yes_price"]
        no_price  = market["no_price"]
        if action == "BUY_YES" and yes_price > 0.88:
            continue  # already priced in
        if action == "BUY_NO" and no_price > 0.88:
            continue  # already priced in

        # Size up for news arb — higher confidence = larger trade
        base_size = SIZE_MIN[mode] * NEWS_ARB_SIZE_MULT
        size = min(base_size, available, MAX_PER_MARKET_USDC)
        if size < SIZE_MIN[mode]:
            log(f"[NEWS ARB] Insufficient balance for news arb trade.", Fore.YELLOW)
            break

        log(f"[NEWS ARB] 🚨 BREAKING: {match['headline'][:80]}", Fore.MAGENTA)
        log(f"[NEWS ARB] {action} ${size:.0f} | {market['question'][:60]}", Fore.MAGENTA)
        log(f"[NEWS ARB] Reasoning: {match['reasoning'][:100]}", Fore.MAGENTA)

        result = place_trade(client, market, action, size)
        if result:
            trades_placed += 1
            available -= size

    return trades_placed


# ── Main Cycle ────────────────────────────────────────────────────────────────

def run_cycle(client, state):
    _read_intel_files()  # refresh soul/lessons cache once per cycle
    log("─" * 60)
    log(f"Starting scan cycle — {datetime.now().strftime('%H:%M:%S')}", Fore.CYAN)

    # ── 1. Manage existing positions (sell signals) ────────────────────────────
    manage_positions(client)

    # ── 2. Get current equity and update control plane ─────────────────────────
    equity_now = get_equity(client)
    if equity_now is None:
        log("Could not determine equity — skipping cycle", Fore.YELLOW)
        return state

    state, allow_new_trades = update_control_plane(state, equity_now)
    mode = state["mode"]
    log(f"Mode: {mode} | Equity: ${equity_now:.2f}", Fore.CYAN)

    # Re-approve allowances if flagged by daily reset
    if state.get("last_approval_date") != date.today().isoformat():
        state = ensure_allowances(state)
        save_state(state)

    # ── 3. Portfolio stats ─────────────────────────────────────────────────────
    stats = get_portfolio_stats(client)
    log(f"Portfolio: {stats['open_orders']} open orders, ${stats['deployed']:.2f} deployed")

    # ── 4. Skip new trades if paused or daily stop hit ─────────────────────────
    if not allow_new_trades or mode == "PAUSED":
        log(f"No new trades this cycle (mode={mode})", Fore.YELLOW)
        if mode == "PAUSED":
            peak   = state.get("equity_peak_eod", equity_now)
            dd_pct = (peak - equity_now) / peak * 100 if peak > 0 else 0
            tg(f"⛔ <b>HARD PAUSE</b> — drawdown {dd_pct:.1f}%\n💰 Equity: ${equity_now:,.2f} | Peak: ${peak:,.2f}")
        save_state(state)
        return state

    # ── 5. Scan markets ────────────────────────────────────────────────────────
    log("Scanning markets...", Fore.CYAN)
    all_markets = scan_markets()
    log(f"Found {len(all_markets)} markets. Scoring top {TOP_MARKETS_TO_SCORE}...")

    # ── 6. Score (pass mode to restrict in Recovery) ──────────────────────────
    scored = score_batch(all_markets[:TOP_MARKETS_TO_SCORE], mode=mode)

    # Build token lookup for stale order resubmission
    markets_by_token = {}
    for m in scored:
        if m.get("yes_token_id"):
            markets_by_token[m["yes_token_id"]] = m
        if m.get("no_token_id"):
            markets_by_token[m["no_token_id"]] = m

    # ── 7. Cancel stale orders ────────────────────────────────────────────────
    freed = cancel_and_resubmit_stale_orders(client, markets_by_token)
    stats["deployed"] = max(0, stats["deployed"] - freed)
    stats = get_portfolio_stats(client)
    log(f"After cleanup: {stats['open_orders']} open orders, ${stats['deployed']:.2f} deployed")

    # ── 8. Order gate: check mode limits ──────────────────────────────────────
    max_orders = MAX_ORDERS[mode]
    # Use fixed dollar budget when equity < $5k (pct-based gives trivially small numbers)
    if equity_now < 5000:
        open_budget = MAX_ORDERS[mode] * SIZE_MAX[mode]  # e.g. 8 * $150 = $1200 max deployed
    else:
        open_budget = RISK_OPEN_PCT[mode] * equity_now

    if stats["open_orders"] >= max_orders:
        log(f"Max open orders for {mode} mode ({max_orders}) reached.", Fore.YELLOW)
        save_state(state)
        return state

    if stats["deployed"] >= open_budget:
        log(f"Max open risk for {mode} mode (${open_budget:.2f}) reached.", Fore.YELLOW)
        save_state(state)
        return state

    # ── 9. Place trades ────────────────────────────────────────────────────────
    actionable = [m for m in scored if m.get("action") in ("BUY_YES", "BUY_NO")]

    if not actionable:
        log("No actionable opportunities this cycle.", Fore.WHITE)
        save_state(state)
        return state

    # Build map of current open exposure keyed by lowercase title
    # Also track which SIDE (YES/NO) we hold to prevent buying the opposite
    existing_exposure = {}  # lowercase title -> current_value USD
    existing_side     = {}  # lowercase title -> "YES" or "NO"
    try:
        pos_resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": FUNDER, "sizeThreshold": "0.01"},
            timeout=10
        )
        if pos_resp.status_code == 200:
            for p in pos_resp.json():
                cur_value = float(p.get("currentValue", 0))
                title = p.get("title", "").strip().lower()
                asset = p.get("asset", "")  # token ID
                outcome = p.get("outcome", "")  # "Yes" or "No"
                if title and cur_value > 0:
                    existing_exposure[title] = existing_exposure.get(title, 0) + cur_value
                    existing_side[title] = outcome  # track which side we hold
                if asset and cur_value > 0:
                    existing_exposure[asset] = cur_value
    except Exception:
        pass

    # Check available balance
    available = max(0, equity_now - stats["deployed"] - MIN_FREE_BALANCE)
    if available < SIZE_MIN[mode]:
        log(f"Insufficient free balance (${available:.2f}) — skipping new trades.", Fore.YELLOW)
        save_state(state)
        return state

    log(f"Found {len(actionable)} opportunities [{mode} mode] | Free: ${available:.2f}:", Fore.GREEN)
    trades_placed = 0

    for m in actionable:
        action = m["action"]
        edge   = m.get("edge", 0)
        market_id = m.get("id", "")

        # ── Side-conflict check: never buy opposite side of existing position ──────
        q_lower = m.get("question", "").strip().lower()
        yes_tok = m.get("yes_token_id", "")
        no_tok  = m.get("no_token_id", "")
        held_side = existing_side.get(q_lower, "")
        want_side = "Yes" if action == "BUY_YES" else "No"
        if held_side and held_side != want_side:
            log(f"  SKIP (holding {held_side}, agent wants {want_side} — conflict): {m['question'][:50]}", Fore.YELLOW)
            continue

        # ── UW signal edge boost / veto ────────────────────────────────────────
        uw_sig  = m.get("_uw_yes_sig") if action == "BUY_YES" else m.get("_uw_no_sig")
        opp_sig = m.get("_uw_no_sig")  if action == "BUY_YES" else m.get("_uw_yes_sig")
        if uw_sig:
            sm     = uw_sig.get("smart_money", 0)
            ins    = uw_sig.get("insider_trades", 0)
            gap    = uw_sig.get("smart_gap", 0)
            n_ins  = uw_sig.get("insider_count", 0)
            uscore = uw_sig.get("unusual_score", 0)
            if gap >= 5 or sm >= 5 or ins >= 5 or n_ins >= 2:
                boost = min(0.06, gap * 0.004 + (sm + ins) * 0.002 + n_ins * 0.005)
                edge  = round(edge + boost, 4)
                m["edge"] = edge
                log(f"  [UW] ✓ Boost +{boost:.3f} | gap={gap:.1f} sm={sm:.0f} ins={ins:.0f} wallets={n_ins:.0f}", Fore.MAGENTA)
            if opp_sig:
                opp_gap = opp_sig.get("smart_gap", 0)
                opp_sm  = opp_sig.get("smart_money", 0)
                if opp_gap > gap + 4 or opp_sm > sm + 4:
                    log(f"  [UW] ✗ VETO — smart money opposes (gap={opp_gap:.1f} sm={opp_sm:.0f}): {m['question'][:45]}", Fore.RED)
                    continue
            if uscore > 0 or gap > 0:
                log(f"  [UW] score={uscore:.1f} gap={gap:.1f} sm={sm:.0f} wallets={n_ins:.0f}", Fore.MAGENTA)

        # ── Per-market cap check ────────────────────────────────────────────────
        already_in = (
            existing_exposure.get(q_lower, 0) or
            existing_exposure.get(yes_tok, 0) or
            existing_exposure.get(no_tok, 0) or
            existing_exposure.get(market_id, 0)
        )
        if already_in >= MAX_PER_MARKET_USDC:
            log(f"  SKIP (already ${already_in:.0f} in this market, cap=${MAX_PER_MARKET_USDC}): {m['question'][:50]}", Fore.YELLOW)
            continue

        log(f"  {action} | edge={edge:+.3f} | {m['question'][:55]}", Fore.GREEN)
        log(f"    Reasoning: {m.get('reasoning','')[:100]}")

        size = calculate_size(edge, mode, equity_now, stats["deployed"], market_price=m.get("price", 0.5))
        size = min(size, available)  # Never exceed available free balance
        size = min(size, MAX_PER_MARKET_USDC - already_in)  # Per-market cap
        if size < SIZE_MIN[mode]:
            log(f"  Skipping — size ${size:.2f} below mode minimum ${SIZE_MIN[mode]}", Fore.YELLOW)
            continue

        receipt = place_trade(client, m, action, size)
        if receipt:
            stats["deployed"] += size
            trades_placed += 1

        if stats["deployed"] >= open_budget or stats["open_orders"] + trades_placed >= max_orders:
            log(f"Mode budget reached during cycle.", Fore.YELLOW)
            break

    log(f"Cycle complete. {trades_placed} new trades placed. Mode: {mode}", Fore.CYAN)
    save_state(state)
    return state


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("  POLYMARKET AUTO-TRADER v2 — Control Plane Active", Fore.CYAN)
    log(f"  Scan interval: {SCAN_INTERVAL_SECONDS//60} minutes")
    log(f"  NORMAL: ${SIZE_MIN['NORMAL']}–${SIZE_MAX['NORMAL']} | RECOVERY: ${SIZE_MIN['RECOVERY']}–${SIZE_MAX['RECOVERY']} | EXPANSION: ${SIZE_MIN['EXPANSION']}–${SIZE_MAX['EXPANSION']}")
    log(f"  DD triggers: Recovery={DD_RECOVERY:.0%}, Hard Pause={DD_HARD_PAUSE:.0%}")
    log("=" * 60)

    if not PRIVATE_KEY:
        log("ERROR: POLYMARKET_PRIVATE_KEY not set in .env", Fore.RED)
        return

    # Load persisted state (survives reboots)
    state = load_state()
    log(f"Loaded state: mode={state['mode']}, peak=${state.get('equity_peak_eod') or '?'}", Fore.CYAN)

    # Auto-approve USDC allowances at startup
    state = ensure_allowances(state)
    save_state(state)

    cycle = 0
    markets_cache = []       # Shared market list between full scans and news arb
    last_full_scan = 0       # Timestamp of last 15-min full scan
    last_news_scan = 0       # Timestamp of last 5-min news arb scan
    last_review    = 0       # Timestamp of last weekly pattern review

    while True:
        now = time.time()
        try:
            client = get_client()

            # ── Full scan every 15 minutes ────────────────────────────────────
            if now - last_full_scan >= SCAN_INTERVAL_SECONDS:
                cycle += 1
                log(f"\nCycle #{cycle}", Fore.CYAN)
                # Refresh market cache during full scan
                fresh_markets = scan_markets()
                if fresh_markets:
                    markets_cache = fresh_markets
                state = run_cycle(client, state)
                last_full_scan = time.time()

            # ── News arb every 5 minutes ──────────────────────────────────────
            elif now - last_news_scan >= NEWS_SCAN_INTERVAL:
                log(f"\n[NEWS ARB] Scanning headlines...", Fore.MAGENTA)
                try:
                    n = news_arb_scan(client, state, markets_cache)
                    if n > 0:
                        log(f"[NEWS ARB] Placed {n} news-driven trade(s).", Fore.MAGENTA)
                except Exception as e:
                    log(f"[NEWS ARB] Error: {e}", Fore.YELLOW)
                last_news_scan = time.time()

            # ── Weekly intelligence review (every 7 days) ─────────────────
            elif time.time() - last_review >= 7 * 24 * 3600:
                log("[INTELLIGENCE] Running weekly pattern review...", Fore.MAGENTA)
                review_patterns()
                last_review = time.time()

            # ── Sleep 60 seconds between checks ──────────────────────────────
            else:
                time.sleep(60)
                continue

        except KeyboardInterrupt:
            log("\nStopped by user. Goodbye.", Fore.WHITE)
            break
        except Exception as e:
            tb = traceback.format_exc()
            log(f"Cycle error: {e}", Fore.RED)
            log(tb, Fore.RED)
            short = tb.strip().splitlines()
            # Send last 15 lines of traceback to Telegram so we can diagnose
            tb_snippet = "\n".join(short[-15:])
            tg(f"🔴 <b>Cycle error</b>\n<code>{str(e)[:200]}</code>\n\n<code>{tb_snippet[:800]}</code>")
            log_mistake("Cycle error", "Unhandled exception", str(e)[:150], "Add specific handling for this error type")
            time.sleep(60)


if __name__ == "__main__":
    main()
