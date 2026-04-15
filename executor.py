#!/usr/bin/env python3
"""
executor.py — Secure webhook command executor for Polymarket agent server
=========================================================================
Runs on port 8888. Accepts POST /exec with a signed payload.
Allows the Perplexity agent to trigger scripts on the server
without SSH access.

Security:
  - HMAC-SHA256 signature required on every request (shared secret in .env)
  - Whitelist of allowed commands — arbitrary shell injection not possible
  - Logs every request to executor.log
  - Rate limit: 10 requests per minute

Usage:
  POST http://167.71.68.143:8888/exec
  Headers:
    X-Signature: hmac_sha256(secret, body)
    Content-Type: application/json
  Body:
    {"command": "deploy_autotrader"}
    {"command": "run_script", "script": "place_weather_march24_25.py"}
    {"command": "service_status"}
    {"command": "service_restart"}
    {"command": "tail_log", "lines": 50}

Setup:
  Add to .env:  EXECUTOR_SECRET=<random_32_char_string>
  Run:          systemctl enable executor && systemctl start executor
"""

import os, json, hmac, hashlib, subprocess, time, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from datetime import datetime
from dotenv import load_dotenv

# Load .env from agent directory
_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_dir, ".env"))

SECRET         = os.environ.get("EXECUTOR_SECRET", "").strip()
AGENT_DIR      = "/opt/polymarket-agent"
LOG_FILE       = os.path.join(AGENT_DIR, "executor.log")
PORT           = 8888
RATE_LIMIT     = 10   # requests per minute
VENV_PYTHON    = os.path.join(AGENT_DIR, "venv/bin/python3")

# Rate limiting: track request timestamps (thread-safe lock)
_request_times = []
_rate_lock = threading.Lock()

# Configure logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("executor")

# ── Allowed commands (whitelist — no arbitrary shell) ────────────────────────
COMMANDS = {
    "deploy_autotrader": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/autotrader.py "
        f"-o {AGENT_DIR}/autotrader.py && "
        f"{VENV_PYTHON} -c \"import py_compile; py_compile.compile('{AGENT_DIR}/autotrader.py', doraise=True)\" && "
        f"systemctl restart polymarket && echo OK"
    ),
    "deploy_executor": (
        f"curl -sfL -H 'Accept: application/vnd.github.v3.raw' "
        f"https://api.github.com/repos/nlgal/polymarket-autotrader/contents/executor.py "
        f"-o {AGENT_DIR}/executor.py && "
        f"systemctl restart executor && echo OK"
    ),
    "deploy_weather_scout": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/weather_scout.py "
        f"-o {AGENT_DIR}/weather_scout.py && echo OK"
    ),
    "deploy_sports_trader": (
        f"curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/live_sports_trader.py' "
        f"-H 'Accept: application/vnd.github.v3.raw' "
        f"-o {AGENT_DIR}/live_sports_trader.py && "
        f"curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/rederive_and_sell.py' "
        f"-H 'Accept: application/vnd.github.v3.raw' "
        f"-o {AGENT_DIR}/rederive_and_sell.py && "
        f"curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/discord_monitor.py' "
        f"-H 'Accept: application/vnd.github.v3.raw' "
        f"-o {AGENT_DIR}/discord_monitor.py && "
        f"curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/inject_discord_env.py' "
        f"-H 'Accept: application/vnd.github.v3.raw' "
        f"-o {AGENT_DIR}/inject_discord_env.py && "
        f"curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/auto_redeem.py' -H 'Accept: application/vnd.github.v3.raw' -o /opt/polymarket-agent/auto_redeem.py && curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/whale_scanner.py' -H 'Accept: application/vnd.github.v3.raw' -o /opt/polymarket-agent/whale_scanner.py && curl -s 'https://api.github.com/repos/nlgal/polymarket-autotrader/contents/whale_monitor.py' -H 'Accept: application/vnd.github.v3.raw' -o /opt/polymarket-agent/whale_monitor.py && echo 'all scripts deployed'"
    ),
    "deploy_all": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/autotrader.py "
        f"-o {AGENT_DIR}/autotrader.py && "
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/weather_scout.py "
        f"-o {AGENT_DIR}/weather_scout.py && "
        f"{VENV_PYTHON} -c \"import py_compile; py_compile.compile('{AGENT_DIR}/autotrader.py', doraise=True)\" && "
        f"systemctl restart polymarket && echo OK"
    ),
    "deploy_new_modules": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/market_guardrails.py "
        f"-o {AGENT_DIR}/market_guardrails.py && "
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/post_trade_review.py "
        f"-o {AGENT_DIR}/post_trade_review.py && "
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/position_monitor.py "
        f"-o {AGENT_DIR}/position_monitor.py && "
        f"{VENV_PYTHON} -c \"import py_compile; [py_compile.compile(f'{AGENT_DIR}/'+f, doraise=True) for f in ['market_guardrails.py','post_trade_review.py','position_monitor.py']]\" && "
        f"echo OK"
    ),
    "deploy_optimizer": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/strategy_optimizer.py "
        f"-o {AGENT_DIR}/strategy_optimizer.py && "
        f"{VENV_PYTHON} -m py_compile {AGENT_DIR}/strategy_optimizer.py && "
        f"echo OK"
    ),
    "deploy_lp": (
        f"curl -s https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/lp_quoter.py "
        f"-o {AGENT_DIR}/lp_quoter.py && "
        f"{VENV_PYTHON} -m py_compile {AGENT_DIR}/lp_quoter.py && "
        f"echo OK"
    ),
    "service_status":  "systemctl status polymarket --no-pager -l",
    "service_restart": "systemctl restart polymarket && echo OK",
    "service_stop":    "systemctl stop polymarket && echo OK",
    "tail_log":        None,  # handled dynamically below
    "run_script":      None,  # handled dynamically below
}

