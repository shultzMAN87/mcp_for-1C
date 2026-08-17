"""
Инкрементальное обновление графа метаданных — задача 4.6.5.
==============================================================

Точечная переиндексация одного файла (`.bsl` или `.xml`) без полной
пересборки графа. Используется `workspace-watcher` через MCP-tools
`metadata_upsert_file` / `metadata_remove_file` (сервер `mcp-metadata-graph`).

Устроен по образцу точечной переиндексации из `mcp-code-rag` (задача 2.3),
только для Neo4j-графа вместо Qdrant-коллекции. Самого сервера в проекте
больше нет — упоминание оставлено как происхождение решения, а не как
ссылка на работающий код.

──────────────────────────────────────────────────────────────────────────
ГРАНИЦА АКТУАЛЬНОСТИ (важно — прочитать перед использованием)
──────────────────────────────────────────────────────────────────────────
Полный `build_call_graph` (indexer.py) — это ГЛОБАЛЬНЫЙ фикс-пойнт-резолв:
тип параметра процедуры в модуле A может зависеть от callsite'а в модуле B,
и наоборот. Инкрементальный апдейт одного файла такой глобальной сходимости
обеспечить не может за O(1) — это была бы пересборка всего графа.

Поэтому v1 (эта реализация) делает прагматичный, локально-консистентный
апдейт — ровно тот объём, что обещан в PLAN.md разделе 4.6.5
(«точечно перестраивать узлы соответствующего файла»):

  ИСХОДЯЩАЯ сторона (полностью корректна):
    • Узлы модуля изменённого файла (:Callable / :Parameter / :CallSite)
      пересобираются с нуля.
    • Исходящие :CALLS / :OPERATES_ON / :CALL_SITE / :HAS_METHOD / :HAS_PARAM
      этого модуля пересобираются полностью — резолв идёт против ЖИВОГО
      Neo4j-индекса (все остальные модули в графе на момент апдейта).

  ВХОДЯЩАЯ сторона (может слегка устаревать до полного reindex):
    • :CALLS-рёбра ИЗ ДРУГИХ файлов, которые резолвились в процедуру этого
      файла. Если процедуру переименовали/удалили — старое :CALLS повисло бы
      «в никуда»; MERGE по callsite в чужих модулях мы здесь не трогаем.
      Чтобы граф не врал о покрытии, `_restale_callsites_into_module`
      переводит такие осиротевшие чужие callsite'ы в `resolved=false`
      с `reason='stale_after_incremental'`. Реальная пере-резолюция
      (вдруг процедуру не удалили, а лишь сдвинули) случится, когда тот
      чужой файл сам переиндексируется или пройдёт полный reindex. Это
      осознанный компромисс v1 — он отмечен в HANDOFF.

  Type inference (:INFERRED_TYPE):
    • Выполняется ЛОКАЛЬНО для процедур изменённого файла (фикс-пойнт по
      его собственным процедурам). Inter-procedural типы, приходящие из
      чужих модулей, в инкрементальном режиме не подтягиваются — это
      требует глобального прохода. Полный `metadata-indexer` досчитает.

Иными словами: инкремент — это «быстрое и достаточно хорошее» обновление
для рабочего цикла агента; раз в N изменений (или по расписанию) имеет
смысл прогонять полный `metadata-indexer` для глобальной досходимости.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from graph_writer import (
    Neo4j, ensure_schema,
    write_module_nodes, write_callable_nodes, write_parameter_nodes,
    write_callsite_nodes, write_type_nodes, write_edges,
    write_meta_nodes, write_attribute_nodes, write_tabular_section_nodes,
    write_form_nodes, write_enum_value_nodes,
)
from bsl_parser import classify_bsl_path, parse_bsl_module, walk_workspace_bsl
from bsl_resolver import build_call_graph, build_index_from_neo4j
from metadata_xml import KINDS, _parse_object, build_graph

log = logging.getLogger("metadata-indexer.incremental")


# ─── Общие хелперы ────────────────────────────────────────────────────────


def _norm_rel(src_root: Path, filepath: str) -> Optional[str]:
    """
    Приводит вход (абсолютный POSIX-путь в контейнере или относительный от
    src_root) к относительному POSIX-пути от корня выгрузки.

    Стратегия резолва:
      1. Если путь относительный — возвращаем как есть (POSIX-нормализованный).
      2. Если путь абсолютный и ВНУТРИ src_root — режем префикс.
      3. ФОЛБЭК: если путь абсолютный, но НЕ внутри src_root, пытаемся
         найти его «общий хвост» с известной схемой 1С-выгрузки. Например,
         watcher шлёт `/workspace/Catalogs/X/Ext/ObjectModule.bsl`,
         а у нас `METADATA_SRC_DIR=/data/1c-xml`. Хвост `Catalogs/X/...`
         однозначно интерпретируется как путь от корня выгрузки. Это
         позволяет watcher'у и серверу видеть один и тот же volume под
         разными точками монтирования без переделок compose.
      4. Если хвост не найден — возвращаем None (watcher следит за чужой
         папкой, либо это случайный левый путь).

    Тонкость кросс-платформенности: контейнер у нас всегда Linux, поэтому
    «абсолютным» считаем POSIX-путь, начинающийся с '/'. Использовать
    `Path.is_absolute()` нельзя — под Windows-разработчиком `Path('/data/...')`
    is_absolute()==False. PurePosixPath одинаково ведёт себя на любой ОС.
    """
    from pathlib import PurePosixPath
    fp_norm = filepath.replace("\\", "/")
    pp = PurePosixPath(fp_norm)
    if not pp.is_absolute():
        return str(pp)

    # Прямой случай: путь внутри src_root.
    root_pp = PurePosixPath(str(src_root).replace("\\", "/"))
    try:
        return str(pp.relative_to(root_pp))
    except ValueError:
        pass

    # Фолбэк: общий хвост по известным top-level директориям выгрузки.
    # Хвост должен начинаться с одной из этих папок ИЛИ с tests-extension.
    # Если src_root реально существует на диске (и в нём есть нужный хвост) —
    # дополнительно проверяем существование, чтобы случайные строки типа
    # `/etc/Catalogs/foo.bsl` не маппились.
    parts = pp.parts  # ('/', 'workspace', 'Catalogs', ...) на POSIX
    for i, part in enumerate(parts):
        if part in _TAIL_TOP_DIRS or part == "tests-extension":
            tail = PurePosixPath(*parts[i:])
            tail_str = str(tail)
            # Доп.санити: если src_root существует — должен существовать
            # и (src_root / tail). На юнит-тестах src_root может быть
            # синтетическим (например, /data/1c-src), тогда .exists() даст
            # False, но это не повод отбрасывать резолв — мы и так знаем
            # схему. Поэтому проверка мягкая: или src_root не существует
            # (тестовая среда), или (src_root / tail) существует (прод).
            try:
                if not src_root.exists() or (src_root / tail_str).exists():
                    return tail_str
            except OSError:
                return tail_str
    return None


# Top-level папки выгрузки 1С (синхрон с metadata_xml.KINDS + bsl_parser.DIR_TO_KIND_ENG).
# Используется только в `_norm_rel` для tail-suffix фолбэка.
_TAIL_TOP_DIRS = frozenset({
    "Catalogs", "Documents", "Enums", "DataProcessors", "Reports",
    "InformationRegisters", "AccumulationRegisters", "AccountingRegisters",
    "CalculationRegisters", "ChartsOfCharacteristicTypes", "ChartsOfAccounts",
    "ChartsOfCalculationTypes", "DocumentJournals", "CommonModules",
    "Constants", "ExchangePlans", "BusinessProcesses", "Tasks",
    "Subsystems", "CommonCommands", "CommonForms", "HTTPServices",
    "WebServices", "ScheduledJobs", "SettingsStorages", "FilterCriteria",
    "SessionParameters", "CommonAttributes", "CommonPictures",
    "CommonTemplates", "FunctionalOptions", "DefinedTypes", "Roles",
    "Languages", "EventSubscriptions",
})


# ─── BSL: точечный апдейт одного модуля ──────────────────────────────────


# Метки слоя 2, прицепляемые ИСКЛЮЧИТЕЛЬНО к узлам кода. :MetadataObject и
# :Module формально слой 1 — их мы при инкременте не удаляем (см. ниже
# _clear_module_code_slice: для не-Form модулей узел :MetadataObject:Module
# создаётся в фазе 2, но мы его НЕ сносим, только пересоздаём MERGE'ем —
# чтобы не уронить рёбра :CONTAINS из подсистем, если они есть).
_CODE_NODE_LABELS = ("Callable", "Parameter", "CallSite")


def _clear_module_code_slice(neo: Neo4j, module_id: str) -> dict:
    """
    Удаляет узлы слоя 2, принадлежащие одному модулю:
      • :Callable с module_id == module_id
      • :Parameter, висящие на этих :Callable
      • :CallSite, висящие на этих :Callable
    DETACH DELETE сносит вместе с ними все инцидентные рёбра — в т.ч.
    исходящие :CALLS/:OPERATES_ON и входящие :CALLS (последние станут
    висячими иначе — поэтому DETACH важен).

    :Module / :MetadataObject узел самого модуля НЕ трогаем — он
    пересоздаётся MERGE'ем в write_module_nodes (идемпотентно).

    Возвращает {'deleted_callables', 'deleted_parameters', 'deleted_callsites'}.
    """
    # Считаем до удаления — для отчёта.
    counts = neo.rows(
        "MATCH (c:Callable {module_id: $mid}) "
        "OPTIONAL MATCH (c)-[:HAS_PARAM]->(p:Parameter) "
        "OPTIONAL MATCH (c)-[:CALL_SITE]->(cs:CallSite) "
        "RETURN count(DISTINCT c) AS c, count(DISTINCT p) AS p, "
        "       count(DISTINCT cs) AS cs",
        {"mid": module_id},
    )
    row = counts[0] if counts else {"c": 0, "p": 0, "cs": 0}

    # Сносим параметры и callsite'ы, затем сами callable'ы.
    neo.query(
        "MATCH (c:Callable {module_id: $mid})-[:HAS_PARAM]->(p:Parameter) "
        "DETACH DELETE p",
        {"mid": module_id},
    )
    neo.query(
        "MATCH (c:Callable {module_id: $mid})-[:CALL_SITE]->(cs:CallSite) "
        "DETACH DELETE cs",
        {"mid": module_id},
    )
    neo.query(
        "MATCH (c:Callable {module_id: $mid}) DETACH DELETE c",
        {"mid": module_id},
    )
    return {
        "deleted_callables":  row["c"],
        "deleted_parameters": row["p"],
        "deleted_callsites":  row["cs"],
    }


def _filter_code_graph_to_module(code_graph: dict, module_id: str) -> dict:
    """
    Из полного `code_graph` (build_call_graph по всем модулям) вырезает
    только тот срез, что относится к указанному модулю:
      • module_nodes      — только этот модуль
      • callable_nodes    — только его callable'ы
      • parameter_nodes   — только параметры его callable'ов
      • callsite_nodes    — только callsite'ы, где caller из этого модуля
      • type_nodes        — типы, на которые ссылаются :INFERRED_TYPE этого среза
      • edges             — только рёбра, ИСХОДЯЩИЕ из узлов этого модуля
                            (HAS_METHOD от модуля, HAS_PARAM/CALL_SITE/CALLS/
                             OPERATES_ON от его callable'ов, INFERRED_TYPE от
                             его параметров, RESOLVES_TO_CALLEE от его
                             callsite'ов)

    Так мы пишем РОВНО срез одного файла, не затирая чужие узлы.
    """
    our_callable_ids = {
        n["id"] for n in code_graph["callable_nodes"]
        if n["module_id"] == module_id
    }
    our_param_ids = {
        n["id"] for n in code_graph["parameter_nodes"]
        if n["callable_id"] in our_callable_ids
    }
    our_callsite_ids = {
        n["id"] for n in code_graph["callsite_nodes"]
        if n["caller_id"] in our_callable_ids
    }
    # Источники рёбер, которые «наши»: сам модуль + его callable'ы +
    # его параметры + его callsite'ы.
    our_edge_srcs = {module_id} | our_callable_ids | our_param_ids | our_callsite_ids

    edges = [e for e in code_graph["edges"] if e["src"] in our_edge_srcs]

    # type_nodes — оставляем только те, на которые реально ссылаются наши
    # :INFERRED_TYPE-рёбра (write_type_nodes делает MERGE — лишние узлы не
    # навредили бы, но чистый срез нагляднее и дешевле).
    referenced_type_ids = {
        e["dst"] for e in edges if e["rel"] == "INFERRED_TYPE"
    }
    type_nodes = [
        t for t in code_graph["type_nodes"]
        if t["id"] in referenced_type_ids
    ]

    module_nodes = [
        n for n in code_graph["module_nodes"] if n["id"] == module_id
    ]
    callable_nodes = [
        n for n in code_graph["callable_nodes"]
        if n["module_id"] == module_id
    ]
    parameter_nodes = [
        n for n in code_graph["parameter_nodes"]
        if n["callable_id"] in our_callable_ids
    ]
    callsite_nodes = [
        n for n in code_graph["callsite_nodes"]
        if n["caller_id"] in our_callable_ids
    ]

    return {
        "module_nodes":    module_nodes,
        "callable_nodes":  callable_nodes,
        "parameter_nodes": parameter_nodes,
        "callsite_nodes":  callsite_nodes,
        "type_nodes":      type_nodes,
        "edges":           edges,
        "stats":           {},
    }


def _write_code_slice(neo: Neo4j, slice_graph: dict) -> dict:
    """
    Пишет срез одного модуля. Порядок тот же, что в write_code_graph:
    сначала Module, потом Callable/Parameter/CallSite/Type, потом рёбра.
    """
    ensure_schema(neo)
    n_module    = write_module_nodes(neo, slice_graph["module_nodes"])
    n_callable  = write_callable_nodes(neo, slice_graph["callable_nodes"])
    n_parameter = write_parameter_nodes(neo, slice_graph["parameter_nodes"])
    n_callsite  = write_callsite_nodes(neo, slice_graph["callsite_nodes"])
    n_type      = write_type_nodes(neo, slice_graph["type_nodes"])
    edge_counters = write_edges(neo, slice_graph["edges"])
    return {
        "Module": n_module, "Callable": n_callable,
        "Parameter": n_parameter, "CallSite": n_callsite, "Type": n_type,
        "edges": edge_counters,
    }


def _restale_callsites_into_module(neo: Neo4j, module_id: str) -> int:
    """
    Помечает `resolved=false` те :CallSite в ДРУГИХ модулях, что ссылались
    на процедуры этого модуля, но потеряли свой :RESOLVES_TO_CALLEE (т.е.
    callee-:Callable снёсся вместе со старым срезом, а после записи нового
    среза процедура с тем же id могла не пересоздаться — переименование/
    удаление процедуры).

    Зачем: после `_clear_module_code_slice` + `_write_code_slice` входящие
    :CALLS из чужих модулей, что вели в пере­именованную/удалённую
    процедуру, исчезли (DETACH DELETE снёс ребро). Но сам чужой :CallSite
    остался с `resolved=true` — это стейл-флаг. Здесь мы его честно
    переводим в `resolved=false` + `reason='stale_after_incremental'`,
    чтобы code_unresolved_callsites / code_v3_stats не врали о покрытии.

    Полную пере-резолюцию чужого callsite (вдруг процедуру не удалили, а
    лишь сдвинули — id тот же) инкремент не делает: id :Callable = module_id
    + имя процедуры, сдвиг строк его не меняет, поэтому :CALLS на тот же id
    переживёт MERGE в _write_code_slice и :CallSite останется валидным.
    Под re-stale попадают ТОЛЬКО реально потерявшие callee.

    Возвращает число перемаркированных callsite'ов.
    """
    rows = neo.rows(
        "MATCH (c:Callable)-[:CALL_SITE]->(cs:CallSite) "
        "WHERE cs.resolved = true "
        "  AND NOT (cs)-[:RESOLVES_TO_CALLEE]->(:Callable) "
        "  AND c.module_id <> $mid "
        "SET cs.resolved = false, cs.reason = 'stale_after_incremental' "
        "RETURN count(cs) AS n",
        {"mid": module_id},
    )
    return rows[0]["n"] if rows else 0


def upsert_bsl_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Точечно переиндексирует один .bsl-файл в слой 2 графа.

    Шаги:
      1. Классифицировать путь → module_id (classify_bsl_path).
      2. Спарсить ОДИН файл в ParsedModule.
      3. Построить индекс резолвера из ЖИВОГО Neo4j (все остальные модули) +
         этот модуль — чтобы исходящие вызовы резолвились против актуального
         графа.
      4. Прогнать build_call_graph на ОДНОМ модуле (фикс-пойнт по его
         собственным процедурам — локальный type inference).
      5. Вырезать срез этого модуля, снести старый срез, записать новый.

    Возвращает JSON-совместимый dict со статусом и счётчиками.
    """
    rel = _norm_rel(src_root, filepath)
    if rel is None:
        return {"status": "skipped", "reason": "path_outside_src_root",
                "filepath": filepath}

    abs_path = src_root / rel
    if abs_path.suffix.lower() != ".bsl":
        return {"status": "skipped", "reason": "not_a_bsl_file", "file": rel}

    if not abs_path.exists():
        return {"status": "skipped", "reason": "file_not_found", "file": rel}

    classified = classify_bsl_path(rel)
    if not classified:
        return {"status": "skipped", "reason": "unknown_path_schema", "file": rel}
    module_id, module_kind, parent_metadata_id, role = classified

    # Pre-flight: слой 1 должен быть в графе (как в indexer.run_bsl_phase).
    meta_rows = neo.rows("MATCH (m:MetadataObject) RETURN count(m) AS n")
    if not (meta_rows and meta_rows[0]["n"]):
        return {"status": "error", "reason": "metadata_layer_empty",
                "file": rel,
                "hint": "Сначала прогоните полный metadata-indexer (фаза 1 XML)"}

    # is_server/is_client — для CommonModule берём из properties_json в Neo4j.
    is_server, is_client = True, False
    if module_kind == "CommonModule":
        prop_rows = neo.rows(
            "MATCH (m:MetadataObject:CommonModule {id: $id}) "
            "RETURN m.properties_json AS props",
            {"id": module_id},
        )
        if prop_rows and prop_rows[0].get("props"):
            import json as _json
            try:
                props = _json.loads(prop_rows[0]["props"])
            except (TypeError, ValueError):
                props = {}
            is_server = bool(props.get("Server", True))
            is_client = bool(
                props.get("ClientManagedApplication", False)
                or props.get("ClientOrdinaryApplication", False)
            )
    elif module_kind in ("ObjectModule", "ManagerModule"):
        is_server, is_client = True, False
    else:  # Form — контекст определяется директивой процедуры
        is_server, is_client = False, False

    try:
        parsed = parse_bsl_module(
            path=abs_path,
            module_id=module_id,
            module_kind=module_kind,
            parent_metadata_id=parent_metadata_id,
            source_path=rel,
            is_server=is_server,
            is_client=is_client,
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": "parse_failed",
                "file": rel, "detail": f"{type(e).__name__}: {e}"}

    # Индекс резолвера — против ЖИВОГО графа. build_index_from_neo4j читает
    # metadata_objects и common_module_props из Neo4j; callable_ids берёт
    # из переданных модулей. Передаём только наш модуль — этого достаточно
    # для резолва ВНУТРИ-модульных вызовов; для меж-модульных резолвер
    # сверяется с index.metadata_full_set + manager-call по callable_ids.
    #
    # Тонкость: build_index_from_neo4j(neo, [parsed]) положит в callable_ids
    # только процедуры нашего модуля. Меж-модульные вызовы Модуль.Метод()
    # резолвятся через common_modules + проверку "модуль такой существует";
    # реальное :CALLS-ребро пишется на callable_id = "CommonModule.X.Метод" —
    # write_edges сделает MATCH (b:Callable {id: ...}), и если узел есть в
    # графе (а он есть — другие модули уже проиндексированы) — ребро ляжет.
    # Чтобы резолвер ЗНАЛ про эти id, дозаполняем callable_ids из Neo4j.
    index = build_index_from_neo4j(neo, [parsed])
    all_callable_rows = neo.rows("MATCH (c:Callable) RETURN c.id AS id")
    index.callable_ids |= {r["id"] for r in all_callable_rows}

    # build_call_graph на одном модуле: фикс-пойнт по его процедурам.
    code_graph = build_call_graph([parsed], index)

    slice_graph = _filter_code_graph_to_module(code_graph, module_id)

    # Снести старый срез модуля, записать новый.
    cleared = _clear_module_code_slice(neo, module_id)
    written = _write_code_slice(neo, slice_graph)
    restaled = _restale_callsites_into_module(neo, module_id)

    s = code_graph["stats"]
    return {
        "status": "reindexed",
        "file": rel,
        "module_id": module_id,
        "module_kind": module_kind,
        "cleared": cleared,
        "written": {k: v for k, v in written.items() if k != "edges"},
        "edges_written": written["edges"],
        "resolve": {
            "resolved":   s.get("resolved", 0),
            "unresolved": s.get("unresolved", 0),
            # FIX-4: разбивка unresolved — сколько из них настоящие пробелы,
            # а сколько вызовы методов объектов платформы.
            "unresolved_gaps":          s.get("unresolved_gaps", 0),
            "unresolved_object_method": s.get("unresolved_object_method", 0),
            "coverage_pct": s.get("resolve_coverage_pct", 0.0),
            "skipped":    s.get("skipped", 0),
            "inferred_types": s.get("inferred_types", 0),
            "fixpoint_iterations": s.get("fixpoint_iterations", 0),
        },
        "stale_callsites_marked": restaled,
    }


def remove_bsl_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Удаляет из графа срез слоя 2, принадлежащий одному .bsl-файлу
    (файл уже удалён/перемещён с диска — watcher шлёт remove).

    :Module / :MetadataObject узел самого модуля НЕ удаляем — он мог быть
    создан фазой 1 (Form) или нести рёбра :CONTAINS из подсистем. Сносим
    только :Callable / :Parameter / :CallSite этого модуля; их рёбра уходят
    вместе с ними (DETACH DELETE).
    """
    rel = _norm_rel(src_root, filepath)
    if rel is None:
        return {"status": "skipped", "reason": "path_outside_src_root",
                "filepath": filepath}

    if Path(rel).suffix.lower() != ".bsl":
        return {"status": "skipped", "reason": "not_a_bsl_file", "file": rel}

    classified = classify_bsl_path(rel)
    if not classified:
        return {"status": "skipped", "reason": "unknown_path_schema", "file": rel}
    module_id, module_kind, _parent, _role = classified

    cleared = _clear_module_code_slice(neo, module_id)
    restaled = _restale_callsites_into_module(neo, module_id)

    return {
        "status": "removed",
        "file": rel,
        "module_id": module_id,
        "module_kind": module_kind,
        "cleared": cleared,
        "stale_callsites_marked": restaled,
    }


# ─── XML: точечный апдейт одного метаобъекта ─────────────────────────────


# Метки слоя 1, относящиеся к одному метаобъекту и его потомкам.
# :Type-узлы НЕ сюда — они шарятся между объектами (MERGE по id).
_META_OBJECT_CHILD_LABELS = ("Attribute", "TabularSection", "Form", "EnumValue")


def _classify_xml_path(rel_path_posix: str) -> Optional[tuple[str, str, str, str]]:
    """
    По относительному POSIX-пути верхнеуровневого XML определяет
    `(dir_name, kind_eng, kind_ru, kind_ru_plural)` или None.

    Поддерживаются ТОЛЬКО верхнеуровневые XML вида `Catalogs/АукАукционы.xml`.
    Вложенные XML (Forms/*.xml, Templates/*.xml) — не самостоятельные
    объекты, они описаны через <Form>/<Template> в верхнем XML; их
    изменение должно триггерить апдейт РОДИТЕЛЬСКОГО XML, но watcher этого
    пока не делает — это отмечено в граблях.
    """
    parts = rel_path_posix.split("/")
    if parts and parts[0] == "tests-extension":
        parts = parts[1:]
    # Верхнеуровневый объект: <Dir>/<Name>.xml — ровно 2 компонента.
    if len(parts) != 2 or not parts[1].endswith(".xml"):
        return None
    dir_name = parts[0]
    for d, kind_eng, kind_ru, kind_ru_plural in KINDS:
        if d == dir_name:
            return (dir_name, kind_eng, kind_ru, kind_ru_plural)
    return None


def _clear_meta_object_slice(neo: Neo4j, meta_id: str) -> dict:
    """
    Удаляет :MetadataObject и его потомков (:Attribute / :TabularSection /
    :Form / :EnumValue), висящих через id-префикс `meta_id.`.

    :Type-узлы НЕ трогаем — они общие. Их «осиротевшие» рёбра :OF_TYPE
    уйдут вместе с :Attribute (DETACH DELETE). Сам :Type-узел останется —
    он переиспользуется при следующей записи (write_type_nodes — MERGE).

    ВАЖНО: :MetadataObject:Module узлы (CommonModule.X и т.п.) — это слой 1,
    но за их ВНУТРЕННОСТЬ (:Callable) отвечает слой 2. При апдейте XML
    CommonModule мы сносим сам :MetadataObject-узел и пересоздаём его —
    :Callable на нём останутся (они висят через :HAS_METHOD, а DETACH
    снёс бы ребро, но не сам :Callable). Чтобы код-слой не осиротел,
    upsert_xml_file пересоздаёт :HAS_METHOD-привязки? Нет — проще: при
    DETACH DELETE :MetadataObject рёбра :HAS_METHOD исчезнут, :Callable
    останутся «висящими» по module_id. Следующий upsert_bsl_file или
    полный reindex их перевяжет. Для XML-апдейта это приемлемо — XML
    CommonModule.xml меняется редко (флаги Server/Client), и почти всегда
    в паре с правкой Module.bsl, который watcher тоже переиндексирует.

    Возвращает счётчики удалённого.
    """
    counts = neo.rows(
        "MATCH (m:MetadataObject {id: $id}) "
        "OPTIONAL MATCH (m)-[:HAS_ATTRIBUTE]->(a:Attribute) "
        "OPTIONAL MATCH (m)-[:HAS_TABULAR_SECTION]->(ts:TabularSection) "
        "OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(tsa:Attribute) "
        "OPTIONAL MATCH (m)-[:HAS_FORM]->(f:Form) "
        "OPTIONAL MATCH (m)-[:HAS_VALUE]->(ev:EnumValue) "
        "RETURN count(DISTINCT a) + count(DISTINCT tsa) AS attrs, "
        "       count(DISTINCT ts) AS ts, count(DISTINCT f) AS forms, "
        "       count(DISTINCT ev) AS evs",
        {"id": meta_id},
    )
    row = counts[0] if counts else {"attrs": 0, "ts": 0, "forms": 0, "evs": 0}

    # Потомки висят через id-префикс "<meta_id>." — сносим по STARTS WITH.
    # (id-схема из metadata_xml.build_graph: "{meta_id}.Attr.X",
    #  "{meta_id}.TS.Y", "{meta_id}.TS.Y.Attr.Z", "{meta_id}.Form.F",
    #  "{meta_id}.Value.V".)
    prefix = meta_id + "."
    for label in _META_OBJECT_CHILD_LABELS:
        neo.query(
            f"MATCH (n:{label}) WHERE n.id STARTS WITH $prefix DETACH DELETE n",
            {"prefix": prefix},
        )
    # Сам объект.
    neo.query(
        "MATCH (m:MetadataObject {id: $id}) DETACH DELETE m",
        {"id": meta_id},
    )
    return {
        "deleted_attributes":       row["attrs"],
        "deleted_tabular_sections": row["ts"],
        "deleted_forms":            row["forms"],
        "deleted_enum_values":      row["evs"],
    }


def _filter_xml_graph_to_object(graph: dict, meta_id: str) -> dict:
    """
    Из полного graph (build_graph по одному объекту) вырезает срез,
    относящийся к указанному meta_id. Так как build_graph вызывается на
    списке из ОДНОГО объекта, почти всё уже «наше» — но RESOLVES_TO-рёбра
    второго прохода ссылаются на ДРУГИЕ объекты (которых нет в нашем
    однообъектном списке), поэтому build_graph их и не создаёт. Здесь же
    мы лишь оставляем всё как есть и пересобираем RESOLVES_TO против
    живого графа отдельно (в upsert_xml_file).
    """
    # build_graph на одном объекте уже даёт чистый срез — фильтрация не
    # нужна, возвращаем как есть. Функция оставлена для симметрии с
    # _filter_code_graph_to_module и как точка расширения.
    return graph


def upsert_xml_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Точечно переиндексирует один верхнеуровневый XML-файл в слой 1 графа.

    Шаги:
      1. Классифицировать путь → (kind_eng, ...).
      2. Спарсить ОДИН XML в MetaObject (_parse_object).
      3. build_graph на списке из одного объекта.
      4. Снести старый срез метаобъекта, записать новый.
      5. Досоздать RESOLVES_TO-рёбра от новых :Type-узлов к существующим
         :MetadataObject (build_graph их не создаёт — он видит только один
         объект; здесь резолвим против живого графа).
      6. Перепривязать входящие RESOLVES_TO: если ДРУГИЕ объекты ссылались
         на этот через :Type — рёбра :OF_TYPE/:RESOLVES_TO уцелели (мы
         сносили только потомков ЭТОГО объекта), достаточно убедиться, что
         :RESOLVES_TO к пересозданному :MetadataObject есть.
    """
    rel = _norm_rel(src_root, filepath)
    if rel is None:
        return {"status": "skipped", "reason": "path_outside_src_root",
                "filepath": filepath}

    abs_path = src_root / rel
    if abs_path.suffix.lower() != ".xml":
        return {"status": "skipped", "reason": "not_an_xml_file", "file": rel}

    if not abs_path.exists():
        return {"status": "skipped", "reason": "file_not_found", "file": rel}

    classified = _classify_xml_path(rel)
    if not classified:
        return {"status": "skipped", "reason": "not_a_toplevel_object_xml",
                "file": rel,
                "hint": "Поддерживаются только верхнеуровневые XML вида "
                        "Catalogs/Имя.xml; вложенные Forms/*.xml игнорируются"}
    dir_name, kind_eng, kind_ru, kind_ru_plural = classified

    obj = _parse_object(abs_path, kind_eng, kind_ru, kind_ru_plural, rel)
    if obj is None:
        return {"status": "error", "reason": "xml_parse_failed", "file": rel}

    meta_id = obj.full_name_eng
    graph = build_graph([obj])
    graph = _filter_xml_graph_to_object(graph, meta_id)

    # Снести старый срез, записать новый.
    cleared = _clear_meta_object_slice(neo, meta_id)

    ensure_schema(neo)
    # Compat-атрибуты на :MetadataObject строит write_graph; здесь нам нужен
    # лёгкий путь — пишем узлы напрямую. metadata_object_details читает
    # attributes_json — для полноты прогоняем тот же compat-расчёт, что
    # write_graph, но локально и компактно.
    _attach_compat_attrs(graph)

    write_type_nodes(neo, graph["type_nodes"])
    n_meta = write_meta_nodes(neo, graph["meta_nodes"])
    n_attr = write_attribute_nodes(neo, graph["attr_nodes"])
    n_ts   = write_tabular_section_nodes(neo, graph["ts_nodes"])
    n_form = write_form_nodes(neo, graph["form_nodes"])
    n_ev   = write_enum_value_nodes(neo, graph["enum_value_nodes"])

    # Рёбра из build_graph: HAS_ATTRIBUTE / HAS_TABULAR_SECTION / HAS_FORM /
    # HAS_VALUE / OF_TYPE / CONTAINS / PARENT_OF / OWNED_BY / BASED_ON /
    # REGISTERS. RESOLVES_TO build_graph НЕ создал (видел только один
    # объект) — досоздаём ниже отдельно.
    edge_counters = write_edges(neo, graph["edges"])

    # ── RESOLVES_TO: от всех :Type, что мы записали, к живым :MetadataObject ──
    # Type.target вида "Catalog.АукВидыАукционов" → ребро (:Type)-[:RESOLVES_TO]->(:MetadataObject {id: target}).
    resolves_written = neo.rows(
        "UNWIND $type_ids AS tid "
        "MATCH (t:Type {id: tid}) "
        "WHERE t.target IS NOT NULL AND t.target CONTAINS '.' "
        "MATCH (m:MetadataObject {id: t.target}) "
        "MERGE (t)-[:RESOLVES_TO]->(m) "
        "RETURN count(*) AS n",
        {"type_ids": [t["id"] for t in graph["type_nodes"]]},
    )
    n_resolves = resolves_written[0]["n"] if resolves_written else 0

    return {
        "status": "reindexed",
        "file": rel,
        "meta_id": meta_id,
        "kind_eng": kind_eng,
        "cleared": cleared,
        "written": {
            "MetadataObject": n_meta, "Attribute": n_attr,
            "TabularSection": n_ts, "Form": n_form, "EnumValue": n_ev,
            "Type": len(graph["type_nodes"]),
        },
        "edges_written": edge_counters,
        "resolves_to_rebuilt": n_resolves,
        "unresolved_refs": graph["stats"].get("unresolved_refs", 0),
    }


def remove_xml_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Удаляет из графа срез слоя 1 для одного верхнеуровневого XML-объекта.

    Сносит :MetadataObject и его потомков. :Type-узлы не трогаем (общие).
    Рёбра :RESOLVES_TO от :Type к этому объекту уйдут вместе с объектом
    (DETACH DELETE). Входящие :CONTAINS / :OWNED_BY и т.п. из других
    объектов тоже уйдут — но это корректно: объекта больше нет.
    """
    rel = _norm_rel(src_root, filepath)
    if rel is None:
        return {"status": "skipped", "reason": "path_outside_src_root",
                "filepath": filepath}

    if Path(rel).suffix.lower() != ".xml":
        return {"status": "skipped", "reason": "not_an_xml_file", "file": rel}

    classified = _classify_xml_path(rel)
    if not classified:
        return {"status": "skipped", "reason": "not_a_toplevel_object_xml",
                "file": rel}
    dir_name, kind_eng, kind_ru, kind_ru_plural = classified

    # meta_id восстанавливаем из пути: "Catalog.АукАукционы".
    name = Path(rel).stem
    meta_id = f"{kind_eng}.{name}"

    cleared = _clear_meta_object_slice(neo, meta_id)
    return {
        "status": "removed",
        "file": rel,
        "meta_id": meta_id,
        "kind_eng": kind_eng,
        "cleared": cleared,
    }


def _attach_compat_attrs(graph: dict) -> None:
    """
    Прицепляет `_attrs_for_compat` к meta_nodes — компактная копия логики
    из graph_writer.write_graph (build_compat_attrs=True), чтобы
    metadata_object_details после инкремента показывал реквизиты.
    """
    type_kind_to_compat = {
        "CatalogRef": "СправочникСсылка", "DocumentRef": "ДокументСсылка",
        "EnumRef": "ПеречислениеСсылка",
        "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
        "ChartOfAccountsRef": "ПланСчетовСсылка",
        "ChartOfCalculationTypesRef": "ПланВидовРасчетаСсылка",
        "BusinessProcessRef": "БизнесПроцессСсылка",
        "TaskRef": "ЗадачаСсылка", "ExchangePlanRef": "ПланОбменаСсылка",
        "DocumentJournalRef": "ЖурналДокументовСсылка",
        "CatalogObject": "СправочникОбъект", "DocumentObject": "ДокументОбъект",
        "String": "Строка", "Number": "Число", "Date": "Дата",
        "Boolean": "Булево", "UUID": "УникальныйИдентификатор",
        "ValueStorage": "ХранилищеЗначения",
        "Reference": "Ссылка", "Unknown": "",
    }
    type_node_by_id = {t["id"]: t for t in graph["type_nodes"]}
    of_type_by_attr: dict[str, list[str]] = {}
    for e in graph["edges"]:
        if e["rel"] == "OF_TYPE":
            of_type_by_attr.setdefault(e["src"], []).append(e["dst"])

    def compat_type(attr_id: str) -> str:
        parts = []
        for tid in of_type_by_attr.get(attr_id, []):
            t = type_node_by_id.get(tid)
            if not t:
                continue
            ru = type_kind_to_compat.get(t["kind"], t["kind"])
            if t.get("target"):
                short = t["target"].split(".", 1)[-1]
                parts.append(f"{ru}.{short}" if ru else short)
            else:
                parts.append(ru)
        return "; ".join([p for p in parts if p])

    attr_by_parent: dict[str, list[dict]] = {}
    for a in graph["attr_nodes"]:
        ac = dict(a)
        ac["type_compat"] = compat_type(a["id"])
        attr_by_parent.setdefault(a["parent"], []).append(ac)
    for n in graph["meta_nodes"]:
        attrs = attr_by_parent.get(n["id"], [])
        n["_attrs_for_compat"] = [a for a in attrs if a.get("role") != "_internal"]


# ─── Единая точка входа (диспетчер по расширению) ─────────────────────────


def upsert_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Диспетчер: по расширению выбирает BSL- или XML-апдейт.
    Используется MCP-tool `metadata_upsert_file`.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".bsl":
        return upsert_bsl_file(neo, src_root, filepath)
    if ext == ".xml":
        return upsert_xml_file(neo, src_root, filepath)
    return {"status": "skipped", "reason": "unsupported_extension",
            "filepath": filepath}


def remove_file(neo: Neo4j, src_root: Path, filepath: str) -> dict:
    """
    Диспетчер: по расширению выбирает BSL- или XML-удаление.
    Используется MCP-tool `metadata_remove_file`.
    """
    ext = Path(filepath).suffix.lower()
    if ext == ".bsl":
        return remove_bsl_file(neo, src_root, filepath)
    if ext == ".xml":
        return remove_xml_file(neo, src_root, filepath)
    return {"status": "skipped", "reason": "unsupported_extension",
            "filepath": filepath}
