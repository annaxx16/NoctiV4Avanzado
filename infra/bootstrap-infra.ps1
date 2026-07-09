# Prepara la infraestructura que Nocti necesita: PostgreSQL vivo + Redis.
#
# Ejecutar en un PowerShell COMO ADMINISTRADOR:
#   powershell -ExecutionPolicy Bypass -File C:\Users\santi\Nocti\NoctiV3\infra\bootstrap-infra.ps1
#
# Hace tres cosas, en este orden, y se para si alguna falla:
#   1. Recupera el PostgreSQL 18 atascado (ahí vive el histórico de book_snapshots).
#   2. Instala WSL2 si falta.  <-- ESTO PIDE REINICIAR
#   3. Instala Docker Desktop.
#
# Tras el reinicio, vuelve a lanzarlo: los pasos ya hechos se saltan solos.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Necesita PowerShell ELEVADO (Ejecutar como administrador)." -ForegroundColor Red
    exit 1
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------- 1. PostgreSQL
Write-Host "`n=== 1/3  PostgreSQL 18 ===" -ForegroundColor Cyan
$env:PGPASSWORD = "umbra_dev"
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $psql -h 127.0.0.1 -p 5432 -U umbra -d umbra -c "SELECT 1" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "   ya acepta conexiones, nada que hacer" -ForegroundColor Green
} else {
    & "$here\fix-pg18.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   fix-pg18 fallo. Paro aqui: sin Postgres no hay histórico." -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------- 2. WSL2
Write-Host "`n=== 2/3  WSL2 ===" -ForegroundColor Cyan
$wslOk = $false
try {
    wsl --status *> $null
    $wslOk = ($LASTEXITCODE -eq 0)
} catch { $wslOk = $false }

if ($wslOk) {
    Write-Host "   ya instalado" -ForegroundColor Green
} else {
    Write-Host "   instalando (Docker Desktop lo necesita)..."
    wsl --install --no-launch
    Write-Host ""
    Write-Host "   REINICIA WINDOWS y vuelve a ejecutar este script." -ForegroundColor Yellow
    Write-Host "   Los pasos ya hechos se saltan solos."
    exit 0
}

# ---------------------------------------------------------------- 3. Docker
Write-Host "`n=== 3/3  Docker Desktop ===" -ForegroundColor Cyan
if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "   ya instalado: $(docker --version)" -ForegroundColor Green
} else {
    Write-Host "   instalando via winget..."
    winget install --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
    Write-Host ""
    Write-Host "   Abre Docker Desktop una vez a mano para que termine de configurarse." -ForegroundColor Yellow
}

Write-Host "`n=== Listo ===" -ForegroundColor Green
Write-Host "Cuando Docker Desktop este corriendo:"
Write-Host ""
Write-Host "    cd C:\Users\santi\Nocti\NoctiV3"
Write-Host "    docker compose up -d          # solo Redis"
Write-Host "    docker compose ps"
Write-Host ""
Write-Host "Postgres NO se levanta en Docker: brain usa el PG18 de la maquina,"
Write-Host "que es donde esta el historico de book_snapshots."
