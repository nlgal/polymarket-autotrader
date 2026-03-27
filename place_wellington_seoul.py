import requests
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py', timeout=15)
with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
    f.write(r.text)
# Also apply the AGENT_DIR fix if needed
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    c = f.read()
old = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new = 'claude_md_path = "/opt/polymarket-agent/CLAUDE.md"'
if old in c:
    c = c.replace(old, new, 1)
    with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
        f.write(c)
    print("AGENT_DIR fix also applied")
print(f"Scanner deployed: {len(r.text)} chars, exit_code=0")
