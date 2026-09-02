$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" run.py --list-browser-cdp
