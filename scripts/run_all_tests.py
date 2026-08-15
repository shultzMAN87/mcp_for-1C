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

A-5. Пропущенный набор — не успех.
──────────────────────────────────
Итоговая строка была «OK: 17 наборов, 696 тестов» и не различала «прошло»
и «не запускалось». Практическое следствие уже случилось:
`evals/runner/tests.py` падает на боевой машине, а во всех прогонах он
пропускался из-за отсутствующего пакета `mcp` — и цифра «0 провалов» была
верна ровно для того окружения, где тест не выполнялся.

Теперь число не запущенных наборов стоит в итоговой строке отдельно, а
`--strict` делает его провалом: в CI «не проверялось» и «проверено» — это
разные исходы.

Запуск:
    python3 scripts/run_all_tests.py
    python3 scripts/run_all_tests.py --quiet     # только итог
    python3 scripts/run_all_tests.py --strict    # не запущенный набор = провал
    python3 scripts/run_all_tests.py --baseline  # ещё и сверка с базой
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# B-3. Печать не должна ронять скрипт.
#
# `FAIL-2`: на приёмке 15 августа набор упал с UnicodeEncodeError на знаке
# ⚠ — консоль была cp1251, а в строке стоял символ, которого в ней нет.
# Тогда починили сервер справки и дочерние процессы run_all_tests, но сами
# хостовые скрипты остались: у них вывод уходит в консоль напрямую, и
# `$OutputEncoding` в PowerShell тут не помогает — он про то, чем консоль
# ЧИТАЕТ вывод, а не чем Python его кодирует.
#
# Воспроизводится одной строкой:
#     PYTHONIOENCODING=cp1251 python3 scripts/eval_all.py --summary-only
#
# errors=replace, а не encoding=utf-8: подмена кодировки дала бы кракозябры
# в cp1251-консоли, а замена — всего лишь «?» вместо галочки. Испортить
# украшение можно, уронить diagnostics-скрипт нельзя. Особенно
# check_prereqs: к нему идут именно тогда, когда что-то не работает.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


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
    # HYG-2: наборы, проверяющие сам инструментарий проекта (сверка двух
    # генераторов лок-файлов). Каталог добавлен по той же причине, что и
    # каталог раннера до него: набор существовал и не запускался.
    (ROOT / "scripts", "tests_*.py"),
]


def _container_hint(path: Path) -> str:
    """
    Как разбираться с набором, который не запустился.

    Раньше подсказка была одна на всех и звала в eval-runner. Когда в
    список попал набор platform-help, скрипт посоветовал запустить его в
    чужом контейнере — совет, который не работает. Сообщение, всегда
    говорящее одно и то же, перестаёт быть сообщением; это `DOC-1` в
    миниатюре.

    Каталог `evals/` монтируется в eval-runner как /app/evals, поэтому его
    набор действительно можно гонять там. Наборы серверов в образы не
    копируются (в Dockerfile идут только рабочие модули), поэтому для них
    честный совет один: посмотреть настоящую причину прямым запуском.
    """
    if {"evals", "runner"} <= set(path.parts):
        return ("Запуск: docker compose run --rm --no-deps eval-runner "
                f"python /app/evals/runner/{path.name}")
    rel = path.relative_to(ROOT)
    return (f"Причина целиком: python {rel}  "
            f"(в образ набор не копируется — он рассчитан на хост)")


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
    # FAIL-2. Дочерний процесс пишет в pipe, а не в консоль, поэтому Python
    # берёт кодировку локали — на русской Windows это cp1251. Половина
    # модулей проекта печатает ✓ и ⚠, которых в cp1251 нет, и набор падает
    # с UnicodeEncodeError в совершенно постороннем месте: именно так
    # 15 августа развалился tests_fail1_fast_fail, где ни одна проверка к
    # выводу отношения не имеет.
    #
    # `$OutputEncoding` в PowerShell на это не влияет — он про то, чем
    # консоль читает вывод, а не про то, чем дочерний Python его кодирует.
    #
    # errors=replace, а не strict: непечатаемый символ обязан портить
    # строку в логе, а не прогон.
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    proc = subprocess.run(
        [sys.executable, path.name],
        cwd=path.parent, capture_output=True, text=True,
        env=env, encoding="utf-8", errors="replace",
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
    ap.add_argument("--strict", action="store_true",
                    help="считать провалом набор, который не удалось запустить")
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
        # Подсказка на набор, а не одна на всех: она была прибита к
        # eval-runner, и когда в список не запустившихся попал набор
        # platform-help, скрипт посоветовал запустить его в чужом
        # контейнере. Сообщение, которое всегда говорит одно и то же,
        # перестаёт быть сообщением — это `DOC-1` в миниатюре.
        print("Не проверялись (нет зависимостей вне контейнера):")
        for path, missing in not_run:
            print(f"    {path.relative_to(ROOT)} — нужен '{missing}'")
            print(f"      {_container_hint(path)}")
        print()

    # A-5: «не запускался» выносится в итоговую строку отдельным числом.
    # Раньше он жил только в примечании выше, а глаз читает последнюю
    # строку — и видел «OK: 17 наборов».
    ran = len(suites) - len(not_run)
    not_run_note = f", НЕ ЗАПУСКАЛИСЬ: {len(not_run)}" if not_run else ""
    skip_note = f", {total_skipped} тестов пропущено внутри наборов" if total_skipped else ""

    if failures:
        for path, tail in failures:
            print(f"─── {path.relative_to(ROOT)} ───")
            print(tail)
            print()
        print(f"ПРОВАЛ: {len(failures)} из {ran} запущенных наборов "
              f"(всего {len(suites)}){not_run_note}, "
              f"{total_tests} тестов за {elapsed:.1f} с")
        return 1

    if not_run and args.strict:
        print(f"ПРОВАЛ (--strict): проверено {ran} наборов из {len(suites)}, "
              f"{len(not_run)} не запускались, {total_tests} тестов за "
              f"{elapsed:.1f} с{skip_note}")
        return 1

    print(f"OK: проверено {ran} наборов из {len(suites)}{not_run_note}, "
          f"{total_tests} тестов за {elapsed:.1f} с{skip_note}")

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
