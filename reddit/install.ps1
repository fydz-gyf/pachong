$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "====================================================================================================" -ForegroundColor Cyan
Write-Host "Reddit Scraper - Environment Setup" -ForegroundColor Cyan
Write-Host "Project: $ProjectDir"
Write-Host "===================================================================================================="

$Launcher = $null
$LauncherArgs = @()
try {
    & py -3.11 --version *> $null
    if ($LASTEXITCODE -eq 0) { $Launcher = "py"; $LauncherArgs = @("-3.11") }
} catch {}
if (-not $Launcher) {
    try {
        & python --version *> $null
        if ($LASTEXITCODE -eq 0) { $Launcher = "python"; $LauncherArgs = @() }
    } catch {}
}
if (-not $Launcher) { throw "Python not found. Install Python 3.11 x64 first." }

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[SETUP] Creating .venv..." -ForegroundColor Cyan
    & $Launcher @LauncherArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host "[OK] Existing .venv found." -ForegroundColor Green
}

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
& $Python -m pip install -U pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

$Check = Join-Path $ProjectDir "_verify.py"
$CheckCode = @'
import bs4, curl_cffi, openpyxl, requests, websocket
import reddit_scraper
from reddit_scraper.cli import main
from reddit_scraper.parser.shreddit import parse_post, parse_comments
from reddit_scraper.services.bootstrap import bootstrap_from_browser
from reddit_scraper.services.scraper import ScrapeRunner
print("DEPENDENCY + PROJECT IMPORT CHECK: OK")
'@
[System.IO.File]::WriteAllText($Check, $CheckCode, [System.Text.UTF8Encoding]::new($false))

$env:PYTHONPATH = $ProjectDir
& $Python $Check
if ($LASTEXITCODE -ne 0) { throw "Project import check failed" }
Remove-Item $Check -Force -ErrorAction SilentlyContinue

& $Python -m compileall -q app.py reddit_scraper
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }
& $Python tests\test_parser.py
if ($LASTEXITCODE -ne 0) { throw "parser tests failed" }

Write-Host ""
Write-Host "====================================================================================================" -ForegroundColor Green
Write-Host "SETUP COMPLETE" -ForegroundColor Green
Write-Host "Start: $ProjectDir\start.bat"
Write-Host "====================================================================================================" -ForegroundColor Green
