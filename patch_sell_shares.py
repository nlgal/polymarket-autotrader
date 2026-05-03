
SHARES_CORRECT = 103.73
path = '/opt/polymarket-agent/sell_aitc_no.py'
with open(path, 'r') as f:
    txt = f.read()
if 'SHARES = 104.0' in txt or 'SHARES = 103' in txt:
    import re
    txt = re.sub(r'SHARES = [0-9.]+', f'SHARES = {SHARES_CORRECT}', txt)
    with open(path, 'w') as f:
        f.write(txt)
    print(f'Patched SHARES to {SHARES_CORRECT}')
else:
    print('Already correct or pattern not found')
with open(path) as f:
    print('Current SHARES lines:', [l for l in f.read().split(chr(10)) if 'SHARES' in l and '=' in l])
