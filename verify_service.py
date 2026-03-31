
import subprocess
r = subprocess.run(["cat", "/etc/systemd/system/polymarket.service"], capture_output=True, text=True)
for l in r.stdout.split("\n"):
    if "ExecStart" in l:
        print(l.strip())
# Also verify start_autotrader.py exists
import os
exists = os.path.exists("/opt/polymarket-agent/start_autotrader.py")
print(f"start_autotrader.py exists: {exists}")
