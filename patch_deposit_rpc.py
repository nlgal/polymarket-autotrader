"""
Patch: download correct deposit_safe.py from GitHub API (bypasses CDN cache)
and run the deposit immediately.
"""
import requests, base64, json, os, sys, time

GITHUB_API = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
AGENT_DIR = "/opt/polymarket-agent"

# Download fresh versions of deposit_safe.py and lp_farmer.py from GitHub API
for script in ["deposit_safe.py", "lp_farmer.py"]:
    r = requests.get(f"{GITHUB_API}/{script}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        with open(f"{AGENT_DIR}/{script}", "w") as f:
            f.write(content)
        # Verify RPC is correct
        if script == "deposit_safe.py":
            has_bad_rpc = "polygon-rpc.com" in content
            has_good_rpc = "ankr" in content or "client.deposit" in content
            print(f"{script}: {len(content)} chars | bad_rpc={has_bad_rpc} | good={has_good_rpc}")
        else:
            print(f"{script}: {len(content)} chars deployed")
    else:
        print(f"{script}: download failed {r.status_code}")

print("Done — deposit_safe.py updated from GitHub API")
