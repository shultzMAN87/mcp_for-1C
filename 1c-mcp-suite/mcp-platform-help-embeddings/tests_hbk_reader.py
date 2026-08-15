"""
Тесты читателя .hbk (HBK-1).

Главный тест здесь — `TestBlockBoundaries`: ZIP-поток, который не помещается
в один блок контейнера. Ровно на этом ломался прежний читатель, и ровно это
нельзя увидеть на маленьком примере, если строить его «как получится» —
поэтому контейнер собирается синтетически, с заведомо маленьким размером
блока.

Файлы .hbk в git не лежат (это файлы 1С), поэтому тесты на живых
контейнерах включаются сами, если каталог platform-help-data непуст, и
пропускаются, если его нет.

Запуск:
    python3 tests_hbk_reader.py
"""

from __future__ import annotations

import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hbk_reader
from shortfall import Tally
from hbk_reader import (
    HbkReadError,
    hbk_inventory,
    is_v8_container,
    iter_container_elements,
    iter_hbk_entries,
    iter_html_from_hbk,
    looks_like_html,
)

EMPTY_ADDR = 0x7FFFFFFF
PAGE = 512


# ─── Сборка синтетического V8-контейнера ─────────────────────────────────

class ContainerBuilder:
    """
    Собирает контейнер того же вида, что и настоящий .hbk.

    Блоки одинакового размера `page`, заголовок блока — 31 байт ASCII.
    Маленький `page` в тестах взят намеренно: он заставляет документы
    разбиваться на несколько блоков даже на коротких данных.
    """

    def __init__(self, page: int = PAGE):
        self.page = page
        self.buffer = bytearray(b"\xff\xff\xff\x7f")          # адрес свободной страницы
        self.buffer += struct.pack("<III", page, 0, 0)        # размер страницы + служебное

    def _write_document(self, payload: bytes) -> int:
        """Кладёт документ цепочкой блоков, возвращает адрес первого блока."""
        first = len(self.buffer)
        blocks = [payload[i:i + self.page] for i in range(0, len(payload), self.page)] or [b""]
        doc_len = len(payload)

        # Адреса блоков известны заранее: каждый занимает 31 + page байт.
        step = 31 + self.page
        addresses = [first + i * step for i in range(len(blocks))]

        for index, block in enumerate(blocks):
            is_last = index == len(blocks) - 1
            next_addr = EMPTY_ADDR if is_last else addresses[index + 1]
            header = "\r\n%08x %08x %08x \r\n" % (
                doc_len if index == 0 else 0,
                self.page,
                next_addr,
            )
            self.buffer += header.encode("ascii")
            self.buffer += block.ljust(self.page, b"\x00")

        return first

    def build(self, elements: list[tuple[str, bytes]]) -> bytes:
        """
        Собирает контейнер из (имя, содержимое).

        Таблица элементов лежит по смещению 16 и пишется последней, поэтому
        сначала резервируем под неё место, потом записываем данные, потом
        перезаписываем таблицу — как это и устроено в контейнере.
        """
        table_payload = b"\x00" * (12 * len(elements))
        table_addr = self._write_document(table_payload)
        assert table_addr == 16, "таблица элементов должна лежать сразу за шапкой"

        rows = []
        for name, content in elements:
            header = (
                b"\x00" * 20
                + name.encode("utf-16le")
                + b"\x00\x00\x00\x00"
            )
            header_addr = self._write_document(header)
            data_addr = self._write_document(content) if content else EMPTY_ADDR
            rows.append(struct.pack("<III", header_addr, data_addr, EMPTY_ADDR))

        # Перезаписываем таблицу поверх зарезервированного места.
        body_start = table_addr + 31
        packed = b"".join(rows)
        self.buffer[body_start:body_start + len(packed)] = packed
        return bytes(self.buffer)


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buf.getvalue()


def make_hbk(entries: dict[str, bytes], *, page: int = PAGE,
             element: str = "FileStorage") -> bytes:
    return ContainerBuilder(page).build([
        ("Book", b"\xef\xbb\xbf{7,\"test\"}"),
        (element, make_zip(entries)),
        ("MainData", b"\x00" * 20),
    ])


