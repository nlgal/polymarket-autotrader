"""
Install py-clob-client-v2 on the server.
Run once before April 28 11am UTC cutover.
"""
import subprocess, sys

print("Installing py-clob-client-v2...")
result = subprocess.run(
    [sys.executable, '-m', 'pip', 'install', 'py-clob-client-v2==1.0.0', '--upgrade', '-q'],
    capture_output=True, text=True
)
print(result.stdout)
print(result.stderr[-300:] if result.stderr else '')
print(f"Exit code: {result.returncode}")

# Verify install
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions, BalanceAllowanceParams
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    print("py-clob-client-v2 v1.0.0 installed and importable ✓")
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)

# Also verify old v1 still installed (shouldn't break anything if both are present)
try:
    from py_clob_client.client import ClobClient as OldClient
    print("py-clob-client v1 still present (ok — server scripts now use v2)")
except ImportError:
    print("py-clob-client v1 not found (ok — fully migrated)")
