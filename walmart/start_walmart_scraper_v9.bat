@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

title Walmart Scraper V9 HTTP

cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "RUNNER=%~dp0run.py"

if not exist "%PYTHON%" (
    echo.
    echo [ERROR] Python virtual environment was not found:
    echo "%PYTHON%"
    echo.
    pause
    exit /b 1
)

if not exist "%RUNNER%" (
    echo.
    echo [ERROR] run.py was not found:
    echo "%RUNNER%"
    echo.
    pause
    exit /b 1
)

cls
echo ==========================================================================================
echo Walmart HTTP Scraper V9 - Sorftime HTTP Direct
echo ==========================================================================================
echo.
echo 1. Open the Walmart profile in AdsPower first.
echo 2. Keep a working walmart.com tab open.
echo 3. Make sure the Sorftime extension is enabled and logged in.
echo 4. V9 reads Sorftime auth once, then fetches estimates by HTTP.
echo 5. Keeping AdsPower open is recommended for Walmart verification fallback.
echo.
echo ==========================================================================================
echo.

"%PYTHON%" "%RUNNER%" --browser adspower --sorftime-mode http
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ==========================================================================================
if "%EXITCODE%"=="0" (
    echo Scraping task finished.
) else (
    echo Scraper exited with error code: %EXITCODE%
)
echo ==========================================================================================
echo.
pause
exit /b %EXITCODE%
