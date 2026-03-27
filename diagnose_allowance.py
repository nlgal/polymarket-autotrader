
import re

path = '/opt/polymarket-agent/opportunity_scanner.py'
with open(path) as f:
    content = f.read()

# Show current line 235
lines = content.split('\n')
for i in range(232, 250):
    print(f"L{i+1}: {lines[i]}")

# Apply the fix
old = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new = 'claude_md_path = os.path.join("/opt/polymarket-agent", "CLAUDE.md")'

if old in content:
    content = content.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(content)
    print("\nFix applied!")
else:
    print("\nOld pattern not found — checking what IS there:")
    for i, l in enumerate(lines):
        if 'CLAUDE.md' in l or 'load_claude_md' in l:
            print(f"  L{i+1}: {l}")
