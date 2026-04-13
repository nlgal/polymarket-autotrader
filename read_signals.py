import json, os
SELL_FILE = '/opt/polymarket-agent/sell_signals.json'
if os.path.exists(SELL_FILE):
    try:
        with open(SELL_FILE) as f:
            signals = json.load(f)
        print(f'Pending sell signals: {len(signals)}')
        for s in signals:
            print(f'  token: {str(s.get("token_id",""))[:80]} | shares: {s.get("shares")} | label: {s.get("label","")}')
    except Exception as e:
        print(f'Error reading: {e}')
else:
    print('No sell_signals.json')
