@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo [FAIL] Missing .venv. Run setup-local.cmd first.
  exit /b 2
)
".venv\Scripts\python.exe" -m src.operator_ui.local_runtime %*
exit /b %errorlevel%
