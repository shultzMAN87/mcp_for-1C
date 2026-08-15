"""
Читатель .hbk файлов справки 1С (платформа 8.3.22+).

HBK-1: что оказалось на самом деле
──────────────────────────────────
Прежняя версия исходила из того, что .hbk — это «префикс V8-контейнера
(~1700 байт), а дальше поток ZIP Local File Header'ов». Отсюда и способ
чтения: найти в файле первый `PK\\x03\\x04` и идти по LFH подряд.

Это не так, и держалось оно на совпадении. .hbk — полноценный
V8-контейнер, а ZIP лежит ВНУТРИ него, как содержимое одного элемента
(`FileStorage`). Контейнер хранит данные блоками, и перед каждым блоком
стоит 31-байтовый ASCII-заголовок вида

    \\r\\n0000abcd 00000200 00001234 \\r\\n
      ^ размер документа  ^ размер   ^ адрес следующего блока
        (в первом блоке)    блока      (7fffffff = последний)

То есть байты ZIP-потока в файле не непрерывны: каждые несколько сотен
байт в них вклинивается заголовок блока. Последовательный проход по LFH
доходил до первой такой врезки и молча останавливался — `return` без
единого слова в лог. Отсюда «0 HTML» у `shlang_ru.hbk`, `mngbase_ru.hbk`,
`frntend_ru.hbk`, `ecsui_ru.hbk` и недобор у остальных: из `ecsui_ru.hbk`
(209 КБ, 66 записей) читались 3 записи, из `mngbase_ru.hbk` — 57 из 184.

Второй дефект того же класса — в `iter_html_from_hbk`: HTML отбирался по
расширению имени. Внутри .hbk расширения нет почти ни у кого: страницы
называются `LEFTJOIN`, `form_AllSubsystemsDlg`, `dcsparameters.lf`. На
проверочном наборе из 39 контейнеров по расширению отбирались 47 страниц
из 1410 — 96% справки отбрасывалось уже после успешного чтения.

Как читаем теперь
─────────────────
1. Убеждаемся, что по смещению 16 стоит заголовок блока. Это надёжнее
   сигнатуры: первое поле шапки контейнера — адрес свободной страницы, и
   0x7FFFFFFF там лежит не всегда (в 25 файлах из 39 — другое значение).
2. Читаем документ по смещению 16 — это таблица элементов: тройки
   uint32 (адрес заголовка, адрес данных, 0x7FFFFFFF).
3. Из заголовка элемента берём имя (UTF-16LE с 20-го байта), из данных —
   содержимое. Нужен элемент `FileStorage`.
4. Внутри `FileStorage` лежит обычный ZIP с центральным каталогом —
   отдаём его штатному `zipfile`. Разбор LFH вручную остался запасным
   путём: он же обслуживает файлы, которые контейнером не оказались.

Если контейнер не разобрался — не падаем, а откатываемся на старое
поведение (сканирование LFH по всему файлу). Хуже, чем было, не станет
ни на одном файле; тихо лучше не станет тоже — причина отката пишется в
stderr.

Использование:
    from hbk_reader import iter_hbk_entries, iter_html_from_hbk
    for name, data in iter_html_from_hbk('path/to/shcntx_ru.hbk'):
        ...

Инвентаризация (что внутри и что пропущено):
    python3 hbk_reader.py /data/1c-platform
"""

from __future__ import annotations

import io
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Iterator, Optional

# A-2: сверка «вход против выхода». В образе всё лежит плоско в /app, при
# локальном запуске тестов — уровнем выше, в 1c-mcp-suite/.
try:
    from shortfall import Tally
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shortfall import Tally

# ─── Формат V8-контейнера ────────────────────────────────────────────────

# Пустой адрес: «следующего блока нет» / «данных нет».
_V8_EMPTY_ADDR = 0x7FFFFFFF

# Заголовок блока: ровно 31 байт ASCII. Три hex-поля по 8 символов —
# размер документа, размер блока, адрес следующего блока.
_BLOCK_HEADER_RE = re.compile(
    rb"\r\n([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) \r\n"
)
_BLOCK_HEADER_SIZE = 31

