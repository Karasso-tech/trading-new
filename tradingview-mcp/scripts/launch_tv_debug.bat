@echo off
REM Launch a dedicated Chrome window pointed at TradingView with Chrome DevTools
REM Protocol enabled. Uses its own browser profile (tradingview-mcp-chrome-profile),
REM completely separate from your everyday Chrome -- this script never touches your
REM regular browsing windows/tabs/profile.
REM Usage: scripts\launch_tv_debug.bat [port]

set PORT=%1
if "%PORT%"=="" set PORT=9222

set "PROFILE_DIR=%LOCALAPPDATA%\tradingview-mcp-chrome-profile"

REM Auto-detect Chrome install location
set "CHROME_EXE="
if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if exist "%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if "%CHROME_EXE%"=="" (
    for /f "tokens=*" %%i in ('where chrome.exe 2^>nul') do set "CHROME_EXE=%%i"
)

if "%CHROME_EXE%"=="" (
    echo Error: Google Chrome not found.
    echo Checked: %%PROGRAMFILES%%\Google\Chrome, %%PROGRAMFILES(x86)%%\Google\Chrome, %%LOCALAPPDATA%%\Google\Chrome
    echo.
    echo If installed elsewhere, run manually:
    echo   "C:\path\to\chrome.exe" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE_DIR%" https://www.tradingview.com/chart/
    exit /b 1
)

echo Found Chrome at: %CHROME_EXE%
echo Using dedicated profile: %PROFILE_DIR%
echo (First run only: log into TradingView in the window that opens -- the session
echo  persists in this profile after that, so you won't need to log in again.)
echo Starting with --remote-debugging-port=%PORT%...
start "" "%CHROME_EXE%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE_DIR%" --no-first-run --no-default-browser-check https://www.tradingview.com/chart/

echo Waiting for CDP to become available...
timeout /t 5 /nobreak >nul

:check
curl -s http://localhost:%PORT%/json/version >nul 2>&1
if %errorlevel% neq 0 (
    echo Still waiting...
    timeout /t 2 /nobreak >nul
    goto check
)

echo.
echo CDP ready at http://localhost:%PORT%
curl -s http://localhost:%PORT%/json/version
echo.
