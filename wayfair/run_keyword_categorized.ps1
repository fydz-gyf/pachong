param(
    [string]$Keyword = "wicker basket",
    [string]$OutputDir = "",
    [string]$CategoryKeywords = "",
    [int]$CategoryPages = 1,
    [float]$DelayMin = 3.0,
    [float]$DelayMax = 6.0,
    [int]$Timeout = 120,
    [int]$Retries = 3
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Scraper = Join-Path $ProjectDir "wayfair_http_scraper.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python environment was not found: $PythonExe"
}
if (-not (Test-Path $Scraper)) {
    throw "HTTP scraper was not found: $Scraper"
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $ProjectDir "output_http"
}

$Arguments = @(
    $Scraper,
    "--keyword", $Keyword,
    "--output-dir", $OutputDir,
    "--category-pages", $CategoryPages.ToString(),
    "--delay-min", $DelayMin.ToString(),
    "--delay-max", $DelayMax.ToString(),
    "--timeout", $Timeout.ToString(),
    "--retries", $Retries.ToString()
)
if (-not [string]::IsNullOrWhiteSpace($CategoryKeywords)) {
    $Arguments += @("--category-keywords", $CategoryKeywords)
}

Write-Host "Mode: keyword search -> group by sub-category"
Write-Host "Keyword: $Keyword"
if ($CategoryKeywords) {
    Write-Host "Category keywords (explicit): $CategoryKeywords"
} else {
    Write-Host "Category keywords: auto-discovered from search page"
}
Write-Host "Category pages each: $CategoryPages"
Write-Host "Output: $OutputDir"
Write-Host ""

& $PythonExe @Arguments
$ExitCode = $LASTEXITCODE
if ($ExitCode -ne 0) {
    throw "Scraper exited with code $ExitCode."
}