# CI-1 — весь набор тестов одной командой (PowerShell-обёртка).
#
# Тот же скрипт, что и на Linux: логика в run_all_tests.py, здесь только
# запуск и проброс кода возврата. Дублировать логику в двух языках значило
# бы обречь одну из копий отстать — а отстала бы, скорее всего, та, которой
# пользуются, потому что правки идут туда, где их видно.
#
#   .\scripts\run_all_tests.ps1
#   .\scripts\run_all_tests.ps1 -Baseline

param([switch]$Quiet, [switch]$Baseline)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $args = @()
    if ($Quiet)    { $args += "--quiet" }
    if ($Baseline) { $args += "--baseline" }
    python scripts/run_all_tests.py @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
