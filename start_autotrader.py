"""
start_autotrader.py
===================
Startup wrapper — clears ALL pycache before running autotrader.py.

This ensures the running process always uses the .py source files,
never stale .pyc bytecode from a previous version.

Systemd runs this instead of autotrader.py directly:
  ExecStart=/opt/polymarket-agent/venv/bin/python3 /opt/polymarket-agent/start_autotrader.py
"""
import os, glob, shutil, sys, runpy

AGENT_DIR = "/opt/polymarket-agent"

# Clear all pycache before importing anything from agent dir
cleared = 0
for pyc in glob.glob(f"{AGENT_DIR}/**/*.pyc", recursive=True):
    try:
        os.remove(pyc)
        cleared += 1
    except Exception:
        pass
for cache_dir in glob.glob(f"{AGENT_DIR}/**/__pycache__", recursive=True):
    try:
        shutil.rmtree(cache_dir)
        cleared += 1
    except Exception:
        pass

print(f"[startup] Cleared {cleared} cached bytecode files")
print(f"[startup] Starting autotrader.py...")
sys.stdout.flush()

# Run autotrader.py as __main__
runpy.run_path(f"{AGENT_DIR}/autotrader.py", run_name="__main__")
