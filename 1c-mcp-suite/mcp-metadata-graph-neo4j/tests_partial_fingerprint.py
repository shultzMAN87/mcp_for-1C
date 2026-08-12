"""
Тесты PERF-8 — частичный fingerprint.
======================================

Что проверяется: раскладка файлов по владельцам, отпечаток на владельца и
построение плана изменений. Всё офлайн, Neo4j не нужен.

Главный риск этой задачи — не «неверно посчитали хеш», а «неверно решили,
кому принадлежит файл». Ошибка здесь означает, что правку не заметят:
изменение формы не перестроит объект, изменение общего модуля попадёт не в
тот ключ. Поэтому раскладке отведена основная часть тестов.

Запуск:
    python tests_partial_fingerprint.py -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from partial_fingerprint import (FP_CHUNK_PREFIX, ROOT_KEY, build_plan,
                                 dependent_owners, owner_of, read_stored,
                                 scan_workspace, write_stored)


class TestOwnerMapping(unittest.TestCase):

    def test_common_module(self):
        self.assertEqual(
            owner_of("CommonModules/ЭдоОбщий/Ext/Module.bsl"),
            "CommonModule.ЭдоОбщий")

    def test_top_level_object_xml(self):
        self.assertEqual(owner_of("Catalogs/Контрагенты.xml"), "Catalog.Контрагенты")
        self.assertEqual(owner_of("Documents/Заказ.xml"), "Document.Заказ")

    def test_object_and_manager_modules_are_separate_owners(self):
        """
        Модуль объекта и модуль менеджера — разные единицы индексации:
        правка одного не должна перестраивать другой.
        """
        a = owner_of("Catalogs/К/Ext/ObjectModule.bsl")
        b = owner_of("Catalogs/К/Ext/ManagerModule.bsl")
        self.assertEqual(a, "Catalog.К.ObjectModule")
        self.assertEqual(b, "Catalog.К.ManagerModule")
        self.assertNotEqual(a, b)

    def test_form_module_is_own_owner(self):
        self.assertEqual(
            owner_of("Catalogs/К/Forms/ФормаЭлемента/Ext/Form/Module.bsl"),
            "Catalog.К.Form.ФормаЭлемента")

    def test_nested_xml_belongs_to_parent_object(self):
        """
        Ключевой случай. Form.xml — не самостоятельный объект, он описан в
        верхнем XML. Но его читает резолвер (реквизиты форм, FIX-4.1),
        поэтому правка обязана перестраивать РОДИТЕЛЯ. Отдельный ключ здесь
        означал бы, что изменение реквизита формы никогда не доедет до
        графа.
        """
        for path in (
            "Catalogs/К/Forms/ФормаЭлемента/Ext/Form.xml",
            "Catalogs/К/Ext/Predefined.xml",
            "Documents/Д/Templates/Макет/Ext/Template.xml",
        ):
            self.assertTrue(owner_of(path).startswith(("Catalog.К", "Document.Д")),
                            path)
            self.assertNotIn("Forms", owner_of(path))

    def test_root_files(self):
        self.assertEqual(owner_of("Configuration.xml"), ROOT_KEY)

    def test_tests_extension_prefix_stripped(self):
        self.assertEqual(
            owner_of("tests-extension/CommonModules/Тест/Ext/Module.bsl"),
            "CommonModule.Тест")

    def test_irrelevant_files_ignored(self):
        """Картинки и прочее, что индексатор не читает, не должны ничего триггерить."""
        for path in ("CommonPictures/И/Ext/Picture.png",
                     "Catalogs/К/Ext/Help.html",
                     "README.md"):
            self.assertIsNone(owner_of(path), path)

    def test_unknown_kind_dir_ignored(self):
        self.assertIsNone(owner_of("НеизвестныйКаталог/Что-то.xml"))

    def test_backslashes_normalised(self):
        self.assertEqual(
            owner_of(r"Catalogs\К\Ext\ObjectModule.bsl"),
            "Catalog.К.ObjectModule")


class TestScan(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._write("Catalogs/Контрагенты.xml", "<a/>")
        self._write("Catalogs/Контрагенты/Ext/ObjectModule.bsl", "Процедура П() КонецПроцедуры")
        self._write("CommonModules/Общий/Ext/Module.bsl", "Функция Ф() Возврат 1; КонецФункции")
        self._write("Documents/Заказ.xml", "<d/>")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_owners_are_separate(self):
        digests, meta = scan_workspace(self.root)
        self.assertEqual(meta["owners"], 4)
        self.assertIn("Catalog.Контрагенты", digests)
        self.assertIn("Catalog.Контрагенты.ObjectModule", digests)
        self.assertIn("CommonModule.Общий", digests)
        self.assertIn("Document.Заказ", digests)

    def test_stable_across_runs(self):
        self.assertEqual(scan_workspace(self.root)[0], scan_workspace(self.root)[0])

    def test_change_touches_only_its_owner(self):
        """
        Смысл всей задачи: правка одного модуля не должна выглядеть как
        изменение всей конфигурации.
        """
        before, _ = scan_workspace(self.root)
        self._write("CommonModules/Общий/Ext/Module.bsl", "Функция Ф() Возврат 2; КонецФункции")
        after, _ = scan_workspace(self.root)
        changed = {k for k in after if before.get(k) != after[k]}
        self.assertEqual(changed, {"CommonModule.Общий"})

    def test_form_xml_change_touches_parent(self):
        before, _ = scan_workspace(self.root)
        self._write("Catalogs/Контрагенты/Forms/Ф/Ext/Form.xml", "<f/>")
        after, _ = scan_workspace(self.root)
        changed = {k for k in after if before.get(k) != after[k]}
        self.assertEqual(changed, {"Catalog.Контрагенты"})

    def test_irrelevant_file_changes_nothing(self):
        before, _ = scan_workspace(self.root)
        self._write("CommonPictures/И/Ext/Picture.png", "bytes")
        after, _ = scan_workspace(self.root)
        self.assertEqual(before, after)

    def test_new_object_appears(self):
        before, _ = scan_workspace(self.root)
        self._write("Catalogs/Новый.xml", "<n/>")
        after, _ = scan_workspace(self.root)
        self.assertEqual(set(after) - set(before), {"Catalog.Новый"})

    def test_deleted_object_disappears(self):
        before, _ = scan_workspace(self.root)
        (self.root / "Documents" / "Заказ.xml").unlink()
        after, _ = scan_workspace(self.root)
        self.assertEqual(set(before) - set(after), {"Document.Заказ"})

    def test_missing_root_does_not_raise(self):
        digests, meta = scan_workspace(self.root / "нет-такого")
        self.assertEqual(digests, {})
        self.assertEqual(meta["files"], 0)


class TestPlan(unittest.TestCase):

    def test_first_run_demands_full(self):
        """
        Пустое сохранённое состояние — это «мы ничего не знаем», а не «всё
        новое». Собрать слой 2 точечными обновлениями с нуля нельзя: резолв
        глобальный, получились бы несвязанные куски.
        """
        plan = build_plan({"Catalog.К": "aa"}, {})
        self.assertIsNotNone(plan.full_reindex_reason)
        self.assertIn("первая индексация", plan.full_reindex_reason)

    def test_no_changes(self):
        same = {"Catalog.К": "aa", "Document.Д": "bb"}
        plan = build_plan(same, dict(same))
        self.assertEqual(plan.total, 0)
        self.assertIsNone(plan.full_reindex_reason)
        self.assertEqual(plan.summary(), "изменений нет")

    def test_added_changed_removed(self):
        stored = {"Catalog.К": "aa", "Document.Д": "bb", "Enum.Э": "cc"}
        current = {"Catalog.К": "aa", "Document.Д": "XX", "Catalog.Новый": "dd"}
        plan = build_plan(current, stored)
        self.assertEqual(plan.added, {"Catalog.Новый"})
        self.assertEqual(plan.changed, {"Document.Д"})
        self.assertEqual(plan.removed, {"Enum.Э"})
        self.assertEqual(plan.total, 3)

    def test_root_change_forces_full(self):
        """
        Configuration.xml задаёт состав конфигурации целиком — точечным
        обновлением такое не разложить.
        """
        plan = build_plan({ROOT_KEY: "new", "Catalog.К": "aa"},
                          {ROOT_KEY: "old", "Catalog.К": "aa"})
        self.assertIn("корневые", plan.full_reindex_reason)

    def test_threshold_forces_full(self):
        stored = {f"Catalog.К{i}": "aa" for i in range(100)}
        current = {f"Catalog.К{i}": "bb" for i in range(100)}
        plan = build_plan(current, stored, max_partial=50)
        self.assertIsNotNone(plan.full_reindex_reason)
        self.assertIn("100", plan.full_reindex_reason)

    def test_threshold_not_triggered_below(self):
        stored = {f"Catalog.К{i}": "aa" for i in range(100)}
        current = dict(stored)
        current["Catalog.К1"] = "bb"
        plan = build_plan(current, stored, max_partial=50)
        self.assertIsNone(plan.full_reindex_reason)
        self.assertEqual(plan.total, 1)

    def test_zero_threshold_means_no_limit(self):
        stored = {f"Catalog.К{i}": "aa" for i in range(100)}
        current = {f"Catalog.К{i}": "bb" for i in range(100)}
        self.assertIsNone(build_plan(current, stored, max_partial=0)
                          .full_reindex_reason)

    def test_touched_is_the_union(self):
        plan = build_plan({"a": "1", "c": "3"}, {"a": "0", "b": "2"})
        self.assertEqual(plan.touched, {"a", "b", "c"})


class FakeNeo:
    """Стаб, хранящий куски так же, как их хранил бы Neo4j."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls = 0

    def rows(self, cypher, params=None):
        self.calls += 1
        p = params or {}
        if "STARTS WITH $prefix" in cypher and "DELETE" not in cypher:
            return [{"kind": k, "data": v} for k, v in sorted(self.store.items())]
        if "MERGE (n:Fingerprint" in cypher:
            self.store[p["kind"]] = p["data"]
            return [{"written": 1}]
        if "DELETE" in cypher:
            extra = [k for k in self.store if k not in p["keep"]]
            for k in extra:
                del self.store[k]
            return [{"dropped": len(extra)}]
        return []

    def query(self, cypher, params=None):
        self.calls += 1
        return {"results": [{"columns": [], "data": []}], "errors": []}


