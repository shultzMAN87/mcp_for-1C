#!/usr/bin/env python3
"""
DOC-3. Разбор корня: что остаётся у входной двери, а что уходит в архив.
========================================================================

Что было
────────
59 файлов `.md` в корне репозитория. Пришедший со стороны видит их все
сразу и не может отличить действующую документацию от разбора правки,
сделанной в мае.

Хуже, чем шум. Два файла из трёх, которые новичок откроет по названию, —
не то, чем называются:

  `00-ЧИТАТЬ-ПЕРВЫМ.md`  — сопроводиловка к архиву захода про стандарты
                            («что в архиве и куда это класть»);
  `УСТАНОВКА.md`         — сопроводиловка к архиву `FIX-3 + FIX-4.2`,
                            начинается словами «разворачивается поверх
                            D:\\Docker\\30_mcp_cursor».

То есть у входа стоят два указателя, и оба показывают в прошлый заход.
Настоящая установка описана в `README.md`.

Почему перенести, а не удалить
──────────────────────────────
Эти разборы — не мусор. В них лежит то, чего нет больше нигде: почему
решение принято именно такое и какая ошибка к нему привела. Проект пятый
заход подряд опирается на них («тот же довод, что в `PERF-6`»), и половина
сегодняшних тестов написана потому, что разбор объяснял, чего именно
бояться.

Удалять такое — терять причины и оставлять следствия. Поэтому переезд:
из корня в `docs/archive/`, с указателем `docs/archive/README.md`, где у
каждого файла написано, о чём он.

Что делает скрипт
─────────────────
Переносит всё, кроме канона, в `docs/archive/`. Через `git mv`, если это
репозиторий, — тогда история файла сохраняется и `git log --follow` его
находит. Иначе обычным переименованием.

Идемпотентен: повторный запуск ничего не делает и говорит об этом.
Ничего не удаляет — только двигает.

Запуск:
    python3 scripts/archive_docs.py --dry-run   # показать, что будет
    python3 scripts/archive_docs.py             # сделать
"""

from __future__ import annotations

import subprocess
import sys
import re
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "docs" / "archive"


# ─── канон ───────────────────────────────────────────────────────────────
#
# Ровно то, что нужно человеку, пришедшему со стороны, и человеку,
# вернувшемуся через месяц. Пять файлов и один текущий план.
#
# Список короткий не ради красоты. Корень — это оглавление, которое
# читается целиком за один взгляд; список из пятнадцати позиций уже не
# оглавление, а начало следующей свалки.

CANON = {
    "README.md":        "входная дверь: что это, из чего состоит, как поднять",
    "ARCHITECTURE.md":  "устройство набора в двух страницах",
    "PROMPTS.md":       "как этим пользоваться каждый день",
    "СТАТУС.md":        "где проект сейчас (DOC-4)",
    # FIX-34, вторая половина. `ОТКРЫТОЕ.md` здесь не было — а сторож
    # канона (`scripts/tests_docs_canon.py`) его требует. Два списка
    # канона разошлись, и цена расхождения крупнее обычной: скрипт унёс
    # бы список открытых вопросов в архив, а сторож после этого упал бы
    # на «канон неполон» — то есть починка ломала бы проверку, которая её
    # стережёт.
    "ОТКРЫТОЕ.md":      "что не сделано и почему (DOC-4)",
    "LICENSE":          "MIT",
}

# Текущий план захода остаётся в корне; предыдущие уходят в архив.
#
# FIX-34. Здесь стояло прошитое `PLAN-6.md` — шестой рукописный список в
# проекте, и самый вредный из шести: скрипт, встретив `PLAN-9.md` в корне,
# счёл бы его лишним и унёс в архив вместе с разборами. Заметить это можно
# было только по факту переноса действующего плана.
#
# Тот же приём, что в `FIX-32` и в `DOC-3`: список выводится из того, что
# есть, а не переписывается руками при каждом заходе. В корне по канону
# ровно один `PLAN*.md` — это стережёт `tests_docs_canon.py`; здесь мы
# просто берём его, какой есть. Пусто — значит переносить нечего, и это не
# ошибка: канон допускает корень без плана между заходами.
PLAN_RE = re.compile(r"^PLAN(-\d+)?\.md$")


def current_plans() -> set[str]:
    """Планы, лежащие в корне сейчас. Они остаются, всё прочее уезжает."""
    return {p.name for p in ROOT.glob("*.md")
            if p.is_file() and PLAN_RE.match(p.name)}


def is_git_repo() -> bool:
    try:
        return run_git(["rev-parse", "--git-dir"]).returncode == 0
    except FileNotFoundError:
        return False


def to_move() -> list[Path]:
    """Всё, что в корне и не канон."""
    keep = set(CANON) | current_plans()
    return sorted(
        p for p in ROOT.glob("*.md")
        if p.is_file() and p.name not in keep
    )


