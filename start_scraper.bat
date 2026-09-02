@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "PY_CMD="

if exist "%ROOT%.venv\Scripts\python.exe" set "PY_CMD=%ROOT%.venv\Scripts\python.exe"

if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer and make sure python or py is in PATH.
    pause
    exit /b 1
)

if not exist "%ROOT%unified_scraper.py" (
    echo [ERROR] unified_scraper.py was not found in the repository root.
    pause
    exit /b 1
)

echo Starting the unified scraper launcher...
echo Repository: %ROOT%
echo.

%PY_CMD% "%ROOT%unified_scraper.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Launcher exited with code %EXIT_CODE%.
)

echo.
pause
exit /b %EXIT_CODE%
