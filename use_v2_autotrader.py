
import subprocess, shutil, os

AGENT = "/opt/polymarket-agent"
SVC   = "/etc/systemd/system/polymarket.service"

# Copy autotrader.py to autotrader_v2.py (no .pyc for this name yet)
shutil.copy2(f"{AGENT}/autotrader.py", f"{AGENT}/autotrader_v2.py")
print(f"Copied autotrader.py → autotrader_v2.py")

# Verify it has the fixes
with open(f"{AGENT}/autotrader_v2.py") as f:
    c = f.read()
t = "# check_thesis_invalidation(client)" in c
p = "_PROFIT_SPORTS_KEYWORDS" in c
n = "NEAR_RESOLUTION_THRESHOLD = 0.99" in c
print(f"thesis_disabled={t} sports_guard={p} threshold_0.99={n}")

if not (t and p and n):
    print("ERROR: fixes missing from autotrader.py on disk!")
    exit(1)

# Update systemd service to use autotrader_v2.py
with open(SVC) as f:
    svc = f.read()
new_svc = svc.replace(
    "/opt/polymarket-agent/autotrader.py",
    "/opt/polymarket-agent/autotrader_v2.py"
)
with open(SVC, "w") as f:
    f.write(new_svc)

subprocess.run(["systemctl", "daemon-reload"], check=True)
print("systemd updated to use autotrader_v2.py")

# Verify
with open(SVC) as f:
    s = f.read()
for l in s.split("\n"):
    if "ExecStart" in l:
        print(f"ExecStart: {l.strip()}")
print("READY")
