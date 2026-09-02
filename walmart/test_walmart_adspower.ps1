$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "Keep the intended AdsPower profile open on walmart.com before continuing." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" run.py -k "office chair" -p 1 --browser adspower --no-images --save-html --save-next-data --force-refresh
