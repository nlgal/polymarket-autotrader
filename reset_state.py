import subprocess, time
r = subprocess.run(["systemctl", "restart", "btc-momentum"], capture_output=True, text=True)
print(f"restart: {r.returncode}")
time.sleep(3)
s = subprocess.run(["systemctl", "status", "btc-momentum", "--no-pager"], capture_output=True, text=True)
for l in s.stdout.split("\n")[:6]:
    if l.strip(): print(l)
