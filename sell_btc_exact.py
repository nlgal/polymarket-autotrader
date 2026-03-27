
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    lines = f.readlines()
for i, l in enumerate(lines[:545]):
    if 'uw_sig' in l:
        print(f"L{i+1}: {l.rstrip()}")
