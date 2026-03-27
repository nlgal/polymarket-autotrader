
path = '/opt/polymarket-agent/opportunity_scanner.py'
with open(path) as f:
    c = f.read()

fixed = 0

# Fix 1: AGENT_DIR NameError
old1 = 'claude_md_path = os.path.join(AGENT_DIR, "CLAUDE.md")'
new1 = 'claude_md_path = "/opt/polymarket-agent/CLAUDE.md"'
if old1 in c:
    c = c.replace(old1, new1, 1)
    fixed += 1
    print("Fix 1 applied: AGENT_DIR")

# Fix 2: uw_sig UnboundLocalError  
old2 = '        if _is_sports_pre and not uw_sig:\n            # Sports markets without whale flow are coin-flips for us\n            _pre_pass = False\n            _pre_reason = "sports market, no UW signal"'
new2 = '        _has_uw_pre = any(t in uw_signals for t in mkt.get("clob_token_ids", []))\n        if _is_sports_pre and not _has_uw_pre:\n            # Sports markets without whale flow are coin-flips for us\n            _pre_pass = False\n            _pre_reason = "sports market, no UW signal"'
if old2 in c:
    c = c.replace(old2, new2, 1)
    fixed += 1
    print("Fix 2 applied: uw_sig -> _has_uw_pre")

with open(path, 'w') as f:
    f.write(c)

# Verify
with open(path) as f:
    cv = f.read()
lines = cv.split('\n')
for i in range(509, 525):
    print(f"L{i+1}: {lines[i]}")

print(f"\nTotal fixes applied: {fixed}")

# Quick syntax check
import py_compile, tempfile, shutil
try:
    py_compile.compile(path, doraise=True)
    print("SYNTAX OK")
except Exception as e:
    print(f"SYNTAX ERROR: {e}")
