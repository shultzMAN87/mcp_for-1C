#!/usr/bin/env python3
"""
CI-1 — весь набор тестов одной командой.
=========================================

Зачем. В Заходе 3 правка в `bsl_resolver.py` сломала `tests_incremental`, и
это заметилось только потому, что весь набор кто-то запустил вручную.
Наборы лежат в трёх каталогах и запускаются по отдельности — то есть
«прогнать всё» до сих пор означало помнить наизусть список из тринадцати
команд и не ошибиться ни в одной.

Скрипт находит наборы сам, по имени `tests_*.py`. Захардкоженный список
рано или поздно разошёлся бы с реальностью — новый набор просто забыли бы
дописать, и он молча не запускался бы месяцами. Ровно этот класс проблем
Заход 4 и разбирал.

Код возврата ненулевой при любом провале — чтобы скрипт годился и для
глаз, и для автоматики.

Запуск:
    python3 scripts/run_all_tests.py
    python3 scripts/run_all_tests.py --quiet     # только итог
    python3 scripts/run_all_tests.py --baseline  # ещё и сверка с базой
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE_DIR = ROOT / "1c-mcp-suite"

# Наборы, требующие живой Neo4j: они сами скипаются, когда базы нет, но их
# результат не считаем показателем — иначе «12 пропущено» выглядело бы
# как успех.
NEEDS_NEO4J = {"tests_graph_writer.py"}


# Где искать наборы и по какой маске.
#
# Каталог раннера добавлен после того, как выяснилось: его набор
# (`evals/runner/tests.py`) не подпадал под маску `tests_*.py` и не
# запускался ни разу. Заявление «наборы находятся сами» оказалось верным
# только для того каталога, о котором я думал, когда его писал.
SUITE_ROOTS = [
    (SUITE_DIR, "tests_*.py"),
    (ROOT / "evals" / "runner", "tests*.py"),
]


def find_suites() -> list[Path]:
    """Все наборы тестов проекта, кроме служебных каталогов."""
    out: list[Path] = []
    seen: set[Path] = set()
    for root, pattern in SUITE_ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob(pattern)):
            if "__pycache__" in p.parts or p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


def run_one(path: Path) -> tuple[bool, int, int, float, str, str | None]:
    """
    Возвращает (успех, тестов, пропущено, секунд, хвост вывода).

    unittest пишет результат в stderr — читаем оба потока.
    """
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, path.name],
        cwd=path.parent, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - t0
    output = (proc.stdout or "") + (proc.stderr or "")

    m = re.search(r"Ran (\d+) tests?", output)
    total = int(m.group(1)) if m else 0
    m = re.search(r"skipped=(\d+)", output)
    skipped = int(m.group(1)) if m else 0

    # Набор может требовать зависимостей, которых нет вне его контейнера
    # (evals/runner тянет пакет `mcp`). Это НЕ провал — но и не успех:
    # такой набор просто не проверялся, и говорить об этом надо прямо.
    missing = None
    if proc.returncode != 0 and "ModuleNotFoundError" in output:
        for line in output.split("\n"):
            if "ModuleNotFoundError" in line:
                missing = line.split("named")[-1].strip().strip("'\"")
                break

    ok = proc.returncode == 0
    tail = "" if ok else "\n".join(output.strip().split("\n")[-25:])
    return ok, total, skipped, elapsed, tail, missing


def main() -> int:
    ap = argparse.ArgumentParser(description="CI-1: прогон всех наборов тестов.")
    ap.add_argument("--quiet", action="store_true", help="только итоговая строка")
    ap.add_argument("--baseline", action="store_true",
                    help="дополнительно сверить граф с evals/baseline.json")
    args = ap.parse_args()

    suites = find_suites()
    if not suites:
        print("Наборов не найдено — проверьте, что скрипт лежит в scripts/",
              file=sys.stderr)
        return 2

    failures, not_run, total_tests, total_skipped = [], [], 0, 0
    t0 = time.monotonic()

    for path in suites:
        ok, n, skipped, elapsed, tail, missing = run_one(path)
        total_tests += n
        total_skipped += skipped
        if missing:
            not_run.append((path, missing))
            if not args.quiet:
                print(f"  НЕТ  {path.name:<28} не запускался — нужен пакет "
                      f"'{missing}' (набор живёт в своём контейнере)")
            continue
        if not args.quiet:
            mark = "OK  " if ok else "ПАД."
            note = ""
            if skipped:
                note = f" ({skipped} пропущено"
                note += ", нужен Neo4j)" if path.name in NEEDS_NEO4J else ")"
            print(f"  {mark} {path.name:<28} {n:>4} тестов  {elapsed:5.1f} с{note}")
        if not ok:
            failures.append((path, tail))

    elapsed = time.monotonic() - t0
    print()
    if not_run:
        print("Не проверялись (нет зависимостей вне контейнера):")
        for path, missing in not_run:
            print(f"    {path.relative_to(ROOT)} — нужен '{missing}'")
        print("    Запуск: docker compose run --rm eval-runner python -m pytest")
        print()
    if failures:
        for path, tail in failures:
            print(f"─── {path.relative_to(ROOT)} ───")
            print(tail)
            print()
        print(f"ПРОВАЛ: {len(failures)} из {len(suites)} наборов, "
              f"{total_tests} тестов за {elapsed:.1f} с")
        return 1

    skip_note = f", {total_skipped} пропущено" if total_skipped else ""
    print(f"OK: {len(suites)} наборов, {total_tests} тестов за "
          f"{elapsed:.1f} с{skip_note}")

    if args.baseline:
        print()
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_baseline.py")],
        ).returncode
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