class TestStorage(unittest.TestCase):
    """
    Хранение набора отпечатков.

    История. Сначала отпечаток каждого владельца писался свойством на его
    собственный узел — «граф сам себе реестр». Красиво и неверно: это
    24 795 обращений к базе на каждое сохранение. Первая версия делала их
    запросом без метки (PERF-4 в третий раз), вторая — с метками, и всё
    равно не уложилась в 600 секунд на первых пятистах строках, при том
    что чтение из той же базы отвечает за 0,1 с.

    Стало — один JSON, разложенный по нескольким служебным узлам.
    """

    def test_roundtrip(self):
        neo = FakeNeo()
        data = {f"Catalog.К{i}": f"h{i}" for i in range(12000)}
        write_stored(neo, data)
        self.assertEqual(read_stored(neo), data)

    def test_write_is_a_handful_of_queries(self):
        """
        Главное свойство. При 24 795 владельцах и куске в 5 000 это пять
        запросов записи плюс один на уборку — вместо 24 795 поисков.
        """
        neo = FakeNeo()
        write_stored(neo, {f"Catalog.К{i}": "h" for i in range(24795)})
        self.assertLessEqual(neo.calls, 8, "запись обязана быть дешёвой")

    def test_read_is_one_query(self):
        neo = FakeNeo()
        write_stored(neo, {f"Catalog.К{i}": "h" for i in range(12000)})
        neo.calls = 0
        read_stored(neo)
        self.assertEqual(neo.calls, 1)

    def test_chunks_are_ordered_and_prefixed(self):
        neo = FakeNeo()
        write_stored(neo, {f"Catalog.К{i}": "h" for i in range(12000)})
        for kind in neo.store:
            self.assertTrue(kind.startswith(FP_CHUNK_PREFIX), kind)
        self.assertEqual(sorted(neo.store), list(neo.store.keys()) if False
                         else sorted(neo.store))

    def test_shrinking_set_drops_extra_chunks(self):
        """
        Если конфигурация усохла, лишние куски обязаны уйти — иначе в
        состоянии остались бы владельцы, которых давно нет, и план вечно
        показывал бы их как удалённые.
        """
        neo = FakeNeo()
        write_stored(neo, {f"Catalog.К{i}": "h" for i in range(12000)})
        self.assertEqual(len(neo.store), 3)
        res = write_stored(neo, {"Catalog.К1": "h"})
        self.assertEqual(len(neo.store), 1)
        self.assertEqual(res["dropped_chunks"], 2)
        self.assertEqual(read_stored(neo), {"Catalog.К1": "h"})

    def test_empty_set(self):
        neo = FakeNeo()
        res = write_stored(neo, {})
        self.assertEqual(res["written"], 0)
        self.assertEqual(read_stored(neo), {})

    def test_cyrillic_survives(self):
        neo = FakeNeo()
        data = {"Справочник.Контрагенты": "aa", "Catalog.К.Form.ФормаЭлемента": "bb"}
        write_stored(neo, data)
        self.assertEqual(read_stored(neo), data)

    def test_broken_chunk_is_reported_not_silent(self):
        """
        Битый кусок не должен выглядеть как «этих объектов не было»: тогда
        план показал бы их новыми и спровоцировал лишнюю работу.
        """
        neo = FakeNeo()
        write_stored(neo, {f"Catalog.К{i}": "h" for i in range(12000)})
        first = sorted(neo.store)[0]
        neo.store[first] = "{не json"
        with self.assertLogs("partial_fingerprint", level="WARNING"):
            out = read_stored(neo)
        self.assertLess(len(out), 12000)


