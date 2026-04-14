
import json, os
wl_file = '/opt/polymarket-agent/whale_watchlist.json'
state_file = '/opt/polymarket-agent/whale_monitor_state.json'
with open(wl_file) as f:
    wl = json.load(f)
wallets = wl.get('wallets', [])
print(f"Total wallets: {len(wallets)}")
for w in wallets:
    print(json.dumps(w))