# Таблица элементов контейнера начинается сразу за 16-байтовой шапкой.
_TABLE_OFFSET = 16
_TABLE_ENTRY = struct.Struct("<III")  # (адрес заголовка, адрес данных, 0x7fffffff)

# Заголовок элемента: 8 байт дата создания, 8 байт дата изменения,
# 4 байта нулей, дальше имя в UTF-16LE.
_ELEMENT_NAME_OFFSET = 20

# Имя элемента с содержимым справки. Во всех 39 проверенных контейнерах
# набор элементов одинаков: Book, FileStorage, IndexMainData,
# IndexPackBlock, MainData, PackBlock, PackLookup. Страницы — только в
# FileStorage; PackBlock и IndexPackBlock тоже ZIP, но внутри у них
# служебный индекс справки, не HTML.
_CONTENT_ELEMENT = "FileStorage"

# ─── Формат ZIP (запасной путь) ──────────────────────────────────────────

_LFH_MAGIC = 0x04034B50            # PK\x03\x04 — Local File Header
_CFH_MAGIC = 0x02014B50            # PK\x01\x02 — Central Directory
_EOCD_MAGIC = 0x06054B50           # PK\x05\x06 — End Of Central Directory
_LFH_STRUCT = struct.Struct("<IHHHHHIIIHH")  # 30 байт
_LFH_SIZE = _LFH_STRUCT.size
_FLAG_DATA_DESCRIPTOR = 0x08       # размеры не в header'е, а после данных
_FLAG_UTF8_NAME = 0x0800           # имя уже в UTF-8

# ─── Определение HTML ────────────────────────────────────────────────────

_BOM = b"\xef\xbb\xbf"
_SNIFF_BYTES = 256                 # сколько байт нюхаем в начале записи

# С чего начинаются страницы справки. Замер по 39 контейнерам (1660
# записей): `<HTML><HEAD>` — 345, `<!DOCTYPE HTML` — 30, `<html><body>` — 7.
# Остальное — PNG (67), служебные структуры `{...}` (16), пустые (16).
# Список намеренно короткий: всё, что не подошло, видно в инвентаризации
# как «прочее», а не пропадает молча.
_HTML_PREFIXES = (
    b"<html",
    b"<!doctype html",
    b"<head",
    b"<body",
)

_HTML_NAME_SUFFIXES = (".html", ".htm")


class HbkReadError(RuntimeError):
    """Ошибка чтения .hbk, не связанная с конкретной записью."""


def _warn(message: str) -> None:
    """Диагностика в stderr: тихий откат — то, из-за чего HBK-1 и случился."""
    print(f"  ⚠ hbk_reader: {message}", file=sys.stderr)


# ─── Чтение документов контейнера ────────────────────────────────────────

def _read_document(data: bytes, offset: int) -> bytes:
    """
    Читает документ контейнера, идя по цепочке блоков от `offset`.

    Возвращает b"" для пустого адреса. Бросает HbkReadError, если по
    указанному смещению нет заголовка блока — это структурная ошибка, а не
    «конец данных», и различать их важно.
    """
    if offset == _V8_EMPTY_ADDR or offset < 0 or offset + _BLOCK_HEADER_SIZE > len(data):
        return b""

    out = bytearray()
    doc_len: int | None = None
    seen: set[int] = set()

    while True:
        if offset in seen:
            # Циклическая цепочка блоков. В корректном контейнере не
            # бывает, но читаем чужой бинарь — зациклиться нельзя.
            raise HbkReadError(f"цикл в цепочке блоков на смещении {offset}")
        seen.add(offset)

        match = _BLOCK_HEADER_RE.match(data, offset)
        if match is None:
            raise HbkReadError(f"нет заголовка блока на смещении {offset}")

        block_doc_len = int(match.group(1), 16)
        block_len = int(match.group(2), 16)
        next_offset = int(match.group(3), 16)

        if doc_len is None:
            doc_len = block_doc_len

        start = match.end()
        out += data[start:start + block_len]

        if next_offset == _V8_EMPTY_ADDR or next_offset + _BLOCK_HEADER_SIZE > len(data):
            break
        offset = next_offset

    # Последний блок дополнен до размера страницы — режем по длине документа.
    if doc_len:
        return bytes(out[:doc_len])
    return bytes(out)


