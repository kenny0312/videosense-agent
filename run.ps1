# ── Video Understanding 本地运行脚本 ──────────────────────────
# 用法: .\run.ps1
# ─────────────────────────────────────────────────────────────

$env:PYTHONUTF8 = "1"
$PYTHON = "C:\Users\User\anaconda3\python.exe"
$ROOT   = $PSScriptRoot

Set-Location $ROOT

# 检查密码
if (-not $env:ALLOYDB_PASSWORD) {
    $env:ALLOYDB_PASSWORD = Read-Host "AlloyDB 密码"
}

Write-Host ""
Write-Host "=================================" -ForegroundColor Cyan
Write-Host "  Video Understanding Launcher   " -ForegroundColor Cyan
Write-Host "=================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  [1] Test Connections      (GCS + AlloyDB)"
Write-Host "  [2] Inspect Database      (tables & stats)"
Write-Host "  [3] Planner REPL          (NL → DAG query)"  -ForegroundColor Green
Write-Host "  [4] Gemini Pipeline       (batch video analysis)"
Write-Host "  [5] MCP Server            (stdio, for integrations)"
Write-Host ""

$choice = Read-Host "选择 (1-5)"

switch ($choice) {
    "1" { & $PYTHON "$ROOT\utils\test_connections.py" }
    "2" { & $PYTHON "$ROOT\utils\inspect_facts.py" }
    "3" { & $PYTHON "$ROOT\planner\dag_planner.py" }
    "4" { & $PYTHON "$ROOT\perception\gemini_predicates.py" }
    "5" { & $PYTHON "$ROOT\mcp_server\server.py" }
    default { Write-Host "无效选择" -ForegroundColor Red }
}
