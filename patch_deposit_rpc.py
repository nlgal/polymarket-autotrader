"""
Comprehensive patch:
1. Fix eth_utils eth_networks.json — swap polygon-rpc.com to working RPC
2. Download correct deposit_safe.py + lp_farmer.py from GitHub API
3. Run the deposit
"""
import requests, base64, json, os, sys

GITHUB_API = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
AGENT_DIR = "/opt/polymarket-agent"
GOOD_RPC = "https://rpc.ankr.com/polygon"
BAD_RPC = "https://polygon-rpc.com/"

# Step 1: Patch eth_networks.json to fix Polygon RPC
import glob
eth_network_files = glob.glob(
    f"{AGENT_DIR}/venv/lib/python*/site-packages/eth_utils/__json/eth_networks.json"
)
# Also check system Python
eth_network_files += glob.glob(
    "/usr/local/lib/python*/site-packages/eth_utils/__json/eth_networks.json"
)
eth_network_files = list(set(eth_network_files))

for fpath in eth_network_files:
    if not os.path.exists(fpath):
        continue
    with open(fpath) as f:
        networks = json.load(f)
    
    patched = False
    for net in networks:
        if net.get("chainId") == 137:  # Polygon
            rpcs = net.get("rpc", [])
            if BAD_RPC in rpcs:
                # Move bad RPC to end, add good RPC at front
                rpcs = [r for r in rpcs if r != BAD_RPC]
                rpcs.insert(0, GOOD_RPC)
                net["rpc"] = rpcs
                patched = True
                print(f"✓ Patched Polygon RPC in {fpath}")
                print(f"  First RPC now: {rpcs[0]}")
    
    if patched:
        with open(fpath, 'w') as f:
            json.dump(networks, f)

# Step 2: Download correct scripts from GitHub API
for script in ["deposit_safe.py", "lp_farmer.py"]:
    r = requests.get(f"{GITHUB_API}/{script}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        with open(f"{AGENT_DIR}/{script}", "w") as f:
            f.write(content)
        print(f"✓ {script}: {len(content)} chars deployed")
    else:
        print(f"✗ {script}: {r.status_code}")

print("\nAll patches complete. Ready to deposit.")
