# Arranca el dashboard Streamlit usando el Python del venv.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$dashboardPort = 8501

if (-not (Test-Path $python)) {
    Write-Error "No se encontro el venv en $python. Crea uno con: py -3.11 -m venv .venv"
    exit 1
}

$owners = @(Get-NetTCPConnection -LocalPort $dashboardPort -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -ne 0 } |
    Select-Object -ExpandProperty OwningProcess -Unique)

if ($owners.Count -gt 0) {
    Write-Output "Cerrando dashboard previo en puerto $dashboardPort..."
    foreach ($processId in $owners) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

Set-Location $root
& $python -m streamlit run "dashboard\app.py" --server.port $dashboardPort
