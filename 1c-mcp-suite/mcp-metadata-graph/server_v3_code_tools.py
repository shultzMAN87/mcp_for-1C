"""
MCP-tools для слоя 2 графа метаданных (call graph, 4.6.2).
==============================================================

Подгружается из server.py в конце файла:

    from server_v3_code_tools import register_v3_code_tools
    register_v3_code_tools(mcp, _neo4j_query, _neo4j_rows, _neo4j_count, _neo4j_available)

Где `mcp` — экземпляр FastMCP, а `_neo4j_*` — функции, уже определённые в server.py.

Новые tools (8):
  code_callers                       — кто зовёт процедуру
  code_callees                       — кого зовёт процедура
  code_call_path                     — путь вызовов между двумя процедурами
  code_procedures_operating_on       — процедуры, работающие со справочником/документом/etc
  code_dead_procedures               — процедуры без входящих :CALLS (с фильтром обработчиков)
  code_method_signature              — параметры процедуры
  code_unresolved_callsites          — диагностика покрытия резолва
  code_v3_stats                      — статистика слоя 2

Стиль регистрации копирует server_v3_tools.py (4.6.1).
"""
from __future__ import annotations

import json
from typing import Callable, Optional


# Стандартные имена обработчиков формы — для exclude_handlers в code_dead_procedures.
# Дубль из bsl_resolver.FORM_HANDLERS; держим локально, чтобы server-side файл не
# зависел от indexer-side кода.
_FORM_HANDLERS_DEFAULT = {
    "ПриСозданииНаСервере", "ПриОткрытии", "ПередЗакрытием", "ПередЗаписью",
    "ПриЗаписи", "ПередЗаписьюНаСервере", "ПриЗаписиНаСервере",
    "ПослеЗаписиНаСервере", "ПослеЗаписи",
    "ОбработкаПроверкиЗаполнения", "ОбработкаПроверкиЗаполненияНаСервере",
    "ОбработкаВыбора", "ОбработкаОповещения",
    "ПриЧтенииНаСервере", "ПриЗагрузкеИзНастроекНаСервере",
    "ПриСохраненииДанныхВНастройкахНаСервере",
    "ПриПолученииДанныхНаСервере",
    "ПриАктивизацииСтроки", "ПриАктивизацииЯчейки",
    "ПриИзменении", "ПриВыборе", "ПередНачаломДобавления",
    "АвтоПодборЗначения", "Очистка", "НачалоВыбора", "ОкончаниеВводаТекста",
    "Регулирование", "ОбработкаКомандыФормы",
    "ПередЗаписью_Справочник", "ПриЗаписи_Справочник",
    "Справочник_ПриСозданииНаСервере", "Справочник_ПриОткрытии",
    "Справочник_ПередЗакрытием", "Справочник_ПослеСозданияНаСервере",
    "Справочник_ПослеОткрытия",
    "ИсполняемыеСценарии",
}

# FIX-4 / FIX-4.1: причины «не пробел в графе» — единый источник в
# graph_state.py (модуль без зависимости от FastMCP, лежит рядом в /app).
# Раньше константа дублировалась здесь и в server.py с пометкой «править оба
# места»; теперь копия одна — server-side, парная к NON_CONFIG_CALL_REASONS
# в indexer-side bsl_resolver.py.
from graph_state import (  # noqa: E402
    NON_CONFIG_CALL_REASONS as _NON_CONFIG_CALL_REASONS,
    NON_CONFIG_REASONS_CYPHER as _NON_CONFIG_REASONS_CYPHER,
)

