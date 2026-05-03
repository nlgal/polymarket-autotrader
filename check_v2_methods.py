import sys
sys.path.insert(0, '/opt/polymarket-agent')
# Try V2 first
try:
    from py_clob_client_v2.client import ClobClient as C2
    methods_v2 = [m for m in dir(C2) if 'order' in m.lower()]
    print('V2 methods:', methods_v2)
except Exception as e:
    print(f'V2 import error: {e}')

# Also check v1 (the one autotrader may be using)
try:
    from py_clob_client.client import ClobClient as C1
    methods_v1 = [m for m in dir(C1) if 'order' in m.lower()]
    print('V1 methods:', methods_v1)
except Exception as e:
    print(f'V1 import error: {e}')