def run_git(args: list[str]):
    """
    Вызов git, переживающий русские имена файлов на узкой консоли.

    `B-3`, третий раз и с другой стороны трубы. Первые два раза чинили СВОЙ
    вывод: `_say()` в сервере справки и `PYTHONIOENCODING` дочерним
    процессам в `run_all_tests.py`. Здесь ломалось ЧТЕНИЕ чужого.

    `subprocess.run(..., text=True)` без явной кодировки декодирует вывод
    тем, что вернёт `locale.getpreferredencoding()` — на русской Windows
    это cp1251. Git на отказе пишет имя файла, а в именах здесь кириллица;
    поток-читатель падает с `UnicodeDecodeError` **в отдельном потоке**,
    `run()` этого не замечает и возвращает `stderr = None`. Дальше
    `.strip()` на `None` — и скрипт умирает посреди переноса, оставив
    половину файлов в корне.

    Два урока, и второй важнее:
      • кодировку при чтении вывода надо задавать явно, всегда;
      • `errors="replace"` тут обязателен. Диагностика не имеет права
        ронять операцию, ради которой её печатают.
    """
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True,
        encoding="utf-8", errors="replace",
    )


def move(paths: list[Path], *, use_git: bool, dry: bool) -> list[str]:
    done = []
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for src in paths:
        dst = ARCHIVE / src.name
        if dst.exists():
            print(f"  пропуск  {src.name} — в архиве уже есть")
            continue
        if dry:
            print(f"  {'git mv' if use_git else 'mv'}   {src.name} → docs/archive/")
            done.append(src.name)
            continue
        moved_by = "mv"
        if use_git:
            r = run_git(["mv", str(src.relative_to(ROOT)),
                         str(dst.relative_to(ROOT))])
            if r.returncode == 0:
                moved_by = "git mv"
            else:
                # Обычный случай, а не сбой: часть документов приезжала
                # архивами и под контроль версий не попадала. Двигаем сами —
                # для неотслеживаемого файла разницы между git mv и mv нет.
                why = (r.stderr or "").strip().replace("\n", " ")
                short = ("не под контролем версий"
                         if "not under version control" in why
                         else why[:60] or "причина не сообщена")
                src.rename(dst)
                print(f"  mv       {src.name} ({short})")
        else:
            src.rename(dst)
        if moved_by == "git mv":
            print(f"  git mv   {src.name}")
        elif use_git is False:
            print(f"  mv       {src.name}")
        done.append(src.name)
    return done


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]

    # Сверка перед необратимым: узнаём ли мы канон в том, что лежит в корне.
    #
    # Нашлось прогоном на узкой локали. Имена файлов здесь кириллические, и
    # если Python читает каталог не в UTF-8 (LC_ALL=C, ASCII-локаль на
    # хосте), `СТАТУС.md` с диска не совпадает со строкой `СТАТУС.md` в
    # коде. Список канона тогда не срабатывает, и скрипт уносит в архив
    # действующую документацию — молча, отрапортовав «остаётся в корне: 6».
    #
    # Хуже всего тут именно молчание: перенос пятидесяти файлов одной
    # командой необратим настолько, насколько необратим `git mv` без
    # коммита. Поэтому не «постараемся угадать», а «не тронем ничего, пока
    # не убедимся, что читаем каталог правильно».
    fs_encoding = (sys.getfilesystemencoding() or "").lower().replace("-", "")
    non_ascii = [p.name for p in ROOT.glob("*.md")
                 if not p.name.isascii()]
    if non_ascii and fs_encoding not in ("utf8", "mbcs"):
        print("DOC-3: разбор корня")
        print("=" * 60)
        print(f"СТОП. Имена файлов читаются кодировкой {fs_encoding!r}, "
              f"а в корне есть кириллические имена:")
        for name in sorted(non_ascii)[:3]:
            print(f"  {name}")
        print("\nВ такой кодировке `СТАТУС.md` с диска не совпадёт со")
        print("строкой `СТАТУС.md` в списке канона — и действующая")
        print("документация уедет в архив вместе с разборами. Молча.")
        print("\nНичего не тронуто. Запустите так:")
        print("  PYTHONUTF8=1 python3 scripts/archive_docs.py")
        return 2

    paths = to_move()

    print("DOC-3: разбор корня")
    print("=" * 60)
    if not paths:
        print("В корне только канон — переносить нечего.")
        print("\nОстались:")
        for name, why in CANON.items():
            if (ROOT / name).exists():
                print(f"  {name:20s} {why}")
        return 0

    already = [p for p in paths if (ARCHIVE / p.name).exists()]
    print(f"Переносим в docs/archive/: {len(paths) - len(already)} файлов")
    if already:
        # Иначе первая строка обещает 56, а последняя рапортует про 55, и
        # читателю приходится гадать, потерялся файл или так и задумано.
        print(f"Уже в архиве, пропустим: {len(already)} "
              f"({', '.join(p.name for p in already)})")
    print(f"Остаётся в корне: {len(CANON) + 1}\n")
    moved = move(paths, use_git=is_git_repo(), dry=dry)

    print("\n" + "=" * 60)
    if dry:
        print(f"Это была примерка (--dry-run). Файлов к переносу: {len(moved)}")
        return 0

    print(f"Перенесено: {len(moved)}")
    print("\nДальше:")
    print("  1. Проверьте ссылки:  python3 scripts/run_all_tests.py")
    print("  2. Индекс архива:     docs/archive/README.md")
    print("  3. Зафиксируйте:      git commit -m 'DOC-3: разбор корня'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
