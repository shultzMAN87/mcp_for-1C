#!/usr/bin/env python3
"""
PERF-8, шаг 2 — применить изменения точечно, без полной пересборки.
====================================================================

Что делает: обходит выгрузку, сравнивает с сохранённым состоянием и
обновляет в графе только изменившиеся единицы через `incremental.py`.

    docker compose run --rm --entrypoint python metadata-indexer \\
        /app/apply_changes.py --dry-run     # показать, ничего не делая
    docker compose run --rm --entrypoint python metadata-indexer \\
        /app/apply_changes.py

    Короче, с хоста: .\\scripts\\graph-refresh.ps1 [-DryRun|-Reconcile|-Full]

──────────────────────────────────────────────────────────────────────
SRC-12: РЕЖИМ GIT
──────────────────────────────────────────────────────────────────────
Если выгрузка лежит в git-репозитории (раскладка — README, «Обновление
выгрузки»), скрипт не обходит выгрузку, а делает ТАКТ — ровно то же, что
watcher по таймеру и metadata_refresh() из чата (source_sync.sync):
HEAD против проиндексированного коммита плюс файлы, которые правил агент.

    (без ключа)     такт: доиндексировать изменившееся
    --dry-run       показать план, ничего не меняя
    --reconcile     сверка через git status: найти правки, о которых никто
                    не сообщил (минуты на большой выгрузке)
    --full          полная индексация (~10 минут), с подтверждением
    --force-unlock  снять аренду, если её держатель упал и ждать её
                    истечения не хочется

Коды выхода в режиме git: 0 — готово или делать нечего; 1 — ошибки при
обновлении; 2 — Neo4j не отвечает; 3 — нужна полная индексация или
состояния ещё нет; 4 — граф сейчас обновляет кто-то другой.

Без git — прежнее поведение, описанное ниже.

──────────────────────────────────────────────────────────────────────
ЧТО ЭТОТ РЕЖИМ НЕ ДЕЛАЕТ — прочитать до первого запуска
──────────────────────────────────────────────────────────────────────
Слой 2 (граф вызовов) собирается ГЛОБАЛЬНЫМ фикс-пойнтом: тип параметра в
модуле A зависит от вызова в модуле B. Точечное обновление такой сходимости
не даёт и дать не может — это была бы пересборка всего.

Следствия, каждое измеримо:

  • Вызовы ИЗ ЧУЖИХ модулей в обновлённый помечаются `resolved=false`
    с причиной `stale_after_incremental`. Покрытие резолва падает.
  • Межмодульные типы (`:INFERRED_TYPE`) для обновлённых процедур не
    пересчитываются.

Поэтому частичное обновление — это «быстро и достаточно хорошо для
рабочего цикла», а НЕ замена полной сборке. Скрипт печатает накопленный
долг после каждого прогона и предупреждает, когда пора прогнать полную:
`METADATA_FORCE_REINDEX=true` плюс обычный запуск индексера.

Порог долга задаётся `METADATA_STALE_DEBT_LIMIT` (по умолчанию 20000).
Число взято не с потолка: на боевой конфигурации 722 тысячи callsite'ов,
то есть 20 тысяч — это около 3% графа вызовов. Больше — и ответы про
использование процедур начнут заметно врать.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from graph_writer import (                              # noqa: E402
    FP_MODE_CONTENT, FP_MODE_STAT, Neo4j, fingerprint_write,
    relations_snapshot_write,
)
from incremental import (                               # noqa: E402
    _clear_meta_object_slice, _clear_module_code_slice, upsert_file,
)
from bsl_parser import is_module_owner                  # noqa: E402
from partial_fingerprint import (                       # noqa: E402
    build_plan, dependent_owners, is_upsertable, read_stored, scan_workspace,
    stale_debt, write_stored,
)
from progress_log import ProgressLogger, human_sec      # noqa: E402

# FIX-27: после точечного обновления проверяем не шаг, а результат.
try:
    from graph_integrity import (
        HAS_METHOD_COUNT_CYPHER, log_ownership, ownership_report,
    )
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from graph_integrity import (
        HAS_METHOD_COUNT_CYPHER, log_ownership, ownership_report,
    )

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("apply_changes")

DEFAULT_DEBT_LIMIT = 20000


def _is_module(owner: str) -> bool:
    """
    Удалять срез модуля (код) или объекта (метаданные)?

    Общий модуль — модуль: его XML и BSL под одним id, и при исчезновении
    владельца снимается код. Остальное — как скажет bsl_parser (FIX-34:
    наборы записей, команды, модули конфигурации).
    """
    return owner.startswith("CommonModule.") or is_module_owner(owner)


# Статусы `upsert_file`, означающие успех.
OK_STATUSES = {"reindexed", "removed", "created", "updated", "ok"}

# Пропуски, которые ожидаемы и не являются проблемой: вложенные XML
# (формы, макеты) не самостоятельные объекты. Отсеиваются заранее
# (is_upsertable), но проверка оставлена — на случай, если список
# расширится со стороны incremental.
EXPECTED_SKIPS = {"not_a_toplevel_object_xml", "not_an_xml_file",
                  "not_a_bsl_file", "unsupported_extension",
                  "form_not_in_metadata"}     # FIX-41: хвост формы в выгрузке


def _ownership(neo: Neo4j) -> dict:
    """Три O(1)-счётчика: процедуры, модули, рёбра владения (FIX-27)."""
    def one(cypher: str, key: str) -> int:
        rows = neo.rows(cypher)
        return (rows[0].get(key) or 0) if rows else 0

    return ownership_report(
        one("MATCH (n:Callable) RETURN count(n) AS c", "c"),
        one("MATCH (n:Module) RETURN count(n) AS c", "c"),
        one(HAS_METHOD_COUNT_CYPHER, "c"),
        objects=one("MATCH (n:MetadataObject) RETURN count(n) AS c", "c"),
    )


def _ordered_files(owner: str, files: list[str]) -> list[str]:
    """
    Только то, что умеет обновлять incremental, и XML раньше BSL.

    Порядок не косметический: `upsert_xml_file` пересобирает срез объекта
    целиком, и если залить BSL до XML, только что записанные узлы модуля
    будут снесены следом.
    """
    usable = [f for f in files if is_upsertable(f)]
    return sorted(usable, key=lambda f: (0 if f.lower().endswith(".xml") else 1, f))


# ─── SRC-12: режим git ────────────────────────────────────────────────────

GIT_EXIT = {"ok": 0, "unchanged": 0, "dry_run": 0, "error": 1,
            "needs_full": 3, "no_state": 3, "busy": 4}


def _neo_from_env() -> Neo4j:
    return Neo4j(
        os.environ.get("NEO4J_URL", "http://neo4j:7474"),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", ""),
        timeout=float(os.environ.get("NEO4J_TIMEOUT_SEC", "600")),
    )


def _run_full(assume_yes: bool) -> int:
    """Полная индексация тем же индексером, что и контейнер metadata-indexer."""
    import subprocess
    if not assume_yes:
        log.warning("Полная индексация: на боевой выгрузке около 10 минут, граф "
                    "всё это время отвечает по старым данным.")
        try:
            answer = input("Продолжить? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes", "д", "да"):
            log.info("Отменено.")
            return 0
    env = dict(os.environ, METADATA_FORCE_REINDEX="true")
    return subprocess.call([sys.executable, "/app/mcp_metadata_indexer.py"], env=env)


def _git_main(args) -> "int | None":
    """
    Режим git. None — выгрузка не в git, работать прежним способом.
    """
    from git_source import GitError
    from source_state import Neo4jStore
    from source_sync import (Neo4jGraphOps, context_from_env, describe,
                             max_partial_from_env, sync)
    try:
        ctx = context_from_env()
    except GitError as e:
        log.error("Источник не определён: %s", e)
        return 2
    if not ctx.is_git:
        if args.reconcile or args.force_unlock:
            log.warning("--reconcile и --force-unlock — только для режима git "
                        "(%s); выполняю обычный прогон", ctx.reason)
        log.info("Режим legacy: %s", ctx.reason)
        return None
    log.info("Режим git: %s", ctx.reason)

    neo = _neo_from_env()
    try:
        neo.rows("RETURN 1 AS ok")
    except Exception as e:
        log.error("Neo4j не отвечает: %s", e)
        return 2
    store = Neo4jStore(neo)
    store.ensure_schema()

    if args.force_unlock:
        holder = store.holder()
        store.force_unlock()
        log.warning("Аренда снята%s.", f" (держал {holder.describe()})"
                    if holder.ok else " (никто не держал)")
        return 0

    t0 = time.monotonic()
    res = sync(ctx, store, None if args.dry_run else Neo4jGraphOps(neo, ctx.src_root),
               owner="manual", reconcile=args.reconcile, dry_run=args.dry_run,
               max_partial=max_partial_from_env())
    took = time.monotonic() - t0

    log.info("ИТОГ: %s — %s (за %s)", res.status, res.message, human_sec(took))
    if res.planned:
        title = "Будет обновлено" if args.dry_run else "Обновлено"
        log.info("%s файлов: %d", title, len(res.planned))
        for rel in res.planned[:50]:
            log.info("    %s", rel)
        if len(res.planned) > 50:
            log.info("    … ещё %d", len(res.planned) - 50)
    if res.applied and res.applied.failed:
        for f in res.applied.files:
            if f["status"] not in OK_STATUSES:
                log.error("    ✗ %s %s → %s (%s)", f["action"], f["rel"],
                          f["status"], f["reason"])
    if res.dirty_count:
        log.info("Правки поверх коммита (dirty): %d", res.dirty_count)
    if res.normalized_matches:
        log.info("Совпадений только после нормализации переводов строк: %d "
                 "(в git LF, на диске CRLF — это нормально)", res.normalized_matches)
    if res.applied and res.applied.finish:
        fin = res.applied.finish
        if fin.get("ownership"):
            log.info("")
            log.info("Проверка целостности:")
            log_ownership(fin["ownership"], log)
        if "stale_debt" in fin:
            limit = int(os.environ.get("METADATA_STALE_DEBT_LIMIT", DEFAULT_DEBT_LIMIT))
            log.info("Долг точечных обновлений: %d callsite'ов (порог %d).",
                     fin["stale_debt"], limit)
            if fin["stale_debt"] >= limit:
                log.warning("Долг превысил порог — пора полную: "
                            ".\\scripts\\graph-refresh.ps1 -Full")
    if res.status == "needs_full":
        log.warning("Полная индексация: .\\scripts\\graph-refresh.ps1 -Full "
                    "(или этот скрипт с --full)")
    elif res.status == "no_state":
        log.warning("Граф ещё не индексировался в режиме git — запустите "
                    "индексер: docker compose up metadata-indexer")
    elif res.status == "busy":
        log.warning("Повторите позже. Если держатель упал: --force-unlock")
    if not args.dry_run:
        src_block = describe(store.read(), ctx)
        if src_block.get("needs_reconcile") and not args.reconcile:
            log.info("Незакоммиченные правки на диске ещё не сверялись: "
                     "запустите с --reconcile")
    return GIT_EXIT.get(res.status, 1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PERF-8: точечно применить изменения выгрузки.")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план и выйти, ничего не меняя")
    ap.add_argument("--max-partial", type=int,
                    default=int(os.environ.get("METADATA_MAX_PARTIAL", "300")),
                    help="выше этого числа изменений частичный режим "
                         "не окупается — нужна полная пересборка")
    ap.add_argument("--reconcile", action="store_true",
                    help="режим git: сверка через git status — найти правки, "
                         "о которых никто не сообщил")
    ap.add_argument("--full", action="store_true",
                    help="полная индексация (~10 минут), с подтверждением")
    ap.add_argument("--yes", action="store_true",
                    help="не спрашивать подтверждения для --full")
    ap.add_argument("--force-unlock", action="store_true",
                    help="режим git: снять аренду записи (держатель упал)")
    args = ap.parse_args()

    if args.full:
        return _run_full(args.yes)
    rc = _git_main(args)
    if rc is not None:
        return rc

    src = Path(os.environ.get("METADATA_SRC_DIR", "/data/1c-xml"))
    if not src.exists():
        log.error("Каталог выгрузки не найден: %s", src)
        return 2

    neo = Neo4j(
        os.environ.get("NEO4J_URL", "http://neo4j:7474"),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", ""),
        timeout=float(os.environ.get("NEO4J_TIMEOUT_SEC", "600")),
    )

    t_all = time.monotonic()
    try:
        neo.rows("MATCH (n:MetadataObject) RETURN count(n) AS c")
    except Exception as e:
        log.error("Neo4j не отвечает: %s", e)
        return 2

    log.info("Обходим выгрузку: %s", src)
    current, meta = scan_workspace(src, keep_files=True)
    files_by_owner = meta["files_by_owner"]
    log.info("  ✓ единиц %d, файлов %d, за %s",
             meta["owners"], meta["files"], human_sec(meta["elapsed_sec"]))

    stored = read_stored(neo)
    plan = build_plan(current, stored, max_partial=args.max_partial)
    log.info("ИТОГ: %s", plan.summary())

    if plan.full_reindex_reason:
        log.warning("")
        log.warning("Частичное обновление не подходит: %s",
                    plan.full_reindex_reason)
        log.warning("Запустите полную индексацию:")
        log.warning("  METADATA_FORCE_REINDEX=true, затем обычный старт "
                    "metadata-indexer")
        return 3

    if plan.total == 0:
        log.info("Делать нечего.")
        return 0

    # ─ Порядок работ ─
    #
    # Сначала удаления: если объект исчез, его срез надо снять до того, как
    # что-то пересобирать, иначе рёбра могут указывать в снесённое.
    # Потом добавленное и изменённое — вместе с зависимыми единицами.
    to_upsert: list[str] = []
    seen = set()
    for owner in sorted(plan.added | plan.changed):
        for o in [owner] + dependent_owners(owner, current):
            if o not in seen and o in files_by_owner:
                seen.add(o)
                to_upsert.append(o)

    extra = len(to_upsert) - len(plan.added | plan.changed)
    if extra:
        log.info("  + %d зависимых единиц: обновление XML объекта сносит его "
                 "срез вместе с модулями, их надо залить заново", extra)

    if args.dry_run:
        log.info("")
        log.info("--dry-run: ничего не изменено.")
        for o in sorted(plan.removed):
            log.info("    удалить  %s", o)
        for o in to_upsert:
            usable = _ordered_files(o, files_by_owner[o])
            nested = len(files_by_owner[o]) - len(usable)
            log.info("    обновить %s (%d файлов%s)", o, len(usable),
                     f", вложенных пропущено {nested}" if nested else "")
        return 0

    # ─ Удаления ─
    for owner in sorted(plan.removed):
        try:
            if _is_module(owner):
                _clear_module_code_slice(neo, owner)
            else:
                _clear_meta_object_slice(neo, owner)
            log.info("  удалено: %s", owner)
        except Exception as e:
            log.error("  ✗ удаление %s: %s", owner, e)
            return 1

    # ─ Обновления ─
    prog = ProgressLogger(log, "точечное обновление", total=len(to_upsert),
                          every_sec=15.0, check_every=1, unit="ед")
    failed = 0
    processed = skipped_ok = 0
    for owner in to_upsert:
        for rel in _ordered_files(owner, files_by_owner[owner]):
            try:
                res = upsert_file(neo, src, rel)
                status = res.get("status")
                reason = res.get("reason")
                if status in OK_STATUSES:
                    processed += 1
                elif status == "skipped" and reason in EXPECTED_SKIPS:
                    skipped_ok += 1
                elif status == "error":
                    log.error("    ✗ %s → %s (%s)", rel, status, reason)
                    failed += 1
                else:
                    # Неожиданный пропуск — файл есть в выгрузке, но
                    # обновление до него не дошло. Молчать нельзя: объект
                    # останется в графе в старом виде, а прогон отчитается
                    # об успехе (тот же принцип, что FIX-15).
                    log.warning("    %s → %s (%s) — не обновлено",
                                rel, status, reason)
                    failed += 1
            except Exception as e:
                log.error("    ✗ %s: %s", rel, e)
                failed += 1
        prog.step()
    prog.done(extra=f"файлов обновлено {processed}"
                    + (f", пропущено вложенных {skipped_ok}" if skipped_ok else ""))

    if failed:
        log.error("Ошибок при обновлении: %d — состояние НЕ сохранено, "
                  "чтобы следующий прогон повторил работу.", failed)
        return 1

    # Состояние пишем ТОЛЬКО после успешного применения. Иначе при сбое
    # посреди прогона отпечатки сказали бы «всё обновлено», а часть единиц
    # осталась бы старой — и разойтись это могло бы надолго.
    res = write_stored(neo, current)
    log.info("Состояние сохранено: %d отпечатков в %d кусках",
             res["written"], res["chunks"])

    # PERF-8b. Обновляем и ОБЩИЙ fingerprint — тот, на который смотрит
    # indexer.py при старте контейнеров.
    #
    # Без этого частичное обновление отменялось само собой: индексер видел
    # расхождение и запускал полную двухчасовую переиндексацию при первом
    # же `docker compose up`. Экономия в 3 минуты превращалась в лишние два
    # часа — то есть режим был бы хуже, чем его отсутствие.
    #
    # Режим ставим stat: обход считает по (путь, размер, mtime), как и
    # индексер по умолчанию. Если у вас включён METADATA_FINGERPRINT_STRICT,
    # индексер увидит смену режима и один раз переиндексирует полностью —
    # он честно об этом скажет в логе.
    strict = os.environ.get("METADATA_FINGERPRINT_STRICT", "").lower() in (
        "1", "true", "yes", "y", "on")
    mode = FP_MODE_CONTENT if strict else FP_MODE_STAT
    gd = meta.get("global_digests") or {}
    if gd and not strict:
        fingerprint_write(neo, gd[".xml"], "metadata_xml", mode=mode)
        fingerprint_write(neo, gd[".bsl"], "bsl_source", mode=mode)
        log.info("Fingerprint индексера обновлён — полной переиндексации "
                 "при следующем старте не будет")
    elif strict:
        log.warning("METADATA_FINGERPRINT_STRICT=true: общий fingerprint "
                    "считается по содержимому, а частичный обход — по stat. "
                    "Обновить его отсюда нельзя, и следующий старт индексера "
                    "запустит полную переиндексацию.")

    # PERF-12: снимок счётчиков рёбер устарел — граф только что менялся.
    try:
        relations_snapshot_write(neo)
    except Exception as e:
        log.warning("Снимок рёбер не обновлён (%s) — metadata_stats посчитает "
                    "их сам, дороже", e)

    # FIX-27. Точечное обновление XML сносит срез объекта вместе с рёбрами
    # владения; `upsert_xml_file` привязывает код обратно, а здесь мы
    # СМОТРИМ, получилось ли. Проверять результат, а не шаг: именно
    # отсутствие такой проверки дало разрушенному графу прожить несколько
    # дней в состоянии «всё хорошо».
    log.info("")
    log.info("Проверка целостности:")
    log_ownership(_ownership(neo), log)

    debt = stale_debt(neo)
    limit = int(os.environ.get("METADATA_STALE_DEBT_LIMIT", DEFAULT_DEBT_LIMIT))
    log.info("")
    log.info("Готово за %s. Долг точечных обновлений: %d callsite'ов.",
             human_sec(time.monotonic() - t_all), debt)
    if debt >= limit:
        log.warning("Долг превысил порог %d — пора прогнать полную сборку:", limit)
        log.warning("  METADATA_FORCE_REINDEX=true, затем обычный старт "
                    "metadata-indexer")
    else:
        log.info("Порог полной пересборки: %d.", limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
