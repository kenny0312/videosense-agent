"""
Quick smoke test for the Sandbox API.

Targets localhost by default. To run against Cloud Run:
    $env:SANDBOX_URL = "https://your-sandbox.run.app"
    $env:SANDBOX_TOKEN = (gcloud auth print-identity-token)
    python sandbox/test_local.py
"""
import os
import urllib.request
import json
import sys

BASE = os.environ.get("SANDBOX_URL", "http://localhost:8080").rstrip("/")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")
PASS = True


def post_execute(code: str, timeout: int = 30) -> dict:
    body = json.dumps({"code": code, "timeout": timeout}).encode()
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(
        f"{BASE}/execute",
        data=body,
        headers=headers,
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())


def check(label: str, condition: bool, detail: str = ""):
    global PASS
    mark = "[PASS]" if condition else "[FAIL]"
    if not condition:
        PASS = False
    print(f"{mark}  {label}" + (f"  ({detail})" if detail else ""))


# ── 1. Health ──────────────────────────────────────────────────────────────────
health_req = urllib.request.Request(f"{BASE}/health")
if TOKEN:
    health_req.add_header("Authorization", f"Bearer {TOKEN}")
data = json.loads(urllib.request.urlopen(health_req).read())
check("Health endpoint", data.get("status") == "ok")

# ── 2. Simple stdout ───────────────────────────────────────────────────────────
r = post_execute("print(2 + 2)")
check("stdout captured", r["stdout"].strip() == "4", r["stdout"].strip())
check("exit_code 0 on success", r["exit_code"] == 0)

# ── 3. Stderr captured ─────────────────────────────────────────────────────────
r = post_execute("import sys; sys.stderr.write('oops'); sys.exit(1)")
check("stderr captured", "oops" in r["stderr"], r["stderr"])
check("exit_code non-zero on failure", r["exit_code"] != 0)

# ── 4. Timeout enforced ────────────────────────────────────────────────────────
r = post_execute("import time; time.sleep(99)", timeout=2)
check("timeout kills process", r["timed_out"] is True)
check("timeout exit_code 124", r["exit_code"] == 124)

# ── 5. No credential leakage ───────────────────────────────────────────────────
r = post_execute("import os; print(os.environ.get('ALLOYDB_PASSWORD', 'NOT_FOUND'))")
check("ALLOYDB_PASSWORD not leaked", r["stdout"].strip() == "NOT_FOUND", r["stdout"].strip())

# ── 6. Pandas available ────────────────────────────────────────────────────────
r = post_execute("import pandas as pd; print(pd.Series([1,2,3]).mean())")
check("pandas works", r["stdout"].strip() == "2.0", r["stdout"].strip())

# ── 7. SyntaxError returns stderr ─────────────────────────────────────────────
r = post_execute("def broken(: pass")
check("SyntaxError captured in stderr", r["exit_code"] != 0)

# ── 8. Elapsed time recorded ──────────────────────────────────────────────────
r = post_execute("print('hi')")
check("elapsed_seconds > 0", r["elapsed_seconds"] > 0, str(r["elapsed_seconds"]))

# ── 9. Import policy: network module blocked ──────────────────────────────────
r = post_execute("import socket")
check("socket import blocked", r["exit_code"] == 3 and "PolicyError" in r["stderr"], r["stderr"])

# ── 10. Import policy: urllib blocked ─────────────────────────────────────────
r = post_execute("import urllib.request")
check("urllib import blocked", r["exit_code"] == 3 and "PolicyError" in r["stderr"], r["stderr"])

# ── 11. Import policy: subprocess blocked ─────────────────────────────────────
r = post_execute("import subprocess")
check("subprocess blocked", r["exit_code"] == 3 and "PolicyError" in r["stderr"], r["stderr"])

# ── 12. Import policy: __import__ blocked ─────────────────────────────────────
r = post_execute("__import__('socket')")
check("__import__ blocked", r["exit_code"] == 3 and "PolicyError" in r["stderr"], r["stderr"])

# ── 13. Import policy: allowed modules still work ─────────────────────────────
r = post_execute("import numpy as np; print(np.array([1,2,3]).sum())")
check("numpy still allowed", r["exit_code"] == 0 and r["stdout"].strip() == "6", r["stdout"].strip())

print()
print("=" * 40)
print("ALL TESTS PASSED" if PASS else "SOME TESTS FAILED")
print("=" * 40)
sys.exit(0 if PASS else 1)
