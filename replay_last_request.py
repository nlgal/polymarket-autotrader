#!/usr/bin/env python3
"""
replay_last_request.py
Usage: python3 replay_last_request.py [--dry-run]

Reads executor_last_request.json and re-executes the last logged request
against the live executor. Single command for crash recovery.
"""
import sys, json, os, time, hmac, hashlib, requests

DRY_RUN  = "--dry-run" in sys.argv
ENV_FILE = "/opt/polymarket-agent/.env"
LOG_FILE = "/opt/polymarket-agent/executor_last_request.json"
SERVER   = "http://127.0.0.1:8888"
SECRET   = None

for line in open(ENV_FILE):
    if line.startswith("EXECUTOR_SECRET="):
        SECRET = line.split("=", 1)[1].strip().strip('"\'')

if not SECRET:
    print("ERROR: EXECUTOR_SECRET not found in .env"); sys.exit(1)

if not os.path.exists(LOG_FILE):
    print(f"ERROR: no crash log at {LOG_FILE}"); sys.exit(1)

with open(LOG_FILE) as f:
    last_req = json.load(f)

command = last_req.pop("command", None)
last_req.pop("_ts", None)
payload = last_req

if not command:
    print("ERROR: no command in crash log"); sys.exit(1)

print(f"Last logged request:")
print(f"  Command : {command}")
print(f"  Payload : {json.dumps(payload) if payload else '{}'}")
print()

if DRY_RUN:
    print("DRY RUN — not executing. Remove --dry-run to replay.")
    sys.exit(0)

print("Replaying against live executor...")
body = json.dumps({"command": command, **payload}).encode()
sig  = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
r    = requests.post(f"{SERVER}/exec", data=body,
       headers={"Content-Type": "application/json", "X-Signature": sig}, timeout=120)
result = r.json()
print(f"Result: {json.dumps(result, indent=2)}")
print("\n✅ Replay succeeded" if result.get("exit_code") == 0 else f"\n⚠️  exit_code={result.get('exit_code')}")
