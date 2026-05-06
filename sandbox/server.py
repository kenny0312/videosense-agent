"""
Stage 5 — Sandbox Engine
FastAPI service that safely executes untrusted Python code.

Phase A (local):  uvicorn sandbox.server:app --reload --port 8080
Phase B (Docker): docker build + docker run
Phase C (Cloud Run): gcloud run deploy --sandbox=gvisor
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import logging

from sandbox.models import ExecuteRequest, ExecuteResponse
from sandbox.executor import run_code

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("sandbox")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Video Understanding Sandbox",
    description="Safe Python execution engine for LLM-generated code (Stage 5)",
    version="0.1.0",
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Liveness probe — Cloud Run and local Docker both hit this."""
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    """
    Execute a Python code string in an isolated subprocess.

    Request body:
        code    (str)  — Python source code
        timeout (int)  — max seconds to allow (1–120, default 30)

    Response:
        stdout          (str)
        stderr          (str)
        exit_code       (int)   — 0 = success, non-zero = failure, 124 = timeout
        elapsed_seconds (float)
        timed_out       (bool)
    """
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code must not be empty")

    log.info("execute | timeout=%ds | code_len=%d chars", req.timeout, len(req.code))

    result = run_code(req.code, timeout=req.timeout)

    log.info(
        "execute | exit_code=%d | elapsed=%.3fs | timed_out=%s",
        result.exit_code,
        result.elapsed_seconds,
        result.timed_out,
    )

    return result


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("sandbox.server:app", host="0.0.0.0", port=8080, reload=True)
