"""
Тесты A-1: сверка «вход против выхода» в парсерах.

Проверяется не то, что счётчик существует, а то, что он ловит ровно те
случаи, которые в этом проекте уже случались и оставались невидимыми:

  • файл, который не разобрался, — с указанием причины;
  • каталог верхнего уровня, которого нет в KINDS (так прожил FIX-8);
  • BSL-файл вне известной схемы путей (так выглядела бы смена раскладки
    каталогов при переходе на другой формат выгрузки).

Запуск:  python3 tests_tally_wiring.py
"""

import tempfile
import unittest
from pathlib import Path

from bsl_parser import walk_workspace_bsl
from metadata_xml import unknown_kind_dirs, walk_workspace
from shortfall import Tally

OBJ_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
 <Catalog uuid="u-{name}">
  <Properties>
   <Name>{name}</Name>
  </Properties>
  <ChildObjects/>
 </Catalog>
</MetaDataObject>
"""

# Тот же файл, но без <Name> — объект молча не появлялся в графе.
OBJ_XML_NO_NAME = OBJ_XML.replace("<Name>{name}</Name>", "")


class TestWalkWorkspaceTally(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "Catalogs").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, body: str = OBJ_XML) -> None:
        (self.root / "Catalogs" / f"{name}.xml").write_text(
            body.format(name=name), encoding="utf-8")

    def test_clean_workspace_has_no_losses(self):
        for n in ("Контрагенты", "Номенклатура", "Организации"):
            self._write(n)
        t = Tally("разбор XML", unit="файл")
        objs = walk_workspace(self.root, tally=t)
        self.assertEqual(len(objs), 3)
        self.assertEqual(t.seen, 3)
        self.assertEqual(t.kept, 3)
        self.assertEqual(t.unexplained, 0)
        self.assertTrue(t.ok)

    def test_broken_xml_is_counted_with_reason(self):
        self._write("Хороший")
        (self.root / "Catalogs" / "Битый.xml").write_text(
            "<MetaDataObject><Catalog>", encoding="utf-8")
        t = Tally("разбор XML", unit="файл")
        objs = walk_workspace(self.root, tally=t)
        self.assertEqual(len(objs), 1)
        self.assertEqual(t.seen, 2)
        # Потеря объяснена — арифметика сходится…
        self.assertEqual(t.unexplained, 0)
        self.assertIn("битый XML", t.dropped)
        # …но причина помечена тревожной, поэтому шаг не «ok».
        self.assertFalse(t.ok)
        self.assertIn("Битый.xml", t.reasons_line())

    def test_object_without_name_is_counted(self):
        self._write("Хороший")
        self._write("Безымянный", OBJ_XML_NO_NAME)
        t = Tally("разбор XML", unit="файл")
        walk_workspace(self.root, tally=t)
        self.assertIn("объект без имени", t.dropped)
        self.assertEqual(t.unexplained, 0)

    def test_foreign_kind_in_kind_dir(self):
        """Файл чужого вида в каталоге вида — законный отсев, не тревога."""
        self._write("Хороший")
        (self.root / "Catalogs" / "Чужой.xml").write_text(
            OBJ_XML.replace("Catalog", "Document").format(name="Чужой"),
            encoding="utf-8")
        t = Tally("разбор XML", unit="файл")
        walk_workspace(self.root, tally=t)
        self.assertEqual(t.explained, t.lost)
        self.assertTrue(any("нет элемента" in r for r in t.dropped))
        self.assertTrue(t.ok)

    def test_tally_is_optional(self):
        """Без счётчика функция работает как раньше — сигнатура совместима."""
        self._write("Контрагенты")
        self.assertEqual(len(walk_workspace(self.root)), 1)


class TestUnknownKindDirs(unittest.TestCase):
    """
    Детектор смены раскладки. Так прожил FIX-8: восемь видов объектов
    лежали на диске, не индексировались, и ни одна строка лога об этом не
    говорила.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_known_dirs_are_silent(self):
        (self.root / "Catalogs").mkdir()
        (self.root / "Catalogs" / "X.xml").write_text("<a/>", encoding="utf-8")
        self.assertEqual(unknown_kind_dirs(self.root), [])

    def test_unknown_dir_with_xml_is_reported(self):
        (self.root / "СовсемНовыйВид").mkdir()
        for i in range(3):
            (self.root / "СовсемНовыйВид" / f"{i}.xml").write_text("<a/>", encoding="utf-8")
        self.assertEqual(unknown_kind_dirs(self.root), [("СовсемНовыйВид", 3)])

    def test_unknown_dir_without_xml_is_ignored(self):
        """Ext, Templates и прочая обвязка — не вид объектов."""
        (self.root / "Ext").mkdir()
        (self.root / "Ext" / "readme.txt").write_text("x", encoding="utf-8")
        self.assertEqual(unknown_kind_dirs(self.root), [])

    def test_missing_root_does_not_raise(self):
        self.assertEqual(unknown_kind_dirs(self.root / "нет-такого"), [])


class TestWalkWorkspaceBslTally(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _common_module(self, name: str) -> None:
        d = self.root / "CommonModules" / name / "Ext"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Module.bsl").write_text(
            "Процедура Тест() Экспорт\nКонецПроцедуры\n", encoding="utf-8")

    def test_input_count_matches_files(self):
        for n in ("МодульА", "МодульБ"):
            self._common_module(n)
        t = Tally("разбор BSL", unit="файл")
        modules = walk_workspace_bsl(self.root, tally=t)
        self.assertEqual(t.seen, 2)
        self.assertEqual(t.kept, len(modules))
        self.assertEqual(t.unexplained, 0)

    def test_file_outside_known_layout_is_counted(self):
        """
        Ровно тот случай, ради которого счётчик здесь и стоит: файл
        уезжал в log.debug, невидимый при штатном INFO, а итоговая строка
        показывала только число разобранных модулей.
        """
        self._common_module("МодульА")
        stray = self.root / "СовсемДругаяРаскладка"
        stray.mkdir()
        (stray / "Module.bsl").write_text("Процедура П() КонецПроцедуры",
                                          encoding="utf-8")
        t = Tally("разбор BSL", unit="файл")
        walk_workspace_bsl(self.root, tally=t)
        self.assertEqual(t.seen, 2)
        self.assertEqual(t.kept, 1)
        self.assertEqual(t.dropped.get("вне схемы путей"), 1)
        self.assertEqual(t.unexplained, 0)

    def test_mass_layout_change_trips_the_ratio(self):
        """Один файл вне схемы — норма. Девять из десяти — авария."""
        self._common_module("МодульА")
        stray = self.root / "EDT-подобная-раскладка"
        stray.mkdir()
        for i in range(9):
            (stray / f"m{i}.bsl").write_text("Процедура П() КонецПроцедуры",
                                             encoding="utf-8")
        t = Tally("разбор BSL", unit="файл", min_keep_ratio=0.9)
        walk_workspace_bsl(self.root, tally=t)
        self.assertFalse(t.ok)
        self.assertTrue(any("дошло" in p for p in t.problems()))

    def test_tally_is_optional(self):
        self._common_module("МодульА")
        self.assertEqual(len(walk_workspace_bsl(self.root)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
