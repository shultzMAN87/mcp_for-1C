#!/usr/bin/env python3
"""
Разбор одного .hbk под лупой — для случая «страница была в индексе и пропала».

Отвечает на три вопроса по порядку:

  1. Сходится ли число записей, если считать его тремя независимыми
     способами: по центральному каталогу ZIP (так читает новый код),
     проходом по Local File Header'ам (так читал старый) и просто по
     числу сигнатур `PK\\x03\\x04` в потоке. Расхождение здесь означает,
     что новый способ чтения теряет записи, — то есть виноват hbk_reader.

  2. Есть ли в контейнере искомая страница и проходит ли она отбор HTML.
     Если запись находится, но отбор её не берёт — виноват фильтр.

  3. Что из неё достаёт парсер: имя, вид, длина текста. Если имя пустое —
     страница в индекс попадёт безымянной, и lookup по имени её не найдёт.

Запуск (изнутри контейнера индексатора, где лежит hbk_reader):

    docker compose run --rm --no-deps -v ${PWD}/scripts:/probe help-indexer \\
        python /probe/probe_hbk.py /data/1c-platform/shcntx_ru.hbk StrLen

Второй аргумент — что искать, по имени записи и по содержимому. Лучше
брать латиницу (StrLen, StrLength): кириллица по дороге через PowerShell и
docker иногда приезжает в другой кодировке, и поиск не находит ничего не
потому, что страницы нет.
"""

from __future__ import annotations

import io
import sys
import zipfile
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


sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(
    Path(__file__).resolve().parent.parent
    / "1c-mcp-suite" / "mcp-platform-help-embeddings"
))

try:
    import hbk_reader as R
except ImportError:
    print("hbk_reader не найден. Скрипт нужно запускать внутри контейнера "
          "help-indexer (там модуль лежит в /app) либо из каталога "
          "1c-mcp-suite/mcp-platform-help-embeddings.")
    raise SystemExit(2)


def count_three_ways(path: Path) -> None:
    print(f"── {path.name}: сверка числа записей ──")
    data = path.read_bytes()
    print(f"  размер файла: {len(data):,} байт")
    print(f"  V8-контейнер: {R.is_v8_container(data)}")

    if not R.is_v8_container(data):
        print("  дальше сверять нечего: файл читается старым способом")
        return

    elements = dict(R.iter_container_elements(data))
    print(f"  элементы: {', '.join(elements)}")

    blob = elements.get("FileStorage")
    if blob is None:
        print("  ⚠ элемента FileStorage нет — вот и причина")
        return

    print(f"  FileStorage: {len(blob):,} байт")

    raw_signatures = blob.count(b"PK\x03\x04")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        via_cd = len([i for i in archive.infolist() if not i.is_dir()])
    except zipfile.BadZipFile as exc:
        via_cd = -1
        print(f"  ⚠ центральный каталог не читается: {exc}")
    via_lfh = len(list(R._iter_zip_via_lfh(blob, path.name)))

    print(f"  записей по центральному каталогу: {via_cd:,}   ← так читает новый код")
    print(f"  записей проходом по LFH:          {via_lfh:,}")
    print(f"  сигнатур PK\\x03\\x04 в потоке:     {raw_signatures:,}")

    if via_cd == via_lfh == raw_signatures:
        print("  ✓ сходится — записи при чтении не теряются")
    else:
        print("  ⚠ РАСХОЖДЕНИЕ: часть записей видна одним способом и не видна другим")
        if via_cd >= 0 and via_lfh > via_cd:
            print(f"    новый способ не видит {via_lfh - via_cd} записей, "
                  f"которые есть в потоке — это и есть потеря")


def count_pages(path: Path) -> None:
    print(f"\n── {path.name}: сколько проходит отбор ──")
    entries = html_by_name = html_by_content = total_html = 0
    for name, content in R.iter_hbk_entries(path):
        entries += 1
        if len(content) < 40:
            continue
        by_name = name.lower().endswith((".html", ".htm"))
        by_content = R.looks_like_html(content)
        html_by_name += by_name
        html_by_content += by_content
        total_html += by_name or by_content
    print(f"  записей всего:             {entries:,}")
    print(f"  HTML по расширению имени:  {html_by_name:,}   ← так отбирал старый код")
    print(f"  HTML по содержимому:       {html_by_content:,}")
    print(f"  идёт в индекс (или/или):   {total_html:,}")


def find_page(path: Path, term: str) -> None:
    print(f"\n── {path.name}: ищу '{term}' ──")
    needle_name = term.lower()
    needle_bytes = term.encode("utf-8").lower()
    found = 0

    try:
        from hbk_parser import parse_html
    except ImportError:
        parse_html = None

    for name, content in R.iter_hbk_entries(path):
        hit_name = needle_name in name.lower()
        hit_body = needle_bytes in content[:4000].lower()
        if not (hit_name or hit_body):
            continue

        found += 1
        if found > 8:
            continue

        passes = len(content) >= 40 and (
            R.looks_like_html(content) or name.lower().endswith((".html", ".htm"))
        )
        where = "в имени" if hit_name else "в тексте"
        print(f"  [{found}] {name!r} — {len(content):,} байт, совпадение {where}")
        print(f"       проходит отбор HTML: {passes}")
        print(f"       первые байты: {content[:48]!r}")

        if passes and parse_html is not None:
            entry = parse_html(name, content, hbk_file=path.name)
            print(f"       парсер: name_ru={entry.name_ru!r} name_en={entry.name_en!r} "
                  f"kind={entry.kind!r} текста={len(entry.raw_text or '')} символов")

    if found == 0:
        print("  ничего не найдено — записи с таким именем/текстом в контейнере нет")
    else:
        print(f"  всего совпадений: {found}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"Нет файла: {path}")
        return 1

    count_three_ways(path)
    count_pages(path)

    if len(argv) > 2:
        find_page(path, argv[2])

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
