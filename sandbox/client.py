"""
Thin HTTP client for the Sandbox /execute endpoint.

Reads SANDBOX_URL and optionally SANDBOX_TOKEN from the environment.
When SANDBOX_URL points at a Cloud Run service (auth required), the token is
acquired automatically via `gcloud auth print-identity-token` if not provided.

pipeline.node_executor imports SandboxClient.execute() to run generated code (with self-heal).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

DEFAULT_URL = "http://localhost:8080"


@dataclass
class ExecuteResult:
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def policy_violation(self) -> bool:
        return self.exit_code == 3 and "PolicyError" in self.stderr


class SandboxClient:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None):
        self.url = (url or os.environ.get("SANDBOX_URL") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("SANDBOX_TOKEN", "")
        if self._needs_auth() and not self.token:
            self.token = self._fetch_gcloud_token()

    def _needs_auth(self) -> bool:
        return ".run.app" in self.url

    @staticmethod
    def _fetch_gcloud_token() -> str:
        # `gcloud` on Windows is a .ps1/.cmd shim, not an exe — needs shell=True.
        # On POSIX it's a real script; either form works.
        try:
            out = subprocess.run(
                "gcloud auth print-identity-token",
                shell=True, capture_output=True, text=True, timeout=15, check=True,
            )
            return out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def execute(self, code: str, timeout: int = 30) -> ExecuteResult:
        body = json.dumps({"code": code, "timeout": timeout}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(f"{self.url}/execute", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return ExecuteResult(
                stdout="", stderr=f"HTTPError {e.code}: {e.read().decode(errors='replace')}",
                exit_code=2, elapsed_seconds=0.0, timed_out=False,
            )

        return ExecuteResult(
            stdout=payload.get("stdout", ""),
            stderr=payload.get("stderr", ""),
            exit_code=int(payload.get("exit_code", 1)),
            elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
            timed_out=bool(payload.get("timed_out", False)),
        )

    def health(self) -> bool:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(f"{self.url}/health", headers=headers)
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            return data.get("status") == "ok"
        except Exception:
            return False


if __name__ == "__main__":
    client = SandboxClient()
    print(f"target: {client.url}")
    print(f"health: {client.health()}")
    r = client.execute("print('hello from sandbox')")
    print(f"  stdout: {r.stdout!r}")
    print(f"  exit:   {r.exit_code}")
