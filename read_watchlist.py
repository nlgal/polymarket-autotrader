
import json
with open("/opt/polymarket-agent/whale_watchlist.json") as f:
    wl = json.load(f)
for w in wl:
    print(w.get("name","?"), "|", w.get("proxy_wallet","") or w.get("address",""))
