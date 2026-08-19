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
  METADATA_FORCE_REINDEX    false                игнорировать ОБА fingerprint'а
  METADATA_FORCE_XML        false                форс только фазы 1 (CFG-3)
  METADATA_FORCE_BSL        false                форс только фазы 2 (CFG-3)
  METADATA_FINGERPRINT_STRICT false              fingerprint по содержимому (PERF-3)
  METADATA_CONFIG_NAME      Конфигурация        имя для узла :Configuration
  METADATA_SKIP_BSL         false                пропустить фазу 2 (R&D-режим)
  METADATA_BSL_LOG_LEVEL    (наследует)          отдельный log level для BSL-фазы

CFG-3. Раздельный форс по фазам. Правка в парсере BSL требует переиндексации
только слоя 2, но METADATA_FORCE_REINDEX гнал обе фазы и заодно переписывал
слой 1 — на боевой конфигурации это 28 минут впустую. Обход существовал и им
пользовались: удалить узел :Fingerprint {kind:'bsl_source'} запросом к базе и
запустить без форса. То, что штатный сценарий делался ручным запросом к
графу, — само по себе диагноз.

PERF-3. Fingerprint по (путь, размер, mtime) вместо sha256 содержимого; см.
graph_writer. Обход дерева теперь один на оба расширения, а не два.
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
    FP_MODE_CONTENT, FP_MODE_STAT,
    Neo4j, clear_code_layer, clear_metadata_layer,
    fingerprint_get_meta, fingerprint_matches, fingerprint_workspace_multi,
    fingerprint_write, relations_snapshot_write, write_code_graph, write_graph,
)
from progress_log import human_bytes, human_sec
from bsl_parser import walk_workspace_bsl
from bsl_resolver import build_call_graph, build_index_from_neo4j

# A-2: сверка «вход против выхода» по всем шагам, где вход известен.
# FIX-27: проверка результата — сколько процедур осталось без владельца.
try:
    from shortfall import TallyBook
    from graph_integrity import (
        HAS_METHOD_COUNT_CYPHER, STATE_OK, log_ownership, ownership_report,
    )
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shortfall import TallyBook
    from graph_integrity import (
        HAS_METHOD_COUNT_CYPHER, STATE_OK, log_ownership, ownership_report,
    )


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("metadata-indexer")
log_bsl = logging.getLogger("metadata-indexer.bsl")

