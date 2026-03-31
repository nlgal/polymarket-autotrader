
import os, glob, shutil
AGENT = "/opt/polymarket-agent"

# Rename to final
os.replace(f"{AGENT}/autotrader_new.py", f"{AGENT}/autotrader.py")
print("Renamed autotrader_new.py → autotrader.py")

# Clear ALL pycache
removed = 0
for pyc in glob.glob(f"{AGENT}/**/*.pyc", recursive=True):
    try: os.remove(pyc); removed += 1
    except: pass
for d in glob.glob(f"{AGENT}/**/__pycache__", recursive=True):
    try: shutil.rmtree(d); removed += 1
    except: pass
print(f"Cleared {removed} cache items")

# Verify key fixes
with open(f"{AGENT}/autotrader.py") as f:
    c = f.read()
print(f"File size: {len(c)} chars")
print(f"Thesis disabled: {'# check_thesis_invalidation(client)' in c}")
print(f"Sports profit guard: {'_PROFIT_SPORTS_KEYWORDS' in c}")
print(f"Near-res threshold 0.99: {'NEAR_RESOLUTION_THRESHOLD = 0.99' in c}")
all_ok = ('# check_thesis_invalidation(client)' in c and 
          '_PROFIT_SPORTS_KEYWORDS' in c and 
          'NEAR_RESOLUTION_THRESHOLD = 0.99' in c)
print(f"ALL FIXES CONFIRMED: {all_ok}")
