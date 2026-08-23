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

CI-5. Скипнувший всё — тоже не успех.
─────────────────────────────────────
`A-5` закрыл два случая из трёх. Третий нашёлся на приёмке `PERF-12`:
набор запускается, возвращает ноль и пропускает все свои тесты до
единого —

    OK    tests_stats_batch.py     28 тестов    0.1 с (28 пропущено)

— и его 28 «тестов» входят в итоговую цифру прогона. Правка `PERF-12`
сломала внутри него три теста, и это не заметил никто: проверено-то было
ноль. Разница между «прошло» и «ничего не проверялось» снова оказалась в
скобках, куда не смотрят.

Такой набор теперь помечен `ПУСТО`, назван отдельным блоком, посчитан в
итоговой строке и роняет `--strict`.

Запуск:
    python3 scripts/run_all_tests.py
    python3 scripts/run_all_tests.py --quiet     # только итог
    python3 scripts/run_all_tests.py --strict    # не запущенный или
                                                 # ничего не проверивший
                                                 # набор = провал
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

# FIX-24. Сколько ждать один набор, прежде чем считать его зависшим.
#
# Здесь не было ничего: `subprocess.run` без `timeout` ждёт вечно. 17
# августа прогон на Windows встал после первого же набора и молчал —
# сколько именно, неизвестно, потому что ждать до конца никто не стал.
#
# Ждать вечно диагностический скрипт не имеет права. Набор, который не
# уложился, — это результат («завис»), а не отсутствие результата, и
# отличается он от провала только текстом.
#
# Триста секунд — с запасом на порядок: самый долгий набор проекта идёт
# около трёх секунд, а один прогон из тридцати занимал 101 секунду, когда
# tests_bsl_lsp упирался в боевые таймауты.
SUITE_TIMEOUT_SEC = float(os.environ.get("RUN_TESTS_TIMEOUT_SEC", "300"))


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


