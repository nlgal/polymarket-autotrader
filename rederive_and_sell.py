"""
Deploy latest files from GitHub to server + restart services.
Uses GitHub API (not CDN) — no cache lag.

Deploys:
- opportunity_scanner.py
- scanner_config.json
- strategy_optimizer.py
- autotrader.py  (with blacklist + commodity check)
Then restarts polymarket.service so new autotrader code takes effect.
"""
import requests, base64, json, os, glob, subprocess

GITHUB_API_BASE = "https://api.github.com/repos/nlgal/polymarket-autotrader/contents"
TARGET_DIR = "/opt/polymarket-agent"

def fetch(path):
    r = requests.get(f"{GITHUB_API_BASE}/{path}",
        headers={"Accept": "application/vnd.github.v3+json"}, timeout=25)
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data.get("sha","?")[:8]
    raise Exception(f"GitHub API {r.status_code} for {path}")

# 1. Deploy scanner
scanner, sha = fetch("opportunity_scanner.py")
with open(f"{TARGET_DIR}/opportunity_scanner.py", "w") as f:
    f.write(scanner)

# 2. Deploy scanner config
try:
    config, _ = fetch("scanner_config.json")
    with open(f"{TARGET_DIR}/scanner_config.json", "w") as f:
        f.write(config)
    print(f"Config deployed: {len(config)} chars")
except Exception as e:
    print(f"Config deploy failed: {e}")

# 3. Deploy strategy optimizer
try:
    optimizer, opt_sha = fetch("strategy_optimizer.py")
    with open(f"{TARGET_DIR}/strategy_optimizer.py", "w") as f:
        f.write(optimizer)
    print(f"Optimizer deployed: {len(optimizer)} chars (sha={opt_sha})")
except Exception as e:
    print(f"Optimizer deploy failed: {e}")

# 4. Deploy HARD_RULES.md (agent guardrails — read before every trade decision)
try:
    hard_rules, hr_sha = fetch("HARD_RULES.md")
    with open(f"{TARGET_DIR}/HARD_RULES.md", "w") as f:
        f.write(hard_rules)
    print(f"HARD_RULES deployed: {len(hard_rules)} chars (sha={hr_sha})")
except Exception as e:
    print(f"HARD_RULES deploy failed: {e}")

# 5. Deploy autotrader.py (CRITICAL — has blacklist + commodity check)
try:
    autotrader, at_sha = fetch("autotrader.py")
    with open(f"{TARGET_DIR}/autotrader.py", "w") as f:
        f.write(autotrader)
    has_bl = "_AUTOTRADER_BLACKLIST" in autotrader
    has_cc = "_get_live_price" in autotrader
    print(f"Autotrader deployed: {len(autotrader)} chars (sha={at_sha}) | blacklist={has_bl} | commodity_check={has_cc}")
    
    # Restart polymarket service to pick up new autotrader
    r1 = subprocess.run(["systemctl", "restart", "polymarket"],
        capture_output=True, text=True, timeout=15)
    r2 = subprocess.run(["systemctl", "is-active", "polymarket"],
        capture_output=True, text=True)
    print(f"polymarket service: restart={r1.returncode} status={r2.stdout.strip()}")
except Exception as e:
    print(f"Autotrader deploy failed: {e}")

# 6. Deploy live_sports_trader.py
try:
    sports, sp_sha = fetch("live_sports_trader.py")
    with open(f"{TARGET_DIR}/live_sports_trader.py", "w") as f:
        f.write(sports)
    print(f"Sports trader deployed: {len(sports)} chars (sha={sp_sha})")
except Exception as e:
    print(f"Sports trader deploy failed: {e}")

# 7. Deploy updated executor.py (with live_sports_trader.py in whitelist)
try:
    executor, ex_sha = fetch("executor.py")
    with open(f"{TARGET_DIR}/executor.py", "w") as f:
        f.write(executor)
    has_sports = "live_sports_trader.py" in executor
    print(f"Executor deployed: {len(executor)} chars (sha={ex_sha}) | sports_whitelisted={has_sports}")
    # Restart executor service to reload whitelist
    rx = subprocess.run(["systemctl", "restart", "executor"],
        capture_output=True, text=True, timeout=15)
    rx2 = subprocess.run(["systemctl", "is-active", "executor"],
        capture_output=True, text=True)
    print(f"executor service: restart={rx.returncode} status={rx2.stdout.strip()}")
except Exception as e:
    print(f"Executor deploy failed: {e}")

# 8. Clear ALL pycache — prevents stale bytecode from overriding fixes
import shutil
for pyc in glob.glob(f"{TARGET_DIR}/**/*.pyc", recursive=True):
    try: os.remove(pyc)
    except: pass
for cache_dir in glob.glob(f"{TARGET_DIR}/**/__pycache__", recursive=True):
    try: shutil.rmtree(cache_dir)
    except: pass
print("pycache cleared")

# Verify scanner fix
has_fix = "COMMODITY_BUFFER_USD" in scanner and "BLACKLISTED_CONDITIONS" in scanner
old_bug = "yes_p < 0.5 and wti >= target * 0.99" in scanner
print(f"Scanner deployed: {len(scanner)} chars (sha={sha}, via GitHub API)")
print(f"Fix verified: commodity_check={has_fix}, old_bug_gone={not old_bug}")

# 9. Deploy discord_monitor.py
try:
    discord_mon, dm_sha = fetch("discord_monitor.py")
    with open(f"{TARGET_DIR}/discord_monitor.py", "w") as f:
        f.write(discord_mon)
    print(f"Discord monitor deployed: {len(discord_mon)} chars (sha={dm_sha})")
except Exception as e:
    print(f"Discord monitor deploy failed: {e}")

# 10. Ensure DISCORD_TOKEN is in .env (add if missing)
try:
    env_path = f"{TARGET_DIR}/.env"
    with open(env_path) as f:
        env_content = f.read()
    if "DISCORD_TOKEN=" not in env_content:
        # Token will be injected by patch script on first run
        print("DISCORD_TOKEN: not yet in .env (run patch_discord_env.py to add)")
    else:
        print("DISCORD_TOKEN: already in .env ✓")
except Exception as e:
    print(f".env check failed: {e}")
