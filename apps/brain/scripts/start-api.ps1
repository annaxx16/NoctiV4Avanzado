# Arranca la API usando el Python del venv directamente.
# No depende de Activate.ps1.

param(
    [int]$ApiPort = 0,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
if ($ApiPort -le 0) {
    if ($env:UMBRA_API_PORT) {
        $ApiPort = [int]$env:UMBRA_API_PORT
    } else {
        $ApiPort = 8000
    }
}

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
$apiLog = Join-Path $logDir "api.log"
$redisPort = 6379
$redisDir = Join-Path $root ".redis"
$redisExe = "C:\Users\santi\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\Redis-8.8.0-Windows-x64-msys2\redis-server.exe"

if (-not (Test-Path $python)) {
    Write-Error "No se encontro el venv en $python. Crea uno con: py -3.11 -m venv .venv"
    exit 1
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
New-Item -ItemType Directory -Path $redisDir -Force | Out-Null

$env:UMBRA_API_PORT = "$ApiPort"

function Get-PortOwners([int]$Port) {
    @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -ne 0 } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

if ((Get-PortOwners $redisPort).Count -eq 0) {
    if (-not (Test-Path $redisExe)) {
        Write-Error "No encontre redis-server.exe en $redisExe"
        exit 1
    }
    Write-Output "Redis no esta escuchando; iniciando en puerto $redisPort..."
    Start-Process -FilePath $redisExe -ArgumentList @("--port", "$redisPort", "--dir", $redisDir, "--appendonly", "no") -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-PortOwners $redisPort).Count -eq 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
    }
    if ((Get-PortOwners $redisPort).Count -eq 0) {
        Write-Error "Redis no se inicio en tiempo."
        exit 1
    }
}

$owners = Get-PortOwners $ApiPort

if ($owners.Count -gt 0) {
    Write-Output "Cerrando procesos previos en puerto $ApiPort..."
    foreach ($processId in $owners) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Write-Output "Esperando a que se libere el puerto..."
    Start-Sleep -Seconds 3
}

Set-Location $root
do {
    Write-Output "Iniciando API en puerto $ApiPort. Log: $apiLog"
    & $python "scripts\run_api.py" 2>&1 | Tee-Object -FilePath $apiLog -Append
    $exitCode = $LASTEXITCODE

    if ($NoRestart) {
        exit $exitCode
    }

    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] API salio con codigo $exitCode; reiniciando en 5s." | Tee-Object -FilePath $apiLog -Append
    Start-Sleep -Seconds 5
} while ($true)
