import json, os, sys, requests

STATE = '/opt/polymarket-agent/sports_state.json'
if not os.path.exists(STATE):
    print("No sports_state.json — nothing to purge")
    sys.exit(0)

with open(STATE) as f:
    state = json.load(f)

positions = state.get('positions', {})
print(f"Tracked positions before: {len(positions)}")

stale = []
for key, pos in list(positions.items()):
    yes_token = pos.get('yes_token','')
    print(f"\n  {key}:")
    print(f"    shares={pos.get('total_shares',0):.1f} avg={pos.get('avg_cost',0):.3f}")
    print(f"    token={yes_token[:40]}...")

    # Check current market mid
    if yes_token:
        try:
            mid_r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={yes_token}", timeout=6)
            mid = float(mid_r.json().get('mid', 0)) if mid_r.ok else 0
            print(f"    current mid={mid:.3f}")
            # If market is resolved (mid ~1.0 or ~0.0) or eliminated (<2c) -> stale
            if mid < 0.02 or mid > 0.98:
                stale.append(key)
                print(f"    → STALE (mid={mid:.3f}) — marking for removal")
        except Exception as e:
            print(f"    → mid check failed: {e}")
    else:
        stale.append(key)
        print(f"    → STALE (no token) — marking for removal")

print(f"\nRemoving {len(stale)} stale positions: {stale}")
for key in stale:
    del state['positions'][key]

with open(STATE, 'w') as f:
    json.dump(state, f, indent=2)

print(f"Done. Positions remaining: {len(state.get('positions', {}))}")
