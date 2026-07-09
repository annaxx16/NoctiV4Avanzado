# Arranca el dashboard Streamlit usando el Python del venv.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "No se encontro el venv en $python. Crea uno con: py -3.11 -m venv .venv"
    exit 1
}

Set-Location $root
& $python -m streamlit run "dashboard\app.py"
