"""
One-shot script: adds DISCORD_TOKEN to .env on server.
The token is passed as PATCH_DISCORD_TOKEN environment variable by the caller.
Run via: exec_server("run_script", script="patch_discord_env.py", env={"PATCH_DISCORD_TOKEN": "..."})
"""
import os

ENV_PATH = "/opt/polymarket-agent/.env"
TOKEN    = os.environ.get("PATCH_DISCORD_TOKEN", "")

if not TOKEN:
    print("ERROR: PATCH_DISCORD_TOKEN env var not set")
    exit(1)

with open(ENV_PATH) as f:
    content = f.read()

if "DISCORD_TOKEN=" in content:
    lines = content.splitlines()
    new_lines = [
        f"DISCORD_TOKEN={TOKEN}" if l.startswith("DISCORD_TOKEN=") else l
        for l in lines
    ]
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    print("DISCORD_TOKEN updated in .env")
else:
    with open(ENV_PATH, "a") as f:
        f.write(f"\nDISCORD_TOKEN={TOKEN}\n")
    print("DISCORD_TOKEN added to .env")

# Verify (masked)
with open(ENV_PATH) as f:
    for line in f:
        if "DISCORD_TOKEN" in line:
            v = line.strip().split("=",1)[1] if "=" in line else ""
            print(f"Confirmed in .env: DISCORD_TOKEN={v[:8]}...{v[-4:]}")
            break
