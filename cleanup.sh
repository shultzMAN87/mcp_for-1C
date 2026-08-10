#!/bin/bash
# =====================================================================
# cleanup.sh — Фаза 0: полная очистка стека 1C MCP Suite
# =====================================================================
# Останавливает контейнеры, удаляет сети, volumes и (опционально) образы
# обоих проектов: 27_1c-mcp-suite-full-stack и yaxunit-stack.
#
# Запуск из корня проекта:
#   chmod +x ./cleanup.sh
#   ./cleanup.sh                  # обычная очистка (без удаления образов)
#   ./cleanup.sh --remove-images  # + удалить образы (полная пересборка)
# =====================================================================

set +e  # не падать на пустых результатах

REMOVE_IMAGES=false
if [[ "$1" == "--remove-images" || "$1" == "-r" ]]; then
    REMOVE_IMAGES=true
fi

# Цвета
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GRAY='\033[0;90m'
NC='\033[0m'

section() {
    echo ""
    echo -e "${CYAN}==> $1${NC}"
}

info() {
    echo -e "${GRAY}    $1${NC}"
}

ok() {
    echo -e "${GREEN}    [OK] $1${NC}"
}

# Проверяем, что мы в корне проекта
if [[ ! -f "./docker-compose.yml" ]]; then
    echo -e "${RED}ОШИБКА: не вижу docker-compose.yml в текущей папке.${NC}"
    echo -e "${RED}Запустите скрипт из корня проекта 27_1c-mcp-suite-full-stack.${NC}"
    exit 1
fi

echo ""
echo -e "${YELLOW}===================================================================${NC}"
echo -e "${YELLOW} ФАЗА 0: Полная очистка стека 1C MCP Suite${NC}"
echo -e "${YELLOW}===================================================================${NC}"
if $REMOVE_IMAGES; then
    echo -e "${YELLOW} Режим: с удалением образов (--remove-images)${NC}"
else
    echo -e "${YELLOW} Режим: без удаления образов (используйте --remove-images чтобы добавить)${NC}"
fi

# ---------------------------------------------------------------------
section "1. docker compose down для основного стека"
docker compose down -v --remove-orphans
ok "compose down (основной) выполнен"

# ---------------------------------------------------------------------
section "2. docker compose down для yaxunit-stack"
if [[ -f "./yaxunit-stack/docker-compose.yml" ]]; then
    (cd ./yaxunit-stack && docker compose down -v --remove-orphans)
    ok "compose down (yaxunit-stack) выполнен"
else
    info "yaxunit-stack/docker-compose.yml не найден — пропускаю"
fi

# ---------------------------------------------------------------------
section "3. Удаление орфанных контейнеров от старых версий"

ORPHANS=(
    "mcp-orchestrator"
    "opencode-dev"
    "neo4j"
    "qdrant"
    "mcp-metadata-graph"
    "mcp-bsl-checker"
    "mcp-platform-help"
    "mcp-1c-naparnik"
    "mcp-code-templates"
    "mcp-query-builder"
    "mcp-testing"
    "mcp-code-rag"
    "mcp-rest-proxy"
    "mcp-sonarqube"
    "sonarqube"
    "sonarqube-db"
    "workspace-watcher"
    "metadata-indexer"
    "help-indexer"
    "its-indexer"
    "code-indexer"
    "mcp-eval-runner"
    "mcp-smoke-runner"
)
for name in "${ORPHANS[@]}"; do
    exists=$(docker ps -aq --filter "name=^${name}$" 2>/dev/null)
    if [[ -n "$exists" ]]; then
        docker rm -f "$name" > /dev/null 2>&1
        ok "удалён контейнер: $name"
    fi
done

# Удаляем все по label обоих проектов
ids=$(docker ps -aq --filter "label=com.docker.compose.project=27_1c-mcp-suite-full-stack")
if [[ -n "$ids" ]]; then
    docker rm -f $ids > /dev/null 2>&1
    ok "удалены контейнеры по label проекта 27_1c-mcp-suite-full-stack"
fi

ids=$(docker ps -aq --filter "label=com.docker.compose.project=yaxunit-stack")
if [[ -n "$ids" ]]; then
    docker rm -f $ids > /dev/null 2>&1
    ok "удалены контейнеры по label проекта yaxunit-stack"
fi

# ---------------------------------------------------------------------
section "4. Проверка сети 1c-suite-net и её удаление"

net_exists=$(docker network ls --filter "name=^1c-suite-net$" -q)
if [[ -n "$net_exists" ]]; then
    holders=$(docker network inspect 1c-suite-net --format "{{range .Containers}}{{.Name}} {{end}}" 2>/dev/null)
    if [[ -n "$holders" ]]; then
        info "сеть держат контейнеры: $holders"
        info "попытка отключить и удалить..."
        for holder in $holders; do
            docker network disconnect -f 1c-suite-net "$holder" 2>/dev/null
            docker rm -f "$holder" > /dev/null 2>&1
        done
    fi
    docker network rm 1c-suite-net > /dev/null 2>&1
    ok "сеть 1c-suite-net удалена"
