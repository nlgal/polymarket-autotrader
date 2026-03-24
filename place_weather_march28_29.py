"""Bootstrap: updates executor.py on server and restarts executor service."""
import subprocess, requests, os, time

print("=== Executor Bootstrap v2 ===")

r = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py", timeout=15)
with open("/opt/polymarket-agent/executor.py", "w") as f:
    f.write(r.text)
print(f"Downloaded executor.py ({len(r.text)} chars)")

result = subprocess.run(["systemctl", "restart", "executor"], capture_output=True, text=True)
print(f"Restart: {result.returncode}")
time.sleep(2)

status = subprocess.run(["systemctl", "is-active", "executor"], capture_output=True, text=True)
print(f"Status: {status.stdout.strip()}")

if "deploy_btc_momentum" in r.text and "opportunity_scanner" in r.text:
    print("✓ New allowlist confirmed")
else:
    print("✗ Allowlist check failed")

print("Done")
