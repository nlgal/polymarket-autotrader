"""
Deploy latest opportunity_scanner.py from GitHub to server.
Uses GitHub API (not CDN) to guarantee we get the freshest commit, not cached content.
CDN (raw.githubusercontent.com) can lag 10-30 minutes behind actual commits.
"""
import requests, base64, json, os

GITHUB_API = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents/opportunity_scanner.py"
TARGET = "/opt/polymarket-agent/opportunity_scanner.py"

# Try GitHub API first (fresh, no cache)
try:
    r = requests.get(GITHUB_API, headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        sha = data.get("sha","?")[:8]
        method = "GitHub API"
    else:
        raise Exception(f"GitHub API {r.status_code}")
except Exception as e:
    # Fallback to CDN if API fails
    r2 = requests.get(
        "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py",
        timeout=15
    )
    content = r2.text
    sha = "cdn"
    method = "CDN fallback"

with open(TARGET, "w") as f:
    f.write(content)

# Verify fix is present
has_fix = "abs(gap)" in content and "<= 5.0" in content
old_bug = "yes_p < 0.5 and wti >= target * 0.99" in content

# Clear pycache
import glob
for pyc in glob.glob("/opt/polymarket-agent/**/*.pyc", recursive=True):
    if "opportunity_scanner" in pyc:
        try: os.remove(pyc)
        except: pass

print(f"Scanner deployed: {len(content)} chars (sha={sha}, via {method})")
print(f"Fix verified: has_gap_fix={has_fix}, old_bug_gone={not old_bug}")
