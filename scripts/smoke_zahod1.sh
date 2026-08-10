#!/usr/bin/env bash
# Приёмка Захода 1 по разделу 6.1 плана. Запускать с хоста, из корня проекта:
#   bash scripts/smoke_zahod1.sh
#
# Проверяет ровно то, что перечислено в критериях, и ничего сверх.
# Ничего не чинит — только показывает, что сломано.

set -uo pipefail

PASS=0
FAIL=0

ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
head_() { echo; echo "── $1"; }

PORTS="8001 8002 8003 8009"

# ─────────────────────────────────────────────────────────────────
head_ "1. Секретов нет в репозитории (SEC-1)"

if git grep -nEi '(pass|secret|token)\s*=\s*["'"'"'][A-Za-z0-9+/_-]{12,}' -- \
     ':!*.example' ':!scripts/smoke_zahod1.sh' >/dev/null 2>&1; then
  bad "git grep нашёл что-то похожее на секрет:"
  git grep -nEi '(pass|secret|token)\s*=\s*["'"'"'][A-Za-z0-9+/_-]{12,}' -- \
     ':!*.example' ':!scripts/smoke_zahod1.sh' | sed 's/^/      /'
else
  ok "паролей и токенов в отслеживаемых файлах не найдено"
fi

if git grep -n 'AlZSOOMyF1k6MwGV7NrsldDe' >/dev/null 2>&1; then
  bad "старый пароль Neo4j всё ещё в рабочем дереве"
else
  ok "старого пароля Neo4j в рабочем дереве нет"
fi
echo "  ! Напоминание: пароль остался в истории git — он должен быть СМЕНЁН в Neo4j,"
echo "    удаления строки недостаточно."

# ─────────────────────────────────────────────────────────────────
head_ "2. Секрет обязателен, аутентификация работает (SEC-3, TR-3)"

if [ -z "${MCP_SHARED_SECRET:-}" ] && [ -f .env ]; then
  MCP_SHARED_SECRET=$(grep -E '^MCP_SHARED_SECRET=' .env | cut -d= -f2- | tr -d '\r')
fi

if [ -z "${MCP_SHARED_SECRET:-}" ]; then
  bad "MCP_SHARED_SECRET не задан — дальнейшие проверки бессмысленны"
  echo "    задайте его в .env и повторите"
  exit 1
fi
ok "MCP_SHARED_SECRET задан (длина ${#MCP_SHARED_SECRET})"

for p in $PORTS; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$p/mcp" \
         -H 'Content-Type: application/json' \
         -H 'Accept: application/json, text/event-stream' \
         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null)
  [ "$code" = "401" ] && ok "порт $p: без заголовка -> 401" \
                      || bad "порт $p: без заголовка -> $code (ожидали 401)"
done

for p in $PORTS; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$p/mcp" \
         -H "Authorization: Bearer $MCP_SHARED_SECRET" \
         -H 'Content-Type: application/json' \
         -H 'Accept: application/json, text/event-stream' \
         -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' 2>/dev/null)
  [ "$code" = "200" ] && ok "порт $p: с заголовком -> 200" \
                      || bad "порт $p: с заголовком -> $code (ожидали 200)"
done

# ─────────────────────────────────────────────────────────────────
head_ "3. SSE-эндпоинтов больше нет (TR-1)"

for p in $PORTS; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/sse" \
         -H "Authorization: Bearer $MCP_SHARED_SECRET" --max-time 5 2>/dev/null)
  [ "$code" = "404" ] && ok "порт $p: /sse -> 404" \
                      || bad "порт $p: /sse -> $code (ожидали 404)"
done

# ─────────────────────────────────────────────────────────────────
head_ "4. Порты только на loopback (SEC-4)"

if grep -nE '^\s*- "[0-9]+:[0-9]+"' docker-compose.yml >/dev/null 2>&1; then
  bad "в docker-compose.yml остались публикации без 127.0.0.1:"
  grep -nE '^\s*- "[0-9]+:[0-9]+"' docker-compose.yml | sed 's/^/      /'
else
  ok "все публикации портов привязаны к 127.0.0.1"
fi

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "${LAN_IP:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$LAN_IP:8001/mcp" 2>/dev/null)
  [ "$code" = "000" ] && ok "по IP в локальной сети ($LAN_IP) сервер не отвечает" \
                      || bad "по IP $LAN_IP:8001 сервер ответил ($code) — порт наружу открыт"
fi

# ─────────────────────────────────────────────────────────────────
head_ "5. Состав инструментов (TOOL-1, TOOL-2, SEC-5, SEC-6)"

total=0
for p in $PORTS; do
  body=$(curl -s -X POST "http://127.0.0.1:$p/mcp" \
         -H "Authorization: Bearer $MCP_SHARED_SECRET" \
         -H 'Content-Type: application/json' \
         -H 'Accept: application/json, text/event-stream' \
         -H 'MCP-Protocol-Version: 2025-06-18' \
         -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>/dev/null)
  n=$(printf '%s' "$body" | grep -o '"name"' | wc -l | tr -d ' ')
  total=$((total+n))
  echo "  порт $p: инструментов $n"
  for forbidden in metadata_reload metadata_cypher its_search search_all \
                   metadata_references_to metadata_object_details metadata_v3_stats; do
    if printf '%s' "$body" | grep -q "\"$forbidden\""; then
      bad "порт $p: $forbidden не должен быть в наборе"
    fi
  done
done
echo "  ИТОГО инструментов по стеку: $total (цель 25–28)"
[ "$total" -le 28 ] && ok "бюджет инструментов соблюдён" \
                    || bad "бюджет превышен: $total > 28"

# ─────────────────────────────────────────────────────────────────
head_ "6. Шесть серверов действительно выключены"

for p in 8007 8008 8010 8011 8013 8014; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$p/mcp" 2>/dev/null)
  [ "$code" = "000" ] && ok "порт $p не слушает" \
                      || bad "порт $p отвечает ($code) — сервер поднят"
done

# ─────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════"
echo "  прошло: $PASS   провалено: $FAIL"
echo "══════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
