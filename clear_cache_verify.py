
import os, glob, subprocess
agent = "/opt/polymarket-agent"

# Delete all .pyc files
removed = []
for pyc in glob.glob(f"{agent}/**/*.pyc", recursive=True):
    os.remove(pyc)
    removed.append(os.path.basename(pyc))

# Delete __pycache__ dirs
for d in glob.glob(f"{agent}/**/__pycache__", recursive=True):
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    
print(f"Removed {len(removed)} .pyc files")
print(f"Removed: {removed[:10]}")

# Verify the fix is in the .py file
with open(f"{agent}/autotrader.py") as f:
    c = f.read()
print(f"_THESIS_BLACKLIST present: {'_THESIS_BLACKLIST' in c}")
print(f"blue devils keyword: {'blue devils' in c}")
print(f"connecticut huskies keyword: {'connecticut huskies' in c}")
print(f"condition_id extracted: {'condition_id' in c}")
