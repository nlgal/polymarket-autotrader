
with open("/opt/polymarket-agent/auto_redeem.py") as f:
    lines = f.readlines()
for i, l in enumerate(lines[:35], 1):
    print(f"{i:3}: {l}", end="")
import os, glob
pycs = glob.glob("/opt/polymarket-agent/**/*.pyc", recursive=True)
print(f"\n\n.pyc files: {[p for p in pycs if 'auto_redeem' in p]}")
