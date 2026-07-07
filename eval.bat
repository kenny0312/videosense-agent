@echo off
REM VS eval one-shot entry. Usage:
REM   eval serve    local web console: run with buttons, auto-refresh (recommended)
REM   eval          quick run, free, opens dashboard
REM   eval live     real Gemini run, costs tokens, opens dashboard
REM   eval view     open dashboard only
REM   eval list     dataset summary
REM   eval check    validate gold grounding
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set REPL_USE_MOCK_DB=1
if exist "C:\Users\User\anaconda3\python.exe" (
  set "PY=C:\Users\User\anaconda3\python.exe"
) else (
  set "PY=python"
)
cd /d "%~dp0"
"%PY%" -m evals %*
endlocal
