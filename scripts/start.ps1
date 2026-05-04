# MedStudies — Start server and open dashboard
# Usage: .\scripts\start.ps1

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Port = 8000
$Url = "http://localhost:$Port"

Set-Location $ProjectDir

# Check if already running
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "MedStudies already running at $Url" -ForegroundColor Green
    Start-Process $Url
    exit 0
}

Write-Host "Starting MedStudies..." -ForegroundColor Cyan

# Init DB if needed
python -m medstudies.persistence.database 2>$null
if (!(Test-Path "data\medstudies.db")) {
    python -c "from medstudies.persistence.database import init_db; init_db()"
}

# Start uvicorn in background
$job = Start-Process -FilePath "uvicorn" `
    -ArgumentList "medstudies.interface.api:app --host 0.0.0.0 --port $Port" `
    -WindowStyle Hidden -PassThru

# Wait for server to be ready
Write-Host "Waiting for server..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $null = Invoke-WebRequest -Uri "$Url/api/stats" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        $ready = $true
        break
    } catch {}
}

if ($ready) {
    Write-Host "MedStudies running at $Url" -ForegroundColor Green
    Start-Process $Url
} else {
    Write-Host "Server took too long to start. Open $Url manually." -ForegroundColor Yellow
}
