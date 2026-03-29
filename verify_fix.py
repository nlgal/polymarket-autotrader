
with open("/opt/polymarket-agent/autotrader.py") as f:
    c = f.read()
print("Duke blacklisted:", "87254ca39f82f1fd" in c)
print("Sports fix:", "_SPORTS_GAME_KEYWORDS" in c)
