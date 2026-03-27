"""
Verification script — checks if check_commodity_reality fix is live.
Run once, then can be deleted.
"""
import sys
sys.path.insert(0, '/opt/polymarket-agent')

with open('/opt/polymarket-agent/opportunity_scanner.py', 'r') as f:
    content = f.read()

total_size = len(content)
has_gap_fix = "abs(gap)" in content
has_5dollar = "within $5" in content or "<= 5.0" in content
old_bug_present = "yes_p < 0.5 and wti >= target * 0.99" in content

print(f"File size: {total_size} chars")
print(f"Has abs(gap) fix: {has_gap_fix}")
print(f"Has $5 block: {has_5dollar}")  
print(f"Old buggy code present: {old_bug_present}")

if has_gap_fix and has_5dollar and not old_bug_present:
    print("\n✅ FIX IS LIVE — commodity check correctly blocks within $5 of trigger")
else:
    print("\n❌ FIX NOT LIVE — old buggy code still running!")
    # Print the actual function
    idx = content.find("def check_commodity_reality")
    if idx >= 0:
        next_def = content.find("\ndef ", idx+10)
        print(content[idx:next_def])

# Also test it live
try:
    from opportunity_scanner import check_commodity_reality, get_commodity_price
    wti = get_commodity_price("CL=F")
    print(f"\nLive WTI price: ${wti:.2f}")
    
    # Simulate the bad trade: $100 target, yes_p=0.57
    skip, reason = check_commodity_reality("Will Crude Oil (CL) hit (HIGH) $100 by end of March", 0.57)
    print(f"Would block $100 NO (yes_p=0.57, WTI=${wti:.2f}): skip={skip}, reason='{reason}'")
    
    if skip:
        print("✅ Bad trade WOULD BE BLOCKED")
    else:
        print("❌ Bad trade WOULD STILL GO THROUGH")
except Exception as e:
    print(f"Live test error: {e}")
