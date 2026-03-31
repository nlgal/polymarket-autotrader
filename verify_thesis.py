
with open("/opt/polymarket-agent/autotrader.py") as f:
    c = f.read()
active = "check_thesis_invalidation(client)  # re-check" in c
disabled_comment = "# DISABLED: check_thesis_invalidation" in c
print(f"Thesis ACTIVE call: {active}")
print(f"Thesis DISABLED comment: {disabled_comment}")
print(f"SAFE: {not active and disabled_comment}")
