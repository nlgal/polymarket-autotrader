"""
patch_executor.py — adds sell_position + whitelist_script to executor.py
Self-disabling via flag file. Safe to re-run.
"""
import os, sys, re, subprocess, time

FLAG = "/opt/polymarket-agent/.executor_patched_v2"
if os.path.exists(FLAG):
    print("Already patched — exiting")
    sys.exit(0)

# ── Find executor.py ──────────────────────────────────────────────────────────
result = subprocess.run(
    ["find", "/", "-name", "executor.py", "-maxdepth", "8",
     "-not", "-path", "*/venv/*", "-not", "-path", "*/site-packages/*"],
    capture_output=True, text=True, timeout=15
)
candidates = [
    p.strip() for p in result.stdout.splitlines()
    if p.strip() and os.path.exists(p.strip()) and os.path.getsize(p.strip()) > 2000
]
print(f"Candidates: {candidates}")
if not candidates:
    print("ERROR: executor.py not found"); sys.exit(1)

executor_py = candidates[0]
print(f"Patching: {executor_py} ({os.path.getsize(executor_py)} bytes)")

with open(executor_py) as f:
    code = f.read()

# Print the dispatch section so we can see the pattern
dispatch_match = re.search(r'elif cmd ==.{0,2000}', code, re.DOTALL)
if dispatch_match:
    print("Dispatch preview:")
    print(dispatch_match.group()[:600])

# Print what "else" / unknown-command handler looks like
else_match = re.search(r'\belse\b.{0,300}unknown command', code, re.DOTALL)
if else_match:
    print("\nUnknown-command handler:")
    print(code[else_match.start()-50:else_match.end()+50])

if "sell_position" in code:
    print("sell_position already present — writing flag and exiting")
    open(FLAG, "w").write("done"); sys.exit(0)

print("\nReady to patch — printing full dispatch block for review:")
print(code[max(0, code.find("async def handle")):code.find("async def handle")+4000])
