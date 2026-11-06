@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Liepin Talent Scout - First-time setup
echo ============================================================
echo.
echo [1/3] Checking Python ...
python --version
if errorlevel 1 (
  echo   Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo   ^(check "Add Python to PATH"^), then run this file again.
  pause
  exit /b 1
)

echo [2/3] Creating virtual environment ...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 ( echo Failed to create venv. & pause & exit /b 1 )

echo [3/3] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m pip install -r requirements-real.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 ( echo Install failed. Check your network and try again. & pause & exit /b 1 )

echo.
echo   Done. Run start.bat, then open http://127.0.0.1:8898
echo   See README.md for the full disclaimer before use.
echo.
pause
