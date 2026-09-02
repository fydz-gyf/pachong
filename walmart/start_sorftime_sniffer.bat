@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Put this BAT in the Walmart project directory.
    pause
    exit /b 1
)

if not exist "sorftime_cdp_sniffer.py" (
    echo [ERROR] sorftime_cdp_sniffer.py not found.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "sorftime_cdp_sniffer.py"

echo.
pause
