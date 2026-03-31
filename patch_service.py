
import subprocess, os

# Read current service file
result = subprocess.run(["cat", "/etc/systemd/system/polymarket.service"],
    capture_output=True, text=True)
current = result.stdout
print("Current ExecStart:", [l for l in current.split("\n") if "ExecStart" in l])

# Patch to use start_autotrader.py
if "start_autotrader.py" not in current:
    new = current.replace(
        "ExecStart=/opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/autotrader.py",
        "ExecStart=/opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/start_autotrader.py"
    )
    with open("/etc/systemd/system/polymarket.service", "w") as f:
        f.write(new)
    subprocess.run(["systemctl", "daemon-reload"])
    print("✓ Service patched to use start_autotrader.py")
else:
    print("✓ Already using start_autotrader.py")

# Verify
result2 = subprocess.run(["cat", "/etc/systemd/system/polymarket.service"],
    capture_output=True, text=True)
for l in result2.stdout.split("\n"):
    if "ExecStart" in l:
        print(f"ExecStart now: {l.strip()}")
