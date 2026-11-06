@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b 1
)

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if "%CHROME%"=="" (
  echo Google Chrome not found. Please install Chrome first.
  pause
  exit /b 1
)

if not exist "data\chrome-profile-liepin" mkdir "data\chrome-profile-liepin"

echo ============================================================
echo   Liepin Talent Scout
echo ============================================================
echo [1/2] Opening Chrome ^(debug port 53471^) ...
start "" "%CHROME%" --remote-debugging-port=53471 --user-data-dir="%~dp0data\chrome-profile-liepin" --no-first-run --no-default-browser-check --disable-blink-features=AutomationControlled "https://h.liepin.com/"
echo   Log in to Liepin in that window and keep it open.
echo [2/2] Console: http://127.0.0.1:8898
echo   Close this window to stop. See README.md for the disclaimer.
echo.
".venv\Scripts\python.exe" -u -m app.console
pause
