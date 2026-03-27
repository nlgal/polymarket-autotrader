import os, glob

# Delete pyc caches
for d in glob.glob('/opt/polymarket-agent/**/__pycache__', recursive=True):
    for f in os.listdir(d):
        if 'opportunity_scanner' in f:
            os.remove(os.path.join(d, f))
            print(f"Deleted: {f}")

# Check actual lines 513-520
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    lines = f.readlines()

print(f"Lines: {len(lines)}")
for i in range(512, 521):
    print(f"L{i+1}: {repr(lines[i].rstrip())}")

# Check for any uw_sig before line 540
for i, l in enumerate(lines[:540]):
    if 'uw_sig' in l and 'uw_signals' not in l and 'uw_sig,' not in l:
        print(f"PROBLEM at L{i+1}: {l.rstrip()}")
