import requests
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/lessons.md', timeout=10)
if r.status_code == 200:
    with open('/opt/polymarket-agent/lessons.md', 'w') as f:
        f.write(r.text)
    print(f"lessons.md deployed ({len(r.text)} chars)")
else:
    print(f"Download failed: {r.status_code}")
    
# Also deploy updated CLAUDE.md with lessons reference
r2 = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/CLAUDE.md', timeout=10)
with open('/opt/polymarket-agent/CLAUDE.md', 'w') as f:
    f.write(r2.text)
print(f"CLAUDE.md updated ({len(r2.text)} chars)")
print("Done")