def write_temp(data: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=".hbk", delete=False)
    handle.write(data)
    handle.close()
    return Path(handle.name)


PAGE_HTML = b"\xef\xbb\xbf<HTML><HEAD>\r\n<META http-equiv=Content-Type></HEAD><BODY>"
PAGE_HTML += b"<h1 class=\"V8SH_pagetitle\">\xd0\x9a\xd0\x9e\xd0\x9d\xd0\x95\xd0\xa6</h1></BODY></HTML>"


# ─── Тесты ───────────────────────────────────────────────────────────────

class TestContainerStructure(unittest.TestCase):
    """Разбор контейнера: таблица элементов, имена, содержимое."""

    def test_detects_container(self):
        self.assertTrue(is_v8_container(make_hbk({"page": PAGE_HTML})))

    def test_plain_zip_is_not_container(self):
        self.assertFalse(is_v8_container(make_zip({"page": PAGE_HTML})))

    def test_short_buffer_is_not_container(self):
        self.assertFalse(is_v8_container(b"\xff\xff\xff\x7f"))

    def test_element_names(self):
        data = make_hbk({"page": PAGE_HTML})
        names = [name for name, _ in iter_container_elements(data)]
        self.assertEqual(names, ["Book", "FileStorage", "MainData"])

    def test_element_content_is_exact(self):
        """Хвост последнего блока добит нулями — документ обязан быть обрезан."""
        data = make_hbk({"page": PAGE_HTML})
        book = dict(iter_container_elements(data))["Book"]
        self.assertEqual(book, b"\xef\xbb\xbf{7,\"test\"}")

    def test_empty_data_address(self):
        data = ContainerBuilder().build([("Book", b"")])
        self.assertEqual(dict(iter_container_elements(data))["Book"], b"")

    def test_broken_table_raises(self):
        data = bytearray(make_hbk({"page": PAGE_HTML}))
        data[16:20] = b"XXXX"  # портим заголовок блока таблицы
        with self.assertRaises(HbkReadError):
            list(iter_container_elements(bytes(data)))


class TestBlockBoundaries(unittest.TestCase):
    """
    Регрессия HBK-1: ZIP шире одного блока.

    Прежний читатель шёл по файлу подряд и упирался в заголовок следующего
    блока, врезанный в середину ZIP-потока. Здесь блок намеренно мелкий,
    так что граница попадает внутрь записей.
    """

    def test_zip_spanning_many_blocks(self):
        pages = {f"page{i}": PAGE_HTML + b"<p>%d</p>" % i for i in range(40)}
        path = write_temp(make_hbk(pages, page=128))
        try:
            got = dict(iter_hbk_entries(path))
        finally:
            path.unlink()
        self.assertEqual(len(got), 40)
        self.assertEqual(set(got), set(pages))

    def test_no_page_is_lost_at_boundary(self):
        """Содержимое каждой записи совпадает байт в байт."""
        pages = {f"page{i}": PAGE_HTML + b"x" * (i * 37) for i in range(20)}
        path = write_temp(make_hbk(pages, page=64))
        try:
            got = dict(iter_hbk_entries(path))
        finally:
            path.unlink()
        self.assertEqual(got, pages)

    def test_old_style_sequential_scan_would_lose_pages(self):
        """
        Страховка от возврата старого способа чтения.

        Если кто-то снова начнёт искать `PK\\x03\\x04` по всему файлу, тест
        напомнит, почему так нельзя: записей найдётся меньше, чем есть.
        """
        pages = {f"page{i}": PAGE_HTML for i in range(40)}
        raw = make_hbk(pages, page=128)
        naive = list(hbk_reader._iter_zip_via_lfh(raw, "naive"))
        self.assertLess(len(naive), 40)


