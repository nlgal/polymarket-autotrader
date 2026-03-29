
with open("/opt/polymarket-agent/autotrader.py") as f:
    content = f.read()
has_fix = "_SPORTS_GAME_KEYWORDS" in content
has_duke = '"duke"' in content
print(f"Sports thesis fix deployed: {has_fix}")
print(f"Duke keyword present: {has_duke}")
# Show the relevant lines
for i, line in enumerate(content.split("\n")):
    if "_SPORTS_GAME_KEYWORDS" in line or "sports game market" in line:
        print(f"  Line {i}: {line.strip()}")
