"""
Core execution engine: runs untrusted Python code in a subprocess with strict limits.

Security model (local / Phase A):
  - Code runs in a separate subprocess (process isolation)
  - Hard timeout enforced via subprocess.run(timeout=...)
  - stdout/stderr captured, never printed to host
  - No network or filesystem restrictions at this phase
    (those are added by Docker + gVisor in Phase B/C)
"""

import subprocess
import sys
import tempfile
import time
import os
from pathlib import Path

from sandbox.models import ExecuteResponse


# Packages available inside the sandbox.
# On Cloud Run the Docker image will have these pre-installed.
# Locally we reuse the host Python — same effect for development.
ALLOWED_IMPORTS_HINT = [
    "pandas", "numpy", "json", "math", "statistics",
    "collections", "itertools", "functools", "datetime",
]


def run_code(code: str, timeout: int = 30) -> ExecuteResponse:
    """
    Write `code` to a temp file and execute it in a subprocess.
    Returns stdout, stderr, exit_code, elapsed time, and timed_out flag.
    """
    # Write code to a temporary file so we get clean tracebacks
    # (line numbers in tracebacks refer to the real file, not exec() strings)
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
