
import os, glob

# Delete any .pyc files for opportunity_scanner
for f in glob.glob('/opt/polymarket-agent/**/*.pyc', recursive=True):
    if 'opportunity_scanner' in f:
        os.remove(f)
        print(f"Deleted: {f}")

for d in glob.glob('/opt/polymarket-agent/**/__pycache__', recursive=True):
    for f in os.listdir(d):
        if 'opportunity_scanner' in f:
            fp = os.path.join(d, f)
            os.remove(fp)
            print(f"Deleted cached: {fp}")

# Show line 516
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    lines = f.readlines()
print(f"Total lines: {len(lines)}")
print(f"L515: {lines[514].rstrip()}")
print(f"L516: {lines[515].rstrip()}")
print(f"L517: {lines[516].rstrip()}")
print(f"L518: {lines[517].rstrip()}")

# Also check if uw_sig appears before line 530
for i, l in enumerate(lines[:530]):
    if 'uw_sig' in l:
        print(f"uw_sig at L{i+1}: {l.rstrip()}")
