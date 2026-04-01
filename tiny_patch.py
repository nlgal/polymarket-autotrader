
import urllib.request, os, sys

url = "https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/merge_apr30.py"
dest = "/opt/polymarket-agent/merge_apr30.py"

try:
    with urllib.request.urlopen(url, timeout=15) as r:
        content = r.read().decode()
    with open(dest, "w") as f:
        f.write(content)
    size = os.path.getsize(dest)
    print(f"OK: wrote {size} bytes to {dest}")
    # Verify it has the new nonce code
    if "Nonce @" in content:
        print("VERIFIED: verbose nonce code present")
    else:
        print("WARNING: verbose nonce code NOT found")
except Exception as e:
    print(f"ERROR: {e}")
