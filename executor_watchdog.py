"""
executor_watchdog.py
====================
Runs as a systemd service alongside the executor.
Monitors executor health and sends Slack + Telegram alerts on crash,
including the last request logged for replay.

Architecture:
  - executor.py writes every incoming request to /opt/polymarket-agent/executor_last_request.json
    BEFORE processing it (pre-write log)
  - This watchdog polls executor health every 30s
  - On crash: reads last_request.json, sends alert with full replay command
  - On recovery: sends all-clear

Alert contains:
  - Crash time
  - Last request (command + payload) for manual replay
  - Number of crash-restarts since last alert
  - How to replay: exec_server("<cmd>", **payload)
"""
import os, sys, json, time, datetime, requests, hmac, hashlib, subprocess

# ── Config ─────────────────────────────────────────────────────────────────────
EXECUTOR_URL      = "http://127.0.0.1:8888"
SECRET_FILE       = "/opt/polymarket-agent/.env"
LAST_REQ_FILE     = "/opt/polymarket-agent/executor_last_request.json"
STATE_FILE        = "/opt/polymarket-agent/watchdog_state.json"
POLL_INTERVAL     = 30   # seconds between health checks
CRASH_COOLDOWN    = 120  # seconds between repeated crash alerts
TG_TOKEN          = None
TG_CHAT           = None
SLACK_TOKEN       = None
SLACK_CHANNEL     = "C06PLRZ98EM"

def load_env():
    global TG_TOKEN, TG_CHAT, SLACK_TOKEN
    try:
        with open(SECRET_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_TOKEN="):
                    TG_TOKEN = line.split("=", 1)[1].strip().strip('"\'')
                elif line.startswith("TELEGRAM_CHAT_ID="):
                    TG_CHAT = line.split("=", 1)[1].strip().strip('"\'')
                elif line.startswith("SLACK_BOT_TOKEN="):
                    SLACK_TOKEN = line.split("=", 1)[1].strip().strip('"\'')
    except:
        pass

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"was_healthy": True, "last_alert_ts": 0, "crash_count": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def executor_healthy():
    """Check executor by calling service_status via systemctl (not HTTP, to avoid circular dependency)."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "executor"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except:
        return False

def read_last_request():
    try:
        with open(LAST_REQ_FILE) as f:
            return json.load(f)
    except:
        return None

def send_slack(msg):
    if not SLACK_TOKEN:
        return
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                     "Content-Type": "application/json"},
            json={"channel": SLACK_CHANNEL, "text": msg, "mrkdwn": True},
            timeout=10
        )
    except:
        pass

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

def alert_crash(last_req, crash_count):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    replay = ""
    if last_req:
        cmd     = last_req.get("command", "unknown")
        payload = {k: v for k, v in last_req.items() if k != "command"}
        payload_str = json.dumps(payload, indent=2) if payload else "{}"
        replay = (
            f"\n*Last request (replay):*\n"
            f"```\nexec_server(\"{cmd}\", **{payload_str})\n```"
        )
    slack_msg = (
        f":rotating_light: *Executor crashed* at {ts}\n"
        f"Crash #{crash_count} since last alert{replay}\n"
        f"*Replay last request:* `python3 /opt/polymarket-agent/replay_last_request.py`\n"
        f"*Restore executor:* `curl -fsSL https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py -o /opt/polymarket-agent/executor.py && systemctl restart executor`"
    )
    tg_msg = (
        f"🚨 <b>Executor crashed</b> at {ts}\n"
        f"Crash #{crash_count}"
        + (f"\n\nLast request:\n<code>exec_server(\"{cmd}\")</code>" if last_req else "")
        + f"\n\nReplay: <code>python3 /opt/polymarket-agent/replay_last_request.py</code>"
        + f"\nRestore: <code>curl -fsSL https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py -o /opt/polymarket-agent/executor.py && systemctl restart executor</code>"
    )
    send_slack(slack_msg)
    send_telegram(tg_msg)
    print(f"[watchdog] CRASH ALERT sent at {ts}")

def alert_recovery():
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    send_slack(f":white_check_mark: *Executor recovered* at {ts}")
    send_telegram(f"✅ <b>Executor recovered</b> at {ts}")
    print(f"[watchdog] RECOVERY alert sent at {ts}")

def run():
    load_env()
    print(f"[watchdog] Starting — polling every {POLL_INTERVAL}s")
    print(f"[watchdog] Slack channel: {SLACK_CHANNEL} | Telegram: {'yes' if TG_TOKEN else 'no'}")

    state = load_state()

    while True:
        try:
            healthy = executor_healthy()

            if healthy:
                if not state["was_healthy"]:
                    alert_recovery()
                    state["was_healthy"] = True
                    state["crash_count"] = 0
                    save_state(state)
            else:
                state["crash_count"] = state.get("crash_count", 0) + 1
                now = time.time()
                if now - state.get("last_alert_ts", 0) > CRASH_COOLDOWN:
                    last_req = read_last_request()
                    alert_crash(last_req, state["crash_count"])
                    state["last_alert_ts"] = now
                state["was_healthy"] = False
                save_state(state)

        except Exception as e:
            print(f"[watchdog] Error in loop: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
