"""Clean executor restart - download latest, kill hanging processes, restart."""
import subprocess, requests, time, os

AGENT_DIR = "/opt/polymarket-agent"

print("=== Clean Executor Restart ===")

# Kill any hanging Python processes on port 8888
kill = subprocess.run(["fuser", "-k", "8888/tcp"], capture_output=True, text=True)
print(f"Killed port 8888: {kill.returncode}")
time.sleep(2)

# Download latest executor
r = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py", timeout=15)
with open(f"{AGENT_DIR}/executor.py", "w") as f: f.write(r.text)
print(f"Downloaded executor ({len(r.text)} chars)")
print(f"  hormuz_rebalance in allowlist: {'hormuz_rebalance' in r.text}")
print(f"  exit_contradictions in allowlist: {'exit_contradictions' in r.text}")

# Restart executor service
subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
result = subprocess.run(["systemctl", "restart", "executor"], capture_output=True, text=True)
print(f"Restart: {result.returncode} {result.stderr[:50]}")
time.sleep(3)

status = subprocess.run(["systemctl", "status", "executor", "--no-pager"], capture_output=True, text=True)
for l in status.stdout.split("\n")[:6]:
    if l.strip(): print(l)
print("Done")
