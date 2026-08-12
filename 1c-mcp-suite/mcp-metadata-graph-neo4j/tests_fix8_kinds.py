"""
Тесты FIX-8 — восемь ранее неразбираемых видов объектов.
=========================================================

До правки парсер знал 35 видов из 43, встреченных в боевой конфигурации.
Следствия были двух родов:

  • 156 неразрешённых ссылок при сборке графа — подсистемы ссылались на
    XDTO-пакеты и элементы стиля, а целей в графе не существовало;

  • `Последовательность` и `ВнешнийИсточникДанных` ЕСТЬ в языке запросов
    (TABLE_PREFIXES в query_parser), и `query_validate` по ним отвечал
    «таблица не найдена» — то есть браковал корректный запрос. Это хуже
    пропуска: агент по такому ответу перепишет верный запрос на неверный.

Запуск:
    python tests_fix8_kinds.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from metadata_xml import (KIND_BY_DIR, KIND_BY_ENG, KINDS, build_graph,
                          walk_workspace)

NEW_KINDS = {
    "Sequences":           ("Sequence",           "Последовательность"),
    "ExternalDataSources": ("ExternalDataSource", "ВнешнийИсточникДанных"),
    "DocumentNumerators":  ("DocumentNumerator",  "НумераторДокументов"),
    "CommandGroups":       ("CommandGroup",       "ГруппаКоманд"),
    "StyleItems":          ("StyleItem",          "ЭлементСтиля"),
    "Styles":              ("Style",              "Стиль"),
    "WSReferences":        ("WSReference",        "WSСсылка"),
    "XDTOPackages":        ("XDTOPackage",        "ПакетXDTO"),
}

OBJ_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
 <{eng} uuid="u-{name}">
  <Properties>
   <Name>{name}</Name>
   <Synonym>
    <v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core">
     <v8:lang>ru</v8:lang><v8:content>{syn}</v8:content>
    </v8:item>
   </Synonym>
  </Properties>
  <ChildObjects>{children}</ChildObjects>
 </{eng}>
</MetaDataObject>
"""

DIMENSION = """
   <Dimension uuid="d-{n}">
    <Properties><Name>{n}</Name>
     <Type><v8:Type xmlns:v8="http://v8.1c.ru/8.1/data/core">cfg:CatalogRef.Орг</v8:Type></Type>
    </Properties>
   </Dimension>
"""


class TestKindsTable(unittest.TestCase):

    def test_all_eight_registered(self):
        for d, (eng, ru) in NEW_KINDS.items():
            with self.subTest(kind=d):
                self.assertIn(d, KIND_BY_DIR)
                self.assertEqual(KIND_BY_DIR[d][1], eng)
                self.assertEqual(KIND_BY_DIR[d][2], ru)
                self.assertIn(eng, KIND_BY_ENG)

    def test_no_duplicate_dirs_or_names(self):
        """
        Дубль в таблице означал бы, что один каталог разбирается дважды и
        объекты попадают в граф по два раза.
        """
        for idx, label in ((0, "каталог"), (1, "kind_eng"), (2, "kind_ru")):
            values = [k[idx] for k in KINDS]
            self.assertEqual(len(values), len(set(values)), f"дубль: {label}")

    def test_query_language_kinds_are_covered(self):
        """
        Главное следствие FIX-8: виды, которые есть в языке запросов,
        обязаны быть и в графе, иначе query_validate бракует верный запрос.
        """
        ru_names = {k[2] for k in KINDS}
        self.assertIn("Последовательность", ru_names)
        self.assertIn("ВнешнийИсточникДанных", ru_names)


class TestParsing(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, dir_name, name, children=""):
        eng = KIND_BY_DIR[dir_name][1]
        d = self.root / dir_name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.xml").write_text(
            OBJ_XML.format(eng=eng, name=name, syn=f"Синоним {name}",
                           children=children),
            encoding="utf-8")

    def test_each_new_kind_parses(self):
        for d in NEW_KINDS:
            self._write(d, f"Объект{d}")
        objs = {o.kind_eng: o for o in walk_workspace(self.root)}
        for d, (eng, ru) in NEW_KINDS.items():
            with self.subTest(kind=d):
                self.assertIn(eng, objs, f"{d} не разобрался")
                self.assertEqual(objs[eng].kind_ru, ru)
                self.assertTrue(objs[eng].synonym)

    def test_sequence_dimensions_parsed(self):
        """
        У последовательности разбор даёт измерения сразу — общий
        _parse_object читает ChildObjects независимо от вида.
        """
        self._write("Sequences", "ПоНоменклатуре",
                    children=DIMENSION.format(n="Организация"))
        objs = walk_workspace(self.root)
        seq = [o for o in objs if o.kind_eng == "Sequence"][0]
        self.assertEqual([a.name for a in seq.attributes], ["Организация"])

    def test_nodes_land_in_graph(self):
        self._write("Sequences", "Посл1")
        self._write("XDTOPackages", "Пакет1")
        graph = build_graph(walk_workspace(self.root))
        ids = {n["id"] for n in graph["meta_nodes"]}
        self.assertIn("Sequence.Посл1", ids)
        self.assertIn("XDTOPackage.Пакет1", ids)

    def test_external_data_source_tables_not_parsed(self):
        """
        Зафиксированное ограничение, а не дефект: таблицы внешнего источника
        лежат в подкаталогах, а walk_workspace намеренно туда не заходит.
        Узел объекта появляется, состав полей — нет. Тест упадёт, если
        ограничение однажды снимут, и тогда README надо будет поправить.
        """
        self._write("ExternalDataSources", "ВнешняяБаза")
        tables = self.root / "ExternalDataSources" / "ВнешняяБаза" / "Tables"
        tables.mkdir(parents=True)
        (tables / "Таблица1.xml").write_text("<x/>", encoding="utf-8")

        objs = walk_workspace(self.root)
        eds = [o for o in objs if o.kind_eng == "ExternalDataSource"]
        self.assertEqual(len(eds), 1, "узел самого источника должен быть")
        self.assertEqual(eds[0].attributes, [],
                         "состав таблиц пока не разбирается — см. FIX-8")

    def test_old_kinds_still_work(self):
        """Добавление видов не должно ломать разбор прежних."""
        self._write("Catalogs", "Контрагенты")
        self._write("Documents", "Заказ")
        objs = {o.kind_eng for o in walk_workspace(self.root)}
        self.assertEqual(objs, {"Catalog", "Document"})

    def test_missing_dirs_are_skipped(self):
        """Конфигурация без последовательностей не должна падать."""
        self._write("Catalogs", "К")
        self.assertEqual(len(walk_workspace(self.root)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
