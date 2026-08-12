#!/usr/bin/env python3
"""
PERF-8 — что изменилось в выгрузке с прошлой индексации.
=========================================================

Отвечает на вопрос «мне опять ждать два часа?» конкретным числом, ничего
при этом не меняя. Только читает: обходит выгрузку, считает отпечаток на
каждый объект и модуль, сравнивает с тем, что сохранено в графе.

    docker compose run --rm --entrypoint python metadata-indexer \\
        /app/what_changed.py

Первый запуск скажет, что сохранённого состояния нет: отпечатки по объектам
появляются в графе только после индексации новым индексером. Чтобы записать
их для текущего графа, не пересобирая его:

    docker compose run --rm --entrypoint python metadata-indexer \\
        /app/what_changed.py --save

Это безопасно: `--save` пишет ТОЛЬКО свойство `fp` на существующие узлы и
не трогает ни рёбра, ни состав графа. Делать это стоит сразу после полной
индексации, когда граф заведомо соответствует выгрузке.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from graph_writer import Neo4j                      # noqa: E402
from partial_fingerprint import (                   # noqa: E402
    ROOT_KEY, build_plan, read_stored, scan_workspace, stale_debt, write_stored,
)
from progress_log import human_bytes, human_sec     # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("what_changed")


def main() -> int:
    ap = argparse.ArgumentParser(description="PERF-8: что изменилось в выгрузке.")
    ap.add_argument("--save", action="store_true",
                    help="записать текущие отпечатки в граф (после полной индексации)")
    ap.add_argument("--limit", type=int, default=40,
                    help="сколько имён показать в каждом списке")
    args = ap.parse_args()

    src = Path(os.environ.get("METADATA_SRC_DIR", "/data/1c-xml"))
    if not src.exists():
        log.error("Каталог выгрузки не найден: %s", src)
        return 2

    # Таймаут поднят: после перезапуска Neo4j страничный кеш холодный, и
    # первые запросы к базе на 1,3 млн узлов идут на диск. Умолчание в
    # 60 секунд на первой же порции записи не выдерживало.
    neo = Neo4j(
        os.environ.get("NEO4J_URL", "http://neo4j:7474"),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", ""),
        timeout=float(os.environ.get("NEO4J_TIMEOUT_SEC", "600")),
    )

    # Быстрая проверка отзывчивости базы ДО двенадцатиминутного обхода.
    # Без неё выяснить, что Neo4j не отвечает, можно было только потратив
    # эти двенадцать минут впустую.
    t0 = time.monotonic()
    try:
        neo.rows("MATCH (n:MetadataObject) RETURN count(n) AS c")
    except Exception as e:
        log.error("Neo4j не отвечает: %s", e)
        log.error("Проверьте `docker compose ps neo4j` и зависшие транзакции: "
                  "SHOW TRANSACTIONS. Перезапуск: docker compose restart neo4j")
        return 2
    ping = time.monotonic() - t0
    log.info("Neo4j отвечает (%.1f с на подсчёт узлов)", ping)
    if ping > 10:
        log.warning("База отвечает медленно — запись отпечатков может занять "
                    "заметное время.")

    log.info("Обходим выгрузку: %s", src)
    current, meta = scan_workspace(src)
    log.info("  ✓ единиц %d, файлов %d (пропущено %d), %s, за %s",
             meta["owners"], meta["files"], meta["skipped"],
             human_bytes(meta["bytes"]), human_sec(meta["elapsed_sec"]))

    stored = read_stored(neo)
    log.info("Сохранено в графе: %d единиц", len(stored))

    if args.save:
        log.info("Записываем отпечатки в граф…")
        res = write_stored(neo, current)
        log.info("  ✓ записано %d отпечатков в %d кусках%s",
                 res["written"], res["chunks"],
                 f", удалено лишних кусков {res['dropped_chunks']}"
                 if res["dropped_chunks"] else "")
        log.info("Теперь следующий запуск покажет реальные изменения.")
        return 0

    plan = build_plan(current, stored)
    log.info("")
    log.info("ИТОГ: %s", plan.summary())

    for title, items in (("Добавлено", plan.added),
                         ("Изменено", plan.changed),
                         ("Удалено", plan.removed)):
        if not items:
            continue
        log.info("")
        log.info("%s (%d):", title, len(items))
        shown = sorted(items)[:args.limit]
        for name in shown:
            log.info("    %s", name)
        if len(items) > len(shown):
            log.info("    … ещё %d", len(items) - len(shown))

    debt = stale_debt(neo)
    if debt:
        log.info("")
        log.info("Долг точечных обновлений: %d callsite'ов помечены устаревшими.", debt)
        log.info("Это места вызова из ЧУЖИХ модулей в те, что обновлялись точечно —")
        log.info("их пере-резолюция требует полного прогона.")

    if plan.full_reindex_reason:
        return 0
    if plan.total == 0:
        log.info("")
        log.info("Переиндексация не нужна.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
