"""
Индексер метаданных 1С → Neo4j (v3.1, двухфазный).
=====================================================
Заменяет однофазный v3-индексер. Сохранена обратная совместимость в
поведении первой фазы (XML).

Источник правды:
  • Фаза 1 (XML)  — XML-выгрузка конфигурации в METADATA_SRC_DIR.
  • Фаза 2 (BSL)  — .bsl-файлы в том же дереве.

Идемпотентность через два независимых fingerprint'а:
  :Fingerprint {kind: 'metadata_xml'}   — для слоя 1
  :Fingerprint {kind: 'bsl_source'}     — для слоя 2

Если изменился XML-fingerprint, фаза 1 переиндексирует, и при этом
clear_metadata_layer сносит :MetadataObject:Module-узлы. Поэтому фаза 2
ОБЯЗАНА запуститься после переиндексации XML, чтобы Module-узлы пересоздались.
(см. PLAN_4_6_2.md «Грабля 3».)

Env:
  METADATA_SRC_DIR          /data/1c-src         корень выгрузки
  NEO4J_URL                 http://neo4j:7474
  NEO4J_USER                neo4j
  NEO4J_PASSWORD            (обязательна, дефолта нет)
  METADATA_FORCE_REINDEX    false                игнорировать оба fingerprint'а
  METADATA_CONFIG_NAME      Конфигурация        имя для узла :Configuration
  METADATA_SKIP_BSL         false                пропустить фазу 2 (R&D-режим)
  METADATA_BSL_LOG_LEVEL    (наследует)          отдельный log level для BSL-фазы
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Импорты соседних модулей.
from metadata_xml import walk_workspace, build_graph
from graph_writer import (
    Neo4j, clear_code_layer, clear_metadata_layer,
    fingerprint_get, fingerprint_workspace_files, fingerprint_write,
    write_code_graph, write_graph,
)
from bsl_parser import walk_workspace_bsl
from bsl_resolver import build_call_graph, build_index_from_neo4j


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("metadata-indexer")
log_bsl = logging.getLogger("metadata-indexer.bsl")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# ─── Фаза 1 (XML) ────────────────────────────────────────────────────────


def run_xml_phase(neo: Neo4j, src_dir: Path, cfg_name: str) -> int:
    """Выполняет фазу 1. Возвращает 0 при успехе, не-0 при ошибке."""
    log.info("Парсим XML…")
    t0 = time.time()
    objects = walk_workspace(src_dir)
    log.info("  ✓ объектов: %d (за %.2f с)", len(objects), time.time() - t0)

    if not objects:
        log.error("XML-парсер ничего не нашёл — выход")
        return 2

    log.info("Собираем граф…")
    t0 = time.time()
    graph = build_graph(objects)
    s = graph["stats"]
    log.info("  ✓ узлов %d + %d + %d + %d + %d + %d; рёбер %d (за %.2f с)",
             s["meta_objects"], s["attributes"], s["tabular_sections"],
             s["forms"], s["enum_values"], s["type_nodes"],
             s["edges_total"], time.time() - t0)

    if graph["unresolved"]:
        log.warning("Неразрешённых ссылок: %d", s["unresolved_refs"])
        for ref, c in sorted(graph["unresolved"].items(), key=lambda x: -x[1])[:10]:
            log.warning("  %s: %d", ref, c)

    log.info("Очищаем прежний слой метаданных в Neo4j…")
    deleted = clear_metadata_layer(neo)
    log.info("  ✓ удалено: %d", deleted["deleted_nodes"])

    log.info("Пишем в Neo4j (батчи UNWIND)…")
    t0 = time.time()
    summary = write_graph(neo, graph, config_name=cfg_name)
    log.info("  ✓ записано за %.2f с", time.time() - t0)
    log.info("  узлы: %s", summary["nodes_written"])
    log.info("  рёбра: %s", summary["edges_written"])
    return 0


# ─── Фаза 2 (BSL): подготовка кода-графа ─────────────────────────────────


def _build_modules_info_from_neo4j(neo: Neo4j) -> dict[str, dict]:
    """
    Читает свойства :CommonModule из Neo4j (server/client флаги).

    Возвращает `module_id` → `{is_server, is_client}`.

    Источник — `properties_json` на узле, который заполняется парсером XML
    в фазе 1. Если поле отсутствует (старый граф) — возвращает пустой dict
    для этого модуля, и BSL-парсер берёт дефолты.
    """
    rows = neo.rows(
        "MATCH (m:MetadataObject:CommonModule) "
        "RETURN m.id AS id, m.properties_json AS props"
    )
    result: dict[str, dict] = {}
    for r in rows:
        info: dict = {}
        props_raw = r.get("props") or "{}"
        try:
            props = json.loads(props_raw) if isinstance(props_raw, str) else (props_raw or {})
        except (TypeError, ValueError):
            props = {}
        # Поля XML CommonModule.xml:
        info["is_server"] = bool(props.get("Server", True))
        info["is_client"] = bool(
            props.get("ClientManagedApplication", False)
            or props.get("ClientOrdinaryApplication", False)
        )
        result[r["id"]] = info
    return result


def run_bsl_phase(neo: Neo4j, src_dir: Path) -> int:
    """Выполняет фазу 2. Возвращает 0 при успехе, не-0 при ошибке."""
    # Pre-flight: слой 1 ДОЛЖЕН быть в графе. См. PLAN_4_6_2.md «Грабля 2».
    rows = neo.rows("MATCH (m:MetadataObject) RETURN count(m) AS n")
    meta_count = rows[0]["n"] if rows else 0
    if not meta_count:
        log_bsl.error("Слой 1 (MetadataObject) пуст. Сначала запустите XML-фазу.")
        return 3
    log_bsl.info("Слой 1 присутствует: %d :MetadataObject", meta_count)

    # Читаем индекс свойств CommonModule (для is_server/is_client).
    log_bsl.info("Читаем свойства :CommonModule из Neo4j…")
    modules_info = _build_modules_info_from_neo4j(neo)
    log_bsl.info("  ✓ модулей: %d", len(modules_info))

    log_bsl.info("Парсим BSL…")
    t0 = time.time()
    modules = walk_workspace_bsl(src_dir, modules_info=modules_info)
    n_procs = sum(len(m.procedures) for m in modules)
    log_bsl.info("  ✓ модулей: %d, процедур/функций: %d (за %.2f с)",
                 len(modules), n_procs, time.time() - t0)

    log_bsl.info("Строим индекс резолвера из Neo4j + модулей…")
    t0 = time.time()
    index = build_index_from_neo4j(neo, modules)
    log_bsl.info("  ✓ common_modules=%d, callable_ids=%d, metadata_objects=%d (за %.2f с)",
                 len(index.common_modules), len(index.callable_ids),
                 len(index.metadata_full_set), time.time() - t0)

    log_bsl.info("Собираем code_graph с резолвом (Day-2)…")
    t0 = time.time()
    code_graph = build_call_graph(modules, index)
    s = code_graph["stats"]
    log_bsl.info(
        "  ✓ узлов Module=%d, Callable=%d, Parameter=%d, CallSite=%d, Type=%d; "
        "рёбер %d (за %.2f с)",
        s["module_nodes"], s["callable_nodes"], s["parameter_nodes"],
        s["callsite_nodes"], s.get("type_nodes", 0), s["edges_total"],
        time.time() - t0,
    )
    # FIX-4: unresolved разложен на настоящие пробелы и методы объектов
    # платформы. Второе — не дефект графа, поэтому в покрытие не входит.
    log_bsl.info(
        "  резолв: resolved=%d, unresolved=%d (пробелы=%d, методы объектов=%d), "
        "skipped(built-in/metadata)=%d",
        s["resolved"], s["unresolved"],
        s.get("unresolved_gaps", s["unresolved"]),
        s.get("unresolved_object_method", 0),
        s["skipped"],
    )
    # 4.6.4: метрики type inference v2 — coverage, inter-procedural, фикс-пойнт.
    log_bsl.info(
        "  coverage=%.2f%%; :INFERRED_TYPE=%d; :Type(слой2)=%d; "
        "фикс-пойнт: %d итер.",
        s.get("resolve_coverage_pct", 0.0),
        s.get("inferred_types", 0), s.get("type_nodes", 0),
        s.get("fixpoint_iterations", 0),
    )
    if s.get("reason_counts"):
        log_bsl.info("  top reasons:")
        for reason, n in sorted(s["reason_counts"].items(), key=lambda x: -x[1])[:8]:
            log_bsl.info("    %s: %d", reason, n)

    log_bsl.info("Очищаем прежний слой кода в Neo4j…")
    deleted = clear_code_layer(neo)
    log_bsl.info("  ✓ удалено: %d", deleted["deleted_nodes"])

    log_bsl.info("Пишем в Neo4j (батчи UNWIND)…")
    t0 = time.time()
    summary = write_code_graph(neo, code_graph)
    log_bsl.info("  ✓ записано за %.2f с", time.time() - t0)
    log_bsl.info("  узлы: %s", summary["nodes_written"])
    log_bsl.info("  рёбра: %s", summary["edges_written"])
    return 0


# ─── Main pipeline ────────────────────────────────────────────────────────


def main() -> int:
    src_dir   = Path(os.environ.get("METADATA_SRC_DIR", "/data/1c-src"))
    neo4j_url = os.environ.get("NEO4J_URL",  "http://neo4j:7474")
    neo4j_usr = os.environ.get("NEO4J_USER", "neo4j")
    # SEC-2: без дефолта, единое имя NEO4J_PASSWORD (как в docker-compose.yml)
    neo4j_pwd = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_PASS")
    if not neo4j_pwd:
        raise SystemExit("NEO4J_PASSWORD не задан — индексация не запускается (SEC-2)")
    force     = _env_bool("METADATA_FORCE_REINDEX", False)
    skip_bsl  = _env_bool("METADATA_SKIP_BSL", False)
    cfg_name  = os.environ.get("METADATA_CONFIG_NAME", "Конфигурация")
    bsl_log_level = os.environ.get("METADATA_BSL_LOG_LEVEL", "").strip().upper()
    if bsl_log_level:
        log_bsl.setLevel(bsl_log_level)

    log.info("=" * 60)
    log.info("Индексер метаданных 1С (v3.1, двухфазный)")
    log.info("=" * 60)
    log.info("Источник:        %s", src_dir)
    log.info("Neo4j:           %s", neo4j_url)
    log.info("METADATA_FORCE_REINDEX: %s", force)
    # Имя переменной в подписи полное: короткое `SKIP_BSL` в логе
    # провоцировало передавать `-e SKIP_BSL=true`, что не работает —
    # проверено на боевом прогоне, где фаза 2 запустилась вопреки намерению.
    log.info("METADATA_SKIP_BSL: %s", skip_bsl)

    if not src_dir.is_dir():
        log.error("Каталог %s не существует или недоступен", src_dir)
        return 1

    if not (src_dir / "Configuration.xml").exists():
        log.warning("В %s не найден Configuration.xml — возможно, это не корень выгрузки",
                    src_dir)

    neo = Neo4j(neo4j_url, neo4j_usr, neo4j_pwd)
    log.info("Ожидание Neo4j…")
    neo.wait(timeout=120)
    log.info("  ✓ доступен")

    # ─ Считаем оба fingerprint'а ────────────────────────────
    log.info("Считаем fingerprint workspace…")
    t0 = time.time()
    fp_xml_new = fingerprint_workspace_files(src_dir, ".xml")
    fp_bsl_new = fingerprint_workspace_files(src_dir, ".bsl")
    log.info("  ✓ xml=%s…, bsl=%s… (за %.2f с)",
             fp_xml_new[:8], fp_bsl_new[:8], time.time() - t0)

    fp_xml_old = fingerprint_get(neo, "metadata_xml")
    fp_bsl_old = fingerprint_get(neo, "bsl_source")

    xml_needs_reindex = (fp_xml_old != fp_xml_new) or force
    bsl_needs_reindex = (fp_bsl_old != fp_bsl_new) or force

    if not xml_needs_reindex and not bsl_needs_reindex:
        log.info("Оба fingerprint совпали — данные актуальны, выход.")
        log.info("Для принудительной переиндексации: METADATA_FORCE_REINDEX=true")
        return 0

    # ─ Фаза 1 (XML) ─────────────────────────────────────────
    if xml_needs_reindex:
        if force and fp_xml_old == fp_xml_new:
            log.info("XML-fingerprint совпал, но FORCE_REINDEX=true — переиндексация")
        elif fp_xml_old:
            log.info("XML-fingerprint изменился (%s… → %s…) — переиндексация",
                     fp_xml_old[:8], fp_xml_new[:8])
        else:
            log.info("XML-fingerprint отсутствует — первая индексация")
        rc = run_xml_phase(neo, src_dir, cfg_name)
        if rc != 0:
            return rc
        # После clear_metadata_layer Module-узлы исчезли — фаза 2 ОБЯЗАНА пройти.
        bsl_needs_reindex = True
        fingerprint_write(neo, fp_xml_new, "metadata_xml")
        log.info("  xml fingerprint сохранён")
    else:
        log.info("XML-fingerprint совпал — фаза 1 пропущена")

    # ─ Фаза 2 (BSL) ─────────────────────────────────────────
    if skip_bsl:
        log_bsl.warning("METADATA_SKIP_BSL=true — фаза 2 пропущена (R&D)")
        return 0

    if bsl_needs_reindex:
        if force and fp_bsl_old == fp_bsl_new:
            log_bsl.info("BSL-fingerprint совпал, но FORCE_REINDEX=true — переиндексация")
        elif fp_bsl_old:
            log_bsl.info("BSL-fingerprint изменился (%s… → %s…) — переиндексация",
                         fp_bsl_old[:8], fp_bsl_new[:8])
        else:
            log_bsl.info("BSL-fingerprint отсутствует — первая индексация")
        rc = run_bsl_phase(neo, src_dir)
        if rc != 0:
            return rc
        fingerprint_write(neo, fp_bsl_new, "bsl_source")
        log_bsl.info("  bsl fingerprint сохранён")
    else:
        log_bsl.info("BSL-fingerprint совпал — фаза 2 пропущена")

    log.info("=" * 60)
    log.info("✓ Готово!")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
