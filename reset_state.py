
import json, os
STATE = "/opt/polymarket-agent/state.json"
with open(STATE) as f:
    state = json.load(f)

print("Before:", json.dumps({k: v for k, v in state.items() if k in ["mode","equity_sod","equity_peak_eod","sod_date"]}, indent=2))

import requests
FUNDER = "0xc2c1892653C175113c65961C7F4227c18D09b52a"
r = requests.get(f"https://data-api.polymarket.com/value?user={FUNDER}", timeout=10)
real_equity = r.json()[0]["value"]
print(f"Real equity from Polymarket API: ${real_equity:.2f}")

from datetime import date
state["mode"] = "NORMAL"
state["equity_sod"] = real_equity
state["sod_date"] = date.today().isoformat()
# Keep peak at or above real equity
if not state.get("equity_peak_eod") or state["equity_peak_eod"] < real_equity:
    state["equity_peak_eod"] = real_equity

with open(STATE, "w") as f:
    json.dump(state, f)

print("After:", json.dumps({k: v for k, v in state.items() if k in ["mode","equity_sod","equity_peak_eod","sod_date"]}, indent=2))
print("State reset complete.")
