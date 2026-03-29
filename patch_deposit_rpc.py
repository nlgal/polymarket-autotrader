"""One-time patch: fix RPC URL in deposit_safe.py on server."""
path = "/opt/polymarket-agent/deposit_safe.py"
with open(path, "r") as f:
    content = f.read()

old = 'RPC = "https://polygon-rpc.com"'
new = 'RPC = "https://rpc.ankr.com/polygon"'

if old in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("✓ RPC patched to ankr.com")
elif 'ankr' in content:
    print("✓ Already using ankr.com — no patch needed")
else:
    print("✗ Pattern not found")
    import re
    m = re.search(r'RPC = .*', content)
    if m:
        print(f"Current RPC line: {m.group()}")
