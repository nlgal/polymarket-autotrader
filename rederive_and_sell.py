
import subprocess, requests, os

# Download CLAUDE.md from GitHub
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/CLAUDE.md', timeout=10)
if r.status_code == 200:
    with open('/opt/polymarket-agent/CLAUDE.md', 'w') as f:
        f.write(r.text)
    print(f"CLAUDE.md deployed ({len(r.text)} chars)")
else:
    print(f"CLAUDE.md download failed: {r.status_code}")

# Also update opportunity_scanner.py with the new version
r2 = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py', timeout=15)
if r2.status_code == 200:
    with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
        f.write(r2.text)
    print(f"Scanner updated ({len(r2.text)} chars)")
print("Done")