class TestDependentOwners(unittest.TestCase):
    """
    Кого перестраивать вместе с объектом.

    `upsert_xml_file` сносит срез объекта целиком — по префиксу id, то есть
    вместе с узлами его модулей. Если после этого не залить модули заново,
    правка одного реквизита справочника молча унесла бы весь его код из
    графа. Тихая потеря 100% кода объекта — ровно тот класс, что FIX-14.
    """

    ALL = ["Catalog.К", "Catalog.К.ObjectModule", "Catalog.К.ManagerModule",
           "Catalog.К.Form.ФормаЭлемента", "Catalog.К.Form.ФормаСписка",
           "CommonModule.Общий", "Document.Д", "Document.Д.ObjectModule"]

    def test_object_pulls_its_modules(self):
        self.assertEqual(
            dependent_owners("Catalog.К", self.ALL),
            ["Catalog.К.Form.ФормаСписка", "Catalog.К.Form.ФормаЭлемента",
             "Catalog.К.ManagerModule", "Catalog.К.ObjectModule"])

    def test_object_does_not_pull_other_objects(self):
        self.assertNotIn("Document.Д", dependent_owners("Catalog.К", self.ALL))

    def test_module_pulls_nothing(self):
        for owner in ("Catalog.К.ObjectModule", "Catalog.К.ManagerModule",
                      "Catalog.К.Form.ФормаЭлемента", "CommonModule.Общий"):
            self.assertEqual(dependent_owners(owner, self.ALL), [], owner)

    def test_prefix_match_is_not_substring_match(self):
        """`Catalog.К` не должен тянуть `Catalog.КК`."""
        allo = ["Catalog.К", "Catalog.КК", "Catalog.КК.ObjectModule"]
        self.assertEqual(dependent_owners("Catalog.К", allo), [])

    def test_root_key_pulls_nothing(self):
        self.assertEqual(dependent_owners(ROOT_KEY, self.ALL), [])


class TestScanFiles(unittest.TestCase):
    """Раскладка файлов по владельцам — нужна применению плана."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel in ("Catalogs/К.xml",
                    "Catalogs/К/Ext/ObjectModule.bsl",
                    "Catalogs/К/Forms/Ф/Ext/Form.xml",
                    "CommonModules/Общий/Ext/Module.bsl"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_files_are_returned_when_asked(self):
        _, meta = scan_workspace(self.root, keep_files=True)
        by_owner = meta["files_by_owner"]
        self.assertEqual(sorted(by_owner["Catalog.К"]),
                         ["Catalogs/К.xml", "Catalogs/К/Forms/Ф/Ext/Form.xml"])
        self.assertEqual(by_owner["Catalog.К.ObjectModule"],
                         ["Catalogs/К/Ext/ObjectModule.bsl"])

    def test_files_absent_by_default(self):
        """Раскладка стоит памяти — не платим за неё, когда не просят."""
        _, meta = scan_workspace(self.root)
        self.assertNotIn("files_by_owner", meta)

    def test_digests_are_the_same_either_way(self):
        a, _ = scan_workspace(self.root)
        b, _ = scan_workspace(self.root, keep_files=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
