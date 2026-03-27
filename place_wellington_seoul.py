import requests

# Step 1: Download from GitHub (has both fixes already)
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py', timeout=15)
content = r.text
print(f"Downloaded {len(content)} chars from GitHub")

# Step 2: Apply AGENT_DIR fix (just in case)
old = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new = 'claude_md_path = "/opt/polymarket-agent/CLAUDE.md"'
if old in content:
    content = content.replace(old, new, 1)
    print("Applied AGENT_DIR fix")

# Step 3: Verify no uw_sig issue
lines = content.split("\n")
for i, l in enumerate(lines):
    if "not uw_sig" in l and i < 545:
        print(f"WARNING: still has uw_sig at L{i+1}: {l}")

# Step 4: Write to disk
with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
    f.write(content)
print("Written to disk")

# Step 5: Syntax check
import py_compile
try:
    py_compile.compile('/opt/polymarket-agent/opportunity_scanner.py', doraise=True)
    print("SYNTAX OK")
except Exception as e:
    print(f"SYNTAX ERROR: {e}")

# Step 6: Check L516-518
lines2 = content.split("\n")
for i in range(514, 523):
    print(f"L{i+1}: {lines2[i]}")
