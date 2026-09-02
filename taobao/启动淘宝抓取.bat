@echo off
setlocal EnableExtensions

title Taobao MTop HTTP Scraper

set "APP_DIR=%~dp0"
set "SCRIPT_NAME=taobao_scraper.py"

cd /d "%APP_DIR%" || (
    echo [ERROR] Cannot enter the BAT directory.
    pause
    exit /b 1
)

if not exist "%SCRIPT_NAME%" (
    echo [ERROR] File not found: %APP_DIR%%SCRIPT_NAME%
    echo Put this BAT file and taobao_scraper.py in the same folder.
    echo.
    pause
    exit /b 1
)

set "PY_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"

if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo [ERROR] Python was not found.
    echo Install Python and make sure python or py is available in PATH.
    echo.
    pause
    exit /b 1
)

echo ================================================================
echo Taobao MTop HTTP Scraper
echo Folder: %APP_DIR%
echo Python: %PY_CMD%
echo ================================================================
echo.

%PY_CMD% -c "import requests, websocket, PIL, openpyxl" >nul 2>nul
if errorlevel 1 (
    echo Missing Python packages. Installing...
    %PY_CMD% -m pip install requests websocket-client pillow openpyxl
    if errorlevel 1 (
        echo.
        echo [ERROR] Package installation failed.
        pause
        exit /b 1
    )
    echo.
)

rem ------------------------------------------------------------------
rem No argument  -> interactive mode
rem With args    -> forward them to the script, for example:
rem   start.bat --keywords "accent chair" --pages 20
rem   start.bat --keywords accent --no-images --timestamp
rem   start.bat --help
rem ------------------------------------------------------------------
set "ARGS=%*"
if not defined ARGS set "ARGS=--interactive"

echo Arguments: %ARGS%
echo.

%PY_CMD% "%SCRIPT_NAME%" %ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" goto SUCCESS
if "%EXIT_CODE%"=="2" goto PARTIAL
goto FAILED

:SUCCESS
echo ================================================================
echo Finished successfully.
echo Excel output folder: %APP_DIR%taobao_output
echo ================================================================
goto END

:PARTIAL
echo ================================================================
echo Finished with PARTIAL data.
echo Taobao risk control interrupted one or more keywords.
echo Successful pages were exported to Excel.
echo Run this BAT again later to resume from the failed page.
echo Excel output folder: %APP_DIR%taobao_output
echo ================================================================
goto END

:FAILED
echo ================================================================
echo Script exited with code: %EXIT_CODE%
echo If authentication expired, start Chrome with port 9222, log in
echo to Taobao, and run this BAT file again.
echo ================================================================

:END
echo.
pause
exit /b %EXIT_CODE%
