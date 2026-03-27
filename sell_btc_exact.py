
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    lines = f.readlines()

# Find all uw_sig references before the proper assignment
print(f"Total lines: {len(lines)}")
for i, l in enumerate(lines):
    if 'uw_sig' in l and i < 545:
        print(f"L{i+1}: {l.rstrip()}")
