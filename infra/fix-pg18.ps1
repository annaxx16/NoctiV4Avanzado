# Recupera el PostgreSQL 18 atascado en "modo de recuperación".
#
# Diagnóstico: la máquina se suspendió con Postgres dentro el 27/06. Un checkpoint
# que tarda 270s tardó 109.206s (30 horas). Al despertar, el postmaster hizo su
# ciclo de crash-and-restart y su proceso `startup` nunca arrancó: cero CPU,
# pg_control congelado, y un servidor que responde "en recuperación" eternamente
# sin recuperar nada.
#
# NO HAY CORRUPCIÓN:
#   - pg_control dice `in production`, checkpoint válido en 4/31516F70
#   - el REDO empieza en 4/2E82A410, dentro del WAL 00000001000000040000002E,
#     que está presente en pg_wal
#
# Esto NO usa pg_resetwal. Solo reinicia el servicio para que la recuperación
# corra de verdad. Interrumpir una recuperación por WAL es seguro: es idempotente,
# se reanuda desde el último checkpoint. Equivale a un corte de luz.
#
# Copia de seguridad previa: C:\Users\santi\pg18-data-backup-prefix (413 MB)
#
# USO — en un PowerShell COMO ADMINISTRADOR:
#   powershell -ExecutionPolicy Bypass -File C:\Users\santi\Nocti\NoctiV3\infra\fix-pg18.ps1

# OJO: nada de `$ErrorActionPreference = "Stop"` aquí. En PowerShell 5.1, redirigir
# el stderr de un .exe nativo (psql) envuelve cada línea en un NativeCommandError,
# y con preferencia Stop eso se vuelve una excepción terminante. El script moriría
# en la primera comprobación sin hacer nada. Se comprueba $LASTEXITCODE a mano.
$ErrorActionPreference = "Continue"

$SVC     = "postgresql-x64-18"
$DATADIR = "C:\Program Files\PostgreSQL\18\data"
$LOGDIR  = "$DATADIR\log"
$PSQL    = "C:\Program Files\PostgreSQL\18\bin\psql.exe"

function Test-PgUp {
    $env:PGPASSWORD = "umbra_dev"
    & $PSQL -h 127.0.0.1 -p 5432 -U umbra -d umbra -c "SELECT 1" 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-Pg18Processes {
    Get-CimInstance Win32_Process -Filter "Name='postgres.exe'" |
        Where-Object { $_.CommandLine -like "*PostgreSQL\18*" -or $_.CommandLine -like "*PostgreSQL/18*" }
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output "Este script necesita PowerShell ELEVADO (Ejecutar como administrador)."
    exit 1
}

if (Test-PgUp) {
    Write-Output "PG18 ya acepta conexiones. Nada que hacer."
    exit 0
}

Write-Output "== 1. Parando $SVC =="
Stop-Service -Name $SVC -Force -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Service $SVC).Status -ne 'Stopped' -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
Write-Output "   estado del servicio: $((Get-Service $SVC).Status)"

Write-Output "== 2. Procesos que sobreviven =="
$alive = @(Get-Pg18Processes)
if ($alive.Count -gt 0) {
    # El postmaster está colgado y no atiende la petición de parada. Matarlo es
    # exactamente lo que un corte de luz haría, y la recuperación por WAL está
    # diseñada para eso: se reanuda desde el último checkpoint.
    Write-Output "   $($alive.Count) procesos siguen vivos: $($alive.ProcessId -join ', ')"
    Write-Output "   matandolos (la recuperacion por WAL es idempotente)"
    foreach ($p in $alive) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    $alive = @(Get-Pg18Processes)
    if ($alive.Count -gt 0) {
        Write-Output "   ABORTO: no consigo pararlos. No toco nada mas."
        exit 1
    }
}
Write-Output "   limpio"

Write-Output "== 3. postmaster.pid huerfano =="
$pidfile = "$DATADIR\postmaster.pid"
if (Test-Path $pidfile) {
    # Solo se borra si NINGUN proceso vivo lo reclama. Borrar el pid file de un
    # postmaster vivo permitiria arrancar un segundo sobre el mismo data dir, y
    # ESO si corrompe.
    if (@(Get-Pg18Processes).Count -gt 0) {
        Write-Output "   ABORTO: sigue habiendo postgres vivo. No toco el pid file."
        exit 1
    }
    Remove-Item $pidfile -Force
    Write-Output "   borrado (era del postmaster muerto)"
} else {
    Write-Output "   no existe, nada que hacer"
}

Write-Output "== 4. Arrancando $SVC =="
Start-Service -Name $SVC -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Service $SVC).Status -ne 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
Write-Output "   estado del servicio: $((Get-Service $SVC).Status)"

Write-Output "== 5. Esperando a que acepte conexiones (max 5 min) =="
Write-Output "   (reproduciendo ~48 MB de WAL desde 4/2E82A410)"
$ok = $false
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    if (Test-PgUp) { $ok = $true; break }
    Start-Sleep 5
}

Write-Output "== 6. Log de la recuperacion =="
$log = (Get-ChildItem $LOGDIR -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
Get-Content $log.FullName -Tail 15 | Where-Object { $_ -notmatch "modo de recuperaci" } | Select-Object -Last 10

Write-Output ""
if ($ok) {
    Write-Output "PG18 RECUPERADO y aceptando conexiones en 5432."
    & "C:\Program Files\PostgreSQL\18\bin\pg_controldata.exe" -D $DATADIR |
        Select-String -Pattern "cluster state|estado del cluster"
    exit 0
} else {
    Write-Output "Sigue sin aceptar conexiones. NO fuerces nada mas."
    Write-Output "La copia de seguridad intacta esta en C:\Users\santi\pg18-data-backup-prefix"
    exit 1
}
