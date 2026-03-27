
path = '/opt/polymarket-agent/opportunity_scanner.py'
with open(path) as f:
    lines = f.readlines()

# Find and fix the problematic line
for i, line in enumerate(lines):
    if 'if _is_sports_pre and not uw_sig:' in line:
        # Insert the _has_uw_pre check before this line
        indent = '        '
        check_line = indent + '_has_uw_pre = any(t in uw_signals for t in mkt.get("clob_token_ids", []))\n'
        lines[i] = check_line + line.replace('not uw_sig', 'not _has_uw_pre')
        print(f"Fixed line {i+1}")
        break

with open(path, 'w') as f:
    f.writelines(lines)

# Verify
with open(path) as f:
    c = f.read()
for j, l in enumerate(c.split('\n')[510:525], start=511):
    print(f"L{j}: {l}")

import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("\nSYNTAX OK")
except Exception as e:
    print(f"\nERROR: {e}")
