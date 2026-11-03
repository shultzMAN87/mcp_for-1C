# SRC-12 — ручная доиндексация графа одной командой (PowerShell).
#
# Делает то же, что watcher по таймеру и metadata_refresh() из чата: в
# режиме git спрашивает git, что изменилось с прошлой индексации, и
# доиндексирует только это. Без git — прежний apply_changes.py (обход
# выгрузки, 3–12 минут).
#
# Логика — в apply_changes.py внутри контейнера; здесь только запуск и
# понятный итог по коду выхода. Работает и при выключенном watcher'е.
#
#   .\scripts\graph-refresh.ps1               # доиндексировать изменившееся
#   .\scripts\graph-refresh.ps1 -DryRun       # показать план, ничего не меняя
#   .\scripts\graph-refresh.ps1 -Reconcile    # сверка: найти правки без сигнала
#   .\scripts\graph-refresh.ps1 -Full         # полная индексация (часы)
#   .\scripts\graph-refresh.ps1 -ForceUnlock  # снять аренду упавшего процесса
#
# Полный лог каждого запуска — logs\graph-refresh-<дата>.log (FIX-40);
# `| Tee-Object` не нужен.
#
# Без PowerShell (Linux/WSL): make graph-refresh  или
#   docker compose run --rm --entrypoint python metadata-indexer /app/apply_changes.py

param(
    [switch]$DryRun,
    [switch]$Reconcile,
    [switch]$Full,
    [switch]$ForceUnlock,
    [switch]$Yes,
    [switch]$PrintOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$py = @("/app/apply_changes.py")
if ($DryRun)      { $py += "--dry-run" }
if ($Reconcile)   { $py += "--reconcile" }
if ($ForceUnlock) { $py += "--force-unlock" }
if ($Full) {
    if (-not $Yes -and -not $PrintOnly) {
        Write-Host "Полная индексация: на боевой выгрузке 1,5–2 часа." -ForegroundColor Yellow
        Write-Host "Граф всё это время отвечает по старым данным, watcher ждёт." -ForegroundColor Yellow
        $answer = Read-Host "Продолжить? [y/N]"
        if ($answer -notmatch '^(y|yes|д|да)$') {
            Write-Host "Отменено."
            exit 0
        }
    }
    $py += @("--full", "--yes")
}

$cmd = @("compose", "run", "--rm", "--entrypoint", "python", "metadata-indexer") + $py

# Для тестов (scripts/tests_host_ps1.py): показать команду, не запуская.
if ($PrintOnly) {
    Write-Output ("docker " + ($cmd -join " "))
    exit 0
}

# FIX-40. Полный лог каждого прогона — в logs\graph-refresh-<дата>.log.
# Раньше лог приходилось ловить `*>&1 | Tee-Object`, и это ломало скрипт:
# с $ErrorActionPreference = "Stop" первая же строка docker в stderr
# (предупреждение compose, логи индексера) становилась ошибкой, PowerShell
# выходил, а контейнер с полной индексацией доживал без свидетелей и
# удалялся (--rm) вместе с логом. Теперь stderr читается как обычный текст,
# вывод идёт и на экран, и в файл. Кодировка — UTF-8: иначе PowerShell 5.1
# раскодирует вывод контейнера как cp866, и кириллица в файле поплывёт.
$docker = if ($env:GRAPH_REFRESH_DOCKER) { $env:GRAPH_REFRESH_DOCKER } else { "docker" }
$logDir = if ($env:GRAPH_REFRESH_LOG_DIR) { $env:GRAPH_REFRESH_LOG_DIR } else { Join-Path $root "logs" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("graph-refresh-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$utf8 = New-Object System.Text.UTF8Encoding($false)
$writer = New-Object System.IO.StreamWriter($logFile, $false, $utf8)
$writer.AutoFlush = $true
$prevEnc = [Console]::OutputEncoding
$prevEap = $ErrorActionPreference

Push-Location $root
try {
    [Console]::OutputEncoding = $utf8
    $ErrorActionPreference = "Continue"
    & $docker @cmd 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $line = $_.Exception.Message
        } else {
            $line = "$_"
        }
        Write-Host $line
        $writer.WriteLine($line)
    }
    $rc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
    [Console]::OutputEncoding = $prevEnc
    $writer.Close()
    Pop-Location
}

Write-Host ""
switch ($rc) {
    0 { Write-Host "Готово." -ForegroundColor Green }
    1 { Write-Host "Часть файлов не обновилась — подробности выше. Состояние не сдвинуто, повторный запуск повторит работу." -ForegroundColor Red }
    2 { Write-Host "Neo4j не отвечает или выгрузка не смонтирована: docker compose ps neo4j; WORKSPACE_DIR в .env" -ForegroundColor Red }
    3 { Write-Host "Нужна полная индексация (или граф ещё не индексировался): .\scripts\graph-refresh.ps1 -Full" -ForegroundColor Yellow }
    4 { Write-Host "Граф сейчас обновляет кто-то другой — повторите через минуту. Если держатель упал: -ForceUnlock" -ForegroundColor Yellow }
    default { Write-Host "Код выхода $rc — подробности выше." -ForegroundColor Red }
}
Write-Host "Лог: $logFile"
exit $rc
