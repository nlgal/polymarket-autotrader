
with open('/opt/polymarket-agent/opportunity_scanner.py') as f:
    c = f.read()
if "_has_uw_pre = any(t in uw_signals" in c:
    print("FIX CONFIRMED on server")
else:
    for i, l in enumerate(c.split("\n")[510:520], start=511):
        print(f"L{i}: {l}")
