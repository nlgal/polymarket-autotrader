"""Bootstrap: update executor + run exit_contradictions.py"""
import subprocess, requests, os, time, sys

AGENT_DIR = "/opt/polymarket-agent"
VENV_PY   = f"{AGENT_DIR}/venv/bin/python3"

# 1. Update executor with new allowlist
r = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/executor.py", timeout=15)
with open(f"{AGENT_DIR}/executor.py", "w") as f: f.write(r.text)
print(f"Executor updated ({len(r.text)} chars), exit_contradictions in allowlist: {'exit_contradictions' in r.text}")
subprocess.run(["systemctl", "restart", "executor"], capture_output=True)
time.sleep(3)

# 2. Download and run exit_contradictions.py
r2 = requests.get("https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/exit_contradictions.py", timeout=15)
with open(f"{AGENT_DIR}/exit_contradictions.py", "w") as f: f.write(r2.text)
print(f"exit_contradictions.py downloaded ({len(r2.text)} chars)")

result = subprocess.run([VENV_PY, f"{AGENT_DIR}/exit_contradictions.py"], 
                        capture_output=True, text=True, timeout=120)
print("EXIT CODE:", result.returncode)
print("STDOUT:", result.stdout[-2000:])
if result.stderr:
    print("STDERR:", result.stderr[-500:])
