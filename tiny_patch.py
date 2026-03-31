import subprocess,shutil,os
shutil.copy2("/opt/polymarket-agent/autotrader.py","/opt/polymarket-agent/autotrader_v2.py")
open("/etc/systemd/system/polymarket.service","w").write(open("/etc/systemd/system/polymarket.service").read().replace("autotrader.py","autotrader_v2.py"))
subprocess.run(["systemctl","daemon-reload"])
print("done:",open("/etc/systemd/system/polymarket.service").read().count("autotrader_v2.py"),"refs to v2")
