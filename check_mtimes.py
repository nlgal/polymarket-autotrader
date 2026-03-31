import os, glob, time
AGENT = "/opt/polymarket-agent"
py = f"{AGENT}/autotrader.py"
pycs = glob.glob(f"{AGENT}/**/__pycache__/autotrader*.pyc", recursive=True)
print(f"autotrader.py mtime: {os.path.getmtime(py):.0f} = {time.ctime(os.path.getmtime(py))}")
print(f"autotrader.py size: {os.path.getsize(py)}")
for pyc in pycs:
    print(f"pyc: {pyc}")
    print(f"  mtime: {os.path.getmtime(pyc):.0f} = {time.ctime(os.path.getmtime(pyc))}")
    print(f"  size: {os.path.getsize(pyc)}")
