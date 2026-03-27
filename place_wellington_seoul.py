"""Deploy scanner + CLAUDE.md from GitHub, clear pyc cache."""
import requests, os, glob

# Download latest scanner from GitHub
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py', timeout=15)
content = r.text

# Apply AGENT_DIR fix if needed
old = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new = 'claude_md_path = "/opt/polymarket-agent/CLAUDE.md"'
if old in content:
    content = content.replace(old, new, 1)
    print("Applied AGENT_DIR fix")

# Write to disk
with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
    f.write(content)

# Clear pyc cache (prevents stale compiled bytecode)
for d in glob.glob('/opt/polymarket-agent/**/__pycache__', recursive=True):
    for fn in os.listdir(d):
        if 'opportunity_scanner' in fn:
            os.remove(os.path.join(d, fn))
            print(f"Cleared cache: {fn}")

# Also download CLAUDE.md
r2 = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/CLAUDE.md', timeout=10)
if r2.status_code == 200:
    with open('/opt/polymarket-agent/CLAUDE.md', 'w') as f:
        f.write(r2.text)
    print(f"CLAUDE.md deployed ({len(r2.text)} chars)")

import py_compile
py_compile.compile('/opt/polymarket-agent/opportunity_scanner.py', doraise=True)
print(f"Scanner deployed and verified ({len(content)} chars) — SYNTAX OK")
