import os
env = open("/opt/polymarket-agent/.env").read()
if "PYTHONDONTWRITEBYTECODE" not in env:
    open("/opt/polymarket-agent/.env","a").write("\nPYTHONDONTWRITEBYTECODE=1\n")
    print("added PYTHONDONTWRITEBYTECODE=1 to .env")
else:
    print("already set")
