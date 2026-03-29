import os
t = "Nzk5MzI2MjMyMzE1NTU5OTU2" + ".GEEv9z." + "EGNWaAeZLcTl18hDnonX21sxIkZBizffaalzmg"
env_path = "/opt/polymarket-agent/.env"
with open(env_path) as f:
    content = f.read()
if "DISCORD_TOKEN=" not in content:
    with open(env_path, "a") as f:
        f.write(f"\nDISCORD_TOKEN={t}\n")
    print(f"DISCORD_TOKEN added ({len(t)} chars)")
else:
    lines = content.splitlines()
    out = ["DISCORD_TOKEN=" + t if l.startswith("DISCORD_TOKEN=") else l for l in lines]
    with open(env_path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"DISCORD_TOKEN updated ({len(t)} chars)")
v = t[:8] + "..." + t[-4:]
print(f"Value (masked): {v}")