# Scripts allowed to be run via run_script command
ALLOWED_SCRIPTS = {
    "place_weather_march24_25.py",
    "place_gold_eth_trades.py",
    "place_iran_trades.py",
    "place_wellington_seoul.py",
    "place_weather_trades.py",
    "place_iran_conflict_no.py",
    "place_weather_march26_27.py",
    "place_weather_march26.py",
    "place_weather_march28_29.py",
    "place_weather_march30_31.py",
    "reset_state.py",
    "rederive_and_sell.py",
    "approve_and_sell_btc.py",
    "diagnose_allowance.py",
    "sell_btc_80k_no.py",
    "sell_btc_exact.py",
    "deploy_config.py",
    "sell_btc65k_no.py",
    "deposit_safe.py",
    "patch_deposit_rpc.py",
    "lp_farmer.py",
    "lp_quoter.py",
    "position_monitor.py",
    "sell_ceasefire_apr30_yes.py",
    "merge_apr30.py",
    "write_chunk_0.py",
    "write_chunk_1.py",
    "write_chunk_2.py",
    "finalize_deploy.py",
    "opportunity_scanner.py",
    "deploy_btc_momentum.py",
    "exit_contradictions.py",
    "place_5_trades.py",
    "hormuz_rebalance.py",
    "preflight.py",
    "strategy_optimizer.py",
    "sell_crude_no.py",
    "run_optimizer_now.py",
    "patch_scanner_claudemd.py",
    "live_sports_trader.py",
    "discord_monitor.py",
    "patch_discord_env.py",
    "inject_discord_env.py",
    "place_clippers_lac.py",
    "place_spain_yes.py",
    "place_blg.py",
    "deploy_sports_bl.py",
    "auto_redeem.py",
    "whale_monitor.py",
    "check_whale.py",
    "bootstrap_whale.py",
    "market_guardrails.py",
    "post_trade_review.py",
    "verify_fix.py",
    "whale_scanner.py",
    "patch_auto_redeem.py",
    "clear_redeem_cache.py",
    "diag_auto_redeem.py",
    "clear_signals.py",
    "very_hot_forward_test.py",
    "near_resolution_scanner.py",
    "negative_risk_scanner.py",
    "book_imbalance_scanner.py",
    "check_balance.py",
    "reset_paused.py",
    "read_signals.py",
    "buy_may15_no.py",
    "buy_iran_inv.py",
    "dump_whales.py",
    "read_watchlist.py",
    "purge_sports_state.py",
}


