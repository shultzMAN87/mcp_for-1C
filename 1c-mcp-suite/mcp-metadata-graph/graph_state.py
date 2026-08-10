"""
Диагностика состояния графа и общие константы серверной стороны (FIX-3).
=========================================================================

Вынесено из `server.py` по одной причине: `server.py` при импорте поднимает
FastMCP и требует `NEO4J_PASSWORD`, поэтому юнит-тестами он не покрывается —
ни в песочнице, ни на хосте пользователя, где `mcp` не установлен. Здесь
зависимостей нет вообще, кроме stdlib, и всё это тестируется напрямую
(`tests_graph_state.py`).

Заодно снята дубликация `NON_CONFIG_CALL_REASONS`: раньше константа лежала
и в `server.py`, и в `server_v3_code_tools.py` с пометкой «править оба
места». Теперь источник один, а от indexer-side (`bsl_resolver.py`) модуль
по-прежнему независим — образы собираются из разных наборов файлов, общего
import-пути между server-side и indexer-side нет.

Что здесь:
  GRAPH_OK / GRAPH_EMPTY / GRAPH_UNAVAILABLE — состояния графа
  graph_state(neo4j_query)  → (состояние, деталь)
  graph_error(state, detail) → JSON-ответ для нерабочего состояния
  make_guard(neo4j_query)   → guard() → None | JSON-ответ
  NON_CONFIG_CALL_REASONS / NON_CONFIG_REASONS_CYPHER — FIX-4/4.1
"""
from __future__ import annotations

import json


# ─── FIX-3: «Neo4j недоступна» ≠ «граф пуст» ─────────────────────────────
#
# До этой правки оба случая давали `_neo4j_available() = False` и один и тот
# же ответ «Neo4j недоступен». Агент, получив его на `metadata_search`,
# уверенно сообщал пользователю, что такого объекта в конфигурации нет —
# хотя на самом деле про конфигурацию не было известно ничего.
#
# Ответ на два нерабочих состояния несёт `answerable: false` и явную
# формулировку «это не значит, что объекта нет»: агент читает текст ошибки,
# и по нему должен остановиться, а не домысливать.

GRAPH_OK = "ok"                    # Neo4j отвечает, граф наполнен
GRAPH_EMPTY = "empty"              # Neo4j отвечает, но :MetadataObject нет
GRAPH_UNAVAILABLE = "unavailable"  # транспорт, авторизация, таймаут


def graph_state(neo4j_query):
    """Состояние графа: (GRAPH_*, деталь для диагностики).

    `neo4j_query` — функция вида `f(cypher, parameters=None) -> dict`
    (в проде это `server._neo4j_query`).
    """
    try:
        result = neo4j_query("MATCH (n:MetadataObject) RETURN count(n) as cnt")
        rows = result["results"][0]["data"]
        count = rows[0]["row"][0] if rows else 0
    except Exception as e:  # noqa: BLE001 — любая ошибка = недоступна
        return GRAPH_UNAVAILABLE, f"{type(e).__name__}: {e}"
    return (GRAPH_OK, "") if count > 0 else (GRAPH_EMPTY, "")


def graph_error(state, detail=""):
    """JSON-ответ для нерабочего состояния графа."""
    if state == GRAPH_EMPTY:
        payload = {
            "error": "graph_empty",
            "graph_state": GRAPH_EMPTY,
            "answerable": False,
            "message": (
                "Neo4j отвечает, но граф пуст: ни одного узла :MetadataObject. "
                "Индексация не выполнялась или не завершилась."
            ),
            "meaning": (
                "Это НЕ значит, что искомого объекта нет в конфигурации. "
                "Про конфигурацию сейчас не известно ничего — не делай вывод "
                "о её содержимом и не отвечай по памяти. Сообщи пользователю, "
                "что граф не построен."
            ),
            "hint": (
                "docker compose run --rm -e METADATA_FORCE_REINDEX=true "
                "metadata-indexer"
            ),
        }
    else:
        payload = {
            "error": "neo4j_unavailable",
            "graph_state": GRAPH_UNAVAILABLE,
            "answerable": False,
            "message": "Neo4j недоступна: запрос не выполнен.",
            "meaning": (
                "Это НЕ значит, что искомого объекта нет в конфигурации. "
                "Состояние графа неизвестно — не делай вывод о содержимом "
                "конфигурации и не отвечай по памяти."
            ),
            "hint": "docker compose ps neo4j && docker compose logs --tail=50 neo4j",
        }
        if detail:
            payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False, indent=2)


def make_guard(neo4j_query):
    """Возвращает guard(): None если граф готов, иначе готовый JSON-ответ.

    Единая точка для всех инструментов, которым нужен наполненный граф.
    """
    def guard():
        state, detail = graph_state(neo4j_query)
        return None if state == GRAPH_OK else graph_error(state, detail)
    return guard


# ─── FIX-4 / FIX-4.1: причины, которые не являются пробелом в графе ──────
#
# Слева от точки стоит не модуль конфигурации: переменная, свойство
# контекста, реквизит формы или глобальный объект платформы. Резолвить такой
# вызов некуда — у метода объекта нет :Callable-адресата.
#
# Смысловой оригинал — NON_CONFIG_CALL_REASONS в
# mcp-metadata-graph-neo4j/bsl_resolver.py (indexer-side). Здесь server-side
# копия: она нужна для Cypher-фильтров, а общего import-пути между двумя
# сторонами нет. При добавлении причины — править оба файла.
NON_CONFIG_CALL_REASONS = (
    "object_method",
    "context_property",
    "platform_global",
    "collection_unknown_method",
    "dataflow_kind_no_module_role",
)

# Готовый литерал списка для подстановки в Cypher (`... IN [...]`).
NON_CONFIG_REASONS_CYPHER = "[" + ", ".join(
    f"'{r}'" for r in NON_CONFIG_CALL_REASONS) + "]"
