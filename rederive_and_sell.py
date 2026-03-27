
import requests
r = requests.get('https://raw.githubusercontent.com/nlgal/polymarket-autotrader/main/opportunity_scanner.py', timeout=15)
with open('/opt/polymarket-agent/opportunity_scanner.py', 'w') as f:
    f.write(r.text)
print(f"Scanner updated ({len(r.text)} chars)")
