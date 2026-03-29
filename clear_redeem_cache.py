
import os, glob

# Remove auto_redeem .pyc files
for pyc in glob.glob("/opt/polymarket-agent/**/*.pyc", recursive=True):
    if "auto_redeem" in pyc:
        os.remove(pyc)
        print(f"Removed: {pyc}")

for pyc in glob.glob("/opt/polymarket-agent/__pycache__/auto_redeem*"):
    os.remove(pyc)
    print(f"Removed: {pyc}")

# Verify current file line 27
with open("/opt/polymarket-agent/auto_redeem.py") as f:
    lines = f.readlines()
print(f"Line 27: {lines[26].strip()}")
print("Cache cleared")
