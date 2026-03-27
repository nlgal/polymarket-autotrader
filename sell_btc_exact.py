
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    lines = f.readlines()
print(f"Total lines: {len(lines)}")
for i in range(509, 527):
    print(f"L{i+1}: {lines[i].rstrip()}")
