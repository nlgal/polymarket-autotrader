
import subprocess, os
# Download new executor.py
r = __import__("requests").get(
    "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py",
    timeout=15
)
with open("/opt/polymarket-agent/executor.py", "w") as f:
    f.write(r.text)
print("executor.py updated")
# Restart executor service
result = subprocess.run(["systemctl", "restart", "executor"], capture_output=True, text=True)
print(f"restart: {result.returncode} {result.stderr}")
print("Done")
