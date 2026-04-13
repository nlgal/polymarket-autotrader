import json, os
SELL_FILE = '/opt/polymarket-agent/sell_signals.json'
if os.path.exists(SELL_FILE):
    with open(SELL_FILE) as f:
        try:
            signals = json.load(f)
        except:
            signals = []
    print(f'Current sell_signals.json: {len(signals)} entries')
    for s in signals:
        print(f'  token: {str(s.get("token_id",""))[:80]} | shares: {s.get("shares")}')
    with open(SELL_FILE, 'w') as f:
        json.dump([], f)
    print('CLEARED: sell_signals.json reset to empty list')
else:
    print('No sell_signals.json found — creating empty')
    with open(SELL_FILE, 'w') as f:
        json.dump([], f)
