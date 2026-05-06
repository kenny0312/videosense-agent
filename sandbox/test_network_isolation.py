"""Verify that network access is blocked inside the container."""
import urllib.request
import json

code = """
import urllib.request
try:
    urllib.request.urlopen("http://google.com", timeout=3)
    print("NETWORK_OPEN")
except Exception as e:
    print("NETWORK_BLOCKED:", type(e).__name__)
"""

body = json.dumps({"code": code, "timeout": 10}).encode()
req = urllib.request.Request(
    "http://localhost:8080/execute",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
r = json.loads(urllib.request.urlopen(req).read())
print("stdout   :", r["stdout"].strip())
print("exit_code:", r["exit_code"])

blocked = "NETWORK_BLOCKED" in r["stdout"]
print()
print("[PASS] Network isolation confirmed" if blocked else "[FAIL] Network NOT isolated!")
