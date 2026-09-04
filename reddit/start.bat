@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0
title Reddit Post + Comment HTTP Scraper

set "REDDIT_DIR=%~dp0"
set "REPO_ROOT=%~dp0..\"
set "PY_CMD="

if exist "%REDDIT_DIR%.venv\Scripts\python.exe" set "PY_CMD=%REDDIT_DIR%.venv\Scripts\python.exe"

if not defined PY_CMD if exist "%REPO_ROOT%.venv\Scripts\python.exe" set "PY_CMD=%REPO_ROOT%.venv\Scripts\python.exe"

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

if not exist "%REDDIT_DIR%app.py" (
  echo [ERROR] app.py was not found in the Reddit project directory.
  pause
  exit /b 1
)

echo ====================================================================================================
echo Reddit Post + Comment HTTP Scraper
echo ====================================================================================================
echo.

%PY_CMD% "%REDDIT_DIR%app.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ====================================================================================================
echo Program finished.
echo ====================================================================================================
pause
exit /b %EXIT_CODE%
