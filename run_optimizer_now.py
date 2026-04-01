import os, subprocess
with open("/opt/polymarket-agent/lp_quoter.py") as f:
    data = f.read()
subprocess.run(["find","/opt/polymarket-agent/__pycache__","-name","lp_quoter*.pyc","-delete"],capture_output=True)
print(f"Total: {len(data)} chars")
print(f"expiration=expiry: {'expiration=expiry' in data}")
print(f"OrderType.GTD: {'OrderType.GTD' in data}")