def _rel(path: Path) -> str:
    """
    Путь для человека: короткий, когда набор лежит в проекте, и полный,
    когда нет. `Path.relative_to` на чужом пути бросает ValueError — то
    есть диагностический скрипт падал бы там, где всего лишь печатает имя.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


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
    rel = _rel(path)
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


def run_one(path: Path) -> tuple:
    """
    Возвращает (успех, тестов, пропущено, секунд, хвост вывода, чего не
    хватило, чем посчитано, сколько дошло до вердикта).

    Последнее число появилось в `CI-5`: по паре «тестов / пропущено»
    нельзя отличить «прошло три из семи» от «не проверено ничего» —
    пропуски на уровне класса не попадают в «Ran N».

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
    try:
        proc = subprocess.run(
            [sys.executable, path.name],
            cwd=path.parent, capture_output=True, text=True,
            env=env, encoding="utf-8", errors="replace",
            timeout=SUITE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        # FIX-24. Зависший набор — это результат, а не его отсутствие.
        elapsed = time.monotonic() - t0
        # `TimeoutExpired` отдаёт то, что успело накопиться, и типы у
        # потоков могут разойтись: один str, другой bytes — даже при
        # text=True. Схлопывать их сложением нельзя, это стоило падения
        # обработчика при первой же проверке.
        def _text(chunk) -> str:
            if chunk is None:
                return ""
            if isinstance(chunk, bytes):
                return chunk.decode("utf-8", "replace")
            return str(chunk)

        partial = _text(exc.stdout) + _text(exc.stderr)
        tail = "\n".join(partial.strip().split("\n")[-25:])
        return (False, 0, 0, elapsed,
                f"НЕ УЛОЖИЛСЯ в {SUITE_TIMEOUT_SEC:g} с и был снят.\n"
                f"Запустить отдельно и посмотреть, на чём стоит:\n"
                f"  python {_rel(path)} -v\n"
                + (f"\nЧто успел напечатать:\n{tail}" if tail else ""),
                None, "таймаут", 0)

    elapsed = time.monotonic() - t0
    output = (proc.stdout or "") + (proc.stderr or "")

    m = re.search(r"Ran (\d+) tests?", output)
    total = int(m.group(1)) if m else 0
    m = re.search(r"skipped=(\d+)", output)
    skipped = int(m.group(1)) if m else 0

    # A-5, третье состояние. Было два: «прошло» и «не запускался». Приёмка
    # 16 августа показала третье — набор запустился, вернул ноль и был
    # засчитан успехом:
    #
    #     OK   tests.py    0 тестов   2.1 с
    #
    # `evals/runner/tests.py` написан не на unittest: пять функций с
    # assert'ами и печатью «[5/5] ... OK». Строки «Ran N tests» в его выводе
    # нет, поэтому счётчик читал ноль. Пять настоящих проверок не попадали
    # ни в число 833, ни в чьё-либо внимание — а набор, который сломался бы
    # так, что выходит с нулём и кодом 0, выглядел бы ровно так же.
    #
    # Учим второй формат: строки вида «[3/5] что-то: OK».
    if not total:
        progress = re.findall(r"^\s*\[(\d+)/(\d+)\]", output, re.M)
        if progress:
            total = int(progress[-1][1])
            counted_by = "прогресс-строки"
        else:
            counted_by = ""
    else:
        counted_by = "unittest"

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
    proved = verdicts_in(output)
    if not proved:
        # Набор без подробного вывода: вердиктов в тексте нет, считаем по
        # числам. Это хуже (см. `is_fully_skipped`), но лучше, чем объявить
        # непроверенным любой краткий вывод.
        proved = max(total - skipped, 0)
    return ok, total, skipped, elapsed, tail, missing, counted_by, proved


def verdicts_in(output: str) -> int:
    """
    Сколько тестов дошли до вердикта — ok, FAIL или ERROR.

    Считается по подробному выводу (`unittest.main(verbosity=2)`, как во
    всех наборах проекта): у каждого теста своя строка, и пропущенный
    оканчивается на `skipped`, а не на вердикт.

    Зачем это вместо арифметики — см. `is_fully_skipped`.
    """
    return len(re.findall(r"\.\.\.\s+(?:ok|FAIL|ERROR)\b", output))


def is_fully_skipped(total: int, skipped: int, proved: int) -> bool:
    """
    CI-5. Набор, пропустивший ВСЕ свои тесты, равен незапущенному.

    Состояний у набора было три: прошёл, упал, не запустился. Четвёртое
    нашлось на приёмке `PERF-12`: набор запускается, отрабатывает за
    доли секунды, возвращает ноль — и пропускает все свои проверки до
    единой. Выглядит это так:

        OK    tests_stats_batch.py     28 тестов    0.1 с (28 пропущено)

    Слово `OK` здесь неверно: не проверено ничего. Правка `PERF-12`
    сломала в этом наборе три теста, и увидел это не прогон, а человек,
    заметивший число 28 в скобках.

    Почему нельзя сравнить два числа
    ─────────────────────────────────
    Первая версия правила звучала «пропущено >= всего» и покраснела на
    `tests_graph_writer`, где всё в порядке:

        Ran 3 tests ... OK (skipped=4)

    Три теста прошли по-настоящему, а четыре пропущены на уровне класса
    (`setUpClass` без Neo4j) — и такие в «Ran N» НЕ входят, в отличие от
    пропущенных декоратором. То есть по паре чисел «3 и 4» отличить
    «прошло три» от «не проверено ничего» невозможно в принципе.

    Поэтому считается третье число — сколько тестов дошли до вердикта.
    Ноль вердиктов при непустом наборе и есть тот случай, ради которого
    задача заведена: набор отработал, проверок не выполнено.
    """
    return total > 0 and skipped > 0 and proved <= 0


def main(argv: list[str] | None = None,
         suites: list[Path] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI-1: прогон всех наборов тестов.")
    ap.add_argument("--quiet", action="store_true", help="только итоговая строка")
    ap.add_argument("--strict", action="store_true",
                    help="считать провалом набор, который не удалось запустить "
                         "или который пропустил все свои тесты")
    ap.add_argument("--baseline", action="store_true",
                    help="дополнительно сверить граф с evals/baseline.json")
    args = ap.parse_args(argv)

    # `suites` — шов для проверки самого раннера (CI-5). Правило «скипнул
    # всё = провал» живёт в `main`, а не в `run_one`, поэтому проверять
    # его надо здесь же: тест на `is_fully_skipped` был бы тестом на
    # арифметику, а не на исход прогона. В работе аргумент не передаётся.
    if suites is None:
        suites = find_suites()
    if not suites:
        print("Наборов не найдено — проверьте, что скрипт лежит в scripts/",
              file=sys.stderr)
        return 2

    failures, not_run, total_tests, total_skipped = [], [], 0, 0
    uncounted = []   # запустились, но сколько тестов прошло — неизвестно
    all_skipped = []  # CI-5: запустились и не проверили ничего
    t0 = time.monotonic()

    # FIX-24, вторая половина. Имя набора печаталось ПОСЛЕ его окончания,
    # поэтому зависший набор не назывался вовсе: последняя строка на
    # экране принадлежала предыдущему, уже закончившемуся. Человек видит
    # «OK tests_bsl_health» и тишину — и ищет виноватого не там.
    #
    # Строка прогресса идёт в живую консоль и стирается результатом. При
    # перенаправлении в файл её нет: там от неё был бы мусор, а зависание
    # закрывает таймаут.
    live = sys.stdout.isatty() and not args.quiet

    for path in suites:
        if live:
            print(f"  ...  {path.name:<28} идёт…", end="\r", flush=True)
        ok, n, skipped, elapsed, tail, missing, counted_by, proved = run_one(path)
        if live:
            print(" " * 60, end="\r")
        total_tests += n
        total_skipped += skipped
        if missing:
            not_run.append((path, missing))
            if not args.quiet:
                print(f"  НЕТ  {path.name:<28} не запускался — нужен пакет "
                      f"'{missing}' (набор живёт в своём контейнере)")
            continue
        if ok and not n:
            uncounted.append(path)
        skipped_everything = ok and is_fully_skipped(n, skipped, proved)
        if skipped_everything:
            all_skipped.append(path)
        if not args.quiet:
            mark = "OK" if ok else ("ЗАВИС" if counted_by == "таймаут" else "ПАД.")
            if skipped_everything:
                # CI-5. Слово OK здесь врёт, поэтому его тут и нет: набор
                # отработал, а проверено ноль. Отметка стоит в той же
                # колонке, чтобы разница читалась глазом при беглом
                # просмотре, а не вычислялась из числа в скобках.
                mark = "ПУСТО"
            mark = f"{mark:<5}"
            note = ""
            if skipped:
                note = f" ({skipped} пропущено"
                note += ", нужен Neo4j)" if path.name in NEEDS_NEO4J else ")"
            count = f"{n:>4} тестов" if n else "   ? тестов"
            if ok and not n:
                note += "  ← счётчик не распознан, набор не на unittest"
            if skipped_everything:
                note += "  ← пропущены ВСЕ, набор ничего не проверил"
            print(f"  {mark} {path.name:<28} {count}  {elapsed:5.1f} с{note}")
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
            print(f"    {_rel(path)} — нужен '{missing}'")
            print(f"      {_container_hint(path)}")
        print()

    # A-5: «не запускался» выносится в итоговую строку отдельным числом.
    # Раньше он жил только в примечании выше, а глаз читает последнюю
    # строку — и видел «OK: 17 наборов».
    if uncounted:
        print("Запустились, но число тестов не распознано "
              "(вывод не в формате unittest):")
        for path in uncounted:
            print(f"    {_rel(path)} — код возврата 0, но сколько "
                  "проверок отработало, из вывода не видно")
        print("    Такой набор засчитан пройденным. Если он сломается так, "
              "что выйдет с нулём проверок,")
        print("    выглядеть это будет точно так же — см. --strict.")
        print()

    # CI-5. Отдельный блок, а не строчка в примечании: до правки такой
    # набор был неотличим от прошедшего, и именно так `tests_stats_batch`
    # прожил приёмку `PERF-12` с тремя сломанными тестами внутри.
    if all_skipped:
        print("Запустились и не проверили ничего (пропущены все тесты):")
        for path in all_skipped:
            print(f"    {_rel(path)} — набор отработал, "
                  f"проверок выполнено ноль")
        print("    Обычно причина — недостающий пакет или лежащий стенд: "
              "тесты скипаются поштучно,")
        print("    и набор целиком превращается в тишину, которая "
              "выглядит согласием. См. --strict.")
        print()

    ran = len(suites) - len(not_run)
    not_run_note = f", НЕ ЗАПУСКАЛИСЬ: {len(not_run)}" if not_run else ""
    uncounted_note = (f", БЕЗ СЧЁТЧИКА: {len(uncounted)}" if uncounted else "")
    # CI-5: число видно и без --strict. Итоговую строку читают чаще, чем
    # всё остальное, — а до правки «28 пропущено» жило только в скобках у
    # своего набора и в общей сумме пропущенных, где терялось.
    empty_note = (f", НИЧЕГО НЕ ПРОВЕРИЛИ: {len(all_skipped)}"
                  if all_skipped else "")
    skip_note = f", {total_skipped} тестов пропущено внутри наборов" if total_skipped else ""

    if failures:
        for path, tail in failures:
            print(f"─── {_rel(path)} ───")
            print(tail)
            print()
        print(f"ПРОВАЛ: {len(failures)} из {ran} запущенных наборов "
              f"(всего {len(suites)}){not_run_note}, "
              f"{total_tests} тестов за {elapsed:.1f} с")
        return 1

    if all_skipped and args.strict:
        names = ", ".join(p.name for p in all_skipped)
        print(f"ПРОВАЛ (--strict): {len(all_skipped)} наборов пропустили ВСЕ "
              f"свои тесты и не проверили ничего ({names}) — такой набор "
              f"равен незапущенному")
        return 1

    if uncounted and args.strict:
        print(f"ПРОВАЛ (--strict): {len(uncounted)} наборов не сообщили, "
              f"сколько проверок отработало — «прошло» и «ничего не "
              f"проверялось» неразличимы")
        return 1

    if not_run and args.strict:
        print(f"ПРОВАЛ (--strict): проверено {ran} наборов из {len(suites)}, "
              f"{len(not_run)} не запускались, {total_tests} тестов за "
              f"{elapsed:.1f} с{skip_note}")
        return 1

    print(f"OK: проверено {ran} наборов из {len(suites)}{not_run_note}"
          f"{uncounted_note}{empty_note}, {total_tests} тестов за "
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
