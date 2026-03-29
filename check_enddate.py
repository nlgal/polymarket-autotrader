
with open("/opt/polymarket-agent/autotrader.py") as f:
    c = f.read()
print("End-date guard (_days_left < 3):", "_days_left < 3" in c)
print("Condition blacklist:", "_THESIS_BLACKLIST" in c)  
print("Keyword guard (connecticut):", "connecticut huskies" in c)
# Show the exact line count as proof of version
lines = c.split("\n")
print(f"File lines: {len(lines)}")
# Find the thesis section
for i, l in enumerate(lines):
    if "_days_left < 3" in l:
        print(f"  Line {i}: {l.strip()}")
        break
