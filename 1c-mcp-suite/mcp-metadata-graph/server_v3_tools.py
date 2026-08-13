"""
Дополнительные MCP-tools для двухслойного графа метаданных (v3).
================================================================

Подгружается из server.py в конце файла:

    from server_v3_tools import register_v3_tools
    register_v3_tools(mcp, _neo4j_query, _neo4j_rows, _neo4j_count, _neo4j_available)

Где `mcp` — экземпляр FastMCP, а `_neo4j_*` — функции, уже определённые в server.py.

Старые tools (`metadata_search`, `metadata_object_details`, ...) не меняются.

Новые tools:
  metadata_attribute_type            тип реквизита (с резолвом ссылочных типов)
  metadata_find_link_path            маршрут связи между двумя объектами
  metadata_referrers                 кто ссылается на объект
  metadata_object_attributes         реквизиты + измерения + ресурсы (по role)
  metadata_subsystem_tree            дерево подсистем (PARENT_OF)
  metadata_dead                      объекты, не входящие ни в одну подсистему
  metadata_v3_stats                  расширенная статистика по новой схеме
"""
from __future__ import annotations

import json
from typing import Callable, Optional


def register_v3_tools(mcp, neo4j_query: Callable, neo4j_rows: Callable,
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


    # ─── metadata_attribute_type ─────────────────────────────────────────

    @mcp.tool()
    def metadata_attribute_type(object_full_name: str, attribute_name: str) -> str:
        """
        Тип реквизита (включая роли — реквизит/измерение/ресурс) с резолвом
        ссылочных типов в имена объектов-целей.

        Параметры:
          object_full_name — полное имя объекта, англ. или рус. ("Catalog.X" или "Справочники.X")
          attribute_name   — имя реквизита

        Возвращает JSON:
          {
            "found": true,
            "object": "Catalog.X",
            "attribute": "Y",
            "role": "attribute" | "dimension" | "resource",
            "is_master": false,
            "types": [
              {"kind": "CatalogRef", "target": "Catalog.Z", "resolved": true},
              {"kind": "String", "target": null, "resolved": false}
            ]
          }
        """
        _err = _guard()
        if _err:
            return _err

        # Резолв object_full_name в id (поле id у :MetadataObject = full_name_eng)
        rows = neo4j_rows(
            "MATCH (m:MetadataObject) "
            "WHERE m.full_name_eng = $fn OR m.full_name_ru = $fn OR m.id = $fn "
            # FIX-18: единственное число вида — «Справочник.X».
            "   OR (m.kind_ru + '.' + m.name) = $fn "
            "RETURN m.id AS id, m.full_name_eng AS fne LIMIT 1",
            {"fn": object_full_name},
        )
        if not rows:
            return json.dumps({"found": False, "error": f"Объект '{object_full_name}' не найден"},
                              ensure_ascii=False)
        obj_id = rows[0]["id"]

        # Атрибут
        attr_rows = neo4j_rows(
            "MATCH (m:MetadataObject {id: $oid})-[r:HAS_ATTRIBUTE]->(a:Attribute) "
            "WHERE a.name = $an "
            "RETURN a.id AS aid, a.role AS role, a.is_master AS is_master, "
            "       a.synonym AS synonym, a.indexing AS indexing "
            "LIMIT 1",
            {"oid": obj_id, "an": attribute_name},
        )
        if not attr_rows:
            # Может быть в ТЧ — попробуем
            ts_rows = neo4j_rows(
                "MATCH (m:MetadataObject {id: $oid})-[:HAS_TABULAR_SECTION]->(ts:TabularSection) "
                "      -[r:HAS_ATTRIBUTE]->(a:Attribute) "
                "WHERE a.name = $an "
                "RETURN a.id AS aid, ts.name AS ts_name, a.role AS role, "
                "       a.synonym AS synonym, a.indexing AS indexing "
                "LIMIT 1",
                {"oid": obj_id, "an": attribute_name},
            )
            if not ts_rows:
                return json.dumps({
                    "found": False,
                    "error": f"Реквизит '{attribute_name}' не найден ни на объекте, ни в его ТЧ"
                }, ensure_ascii=False)
            attr_row = ts_rows[0]
            attr_row["is_master"] = False
            in_ts = ts_rows[0]["ts_name"]
        else:
            attr_row = attr_rows[0]
            in_ts = None

        # Типы
        type_rows = neo4j_rows(
            "MATCH (a:Attribute {id: $aid})-[:OF_TYPE]->(t:Type) "
            "OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject) "
            "RETURN t.kind AS kind, t.target AS target, o.full_name_eng AS resolved_eng, "
            "       o.full_name_ru AS resolved_ru, o.synonym AS resolved_synonym",
            {"aid": attr_row["aid"]},
        )
        types = [{
            "kind":              r["kind"],
            "target":            r["target"],
            "resolved":          r["resolved_eng"] is not None,
            "target_full_name_eng": r["resolved_eng"],
            "target_full_name_ru":  r["resolved_ru"],
            "target_synonym":    r["resolved_synonym"],
        } for r in type_rows]

        return json.dumps({
            "found":     True,
            "object":    obj_id,
            "attribute": attribute_name,
            "in_tabular_section": in_ts,
            "role":      attr_row.get("role", "attribute"),
            "is_master": attr_row.get("is_master", False),
            "synonym":   attr_row.get("synonym", ""),
            "indexing":  attr_row.get("indexing", ""),
            "types":     types,
        }, ensure_ascii=False, indent=2)

    # ─── metadata_find_link_path ─────────────────────────────────────────

    @mcp.tool()
    def metadata_find_link_path(from_object: str, to_object: str,
                                max_depth: int = 4, limit: int = 10) -> str:
        """
        Найти пути связи между двумя объектами метаданных через реквизиты-ссылки.

        Полезно для написания запросов:
        «Как соединить Справочник.АукАукционы и Справочник.АукВидыАукционов?»
        → агент видит, что у АукАукционы есть реквизит ВидАукциона типа
          CatalogRef.АукВидыАукционов, и сразу пишет INNER JOIN.

        Параметры:
          from_object — полное имя объекта-источника
          to_object   — полное имя объекта-цели
          max_depth   — максимальная длина пути в рёбрах графа (по умолчанию 4)
          limit       — максимум путей в ответе
        """
        _err = _guard()
        if _err:
            return _err
        # Прямой путь: HAS_ATTRIBUTE → OF_TYPE → RESOLVES_TO  (длина 3)
        # Через ТЧ:    HAS_TABULAR_SECTION → HAS_ATTRIBUTE → OF_TYPE → RESOLVES_TO  (4)
        # Обратный:    то же, но stack начинается с to_object и идёт обратно
        rows = neo4j_rows(
            f"""
            MATCH (a:MetadataObject), (b:MetadataObject)
            WHERE (a.full_name_eng = $a OR a.full_name_ru = $a OR a.id = $a
                   OR (a.kind_ru + '.' + a.name) = $a)
              AND (b.full_name_eng = $b OR b.full_name_ru = $b OR b.id = $b
                   OR (b.kind_ru + '.' + b.name) = $b)
            WITH a, b LIMIT 1
            MATCH p = shortestPath((a)-[:HAS_ATTRIBUTE|HAS_TABULAR_SECTION|OF_TYPE|RESOLVES_TO*1..{int(max_depth)}]-(b))
            WITH p LIMIT {int(limit)}
            WITH nodes(p) AS ns, relationships(p) AS rs
            RETURN [n IN ns | {{
                labels: labels(n),
                id: coalesce(n.id, n.full_name_eng),
                name: coalesce(n.name, n.full_name_eng),
                kind: n.kind_eng,
                role: n.role
            }}] AS path_nodes,
                   [r IN rs | type(r)] AS path_rels
            """,
            {"a": from_object, "b": to_object},
        )
        if not rows:
            return json.dumps({
                "from": from_object, "to": to_object, "found": False,
                "hint": "Прямого пути не найдено. Попробуйте увеличить max_depth или "
                        "проверить имена через metadata_search."
            }, ensure_ascii=False)

        paths = []
        for r in rows:
            paths.append({
                "rels":  r["path_rels"],
                "nodes": r["path_nodes"],
                "length": len(r["path_rels"]),
            })
        return json.dumps({
            "from": from_object, "to": to_object, "found": True,
            "paths_count": len(paths), "paths": paths,
        }, ensure_ascii=False, indent=2)

    # ─── metadata_referrers ──────────────────────────────────────────────

    @mcp.tool()
    def metadata_referrers(object_full_name: str, limit: int = 50, offset: int = 0) -> str:
        """
        Кто ссылается на этот объект через реквизиты (включая ТЧ).

        Цепочка: (other:MetadataObject)-[:HAS_ATTRIBUTE|HAS_TABULAR_SECTION..]->
                 (:Attribute)-[:OF_TYPE]->(:Type {target: object_id})-[:RESOLVES_TO]->(this)

        Возвращает список ссылающихся реквизитов с указанием:
          - имя объекта-источника
          - имя реквизита
          - находится ли в ТЧ (имя ТЧ)
          - роль (attribute/dimension/resource)
        """
        _err = _guard()
        if _err:
            return _err
        if limit < 1: limit = 50
        if limit > 200: limit = 200
        if offset < 0: offset = 0

        # Резолв
        rows = neo4j_rows(
            "MATCH (m:MetadataObject) "
            "WHERE m.full_name_eng = $fn OR m.full_name_ru = $fn OR m.id = $fn "
            # FIX-18: единственное число вида — «Справочник.X».
            "   OR (m.kind_ru + '.' + m.name) = $fn "
            "RETURN m.id AS id LIMIT 1",
            {"fn": object_full_name},
        )
        if not rows:
            return json.dumps({"found": False, "error": f"Объект '{object_full_name}' не найден"},
                              ensure_ascii=False)
        target_id = rows[0]["id"]

        total = neo4j_count(
            "MATCH (t:Type)-[:RESOLVES_TO]->(target:MetadataObject {id: $tid}) "
            "MATCH (a:Attribute)-[:OF_TYPE]->(t) "
            "MATCH (parent)-[:HAS_ATTRIBUTE]->(a) "
            "RETURN count(a)",
            {"tid": target_id},
        )

        items = neo4j_rows(
            """
            MATCH (t:Type)-[:RESOLVES_TO]->(target:MetadataObject {id: $tid})
            MATCH (a:Attribute)-[:OF_TYPE]->(t)
            MATCH (parent)-[:HAS_ATTRIBUTE]->(a)
            OPTIONAL MATCH (owner:MetadataObject)-[:HAS_TABULAR_SECTION]->(parent)
            WITH a, parent, owner, t,
                 CASE WHEN owner IS NULL THEN parent ELSE owner END AS root
            RETURN root.full_name_eng AS owner_object,
                   root.kind_eng       AS owner_kind,
                   root.synonym        AS owner_synonym,
                   a.name              AS attr_name,
                   a.role              AS attr_role,
                   CASE WHEN owner IS NULL THEN null ELSE parent.name END AS in_tabular_section,
                   t.kind              AS type_kind
            ORDER BY root.full_name_eng, a.name
            SKIP $offset LIMIT $limit
            """,
            {"tid": target_id, "offset": offset, "limit": limit},
        )

        end = offset + len(items)
        return json.dumps({
            "found":   True,          # FIX-19: симметрично ветке «не найдено»
            "object":  target_id,
            "total":   total,
            "returned": len(items),
            "offset":  offset,
            "limit":   limit,
            "has_more": end < total,
            "next_offset": end if end < total else None,
            "items":   items,
        }, ensure_ascii=False, indent=2)

    # ─── metadata_object_attributes ──────────────────────────────────────

    @mcp.tool()
    def metadata_object_attributes(object_full_name: str, include_tabular: bool = True,
                                   role: str = "") -> str:
        """
        Реквизиты объекта в виде узлов :Attribute (а не JSON-поля).

        В отличие от старого metadata_object_details, здесь можно:
          - отфильтровать по role: attribute | dimension | resource
          - включить или исключить реквизиты ТЧ
        """
        _err = _guard()
        if _err:
            return _err

        rows = neo4j_rows(
            "MATCH (m:MetadataObject) "
            "WHERE m.full_name_eng = $fn OR m.full_name_ru = $fn OR m.id = $fn "
            # FIX-18: единственное число вида — «Справочник.X».
            "   OR (m.kind_ru + '.' + m.name) = $fn "
            "RETURN m.id AS id LIMIT 1",
            {"fn": object_full_name},
        )
        if not rows:
            return json.dumps({"found": False, "error": f"Объект '{object_full_name}' не найден"},
                              ensure_ascii=False)
        oid = rows[0]["id"]
        role_filter = " AND a.role = $role" if role else ""

        # Прямые реквизиты
        direct = neo4j_rows(
            f"""
            MATCH (m:MetadataObject {{id: $oid}})-[:HAS_ATTRIBUTE]->(a:Attribute)
            WHERE 1=1{role_filter}
            OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type)
            OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject)
            WITH a, collect({{kind: t.kind, target: t.target,
                              resolved: o.full_name_eng}}) AS types
            RETURN a.name AS name, a.synonym AS synonym, a.role AS role,
                   a.is_master AS is_master, a.indexing AS indexing, types
            ORDER BY a.name
            """,
            {"oid": oid, **({"role": role} if role else {})},
        )

        result = {
            # FIX-19. Поле `found` возвращалось ТОЛЬКО при неудаче, а на
            # успешном ответе отсутствовало. Проверяющий его агент получал
            # null и мог заключить, что объект не найден, — притом что
            # состав лежит рядом в том же ответе. Асимметричный признак
            # хуже отсутствующего: он выглядит надёжным.
            #
            # Нашлось на EVAL-1: пример с русской формой имени вернул
            # реквизиты и одновременно «не найден».
            "found":            True,
            "object":           oid,
            "direct_attributes": direct,
            "direct_count":     len(direct),
        }

        if include_tabular:
            ts = neo4j_rows(
                f"""
                MATCH (m:MetadataObject {{id: $oid}})-[:HAS_TABULAR_SECTION]->(ts:TabularSection)
                OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(a:Attribute){' WHERE a.role = $role' if role else ''}
                OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type)
                OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject)
                WITH ts, a, collect({{kind: t.kind, target: t.target,
                                       resolved: o.full_name_eng}}) AS types
                WITH ts, collect(CASE WHEN a IS NULL THEN null
                                       ELSE {{name: a.name, synonym: a.synonym,
                                              role: a.role, types: types}} END) AS attrs
                RETURN ts.name AS ts_name, ts.synonym AS ts_synonym,
                       [x IN attrs WHERE x IS NOT NULL] AS attributes
                ORDER BY ts.name
                """,
                {"oid": oid, **({"role": role} if role else {})},
            )
            result["tabular_sections"] = ts
            result["tabular_section_count"] = len(ts)
            result["tabular_attribute_count"] = sum(len(t["attributes"]) for t in ts)

        return json.dumps(result, ensure_ascii=False, indent=2)

    # ─── metadata_subsystem_tree ─────────────────────────────────────────

    @mcp.tool()
    def metadata_subsystem_tree(root: str = "", max_depth: int = 5) -> str:
        """
        Дерево подсистем (PARENT_OF). С указанным корнем — поддерево от него,
        без — все верхнеуровневые подсистемы.
        """
        _err = _guard()
        if _err:
            return _err

        if root:
            rows = neo4j_rows(
                f"""
                MATCH (r:MetadataObject) WHERE r.kind_eng = 'Subsystem'
                  AND (r.full_name_eng = $r OR r.name = $r)
                WITH r LIMIT 1
                MATCH p = (r)-[:PARENT_OF*0..{int(max_depth)}]->(s:MetadataObject)
                WHERE s.kind_eng = 'Subsystem'
                RETURN s.name AS name, s.full_name_eng AS full_name,
                       length(p) AS depth
                ORDER BY depth, name
                """, {"r": root},
            )
        else:
            rows = neo4j_rows(
                f"""
                MATCH (s:MetadataObject {{kind_eng: 'Subsystem'}})
                OPTIONAL MATCH (parent:MetadataObject)-[:PARENT_OF]->(s)
                WITH s, parent
                WHERE parent IS NULL
                MATCH p = (s)-[:PARENT_OF*0..{int(max_depth)}]->(child:MetadataObject)
                RETURN child.name AS name, child.full_name_eng AS full_name,
                       length(p) AS depth, s.full_name_eng AS root
                ORDER BY root, depth, name
                """, {},
            )

        return json.dumps({
            "root":  root or "(все корневые)",
            "count": len(rows),
            "items": rows,
        }, ensure_ascii=False, indent=2)

    # ─── metadata_dead ───────────────────────────────────────────────────

    @mcp.tool()
    def metadata_dead(kind: str = "", limit: int = 50, offset: int = 0) -> str:
        """
        Объекты конфигурации, не входящие ни в одну подсистему.

        В 1С это типичный признак «забытой» или экспериментальной метаданной,
        не отображаемой в командном интерфейсе. Полезно для аудита.

        Параметры:
          kind  — фильтр по виду объекта ("Catalog", "Document", ...)
                  Если пусто — все, кроме самих Subsystem.
        """
        _err = _guard()
        if _err:
            return _err
        if limit < 1: limit = 50
        if limit > 200: limit = 200

        params = {"offset": offset, "limit": limit}
        where_kind = "AND m.kind_eng = $kind" if kind else "AND m.kind_eng <> 'Subsystem'"
        if kind:
            params["kind"] = kind

        total = neo4j_count(
            f"""
            MATCH (m:MetadataObject)
            WHERE NOT EXISTS {{ (:MetadataObject)-[:CONTAINS]->(m) }} {where_kind}
            RETURN count(m)
            """, params,
        )

        items = neo4j_rows(
            f"""
            MATCH (m:MetadataObject)
            WHERE NOT EXISTS {{ (:MetadataObject)-[:CONTAINS]->(m) }} {where_kind}
            RETURN m.full_name_eng AS full_name, m.kind_eng AS kind,
                   m.synonym AS synonym
            ORDER BY m.kind_eng, m.name
            SKIP $offset LIMIT $limit
            """, params,
        )
        end = offset + len(items)
        return json.dumps({
            "kind_filter":  kind or "all (кроме Subsystem)",
            "total":        total,
            "returned":     len(items),
            "offset":       offset,
            "limit":        limit,
            "has_more":     end < total,
            "next_offset":  end if end < total else None,
            "items":        items,
        }, ensure_ascii=False, indent=2)

    # ─── metadata_v3_stats ───────────────────────────────────────────────

    @mcp.tool()
    def metadata_v3_stats() -> str:
        """
        Расширенная статистика графа (v3-схема: :Attribute, :Type, :Form, :EnumValue).
        Дополняет старый metadata_stats, показывая разбивку по новым видам узлов.
        """
        _err = _guard()
        if _err:
            return _err

        meta_count = neo4j_count("MATCH (n:MetadataObject) RETURN count(n)")
        attr_count = neo4j_count("MATCH (n:Attribute) RETURN count(n)")
        ts_count   = neo4j_count("MATCH (n:TabularSection) RETURN count(n)")
        form_count = neo4j_count("MATCH (n:Form) RETURN count(n)")
        ev_count   = neo4j_count("MATCH (n:EnumValue) RETURN count(n)")
        type_count = neo4j_count("MATCH (n:Type) RETURN count(n)")

        # Разбивка рёбер по типу
        edge_rows = neo4j_rows(
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC"
        )

        # Top-10 типов по числу использований (по входящим OF_TYPE)
        hot_types = neo4j_rows(
            """
            MATCH (a:Attribute)-[:OF_TYPE]->(t:Type)
            RETURN t.kind AS kind, t.target AS target, count(a) AS used_by
            ORDER BY used_by DESC LIMIT 10
            """
        )

        # Топ-10 объектов по числу входящих RESOLVES_TO (наиболее «упомянутые»)
        hot_targets = neo4j_rows(
            """
            MATCH (t:Type)-[:RESOLVES_TO]->(m:MetadataObject)
            MATCH (a:Attribute)-[:OF_TYPE]->(t)
            WITH m, count(a) AS refs
            RETURN m.full_name_eng AS object, refs
            ORDER BY refs DESC LIMIT 10
            """
        )

        fp = neo4j_rows(
            "MATCH (n:Fingerprint {kind: 'metadata_xml'}) "
            "RETURN n.value AS value, n.updated_at AS updated_at LIMIT 1"
        )
        fingerprint = None
        if fp:
            fingerprint = {
                "value":       (fp[0]["value"] or "")[:16] + "…",
                "updated_at":  fp[0]["updated_at"],
            }

        return json.dumps({
            "nodes": {
                "MetadataObject": meta_count,
                "Attribute":      attr_count,
                "TabularSection": ts_count,
                "Form":           form_count,
                "EnumValue":      ev_count,
                "Type":           type_count,
            },
            "edges":              {r["rel"]: r["cnt"] for r in edge_rows},
            "edges_total":        sum(r["cnt"] for r in edge_rows),
            "hot_types":          hot_types,
            "hot_targets":        hot_targets,
            "fingerprint":        fingerprint,
            "schema_version":     "v3",
        }, ensure_ascii=False, indent=2)
