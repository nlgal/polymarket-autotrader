"""
Deploy latest opportunity_scanner.py + scanner_config.json from GitHub to server.
Uses GitHub API (not CDN) to guarantee freshest commit, no cache lag.
CDN (raw.githubusercontent.com) can lag 10-30min behind commits.
"""
import requests, base64, json, os, glob

GITHUB_API_BASE = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
TARGET_DIR = "/opt/polymarket-agent"

def fetch_github_file(path):
    r = requests.get(f"{GITHUB_API_BASE}/{path}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data.get("sha","?")[:8]
    raise Exception(f"GitHub API {r.status_code} for {path}")

# Deploy scanner
scanner, sha = fetch_github_file("opportunity_scanner.py")
with open(f"{TARGET_DIR}/opportunity_scanner.py", "w") as f:
    f.write(scanner)

# Deploy scanner config (blacklists, tunable params)
try:
    config, _ = fetch_github_file("scanner_config.json")
    with open(f"{TARGET_DIR}/scanner_config.json", "w") as f:
        f.write(config)
    print(f"Config deployed: {len(config)} chars")
except Exception as e:
    print(f"Config deploy failed (non-critical): {e}")

# Clear pycache
for pyc in glob.glob(f"{TARGET_DIR}/**/*.pyc", recursive=True):
    if "opportunity_scanner" in pyc:
        try: os.remove(pyc)
        except: pass

# Verify the key fixes are present
has_fix = "COMMODITY_BUFFER_USD" in scanner and "BLACKLISTED_CONDITIONS" in scanner
old_bug = "yes_p < 0.5 and wti >= target * 0.99" in scanner

print(f"Scanner deployed: {len(scanner)} chars (sha={sha}, via GitHub API)")
print(f"Fix verified: commodity_check={has_fix}, old_bug_gone={not old_bug}")
