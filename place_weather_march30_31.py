"""
Full deploy + executor restart.
Downloads latest executor.py (with updated whitelist) and strategy_optimizer.py,
restarts the executor service to pick up new whitelist entries.
"""
import subprocess, requests, base64, json, time, os

AGENT_DIR = "/opt/polymarket-agent"
GITHUB_API = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"

RAW_URL = "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main"

def fetch(path):
    # Try raw CDN first (no rate limit), fall back to API
    r = requests.get(f"{RAW_URL}/{path}", timeout=20)
    if r.status_code == 200:
        return r.text
    # Fallback: GitHub API
    r2 = requests.get(f"{GITHUB_API}/{path}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=20)
    if r2.status_code == 200:
        data = r2.json()
        return base64.b64decode(data["content"]).decode("utf-8")
    raise Exception(f"fetch failed: {path} CDN={r.status_code} API={r2.status_code}")

print("=== Full Deploy + Executor Restart ===")

# 1. Deploy strategy_optimizer.py (fix KeyError: version)
print("\n[1] Deploying strategy_optimizer.py...")
optimizer = fetch("strategy_optimizer.py")
with open(f"{AGENT_DIR}/strategy_optimizer.py", "w") as f:
    f.write(optimizer)
has_fix = 'cfg.get("version", 0)' in optimizer or 'str(int(' in optimizer
print(f"  Deployed {len(optimizer)} chars | version_fix={has_fix}")

# 2. Deploy executor.py (updated whitelist with sell_btc65k_no + deploy_config)
print("\n[2] Deploying executor.py...")
executor = fetch("executor.py")
with open(f"{AGENT_DIR}/executor.py", "w") as f:
    f.write(executor)
print(f"  Deployed {len(executor)} chars")
print(f"  sell_btc65k_no in whitelist: {'sell_btc65k_no' in executor}")
print(f"  deploy_config in whitelist: {'deploy_config' in executor}")
print(f"  whale_scanner in whitelist: {'whale_scanner' in executor}")
print(f"  whale_monitor in whitelist: {'whale_monitor' in executor}")

# 2b. Deploy whale scripts
print("\n[2b] Deploying whale_scanner.py + whale_monitor.py...")
for script in ["whale_scanner.py", "whale_monitor.py", "auto_redeem.py",
               "discord_monitor.py", "inject_discord_env.py"]:
    try:
        content = fetch(script)
        with open(f"{AGENT_DIR}/{script}", "w") as f:
            f.write(content)
        print(f"  {script}: {len(content)} chars")
    except Exception as e:
        print(f"  {script}: FAILED — {e}")

# 3. Kill hanging processes and restart executor
print("\n[3] Restarting executor service...")
kill = subprocess.run(["fuser", "-k", "8888/tcp"], capture_output=True, text=True)
print(f"  Killed port 8888: returncode={kill.returncode}")
time.sleep(2)

# Start executor as background process
proc = subprocess.Popen(
    [f"{AGENT_DIR}/venv/bin/python3", f"{AGENT_DIR}/executor.py"],
    cwd=AGENT_DIR,
    stdout=open(f"{AGENT_DIR}/executor.log", "a"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
time.sleep(2)
print(f"  Executor restarted (pid={proc.pid})")

print("\n=== Deploy complete ===")
