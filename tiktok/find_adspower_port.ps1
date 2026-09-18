$ErrorActionPreference = "SilentlyContinue"

$cacheRoot = "C:\.ADSPOWER_GLOBAL\cache"

if (-not (Test-Path $cacheRoot)) {
    exit 1
}

$files = Get-ChildItem `
    $cacheRoot `
    -Filter "DevToolsActivePort" `
    -Recurse `
    -ErrorAction SilentlyContinue

foreach ($file in $files) {

    try {
        $port = (
            Get-Content `
                $file.FullName `
                -First 1 `
                -ErrorAction Stop
        ).Trim()
    }
    catch {
        continue
    }

    if ($port -notmatch '^\d+$') {
        continue
    }

    try {
        $targets = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$port/json/list" `
            -TimeoutSec 2
    }
    catch {
        continue
    }

    $tiktok = $targets |
        Where-Object {
            $_.type -eq "page" -and
            $_.url -like "*tiktok.com*"
        } |
        Select-Object -First 1

    if ($tiktok) {
        Write-Output $port
        exit 0
    }
}

exit 2