class TestEntryNames(unittest.TestCase):
    def test_names_without_extension(self):
        path = write_temp(make_hbk({"LEFTJOIN": PAGE_HTML, "root.html": PAGE_HTML}))
        try:
            names = [name for name, _ in iter_hbk_entries(path)]
        finally:
            path.unlink()
        self.assertIn("LEFTJOIN", names)
        self.assertIn("root.html", names)

    def test_russian_name_survives(self):
        path = write_temp(make_hbk({"Справочники": PAGE_HTML}))
        try:
            names = [name for name, _ in iter_hbk_entries(path)]
        finally:
            path.unlink()
        self.assertEqual(names, ["Справочники"])


class TestHtmlDetection(unittest.TestCase):
    """Отбор HTML по содержимому, а не по расширению."""

    def test_bom_and_uppercase(self):
        self.assertTrue(looks_like_html(b"\xef\xbb\xbf<HTML><HEAD>"))

    def test_doctype(self):
        self.assertTrue(looks_like_html(b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">'))

    def test_leading_whitespace(self):
        self.assertTrue(looks_like_html(b"\r\n  <html>"))

    def test_png_is_not_html(self):
        self.assertFalse(looks_like_html(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"))

    def test_1c_structure_is_not_html(self):
        self.assertFalse(looks_like_html(b'\xef\xbb\xbf{1,\r\n{2,\r\n{"",1,0,"",""}'))

    def test_empty_is_not_html(self):
        self.assertFalse(looks_like_html(b""))
        self.assertFalse(looks_like_html(b"\xef\xbb\xbf"))

    def test_html_deeper_than_sniff_window_is_not_html(self):
        """Нюхаем начало: текст с <html> в середине страницей не считается."""
        self.assertFalse(looks_like_html(b"x" * 400 + b"<html>"))


class TestIterHtml(unittest.TestCase):
    def test_extensionless_pages_are_returned(self):
        path = write_temp(make_hbk({
            "LEFTJOIN": PAGE_HTML,
            "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
            "struct_For.st": b'\xef\xbb\xbf{1,\r\n{2,\r\n{"",1,0,"",""},\r\n{0,\r\n{"ru"}}}',
        }))
        try:
            names = [name for name, _ in iter_html_from_hbk(path)]
        finally:
            path.unlink()
        self.assertEqual(names, ["LEFTJOIN"])

    def test_min_size_filters_service_pages(self):
        path = write_temp(make_hbk({
            "_CONTENTS_NODE_fileEnterprise": b"\xef\xbb\xbf<html><body></body></html>",
            "real": PAGE_HTML,
        }))
        try:
            names = [name for name, _ in iter_html_from_hbk(path)]
        finally:
            path.unlink()
        self.assertEqual(names, ["real"])

    def test_html_by_name_still_accepted(self):
        """
        Старый признак не выброшен: страница с расширением проходит, даже
        если начинается нестандартно.
        """
        path = write_temp(make_hbk({"weird.html": b"<META charset=utf-8>" + b"y" * 60}))
        try:
            names = [name for name, _ in iter_html_from_hbk(path)]
        finally:
            path.unlink()
        self.assertEqual(names, ["weird.html"])


class TestIterHtmlTally(unittest.TestCase):
    """
    A-1. Тот самый шаг, где отказ выглядел как результат: читатель отдавал
    25 страниц из 128 записей, и «25» ничем не отличалось от честного
    результата. Теперь вход, выход и причины отсева лежат в одном месте.
    """

    def _run(self, entries: dict) -> Tally:
        path = write_temp(make_hbk(entries))
        t = Tally("записи → страницы", unit="запись")
        try:
            list(iter_html_from_hbk(path, tally=t))
        finally:
            path.unlink()
        return t

    def test_counts_input_and_output(self):
        t = self._run({
            "LEFTJOIN": PAGE_HTML,
            "INNERJOIN": PAGE_HTML,
            "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
        })
        self.assertEqual(t.seen, 3)
        self.assertEqual(t.kept, 2)
        # Главное: разница объяснена, а не растворилась.
        self.assertEqual(t.unexplained, 0)
        self.assertIn("картинка", t.dropped)

    def test_service_pages_have_their_own_reason(self):
        t = self._run({
            "_CONTENTS_NODE_fileEnterprise": b"\xef\xbb\xbf<html><body></body></html>",
            "real": PAGE_HTML,
        })
        self.assertEqual(t.kept, 1)
        self.assertEqual(t.dropped.get("меньше порога размера"), 1)
        self.assertEqual(t.unexplained, 0)

    def test_container_without_pages_is_a_failure(self):
        """Ноль страниц при непустом входе — отказ, а не результат."""
        t = self._run({
            "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
            "logo2.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
        })
        self.assertEqual(t.kept, 0)
        self.assertFalse(t.ok)
        self.assertTrue(any("это отказ" in p for p in t.problems()))

    def test_tally_is_optional(self):
        path = write_temp(make_hbk({"LEFTJOIN": PAGE_HTML}))
        try:
            self.assertEqual([n for n, _ in iter_html_from_hbk(path)], ["LEFTJOIN"])
        finally:
            path.unlink()


class TestFallbacks(unittest.TestCase):
    """Файл, который не контейнер, читается как раньше — хуже не стало."""

    def test_plain_zip_file(self):
        path = write_temp(make_zip({"root.html": PAGE_HTML}))
        try:
            got = dict(iter_hbk_entries(path))
        finally:
            path.unlink()
        self.assertEqual(got, {"root.html": PAGE_HTML})

    def test_empty_file(self):
        path = write_temp(b"")
        try:
            self.assertEqual(list(iter_hbk_entries(path)), [])
        finally:
            path.unlink()

    def test_garbage_file(self):
        path = write_temp(b"\x00" * 4096)
        try:
            self.assertEqual(list(iter_hbk_entries(path)), [])
        finally:
            path.unlink()

    def test_container_without_filestorage(self):
        """Элемент назвали иначе — ZIP всё равно находим, но с предупреждением."""
        path = write_temp(make_hbk({"page": PAGE_HTML}, element="Storage2"))
        try:
            names = [name for name, _ in iter_hbk_entries(path)]
        finally:
            path.unlink()
        self.assertEqual(names, ["page"])


class TestInventory(unittest.TestCase):
    def test_counts_and_kinds(self):
        path = write_temp(make_hbk({
            "page1": PAGE_HTML,
            "page2": PAGE_HTML,
            "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
            "struct.st": b'\xef\xbb\xbf{1,\r\n{2,\r\n{"",1,0,"",""}}}',
            "_CONTENTS_NODE_file1": b"\xef\xbb\xbf",
        }))
        try:
            inv = hbk_inventory(path)
        finally:
            path.unlink()
        self.assertTrue(inv["is_container"])
        self.assertEqual(inv["entries"], 5)
        self.assertEqual(inv["html"], 2)
        self.assertEqual(inv["by_kind"].get("картинка"), 1)
        self.assertEqual(inv["by_kind"].get("структура 1С"), 1)
        self.assertEqual(inv["by_kind"].get("пусто"), 1)
        self.assertEqual(inv["elements"], ["Book", "FileStorage", "MainData"])
        self.assertEqual(inv["error"], "")


# ─── Живые контейнеры (если каталог со справкой на месте) ────────────────

def _real_hbk_files() -> list[Path]:
    base = Path(__file__).resolve().parents[2] / "platform-help-data"
    return sorted(base.glob("*.hbk")) if base.is_dir() else []


@unittest.skipUnless(_real_hbk_files(), "нет platform-help-data/*.hbk (файлы 1С не в git)")
class TestRealContainers(unittest.TestCase):
    """
    Приёмочная часть: на реальных файлах каждый контейнер обязан отдать
    страницы. Именно «0 HTML» и было симптомом HBK-1.
    """

    def test_every_container_parses(self):
        for path in _real_hbk_files():
            with self.subTest(file=path.name):
                self.assertTrue(is_v8_container(path.read_bytes()))

    def test_every_container_has_pages(self):
        empty = [p.name for p in _real_hbk_files()
                 if next(iter_html_from_hbk(p), None) is None]
        self.assertEqual(empty, [], f"контейнеры без страниц: {empty}")

    def test_pages_are_decodable(self):
        for path in _real_hbk_files()[:5]:
            for name, content in iter_html_from_hbk(path):
                content.decode("utf-8-sig")
                self.assertTrue(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
