"""
reset_state.py — Resets the autotrader state to NORMAL mode.
Run this if the bot is stuck in PAUSED or has stale state.
"""
import os, json
STATE_FILE = "/opt/polymarket-agent/state.json"
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        state = json.load(f)
    print("Before:", json.dumps({k: v for k, v in state.items() if k not in ["open_positions"]}, indent=2)[:400])
    state["mode"] = "NORMAL"
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print("Reset: mode=NORMAL")
else:
    print("State file not found")
