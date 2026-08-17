# ⚠️ ФАЙЛ ДОЛЖЕН БЫТЬ СОХРАНЁН В UTF-8 С BOM.
# PowerShell 5.1 читает .ps1 без BOM как ANSI (cp1251): кириллица
# превращается в мусор, кавычки разъезжаются, скрипт не парсится. Если
# правите его редактором — проверьте кодировку перед сохранением.
#
# Генерация лок-файлов для трёх образов (TR-2). Версия для PowerShell.
#
# Аналог scripts/gen_lockfiles.sh — на Windows без WSL make и bash
# недоступны, а лок-файлы нужны всё те же.
#
#     .\scripts\gen_lockfiles.ps1                # все три
#     .\scripts\gen_lockfiles.ps1 -Only bsl      # только один
#     .\scripts\gen_lockfiles.ps1 -Only base,bsl # несколько
#
# Если PowerShell откажется запускать скрипт из-за политики выполнения:
#     powershell -ExecutionPolicy Bypass -File .\scripts\gen_lockfiles.ps1
#
# Нужна сеть: здесь резолвятся транзитивные зависимости. Резолв идёт внутри
# базового образа, а не локальным pip — набор версий зависит от версии
# Python и платформы, и лок-файл, собранный на хосте, описывал бы не тот
# образ, который собирается.
#
# ⚠️ И по той же причине КАЖДЫЙ лок-файл резолвится под СВОЮ версию Python.
# Наступили на это ровно один раз: все три прогнали в python:3.12-slim, а
# Dockerfile.bsl собирается на eclipse-temurin:17-jre-jammy, где системный
# python — 3.10. Сборка упала на `rpds-py==2026.6.3`: под 3.10 такого
# дистрибутива нет.
#
# Резолвим при этом в python:<версия>-slim, а не в самом базовом образе:
# значение имеет версия Python, а тащить ради неё apt, venv и чужие
# репозитории — источник отказов, не имеющих отношения к зависимостям.

param(
    [ValidateSet("base", "bsl", "embeddings", "v8std")]
    [string[]]$Only
)

$ErrorActionPreference = "Stop"

$suite = Join-Path (Split-Path $PSScriptRoot -Parent) "1c-mcp-suite"

# image совпадает с FROM соответствующего Dockerfile.
$pairs = @(
    @{ key   = "base"
       src   = "requirements.txt"
       dst   = "requirements.lock.txt"
       image = "python:3.12-slim"
       prep  = "" },
    @{ key   = "bsl"
       src   = "requirements-bsl.txt"
       dst   = "requirements-bsl.lock.txt"
       # НЕ eclipse-temurin, хотя образ собирается именно на нём.
       # Значение имеет версия Python, а не дистрибутив: в jammy системный
       # python — 3.10, и python:3.10-slim даёт ровно его, но с готовым pip.
       # Резолв в самом temurin стоил четырёх падений подряд — своя версия
       # pip, отсутствующий typing_extensions, недоступный jammy-backports
       # и, наконец, apt с кодом 100. Ни одно из них не относилось к
       # зависимостям проекта.
       #
       # ⚠️ Меняете python в Dockerfile.bsl — меняйте тег и здесь.
       image = "python:3.10-slim"
       prep  = "" },
    @{ key   = "embeddings"
       src   = "requirements-embeddings.txt"
       dst   = "requirements-embeddings.lock.txt"
       image = "python:3.12-slim"
       prep  = "" },
    # STD-4: образ v8std-mcp. База — python:3.12-slim, как в Dockerfile.v8std.
    #
    # LOCK-1: в gen_lockfiles.sh эта пара была с самого начала, а здесь —
    # нет. Разошлись два скрипта, делающих одно и то же: на Linux `make lock`
    # генерировал четыре лок-файла, на Windows — три, и v8std молча оставался
    # без лока. Правите список — правьте оба файла.
    @{ key   = "v8std"
       src   = "requirements-v8std.txt"
       dst   = "requirements-v8std.lock.txt"
       image = "python:3.12-slim"
       prep  = "" }
)

if ($Only) {
    $pairs = $pairs | Where-Object { $Only -contains $_.key }
}

Push-Location $suite
try {
    foreach ($p in $pairs) {
        Write-Host "→ $($p.src) → $($p.dst)  (в $($p.image))" -ForegroundColor Cyan
        # typing_extensions ставится явно: pip-compile импортирует его
        # безусловно (piptools/writer.py), а pip-tools под Python 3.10 не
        # объявляет его зависимостью, и импорт падает. Диагноз «старый pip»
        # был неверен: обновление pip и отдельный venv проблему не сняли.
        # На 3.12 пакет лишний, но безвредный.
        # PERF-11: --emit-index-url переносит в лок адрес индекса CPU-сборок
        # torch из requirements-embeddings.txt. Без него сборка образа снова
        # притащит ~2,5 ГБ колёс nvidia-* из PyPI. Флаг обязан совпадать с
        # bash-версией — за этим следит scripts/tests_lockfile_pairs.py.
        $cmd = $p.prep +
               "pip install --quiet pip-tools typing_extensions && " +
               "pip-compile --quiet --strip-extras --emit-index-url --output-file '$($p.dst)' '$($p.src)'"
        docker run --rm -v "${PWD}:/w" -w /w $p.image sh -c $cmd
        if ($LASTEXITCODE -ne 0) {
            throw "pip-compile завершился с кодом $LASTEXITCODE на $($p.src)"
        }
    }

    Write-Host ""
    Write-Host "Готово:" -ForegroundColor Green
    foreach ($p in $pairs) {
        $n = (Select-String -Path $p.dst -Pattern '^[a-zA-Z]').Count
        Write-Host ("  {0,-38} пакетов: {1}" -f $p.dst, $n)
    }
    Write-Host ""
    Write-Host "Дальше: закоммитить лок-файлы и пересобрать образы —"
    Write-Host "  docker compose build"
}
finally {
    Pop-Location
}
