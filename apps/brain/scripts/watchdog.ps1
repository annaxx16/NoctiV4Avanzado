# Mantiene vivos Redis y la API de umbraNocti sin abrir duplicados.
# Uso: .\scripts\watchdog.ps1
# Opcional: .\scripts\watchdog.ps1 -IntervalSeconds 60

param(
    [int]$IntervalSeconds = 60,
    [int]$ApiPort = 0,
    [int]$FailuresBeforeRestart = 3,
    [switch]$Once
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
$apiUrl = "http://127.0.0.1:${ApiPort}/health"
$redisPort = 6379
$logDir = Join-Path $root "logs"
$logFile = Join-Path $logDir "watchdog.log"
$redisDir = Join-Path $root ".redis"
$redisExe = "C:\Users\santi\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\Redis-8.8.0-Windows-x64-msys2\redis-server.exe"
$apiScript = Join-Path $root "scripts\start-api.ps1"
$script:ConsecutiveApiFailures = 0

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
New-Item -ItemType Directory -Path $redisDir -Force | Out-Null

function Write-WatchdogLog([string]$Message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Message"
    $line | Tee-Object -FilePath $logFile -Append
}

function Get-PortOwners([int]$Port) {
    @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" -and $_.OwningProcess -ne 0 } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Test-HttpHealth([switch]$LogFailure) {
    try {
        $res = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 5
        $ok = $res.status -eq "ok"
        if (-not $ok -and $LogFailure) {
            Write-WatchdogLog "Health respondio pero status no es ok: $($res | ConvertTo-Json -Compress)"
        }
        return $ok
    } catch {
        if ($LogFailure) {
            Write-WatchdogLog "Health fallo: $($_.Exception.Message)"
        }
        return $false
    }
}

function Start-RedisIfNeeded() {
    $owners = Get-PortOwners $redisPort
    if ($owners.Count -gt 0) { return }
    if (-not (Test-Path $redisExe)) {
        throw "No encontre redis-server.exe en $redisExe"
    }
    Write-WatchdogLog "Redis no esta escuchando; iniciando en puerto $redisPort."
    Start-Process -FilePath $redisExe -ArgumentList @("--port", "$redisPort", "--dir", $redisDir, "--appendonly", "no") -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-PortOwners $redisPort).Count -eq 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
    }
    if ((Get-PortOwners $redisPort).Count -eq 0) {
        Write-WatchdogLog "Redis no se inicio en tiempo." 
    }
}

function Start-ApiIfNeeded() {
    if (Test-HttpHealth) {
        if ($script:ConsecutiveApiFailures -gt 0) {
            Write-WatchdogLog "API recuperada sin reinicio tras $script:ConsecutiveApiFailures fallo(s)."
        }
        $script:ConsecutiveApiFailures = 0
        return
    }

    $script:ConsecutiveApiFailures += 1
    Test-HttpHealth -LogFailure | Out-Null

    $owners = Get-PortOwners $ApiPort
    if ($owners.Count -gt 0 -and $script:ConsecutiveApiFailures -lt $FailuresBeforeRestart) {
        Write-WatchdogLog "API no respondio health ($script:ConsecutiveApiFailures/$FailuresBeforeRestart); esperando antes de reiniciar. Puerto $ApiPort, PID(s): $($owners -join ',')."
        return
    }

    foreach ($owner in $owners) {
        Write-WatchdogLog "API en puerto $ApiPort no responde bien; cerrando PID $owner."
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
    }
    
    if ($owners.Count -gt 0) {
        Write-WatchdogLog "Esperando a que se libere el puerto $ApiPort..."
        Start-Sleep -Seconds 3
    } else {
        Start-Sleep -Seconds 1
    }

    Write-WatchdogLog "Iniciando API con scripts\start-api.ps1."
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $apiScript, "-ApiPort", "$ApiPort", "-NoRestart") -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-HttpHealth) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
    }

    if (Test-HttpHealth) {
        $script:ConsecutiveApiFailures = 0
        Write-WatchdogLog "API OK."
    } else {
        Write-WatchdogLog "API todavia no responde OK; se reintentara en el proximo ciclo."
    }
}

Write-WatchdogLog "Watchdog iniciado. Puerto=$ApiPort Intervalo=${IntervalSeconds}s FailuresBeforeRestart=$FailuresBeforeRestart Once=$Once"
do {
    try {
        Start-RedisIfNeeded
        Start-ApiIfNeeded
    } catch {
        Write-WatchdogLog "ERROR: $($_.Exception.Message)"
    }

    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
} while ($true)
