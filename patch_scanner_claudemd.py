"""One-time patch: fix AGENT_DIR in opportunity_scanner.py"""
path = '/opt/polymarket-agent/opportunity_scanner.py'
with open(path) as f:
    content = f.read()

old = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new = 'claude_md_path = "/opt/polymarket-agent/CLAUDE.md"'

if old in content:
    content = content.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(content)
    print("✅ AGENT_DIR fix applied to opportunity_scanner.py")
else:
    # Check if already fixed
    if '/opt/polymarket-agent/CLAUDE.md' in content or '"/opt/polymarket-agent", "CLAUDE.md"' in content:
        print("Already fixed")
    else:
        # Find what's there
        for i, l in enumerate(content.split('\n')):
            if 'claude_md' in l.lower():
                print(f"L{i+1}: {l}")
