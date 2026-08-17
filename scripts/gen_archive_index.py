#!/usr/bin/env python3
"""
DOC-3. Указатель по архиву документов.
=======================================

Зачем генерировать, а не написать руками
────────────────────────────────────────
Список из 55 позиций, который ведёт человек, — это шестой такой список в
проекте. Предыдущие пять кончились одинаково: `LOCK-1` (пара есть в `.sh`,
нет в `.ps1`), `B-6` (три набора со своими списками образов), `HYG-2`,
`HYG-4` (конфиг с четырьмя серверами из пяти). Каждый раз расхождение
происходило молча и находилось месяцами позже.

Указатель по архиву разойдётся с архивом ровно так же — при первом же
файле, который туда положат мимо него. Поэтому он собирается из самих
файлов: заголовок берётся из первой строки, метка задачи — из имени,
группа — из вида имени.

Что не генерируется
───────────────────
Раздел «с чего начать» вверху. Это единственное, что нельзя вывести из
имён: какие пять разборов стоит прочитать тому, кто пришёл разбираться в
устройстве, а не искать конкретную правку. Его ведут руками — и он
короткий именно поэтому.

Запуск:  python3 scripts/gen_archive_index.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "docs" / "archive"
INDEX = ARCHIVE / "README.md"


# Группы по виду имени. Порядок — порядок разделов в указателе.
GROUPS = (
    ("Планы заходов", lambda n: n.startswith("PLAN")),
    ("Итоги заходов", lambda n: n.startswith("ИТОГИ")),
    ("Сопроводиловки к архивам",
     lambda n: n in ("00-ЧИТАТЬ-ПЕРВЫМ.md", "УСТАНОВКА.md",
                     "КУДА-КЛАСТЬ.md", "README-ПРИМЕНИТЬ.md")),
    ("Разборы правок", lambda n: n.startswith("README-")),
)

HEAD = """# Архив документов

Здесь лежат планы, итоги и разборы правок за все заходы. Из корня они
убраны в `DOC-3`: 59 файлов `.md` у входной двери не позволяли отличить
действующую документацию от разбора майской правки.

**Это не мусор.** В разборах лежит то, чего нет больше нигде, — почему
решение принято именно такое и какая ошибка к нему привела. Половина
сегодняшних тестов написана потому, что разбор объяснил, чего бояться.
Проект пятый заход подряд на них ссылается («тот же довод, что в
`PERF-6`»), и ссылки эти надо было куда-то направить.

Действующая документация осталась в корне: `README.md`, `ARCHITECTURE.md`,
`PROMPTS.md`, `СТАТУС.md`.

---

## С чего начать, если разбираетесь в устройстве

Пять разборов, которые объясняют повторяющиеся решения набора. Остальное
читается по надобности — через поиск по метке (`PERF-7`, `FIX-16`) в
таблицах ниже.

| Файл | О чём и почему стоит |
|---|---|
| `README-AUDIT-1.md` | где отказ выглядит как успех. Сквозная тема проекта: пустой ответ неотличим от «ничего не нашлось» |
| `README-PERF-6.md` | «флаг вместо замка». Дефект, который потом нашёлся ещё дважды (`PERF-6.1`, `AUDIT-3`) |
| `README-PERF-7.md` | почему JVM живёт процессом. Основание для `PERF-9` |
| `README-B-6.md` | доставка модулей в образы: как один список заменил три |
| `README-SOFT-РАЗБОР.md` | четыре промаха из пяти оказались ошибками в ожиданиях, а не в коде |

---
"""

FOOT = """
---

Указатель собран `scripts/gen_archive_index.py` из самих файлов: заголовки
взяты из первой строки, группы — из вида имени. Руками правится только
раздел «с чего начать» — его из имён не вывести.

Пересобрать после добавления файла:

    python3 scripts/gen_archive_index.py
"""


def title_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line).replace("|", "\\|")
        if line:
            return line[:90].replace("|", "\\|")
    return "—"


def tags_of(name: str) -> str:
    """Метки задач из имени файла: README-FIX-14-FIX-15.md → FIX-14, FIX-15."""
    stem = name.removesuffix(".md").removeprefix("README-")
    found = re.findall(r"[A-Z]{2,}-\d+(?:-\d+)?", stem)
    return ", ".join(f"`{t}`" for t in found)


def build() -> str:
    files = sorted(p for p in ARCHIVE.glob("*.md") if p.name != "README.md")
    out = [HEAD]
    placed = set()

    for heading, matches in GROUPS:
        rows = [p for p in files if matches(p.name) and p.name not in placed]
        if not rows:
            continue
        placed.update(p.name for p in rows)
        out.append(f"\n## {heading}\n")
        has_tags = any(tags_of(p.name) for p in rows)
        if has_tags:
            out.append("| Файл | Метки | О чём |")
            out.append("|---|---|---|")
            for p in rows:
                out.append(f"| `{p.name}` | {tags_of(p.name) or '—'} | {title_of(p)} |")
        else:
            out.append("| Файл | О чём |")
            out.append("|---|---|")
            for p in rows:
                out.append(f"| `{p.name}` | {title_of(p)} |")
        out.append("")

    rest = [p for p in files if p.name not in placed]
    if rest:
        out.append("\n## Прочее\n")
        out.append("| Файл | О чём |")
        out.append("|---|---|")
        for p in rest:
            out.append(f"| `{p.name}` | {title_of(p)} |")
        out.append("")

    out.append(FOOT)
    return "\n".join(out)


def main() -> int:
    if not ARCHIVE.is_dir():
        print("docs/archive/ ещё нет — сначала scripts/archive_docs.py")
        return 1
    text = build()
    INDEX.write_text(text, encoding="utf-8")
    count = len([p for p in ARCHIVE.glob("*.md") if p.name != "README.md"])
    print(f"docs/archive/README.md: {count} файлов в указателе")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
