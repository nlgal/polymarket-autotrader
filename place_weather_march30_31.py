"""
Bootstrap: updates executor.py on the server to the latest version from GitHub,
then restarts the executor service.
"""
import subprocess, requests, os

print("=== Executor Bootstrap ===")

# Download new executor.py
r = requests.get(
    "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py",
    timeout=15
)
print(f"Downloaded executor.py ({len(r.text)} bytes)")

with open("/opt/polymarket-agent/executor.py", "w") as f:
    f.write(r.text)
print("Saved to /opt/polymarket-agent/executor.py")

# Restart executor service
result = subprocess.run(["systemctl", "restart", "executor"], 
                        capture_output=True, text=True)
print(f"Executor restart: returncode={result.returncode}")
if result.stderr:
    print(f"stderr: {result.stderr[:100]}")

import time
time.sleep(2)

# Verify it's running
status = subprocess.run(["systemctl", "is-active", "executor"], 
                        capture_output=True, text=True)
print(f"Executor status: {status.stdout.strip()}")

# Verify allowlist
if "opportunity_scanner" in r.text:
    print("✓ opportunity_scanner.py in new allowlist")
else:
    print("✗ NOT in allowlist")

print("Bootstrap complete")