def is_v8_container(data: bytes) -> bool:
    """
    Похож ли буфер на V8-контейнер.

    Проверяем не сигнатуру, а наличие заголовка блока сразу за 16-байтовой
    шапкой: первое поле шапки — адрес свободной страницы, и он у разных
    файлов разный (0x7FFFFFFF только когда свободных страниц нет).
    """
    if len(data) < _TABLE_OFFSET + _BLOCK_HEADER_SIZE:
        return False
    return _BLOCK_HEADER_RE.match(data, _TABLE_OFFSET) is not None


def iter_container_elements(data: bytes) -> Iterator[tuple[str, bytes]]:
    """
    Итерирует элементы V8-контейнера: (имя, содержимое).

    Бросает HbkReadError, если не читается таблица элементов, — тогда
    контейнером это считать нельзя. Отдельный сбойный элемент не роняет
    итерацию: пишем предупреждение и идём дальше.
    """
    table = _read_document(data, _TABLE_OFFSET)
    if len(table) < _TABLE_ENTRY.size:
        raise HbkReadError("таблица элементов пуста или короче одной записи")

    for pos in range(0, len(table) - _TABLE_ENTRY.size + 1, _TABLE_ENTRY.size):
        header_addr, data_addr, _tail = _TABLE_ENTRY.unpack_from(table, pos)
        try:
            header = _read_document(data, header_addr)
            content = _read_document(data, data_addr)
        except HbkReadError as exc:
            _warn(f"элемент по смещению {header_addr} пропущен: {exc}")
            continue

        raw_name = header[_ELEMENT_NAME_OFFSET:]
        name = raw_name.decode("utf-16le", errors="replace").split("\x00", 1)[0]
        yield name, content


# ─── Чтение ZIP-потока ───────────────────────────────────────────────────

