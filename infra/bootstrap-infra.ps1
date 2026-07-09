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

# OJO: nada de `$ErrorActionPreference = "Stop"`. En PowerShell 5.1, redirigir el
# stderr de un .exe nativo (psql, wsl, winget) lo envuelve en un NativeCommandError
# y, con preferencia Stop, se vuelve una excepción terminante. El script moriría en
# la primera comprobación sin haber hecho nada y sin decir por qué. Se comprueba
# $LASTEXITCODE a mano.
$ErrorActionPreference = "Continue"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output "Necesita PowerShell ELEVADO (Ejecutar como administrador)."
    exit 1
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$transcript = Join-Path $here "bootstrap.log"
Start-Transcript -Path $transcript -Force | Out-Null

# ---------------------------------------------------------------- 1. PostgreSQL
Write-Output "`n=== 1/3  PostgreSQL 18 ==="
& "$here\fix-pg18.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Output "   fix-pg18 fallo. Paro aqui: sin Postgres no hay historico."
    Stop-Transcript | Out-Null
    exit 1
}

# ---------------------------------------------------------------- 2. WSL2
Write-Output "`n=== 2/3  WSL2 ==="
wsl --status 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Output "   ya instalado"
} else {
    Write-Output "   instalando (Docker Desktop lo necesita)..."
    wsl --install --no-launch
    Write-Output ""
    Write-Output "   REINICIA WINDOWS y vuelve a ejecutar este script."
    Write-Output "   Los pasos ya hechos se saltan solos."
    Stop-Transcript | Out-Null
    exit 0
}

# ---------------------------------------------------------------- 3. Docker
Write-Output "`n=== 3/3  Docker Desktop ==="
if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Output "   ya instalado: $(docker --version)"
} else {
    Write-Output "   instalando via winget..."
    winget install --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
    Write-Output ""
    Write-Output "   Abre Docker Desktop una vez a mano para que termine de configurarse."
}

Write-Output "`n=== Listo ==="
Write-Output "Cuando Docker Desktop este corriendo:"
Write-Output "    cd C:\Users\santi\Nocti\NoctiV3"
Write-Output "    docker compose up -d          # solo Redis"
Write-Output ""
Write-Output "Postgres NO se levanta en Docker: brain usa el PG18 de la maquina,"
Write-Output "que es donde esta el historico de book_snapshots."
Stop-Transcript | Out-Null
