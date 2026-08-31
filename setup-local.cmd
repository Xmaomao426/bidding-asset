@echo off
setlocal
cd /d "%~dp0"
py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
  echo [FAIL] Windows Python 3.12 and the py launcher are required.
  exit /b 2
)
if not exist ".venv\" (
  echo Creating .venv...
  py -3.12 -m venv .venv
  if errorlevel 1 exit /b 2
) else (
  echo Keeping the existing .venv; it will not be deleted or rebuilt.
)
if not exist ".venv\Scripts\python.exe" (
  echo [FAIL] Existing .venv is incomplete; fix it manually.
  exit /b 2
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 2
echo Setup complete. Next run: start-local.cmd --check
exit /b 0
