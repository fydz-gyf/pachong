param(
    [string]$CategoryUrl = "https://www.wayfair.com/furniture/sb0/accent-chairs-c416225.html",
    [int]$StartPage = 1,
    [int]$EndPage = 50,
    [string]$OutputDir = "",
    [switch]$Fresh,
    [switch]$KeepHtml,
    [switch]$ScrapeReviews,
    [switch]$ReviewsOnly,
    [switch]$ReviewBrowser,
    [switch]$ReviewHeadless,
    [string]$CookieHeaderFile = "",
    [int]$ReviewsPerProduct = 0,
    [int]$ReviewPageSize = 10,
    [int]$ReviewWorkers = 1,
    [int]$ReviewTimeout = 120,
    [int]$ReviewRetries = 3,
    [string]$OfflineProductHtmlDir = "",
    [switch]$Categorize,
    [int]$CategoryPages = 1
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
    "--url", $CategoryUrl,
    "--start-page", $StartPage.ToString(),
    "--end-page", $EndPage.ToString(),
    "--output-dir", $OutputDir
)

if ($Fresh.IsPresent) {
    $Arguments += "--fresh"
}
if ($KeepHtml.IsPresent) {
    $Arguments += "--keep-html"
}
if ($ScrapeReviews.IsPresent) {
    $Arguments += "--scrape-reviews"
    if ($ReviewsOnly.IsPresent) {
        $Arguments += "--reviews-only"
    }
    $Arguments += @("--reviews-per-product", $ReviewsPerProduct.ToString())
    $Arguments += @("--review-page-size", $ReviewPageSize.ToString())
    $Arguments += @("--review-workers", $ReviewWorkers.ToString())
    $Arguments += @("--review-timeout", $ReviewTimeout.ToString())
    $Arguments += @("--review-retries", $ReviewRetries.ToString())
    if ($ReviewBrowser.IsPresent) {
        $Arguments += "--review-browser"
    }
    if ($ReviewHeadless.IsPresent) {
        $Arguments += "--review-headless"
    }
    if (-not [string]::IsNullOrWhiteSpace($CookieHeaderFile)) {
        $Arguments += @("--cookie-header-file", $CookieHeaderFile)
    }
}
if (-not [string]::IsNullOrWhiteSpace($OfflineProductHtmlDir)) {
    $Arguments += @("--offline-product-html-dir", $OfflineProductHtmlDir)
}
if ($Categorize.IsPresent) {
    $Arguments += "--categorize"
    $Arguments += @("--category-pages", $CategoryPages.ToString())
}

if ($ReviewsOnly.IsPresent) {
    Write-Host "Mode: review-only + browser HTTP GraphQL + Chrome UI fallback"
} elseif ($Categorize.IsPresent) {
    Write-Host "Mode: HTTP listing grouped by 'Narrow Your Search' sub-categories"
} elseif ($ReviewBrowser.IsPresent) {
    Write-Host "Mode: HTTP listing + normal Chrome review fallback"
} else {
    Write-Host "Mode: HTTP only (no browser)"
}
Write-Host "Pages: $StartPage-$EndPage"
Write-Host "Output: $OutputDir"
Write-Host ""

& $PythonExe @Arguments
$ExitCode = $LASTEXITCODE
if ($ExitCode -ne 0) {
    throw "HTTP scraper exited with code $ExitCode."
}
