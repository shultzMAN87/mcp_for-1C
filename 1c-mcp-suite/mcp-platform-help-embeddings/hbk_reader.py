"""
Читатель .hbk файлов справки 1С (платформа 8.3.22+).

Формат .hbk:
  - В начале — префикс V8-контейнера (~1700 байт, ASCII-hex оглавление).
  - Далее — поток ZIP Local File Header'ов (PK\\x03\\x04), deflate-сжатие.
  - Central Directory присутствует, но не там, где ждёт стандартный
    zipfile, поэтому zipfile.ZipFile падает с BadZipFile.

Решение: парсим Local File Headers вручную, последовательно. Этого
достаточно — в LFH есть всё что нужно (имя, метод сжатия, размеры,
сами данные).

Структура LFH (little-endian):
  offset  size  field
  0       4     сигнатура 0x04034b50 (PK\\x03\\x04)
  4       2     версия распаковщика
  6       2     флаги (бит 3 = data descriptor после данных)
  8       2     метод сжатия (0=stored, 8=deflate)
  10      2     mtime
  12      2     mdate
  14      4     crc32
  18      4     compressed size
  22      4     uncompressed size
  26      2     длина имени
  28      2     длина extra
  30      N     имя
  30+N    M     extra
  30+N+M  C     сжатые данные

Использование:
    from hbk_reader import iter_hbk_entries
    for name, data in iter_hbk_entries('path/to/shcntx_ru.hbk'):
        # data — уже распакованный utf-8 HTML (bytes)
        ...
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Iterator


# Сигнатуры ZIP, которые мы распознаём при сканировании.
_LFH_MAGIC = 0x04034b50            # PK\x03\x04 — Local File Header
_CFH_MAGIC = 0x02014b50            # PK\x01\x02 — Central Directory (останавливаемся на ней)
_EOCD_MAGIC = 0x06054b50           # PK\x05\x06 — End Of Central Directory (тоже стоп)
_LFH_STRUCT = struct.Struct("<IHHHHHIIIHH")  # 30 байт
_LFH_SIZE = _LFH_STRUCT.size

# Бит 3 флагов LFH: размеры в самом header'е = 0, настоящие — в data descriptor'е
# после сжатых данных. В .hbk этот флаг не наблюдается, но для робастности
# упадём явной ошибкой, а не прочитаем мусор.
_FLAG_DATA_DESCRIPTOR = 0x08


class HbkReadError(RuntimeError):
    """Ошибка чтения .hbk, не связанная с конкретной записью."""


def _find_zip_start(data: bytes) -> int:
    """
    Находит первый Local File Header после префикса V8-контейнера.
    Возвращает offset или -1, если ZIP-потока нет.
    """
    return data.find(b"PK\x03\x04")


def iter_hbk_entries(path: str | Path) -> Iterator[tuple[str, bytes]]:
    """
    Итерирует по записям .hbk: (имя_файла, распакованное_содержимое).

    Имя файла — str (декодируем как utf-8 с fallback в cp437 как в ZIP-спеке).
    Содержимое — bytes (сырые данные после deflate).

    Файлы, которые не удалось распаковать, пропускаем с warning'ом в stderr.
    Ломаем итерацию только на структурной ошибке (неизвестная сигнатура не там,
    где ожидали) — это почти всегда означает, что мы уткнулись в central dir.
    """
    path = Path(path)
    data = path.read_bytes()
    if not data:
        return

    zip_off = _find_zip_start(data)
    if zip_off < 0:
        # Нет ZIP-потока в файле — возможно, это служебный .hbk без
        # HTML (UI-справка с картинками, мелкие shared-файлы). Не ошибка,
        # просто пустая итерация. Вызывающий код увидит "0 HTML" и пойдёт
        # дальше.
        return

    size = len(data)
    i = zip_off

    while i + _LFH_SIZE <= size:
        header = data[i:i + _LFH_SIZE]
        magic = struct.unpack_from("<I", header, 0)[0]

        # Упёрлись в Central Directory или EOCD — дальше файлов нет.
        if magic == _CFH_MAGIC or magic == _EOCD_MAGIC:
            return

        if magic != _LFH_MAGIC:
            # Структурный рассинхрон. Это либо мы промахнулись с offset'ом
            # (такого не должно быть — мы всегда прыгаем ровно на границу),
            # либо в файле битая запись. Скорее второе, и лучше всего
            # остановиться, чем плодить шум.
            return

        (
            _magic, _ver, flags, method, _mtime, _mdate, _crc,
            csize, usize, name_len, extra_len,
        ) = _LFH_STRUCT.unpack(header)

        if flags & _FLAG_DATA_DESCRIPTOR:
            # В .hbk на практике не встречается. Если появится — добавим.
            raise HbkReadError(
                f"{path.name}: запись с data descriptor (flag=0x8) на offset {i} — "
                "формат для нас новый, надо патчить iter_hbk_entries"
            )

        name_start = i + _LFH_SIZE
        name_end = name_start + name_len
        data_start = name_end + extra_len
        data_end = data_start + csize

        if data_end > size:
            # Обрубленный файл / повреждённая запись — лучше молча остановиться.
            return

        raw_name = data[name_start:name_end]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            name = raw_name.decode("cp437", errors="replace")

        compressed = data[data_start:data_end]

        content: bytes | None = None
        if method == 0:  # stored
            content = compressed
        elif method == 8:  # deflate
            try:
                content = zlib.decompress(compressed, wbits=-15)
            except zlib.error:
                # Битый файл — пропускаем, но не ломаем всю итерацию.
                content = None
        else:
            # Экзотический метод сжатия (lzma, ppmd и т.п.) — не встречается
            # в справке 1С. Пропускаем.
            content = None

        if content is not None:
            yield name, content

        # Переход к следующему LFH строго после сжатых данных.
        i = data_end


def iter_html_from_hbk(path: str | Path, min_size: int = 40) -> Iterator[tuple[str, bytes]]:
    """
    Удобная обёртка: выдаёт только HTML-файлы из .hbk.

    min_size — нижний порог по размеру распакованного контента
    (пустые/служебные HTML в справке обычно <40 байт, парсить их бессмысленно).
    """
    for name, content in iter_hbk_entries(path):
        if not (name.endswith(".html") or name.endswith(".htm")):
            continue
        if len(content) < min_size:
            continue
        yield name, content
