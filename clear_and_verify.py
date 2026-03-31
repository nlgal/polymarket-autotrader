
import os, glob, shutil
AGENT = "/opt/polymarket-agent"
# Clear pycache
removed = 0
for pyc in glob.glob(f"{AGENT}/**/*.pyc", recursive=True):
    try: os.remove(pyc); removed += 1
    except: pass
for d in glob.glob(f"{AGENT}/**/__pycache__", recursive=True):
    try: shutil.rmtree(d); removed += 1
    except: pass
print(f"Cleared {removed} cached files")
# Quick verify
with open(f"{AGENT}/autotrader.py") as f:
    c = f.read()
t = "# check_thesis_invalidation(client)" in c
p = "_PROFIT_SPORTS_KEYWORDS" in c
n = "NEAR_RESOLUTION_THRESHOLD = 0.99" in c
print(f"thesis_disabled={t}")
print(f"sports_guard={p}")
print(f"threshold_0.99={n}")
print(f"ALL_FIXES_OK={t and p and n}")
