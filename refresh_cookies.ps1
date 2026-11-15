# Dump fresh YouTube cookies from Firefox into yt_cookies.txt (the backend reads it on every download).
# Once:   .\refresh_cookies.ps1
# Loop:   .\refresh_cookies.ps1 -EveryMinutes 5   (keeps the file fresh before any download)
param([int]$EveryMinutes = 0)
$out = Join-Path $PSScriptRoot "yt_cookies.txt"
$tmp = "$out.new"
do {
    uvx yt-dlp --cookies-from-browser firefox --cookies $tmp --skip-download --simulate "https://www.youtube.com/watch?v=jNQXAC9IVRw" *> $null
    if ((Test-Path $tmp) -and (Select-String -Path $tmp -Pattern "youtube.com" -Quiet)) {
        Move-Item $tmp $out -Force
        Write-Host "$(Get-Date -f HH:mm:ss) OK: yt_cookies.txt updated"
    } else {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        Write-Host "$(Get-Date -f HH:mm:ss) FAIL: no YouTube cookies in Firefox (log in there first)"
        if (-not $EveryMinutes) { exit 1 }
    }
    if ($EveryMinutes) { Start-Sleep -Seconds (60 * $EveryMinutes) }
} while ($EveryMinutes)
