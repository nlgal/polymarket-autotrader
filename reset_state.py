import subprocess, sys
result = subprocess.run(
    ['/opt/polymarket-agent/venv/bin/python3', '/opt/polymarket-agent/strategy_optimizer.py'],
    capture_output=True, text=True, timeout=90,
    cwd='/opt/polymarket-agent'
)
print("STDOUT:", result.stdout[-2000:])
if result.stderr.strip():
    print("STDERR:", result.stderr[-300:])
print(f"Exit: {result.returncode}")