# A-1/A-2. Одна книга счётчиков на прогон: каждый шаг, у которого известен
# вход, кладёт сюда свою пару чисел, а в конце печатается сводка.
#
# Смысл именно в сводке. Отдельный шаг может отчитаться честно, а прогон в
# целом — потерять треть данных на трёх шагах по одиннадцать процентов:
# ни один из трёх не выглядит поломкой, и заметить это можно только когда
# все пары чисел стоят рядом.
TALLIES = TallyBook(log)


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
    # A-1: раньше здесь печаталось только число объектов, а защита стояла
    # ровно от одного случая — «не нашлось вообще ничего». Потеря 30%
    # файлов была неотличима от нормы: сколько *.xml лежало на диске, не
    # знал никто.
    xml_tally = TALLIES.stage("разбор XML метаданных", unit="файл", log=log)
    objects = walk_workspace(src_dir, tally=xml_tally)
    log.info("  ✓ объектов: %d (за %.2f с)", len(objects), time.time() - t0)
    xml_tally.report(hint="проверьте выгрузку: конфигуратор битых XML не даёт")

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
    bsl_tally = TALLIES.stage("разбор BSL", unit="файл", log=log_bsl,
                              min_keep_ratio=0.9)
    modules = walk_workspace_bsl(src_dir, modules_info=modules_info,
                                 tally=bsl_tally)
    n_procs = sum(len(m.procedures) for m in modules)
    log_bsl.info("  ✓ модулей: %d, процедур/функций: %d (за %.2f с)",
                 len(modules), n_procs, time.time() - t0)
    bsl_tally.report(hint="схема путей — classify_bsl_path() в bsl_parser.py")

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
    # A-1: сборка графа тоже шаг с известным входом. len(modules) и
    # n_procs посчитаны выше, а наружу шли только узлы. Расхождение здесь
    # означает, что модуль разобрался, но в граф не попал, — раньше это
    # было видно только сверкой двух строк лога глазами.
    #
    # FIX-28. Сводка говорила «вход 14 679 → выход 14 048» и сама называла
    # главное: «в шаге есть выход, который никто не считает». Выходов
    # оказалось два, и оба законны — но законность надо объявить, иначе
    # 631 модуль выглядит потерей и разбирается заново каждый раз.
    mod_tally = TALLIES.stage("модули → узлы :Module", unit="модуль", log=log_bsl)
    mod_tally.see(len(modules))
    mod_tally.keep(s["module_nodes"])
    if s.get("module_nodes_common"):
        mod_tally.drop(
            "общий модуль — узел слоя 1 уже есть, метка :Module ему не нужна "
            "(с ней он выпал бы из metadata_search, см. фильтр NOT n:Module)",
            n=s["module_nodes_common"])
    if s.get("module_nodes_duplicate"):
        # Дубль module_id — единственный по-настоящему тревожный выход:
        # процедуры проигравшего модуля схлопываются MERGE'ем по callable_id,
        # и в графе остаётся код одного файла под именем двух.
        for example in s.get("module_duplicate_examples") or []:
            mod_tally.drop("дубль module_id — два .bsl на один модуль",
                           example=example, n=0, alarm=True)
        mod_tally.drop("дубль module_id — два .bsl на один модуль",
                       n=s["module_nodes_duplicate"], alarm=True)
    mod_tally.report(hint="схема путей — classify_bsl_path() в bsl_parser.py")

    proc_tally = TALLIES.stage("процедуры → узлы :Callable", unit="процедура",
                               log=log_bsl)
    proc_tally.see(n_procs)
    proc_tally.keep(s["callable_nodes"])
    proc_tally.report()

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


# ─── FIX-27: проверка результата, а не шага ──────────────────────────────


def check_ownership(neo: Neo4j) -> dict:
    """
    Сколько процедур в графе осталось без владельца — прямо сейчас.

    Три счётчика, каждый O(1) из счётчиков хранилища Neo4j. Считается
    ПОСЛЕ записи и независимо от неё: `FIX-15` сторожит запись («отправлено
    столько, записано столько»), а здесь сторожится результат. Разница
    ровно та, из-за которой FIX-27 прожил незамеченным несколько дней:
    слой владения снесла фаза 1, а фаза 2, которая его пересоздаёт, не
    запустилась — и оба шага отчитались честно.
    """
    rows = neo.rows(
        "MATCH (n:Callable) RETURN count(n) AS callables "
    )
    callables = rows[0]["callables"] if rows else 0
    rows = neo.rows("MATCH (n:Module) RETURN count(n) AS modules")
    modules = rows[0]["modules"] if rows else 0
    rows = neo.rows(HAS_METHOD_COUNT_CYPHER)
    has_method = rows[0]["c"] if rows else 0
    rows = neo.rows("MATCH (n:MetadataObject) RETURN count(n) AS objects")
    objects = rows[0]["objects"] if rows else 0
    # `objects` приезжает внутри отчёта: по нему решается, чем чинить —
    # одной фазой 2 или полным прогоном.
    return ownership_report(callables, modules, has_method, objects=objects)


# ─── Main pipeline ────────────────────────────────────────────────────────


