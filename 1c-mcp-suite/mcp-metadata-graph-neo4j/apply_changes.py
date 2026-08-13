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
)
from incremental import (                               # noqa: E402
    _clear_meta_object_slice, _clear_module_code_slice, upsert_file,
)
from partial_fingerprint import (                       # noqa: E402
    build_plan, dependent_owners, is_upsertable, read_stored, scan_workspace,
    stale_debt, write_stored,
)
from progress_log import ProgressLogger, human_sec      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("apply_changes")

DEFAULT_DEBT_LIMIT = 20000


def _is_module(owner: str) -> bool:
    tail = owner.rsplit(".", 1)[-1]
    return (owner.startswith("CommonModule.")
            or tail in ("ObjectModule", "ManagerModule")
            or ".Form." in owner)


# Статусы `upsert_file`, означающие успех.
OK_STATUSES = {"reindexed", "removed", "created", "updated", "ok"}

# Пропуски, которые ожидаемы и не являются проблемой: вложенные XML
# (формы, макеты) не самостоятельные объекты. Отсеиваются заранее
# (is_upsertable), но проверка оставлена — на случай, если список
# расширится со стороны incremental.
EXPECTED_SKIPS = {"not_a_toplevel_object_xml", "not_an_xml_file",
                  "not_a_bsl_file", "unsupported_extension"}


def _ordered_files(owner: str, files: list[str]) -> list[str]:
    """
    Только то, что умеет обновлять incremental, и XML раньше BSL.

    Порядок не косметический: `upsert_xml_file` пересобирает срез объекта
    целиком, и если залить BSL до XML, только что записанные узлы модуля
    будут снесены следом.
    """
    usable = [f for f in files if is_upsertable(f)]
    return sorted(usable, key=lambda f: (0 if f.lower().endswith(".xml") else 1, f))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PERF-8: точечно применить изменения выгрузки.")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план и выйти, ничего не меняя")
    ap.add_argument("--max-partial", type=int,
                    default=int(os.environ.get("METADATA_MAX_PARTIAL", "300")),
                    help="выше этого числа изменений частичный режим "
                         "не окупается — нужна полная пересборка")
    args = ap.parse_args()

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
