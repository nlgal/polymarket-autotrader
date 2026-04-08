"""
very_hot_forward_test.py
========================
Tracks every VERY_HOT (≥25% 1h move) signal fired by the opportunity
scanner. For each signal records:
  - Market, price at signal time, direction, timestamp
  - Final resolution outcome (YES/NO)
  - Whether signal direction was correct

Runs alongside the scanner. Appended to by opportunity_scanner.py when a
VERY_HOT signal fires. Evaluated once we have 5+ signals.

Usage:
  Record signal: record_very_hot_signal(question, token_id, price, direction, delta_1h)
  Evaluate:      evaluate_forward_test()   -- call manually or via cron
"""

import os, json, time, datetime, requests

TRACKER_FILE = "/opt/polymarket-agent/very_hot_forward_test.json"

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE) as f:
            return json.load(f)
    return {"signals": [], "version": 1}

def save_tracker(data):
    with open(TRACKER_FILE, "w") as f:
        json.dump(data, f, indent=2)

def record_very_hot_signal(question, token_id, yes_p, direction, delta_1h,
                            condition_id=""):
    """
    Called by opportunity_scanner when VERY_HOT tier fires.
    Records the signal for forward-test tracking.
    """
    data = load_tracker()

    # Check if already recorded this market+direction recently (24h dedup)
    cutoff = time.time() - 86400
    for s in data["signals"]:
        if s["token_id"] == token_id and s.get("ts", 0) > cutoff:
            return  # already recorded today

    signal = {
        "ts":           time.time(),
        "date":         datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "question":     question[:80],
        "token_id":     token_id,
        "condition_id": condition_id,
        "yes_p_at_signal": round(yes_p, 4),
        "direction":    direction,       # "UP" or "DOWN"
        "delta_1h":     round(delta_1h, 4),
        "outcome":      None,            # filled in when market resolves
        "correct":      None,
        "resolved_at":  None,
        "final_yes_p":  None,
    }
    data["signals"].append(signal)
    save_tracker(data)
    print(f"[FORWARD-TEST] Recorded VERY_HOT: {question[:50]} {direction} "
          f"Δ{delta_1h*100:+.1f}% @ {yes_p:.3f} (#{len(data['signals'])} total)")

def evaluate_forward_test():
    """
    Check all pending signals for resolution. Print evaluation report.
    Called periodically to update outcomes.
    """
    data = load_tracker()
    signals = data["signals"]

    if not signals:
        print("[FORWARD-TEST] No signals recorded yet.")
        return

    updated = False
    for s in signals:
        if s["outcome"] is not None:
            continue  # already resolved

        token_id = s.get("token_id","")
        if not token_id:
            continue

        try:
            r = requests.get(
                f"https://clob.polymarket.com/midpoint?token_id={token_id}",
                timeout=6
            )
            if not r.ok:
                continue
            mid = float(r.json().get("mid", 0))
            s["final_yes_p"] = round(mid, 4)

            # Resolved?
            if mid > 0.92:
                s["outcome"]     = "YES"
                s["resolved_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                s["correct"]     = (s["direction"] == "UP")
                updated = True
            elif mid < 0.08:
                s["outcome"]     = "NO"
                s["resolved_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                s["correct"]     = (s["direction"] == "DOWN")
                updated = True
        except:
            pass

    if updated:
        save_tracker(data)

    # Print report
    resolved   = [s for s in signals if s["outcome"] is not None]
    pending    = [s for s in signals if s["outcome"] is None]
    correct    = [s for s in resolved if s["correct"]]
    win_rate   = len(correct) / len(resolved) if resolved else 0

    print(f"\n{'='*65}")
    print(f"VERY_HOT FORWARD TEST — {len(signals)} signals total")
    print(f"  Resolved: {len(resolved)} | Pending: {len(pending)}")
    if resolved:
        print(f"  Win rate: {len(correct)}/{len(resolved)} ({win_rate*100:.0f}%)")
        print(f"  Target:   70-86% (backtest range)")
        if len(resolved) >= 5:
            if win_rate >= 0.70:
                print(f"  ✅ VALIDATED — raise S4 IC to 0.12, enable full sizing")
            elif win_rate >= 0.55:
                print(f"  ⚠️  MARGINAL — keep IC=0.09, continue collecting")
            else:
                print(f"  ❌ BELOW TARGET — reduce IC to 0.05, review threshold")
        else:
            print(f"  ⏳ Need {5-len(resolved)} more resolved signals for verdict")
    print(f"{'='*65}\n")

    print(f"{'#':>3} {'Date':>18} {'Direction':>10} {'Δ1h':>7} {'P@sig':>7} "
          f"{'Outcome':>8} {'Correct':>8}")
    print("-"*75)
    for i, s in enumerate(signals, 1):
        out = s.get("outcome") or "PENDING"
        cor = "✅" if s.get("correct") else ("❌" if s.get("correct")==False else "—")
        d   = s.get("direction","?")
        print(f"{i:>3} {s['date']:>18} {d:>10} {s['delta_1h']*100:>+6.1f}% "
              f"{s['yes_p_at_signal']:>7.3f} {out:>8}  {cor}")
        print(f"    {s['question'][:60]}")

if __name__ == "__main__":
    evaluate_forward_test()
