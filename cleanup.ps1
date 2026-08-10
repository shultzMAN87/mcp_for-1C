# =====================================================================
# cleanup.ps1 — Фаза 0: полная очистка стека 1C MCP Suite
# =====================================================================
# Останавливает контейнеры, удаляет сети, volumes и (опционально) образы
# обоих проектов: 27_1c-mcp-suite-full-stack и yaxunit-stack.
#
# Запуск из корня проекта (PowerShell):
#   .\cleanup.ps1               — обычная очистка (без удаления образов)
#   .\cleanup.ps1 -RemoveImages — + удалить образы (полная пересборка)
#
# Если PowerShell ругается на политику выполнения скриптов, разово:
#   powershell -ExecutionPolicy Bypass -File .\cleanup.ps1
# =====================================================================

param(
    [switch]$RemoveImages
)

$ErrorActionPreference = "Continue"  # не падать на пустых результатах

function Section($title) {
    Write-Host ""
    Write-Host "==> $title" -ForegroundColor Cyan
}

function Info($msg) {
    Write-Host "    $msg" -ForegroundColor Gray
}

function Ok($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

# Проверяем что мы в корне проекта
if (-not (Test-Path ".\docker-compose.yml")) {
    Write-Host "ОШИБКА: не вижу docker-compose.yml в текущей папке." -ForegroundColor Red
    Write-Host "Запустите скрипт из корня проекта 27_1c-mcp-suite-full-stack." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Yellow
Write-Host " ФАЗА 0: Полная очистка стека 1C MCP Suite" -ForegroundColor Yellow
Write-Host "===================================================================" -ForegroundColor Yellow
if ($RemoveImages) {
    Write-Host " Режим: с удалением образов (-RemoveImages)" -ForegroundColor Yellow
} else {
    Write-Host " Режим: без удаления образов (используйте -RemoveImages чтобы добавить)" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------
Section "1. docker compose down для основного стека"
docker compose down -v --remove-orphans
Ok "compose down (основной) выполнен"

# ---------------------------------------------------------------------
Section "2. docker compose down для yaxunit-stack"
if (Test-Path ".\yaxunit-stack\docker-compose.yml") {
    Push-Location .\yaxunit-stack
    docker compose down -v --remove-orphans
    Pop-Location
    Ok "compose down (yaxunit-stack) выполнен"
} else {
    Info "yaxunit-stack/docker-compose.yml не найден — пропускаю"
}

# ---------------------------------------------------------------------
Section "3. Удаление орфанных контейнеров от старых версий"

# Удаляем mcp-orchestrator (упомянут в README как типичный орфан после v3)
$orphans = @(
    "mcp-orchestrator",
    "opencode-dev",
    "neo4j",
    "qdrant",
    "mcp-metadata-graph",
    "mcp-bsl-checker",
    "mcp-platform-help",
    "mcp-1c-naparnik",
    "mcp-code-templates",
    "mcp-query-builder",
    "mcp-testing",
    "mcp-code-rag",
    "mcp-rest-proxy",
    "mcp-sonarqube",
    "sonarqube",
    "sonarqube-db",
    "workspace-watcher",
    "metadata-indexer",
    "help-indexer",
    "its-indexer",
    "code-indexer",
    "mcp-eval-runner",
    "mcp-smoke-runner"
)
foreach ($name in $orphans) {
    $exists = docker ps -aq --filter "name=^$name$" 2>$null
    if ($exists) {
        docker rm -f $name 2>$null | Out-Null
        Ok "удалён контейнер: $name"
    }
}

# Удаляем все по label обоих проектов
$ids = docker ps -aq --filter "label=com.docker.compose.project=27_1c-mcp-suite-full-stack"
if ($ids) {
    docker rm -f $ids | Out-Null
    Ok "удалены контейнеры по label проекта 27_1c-mcp-suite-full-stack"
}

$ids = docker ps -aq --filter "label=com.docker.compose.project=yaxunit-stack"
if ($ids) {
    docker rm -f $ids | Out-Null
    Ok "удалены контейнеры по label проекта yaxunit-stack"
}

# ---------------------------------------------------------------------
Section "4. Проверка сети 1c-suite-net и её удаление"

$netExists = docker network ls --filter "name=^1c-suite-net$" -q
if ($netExists) {
    $holders = docker network inspect 1c-suite-net --format "{{range .Containers}}{{.Name}} {{end}}" 2>$null
    if ($holders -and $holders.Trim()) {
        Info "сеть держат контейнеры: $holders"
        Info "попытка отключить и удалить..."
        foreach ($holder in $holders.Trim().Split(" ")) {
            if ($holder) {
                docker network disconnect -f 1c-suite-net $holder 2>$null | Out-Null
                docker rm -f $holder 2>$null | Out-Null
            }
        }
    }
    docker network rm 1c-suite-net 2>$null | Out-Null
    Ok "сеть 1c-suite-net удалена"
} else {
    Info "сеть 1c-suite-net не существует — пропускаю"
}

# ---------------------------------------------------------------------
Section "5. Удаление volumes"

# Volumes по label обоих проектов
$vols = docker volume ls -q --filter "label=com.docker.compose.project=27_1c-mcp-suite-full-stack"
if ($vols) {
    docker volume rm $vols 2>$null | Out-Null
    Ok "удалены volumes по label 27_1c-mcp-suite-full-stack"
}

$vols = docker volume ls -q --filter "label=com.docker.compose.project=yaxunit-stack"
if ($vols) {
    docker volume rm $vols 2>$null | Out-Null
    Ok "удалены volumes по label yaxunit-stack"
}

# Дополнительно — известные имена volume (вдруг что-то по имени без label)
$knownVolumes = @(
    "yaxunit-payloads",
    "27_1c-mcp-suite-full-stack_neo4j-data",
    "27_1c-mcp-suite-full-stack_qdrant-data",
    "27_1c-mcp-suite-full-stack_mcp-metrics",
    "27_1c-mcp-suite-full-stack_audit-logs",
    "27_1c-mcp-suite-full-stack_sonarqube-data",
    "27_1c-mcp-suite-full-stack_sonarqube-extensions",
    "27_1c-mcp-suite-full-stack_sonarqube-db-data"
)
foreach ($vol in $knownVolumes) {
    $exists = docker volume ls -q --filter "name=^$vol$" 2>$null
    if ($exists) {
        docker volume rm $vol 2>$null | Out-Null
        Ok "удалён volume: $vol"
    }
}

# ---------------------------------------------------------------------
Section "6. Удаление образов (опционально)"

if ($RemoveImages) {
    $imgs = docker images --filter "reference=27_1c-mcp-suite-full-stack*" -q
    if ($imgs) {
        $imgs | ForEach-Object { docker rmi -f $_ 2>$null | Out-Null }
        Ok "удалены образы 27_1c-mcp-suite-full-stack*"
    }
    $imgs = docker images --filter "reference=yaxunit-stack*" -q
    if ($imgs) {
        $imgs | ForEach-Object { docker rmi -f $_ 2>$null | Out-Null }
        Ok "удалены образы yaxunit-stack*"
    }
    $imgs = docker images --filter "reference=mcp-eval-runner*" -q
    if ($imgs) {
        $imgs | ForEach-Object { docker rmi -f $_ 2>$null | Out-Null }
        Ok "удалены образы mcp-eval-runner*"
    }
} else {
    Info "пропущено (запустите с -RemoveImages чтобы удалить и образы)"
}

# ---------------------------------------------------------------------
Section "7. Финальная проверка"

Write-Host ""
Write-Host "    Контейнеры проекта (должно быть пусто):" -ForegroundColor Gray
$remaining = docker ps -a --format "{{.Names}}" | Select-String -Pattern "mcp-|neo4j|qdrant|sonar|opencode|onec-|workspace-watcher|metadata-indexer|help-indexer|its-indexer|code-indexer"
if ($remaining) {
    Write-Host $remaining -ForegroundColor Yellow
} else {
    Write-Host "    (нет)" -ForegroundColor Green
}

Write-Host ""
Write-Host "    Volumes проекта (должно быть пусто):" -ForegroundColor Gray
$remaining = docker volume ls --format "{{.Name}}" | Select-String -Pattern "mcp-|neo4j|qdrant|sonar|yaxunit|27_1c-mcp"
if ($remaining) {
    Write-Host $remaining -ForegroundColor Yellow
} else {
    Write-Host "    (нет)" -ForegroundColor Green
}

Write-Host ""
Write-Host "    Сети проекта (должно быть пусто):" -ForegroundColor Gray
$remaining = docker network ls --format "{{.Name}}" | Select-String -Pattern "1c-suite|yaxunit"
if ($remaining) {
    Write-Host $remaining -ForegroundColor Yellow
} else {
    Write-Host "    (нет)" -ForegroundColor Green
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host " ОЧИСТКА ЗАВЕРШЕНА" -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""
