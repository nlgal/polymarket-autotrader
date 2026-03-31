
with open("/opt/polymarket-agent/autotrader.py") as f:
    c = f.read()
print(f"1. Thesis disabled: {'# check_thesis_invalidation(client)' in c and 'check_thesis_invalidation(client)  # re-check' not in c}")
print(f"2. Sports profit guard: {'_PROFIT_SPORTS_KEYWORDS' in c}")
print(f"3. Near-res 0.99: {'NEAR_RESOLUTION_THRESHOLD = 0.99' in c}")
print(f"4. File size: {len(c)} chars")
