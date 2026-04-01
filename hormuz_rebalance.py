import base64, os, subprocess
chunk = base64.b64decode("dXNkY19yZW1haW5pbmcgID0gbWF4KDAsIHVzZGNfcmVtYWluaW5nKQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZToKICAgICAgICAgICAgbG9nKGYiW3tta3RbJ2xhYmVsJ119XSBFUlJPUjoge2V9IikKICAgICAgICAgICAgdGcoZiLimqDvuI8gPGI+TFAgUXVvdGVyPC9iPiB7bWt0WydsYWJlbCddfToge2V9IikKCiAgICAjIERhaWx5IHN1bW1hcnkKICAgIG1heWJlX3NlbmRfZGFpbHlfc3VtbWFyeShzdGF0ZSkKCiAgICBzYXZlX3N0YXRlKHN0YXRlKQogICAgbG9nKCJMUCBRVU9URVIg4oCUIHJ1biBjb21wbGV0ZVxuIikKCgppZiBfX25hbWVfXyA9PSAiX19tYWluX18iOgogICAgbWFpbigpCg==").decode()
with open("/opt/polymarket-agent/lp_quoter.py", "a") as f:
    f.write(chunk)
# Clear pycache and verify
subprocess.run(["find","/opt/polymarket-agent/__pycache__","-name","lp_quoter*.pyc","-delete"],capture_output=True)
with open("/opt/polymarket-agent/lp_quoter.py") as f:
    data = f.read()
print(f"Total file: {len(data)} chars")
print(f"except TypeError: {'except TypeError' in data}")
print(f"Old bug gone: {'post_order(signed, OrderType.GTD, expiration' not in data}")
print(f"place_buy present: {'def place_buy' in data}")
