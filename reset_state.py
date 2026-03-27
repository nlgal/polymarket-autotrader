import requests
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/strategy_optimizer.py', timeout=15)
with open('/opt/polymarket-agent/strategy_optimizer.py', 'w') as f:
    f.write(r.text)
print(f"strategy_optimizer.py deployed ({len(r.text)} chars)")
