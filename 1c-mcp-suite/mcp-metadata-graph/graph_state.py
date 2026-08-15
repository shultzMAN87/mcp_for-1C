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
  make_state_probe(...)     → probe() → (состояние, деталь), с кешем
  make_guard(neo4j_query)   → guard() → None | JSON-ответ
  NON_CONFIG_CALL_REASONS / NON_CONFIG_REASONS_CYPHER — FIX-4/4.1
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# OBS-1. Формат отказа, придуманный здесь (FIX-3), стал общим для всех
# серверов набора. Тексты и поля переехали в refusal.py; здесь остаётся
# знание про граф — какие у него бывают нерабочие состояния и что про них
# сказать. Второй копии словаря быть не должно: это ровно те «два списка,
# которые обязаны совпадать», из-за которых разошлись генераторы лок-файлов.
try:
    from refusal import refusal as _refusal
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import refusal as _refusal


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
        payload = _refusal(
            "graph_empty",
            "Neo4j отвечает, но граф пуст: ни одного узла :MetadataObject. "
            "Индексация не выполнялась или не завершилась.",
            meaning=(
                "Это НЕ значит, что искомого объекта нет в конфигурации. "
                "Про конфигурацию сейчас не известно ничего — не делай вывод "
                "о её содержимом и не отвечай по памяти. Сообщи пользователю, "
                "что граф не построен."
            ),
            hint=("docker compose run --rm -e METADATA_FORCE_REINDEX=true "
                  "metadata-indexer"),
            graph_state=GRAPH_EMPTY,
        )
    else:
        payload = _refusal(
            "neo4j_unavailable",
            "Neo4j недоступна: запрос не выполнен.",
            meaning=(
                "Это НЕ значит, что искомого объекта нет в конфигурации. "
                "Состояние графа неизвестно — не делай вывод о содержимом "
                "конфигурации и не отвечай по памяти."
            ),
            hint="docker compose ps neo4j && docker compose logs --tail=50 neo4j",
            graph_state=GRAPH_UNAVAILABLE,
        )
        if detail:
            payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False, indent=2)


# B-1. Быстрый отказ, когда уже известно, что Neo4j лежит.
#
# `FAIL-1` научил platform-help не выжидать таймаут на каждом вызове, если
# предыдущая попытка уже сказала «недоступно»: замер дал 7 916 мс → 102 мс.
# Здесь этого не было. Замер 15 августа при остановленной Neo4j:
# восемнадцать примеров, каждый по 3 850 мс, — и это меньшая часть, всего
# инструментов через guard проходит двадцать девять.
#
# В чате с Cursor это четыре секунды тишины на каждый вопрос про
# конфигурацию. Ровно тот исход, ради которого чинились FIX-14 и FAIL-1:
# агент считает инструмент неотвечающим и уходит отвечать по памяти.
#
# Кешируется ТОЛЬКО «недоступна». Остальные два состояния — нет, и это
# принципиально:
#   • GRAPH_OK кешировать нельзя: сервер ослеп бы к падению Neo4j ровно на
#     то же окно, а проба на живой базе стоит миллисекунды;
#   • GRAPH_EMPTY кешировать незачем: Neo4j отвечает, проба дешёвая, а
#     кеш задержал бы момент, когда индексация закончилась. Это дефект
#     FIX-12 в чистом виде, повторять его не будем.
GRAPH_RECHECK_SEC = int(os.environ.get("GRAPH_RECHECK_SEC", "30"))


def make_state_probe(neo4j_query, recheck_sec=None):
    """
    Возвращает probe() → (состояние, деталь) с кешем «недоступна».

    B-1, вторая половина. Первая правка положила кеш внутрь `make_guard`,
    и замер 15 августа показал результат: медиана вызова при мёртвой Neo4j
    15 мс против 3 850. Но три примера остались медленными —
    `metadata_stats` (×3 в датасете) ходит не через guard, а напрямую через
    `graph_state()`, потому что ему нужно РАЗЛИЧАТЬ пустой граф и мёртвую
    базу: на пустом он обязан отвечать, а guard блокирует оба состояния.

    То же самое и у `metadata_reload`, и у watch-инструментов.

    То есть кеш был написан, а трое из четырёх потребителей состояния им не
    пользовались — ровно тот узор, ради которого затевался `AUDIT-2`, и он
    воспроизвёлся внутри правки по его же результатам. Поэтому кеш теперь
    живёт здесь, а `make_guard` — тонкая надстройка над ним.
    """
    recheck = GRAPH_RECHECK_SEC if recheck_sec is None else recheck_sec
    cache = {"detail": "", "at": 0.0, "active": False}

    def probe():
        if cache["active"]:
            left = recheck - (time.monotonic() - cache["at"])
            if left > 0:
                # Кешированный ответ обязан называть себя кешированным.
                # Урок приёмки 15 августа: metadata_stats отдавал из кеша
                # картину здоровья работающего графа в момент, когда графа
                # не было, и по ответу это было никак не видно.
                note = (f"Ответ из кеша: Neo4j признана недоступной "
                        f"{recheck - left:.0f} с назад, следующая проверка "
                        f"через {left:.0f} с. Сеть не опрашивалась.")
                detail = cache["detail"]
                return GRAPH_UNAVAILABLE, (f"{detail} | {note}" if detail else note)
            cache["active"] = False

        state, detail = graph_state(neo4j_query)
        if state == GRAPH_UNAVAILABLE:
            cache.update(detail=detail, at=time.monotonic(), active=True)
        else:
            cache.update(detail="", at=0.0, active=False)
        return state, detail

    def reset():
        cache.update(detail="", at=0.0, active=False)

    probe.reset = reset
    return probe


def make_guard(neo4j_query=None, recheck_sec=None, probe=None):
    """Возвращает guard(): None если граф готов, иначе готовый JSON-ответ.

    Единая точка для всех инструментов, которым нужен наполненный граф.

    B-1: вердикт «Neo4j недоступна» держится `recheck_sec` секунд, и всё
    это время guard отвечает мгновенно, не трогая сеть. По истечении окна
    проба делается заново — сервер поднимается сам, руками перезапускать
    его не нужно.

    У возвращённой функции есть `reset()`: сбрасывает кеш. Нужен тестам и
    ручной диагностике; в рабочем пути не зовётся.
    """
    probe = probe or make_state_probe(neo4j_query, recheck_sec)

    def guard():
        state, detail = probe()
        return None if state == GRAPH_OK else graph_error(state, detail)

    # Кеш общий: передайте тот же probe инструментам, которым нужно
    # различать пустой граф и мёртвую базу, и они получат быстрый отказ
    # даром.
    guard.probe = probe
    guard.reset = probe.reset
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
