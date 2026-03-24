
import subprocess, requests, os

# Download new executor.py
r = requests.get(
    "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py",
    timeout=15
)
with open("/opt/polymarket-agent/executor.py", "w") as f:
    f.write(r.text)
print("executor.py downloaded, size:", len(r.text))

# Restart executor service
result = subprocess.run(["systemctl", "restart", "executor"], 
                        capture_output=True, text=True)
print("executor restart:", result.returncode, result.stderr[:100])

# Also verify opportunity_scanner is in the new allowlist
if "opportunity_scanner" in r.text:
    print("✓ opportunity_scanner.py in new allowlist")
else:
    print("✗ opportunity_scanner.py NOT found in new allowlist")
    
print("Done")
