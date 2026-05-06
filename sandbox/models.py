"""
Pydantic schemas for the Sandbox API.
"""
from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    code: str = Field(..., description="Python source code to execute")
    timeout: int = Field(default=30, ge=1, le=120, description="Max execution time in seconds")


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    elapsed_seconds: float
    timed_out: bool = False
