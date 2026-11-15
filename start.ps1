# One-click start: fresh cookies, the stack, and a background loop that keeps cookies fresh.
Set-Location $PSScriptRoot
if (-not (Test-Path "docker-compose.override.yml")) {
    Copy-Item "docker-compose.override.example.yml" "docker-compose.override.yml"
    Write-Host "Created docker-compose.override.yml (agent mode) from the example"
}
.\refresh_cookies.ps1
# Only the services clips need: the Remotion renderer is not used and does not need to be built.
docker compose up -d backend frontend
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose failed: is Docker Desktop running?"; exit 1 }
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$PSScriptRoot\refresh_cookies.ps1","-EveryMinutes","5"
Write-Host "Ready: http://localhost:5175/#app"
