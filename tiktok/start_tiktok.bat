@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "TIKTOK_DIR=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%TIKTOK_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%TIKTOK_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%TIKTOK_DIR%..\.venv\Scripts\python.exe" set "PYTHON_EXE=%TIKTOK_DIR%..\.venv\Scripts\python.exe"

if not defined PYTHON_EXE (
    where python.exe >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python.exe"
)

if not defined PYTHON_EXE (
    where py.exe >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py.exe"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_EXE (
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer and make sure python or py is in PATH.
    pause
    exit /b 1
)

if not exist "%TIKTOK_DIR%tiktok_collector.py" (
    echo [ERROR] tiktok_collector.py was not found.
    pause
    exit /b 1
)

if not exist "%TIKTOK_DIR%keywords.txt" (
    echo [ERROR] keywords.txt was not found.
    pause
    exit /b 1
)

set "DEBUG_PORT="
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%TIKTOK_DIR%find_adspower_port.ps1"`) do set "DEBUG_PORT=%%P"

if not defined DEBUG_PORT (
    echo [ERROR] No running AdsPower TikTok browser was found.
    echo Start an AdsPower profile and open a TikTok page first.
    pause
    exit /b 1
)

echo Starting TikTok collector on debug port %DEBUG_PORT%...
"%PYTHON_EXE%" %PYTHON_ARGS% "%TIKTOK_DIR%tiktok_collector.py" ^
    --debug-port "%DEBUG_PORT%" ^
    --keywords "%TIKTOK_DIR%keywords.txt" ^
    --max-videos 10 ^
    --search-scrolls 20 ^
    --comment-scrolls 80 ^
    --expand-replies

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Collector exited with code %EXIT_CODE%.
echo.
pause
exit /b %EXIT_CODE%
