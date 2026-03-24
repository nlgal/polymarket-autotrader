"""
deploy_btc_momentum.py — One-time setup script for btc_momentum service.
Runs on server via executor. Sets up systemd service + installs websocket-client.
"""
import subprocess, requests, os, sys

AGENT_DIR = "/opt/polymarket-agent"
VENV_PY   = f"{AGENT_DIR}/venv/bin/python3"
VENV_PIP  = f"{AGENT_DIR}/venv/bin/pip"

print("=== BTC Momentum Bot Deployment ===")

# 1. Install websocket-client in venv
print("\n[1] Installing websocket-client...")
r = subprocess.run([VENV_PIP, "install", "websocket-client", "-q"],
                   capture_output=True, text=True)
print(f"  pip result: {r.returncode}")
if r.returncode == 0:
    print("  ✓ websocket-client installed")
else:
    print(f"  ✗ {r.stderr[:100]}")

# 2. Download btc_momentum.py
print("\n[2] Downloading btc_momentum.py...")
r2 = requests.get(
    "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/btc_momentum.py",
    timeout=15)
with open(f"{AGENT_DIR}/btc_momentum.py", "w") as f:
    f.write(r2.text)
print(f"  ✓ Downloaded ({len(r2.text)} bytes)")

# 3. Syntax check
print("\n[3] Syntax check...")
r3 = subprocess.run([VENV_PY, "-c", 
    f"import py_compile; py_compile.compile('{AGENT_DIR}/btc_momentum.py', doraise=True); print('OK')"],
    capture_output=True, text=True)
print(f"  {r3.stdout.strip() or r3.stderr.strip()[:100]}")

# 4. Create systemd service
print("\n[4] Creating systemd service...")
service = """[Unit]
Description=BTC 5-Minute Momentum Trader
After=network.target
Requires=polymarket.service

[Service]
Type=simple
WorkingDirectory=/opt/polymarket-agent
ExecStart=/opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/btc_momentum.py
Restart=always
RestartSec=10
User=root
EnvironmentFile=/opt/polymarket-agent/.env

[Install]
WantedBy=multi-user.target
"""

with open("/etc/systemd/system/btc-momentum.service", "w") as f:
    f.write(service)
print("  ✓ Service file written")

# 5. Enable and start
print("\n[5] Enabling and starting service...")
for cmd in [
    ["systemctl", "daemon-reload"],
    ["systemctl", "enable", "btc-momentum"],
    ["systemctl", "start", "btc-momentum"],
]:
    r4 = subprocess.run(cmd, capture_output=True, text=True)
    print(f"  {' '.join(cmd)}: {r4.returncode}")

import time; time.sleep(3)

# 6. Check status
r5 = subprocess.run(["systemctl", "status", "btc-momentum", "--no-pager", "-l"],
                    capture_output=True, text=True)
print("\n[6] Service status:")
print(r5.stdout[:500])

print("\n=== Deployment complete ===")
print(f"Logs: journalctl -u btc-momentum -f")
print(f"Or:   tail -f {AGENT_DIR}/btc_momentum.log")
