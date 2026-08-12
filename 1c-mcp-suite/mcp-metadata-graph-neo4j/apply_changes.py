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

from graph_writer import Neo4j                          # noqa: E402
from incremental import (                               # noqa: E402
    _clear_meta_object_slice, _clear_module_code_slice, upsert_file,
)
from partial_fingerprint import (                       # noqa: E402
    build_plan, dependent_owners, read_stored, scan_workspace, stale_debt,
    write_stored,
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


def _ordered_files(owner: str, files: list[str]) -> list[str]:
    """
    XML раньше BSL.

    Порядок не косметический: `upsert_xml_file` пересобирает срез объекта
    целиком, и если залить BSL до XML, только что записанные узлы модуля
    будут снесены следом.
    """
    return sorted(files, key=lambda f: (0 if f.lower().endswith(".xml") else 1, f))


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
            log.info("    обновить %s (%d файлов)", o, len(files_by_owner[o]))
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
    for owner in to_upsert:
        for rel in _ordered_files(owner, files_by_owner[owner]):
            try:
                res = upsert_file(neo, src, rel)
                if res.get("status") not in (None, "ok", "updated", "created"):
                    log.warning("    %s → %s (%s)", rel, res.get("status"),
                                res.get("reason"))
            except Exception as e:
                log.error("    ✗ %s: %s", rel, e)
                failed += 1
        prog.step()
    prog.done()

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
