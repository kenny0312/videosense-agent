"""
Verify the sandbox blocks network access at the policy layer.

Target is BASE (defaults to localhost). To run against Cloud Run:
    $env:SANDBOX_URL = "https://your-sandbox.run.app"
    $env:SANDBOX_TOKEN = (gcloud auth print-identity-token)
    python sandbox/test_network_isolation.py
"""
import os
import json
import urllib.request

BASE = os.environ.get("SANDBOX_URL", "http://localhost:8080").rstrip("/")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")


def post_execute(code: str, timeout: int = 10) -> dict:
    body = json.dumps({"code": code, "timeout": timeout}).encode()
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"{BASE}/execute", data=body, headers=headers, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


CASES = [
    ("urllib import",  "import urllib.request\nurllib.request.urlopen('http://example.com')"),
    ("socket import",  "import socket\nsocket.socket().connect(('1.1.1.1', 80))"),
    ("requests import","import requests\nrequests.get('http://example.com')"),
    ("from urllib",    "from urllib.request import urlopen\nurlopen('http://example.com')"),
    ("__import__",     "__import__('socket').socket().connect(('1.1.1.1', 80))"),
    ("subprocess",     "import subprocess\nsubprocess.run(['curl','http://example.com'])"),
]

ok = True
for label, code in CASES:
    r = post_execute(code)
    blocked = r["exit_code"] != 0 and "PolicyError" in r["stderr"]
    mark = "[PASS]" if blocked else "[FAIL]"
    if not blocked:
        ok = False
    print(f"{mark}  {label:18s}  exit={r['exit_code']}  stderr={r['stderr'][:80]!r}")

print()
print("=" * 50)
print("NETWORK ISOLATION CONFIRMED" if ok else "ISOLATION BROKEN")
print("=" * 50)
