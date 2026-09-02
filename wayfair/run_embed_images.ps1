param(
    [string]$ProjectDir = "",
    [string]$InputFile = "",
    [string]$Sheet = "UniqueProducts",
    [int]$ChunkSize = 300,
    [int]$MaxWorkers = 6,
    [switch]$Categorized
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $ProjectDir = $PSScriptRoot
}

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$ScriptPath = Join-Path $ProjectDir "embed_wayfair_images.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python environment was not found: $PythonExe"
}
if (-not (Test-Path $ScriptPath)) {
    throw "Image embedder was not found: $ScriptPath"
}
if ([string]::IsNullOrWhiteSpace($InputFile)) {
    $InputFile = Join-Path $ProjectDir "output_http\wayfair_products.xlsx"
}
if (-not (Test-Path $InputFile)) {
    throw "Input workbook was not found: $InputFile"
}

$OutputDir = Join-Path (Split-Path -Parent $InputFile) "with_images"

& $PythonExe -m pip install --upgrade Pillow
if ($LASTEXITCODE -ne 0) {
    throw "Pillow installation failed."
}

& $PythonExe $ScriptPath `
    --input $InputFile `
    --output-dir $OutputDir `
    --sheet $Sheet `
    --chunk-size $ChunkSize `
    --max-workers $MaxWorkers `
    $(if ($Categorized) { "--categorized" } else { "" })

if ($LASTEXITCODE -ne 0) {
    throw "Image embedding failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Completed."
Write-Host "Output: $OutputDir"