else
    info "сеть 1c-suite-net не существует — пропускаю"
fi

# ---------------------------------------------------------------------
section "5. Удаление volumes"

# Volumes по label обоих проектов
vols=$(docker volume ls -q --filter "label=com.docker.compose.project=27_1c-mcp-suite-full-stack")
if [[ -n "$vols" ]]; then
    docker volume rm $vols > /dev/null 2>&1
    ok "удалены volumes по label 27_1c-mcp-suite-full-stack"
fi

vols=$(docker volume ls -q --filter "label=com.docker.compose.project=yaxunit-stack")
if [[ -n "$vols" ]]; then
    docker volume rm $vols > /dev/null 2>&1
    ok "удалены volumes по label yaxunit-stack"
fi

# Дополнительно — известные имена volume (вдруг что-то по имени без label)
KNOWN_VOLUMES=(
    "yaxunit-payloads"
    "27_1c-mcp-suite-full-stack_neo4j-data"
    "27_1c-mcp-suite-full-stack_qdrant-data"
    "27_1c-mcp-suite-full-stack_mcp-metrics"
    "27_1c-mcp-suite-full-stack_audit-logs"
    "27_1c-mcp-suite-full-stack_sonarqube-data"
    "27_1c-mcp-suite-full-stack_sonarqube-extensions"
    "27_1c-mcp-suite-full-stack_sonarqube-db-data"
)
for vol in "${KNOWN_VOLUMES[@]}"; do
    exists=$(docker volume ls -q --filter "name=^${vol}$" 2>/dev/null)
    if [[ -n "$exists" ]]; then
        docker volume rm "$vol" > /dev/null 2>&1
        ok "удалён volume: $vol"
    fi
done

# ---------------------------------------------------------------------
section "6. Удаление образов (опционально)"

if $REMOVE_IMAGES; then
    imgs=$(docker images --filter "reference=27_1c-mcp-suite-full-stack*" -q)
    if [[ -n "$imgs" ]]; then
        echo "$imgs" | xargs -r docker rmi -f > /dev/null 2>&1
        ok "удалены образы 27_1c-mcp-suite-full-stack*"
    fi
    imgs=$(docker images --filter "reference=yaxunit-stack*" -q)
    if [[ -n "$imgs" ]]; then
        echo "$imgs" | xargs -r docker rmi -f > /dev/null 2>&1
        ok "удалены образы yaxunit-stack*"
    fi
    imgs=$(docker images --filter "reference=mcp-eval-runner*" -q)
    if [[ -n "$imgs" ]]; then
        echo "$imgs" | xargs -r docker rmi -f > /dev/null 2>&1
        ok "удалены образы mcp-eval-runner*"
    fi
else
    info "пропущено (запустите с --remove-images чтобы удалить и образы)"
fi

# ---------------------------------------------------------------------
section "7. Финальная проверка"

echo ""
echo -e "${GRAY}    Контейнеры проекта (должно быть пусто):${NC}"
remaining=$(docker ps -a --format "{{.Names}}" | grep -E "mcp-|neo4j|qdrant|sonar|opencode|onec-|workspace-watcher|metadata-indexer|help-indexer|its-indexer|code-indexer" 2>/dev/null)
if [[ -n "$remaining" ]]; then
    echo -e "${YELLOW}$remaining${NC}"
else
    echo -e "${GREEN}    (нет)${NC}"
fi

echo ""
echo -e "${GRAY}    Volumes проекта (должно быть пусто):${NC}"
remaining=$(docker volume ls --format "{{.Name}}" | grep -E "mcp-|neo4j|qdrant|sonar|yaxunit|27_1c-mcp" 2>/dev/null)
if [[ -n "$remaining" ]]; then
    echo -e "${YELLOW}$remaining${NC}"
else
    echo -e "${GREEN}    (нет)${NC}"
fi

echo ""
echo -e "${GRAY}    Сети проекта (должно быть пусто):${NC}"
remaining=$(docker network ls --format "{{.Name}}" | grep -E "1c-suite|yaxunit" 2>/dev/null)
if [[ -n "$remaining" ]]; then
    echo -e "${YELLOW}$remaining${NC}"
else
    echo -e "${GREEN}    (нет)${NC}"
fi

echo ""
echo -e "${GREEN}===================================================================${NC}"
echo -e "${GREEN} ОЧИСТКА ЗАВЕРШЕНА${NC}"
echo -e "${GREEN}===================================================================${NC}"
echo ""