def register_v3_code_tools(mcp, neo4j_query: Callable, neo4j_rows: Callable,
                           neo4j_count: Callable, neo4j_available: Callable,
                           graph_guard: Optional[Callable] = None) -> None:

    def _guard():
        """FIX-3: None если граф готов, иначе готовый JSON с диагностикой.

        `graph_guard` приходит из server.py и различает «Neo4j недоступна»
        и «граф пуст». Фолбэк на `neo4j_available` оставлен для случая, когда
        модуль импортируют отдельно (тесты, отладка) — тогда поведение как
        до FIX-3.
        """
        if graph_guard is not None:
            return graph_guard()
        if neo4j_available():
            return None
        return json.dumps({"error": "Neo4j недоступен"}, ensure_ascii=False)


    def _resolve_callable_id(name_or_id: str) -> Optional[str]:
        """
        Резолвит вход (id, full_name или name) в callable.id.
        Стратегия: 1) точное совпадение по id/full_name; 2) по name (если уникально).
        Возвращает id или None.
        """
        rows = neo4j_rows(
            "MATCH (c:Callable) "
            "WHERE c.id = $q OR c.full_name = $q "
            "RETURN c.id AS id LIMIT 1",
            {"q": name_or_id},
        )
        if rows:
            return rows[0]["id"]
        # Fallback: по name (если ровно одно совпадение)
        rows = neo4j_rows(
            "MATCH (c:Callable {name: $q}) RETURN c.id AS id LIMIT 2",
            {"q": name_or_id},
        )
        if len(rows) == 1:
            return rows[0]["id"]
        return None

    def _resolve_metadata_id(name_or_id: str) -> Optional[str]:
        """
        Резолвит вход в :MetadataObject.id (full_name_eng).
        Поддерживает: id, full_name_eng, full_name_ru, name.
        """
        rows = neo4j_rows(
            "MATCH (m:MetadataObject) "
            "WHERE m.id = $q OR m.full_name_eng = $q OR m.full_name_ru = $q "
            # FIX-18: единственное число вида — «Справочник.X».
            "   OR (m.kind_ru + '.' + m.name) = $q "
            "RETURN m.id AS id LIMIT 1",
            {"q": name_or_id},
        )
        if rows:
            return rows[0]["id"]
        rows = neo4j_rows(
            "MATCH (m:MetadataObject {name: $q}) RETURN m.id AS id LIMIT 2",
            {"q": name_or_id},
        )
        if len(rows) == 1:
            return rows[0]["id"]
        return None

    def _clamp(value: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, value))

    # ─── code_callers ─────────────────────────────────────────────────────

    @mcp.tool()
    def code_callers(full_name: str, depth: int = 1, limit: int = 100) -> str:
        """
        Возвращает список процедур/функций, которые (транзитивно) вызывают заданную.

        Параметры:
          full_name  — id, full_name или имя callable (например "CommonModule.АукОбщийКлиент.Факториал")
          depth      — макс. глубина обхода CALLS (1..10, по умолчанию 1)
          limit      — макс. число результатов (1..500, по умолчанию 100)

        Возвращает JSON:
          {
            "found": true,
            "target": "<callable.id>",
            "depth": 1,
            "callers": [
              {"id": "...", "full_name": "...", "module_id": "...", "kind": "Procedure",
               "is_export": true, "min_distance": 1}
            ],
            "total": N
          }
        """
        _err = _guard()
        if _err:
            return _err
        depth = _clamp(depth, 1, 10)
        limit = _clamp(limit, 1, 500)

        target_id = _resolve_callable_id(full_name)
        if not target_id:
            return json.dumps({"found": False,
                               "error": f"Callable '{full_name}' не найден"},
                              ensure_ascii=False)

        cypher = (
            f"MATCH p = shortestPath((caller:Callable)-[:CALLS*1..{depth}]->(t:Callable {{id: $tid}})) "
            "WHERE caller <> t "
            "RETURN caller.id AS id, caller.full_name AS full_name, "
            "       caller.module_id AS module_id, caller.kind AS kind, "
            "       caller.is_export AS is_export, length(p) AS min_distance "
            "ORDER BY min_distance, caller.full_name "
            f"LIMIT {limit}"
        )
        rows = neo4j_rows(cypher, {"tid": target_id})
        return json.dumps({
            "found": True,
            "target": target_id,
            "depth": depth,
            "callers": rows,
            "total": len(rows),
        }, ensure_ascii=False)

    # ─── code_callees ─────────────────────────────────────────────────────

    @mcp.tool()
    def code_callees(full_name: str, depth: int = 1, limit: int = 100) -> str:
        """
        Возвращает список процедур/функций, которые (транзитивно) вызываются из заданной.

        Параметры:
          full_name  — id, full_name или имя callable
          depth      — макс. глубина обхода CALLS (1..10)
          limit      — макс. число результатов (1..500)

        Возвращает JSON:
          {
            "found": true,
            "source": "<callable.id>",
            "depth": 1,
            "callees": [...]
          }
        """
        _err = _guard()
        if _err:
            return _err
        depth = _clamp(depth, 1, 10)
        limit = _clamp(limit, 1, 500)

        src_id = _resolve_callable_id(full_name)
        if not src_id:
            return json.dumps({"found": False,
                               "error": f"Callable '{full_name}' не найден"},
                              ensure_ascii=False)

        cypher = (
            f"MATCH p = shortestPath((s:Callable {{id: $sid}})-[:CALLS*1..{depth}]->(callee:Callable)) "
            "WHERE callee <> s "
            "RETURN callee.id AS id, callee.full_name AS full_name, "
            "       callee.module_id AS module_id, callee.kind AS kind, "
            "       callee.is_export AS is_export, length(p) AS min_distance "
            "ORDER BY min_distance, callee.full_name "
            f"LIMIT {limit}"
        )
        rows = neo4j_rows(cypher, {"sid": src_id})
        return json.dumps({
            "found": True,
            "source": src_id,
            "depth": depth,
            "callees": rows,
            "total": len(rows),
        }, ensure_ascii=False)

    # ─── code_call_path ───────────────────────────────────────────────────

    @mcp.tool()
    def code_call_path(from_full_name: str, to_full_name: str,
                       max_depth: int = 5) -> str:
        """
        Кратчайший путь вызовов от одной процедуры к другой через :CALLS.

        Параметры:
          from_full_name — id/full_name/name source
          to_full_name   — id/full_name/name target
          max_depth      — макс. длина пути (1..10, по умолчанию 5)

        Возвращает JSON:
          {
            "found": true,
            "from": "...", "to": "...", "length": 3,
            "path": [
              {"id": "...", "full_name": "...", "kind": "Procedure"},  # 4 узла на пути длины 3
              ...
            ]
          }
        Если пути нет: {"found": true, "length": null, "path": []}
        """
        _err = _guard()
        if _err:
            return _err
        max_depth = _clamp(max_depth, 1, 10)

        from_id = _resolve_callable_id(from_full_name)
        if not from_id:
            return json.dumps({"found": False,
                               "error": f"Source '{from_full_name}' не найден"},
                              ensure_ascii=False)
        to_id = _resolve_callable_id(to_full_name)
        if not to_id:
            return json.dumps({"found": False,
                               "error": f"Target '{to_full_name}' не найден"},
                              ensure_ascii=False)

        cypher = (
            f"MATCH p = shortestPath((a:Callable {{id: $a}})-[:CALLS*1..{max_depth}]->(b:Callable {{id: $b}})) "
            "RETURN length(p) AS len, "
            # FIX-17: full_name_eng/kind_ru — см. search_fulltext. Узлы на пути
            # могут быть и :Callable (у них есть full_name), и :MetadataObject
            # (у которых его нет), поэтому coalesce по обоим вариантам.
            "       [n IN nodes(p) | {id: n.id, "
            "         full_name: coalesce(n.full_name, n.full_name_eng), "
            "         kind: coalesce(n.kind_ru, n.kind)}] AS path"
        )
        rows = neo4j_rows(cypher, {"a": from_id, "b": to_id})

        if not rows:
            return json.dumps({
                "found": True, "from": from_id, "to": to_id,
                "length": None, "path": [],
                "note": "Путь не найден в пределах max_depth",
            }, ensure_ascii=False)

        return json.dumps({
            "found": True,
            "from": from_id, "to": to_id,
            "length": rows[0]["len"],
            "path": rows[0]["path"],
        }, ensure_ascii=False)

    # ─── code_procedures_operating_on ─────────────────────────────────────

    @mcp.tool()
    def code_procedures_operating_on(metadata_full_name: str,
                                     via: Optional[str] = None,
                                     limit: int = 100) -> str:
        """
        Процедуры/функции, которые работают со справочником/документом/регистром/перечислением.

        Параметры:
          metadata_full_name — id/full_name_eng/full_name_ru/name объекта метаданных
          via                — необязательный фильтр по способу обращения:
                                'Справочники' / 'Документы' / 'Перечисления' / ...
                                'predefined_value' — только через ПредопределенноеЗначение
          limit              — макс. число результатов (1..500)

        Возвращает JSON:
          {
            "found": true,
            "metadata_object": "Catalog.АукАукционы",
            "callables": [
              {"id": "...", "full_name": "...", "kind": "Procedure",
               "module_id": "...", "via": "Справочники", "access": "manager_collection"}
            ],
            "total": N
          }
        """
        _err = _guard()
        if _err:
            return _err
        limit = _clamp(limit, 1, 500)

        meta_id = _resolve_metadata_id(metadata_full_name)
        if not meta_id:
            return json.dumps({"found": False,
                               "error": f"Объект '{metadata_full_name}' не найден"},
                              ensure_ascii=False)

        where_via = ""
        params = {"mid": meta_id, "limit": limit}
        if via:
            where_via = "AND r.via = $via "
            params["via"] = via

        cypher = (
            "MATCH (c:Callable)-[r:OPERATES_ON]->(m:MetadataObject {id: $mid}) "
            f"{where_via}"
            "RETURN c.id AS id, c.full_name AS full_name, c.module_id AS module_id, "
            "       c.kind AS kind, r.via AS via, r.access AS access "
            "ORDER BY c.full_name "
            "LIMIT $limit"
        )
        rows = neo4j_rows(cypher, params)
        return json.dumps({
            "found": True,
            "metadata_object": meta_id,
            "callables": rows,
            "total": len(rows),
        }, ensure_ascii=False)

    # ─── code_dead_procedures ─────────────────────────────────────────────

    @mcp.tool()
    def code_dead_procedures(module_id: Optional[str] = None,
                             exclude_handlers: bool = True,
                             include_exports: bool = False,
                             limit: int = 100) -> str:
        """
        Процедуры/функции, у которых нет входящих :CALLS-рёбер. Это «статически
        мёртвый» код. На практике многие из них зовутся платформой 1С (обработчики
        формы, подписки на события), поэтому по умолчанию они исключены через имена.

        Параметры:
          module_id         — фильтр по module_id (например "Catalog.АукАукционы.ObjectModule").
                              По умолчанию — все модули.
          exclude_handlers  — исключить стандартные обработчики (по умолчанию True)
          include_exports   — включать ли экспортные процедуры (по умолчанию False, т.к.
                              экспортные могут зваться извне через resolved=False callsite'ы
                              или динамически)
          limit             — макс. число результатов (1..500)

        Возвращает JSON:
          {
            "found": true,
            "module_id": null | "...",
            "exclude_handlers": true,
            "dead": [
              {"id": "...", "full_name": "...", "kind": "Procedure", "module_id": "..."}
            ],
            "total": N
          }
        """
        _err = _guard()
        if _err:
            return _err
        limit = _clamp(limit, 1, 500)

        clauses = ["NOT EXISTS { MATCH ()-[:CALLS]->(c) }"]
        params: dict = {"limit": limit}
        if module_id:
            clauses.append("c.module_id = $mid")
            params["mid"] = module_id
        if not include_exports:
            clauses.append("(c.is_export IS NULL OR c.is_export = false)")
        if exclude_handlers:
            handlers = sorted(_FORM_HANDLERS_DEFAULT)
            clauses.append("NOT c.name IN $handlers")
            params["handlers"] = handlers

        where = " AND ".join(clauses)
        cypher = (
            "MATCH (c:Callable) "
            f"WHERE {where} "
            "RETURN c.id AS id, c.full_name AS full_name, c.module_id AS module_id, "
            "       c.kind AS kind, c.directive AS directive, c.line_start AS line_start "
            "ORDER BY c.module_id, c.full_name "
            "LIMIT $limit"
        )
        rows = neo4j_rows(cypher, params)
        return json.dumps({
            "found": True,
            "module_id": module_id,
            "exclude_handlers": exclude_handlers,
            "include_exports": include_exports,
            "dead": rows,
            "total": len(rows),
        }, ensure_ascii=False)

    # ─── code_method_signature ────────────────────────────────────────────

    @mcp.tool()
    def code_method_signature(full_name: str) -> str:
        """
        Сигнатура процедуры/функции: имя, тип, директива, экспортность, параметры.

        Параметры:
          full_name — id/full_name/name callable

        Возвращает JSON:
          {
            "found": true,
            "callable": {
              "id": "...", "full_name": "...", "name": "...", "kind": "Function",
              "module_id": "...", "is_export": true, "directive": "НаКлиенте",
              "line_start": 10, "line_end": 25, "source_path": "..."
            },
            "parameters": [
              {"name": "пСтк", "position": 0, "is_by_value": false,
               "has_default": false, "default_value": ""}
            ]
          }
        """
        _err = _guard()
        if _err:
            return _err
        cid = _resolve_callable_id(full_name)
        if not cid:
            return json.dumps({"found": False,
                               "error": f"Callable '{full_name}' не найден"},
                              ensure_ascii=False)

        rows = neo4j_rows(
            "MATCH (c:Callable {id: $id}) "
            "RETURN c.id AS id, c.full_name AS full_name, c.name AS name, "
            "       c.kind AS kind, c.module_id AS module_id, c.is_export AS is_export, "
            "       c.directive AS directive, c.line_start AS line_start, "
            "       c.line_end AS line_end, c.source_path AS source_path",
            {"id": cid},
        )
        callable_info = rows[0] if rows else {}

        params = neo4j_rows(
            "MATCH (c:Callable {id: $id})-[:HAS_PARAM]->(p:Parameter) "
            "RETURN p.name AS name, p.position AS position, p.is_by_value AS is_by_value, "
            "       p.has_default AS has_default, p.default_value AS default_value "
            "ORDER BY p.position",
            {"id": cid},
        )

        return json.dumps({
            "found": True,
            "callable": callable_info,
            "parameters": params,
        }, ensure_ascii=False)

    # ─── code_unresolved_callsites ────────────────────────────────────────

    @mcp.tool()
    def code_unresolved_callsites(module_id: Optional[str] = None,
                                  reason: Optional[str] = None,
                                  include_object_methods: bool = False,
                                  limit: int = 50) -> str:
        """
        Настоящие пробелы в графе вызовов: callsite'ы, которые похожи на
        обращение к коду конфигурации, но адресата резолвер не нашёл.
        Инструмент аудита — что мешает поднять покрытие резолва.

        По умолчанию НЕ показывает вызовы методов объектов платформы
        (`РезультатЗапроса.Выбрать()`, `Выборка.Следующий()`): слева от точки
        там объявленная в процедуре переменная, :Callable-адресата у такого
        вызова нет и быть не может. До FIX-4 они составляли 861 запись из 913
        и топили в шуме настоящие пробелы.

        Параметры:
          module_id              — фильтр по module_id вызывающей процедуры
          reason                 — фильтр по конкретной причине; задан явно —
                                   работает и для 'object_method'
          include_object_methods — вернуть и вызовы методов объектов (полная
                                   картина, как до FIX-4)
          limit                  — макс. число результатов (1..500)

        Причины настоящих пробелов:
          unknown_module                — модуль с таким именем не найден
          unknown_local_method          — метод не найден в своём же модуле
          unknown_method_in_common_module — модуль найден, метода в нём нет
          method_not_in_resolved_module — тип переменной выведен, метода нет
          stale_after_incremental       — срез устарел после инкрементального
                                          апдейта, ждёт полного reindex

        Причины «не пробел» (скрыты без include_object_methods):
          object_method                 — module_ref объявлен как переменная
          context_property              — свойство контекста модуля (`Ссылка`,
                                          `Параметры`) или реквизит формы
          platform_global               — глобальный объект платформы
                                          (`Метаданные`, `ФоновыеЗадания`)
          collection_unknown_method     — метод на переменной-коллекции
          dataflow_kind_no_module_role  — у выведенного типа нет модуля

        Возвращает JSON:
          {
            "found": true,
            "callsites": [
              {"caller_id": "...", "caller_name": "...",
               "module_ref": "ОбщегоНазначения", "method_name": "ЗначениеРеквизита",
               "line": 35, "col": 4, "reason": "unknown_module"}
            ],
            "returned": N,                        # сколько отдано (≤ limit)
            "total": N,                           # сколько всего подходит
            "reason_counts": {"unknown_module": 50, ...},   # по ВСЕЙ выборке
            "object_methods_hidden": N            # скрыто этим фильтром
          }
        """
        _err = _guard()
        if _err:
            return _err
        limit = _clamp(limit, 1, 500)

        clauses = ["cs.resolved = false"]
        params: dict = {"limit": limit}
        if module_id:
            clauses.append("c.module_id = $mid")
            params["mid"] = module_id
        if reason:
            # Явно запрошенная причина сильнее умолчания: если спросили
            # 'object_method' — отдаём его, не поднимая флаг.
            clauses.append("cs.reason = $reason")
            params["reason"] = reason
        elif not include_object_methods:
            clauses.append(f"NOT cs.reason IN {_NON_CONFIG_REASONS_CYPHER}")

        where = " AND ".join(clauses)
        base = f"MATCH (c:Callable)-[:CALL_SITE]->(cs:CallSite) WHERE {where} "

        rows = neo4j_rows(
            base +
            "RETURN c.id AS caller_id, c.full_name AS caller_name, "
            "       cs.module_ref AS module_ref, cs.method_name AS method_name, "
            "       cs.line AS line, cs.col AS col, cs.reason AS reason "
            "ORDER BY c.full_name, cs.line "
            "LIMIT $limit",
            params,
        )

        # reason_counts и total — по всей выборке, а не по странице:
        # иначе при limit=50 картина причин зависит от сортировки, и по ней
        # нельзя судить, что чинить в первую очередь.
        count_params = {k: v for k, v in params.items() if k != "limit"}
        agg = neo4j_rows(
            base + "RETURN cs.reason AS reason, count(cs) AS n ORDER BY n DESC",
            count_params,
        )
        reason_counts = {r["reason"]: r["n"] for r in agg}
        total = sum(reason_counts.values())

        # Сколько отсечено фильтром — чтобы агент видел, что картина неполная.
        hidden_clauses = ["cs.resolved = false",
                          f"cs.reason IN {_NON_CONFIG_REASONS_CYPHER}"]
        hidden_params: dict = {}
        if module_id:
            hidden_clauses.append("c.module_id = $mid")
            hidden_params["mid"] = module_id
        hidden = neo4j_count(
            "MATCH (c:Callable)-[:CALL_SITE]->(cs:CallSite) "
            f"WHERE {' AND '.join(hidden_clauses)} RETURN count(cs) AS total",
            hidden_params,
        )

        return json.dumps({
            "found": True,
            "callsites": rows,
            "returned": len(rows),
            "total": total,
            "reason_counts": reason_counts,
            "object_methods_hidden": 0 if (reason or include_object_methods) else hidden,
        }, ensure_ascii=False)

    # ─── code_v3_stats ────────────────────────────────────────────────────

    @mcp.tool()
    def code_v3_stats() -> str:
        """
        Расширенная статистика слоя 2 (call graph): размеры, покрытие резолва,
        распределение по типам, топ горячих узлов.

        Возвращает JSON:
          {
            "found": true,
            "nodes": {"Module": N, "Callable": N, "Procedure": N, "Function": N,
                      "Parameter": N, "CallSite": N},
            "edges": {"HAS_METHOD": N, "HAS_PARAM": N, "CALL_SITE": N,
                      "CALLS": N, "RESOLVES_TO_CALLEE": N, "OPERATES_ON": N},
            "callsites": {"resolved": N, "unresolved": N, "object_method": N,
                          "coverage_pct": 91.4},
            "operates_on_by_kind": [{"kind": "Catalog", "n": N}, ...],
            "top_hot_callees": [{"id": "...", "full_name": "...", "callers": N}],
            "top_hot_callers": [{"id": "...", "full_name": "...", "callees": N}],
            "unresolved_reasons": [{"reason": "unknown_module", "n": N}, ...]
          }
        """
        _err = _guard()
        if _err:
            return _err

        nodes = {
            "Module":    neo4j_count("MATCH (n:Module) RETURN count(n) AS total"),
            "Callable":  neo4j_count("MATCH (n:Callable) RETURN count(n) AS total"),
            "Procedure": neo4j_count("MATCH (n:Procedure) RETURN count(n) AS total"),
            "Function":  neo4j_count("MATCH (n:Function) RETURN count(n) AS total"),
            "Parameter": neo4j_count("MATCH (n:Parameter) RETURN count(n) AS total"),
            "CallSite":  neo4j_count("MATCH (n:CallSite) RETURN count(n) AS total"),
        }
        edges = {
            "HAS_METHOD":         neo4j_count("MATCH ()-[r:HAS_METHOD]->() RETURN count(r) AS total"),
            "HAS_PARAM":          neo4j_count("MATCH ()-[r:HAS_PARAM]->() RETURN count(r) AS total"),
            "CALL_SITE":          neo4j_count("MATCH ()-[r:CALL_SITE]->() RETURN count(r) AS total"),
            "CALLS":              neo4j_count("MATCH ()-[r:CALLS]->() RETURN count(r) AS total"),
            "RESOLVES_TO_CALLEE": neo4j_count("MATCH ()-[r:RESOLVES_TO_CALLEE]->() RETURN count(r) AS total"),
            "OPERATES_ON":        neo4j_count("MATCH ()-[r:OPERATES_ON]->() RETURN count(r) AS total"),
        }

        cs_resolved = neo4j_count("MATCH (cs:CallSite {resolved: true}) RETURN count(cs) AS total")
        cs_unresolved = neo4j_count("MATCH (cs:CallSite {resolved: false}) RETURN count(cs) AS total")
        # FIX-4: методы объектов платформы — не пробел в графе, из знаменателя
        # покрытия исключаются (см. code_unresolved_callsites).
        cs_object_method = neo4j_count(
            "MATCH (cs:CallSite) WHERE cs.resolved = false "
            f"AND cs.reason IN {_NON_CONFIG_REASONS_CYPHER} "
            "RETURN count(cs) AS total"
        )
        cs_gaps = cs_unresolved - cs_object_method
        cs_denom = cs_resolved + cs_gaps
        coverage_pct = round(100.0 * cs_resolved / cs_denom, 2) if cs_denom else 0.0

        operates_on_by_kind = neo4j_rows(
            "MATCH (c:Callable)-[r:OPERATES_ON]->(m:MetadataObject) "
            "RETURN m.kind_eng AS kind, count(r) AS n ORDER BY n DESC"
        )

        top_callees = neo4j_rows(
            "MATCH (c:Callable)<-[:CALLS]-(a:Callable) "
            "RETURN c.id AS id, c.full_name AS full_name, count(a) AS callers "
            "ORDER BY callers DESC LIMIT 10"
        )

        top_callers = neo4j_rows(
            "MATCH (a:Callable)-[:CALLS]->(b:Callable) "
            "RETURN a.id AS id, a.full_name AS full_name, count(b) AS callees "
            "ORDER BY callees DESC LIMIT 10"
        )

        unresolved_reasons = neo4j_rows(
            "MATCH (cs:CallSite {resolved: false}) "
            "RETURN cs.reason AS reason, count(cs) AS n "
            "ORDER BY n DESC LIMIT 10"
        )

        return json.dumps({
            "found": True,
            "nodes": nodes,
            "edges": edges,
            "callsites": {
                "resolved": cs_resolved,
                "unresolved": cs_gaps,
                "object_method": cs_object_method,
                "coverage_pct": coverage_pct,
            },
            "operates_on_by_kind": operates_on_by_kind,
            "top_hot_callees": top_callees,
            "top_hot_callers": top_callers,
            "unresolved_reasons": unresolved_reasons,
        }, ensure_ascii=False)
