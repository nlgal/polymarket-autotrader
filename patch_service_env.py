
import subprocess, re

svc = "/etc/systemd/system/polymarket.service"
with open(svc) as f:
    content = f.read()

print("Current service file:")
print(content)

# Add PYTHONDONTWRITEBYTECODE=1 env var to prevent pyc loading
if "PYTHONDONTWRITEBYTECODE" not in content:
    # Add after [Service] section
    new_content = content.replace(
        "[Service]",
        "[Service]\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=PYTHONPYCACHEPREFIX=/tmp/py_discard"
    )
    with open(svc, "w") as f:
        f.write(new_content)
    subprocess.run(["systemctl", "daemon-reload"])
    print("\nPatched: PYTHONDONTWRITEBYTECODE=1 added")
else:
    print("Already has PYTHONDONTWRITEBYTECODE")

# Verify
with open(svc) as f:
    c = f.read()
for l in c.split("\n"):
    if "PYTHON" in l or "ExecStart" in l or "Service" in l:
        print(l)
