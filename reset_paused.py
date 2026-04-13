"""
Reset PAUSED mode by updating state.json:
- Set mode = NORMAL
- Set equity_sod to current equity (positions + CLOB cash)
- Clear daily_pnl
This unpauses the autotrader when the HARD STOP was triggered incorrectly
due to the value API not counting CLOB cash.
"""
import json, os, sys, time

STATE_FILE = '/opt/polymarket-agent/state.json'

if not os.path.exists(STATE_FILE):
    print('No state.json found')
    sys.exit(1)

with open(STATE_FILE) as f:
    state = json.load(f)

old_mode = state.get('mode', 'UNKNOWN')
old_sod = state.get('equity_sod', 0)
print(f'Current mode: {old_mode}')
print(f'Current equity_sod: ${old_sod:.2f}')
print(f'Current daily_pnl: ${state.get("daily_pnl", 0):.2f}')

if old_mode != 'PAUSED':
    print(f'Not in PAUSED mode ({old_mode}) — nothing to do')
    sys.exit(0)

# Reset mode and equity_sod
state['mode'] = 'NORMAL'
state['daily_pnl'] = 0.0
state['daily_hard_stop_hit'] = False
# Reset equity_sod to 0 so it gets recalculated on next cycle
state['equity_sod'] = 0.0
state['last_mode_change'] = time.time()

with open(STATE_FILE, 'w') as f:
    json.dump(state, f, indent=2)

print(f'RESET: mode PAUSED → NORMAL | equity_sod reset to 0 (will recalculate on next cycle)')
