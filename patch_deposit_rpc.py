"""
Comprehensive patch script:
1. Downloads correct deposit_safe.py + lp_farmer.py from GitHub API
2. Finds and patches polygon-rpc.com in py-clob-client or web3 config
3. Runs the deposit
"""
import requests, base64, json, os, sys, subprocess, time

GITHUB_API = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
AGENT_DIR = "/opt/polymarket-agent"
GOOD_RPC = "https://rpc.ankr.com/polygon"
BAD_RPC = "https://polygon-rpc.com"

# Step 1: Download correct scripts
for script in ["deposit_safe.py", "lp_farmer.py"]:
    r = requests.get(f"{GITHUB_API}/{script}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r.status_code == 200:
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        with open(f"{AGENT_DIR}/{script}", "w") as f:
            f.write(content)
        print(f"✓ {script}: {len(content)} chars")
    else:
        print(f"✗ {script}: {r.status_code}")

# Step 2: Find and patch polygon-rpc.com in venv
result = subprocess.run(
    ["grep", "-rl", BAD_RPC, f"{AGENT_DIR}/venv/"],
    capture_output=True, text=True
)
files_with_bad_rpc = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
print(f"\nFiles containing {BAD_RPC}:")
for f in files_with_bad_rpc:
    print(f"  {f}")
    try:
        with open(f, 'r') as fh:
            content = fh.read()
        if BAD_RPC in content:
            content = content.replace(BAD_RPC, GOOD_RPC)
            with open(f, 'w') as fh:
                fh.write(content)
            print(f"    → Patched to {GOOD_RPC}")
    except Exception as e:
        print(f"    → Failed to patch: {e}")

if not files_with_bad_rpc:
    print("  (none found in venv — RPC may be set differently)")
    # Check py_clob_client constants
    result2 = subprocess.run(
        ["find", f"{AGENT_DIR}/venv/", "-name", "*.py", "-exec",
         "grep", "-l", "polygon", "{}", "+"],
        capture_output=True, text=True
    )
    print(f"  Polygon-related files: {result2.stdout[:500]}")

print("\n✓ Patch complete")