def main() -> int:
    src_dir   = Path(os.environ.get("METADATA_SRC_DIR", "/data/1c-src"))
    neo4j_url = os.environ.get("NEO4J_URL",  "http://neo4j:7474")
    neo4j_usr = os.environ.get("NEO4J_USER", "neo4j")
    # SEC-2: без дефолта, единое имя NEO4J_PASSWORD (как в docker-compose.yml)
    neo4j_pwd = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_PASS")
    if not neo4j_pwd:
        raise SystemExit("NEO4J_PASSWORD не задан — индексация не запускается (SEC-2)")
    # CFG-3: METADATA_FORCE_REINDEX сохранён как «обе фазы» для совместимости
    # с уже написанными скриптами и с docker-compose; поверх него — два
    # отдельных флага.
    force_all = _env_bool("METADATA_FORCE_REINDEX", False)
    force_xml = force_all or _env_bool("METADATA_FORCE_XML", False)
    force_bsl = force_all or _env_bool("METADATA_FORCE_BSL", False)
    strict_fp = _env_bool("METADATA_FINGERPRINT_STRICT", False)
    skip_bsl  = _env_bool("METADATA_SKIP_BSL", False)
    cfg_name  = os.environ.get("METADATA_CONFIG_NAME", "Конфигурация")
    bsl_log_level = os.environ.get("METADATA_BSL_LOG_LEVEL", "").strip().upper()
    if bsl_log_level:
        log_bsl.setLevel(bsl_log_level)

    t_total = time.time()

    log.info("=" * 60)
    log.info("Индексер метаданных 1С (v3.1, двухфазный)")
    log.info("=" * 60)
    log.info("Источник:        %s", src_dir)
    log.info("Neo4j:           %s", neo4j_url)
    log.info("METADATA_FORCE_REINDEX: %s (XML: %s, BSL: %s)",
             force_all, force_xml, force_bsl)
    log.info("METADATA_FINGERPRINT_STRICT: %s", strict_fp)
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

    # ─ Считаем оба fingerprint'а за один обход дерева (PERF-3) ──────
    log.info("Считаем fingerprint workspace…")
    digests, fp_meta = fingerprint_workspace_multi(
        src_dir, (".xml", ".bsl"), strict=strict_fp,
    )
    fp_mode = fp_meta["mode"]
    fp_xml_new = digests[".xml"]
    fp_bsl_new = digests[".bsl"]
    log.info(
        "  ✓ режим=%s, файлов %d (xml %d, bsl %d), %s, за %s",
        fp_mode, fp_meta["files"],
        fp_meta["by_suffix"].get(".xml", 0), fp_meta["by_suffix"].get(".bsl", 0),
        human_bytes(fp_meta["bytes"]), human_sec(fp_meta["elapsed_sec"]),
    )
    log.info("  ✓ xml=%s…, bsl=%s…", fp_xml_new[:8], fp_bsl_new[:8])
    if fp_mode == FP_MODE_STAT:
        # PERF-3, обратная сторона режима: копирование выгрузки утилитой,
        # сохраняющей mtime, останется незамеченным. Свежайший mtime в логе
        # даёт зацепку — если он старше самой правки, ищите здесь.
        log.info("  ✓ самый свежий mtime в выгрузке: %s",
                 time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(fp_meta["newest_mtime"]))
                 if fp_meta["newest_mtime"] else "—")
        log.info("  При сомнениях (копия выгрузки, переключение ветки): "
                 "METADATA_FINGERPRINT_STRICT=true")

    fp_xml_old = fingerprint_get_meta(neo, "metadata_xml")
    fp_bsl_old = fingerprint_get_meta(neo, "bsl_source")

    xml_same, xml_why = fingerprint_matches(fp_xml_old, fp_xml_new, fp_mode)
    bsl_same, bsl_why = fingerprint_matches(fp_bsl_old, fp_bsl_new, fp_mode)

    xml_needs_reindex = (not xml_same) or force_xml
    bsl_needs_reindex = (not bsl_same) or force_bsl

    # ─ FIX-27. Отпечаток говорит про ИСТОЧНИК, а не про граф ─────────
    #
    # «Оба fingerprint совпали» означает ровно одно: файлы выгрузки не
    # менялись с прошлого прогона. Про то, чем этот прогон кончился, здесь
    # не сказано ничего — и именно на этом месте разрушенный граф шесть
    # дней подряд объявлялся актуальным.
    #
    # Сценарий, который это делал: фаза 1 переиндексировала XML (снеся
    # :Module-узлы и все рёбра владения), а фаза 2 не отработала — упала,
    # была снята по памяти или пропущена METADATA_SKIP_BSL. Отпечаток слоя
    # BSL при этом остался от прошлого успешного прогона, файлы .bsl с тех
    # пор не менялись, — значит, «совпал», значит «делать нечего».
    #
    # Поэтому перед выходом спрашиваем не отпечаток, а граф.
    ownership = check_ownership(neo)
    if not xml_needs_reindex and not bsl_needs_reindex:
        if ownership["state"] == STATE_OK:
            log.info("Оба fingerprint совпали — данные актуальны, выход.")
            log_ownership(ownership, log)
            log.info("Для принудительной переиндексации: METADATA_FORCE_REINDEX=true "
                     "(обе фазы), METADATA_FORCE_XML / METADATA_FORCE_BSL (по одной)")
            return 0
        log.warning("Оба fingerprint совпали, но граф не в порядке:")
        log_ownership(ownership, log)
        if not ownership.get("objects"):
            # Слоя 1 нет вовсе — базу очистили или она новая. Фазе 2 не на
            # что опереться (она сама откажется, см. её pre-flight), значит
            # нужен полный прогон.
            log.warning("Слоя метаданных в графе тоже нет — прогоняем обе "
                        "фазы, отпечатки при этом ни при чём.")
            xml_needs_reindex = True
        else:
            log.warning("Отпечаток говорит про выгрузку, а не про граф. "
                        "Запускаю фазу 2 — она пересоберёт слой кода и связи "
                        "владения; выгрузка при этом не перечитывается.")
        bsl_needs_reindex = True
    elif ownership["state"] != STATE_OK:
        # Прогон и так будет, но сказать о состоянии «до» стоит: по паре
        # строк «до» и «после» видно, починил прогон что-нибудь или нет.
        log.info("Состояние графа до прогона:")
        log_ownership(ownership, log)

    # ─ Фаза 1 (XML) ─────────────────────────────────────────
    # FIX-27: отпечаток слоя XML ждёт успешной фазы 2 — см. ниже.
    xml_fp_pending = False
    if xml_needs_reindex:
        if xml_same:
            log.info("Фаза 1: %s, но форс включён — переиндексация", xml_why)
        else:
            log.info("Фаза 1: %s — переиндексация", xml_why)
        rc = run_xml_phase(neo, src_dir, cfg_name)
        if rc != 0:
            return rc
        # После clear_metadata_layer Module-узлы исчезли — фаза 2 ОБЯЗАНА пройти.
        # Это не «заодно», а обязательство: см. PLAN_4_6_2.md «Грабля 3».
        # CFG-3 не отменяет его — METADATA_FORCE_XML в одиночку всё равно
        # тянет за собой фазу 2, иначе граф останется без слоя кода.
        if not bsl_needs_reindex:
            log_bsl.info("Фаза 2 запускается принудительно: переиндексация XML "
                         "снесла :Module-узлы, без неё слой кода останется пустым")
        bsl_needs_reindex = True
        # FIX-27. Здесь стояло `fingerprint_write(...)` — отпечаток слоя XML
        # сохранялся СРАЗУ, до фазы 2. Тем самым прогон объявлял слой
        # актуальным в момент, когда граф заведомо неполон: :Module-узлы
        # только что снесены, а пересоздать их должна следующая фаза.
        #
        # Пока фаза 2 отрабатывала, это сходило с рук. Стоило ей не
        # отработать — и следующий старт видел два совпавших отпечатка и
        # выходил, оставляя граф без слоя владения навсегда.
        #
        # Теперь отпечаток пишется ПОСЛЕ фазы 2, вместе с её собственным.
        # Цена ошибки поменялась местами: сбой посреди прогона стоит
        # повторной фазы 1 (минуты), а не молчаливой потери связи «объект →
        # его методы» (дни).
        xml_fp_pending = True
    else:
        log.info("Фаза 1 пропущена: %s", xml_why)

    # ─ Фаза 2 (BSL) ─────────────────────────────────────────
    if skip_bsl:
        log_bsl.warning("METADATA_SKIP_BSL=true — фаза 2 пропущена (R&D)")
        if xml_fp_pending:
            # Тот же FIX-27, вторая дверь. Пропуск фазы 2 после
            # переиндексации XML оставляет граф без :Module-узлов и без
            # единого ребра HAS_METHOD. Раньше отпечаток при этом уже был
            # сохранён, и починить это обычным запуском было нельзя — он
            # говорил «данные актуальны».
            log_bsl.warning("Слой владения кодом снесён фазой 1 и не "
                            "восстановлен: %s",
                            check_ownership(neo)["message"])
            log_bsl.warning("Отпечаток XML НЕ сохранён — обычный следующий "
                            "запуск (без METADATA_SKIP_BSL) пересоберёт слой "
                            "полностью. Это намеренно.")
        return 0

    if bsl_needs_reindex:
        if bsl_same and force_bsl:
            log_bsl.info("Фаза 2: %s, но форс включён — переиндексация", bsl_why)
        elif bsl_same:
            log_bsl.info("Фаза 2: %s", bsl_why)
        else:
            log_bsl.info("Фаза 2: %s — переиндексация", bsl_why)
        rc = run_bsl_phase(neo, src_dir)
        if rc != 0:
            if xml_fp_pending:
                log.error("Фаза 2 не отработала (код %d) — отпечаток XML не "
                          "сохранён, следующий запуск повторит обе фазы.", rc)
            return rc
        fingerprint_write(neo, fp_bsl_new, "bsl_source", mode=fp_mode)
        log_bsl.info("  bsl fingerprint сохранён (режим %s)", fp_mode)
        if xml_fp_pending:
            fingerprint_write(neo, fp_xml_new, "metadata_xml", mode=fp_mode)
            log.info("  xml fingerprint сохранён (режим %s) — после фазы 2, "
                     "когда слой владения уже восстановлен (FIX-27)", fp_mode)
            xml_fp_pending = False
    else:
        log_bsl.info("Фаза 2 пропущена: %s", bsl_why)
        if xml_fp_pending:  # pragma: no cover — сюда попасть нельзя
            log.error("Внутренняя ошибка: фаза 1 отработала, фаза 2 — нет, "
                      "а отпечаток XML остался несохранённым. Граф без слоя "
                      "владения; запустите прогон заново.")
            return 4

    # PERF-12. Снимок счётчиков рёбер: обход всех рёбер платит индексация,
    # раз за прогон, а не `metadata_stats` на каждом вызове.
    try:
        rel_counts = relations_snapshot_write(neo)
        log.info("Снимок рёбер по типам обновлён: %d типов, %d рёбер",
                 len(rel_counts), sum(rel_counts.values()))
    except Exception as e:  # снимок — удобство, а не условие успеха
        log.warning("Снимок рёбер не обновлён (%s) — metadata_stats посчитает "
                    "их сам, дороже", e)

    # FIX-27. Итоговая проверка результата. Печатается ВСЕГДА, в том числе
    # когда всё сошлось: «владение на месте, без владельца 28 из 231 114» —
    # это утверждение, которое можно сравнить со следующим прогоном.
    log.info("Проверка целостности:")
    log_ownership(check_ownership(neo), log)

    log.info("=" * 60)
    # A-1/A-2: сводка потерь. Печатается ВСЕГДА, в том числе когда всё
    # сошлось: строка «✓ шагов с потерями: 0» — это утверждение, которое
    # можно сравнить со следующим прогоном. Отсутствие строки утверждением
    # не является.
    bad = TALLIES.report(log)
    if bad:
        log.warning("Прогон завершён, но %d шаг(ов) потеряли данные — "
                    "смотрите строки ⚠ выше", bad)
    log.info("✓ Готово! Полный прогон занял %s", human_sec(time.time() - t_total))
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
