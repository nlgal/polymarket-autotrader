
import requests, json

r = requests.get(
    "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/sports_blacklist.json",
    timeout=10)
if r.status_code == 200:
    with open("/opt/polymarket-agent/sports_blacklist.json", "w") as f:
        f.write(r.text)
    data = json.loads(r.text)
    print(f"sports_blacklist.json deployed: {len(data.get('condition_ids',[]))} condition IDs")
    for cid in data.get("condition_ids", []):
        print(f"  {cid[:30]}...")
else:
    print(f"Failed to fetch: {r.status_code}")
