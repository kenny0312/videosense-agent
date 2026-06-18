# ── Video Understanding 本地运行脚本 ──────────────────────────
# 用法: .\run.ps1
# ─────────────────────────────────────────────────────────────

$env:PYTHONUTF8 = "1"
$PYTHON = "C:\Users\User\anaconda3\python.exe"
$ROOT   = $PSScriptRoot

Set-Location $ROOT

# ── helpers ───────────────────────────────────────────────────
function Ensure-Gcp {
    # 没设(或还是占位符)时,自动取 gcloud 默认项目;取不到再手输
    if (-not $env:GCP_PROJECT -or $env:GCP_PROJECT -eq "your-gcp-project-id") {
        $p = ""
        try { $p = (gcloud config get-value project 2>$null) } catch {}
        if ($p) { $env:GCP_PROJECT = ($p | Select-Object -First 1).ToString().Trim() }
        if (-not $env:GCP_PROJECT) { $env:GCP_PROJECT = Read-Host "GCP 项目 ID (GCP_PROJECT)" }
    }
    Write-Host ("  GCP_PROJECT = " + $env:GCP_PROJECT) -ForegroundColor DarkGray
}

function Ensure-DbPassword {
    if (-not $env:ALLOYDB_PASSWORD) {
        $env:ALLOYDB_PASSWORD = Read-Host "AlloyDB 密码"
    }
}

# 选数据库模式:m=mock(免费内存) / r=真 AlloyDB。默认 mock。
function Choose-Db {
    $m = Read-Host "数据库模式 [m=mock(免费) / r=真AlloyDB] (默认 m)"
    if ($m -eq "r") {
        Remove-Item Env:REPL_USE_MOCK_DB -ErrorAction SilentlyContinue
        Ensure-DbPassword
        Write-Host "  DB = AlloyDB (real)" -ForegroundColor DarkGray
    } else {
        $env:REPL_USE_MOCK_DB = "1"
        Write-Host "  DB = mock (in-memory SQLite)" -ForegroundColor DarkGray
    }
}

# ── menu ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "=================================" -ForegroundColor Cyan
Write-Host "  Video Understanding Launcher   " -ForegroundColor Cyan
Write-Host "=================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  --- 组件 ---"
Write-Host "  [1] Test Connections      (GCS + AlloyDB)"
Write-Host "  [2] Inspect Database      (tables & stats)"
Write-Host "  [3] Gemini Pipeline       (batch video analysis)"
Write-Host "  [4] MCP Server            (stdio, for integrations)"
Write-Host "  [5] Sandbox API           (FastAPI :8080)" -ForegroundColor Yellow
Write-Host ""
Write-Host "  --- 完整流水线 ---" -ForegroundColor Green
Write-Host "  [6] Full Pipeline CLI     (pipeline.main, 看 trace)" -ForegroundColor Green
Write-Host "  [7] HTTP API + 前端测试页 (api.server :8000)" -ForegroundColor Green
Write-Host ""

$choice = Read-Host "选择 (1-7)"

switch ($choice) {
    "1" { Ensure-DbPassword; & $PYTHON "$ROOT\utils\test_connections.py" }
    "2" { Ensure-DbPassword; & $PYTHON "$ROOT\utils\inspect_facts.py" }
    "3" { Ensure-Gcp; & $PYTHON "$ROOT\perception\gemini_predicates.py" }
    "4" { & $PYTHON "$ROOT\mcp_server\server.py" }
    "5" {
        Write-Host ""
        Write-Host "Starting Sandbox API on http://localhost:8080 ..." -ForegroundColor Yellow
        Write-Host "Press Ctrl+C to stop." -ForegroundColor Gray
        Write-Host ""
        & $PYTHON -m uvicorn sandbox.server:app --host 0.0.0.0 --port 8080 --reload
    }
    "6" {
        Ensure-Gcp; Choose-Db
        Write-Host ""
        Write-Host "Starting Full Pipeline CLI ..." -ForegroundColor Green
        Write-Host ""
        & $PYTHON -m pipeline.main
    }
    "7" {
        Ensure-Gcp; Choose-Db
        Write-Host ""
        Write-Host "  前端测试页:  http://localhost:8000/" -ForegroundColor Green
        Write-Host "  Swagger:     http://localhost:8000/docs" -ForegroundColor Green
        Write-Host "  健康检查:    http://localhost:8000/health" -ForegroundColor DarkGray
        Write-Host "  注意: 科学/画图类问题还需另开 [5] 沙箱(:8080);纯 SQL 问题无需。" -ForegroundColor DarkGray
        Write-Host "  Ctrl+C 停止" -ForegroundColor Gray
        Write-Host ""
        Start-Process "http://localhost:8000/"   # 自动打开浏览器(服务起来后刷新即可)
        & $PYTHON -m uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
    }
    default { Write-Host "无效选择" -ForegroundColor Red }
}
