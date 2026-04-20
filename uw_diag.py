
import os, sys, json, requests
sys.path.insert(0, "/opt/polymarket-agent")
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

UW_API_KEY = os.environ.get("UW_API_KEY","").strip()
if not UW_API_KEY:
    print("NO UW_API_KEY")
    sys.exit(0)

headers = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
BASE = "https://api.unusualwhales.com"

for endpoint in ["/api/predictions/unusual", "/api/predictions/smart-money"]:
    r = requests.get(f"{BASE}{endpoint}", headers=headers, timeout=10)
    print(f"\n=== {endpoint} status={r.status_code} ===")
    if r.status_code == 200:
        data = r.json()
        # Print raw structure of first item
        items = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("data", [])
        if items:
            print("First item keys:", list(items[0].keys()))
            print("First item raw:", json.dumps(items[0], indent=2)[:800])
    else:
        print("Error:", r.text[:200])

# Also check our positions to see conditionId format
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","")
pos_r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=3", timeout=8)
print("\n=== OUR POSITION IDs ===")
for p in (pos_r.json()[:3] if pos_r.ok else []):
    print(f"  asset (CLOB token): {p.get('asset','')}")
    print(f"  conditionId:        {p.get('conditionId','')}")
    print(f"  title: {p.get('title','')[:50]}")
    print()
