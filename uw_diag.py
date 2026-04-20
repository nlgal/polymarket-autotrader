
import os, sys, json, requests
sys.path.insert(0, "/opt/polymarket-agent")
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

UW_API_KEY = os.environ.get("UW_API_KEY","").strip()
UW_BASE = "https://api.unusualwhales.com"
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","")

if not UW_API_KEY:
    print("NO UW_API_KEY found")
    sys.exit(0)

headers = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}

# Get a few UW signals
r = requests.get(f"{UW_BASE}/api/predictions/unusual", headers=headers, timeout=10)
print(f"UW /unusual status: {r.status_code}")
if r.status_code == 200:
    items = r.json().get("data",{}).get("data",[])[:3]
    for item in items:
        aid = item.get("asset_id","")
        mkt = item.get("market","")
        print(f"  asset_id={aid!r} market={mkt!r}")

# Get our CLOB token IDs for comparison
r2 = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=3", timeout=8)
for p in (r2.json()[:3] if r2.ok else []):
    print(f"  CLOB token (asset)={p.get('asset','')!r} title={p.get('title','')[:30]!r}")
