"""
MCP-сервер: Графовый поиск по метаданным 1С (v2.1, с пагинацией)
=================================================================
Поиск через Neo4j (если доступен) или in-memory фолбэк.

Изменения v2.1:
  - Добавлены параметры limit/offset во все «тяжёлые» инструменты
  - metadata_object_details теперь разделён на секции (опциональные реквизиты,
    связи, подсистемы), чтобы не возвращать всё сразу
  - Все ответы возвращают has_more/next_offset для пагинации
  - metadata_list_objects БЕЗ kind требует явного limit (защита от «дай всё»)
  - Добавлен metadata_object_modules для чтения модулей объектов частями
"""

import os
import json
import re
import base64
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict
import logging

from mcp.server.fastmcp import FastMCP

# Импорт модуля пагинации (должен лежать рядом в /app)
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/app")

# FIX-3: диагностика состояния графа и общие константы. Отдельный модуль без
# зависимости от FastMCP — иначе логику не покрыть юнит-тестами.
from graph_state import (            # noqa: E402
    GRAPH_OK, GRAPH_EMPTY, GRAPH_UNAVAILABLE,
    graph_state, graph_error, make_guard,
    NON_CONFIG_CALL_REASONS, NON_CONFIG_REASONS_CYPHER,
)

try:
    from mcp_pagination import PaginationParams, paginate, summarize, truncate_text_window
except ImportError:
    # Фолбэк: встроенные определения если модуль не найден
    from dataclasses import dataclass
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100

    @dataclass
    class PaginationParams:
        limit: int = DEFAULT_LIMIT
        offset: int = 0
        max_limit: int = MAX_LIMIT
        def __post_init__(self):
            if self.limit <= 0: self.limit = DEFAULT_LIMIT
            if self.limit > self.max_limit: self.limit = self.max_limit
            if self.offset < 0: self.offset = 0

    def paginate(items, params, extra=None):
        total = len(items)
        end = params.offset + params.limit
        page = items[params.offset:end]
        response = {"total": total, "returned": len(page), "offset": params.offset,
                    "limit": params.limit, "has_more": end < total}
        if end < total: response["next_offset"] = end
        response["items"] = page
        if extra: response.update(extra)
        return response

    def summarize(items, preview=5):
        return {"total": len(items), "preview_count": min(preview, len(items)),
                "preview": items[:preview]}

    def truncate_text_window(text, offset=0, window=2000):
        if not text: return {"text": "", "total": 0, "has_more": False}
        total = len(text)
        if offset >= total:
            return {"text": "", "offset": offset, "total": total, "has_more": False}
        end = min(offset + window, total)
        result = {"text": text[offset:end], "offset": offset, "window": window,
                  "total": total, "shown_chars": end - offset, "has_more": end < total}
        if end < total: result["next_offset"] = end
        return result


mcp = FastMCP("1C Metadata Graph")
logger = logging.getLogger(__name__)

# Опциональный кэш для тяжёлых read-only запросов
try:
    from mcp_cache import cached
except ImportError:
    def cached(ttl=300):  # noqa: D401 - совместимая no-op заглушка
        def deco(fn): return fn
        return deco

SRC_DIR = os.environ.get("METADATA_SRC_DIR", "/data/1c-src")
NEO4J_URL = os.environ.get("NEO4J_URL", "http://neo4j:7474")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
# SEC-2: слабого дефолта больше нет. Имя переменной приведено к тому же
# виду, что в docker-compose.yml — NEO4J_PASSWORD.
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_PASS")
if not NEO4J_PASS:
    raise SystemExit(
        "NEO4J_PASSWORD не задан — сервер не стартует (SEC-2).\n"
        "Задайте пароль в .env; дефолтного значения больше нет."
    )



