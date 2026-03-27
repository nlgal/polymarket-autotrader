
path = '/opt/polymarket-agent/opportunity_scanner.py'
with open(path) as f:
    lines = f.readlines()

# Fix ALL occurrences of "not uw_sig" in the pre-filter section (before line 540)
count = 0
for i in range(min(540, len(lines))):
    if 'if _is_sports_pre and not uw_sig:' in lines[i]:
        # Replace this line with the fixed version
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        check_line = indent + '_has_uw_pre = any(t in uw_signals for t in mkt.get("clob_token_ids", []))\n'
        lines[i] = check_line + lines[i].replace('not uw_sig', 'not _has_uw_pre')
        count += 1
        print(f"Fixed occurrence at line {i+1}")

with open(path, 'w') as f:
    f.writelines(lines)
print(f"Total fixed: {count}")

# Verify lines 514-522
with open(path) as f:
    vlines = f.readlines()
for i in range(513, 523):
    print(f"L{i+1}: {vlines[i].rstrip()}")

import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("\nSYNTAX OK")
except Exception as e:
    print(f"\nSYNTAX ERROR: {e}")
