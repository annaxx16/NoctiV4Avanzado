# Recupera el PostgreSQL 18 atascado en "modo de recuperación".
#
# Diagnóstico (ver MERGE_PLAN.md): la máquina se suspendió con Postgres dentro
# el 27/06. Un checkpoint que tarda 270s tardó 109.206s (30 horas). Al despertar,
# el postmaster hizo crash-and-restart y su proceso `startup` nunca arrancó:
# cero CPU, pg_control congelado, y un servidor que responde "en recuperación"
# eternamente sin recuperar nada.
#
# NO HAY CORRUPCIÓN:
#   - pg_control dice `in production`, checkpoint válido en 4/31516F70
#   - el REDO empieza en 4/2E82A410, dentro del WAL 00000001000000040000002E,
#     que está presente en pg_wal
#
# Esto NO usa pg_resetwal. No hay pérdida de datos. Solo reinicia el servicio
# para que la recuperación corra de verdad.
#
# Copia de seguridad previa: C:\Users\santi\pg18-data-backup-prefix (413 MB)
#
# USO — en un PowerShell COMO ADMINISTRADOR:
#   powershell -ExecutionPolicy Bypass -File C:\Users\santi\Nocti\NoctiV3\infra\fix-pg18.ps1

$ErrorActionPreference = "Stop"
$SVC     = "postgresql-x64-18"
$DATADIR = "C:\Program Files\PostgreSQL\18\data"
$LOGDIR  = "$DATADIR\log"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Este script necesita PowerShell ELEVADO (Ejecutar como administrador)." -ForegroundColor Red
    exit 1
}

Write-Host "== 1. Parando $SVC ==" -ForegroundColor Cyan
Stop-Service -Name $SVC -Force
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Service $SVC).Status -ne 'Stopped' -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
Write-Host "   estado: $((Get-Service $SVC).Status)"

Write-Host "== 2. Comprobando que no queda ningun postgres de PG18 ==" -ForegroundColor Cyan
$alive = Get-CimInstance Win32_Process -Filter "Name='postgres.exe'" |
    Where-Object { $_.CommandLine -like "*PostgreSQL\18*" }
if ($alive) {
    Write-Host "   quedan procesos: $($alive.ProcessId -join ', ')" -ForegroundColor Yellow
    Write-Host "   matandolos (seguro: la recuperacion por WAL es idempotente)"
    $alive | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
} else {
    Write-Host "   limpio"
}

Write-Host "== 3. postmaster.pid huerfano? ==" -ForegroundColor Cyan
$pidfile = "$DATADIR\postmaster.pid"
if (Test-Path $pidfile) {
    # Solo se borra si NINGUN proceso vivo lo reclama. Si hubiera uno, abortamos:
    # borrar el pid file de un postmaster vivo permite arrancar un segundo sobre
    # el mismo data dir, y ESO si corrompe.
    $stillAlive = Get-CimInstance Win32_Process -Filter "Name='postgres.exe'" |
        Where-Object { $_.CommandLine -like "*PostgreSQL\18*" }
    if ($stillAlive) {
        Write-Host "   ABORTO: sigue habiendo postgres vivo. No toco el pid file." -ForegroundColor Red
        exit 1
    }
    Remove-Item $pidfile -Force
    Write-Host "   borrado (era del postmaster muerto)"
} else {
    Write-Host "   no existe, nada que hacer"
}

Write-Host "== 4. Arrancando $SVC ==" -ForegroundColor Cyan
$before = (Get-ChildItem $LOGDIR -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
Start-Service -Name $SVC
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Service $SVC).Status -ne 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
Write-Host "   estado: $((Get-Service $SVC).Status)"

Write-Host "== 5. Esperando a que acepte conexiones (max 3 min) ==" -ForegroundColor Cyan
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$env:PGPASSWORD = "umbra_dev"
$ok = $false
$deadline = (Get-Date).AddMinutes(3)
while ((Get-Date) -lt $deadline) {
    & $psql -h 127.0.0.1 -p 5432 -U umbra -d umbra -c "SELECT 1" *> $null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Start-Sleep 5
}

Write-Host "== 6. Log de la recuperacion ==" -ForegroundColor Cyan
$log = (Get-ChildItem $LOGDIR -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
Get-Content $log -Tail 20

Write-Host ""
if ($ok) {
    Write-Host "PG18 RECUPERADO y aceptando conexiones en 5432." -ForegroundColor Green
    & "C:\Program Files\PostgreSQL\18\bin\pg_controldata.exe" -D $DATADIR |
        Select-String -Pattern "cluster state|estado del cluster"
} else {
    Write-Host "Sigue sin aceptar conexiones. NO fuerces nada mas." -ForegroundColor Red
    Write-Host "Pegame las 20 lineas de log de arriba y seguimos desde ahi."
    Write-Host "La copia de seguridad intacta esta en C:\Users\santi\pg18-data-backup-prefix"
}
