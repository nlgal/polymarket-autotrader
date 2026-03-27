"""
Deploy scanner_config.json from GitHub to server.
Run this whenever scanner_config.json is updated.
"""
import requests, base64, json, os

GITHUB_API_BASE = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
TARGET_DIR = "/opt/polymarket-agent"

def fetch_github_file(path):
    r = requests.get(f"{GITHUB_API_BASE}/{path}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data.get("sha","?")[:8]
    raise Exception(f"GitHub API {r.status_code} for {path}")

config, sha = fetch_github_file("scanner_config.json")
with open(f"{TARGET_DIR}/scanner_config.json", "w") as f:
    f.write(config)

data = json.loads(config)
n_blacklisted = len(data.get("BLACKLISTED_CONDITION_IDS", {}))
buffer = data.get("COMMODITY_BUFFER_USD", "?")

print(f"scanner_config.json deployed (sha={sha})")
print(f"  blacklisted markets: {n_blacklisted}")
print(f"  commodity buffer: ${buffer}")
print(f"  min edge: {data.get('MIN_SCAN_EDGE','?')}")
