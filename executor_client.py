"""
executor_client.py — Client for the Polymarket agent webhook executor
Used by the Perplexity agent to trigger commands on the DigitalOcean server
without requiring SSH.

Usage:
    from executor_client import exec_server
    result = exec_server("deploy_autotrader")
    result = exec_server("run_script", script="place_weather_march24_25.py")
    result = exec_server("tail_log", lines=100)
"""

import hmac, hashlib, json, requests

SERVER_URL = "http://167.71.68.143:8888"
# Secret is stored in autotrader .env on the server.
# This client needs the SAME secret — store it in the context summary.
EXECUTOR_SECRET = ""  # Will be set after server setup

def exec_server(command: str, secret: str = None, **kwargs) -> dict:
    """
    Execute a whitelisted command on the server via the webhook executor.
    
    Args:
        command: One of deploy_autotrader, deploy_weather_scout, deploy_all,
                 service_status, service_restart, service_stop,
                 tail_log (lines=N), run_script (script="name.py")
        secret: EXECUTOR_SECRET from .env on the server
        **kwargs: Extra payload fields (script=, lines=)
    
    Returns:
        dict with exit_code, stdout, stderr
    """
    _secret = secret or EXECUTOR_SECRET
    if not _secret:
        raise ValueError("EXECUTOR_SECRET required — get it from server .env")
    
    payload = {"command": command, **kwargs}
    body = json.dumps(payload).encode()
    sig = hmac.new(_secret.encode(), body, hashlib.sha256).hexdigest()
    
    try:
        r = requests.post(
            f"{SERVER_URL}/exec",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": sig,
            },
            timeout=120,
        )
        result = r.json()
        if result.get("exit_code") == 0:
            print(f"✓ {command}: {result.get('stdout','').strip()[:200]}")
        else:
            print(f"✗ {command} (exit {result.get('exit_code')}): {result.get('stderr','').strip()[:200]}")
        return result
    except requests.exceptions.ConnectionError:
        return {"exit_code": -1, "stdout": "", "stderr": f"Cannot connect to {SERVER_URL} — is executor running?"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


def check_server_health(secret: str = None) -> bool:
    """Returns True if the executor is reachable and healthy."""
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    # Quick test
    import sys
    secret = sys.argv[1] if len(sys.argv) > 1 else EXECUTOR_SECRET
    if not secret:
        print("Usage: python3 executor_client.py <SECRET>")
        sys.exit(1)
    
    print("Testing executor health...")
    healthy = check_server_health()
    print(f"Health: {'OK' if healthy else 'UNREACHABLE'}")
    
    if healthy:
        print("\nTesting service_status...")
        result = exec_server("service_status", secret=secret)
        print(result.get("stdout","")[:500])
