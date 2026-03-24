"""
Full deployment: updates executor AND sets up btc-momentum service in one shot.
"""
import subprocess, requests, os, time

AGENT_DIR = "/opt/polymarket-agent"
VENV_PY   = f"{AGENT_DIR}/venv/bin/python3"
VENV_PIP  = f"{AGENT_DIR}/venv/bin/pip"

print("=== Full Deploy: Executor + BTC Momentum ===")

# 1. Update executor.py
print("\n[1] Updating executor.py...")
r = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py", timeout=15)
with open(f"{AGENT_DIR}/executor.py", "w") as f:
    f.write(r.text)
print(f"  Downloaded ({len(r.text)} chars)")
check = "deploy_btc_momentum" in r.text and "opportunity_scanner" in r.text
print(f"  Allowlist OK: {check}")

# 2. Install websocket-client
print("\n[2] Installing websocket-client...")
r2 = subprocess.run([VENV_PIP, "install", "websocket-client", "-q"], capture_output=True, text=True)
print(f"  pip: {r2.returncode}")

# 3. Download btc_momentum.py
print("\n[3] Downloading btc_momentum.py...")
r3 = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/btc_momentum.py", timeout=15)
with open(f"{AGENT_DIR}/btc_momentum.py", "w") as f:
    f.write(r3.text)
print(f"  Downloaded ({len(r3.text)} bytes)")

# 4. Syntax check
r4 = subprocess.run([VENV_PY, "-c", 
    f"import py_compile; py_compile.compile('{AGENT_DIR}/btc_momentum.py', doraise=True); print('OK')"],
    capture_output=True, text=True)
print(f"  Syntax: {r4.stdout.strip() or r4.stderr[:100]}")

# 5. Create systemd service
print("\n[4] Creating btc-momentum.service...")
service = """[Unit]
Description=BTC 5-Minute Momentum Trader
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/polymarket-agent
ExecStart=/opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/btc_momentum.py
Restart=always
RestartSec=15
User=root

[Install]
WantedBy=multi-user.target
"""
with open("/etc/systemd/system/btc-momentum.service", "w") as f:
    f.write(service)

for cmd in [["systemctl", "daemon-reload"], 
            ["systemctl", "enable", "btc-momentum"],
            ["systemctl", "start", "btc-momentum"]]:
    r5 = subprocess.run(cmd, capture_output=True, text=True)
    print(f"  {' '.join(cmd[-2:])}: {r5.returncode}")

time.sleep(3)

# 6. Status
r6 = subprocess.run(["systemctl", "status", "btc-momentum", "--no-pager"],
                    capture_output=True, text=True)
print("\n[5] Service status:")
for line in r6.stdout.split("\n")[:10]:
    if line.strip():
        print(f"  {line}")

# 7. Restart executor with new allowlist
print("\n[6] Restarting executor with new allowlist...")
subprocess.run(["systemctl", "restart", "executor"], capture_output=True)
time.sleep(2)
print("  Done")

print("\n=== Deployment Complete ===")