# ═══════════════ START ═══════════════
def _neo4j_query(cypher, parameters=None):
    auth = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASS}".encode()).decode()
    payload = json.dumps({
        "statements": [{
            "statement": cypher,
            "parameters": parameters or {},
        }]
    }).encode()
    req = urllib.request.Request(
        f"{NEO4J_URL}/db/neo4j/tx/commit",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        # Транспортная ошибка (Neo4j недоступен/таймаут) — лог + raise, чтобы
        # ловить выше через try/except Exception.
        logger.warning("Neo4j transport error: %s; cypher head: %s",
                       e, (cypher or "")[:200].replace("\n", " "))
        raise
    errors = result.get("errors", [])
    if errors:
        # Это ошибки Cypher-уровня: SyntaxError, ConstraintViolation, etc.
        # Раньше летели как RuntimeError без логирования — отсюда невидимые
        # баги типа "Variable t not defined" в WITH-клаузе.
        logger.warning("Neo4j query error: %s; cypher head: %s",
                       errors, (cypher or "")[:200].replace("\n", " "))
        raise RuntimeError(f"Neo4j: {errors}")
    return result


def _neo4j_available():
    try:
        result = _neo4j_query("MATCH (n:MetadataObject) RETURN count(n) as cnt")
        rows = result["results"][0]["data"]
        return rows and rows[0]["row"][0] > 0
    except Exception:
        return False


# ─── FIX-3 / FIX-4: диагностика и константы вынесены в graph_state.py ───
# Модуль без зависимости от FastMCP — только так серверную логику получилось
# накрыть юнит-тестами (tests_graph_state.py): импорт server.py поднимает
# FastMCP и требует NEO4J_PASSWORD.
_graph_state = lambda: graph_state(_neo4j_query)          # noqa: E731
_graph_error = graph_error
_graph_guard = make_guard(_neo4j_query)


def _neo4j_rows(cypher, params=None):
    result = _neo4j_query(cypher, params)
    columns = result["results"][0].get("columns", [])
    rows = []
    for data in result["results"][0].get("data", []):
        row = {}
        for i, col in enumerate(columns):
            row[col] = data["row"][i]
        rows.append(row)
    return rows


def _neo4j_count(cypher, params=None):
    """Получить счётчик из запроса с RETURN count(...).

    ВАЖНО: при ошибке Cypher (например, синтаксис) пишем WARNING и
    возвращаем 0 — это позволяет вызывающему получить «тотал=0» вместо
    падения, но в логах сервера остаётся след с текстом запроса.

    Раньше использовалось logger.debug, что приводило к молчанию: tool
    возвращал {"total": 0, "items": []}, и было непонятно, баг в данных
    или в Cypher. Эта правка появилась после фикса 4.6.1 (баг с переменной
    `t`, не упомянутой в WITH).
    """
    try:
        rows = _neo4j_rows(cypher, params)
        if rows:
            for v in rows[0].values():
                if isinstance(v, (int, float)):
                    return int(v)
    except Exception as e:
        logger.warning("Neo4j count failed (returning 0): %s; cypher head: %s",
                       e, (cypher or "")[:200].replace("\n", " "))
    return 0
# ═══════════════ END ═══════════════


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
@cached(ttl=600)
def metadata_stats() -> str:
    """
    Статистика графа: слой метаданных (объекты, реквизиты, типы) и слой кода
    (модули, процедуры, места вызова, покрытие резолва).

    TOOL-1: заменяет собой прежние metadata_stats + metadata_v3_stats +
    code_v3_stats. Три почти одинаковых инструмента заставляли агента
    выбирать наугад; теперь один ответ содержит обе картины.

    Возвращает JSON:
      {
        "metadata": {"objects": N, "by_kind": [...], "attributes": N,
                     "tabular_sections": N, "forms": N, "enum_values": N,
                     "types": N},
        "code":     {"modules": N, "callables": N, "callsites": N,
                     "callsites_resolved": N, "callsites_unresolved": N,
                     "callsites_object_method": N,
                     "resolve_coverage_pct": X},
        "relations": {"<тип ребра>": N, ...},
        "graph_empty": bool
      }

    FIX-4: `callsites_unresolved` — только настоящие пробелы в графе (вызов
    похож на обращение к коду конфигурации, но адресат не найден).
    `callsites_object_method` — вызовы методов объектов платформы
    (`РезультатЗапроса.Выбрать()`): слева от точки объявленная переменная,
    :Callable-адресата у них нет и быть не может. В `resolve_coverage_pct`
    вторая группа не входит — иначе метрика занижается (на Котировках было
    72.66% при фактическом покрытии за 90%).

    FIX-3: инструмент отвечает и на пустом графе — он для того и нужен,
    чтобы отличить «объекта нет в конфигурации» от «граф не построен».
    При пустом графе возвращает нули и `graph_empty: true`; ошибку отдаёт
    только если Neo4j недоступна.
    """
    # FIX-3: пустой граф — валидный ответ этого инструмента, а не ошибка.
    state, detail = _graph_state()
    if state == GRAPH_UNAVAILABLE:
        return _graph_error(state, detail)

    def _n(cypher: str) -> int:
        rows = _neo4j_rows(cypher)
        if not rows:
            return 0
        return list(rows[0].values())[0] or 0

    metadata_block = {
        "objects":          _n("MATCH (n:MetadataObject) RETURN count(n) AS c"),
        "attributes":       _n("MATCH (n:Attribute) RETURN count(n) AS c"),
        "tabular_sections": _n("MATCH (n:TabularSection) RETURN count(n) AS c"),
        "forms":            _n("MATCH (n:Form) RETURN count(n) AS c"),
        "enum_values":      _n("MATCH (n:EnumValue) RETURN count(n) AS c"),
        "types":            _n("MATCH (n:Type) RETURN count(n) AS c"),
        "by_kind": _neo4j_rows(
            "MATCH (n:MetadataObject) RETURN n.kind AS kind, count(n) AS count "
            "ORDER BY count DESC"
        ),
    }

    cs_resolved   = _n("MATCH (cs:CallSite {resolved: true}) RETURN count(cs) AS c")
    cs_unresolved = _n("MATCH (cs:CallSite {resolved: false}) RETURN count(cs) AS c")
    cs_total = cs_resolved + cs_unresolved

    # FIX-4: часть неразрешённых вызовов — обращения к методам объектов
    # платформы (`РезультатЗапроса.Выбрать()`), а не пробелы в графе.
    # Их :Callable-адресата не существует, поэтому в знаменатель покрытия
    # они не идут: до FIX-4 они занижали метрику с ~91% до 72.66%.
    cs_object_method = _n(
        "MATCH (cs:CallSite) WHERE cs.resolved = false "
        f"AND cs.reason IN {NON_CONFIG_REASONS_CYPHER} "
        "RETURN count(cs) AS c"
    )
    cs_gaps = cs_unresolved - cs_object_method
    cs_denom = cs_resolved + cs_gaps

    code_block = {
        "modules":                 _n("MATCH (n:Module) RETURN count(n) AS c"),
        "callables":               _n("MATCH (n:Callable) RETURN count(n) AS c"),
        "callsites":               cs_total,
        "callsites_resolved":      cs_resolved,
        "callsites_unresolved":    cs_gaps,
        "callsites_object_method": cs_object_method,
        "resolve_coverage_pct": round(100.0 * cs_resolved / cs_denom, 2) if cs_denom else 0.0,
    }

    relations = {
        row["rel"]: row["cnt"]
        for row in _neo4j_rows(
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC"
        )
    }

    return json.dumps({
        "metadata":    metadata_block,
        "code":        code_block,
        "relations":   relations,
        "graph_empty": metadata_block["objects"] == 0 and code_block["callables"] == 0,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_search(query: str, kind: str = "", limit: int = 20, offset: int = 0) -> str:
    """
    Поиск объектов метаданных.

    Параметры:
      query  — строка поиска
      kind   — фильтр по типу ("Справочник", "РегистрСведений", ...)
      limit  — макс. результатов (1-100, по умолчанию 20)
      offset — смещение для пагинации
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    where = "WHERE (toLower(n.name) CONTAINS toLower($q) OR toLower(n.synonym) CONTAINS toLower($q) OR toLower(n.full_name) CONTAINS toLower($q))"
    params = {"q": query, "limit": p.limit, "offset": p.offset}
    if kind:
        where += " AND (toLower(n.kind) = toLower($kind) OR toLower(n.kind_eng) = toLower($kind))"
        params["kind"] = kind

    # Сначала считаем total
    total = _neo4j_count(f"""
        MATCH (n:MetadataObject)
        {where}
        RETURN count(n)
    """, params)

    # Потом выбираем страницу
    rows = _neo4j_rows(f"""
        MATCH (n:MetadataObject)
        {where}
        RETURN n.full_name as full_name, n.kind as kind, n.name as name,
               n.synonym as synonym
        ORDER BY n.full_name
        SKIP $offset
        LIMIT $limit
    """, params)

    end = p.offset + len(rows)
    response = {
        "query": query,
        "kind_filter": kind,
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "items": rows,
    }
    if end < total:
        response["next_offset"] = end

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_object_details(
    full_name: str,
    include_attributes: bool = True,
    include_references: bool = False,
    include_subsystems: bool = False,
    attributes_limit: int = 50,
) -> str:
    """
    Описание объекта метаданных. По умолчанию возвращает только основную
    информацию и реквизиты. Связи и подсистемы — опционально.

    Параметры:
      full_name           — например "Справочники.Аук_Аукционы"
      include_attributes  — включить реквизиты (по умолчанию True)
      include_references  — включить входящие/исходящие связи (по умолчанию False,
                            используйте metadata_references_to/from для страниц)
      include_subsystems  — включить список подсистем (по умолчанию False)
      attributes_limit    — макс. реквизитов (1-200, по умолчанию 50)
    """
    _err = _graph_guard()
    if _err:
        return _err

    # Основные данные
    rows = _neo4j_rows("""
        MATCH (n:MetadataObject {full_name: $fn})
        RETURN n.full_name as full_name, n.kind as kind, n.name as name,
               n.synonym as synonym, n.kind_eng as kind_eng,
               n.attributes_json as attributes_json
    """, {"fn": full_name})

    if not rows:
        return json.dumps({"error": f"'{full_name}' не найден"}, ensure_ascii=False)

    obj = rows[0]
    response = {
        "full_name": obj["full_name"],
        "kind": obj.get("kind"),
        "name": obj.get("name"),
        "synonym": obj.get("synonym"),
        "kind_eng": obj.get("kind_eng"),
    }

    # Реквизиты (опционально + пагинация)
    if include_attributes:
        try:
            all_attrs = json.loads(obj.get("attributes_json", "[]") or "[]")
        except Exception:
            all_attrs = []

        limit = max(1, min(attributes_limit, 200))
        response["attributes"] = {
            "total": len(all_attrs),
            "returned": min(limit, len(all_attrs)),
            "items": all_attrs[:limit],
            "has_more": len(all_attrs) > limit,
        }
    else:
        # Только счётчик
        try:
            all_attrs = json.loads(obj.get("attributes_json", "[]") or "[]")
            response["attributes_count"] = len(all_attrs)
        except Exception:
            response["attributes_count"] = 0

    # Счётчики связей (всегда лёгкие)
    refs_in_count = _neo4j_count("""
        MATCH (n:MetadataObject {full_name: $fn})<-[r]-(m:MetadataObject)
        RETURN count(r)
    """, {"fn": full_name})
    refs_out_count = _neo4j_count("""
        MATCH (n:MetadataObject {full_name: $fn})-[r]->(m:MetadataObject)
        RETURN count(r)
    """, {"fn": full_name})

    response["references_in_count"] = refs_in_count
    response["references_out_count"] = refs_out_count

    # Полные связи (опционально, с ограничением 10 для превью)
    if include_references:
        out_rows = _neo4j_rows("""
            MATCH (n:MetadataObject {full_name: $fn})-[r]->(m:MetadataObject)
            RETURN type(r) as relation, r.context as context,
                   m.full_name as target, m.kind as target_kind
            LIMIT 10
        """, {"fn": full_name})
        in_rows = _neo4j_rows("""
            MATCH (n:MetadataObject {full_name: $fn})<-[r]-(m:MetadataObject)
            RETURN type(r) as relation, r.context as context,
                   m.full_name as source, m.kind as source_kind
            LIMIT 10
        """, {"fn": full_name})
        response["references_out_preview"] = out_rows
        response["references_in_preview"] = in_rows
        if refs_out_count > 10 or refs_in_count > 10:
            response["references_hint"] = (
                "Показаны только первые 10 связей каждого направления. "
                "Используйте metadata_references_from / metadata_references_to "
                "с параметрами limit/offset для полного списка."
            )

    # Подсистемы (опционально)
    if include_subsystems:
        sub_rows = _neo4j_rows("""
            MATCH (s:Подсистема)-[:СОДЕРЖИТ]->(n:MetadataObject {full_name: $fn})
            RETURN s.name as subsystem
        """, {"fn": full_name})
        response["subsystems"] = [s["subsystem"] for s in sub_rows]

    # Подсказки для следующих шагов
    hints = []
    if not include_attributes and response.get("attributes_count", 0) > 0:
        hints.append(f"Реквизитов: {response['attributes_count']}. "
                     "Вызовите с include_attributes=true для деталей.")
    if refs_in_count > 0 and not include_references:
        hints.append(f"Входящих связей: {refs_in_count}. "
                     "Используйте metadata_references_to для списка.")
    if refs_out_count > 0 and not include_references:
        hints.append(f"Исходящих связей: {refs_out_count}. "
                     "Используйте metadata_references_from для списка.")
    if hints:
        response["hints"] = hints

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_references_from(full_name: str, limit: int = 20, offset: int = 0) -> str:
    """
    На какие объекты ссылается данный (исходящие связи).

    Параметры:
      full_name — полное имя объекта
      limit     — макс. результатов (1-100, по умолчанию 20)
      offset    — смещение для пагинации
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    total = _neo4j_count("""
        MATCH (n:MetadataObject {full_name: $fn})-[r]->(m:MetadataObject)
        RETURN count(r)
    """, {"fn": full_name})

    rows = _neo4j_rows("""
        MATCH (n:MetadataObject {full_name: $fn})-[r]->(m:MetadataObject)
        RETURN type(r) as relation, r.context as context,
               m.full_name as target, m.kind as target_kind, m.synonym as target_synonym
        ORDER BY m.full_name
        SKIP $offset
        LIMIT $limit
    """, {"fn": full_name, "offset": p.offset, "limit": p.limit})

    end = p.offset + len(rows)
    return json.dumps({
        "object": full_name,
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "items": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_references_to(full_name: str, limit: int = 20, offset: int = 0) -> str:
    """
    Какие объекты ссылаются на данный (входящие связи).

    Параметры:
      full_name — полное имя объекта
      limit     — макс. результатов (1-100, по умолчанию 20)
      offset    — смещение для пагинации
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    total = _neo4j_count("""
        MATCH (n:MetadataObject {full_name: $fn})<-[r]-(m:MetadataObject)
        RETURN count(r)
    """, {"fn": full_name})

    rows = _neo4j_rows("""
        MATCH (n:MetadataObject {full_name: $fn})<-[r]-(m:MetadataObject)
        RETURN type(r) as relation, r.context as context,
               m.full_name as source, m.kind as source_kind, m.synonym as source_synonym
        ORDER BY m.full_name
        SKIP $offset
        LIMIT $limit
    """, {"fn": full_name, "offset": p.offset, "limit": p.limit})

    end = p.offset + len(rows)
    return json.dumps({
        "object": full_name,
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "items": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_dependency_tree(full_name: str, depth: int = 2, limit: int = 50) -> str:
    """
    Дерево зависимостей объекта.

    Параметры:
      full_name — полное имя объекта
      depth     — глубина (1-3, по умолчанию 2)
      limit     — макс. узлов в ответе (1-100, по умолчанию 50)
    """
    depth = max(1, min(depth, 3))
    limit = max(1, min(limit, 100))

    _err = _graph_guard()
    if _err:
        return _err

    # Сначала узнаём общее количество узлов в дереве
    total = _neo4j_count(f"""
        MATCH path = (n:MetadataObject {{full_name: $fn}})-[*1..{depth}]-(m:MetadataObject)
        RETURN count(distinct m)
    """, {"fn": full_name})

    rows = _neo4j_rows(f"""
        MATCH path = (n:MetadataObject {{full_name: $fn}})-[*1..{depth}]-(m:MetadataObject)
        WITH m, relationships(path) as rels, nodes(path) as nds
        RETURN DISTINCT m.full_name as connected_object, m.kind as kind,
               [r in rels | type(r)] as relations,
               [nd in nds | nd.full_name] as path
        LIMIT $limit
    """, {"fn": full_name, "limit": limit})

    return json.dumps({
        "object": full_name,
        "depth": depth,
        "total_distinct_nodes": total,
        "returned": len(rows),
        "limit": limit,
        "has_more": total > len(rows),
        "hint": ("Для больших деревьев используйте меньший depth или "
                 "работайте через metadata_references_from/to постранично")
                 if total > limit else None,
        "items": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@cached(ttl=600)
def metadata_list_kinds() -> str:
    """Список типов объектов в конфигурации (всегда лёгкий ответ)."""
    _err = _graph_guard()
    if _err:
        return _err
    rows = _neo4j_rows(
        "MATCH (n:MetadataObject) RETURN DISTINCT n.kind as kind, count(n) as count ORDER BY count DESC"
    )
    return json.dumps({
        "total_kinds": len(rows),
        "kinds": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@cached(ttl=600)
def metadata_list_objects(kind: str = "", limit: int = 50, offset: int = 0) -> str:
    """
    Список объектов метаданных.

    ВАЖНО: Без параметра kind и при большом количестве объектов возвращает
    только превью. Для полного обхода всегда указывайте kind ИЛИ используйте
    limit/offset постранично.

    Параметры:
      kind   — фильтр по типу ("Справочник", "Документ", ...)
      limit  — макс. результатов (1-100, по умолчанию 50)
      offset — смещение для пагинации
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    if kind:
        total = _neo4j_count("""
            MATCH (n:MetadataObject)
            WHERE toLower(n.kind) = toLower($kind) OR toLower(n.kind_eng) = toLower($kind)
            RETURN count(n)
        """, {"kind": kind})

        rows = _neo4j_rows("""
            MATCH (n:MetadataObject)
            WHERE toLower(n.kind) = toLower($kind) OR toLower(n.kind_eng) = toLower($kind)
            RETURN n.full_name as full_name, n.synonym as synonym
            ORDER BY n.full_name
            SKIP $offset
            LIMIT $limit
        """, {"kind": kind, "offset": p.offset, "limit": p.limit})
    else:
        total = _neo4j_count("MATCH (n:MetadataObject) RETURN count(n)")

        rows = _neo4j_rows("""
            MATCH (n:MetadataObject)
            RETURN n.full_name as full_name, n.synonym as synonym
            ORDER BY n.full_name
            SKIP $offset
            LIMIT $limit
        """, {"offset": p.offset, "limit": p.limit})

    end = p.offset + len(rows)
    response = {
        "kind_filter": kind or None,
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "items": rows,
    }
    if end < total:
        response["next_offset"] = end

    # Подсказка для больших списков
    if not kind and total > 100:
        response["hint"] = (
            f"В конфигурации {total} объектов. Рекомендуется фильтровать по kind "
            "(metadata_list_kinds покажет доступные типы) или использовать поиск "
            "через metadata_search."
        )

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_subsystems(limit: int = 30, offset: int = 0) -> str:
    """
    Подсистемы конфигурации и количество объектов в каждой.
    Полный состав подсистемы — отдельным вызовом metadata_subsystem_members.

    Параметры:
      limit  — макс. подсистем (1-100, по умолчанию 30)
      offset — смещение
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    total = _neo4j_count("MATCH (s:Подсистема) RETURN count(s)")

    rows = _neo4j_rows("""
        MATCH (s:Подсистема)
        OPTIONAL MATCH (s)-[:СОДЕРЖИТ]->(m:MetadataObject)
        WITH s, count(m) as members_count
        RETURN s.name as subsystem, members_count
        ORDER BY s.name
        SKIP $offset
        LIMIT $limit
    """, {"offset": p.offset, "limit": p.limit})

    end = p.offset + len(rows)
    return json.dumps({
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "items": rows,
        "hint": "Для состава конкретной подсистемы вызовите metadata_subsystem_members",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_subsystem_members(
    subsystem_name: str,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """
    Объекты, входящие в указанную подсистему (с пагинацией).

    Параметры:
      subsystem_name — имя подсистемы
      limit          — макс. результатов
      offset         — смещение
    """
    _err = _graph_guard()
    if _err:
        return _err

    p = PaginationParams(limit=limit, offset=offset)

    total = _neo4j_count("""
        MATCH (s:Подсистема {name: $name})-[:СОДЕРЖИТ]->(m:MetadataObject)
        RETURN count(m)
    """, {"name": subsystem_name})

    rows = _neo4j_rows("""
        MATCH (s:Подсистема {name: $name})-[:СОДЕРЖИТ]->(m:MetadataObject)
        RETURN m.full_name as full_name, m.kind as kind, m.synonym as synonym
        ORDER BY m.full_name
        SKIP $offset
        LIMIT $limit
    """, {"name": subsystem_name, "offset": p.offset, "limit": p.limit})

    end = p.offset + len(rows)
    return json.dumps({
        "subsystem": subsystem_name,
        "total": total,
        "returned": len(rows),
        "offset": p.offset,
        "limit": p.limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "items": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def metadata_cypher(query: str, limit: int = 50) -> str:
    """
    Выполнить произвольный Cypher-запрос к графу метаданных Neo4j.
    Только для чтения (MATCH).

    Параметры:
      query — Cypher-запрос, например "MATCH (n:Справочник) RETURN n.name"
      limit — защитный лимит если в запросе нет LIMIT (по умолчанию 50)
    """
    q_upper = query.upper().strip()
    forbidden = ["CREATE", "DELETE", "SET", "REMOVE", "MERGE", "DROP", "DETACH"]
    for word in forbidden:
        if word in q_upper:
            return json.dumps({
                "error": f"Запрос содержит запрещённую операцию: {word}. Только MATCH."
            }, ensure_ascii=False)

    # Добавляем защитный LIMIT если его нет в запросе
    if "LIMIT" not in q_upper:
        query = query.rstrip().rstrip(";") + f" LIMIT {max(1, min(limit, 200))}"

    _err = _graph_guard()
    if _err:
        return _err
    try:
        rows = _neo4j_rows(query)
        return json.dumps({
            "query": query,
            "returned": len(rows),
            "limit_applied": limit if "LIMIT" not in q_upper else "user-specified",
            "items": rows,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def metadata_reload() -> str:
    """Перезагрузить метаданные (пересоздаёт граф в Neo4j)."""
    # FIX-3: очистка осмысленна и на пустом графе — блокируем только тогда,
    # когда Neo4j реально недоступна.
    state, detail = _graph_state()
    if state == GRAPH_UNAVAILABLE:
        return _graph_error(state, detail)
    _neo4j_query("MATCH (n) DETACH DELETE n")
    return json.dumps({
        "status": "Граф очищен. Перезапустите metadata-indexer для переиндексации."
    }, ensure_ascii=False)

# ─── v3: новые tools поверх расширенного XML-графа (задача 4.6.1) ────────
# Регистрируем ниже, чтобы старые tools оставались как есть — это позволяет
# тестировать v3 рядом с v2 и легко откатывать при необходимости.
try:
    from server_v3_tools import register_v3_tools
    register_v3_tools(mcp, _neo4j_query, _neo4j_rows, _neo4j_count,
                      _neo4j_available, _graph_guard)   # FIX-3
    from server_v3_code_tools import register_v3_code_tools
    register_v3_code_tools(mcp, _neo4j_query, _neo4j_rows, _neo4j_count,
                           _neo4j_available, _graph_guard)   # FIX-3
    # 4.6.5: инкрементальный апдейт графа для workspace-watcher.
    from server_v3_watch_tools import register_v3_watch_tools
    register_v3_watch_tools(mcp, SRC_DIR, NEO4J_URL, NEO4J_USER, NEO4J_PASS,
                            _neo4j_available, _graph_state)   # FIX-3
    logger.info("v3 tools (XML-graph) зарегистрированы")
except ImportError as e:
    logger.warning("v3 tools недоступны: %s. "
                   "Убедитесь, что server_v3_tools.py скопирован в /app", e)
except Exception as e:
    logger.error("Ошибка регистрации v3 tools: %s", e)