def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """
    Имя записи ZIP.

    zipfile без флага UTF-8 декодирует имя как cp437 — русские имена
    превращаются в кашу. Прежний читатель пробовал сначала utf-8, и это
    правильный для .hbk порядок: возвращаем ему исходные байты и пробуем
    utf-8, а cp437-вариант оставляем как запасной.
    """
    name = info.filename
    if info.flag_bits & _FLAG_UTF8_NAME:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _iter_zip_via_zipfile(blob: bytes, source: str) -> Iterator[tuple[str, bytes]] | None:
    """
    Разбор ZIP штатным zipfile — путь по умолчанию: у ZIP внутри
    FileStorage центральный каталог на месте.

    Возвращает None, если это не ZIP (тогда вызывающий пробует LFH).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError):
        return None

    def _generate() -> Iterator[tuple[str, bytes]]:
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                try:
                    content = archive.read(info)
                except (zipfile.BadZipFile, RuntimeError, zlib.error, OSError) as exc:
                    _warn(f"{source}: запись {info.filename!r} не распаковалась: {exc}")
                    continue
                yield _decode_zip_name(info), content

    return _generate()


def _iter_zip_via_lfh(blob: bytes, source: str) -> Iterator[tuple[str, bytes]]:
    """
    Запасной разбор: идём по Local File Header'ам подряд.

    Нужен для потоков без центрального каталога и для файлов, которые не
    оказались контейнером. Логика та же, что была до HBK-1.
    """
    zip_off = blob.find(b"PK\x03\x04")
    if zip_off < 0:
        return

    size = len(blob)
    i = zip_off

    while i + _LFH_SIZE <= size:
        header = blob[i:i + _LFH_SIZE]
        magic = struct.unpack_from("<I", header, 0)[0]

        if magic in (_CFH_MAGIC, _EOCD_MAGIC):
            return
        if magic != _LFH_MAGIC:
            _warn(f"{source}: рассинхрон на смещении {i}, дальше не читаем")
            return

        (
            _magic, _ver, flags, method, _mtime, _mdate, _crc,
            csize, usize, name_len, extra_len,
        ) = _LFH_STRUCT.unpack(header)

        if flags & _FLAG_DATA_DESCRIPTOR:
            raise HbkReadError(
                f"{source}: запись с data descriptor (flag=0x8) на смещении {i} — "
                "размеры лежат после данных, последовательный проход невозможен"
            )

        name_start = i + _LFH_SIZE
        name_end = name_start + name_len
        data_start = name_end + extra_len
        data_end = data_start + csize

        if data_end > size:
            _warn(f"{source}: запись на смещении {i} обрезана, дальше не читаем")
            return

        raw_name = blob[name_start:name_end]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            name = raw_name.decode("cp437", errors="replace")

        compressed = blob[data_start:data_end]

        content: bytes | None = None
        if method == 0:            # stored
            content = compressed
        elif method == 8:          # deflate
            try:
                content = zlib.decompress(compressed, wbits=-15)
            except zlib.error:
                _warn(f"{source}: запись {name!r} не распаковалась")
                content = None
        else:
            _warn(f"{source}: запись {name!r} — метод сжатия {method}, пропущена")

        if content is not None:
            yield name, content

        i = data_end


def _iter_zip_entries(blob: bytes, source: str) -> Iterator[tuple[str, bytes]]:
    """ZIP штатно, при отказе — по LFH."""
    entries = _iter_zip_via_zipfile(blob, source)
    if entries is not None:
        yield from entries
        return
    yield from _iter_zip_via_lfh(blob, source)


# ─── Публичное API ───────────────────────────────────────────────────────

def iter_hbk_entries(path: str | Path) -> Iterator[tuple[str, bytes]]:
    """
    Итерирует по записям .hbk: (имя, распакованное содержимое).

    Имя — str, содержимое — bytes (любые: HTML, PNG, служебные структуры).
    Отбор HTML — в iter_html_from_hbk.
    """
    path = Path(path)
    data = path.read_bytes()
    if not data:
        return

    if not is_v8_container(data):
        _warn(f"{path.name}: не похоже на V8-контейнер, читаю как поток ZIP")
        yield from _iter_zip_via_lfh(data, path.name)
        return

    try:
        elements = list(iter_container_elements(data))
    except HbkReadError as exc:
        _warn(f"{path.name}: контейнер не разобран ({exc}), читаю как поток ZIP")
        yield from _iter_zip_via_lfh(data, path.name)
        return

    # Содержимое справки — в FileStorage. Если его нет (формат сменился),
    # берём любой элемент, который выглядит как ZIP, — но об этом говорим,
    # потому что дальше могут поехать служебные индексы вместо страниц.
    blobs = [c for n, c in elements if n == _CONTENT_ELEMENT]
    if not blobs:
        blobs = [c for n, c in elements if c[:4] == b"PK\x03\x04"]
        if blobs:
            names = ", ".join(n for n, _ in elements) or "нет элементов"
            _warn(
                f"{path.name}: элемента {_CONTENT_ELEMENT!r} нет (есть: {names}), "
                f"читаю все ZIP-элементы — проверьте результат"
            )

    if not blobs:
        _warn(f"{path.name}: в контейнере нет ZIP-элементов, страниц не будет")
        return

    for blob in blobs:
        yield from _iter_zip_entries(blob, path.name)


def looks_like_html(content: bytes) -> bool:
    """
    HTML ли это, по содержимому.

    По имени определять нельзя: внутри .hbk у большинства страниц
    расширения нет вовсе (`LEFTJOIN`, `form_AllSubsystemsDlg`), а часть
    носит своё (`dcsparameters.lf`).
    """
    head = content[:_SNIFF_BYTES]
    if head.startswith(_BOM):
        head = head[len(_BOM):]
    head = head.lstrip()[:64].lower()
    return head.startswith(_HTML_PREFIXES)


def iter_html_from_hbk(path: str | Path, min_size: int = 40,
                       tally: Optional[Tally] = None) -> Iterator[tuple[str, bytes]]:
    """
    Только HTML-страницы из .hbk.

    min_size — нижний порог по размеру содержимого. Служебные страницы
    вроде `_CONTENTS_NODE_fileEnterprise` (`<html><body></body></html>`,
    29 байт) отсеиваются именно им.

    A-1: это тот самый шаг, где отказ выглядел как результат. Читатель
    отдавал 25 страниц из 128 записей, и «25» ничем не отличалось от
    честного результата — число записей на входе наружу не выходило.
    Передайте `tally`, и вход, выход и причины отсева окажутся рядом.
    Заполняется он по мере итерации, поэтому смотреть на него надо после
    того, как генератор исчерпан.
    """
    for name, content in iter_hbk_entries(path):
        if tally is not None:
            tally.see()
        if len(content) < min_size:
            if tally is not None:
                # Служебные пустышки — законный и массовый отсев.
                tally.drop("меньше порога размера", example=name)
            continue
        if looks_like_html(content) or name.lower().endswith(_HTML_NAME_SUFFIXES):
            if tally is not None:
                tally.keep()
            yield name, content
        elif tally is not None:
            tally.drop(_content_kind(content), example=name)


# ─── Инвентаризация (диагностика) ────────────────────────────────────────

def _content_kind(content: bytes) -> str:
    """Грубая классификация записи — для отчёта, не для отбора."""
    if looks_like_html(content):
        return "html"
    # Служебные записи вида `_CONTENTS_NODE_file1` — это ровно BOM и ничего
    # больше. Пустые они и есть, «прочим» их считать незачем.
    if not content.lstrip(_BOM).strip():
        return "пусто"
    if content[:4] == b"\x89PNG" or content[:3] == b"\xff\xd8\xff" or content[:4] == b"GIF8":
        return "картинка"
    head = content.lstrip(_BOM).lstrip()[:1]
    if head == b"{":
        return "структура 1С"
    return "прочее"


def hbk_inventory(path: str | Path) -> dict:
    """
    Что внутри одного .hbk: сколько записей, сколько страниц, что отброшено.

    Отдельная функция, а не print: этим же пользуются тесты и индексатор,
    и по ней видно «прочее» — то, из-за чего страницы могли бы потеряться
    молча.
    """
    path = Path(path)
    result = {
        "file": path.name,
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "is_container": False,
        "elements": [],
        "entries": 0,
        "html": 0,
        "by_kind": {},
        "error": "",
    }

    try:
        data = path.read_bytes()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["is_container"] = is_v8_container(data)
    if result["is_container"]:
        try:
            result["elements"] = [n for n, _ in iter_container_elements(data)]
        except HbkReadError as exc:
            result["error"] = str(exc)

    try:
        for name, content in iter_hbk_entries(path):
            result["entries"] += 1
            kind = _content_kind(content)
            result["by_kind"][kind] = result["by_kind"].get(kind, 0) + 1
            if kind == "html" and len(content) >= 40:
                result["html"] += 1
    except HbkReadError as exc:
        result["error"] = str(exc)

    return result


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("Использование: python3 hbk_reader.py <файл.hbk | каталог>")
        return 2

    target = Path(argv[1])
    files = sorted(target.glob("*.hbk")) if target.is_dir() else [target]
    if not files:
        print(f"Нет .hbk в {target}")
        return 1

    print(f"{'файл':24} {'КБ':>7} {'записей':>8} {'страниц':>8}  прочее")
    total_entries = total_html = 0
    empty: list[str] = []

    for path in files:
        inv = hbk_inventory(path)
        total_entries += inv["entries"]
        total_html += inv["html"]
        if inv["html"] == 0:
            empty.append(inv["file"])
        other = ", ".join(
            f"{k}={v}" for k, v in sorted(inv["by_kind"].items()) if k != "html"
        )
        mark = "" if inv["is_container"] else " [не контейнер]"
        print(
            f"{inv['file']:24} {inv['size_bytes'] // 1024:7} "
            f"{inv['entries']:8} {inv['html']:8}  {other}{mark}"
        )
        if inv["error"]:
            print(f"    ошибка: {inv['error']}")

    print(f"\nИтого: файлов {len(files)}, записей {total_entries}, страниц {total_html}")
    if empty:
        print(f"⚠ Без единой страницы ({len(empty)}): {', '.join(empty)}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