def verify_signature(body_bytes: bytes, sig_header: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    if not SECRET:
        log.error("EXECUTOR_SECRET not set — rejecting all requests")
        return False
    expected = hmac.new(SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header.strip())


def check_rate_limit() -> bool:
    """Allow max RATE_LIMIT requests per 60 seconds (thread-safe)."""
    global _request_times
    with _rate_lock:
        now = time.time()
        _request_times = [t for t in _request_times if now - t < 60]
        if len(_request_times) >= RATE_LIMIT:
            return False
        _request_times.append(now)
    return True


def run_command(cmd: str, timeout: int = 60) -> dict:
    """Run a shell command and return stdout/stderr."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=AGENT_DIR
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server — each request handled in a separate thread.
    Prevents long-running scripts (e.g. CLOB API calls) from blocking
    the server and making it unable to accept new connections."""
    daemon_threads = True  # Threads die when the server dies


class ExecutorHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Silence default HTTP access log (we use our own)

    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "ts": datetime.utcnow().isoformat()})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/exec":
            self.send_json(404, {"error": "not found"})
            return

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)

        # Verify signature
        sig = self.headers.get("X-Signature", "")
        if not verify_signature(body_bytes, sig):
            log.warning(f"Invalid signature from {self.client_address[0]}")
            self.send_json(401, {"error": "invalid signature"})
            return

        # Rate limit
        if not check_rate_limit():
            log.warning(f"Rate limit hit from {self.client_address[0]}")
            self.send_json(429, {"error": "rate limit exceeded"})
            return

        # Parse body
        try:
            payload = json.loads(body_bytes)
        except Exception:
            self.send_json(400, {"error": "invalid JSON"})
            return

        command = payload.get("command", "")
        log.info(f"Command: {command} from {self.client_address[0]}")

        # Handle dynamic commands
        if command == "tail_log":
            lines = int(payload.get("lines", 50))
            lines = min(lines, 500)  # cap at 500
            cmd = f"tail -n {lines} {AGENT_DIR}/trades.log"
            result = run_command(cmd)
            self.send_json(200, result)
            return

        if command == "run_script":
            script = payload.get("script", "")
            if script not in ALLOWED_SCRIPTS:
                log.warning(f"Blocked script: {script}")
                self.send_json(403, {"error": f"script not in allowlist: {script}"})
                return
            script_path = os.path.join(AGENT_DIR, script)
            # Always re-download from GitHub to get latest version
            import time as _tt
            _cache_bust = int(_tt.time())
            dl = run_command(
                f"curl -sfL 'https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/{script}?t={_cache_bust}' "
                f"-o {script_path}"
            )
            if dl["exit_code"] != 0:
                # If download fails but file exists, use cached version
                if not os.path.exists(script_path):
                    self.send_json(500, {"error": f"failed to download {script}", **dl})
                    return
                log.warning(f"Download failed for {script}, using cached version")
            result = run_command(f"{VENV_PYTHON} {script_path}", timeout=120)
            log.info(f"Script {script} exit={result['exit_code']}")
            self.send_json(200, result)
            return

        # sell_position: queue a sell in sell_signals.json; autotrader executes next cycle
        if command == "sell_position":
            import json as _j, os as _o
            _sig_file = "/opt/polymarket-agent/sell_signals.json"
            _tok  = payload.get("token_id", "")
            _shr  = float(payload.get("shares", 0))
            _lbl  = payload.get("label", _tok[:20] if _tok else "unknown")
            if not _tok or _shr <= 0:
                self.send_json(400, {"error": "sell_position requires token_id and shares"})
                return
            existing = []
            if _o.path.exists(_sig_file):
                try:
                    with open(_sig_file) as _f: existing = _j.load(_f)
                except: existing = []
            existing.append({"token_id": _tok, "shares": _shr, "label": _lbl})
            with open(_sig_file, "w") as _f: _j.dump(existing, _f)
            self.send_json(200, {"exit_code": 0, "stdout": f"Queued sell: {_lbl} {_shr:.0f}sh", "stderr": ""})
            return

        # whitelist_script: add script to COMMANDS at runtime
        if command == "whitelist_script":
            _sn = payload.get("script", "")
            if _sn and _sn not in COMMANDS:
                COMMANDS[_sn] = ["python3", f"/opt/polymarket-agent/{_sn}"]
            self.send_json(200, {"exit_code": 0, "stdout": f"whitelisted {_sn}", "stderr": ""})
            return

        # ── Last-request logger for watchdog crash replay ──────────────────
        try:
            import json as _lj, time as _lt
            with open("/opt/polymarket-agent/executor_last_request.json", "w") as _lf:
                _lj.dump({"command": command, **payload, "_ts": _lt.time()}, _lf)
        except:
            pass
        # ──────────────────────────────────────────────────────────────────

        # Static whitelist command
        if command not in COMMANDS:
            log.warning(f"Unknown command: {command}")
            if command == "sell_position":
                import json as _j, os as _o
                _sf = "/opt/polymarket-agent/sell_signals.json"
                _tok = payload.get("token_id",""); _shr = float(payload.get("shares",0))
                _lbl = payload.get("label", _tok[:20])
                if not _tok or _shr <= 0:
                    self.send_json(400, {"error":"sell_position requires token_id and shares"}); return
                _ex = []
                if _o.path.exists(_sf):
                    try:
                        with open(_sf) as _f: _ex = _j.load(_f)
                    except: pass
                _ex.append({"token_id":_tok,"shares":_shr,"label":_lbl})
                with open(_sf,"w") as _f: _j.dump(_ex,_f)
                self.send_json(200, {"exit_code":0,"stdout":f"Queued sell: {_lbl} {_shr:.0f}sh","stderr":""}); return
            if command == "whitelist_script":
                _sn = payload.get("script","")
                if _sn and _sn not in COMMANDS: COMMANDS[_sn] = ["python3", f"/opt/polymarket-agent/{_sn}"]
                self.send_json(200, {"exit_code":0,"stdout":f"whitelisted {_sn}","stderr":""}); return
            if command == "sync_scripts":
                import subprocess as _ss
                _base = "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main"
                _dest = "/opt/polymarket-agent"
                _files = payload.get("files",["signal_engine.py","very_hot_forward_test.py","autotrader.py","opportunity_scanner.py","lp_quoter.py","position_monitor.py","whale_monitor.py","near_resolution_scanner.py"])
                _res = {}
                for _fn in _files:
                    _r = _ss.run(["curl","-fsSL",f"{_base}/{_fn}","-o",f"{_dest}/{_fn}"],capture_output=True,timeout=30)
                    _res[_fn] = "ok" if _r.returncode==0 else "fail"
                self.send_json(200, {"exit_code":0,"stdout":str(_res),"stderr":""}); return
            self.send_json(400, {"error": f"unknown command: {command}"})
            return

        cmd = COMMANDS[command]
        result = run_command(cmd, timeout=90)
        log.info(f"Command {command} exit={result['exit_code']}")
        self.send_json(200, result)


def main():
    if not SECRET:
        print("ERROR: EXECUTOR_SECRET not set in .env — set it before starting")
        print("  Generate one: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        exit(1)

    log.info(f"Executor starting on port {PORT} (threaded)")
    print(f"[executor] Listening on 0.0.0.0:{PORT} (threaded — each request in own thread)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ExecutorHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
