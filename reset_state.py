import subprocess, time
r = subprocess.run(["systemctl", "restart", "btc-momentum"], capture_output=True, text=True)
print(f"restart: {r.returncode} {r.stderr[:50]}")
time.sleep(3)
status = subprocess.run(["systemctl", "status", "btc-momentum", "--no-pager"],
                        capture_output=True, text=True)
for line in status.stdout.split("\n")[:8]:
    if line.strip(): print(line)
