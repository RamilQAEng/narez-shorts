# One-click start: fresh cookies, the stack, and a background loop that keeps cookies fresh.
Set-Location $PSScriptRoot
.\refresh_cookies.ps1
docker compose up -d
docker stop openshorts-renderer *> $null   # Remotion renderer is not needed for clips
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$PSScriptRoot\refresh_cookies.ps1","-EveryMinutes","5"
Write-Host "Ready: http://localhost:5175/#app"
