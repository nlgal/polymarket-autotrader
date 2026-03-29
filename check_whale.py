
import os, json
wl = "/opt/polymarket-agent/whale_watchlist.json"
ws = "/opt/polymarket-agent/whale_scanner.py"
wm = "/opt/polymarket-agent/whale_monitor.py"
print(f"whale_scanner.py: {os.path.exists(ws)}")
print(f"whale_monitor.py: {os.path.exists(wm)}")
print(f"whale_watchlist.json: {os.path.exists(wl)}")
if os.path.exists(wl):
    with open(wl) as f:
        d = json.load(f)
    wallets = d.get("wallets", [])
    print(f"Watchlist has {len(wallets)} wallets")
    for w in wallets[:5]:
        print(f"  {w.get('name','?')}: ${w.get('pnl',0):,.0f} PnL")
