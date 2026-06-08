"""
Core execution engine: runs untrusted Python code in a subprocess with strict limits.

Security model:
  - Static policy gate: AST scan rejects forbidden imports (network, subprocess,
    importlib, ctypes, pickle, marshal) BEFORE the code ever runs. Reason: Cloud
    Run's gVisor sandbox isolates syscalls but does not block outbound network,
    so we deny the modules that would reach it.
  - Process isolation: code runs in a separate subprocess
  - Hard timeout enforced via subprocess.run(timeout=...)
  - stdout/stderr captured, never printed to host
  - Environment scrubbed: no DB password, no GCP credentials, no tokens
  - gVisor (Cloud Run gen2) provides kernel-syscall isolation
"""

import ast
import subprocess
import sys
import tempfile
import time
import os
from pathlib import Path

from sandbox.models import ExecuteResponse


# Packages available inside the sandbox.
ALLOWED_IMPORTS_HINT = [
    "pandas", "numpy", "json", "math", "statistics",
    "collections", "itertools", "functools", "datetime",
]

# Modules denied by the static policy gate.
# Network: any module that can open a socket or speak HTTP.
# Process: anything that can spawn another process.
# Loopholes: anything that can defeat the static check itself.
DENY_MODULES = frozenset({
    "socket", "ssl",
    "urllib", "urllib2", "urllib3",
    "http", "httpx", "requests", "aiohttp",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
    "xmlrpc", "webbrowser",
    "subprocess", "multiprocessing",
    "importlib", "imp",
    "ctypes", "cffi",
    "pickle", "cPickle", "marshal", "shelve", "dill",
})

# Builtin functions denied by the policy gate (can defeat AST checks).
DENY_BUILTINS = frozenset({"__import__", "eval", "exec", "compile", "open"})


def _check_policy(code: str) -> str | None:
    """
    Static AST scan. Returns None if the code passes, or an error string if it
    violates the policy. We block at the module root: `import urllib.request`
    and `from urllib import request` both fail on `urllib`.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Let the subprocess produce the real SyntaxError traceback.
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in DENY_MODULES:
                    return f"ImportPolicyError: module '{alias.name}' is not allowed inside the sandbox"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in DENY_MODULES:
                return f"ImportPolicyError: module '{node.module}' is not allowed inside the sandbox"
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name in DENY_BUILTINS:
                return f"ImportPolicyError: builtin '{name}' is not allowed inside the sandbox"


def run_code(code: str, timeout: int = 30) -> ExecuteResponse:
    """
    Write `code` to a temp file and execute it in a subprocess.
    Returns stdout, stderr, exit_code, elapsed time, and timed_out flag.
    """
    policy_error = _check_policy(code)
    if policy_error is not None:
        return ExecuteResponse(
            stdout="",
            stderr=policy_error,
            exit_code=3,
            elapsed_seconds=0.0,
            timed_out=False,
        )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(code)
        tmp_path = f.name

    start = time.perf_counter()
    timed_out = False

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            # Inherit nothing sensitive from the host environment.
            # Only pass minimal vars needed for Python to work.
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                "PYTHONUTF8": "1",
                # No ALLOYDB_PASSWORD, no GCP credentials, no tokens.
            },
        )
        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.returncode

    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = f"TimeoutError: execution exceeded {timeout}s limit"
        exit_code = 124   # same convention as the Unix `timeout` command
        timed_out = True

    except Exception as e:
        stdout = ""
        stderr = f"ExecutorError: {e}"
        exit_code = 1

    finally:
        elapsed = time.perf_counter() - start
        # Always clean up the temp file
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    return ExecuteResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        elapsed_seconds=round(elapsed, 3),
        timed_out=timed_out,
    )
