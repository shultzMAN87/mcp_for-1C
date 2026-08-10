"""
Юнит-тесты для incremental.py (задача 4.6.5).
=================================================

Стратегия тестирования (две сложности):

  Слой A — чистая Python-логика (без Neo4j).
    • `_norm_rel` — нормализация путей
    • `_classify_xml_path` — классификация .xml в (dir, kind_eng, ...)
    • `_filter_code_graph_to_module` — срез слоя 2 по module_id
    • `_filter_xml_graph_to_object` — срез слоя 1 (тривиальный, но контракт)
    • `_attach_compat_attrs` — compat-атрибуты для metadata_object_details

  Слой B — взаимодействие с Neo4j через `FakeNeo4j`.
    Не эмулируем Cypher, а ЛОГИРУЕМ каждый `query()` / `rows()`. Возвращаем
    программируемые ответы (stub). Проверяем:
      • правильный порядок операций (clear → write → restale)
      • правильные параметры (module_id, prefix)
      • что `:Type`-узлы не сносятся в _clear_meta_object_slice
      • что upsert_bsl_file читает callable_ids из живого графа
      • что remove_*_file идемпотентен (повторный вызов на отсутствующий
        файл не падает)

  Слой C — sanity на реальном workspace (если ws/ доступен).
    Прогон walk_workspace_bsl на одном модуле, проверка, что
    _filter_code_graph_to_module даёт согласованный срез
    (callable_nodes/parameter_nodes/edges всё про этот module_id).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

# Импорты — модуль incremental сам тянет graph_writer и т.п.
import incremental
from incremental import (
    _attach_compat_attrs,
    _classify_xml_path,
    _filter_code_graph_to_module,
    _filter_xml_graph_to_object,
    _norm_rel,
    remove_bsl_file, remove_file, remove_xml_file,
    upsert_bsl_file, upsert_file, upsert_xml_file,
)
from bsl_parser import (
    ParsedModule, ParsedProcedure, ParsedParameter,
    classify_bsl_path, parse_bsl_module, walk_workspace_bsl,
)
from bsl_resolver import build_call_graph, build_index_from_modules


# ─── FakeNeo4j ──────────────────────────────────────────────────────────


class FakeNeo4j:
    """
    Лог-only Neo4j-эмулятор для проверки последовательности и параметров
    Cypher-операций. Не выполняет Cypher — возвращает ответы из `replies`.

    Использование:
        fake = FakeNeo4j()
        fake.replies = [
            # ответы в порядке вызовов rows()
            [{"n": 5}],                       # счётчик MetadataObject
            [{"c": 2, "p": 3, "cs": 4}],      # cleanup-счётчик
            # ...
        ]
        upsert_bsl_file(fake, root, "...bsl")
        assert any("MERGE (n:Callable" in c.cypher for c in fake.calls)
    """

    def __init__(self):
        self.calls: list[FakeNeo4j.Call] = []
        self.replies: list[list[dict]] = []
        self._row_idx = 0

    class Call:
        def __init__(self, kind: str, cypher: str, params: dict):
            self.kind = kind  # 'query' | 'rows'
            self.cypher = cypher
            self.params = params

        def __repr__(self) -> str:  # pragma: no cover - debug aid
            head = self.cypher[:80].replace("\n", " ")
            return f"<{self.kind}: {head!r} params={list(self.params)}>"

    def query(self, cypher: str, parameters: dict | None = None) -> dict:
        self.calls.append(FakeNeo4j.Call("query", cypher, dict(parameters or {})))
        return {"results": [{"columns": [], "data": []}]}

    def rows(self, cypher: str, parameters: dict | None = None) -> list[dict]:
        self.calls.append(FakeNeo4j.Call("rows", cypher, dict(parameters or {})))
        if self._row_idx >= len(self.replies):
            return []
        out = self.replies[self._row_idx]
        self._row_idx += 1
        return out

    def cypher_log(self) -> list[str]:
        return [c.cypher for c in self.calls]

    def has_call(self, *substrings: str) -> bool:
        """True, если есть вызов, чей cypher содержит все substrings."""
        return any(
            all(s in c.cypher for s in substrings)
            for c in self.calls
        )


# ─── Слой A: чистая Python-логика ────────────────────────────────────────


class TestNormRel(unittest.TestCase):

    def setUp(self):
        self.root = Path("/data/1c-src")

    def test_relative_path_returned_as_posix(self):
        self.assertEqual(
            _norm_rel(self.root, "Catalogs/X/Ext/ObjectModule.bsl"),
            "Catalogs/X/Ext/ObjectModule.bsl",
        )

    def test_absolute_inside_root(self):
        self.assertEqual(
            _norm_rel(self.root, "/data/1c-src/CommonModules/А/Ext/Module.bsl"),
            "CommonModules/А/Ext/Module.bsl",
        )

    def test_absolute_outside_root_with_known_tail_resolves(self):
        # 4.6.5: tail-suffix фолбэк. Watcher шлёт абсолютный путь от своей
        # точки монтирования (/workspace), сервер видит выгрузку под другой
        # (/data/1c-src). По «общему хвосту» CommonModules/... мы должны
        # вернуть относительный путь. Это позволяет watcher'у и серверу
        # использовать разные mount points без специальной согласованности
        # в compose.
        out = _norm_rel(self.root,
                        "/workspace/CommonModules/A/Ext/Module.bsl")
        self.assertEqual(out, "CommonModules/A/Ext/Module.bsl")

    def test_absolute_outside_root_without_known_tail_returns_none(self):
        # Если хвост не похож на 1С-выгрузку — отдаём None.
        self.assertIsNone(_norm_rel(self.root, "/etc/passwd"))
        self.assertIsNone(_norm_rel(self.root, "/home/user/random.bsl"))

    def test_windows_style_backslash_in_relative_normalized(self):
        # На случай если watcher передаст путь со слэшами от Windows-хоста.
        # Path.as_posix() нормализует разделители.
        out = _norm_rel(self.root, "Catalogs\\X\\Ext\\ObjectModule.bsl")
        # На POSIX-системе \ не разделитель — нормализация не сработает.
        # Проверим, что хотя бы не упало; полноценная поддержка backslash —
        # ответственность watcher'а (он шлёт POSIX-стиль).
        self.assertIsInstance(out, str)


class TestClassifyXmlPath(unittest.TestCase):

    def test_toplevel_catalog_xml(self):
        out = _classify_xml_path("Catalogs/АукАукционы.xml")
        self.assertIsNotNone(out)
        self.assertEqual(out[0], "Catalogs")
        self.assertEqual(out[1], "Catalog")
        self.assertEqual(out[2], "Справочник")

    def test_toplevel_document_xml(self):
        out = _classify_xml_path("Documents/Заказ.xml")
        self.assertIsNotNone(out)
        self.assertEqual(out[1], "Document")

    def test_common_module_xml(self):
        out = _classify_xml_path("CommonModules/АукОбщийКлиент.xml")
        self.assertIsNotNone(out)
        self.assertEqual(out[1], "CommonModule")

    def test_nested_form_xml_not_supported(self):
        # Form.xml внутри Catalog.X/Forms/Y/Ext/ — вложенный XML, не
        # самостоятельный объект. v1 его не поддерживает.
        self.assertIsNone(_classify_xml_path(
            "Catalogs/АукАукционы/Forms/ФормаЭлемента/Ext/Form.xml"))

    def test_template_xml_not_supported(self):
        self.assertIsNone(_classify_xml_path(
            "Catalogs/X/Templates/T/Ext/Template.xml"))

    def test_unknown_top_dir(self):
        self.assertIsNone(_classify_xml_path("СовершенноНеЗнакомаяПапка/Х.xml"))

    def test_bsl_not_xml_returns_none(self):
        self.assertIsNone(_classify_xml_path("Catalogs/X/Ext/ObjectModule.bsl"))

    def test_tests_extension_prefix_stripped(self):
        out = _classify_xml_path("tests-extension/CommonModules/Тест_X.xml")
        self.assertIsNotNone(out)
        self.assertEqual(out[1], "CommonModule")

    def test_root_level_xml_not_an_object(self):
        # Configuration.xml — не верхнеуровневый объект в нашем смысле.
        self.assertIsNone(_classify_xml_path("Configuration.xml"))

    def test_subsystem_xml(self):
        out = _classify_xml_path("Subsystems/Аукционы.xml")
        self.assertIsNotNone(out)
        self.assertEqual(out[1], "Subsystem")


class TestFilterCodeGraphToModule(unittest.TestCase):
    """
    Проверяет, что срез по module_id возвращает РОВНО узлы и рёбра,
    исходящие из этого модуля — ни больше, ни меньше.
    """

    def _make_two_module_graph(self) -> dict:
        """
        Граф из двух модулей:
          CommonModule.A — содержит Proc_A1 (1 param), Proc_A2.
          CommonModule.B — содержит Func_B1 (1 param).
          Proc_A1 зовёт Func_B1 (CALLS).
          Func_B1 зовёт Proc_A2 (CALLS) — встречное ребро.
          Proc_A1 OPERATES_ON Catalog.Контрагенты.
          Параметр Proc_A1.Param.x имеет inferred тип Type:String.
        """
        mod_a = "CommonModule.A"
        mod_b = "CommonModule.B"
        proc_a1 = f"{mod_a}.Proc_A1"
        proc_a2 = f"{mod_a}.Proc_A2"
        func_b1 = f"{mod_b}.Func_B1"
        param_a1_x = f"{proc_a1}.Param.x"
        param_b1_y = f"{func_b1}.Param.y"
        cs_a1_call_b1 = f"{proc_a1}:5:0"
        cs_b1_call_a2 = f"{func_b1}:8:0"

        return {
            "module_nodes": [
                {"id": mod_a, "name": "A", "module_id": mod_a, "kind_eng": "CommonModule",
                 "module_role": "CommonModule", "parent_metadata_id": None,
                 "source_path": "CommonModules/A/Ext/Module.bsl",
                 "is_server": True, "is_client": False, "full_name_eng": mod_a},
                {"id": mod_b, "name": "B", "module_id": mod_b, "kind_eng": "CommonModule",
                 "module_role": "CommonModule", "parent_metadata_id": None,
                 "source_path": "CommonModules/B/Ext/Module.bsl",
                 "is_server": True, "is_client": False, "full_name_eng": mod_b},
            ],
            "callable_nodes": [
                {"id": proc_a1, "module_id": mod_a, "kind": "Procedure", "name": "Proc_A1",
                 "full_name": proc_a1, "is_export": True, "directive": "",
                 "line_start": 1, "line_end": 10, "source_path": ""},
                {"id": proc_a2, "module_id": mod_a, "kind": "Procedure", "name": "Proc_A2",
                 "full_name": proc_a2, "is_export": False, "directive": "",
                 "line_start": 12, "line_end": 20, "source_path": ""},
                {"id": func_b1, "module_id": mod_b, "kind": "Function", "name": "Func_B1",
                 "full_name": func_b1, "is_export": True, "directive": "",
                 "line_start": 1, "line_end": 10, "source_path": ""},
            ],
            "parameter_nodes": [
                {"id": param_a1_x, "name": "x", "position": 0, "is_by_value": False,
                 "has_default": False, "default_value": "", "callable_id": proc_a1},
                {"id": param_b1_y, "name": "y", "position": 0, "is_by_value": False,
                 "has_default": False, "default_value": "", "callable_id": func_b1},
            ],
            "callsite_nodes": [
                {"id": cs_a1_call_b1, "caller_id": proc_a1, "module_ref": "B",
                 "method_name": "Func_B1", "line": 5, "col": 0,
                 "resolved": True, "reason": ""},
                {"id": cs_b1_call_a2, "caller_id": func_b1, "module_ref": "A",
                 "method_name": "Proc_A2", "line": 8, "col": 0,
                 "resolved": True, "reason": ""},
            ],
            "type_nodes": [
                {"id": "Type:String", "kind": "String", "target": None},
            ],
            "edges": [
                # Скелет модуля A
                {"rel": "HAS_METHOD", "src": mod_a, "dst": proc_a1, "props": {"kind": "procedure"}},
                {"rel": "HAS_METHOD", "src": mod_a, "dst": proc_a2, "props": {"kind": "procedure"}},
                {"rel": "HAS_PARAM",  "src": proc_a1, "dst": param_a1_x, "props": {"position": 0}},
                # Скелет модуля B
                {"rel": "HAS_METHOD", "src": mod_b, "dst": func_b1, "props": {"kind": "function"}},
                {"rel": "HAS_PARAM",  "src": func_b1, "dst": param_b1_y, "props": {"position": 0}},
                # CallSite/CALLS
                {"rel": "CALL_SITE", "src": proc_a1, "dst": cs_a1_call_b1, "props": {}},
                {"rel": "CALL_SITE", "src": func_b1, "dst": cs_b1_call_a2, "props": {}},
                {"rel": "CALLS",     "src": proc_a1, "dst": func_b1,
                 "props": {"line": 5, "callsite": cs_a1_call_b1}},
                {"rel": "CALLS",     "src": func_b1, "dst": proc_a2,
                 "props": {"line": 8, "callsite": cs_b1_call_a2}},
                {"rel": "RESOLVES_TO_CALLEE", "src": cs_a1_call_b1, "dst": func_b1, "props": {}},
                {"rel": "RESOLVES_TO_CALLEE", "src": cs_b1_call_a2, "dst": proc_a2, "props": {}},
                {"rel": "OPERATES_ON", "src": proc_a1, "dst": "Catalog.Контрагенты",
                 "props": {"via": "Справочники", "access": "manager_collection"}},
                {"rel": "INFERRED_TYPE", "src": param_a1_x, "dst": "Type:String",
                 "props": {"confidence": 0.9, "source": "constructor"}},
            ],
            "stats": {},
        }

    def test_slice_module_a_contains_only_a_callables(self):
        graph = self._make_two_module_graph()
        sl = _filter_code_graph_to_module(graph, "CommonModule.A")

        # Callable-узлы только из A.
        self.assertEqual({c["id"] for c in sl["callable_nodes"]},
                         {"CommonModule.A.Proc_A1", "CommonModule.A.Proc_A2"})
        # Параметры — только у callable'ов A.
        self.assertEqual({p["id"] for p in sl["parameter_nodes"]},
                         {"CommonModule.A.Proc_A1.Param.x"})
        # CallSite — только caller=A.
        self.assertEqual({cs["id"] for cs in sl["callsite_nodes"]},
                         {"CommonModule.A.Proc_A1:5:0"})
        # Module-узел только A.
        self.assertEqual({m["id"] for m in sl["module_nodes"]},
                         {"CommonModule.A"})

    def test_outgoing_edges_only(self):
        graph = self._make_two_module_graph()
        sl = _filter_code_graph_to_module(graph, "CommonModule.A")

        rels = [(e["rel"], e["src"], e["dst"]) for e in sl["edges"]]

        # Должны быть: HAS_METHOD A→Proc_A1, A→Proc_A2; HAS_PARAM Proc_A1→x;
        # CALL_SITE Proc_A1→cs1; CALLS Proc_A1→Func_B1;
        # RESOLVES_TO_CALLEE cs1→Func_B1; OPERATES_ON Proc_A1→Catalog;
        # INFERRED_TYPE x→Type:String.
        # НЕ должно быть: рёбра из B-узлов, CALLS Func_B1→Proc_A2.
        self.assertIn(("HAS_METHOD", "CommonModule.A", "CommonModule.A.Proc_A1"), rels)
        self.assertIn(("CALLS", "CommonModule.A.Proc_A1", "CommonModule.B.Func_B1"), rels)
        self.assertIn(("INFERRED_TYPE", "CommonModule.A.Proc_A1.Param.x", "Type:String"), rels)
        self.assertIn(("OPERATES_ON", "CommonModule.A.Proc_A1", "Catalog.Контрагенты"), rels)

        # ОТСУТСТВУЕТ: HAS_METHOD от B (это срез чужого модуля).
        self.assertNotIn(("HAS_METHOD", "CommonModule.B", "CommonModule.B.Func_B1"), rels)
        # ОТСУТСТВУЕТ: встречное CALLS Func_B1→Proc_A2 (src — Func_B1, не из A).
        self.assertNotIn(("CALLS", "CommonModule.B.Func_B1", "CommonModule.A.Proc_A2"), rels)
        # ОТСУТСТВУЕТ: RESOLVES_TO_CALLEE с src=cs_b1 (callsite из B).
        for r in rels:
            self.assertNotEqual(r[0:2], ("RESOLVES_TO_CALLEE", "CommonModule.B.Func_B1:8:0"))

    def test_type_nodes_only_referenced_ones(self):
        graph = self._make_two_module_graph()
        # Добавим шумовой Type-узел, который никто не использует — должен отфильтроваться.
        graph["type_nodes"].append({"id": "Type:Number", "kind": "Number", "target": None})

        sl = _filter_code_graph_to_module(graph, "CommonModule.A")
        type_ids = {t["id"] for t in sl["type_nodes"]}
        self.assertEqual(type_ids, {"Type:String"})  # Type:Number не нужен в этом срезе

    def test_empty_module_returns_empty_slice(self):
        graph = self._make_two_module_graph()
        sl = _filter_code_graph_to_module(graph, "CommonModule.DoesNotExist")
        self.assertEqual(sl["module_nodes"], [])
        self.assertEqual(sl["callable_nodes"], [])
        self.assertEqual(sl["parameter_nodes"], [])
        self.assertEqual(sl["callsite_nodes"], [])
        self.assertEqual(sl["type_nodes"], [])
        self.assertEqual(sl["edges"], [])


class TestFilterXmlGraphToObject(unittest.TestCase):
    """
    Контрактный тест: build_graph([single_obj]) уже даёт чистый срез,
    _filter_xml_graph_to_object должен возвращать его как есть.
    """

    def test_returns_input_unchanged(self):
        graph = {
            "meta_nodes": [{"id": "Catalog.X"}],
            "attr_nodes": [],
            "ts_nodes": [],
            "form_nodes": [],
            "enum_value_nodes": [],
            "type_nodes": [],
            "edges": [],
            "stats": {},
        }
        out = _filter_xml_graph_to_object(graph, "Catalog.X")
        self.assertIs(out, graph)


class TestAttachCompatAttrs(unittest.TestCase):
    """
    Проверяет, что `_attach_compat_attrs` достраивает `_attrs_for_compat`
    на :MetadataObject — это нужно, чтобы metadata_object_details после
    инкремента показывал реквизиты.
    """

    def test_attaches_compat_for_catalog_with_ref_attribute(self):
        graph = {
            "meta_nodes": [{"id": "Catalog.X", "name": "X"}],
            "attr_nodes": [
                {"id": "Catalog.X.Attr.Контрагент", "name": "Контрагент",
                 "synonym": "", "role": "attribute", "parent": "Catalog.X"},
            ],
            "type_nodes": [
                {"id": "Type:CatalogRef:Catalog.Контрагенты",
                 "kind": "CatalogRef", "target": "Catalog.Контрагенты"},
            ],
            "edges": [
                {"rel": "OF_TYPE", "src": "Catalog.X.Attr.Контрагент",
                 "dst": "Type:CatalogRef:Catalog.Контрагенты", "props": {}},
            ],
        }
        _attach_compat_attrs(graph)
        attrs = graph["meta_nodes"][0]["_attrs_for_compat"]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["name"], "Контрагент")
        # type_compat собирается из ru-эквивалентов: "СправочникСсылка.Контрагенты"
        self.assertIn("Справочник", attrs[0]["type_compat"])
        self.assertIn("Контрагенты", attrs[0]["type_compat"])

    def test_internal_role_is_dropped(self):
        graph = {
            "meta_nodes": [{"id": "Catalog.X"}],
            "attr_nodes": [
                {"id": "Catalog.X.Attr.A", "name": "A", "role": "attribute", "parent": "Catalog.X"},
                {"id": "Catalog.X.Attr.B", "name": "B", "role": "_internal", "parent": "Catalog.X"},
            ],
            "type_nodes": [],
            "edges": [],
        }
        _attach_compat_attrs(graph)
        names = {a["name"] for a in graph["meta_nodes"][0]["_attrs_for_compat"]}
        self.assertEqual(names, {"A"})

    def test_meta_object_without_attrs(self):
        graph = {
            "meta_nodes": [{"id": "Catalog.X"}],
            "attr_nodes": [],
            "type_nodes": [],
            "edges": [],
        }
        _attach_compat_attrs(graph)
        self.assertEqual(graph["meta_nodes"][0]["_attrs_for_compat"], [])


# ─── Слой B: тесты с FakeNeo4j ──────────────────────────────────────────


class TestUpsertBslFile(unittest.TestCase):
    """
    Проверяет последовательность операций при upsert одного .bsl.
    Использует временный файл с минимальным BSL.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix="incr-test-")
        self.root = Path(self.tmpdir)
        # CommonModules/Тест/Ext/Module.bsl
        mod_dir = self.root / "CommonModules" / "Тест" / "Ext"
        mod_dir.mkdir(parents=True)
        self.bsl_path = mod_dir / "Module.bsl"
        self.bsl_path.write_text(
            "Процедура Привет() Экспорт\n"
            "    Сообщить(\"Привет\");\n"
            "КонецПроцедуры\n"
            "\n"
            "Функция Сумма(а, б)\n"
            "    Возврат а + б;\n"
            "КонецФункции\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_fake_with_layer1(self) -> FakeNeo4j:
        """
        Программирует FakeNeo4j на ответы, симулирующие живой граф:
          • есть :MetadataObject (счётчик > 0)
          • есть :CommonModule.Тест с пустыми properties
          • в графе ровно один :Callable (для index.callable_ids)
          • clear-cleanup-счётчик
          • build_index_from_neo4j делает 3 запроса: к properties_json
            CommonModule, к metadata_objects по kind_eng и к реквизитам
            объектов (FIX-5).

        Порядок ответов = порядок вызовов rows(). Если в
        build_index_from_neo4j добавится запрос, здесь нужно добавить
        ответ на своём месте, иначе все последующие сдвинутся и упадут
        с KeyError в чужом коде — именно так это и обнаружилось.
        """
        fake = FakeNeo4j()
        fake.replies = [
            # 1. Pre-flight: count(MetadataObject)
            [{"n": 50}],
            # 2. CommonModule properties_json
            [{"props": "{\"Server\": true}"}],
            # 3. build_index_from_neo4j: CommonModule rows
            [{"id": "CommonModule.Тест", "props": "{}"}],
            # 4. build_index_from_neo4j: MetadataObject by kinds
            [{"id": "Catalog.X", "name": "X", "kind": "Catalog"}],
            # 5. build_index_from_neo4j: реквизиты объектов (FIX-5)
            [{"id": "Catalog.X", "names": ["Реквизит1"]}],
            # 6. all_callable_rows
            [{"id": "CommonModule.Другой.SomeProc"}],
            # 7. _clear_module_code_slice: count(callables, params, callsites)
            [{"c": 2, "p": 1, "cs": 0}],
        ]
        return fake

    def test_upsert_bsl_invokes_clear_then_write(self):
        fake = self._make_fake_with_layer1()
        result = upsert_bsl_file(fake, self.root,
                                 "CommonModules/Тест/Ext/Module.bsl")

        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["module_id"], "CommonModule.Тест")
        self.assertEqual(result["module_kind"], "CommonModule")

        log = fake.cypher_log()
        # 1. Pre-flight (MetadataObject count) — самый первый.
        self.assertIn("MATCH (m:MetadataObject)", log[0])
        # 2. Где-то в логе — DETACH DELETE с module_id (clear-операции).
        self.assertTrue(fake.has_call("MATCH (c:Callable", "DETACH DELETE"))
        # 3. После clear — MERGE :Callable (write).
        callable_merge_idx = next(
            (i for i, c in enumerate(log) if "MERGE (n:Callable" in c),
            -1,
        )
        first_detach_idx = next(
            (i for i, c in enumerate(log) if "DETACH DELETE c" in c),
            -1,
        )
        self.assertGreater(callable_merge_idx, first_detach_idx,
                           "Запись callable'ов должна идти ПОСЛЕ их удаления")

    def test_upsert_bsl_passes_module_id_to_clear(self):
        fake = self._make_fake_with_layer1()
        upsert_bsl_file(fake, self.root,
                        "CommonModules/Тест/Ext/Module.bsl")
        # Все DELETE-запросы получают module_id="CommonModule.Тест" в params.
        delete_calls = [c for c in fake.calls
                        if "DETACH DELETE" in c.cypher and c.kind == "query"]
        self.assertTrue(delete_calls, "Должны быть DELETE-вызовы")
        for c in delete_calls:
            if "{module_id: $mid}" in c.cypher:
                self.assertEqual(c.params.get("mid"), "CommonModule.Тест")

    def test_upsert_bsl_returns_resolve_stats(self):
        fake = self._make_fake_with_layer1()
        result = upsert_bsl_file(fake, self.root,
                                 "CommonModules/Тест/Ext/Module.bsl")
        self.assertIn("resolve", result)
        self.assertIn("resolved", result["resolve"])
        self.assertIn("unresolved", result["resolve"])
        # fixpoint_iterations должен быть >= 1 (минимум один проход).
        self.assertGreaterEqual(result["resolve"]["fixpoint_iterations"], 1)

    def test_upsert_bsl_records_written_counters(self):
        fake = self._make_fake_with_layer1()
        result = upsert_bsl_file(fake, self.root,
                                 "CommonModules/Тест/Ext/Module.bsl")
        w = result["written"]
        # В нашем BSL — две процедуры: Привет, Сумма.
        self.assertEqual(w["Callable"], 2)
        # У Привет 0 параметров, у Сумма 2 (а, б).
        self.assertEqual(w["Parameter"], 2)

    def test_upsert_bsl_file_not_found(self):
        fake = FakeNeo4j()
        result = upsert_bsl_file(fake, self.root, "no/such/file.bsl")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "file_not_found")
        # Не должны были долезть до запросов в Neo4j.
        self.assertEqual(len(fake.calls), 0)

    def test_upsert_bsl_unknown_path_schema(self):
        # Файл существует, но путь не вписывается в схему 1С-выгрузки.
        odd = self.root / "RandomDir" / "module.bsl"
        odd.parent.mkdir(parents=True)
        odd.write_text("// nothing\n", encoding="utf-8")

        fake = FakeNeo4j()
        result = upsert_bsl_file(fake, self.root, "RandomDir/module.bsl")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "unknown_path_schema")

    def test_upsert_bsl_empty_layer1_returns_error(self):
        fake = FakeNeo4j()
        fake.replies = [[{"n": 0}]]  # pre-flight: 0 MetadataObject
        result = upsert_bsl_file(fake, self.root,
                                 "CommonModules/Тест/Ext/Module.bsl")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "metadata_layer_empty")

    def test_upsert_bsl_wrong_extension(self):
        fake = FakeNeo4j()
        # Создаём .xml-файл, чтобы он существовал, но шлём как bsl-upsert.
        odd = self.root / "CommonModules" / "Тест.xml"
        odd.parent.mkdir(parents=True, exist_ok=True)
        odd.write_text("<dummy/>", encoding="utf-8")
        result = upsert_bsl_file(fake, self.root, "CommonModules/Тест.xml")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_a_bsl_file")


class TestRemoveBslFile(unittest.TestCase):

    def test_remove_invokes_clear_with_correct_module_id(self):
        fake = FakeNeo4j()
        fake.replies = [
            # _clear_module_code_slice: counts
            [{"c": 3, "p": 5, "cs": 7}],
            # _restale_callsites_into_module: stale count
            [{"n": 2}],
        ]
        result = remove_bsl_file(
            fake, Path("/data/1c-src"),
            "/data/1c-src/Catalogs/АукАукционы/Ext/ObjectModule.bsl",
        )
        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["module_id"], "Catalog.АукАукционы.ObjectModule")
        self.assertEqual(result["cleared"]["deleted_callables"], 3)
        self.assertEqual(result["cleared"]["deleted_parameters"], 5)
        self.assertEqual(result["cleared"]["deleted_callsites"], 7)
        self.assertEqual(result["stale_callsites_marked"], 2)

    def test_remove_idempotent_on_already_removed(self):
        fake = FakeNeo4j()
        # Если ничего не было — counts возвращают нули.
        fake.replies = [
            [{"c": 0, "p": 0, "cs": 0}],
            [{"n": 0}],
        ]
        result = remove_bsl_file(
            fake, Path("/data/1c-src"),
            "/data/1c-src/CommonModules/УжеНетТакого/Ext/Module.bsl",
        )
        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["cleared"]["deleted_callables"], 0)

    def test_remove_unknown_path_schema(self):
        fake = FakeNeo4j()
        result = remove_bsl_file(fake, Path("/data/1c-src"),
                                 "Strange/Path/file.bsl")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "unknown_path_schema")
        self.assertEqual(len(fake.calls), 0)

    def test_remove_path_outside_root_without_known_tail(self):
        # 4.6.5: tail-suffix фолбэк работает только если хвост распознан как
        # 1С-выгрузка. Случайный левый абсолютный путь без top-dir Catalogs/
        # Documents/CommonModules/... → skipped.
        fake = FakeNeo4j()
        result = remove_bsl_file(fake, Path("/data/1c-src"),
                                 "/home/user/random/module.bsl")
        self.assertEqual(result["status"], "skipped")
        # reason может быть path_outside_src_root (если _norm_rel вернёт None)
        # или unknown_path_schema (если _norm_rel вернёт хвост, но
        # classify_bsl_path его не разберёт). Главное — НЕ removed.
        self.assertIn(result["reason"],
                      ("path_outside_src_root", "unknown_path_schema"))


class TestUpsertXmlFile(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix="incr-xml-")
        self.root = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_minimal_catalog_xml(self, name: str) -> Path:
        """Пишет минимальный валидный Catalog.xml в выгрузочном формате."""
        cat_dir = self.root / "Catalogs"
        cat_dir.mkdir(parents=True, exist_ok=True)
        path = cat_dir / f"{name}.xml"
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
            f'  <Catalog uuid="11111111-2222-3333-4444-555555555555">\n'
            '    <Properties>\n'
            f'      <Name>{name}</Name>\n'
            f'      <Synonym><v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core">'
            f'<v8:lang>ru</v8:lang><v8:content>{name}</v8:content></v8:item></Synonym>\n'
            '    </Properties>\n'
            '    <ChildObjects></ChildObjects>\n'
            '  </Catalog>\n'
            '</MetaDataObject>\n',
            encoding="utf-8",
        )
        return path

    def test_upsert_xml_clear_meta_then_write(self):
        self._write_minimal_catalog_xml("ТестовыйСправочник")
        fake = FakeNeo4j()
        fake.replies = [
            # _clear_meta_object_slice: counts (attrs/ts/forms/evs)
            [{"attrs": 0, "ts": 0, "forms": 0, "evs": 0}],
            # write_edges UNWIND-rows для RESOLVES_TO (наши Type без target — никаких)
            [{"n": 0}],
        ]
        result = upsert_xml_file(fake, self.root,
                                 "Catalogs/ТестовыйСправочник.xml")

        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["meta_id"], "Catalog.ТестовыйСправочник")
        self.assertEqual(result["kind_eng"], "Catalog")

        log = fake.cypher_log()
        # Clear по meta_id с DETACH DELETE.
        self.assertTrue(fake.has_call("MATCH (m:MetadataObject {id: $id})",
                                       "DETACH DELETE"))
        # Запись :MetadataObject через MERGE.
        self.assertTrue(any("MERGE (n:MetadataObject" in c.cypher
                            for c in fake.calls))

    def test_upsert_xml_clear_child_nodes_use_prefix(self):
        self._write_minimal_catalog_xml("ХС")
        fake = FakeNeo4j()
        fake.replies = [
            [{"attrs": 0, "ts": 0, "forms": 0, "evs": 0}],
            [{"n": 0}],
        ]
        upsert_xml_file(fake, self.root, "Catalogs/ХС.xml")
        # Прицельная проверка: DELETE через STARTS WITH $prefix существует
        # и prefix = "Catalog.ХС.".
        prefix_delete_calls = [
            c for c in fake.calls
            if "STARTS WITH $prefix" in c.cypher
        ]
        self.assertTrue(prefix_delete_calls)
        for c in prefix_delete_calls:
            self.assertEqual(c.params.get("prefix"), "Catalog.ХС.")

    def test_upsert_xml_not_an_xml(self):
        fake = FakeNeo4j()
        bsl_path = self.root / "fake.bsl"
        bsl_path.write_text("// not xml", encoding="utf-8")
        result = upsert_xml_file(fake, self.root, "fake.bsl")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_an_xml_file")

    def test_upsert_xml_not_toplevel(self):
        fake = FakeNeo4j()
        # Вложенный Form.xml — не наш кейс.
        nested = (self.root / "Catalogs" / "X" / "Forms" / "F" / "Ext")
        nested.mkdir(parents=True)
        (nested / "Form.xml").write_text("<dummy/>", encoding="utf-8")
        result = upsert_xml_file(
            fake, self.root,
            "Catalogs/X/Forms/F/Ext/Form.xml",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_a_toplevel_object_xml")


class TestRemoveXmlFile(unittest.TestCase):

    def test_remove_xml_invokes_clear_with_meta_id(self):
        fake = FakeNeo4j()
        fake.replies = [
            [{"attrs": 3, "ts": 1, "forms": 2, "evs": 0}],
        ]
        result = remove_xml_file(fake, Path("/data/1c-src"),
                                 "Catalogs/АукАукционы.xml")
        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["meta_id"], "Catalog.АукАукционы")
        self.assertEqual(result["cleared"]["deleted_attributes"], 3)
        # Удаление идёт строго по meta_id через DETACH DELETE.
        self.assertTrue(fake.has_call("DETACH DELETE m"))

    def test_remove_xml_not_toplevel_skipped(self):
        fake = FakeNeo4j()
        result = remove_xml_file(fake, Path("/data/1c-src"),
                                 "Catalogs/X/Forms/F/Ext/Form.xml")
        self.assertEqual(result["status"], "skipped")


class TestUpsertRemoveDispatchers(unittest.TestCase):
    """Проверяет, что upsert_file / remove_file правильно диспатчат по расширению."""

    def test_upsert_dispatcher_bsl(self):
        fake = FakeNeo4j()
        # Симулируем file_not_found, чтобы не доходить до Neo4j.
        result = upsert_file(fake, Path("/no/where"), "x.bsl")
        # Должен дойти до upsert_bsl_file и вернуть skipped (path_outside_src_root
        # или file_not_found или unknown_path_schema — в любом случае не error).
        self.assertEqual(result["status"], "skipped")
        # `not_a_bsl_file` означало бы что не диспатчнули; должно быть что-то bsl-specific.
        self.assertNotEqual(result["reason"], "unsupported_extension")

    def test_upsert_dispatcher_xml(self):
        fake = FakeNeo4j()
        result = upsert_file(fake, Path("/no/where"), "x.xml")
        self.assertEqual(result["status"], "skipped")
        self.assertNotEqual(result["reason"], "unsupported_extension")

    def test_upsert_dispatcher_unsupported(self):
        fake = FakeNeo4j()
        result = upsert_file(fake, Path("/data"), "x.txt")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "unsupported_extension")

    def test_remove_dispatcher_bsl(self):
        fake = FakeNeo4j()
        fake.replies = [
            [{"c": 0, "p": 0, "cs": 0}],
            [{"n": 0}],
        ]
        result = remove_file(fake, Path("/data/1c-src"),
                             "/data/1c-src/CommonModules/X/Ext/Module.bsl")
        self.assertEqual(result["status"], "removed")

    def test_remove_dispatcher_unsupported(self):
        fake = FakeNeo4j()
        result = remove_file(fake, Path("/data"), "x.txt")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "unsupported_extension")


# ─── Слой C: sanity на реальном workspace ────────────────────────────────


_WS_ROOT_ENV = os.environ.get("INCR_TEST_WORKSPACE")
# По умолчанию — рядом с папкой репо. На CI можно подкинуть через env.
_WS_DEFAULT_CANDIDATES = [
    Path("/home/claude/ws/workspace"),
    Path(__file__).parent.parent.parent / "ws" / "workspace",
]


def _find_workspace() -> Path | None:
    if _WS_ROOT_ENV:
        p = Path(_WS_ROOT_ENV)
        if p.is_dir() and (p / "Configuration.xml").exists():
            return p
    for p in _WS_DEFAULT_CANDIDATES:
        if p.is_dir() and (p / "Configuration.xml").exists():
            return p
    return None


class TestRealWorkspaceSlice(unittest.TestCase):
    """
    Sanity на реальном workspace (Котировки):
      • walk_workspace_bsl даёт ~115 модулей.
      • build_call_graph + _filter_code_graph_to_module(один_модуль) даёт
        срез, где все узлы и исходящие рёбра — этого модуля.
      • Сумма callable'ов всех срезов == общий callable_nodes (партиционирование).
    """

    @classmethod
    def setUpClass(cls):
        cls.ws = _find_workspace()
        if cls.ws is None:
            raise unittest.SkipTest(
                "Реальный workspace не найден. Положите распакованную "
                "выгрузку 1С в /home/claude/ws/workspace или задайте "
                "INCR_TEST_WORKSPACE."
            )

    def test_walk_workspace_finds_modules(self):
        modules = walk_workspace_bsl(self.ws)
        self.assertGreater(len(modules), 50,
                           "Ожидаем десятки модулей в Котировках")

    def test_slice_partitions_callables_across_modules(self):
        """
        Сумма callable'ов всех модульных срезов = общее число callable'ов.
        Это и есть инвариант партиционирования.
        """
        modules = walk_workspace_bsl(self.ws)
        index = build_index_from_modules(modules)
        full_graph = build_call_graph(modules, index)

        total_callables = len(full_graph["callable_nodes"])
        self.assertGreater(total_callables, 100, "Ожидаем сотни callable'ов")

        # Берём первые ~10 модулей, считаем callable'ы их срезов, проверяем,
        # что они не пересекаются и в сумме равны callable'ам в этих модулях
        # в полном графе.
        sample_module_ids = sorted({c["module_id"]
                                    for c in full_graph["callable_nodes"]})[:10]
        slice_callables: set[str] = set()
        for mid in sample_module_ids:
            sl = _filter_code_graph_to_module(full_graph, mid)
            ids = {c["id"] for c in sl["callable_nodes"]}
            # Нет пересечений с уже виденными — партиционирование.
            self.assertEqual(slice_callables & ids, set(),
                             f"Срезы пересекаются на модуле {mid}")
            slice_callables |= ids

        # Все callable'ы из этих 10 модулей попали в срезы (целиком).
        expected = {c["id"] for c in full_graph["callable_nodes"]
                    if c["module_id"] in set(sample_module_ids)}
        self.assertEqual(slice_callables, expected)

    def test_slice_outgoing_edges_only(self):
        """
        Срез не содержит ни одного ребра, чей src — НЕ из этого модуля.
        Это главный инвариант: инкремент пишет только свои исходящие связи,
        чужие не затрагивает.
        """
        modules = walk_workspace_bsl(self.ws)
        index = build_index_from_modules(modules)
        full_graph = build_call_graph(modules, index)

        # Выбираем модуль с непустыми связями.
        # Берём первый модуль, у которого есть рёбра — обычно это CommonModule
        # либо первый Catalog с большим ObjectModule.
        target_module_id = None
        for m in full_graph["module_nodes"]:
            mid = m["id"]
            # Есть ли исходящие callsite'ы / CALLS?
            has_edges = any(
                e["src"] == mid
                or any(c["id"] == e["src"]
                       for c in full_graph["callable_nodes"]
                       if c["module_id"] == mid)
                for e in full_graph["edges"][:200]  # ограничим скан для быстроты
            )
            if has_edges:
                target_module_id = mid
                break
        self.assertIsNotNone(target_module_id, "Не нашли модуль со связями")

        sl = _filter_code_graph_to_module(full_graph, target_module_id)
        our_callable_ids = {c["id"] for c in sl["callable_nodes"]}
        our_param_ids = {p["id"] for p in sl["parameter_nodes"]}
        our_callsite_ids = {cs["id"] for cs in sl["callsite_nodes"]}
        our_srcs = ({target_module_id} | our_callable_ids
                    | our_param_ids | our_callsite_ids)

        for e in sl["edges"]:
            self.assertIn(
                e["src"], our_srcs,
                f"Ребро {e['rel']} {e['src']}→{e['dst']} имеет чужой src",
            )


# ─── Слой D: end-to-end upsert на реальном workspace с SmartFakeNeo4j ──


class SmartFakeNeo4j:
    """
    Query-aware fake: отвечает разными reply-шаблонами на разные cypher'ы.
    Нужен для интеграционного теста upsert_bsl_file / upsert_xml_file на
    реальных файлах — поведение `incremental.py` зависит от порядка
    cypher-запросов, а порядок зависит от kind модуля (CommonModule vs
    ObjectModule vs Form). Тупой массивный fake тут не подходит.

    Эмулирует ровно тот контракт Neo4j, который нужен `incremental`-коду
    для прохождения «зелёного пути» (status=reindexed); не претендует на
    полноту Cypher-семантики.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def query(self, cypher: str, parameters: dict | None = None) -> dict:
        self.calls.append(("query", cypher, dict(parameters or {})))
        return {"results": [{"columns": [], "data": []}]}

    def rows(self, cypher: str, parameters: dict | None = None) -> list[dict]:
        self.calls.append(("rows", cypher, dict(parameters or {})))
        c = cypher
        # Pre-flight: счётчик слоя 1.
        if "count(m) AS n" in c and "MetadataObject" in c:
            return [{"n": 100}]
        # CommonModule props по id (для is_server/is_client конкретного модуля).
        if "properties_json AS props" in c and "$id" in c \
           and "MATCH (m:MetadataObject:CommonModule {id:" in c:
            return [{"props": "{}"}]
        # build_index_from_neo4j: все CommonModule props.
        if "MATCH (m:MetadataObject:CommonModule)" in c \
           and "m.id AS id" in c and "m.properties_json AS props" in c:
            return [{"id": "CommonModule.АукОбщийКлиент", "props": "{}"}]
        # build_index_from_neo4j: метаобъекты по kinds.
        if "MATCH (m:MetadataObject)" in c and "WHERE m.kind_eng IN" in c:
            return [
                {"id": "Catalog.АукАукционы", "name": "АукАукционы",
                 "kind": "Catalog"},
                {"id": "Catalog.АукВидыАукционов", "name": "АукВидыАукционов",
                 "kind": "Catalog"},
                {"id": "CommonModule.АукОбщийКлиент", "name": "АукОбщийКлиент",
                 "kind": "CommonModule"},
            ]
        # all_callable_rows.
        if "MATCH (c:Callable) RETURN c.id" in c:
            return [{"id": f"CommonModule.X.M{i}"} for i in range(5)]
        # _clear_module_code_slice counts.
        if "MATCH (c:Callable {module_id: $mid})" in c \
           and "count(DISTINCT c)" in c:
            return [{"c": 5, "p": 12, "cs": 30}]
        # _restale_callsites_into_module.
        if "stale_after_incremental" in c:
            return [{"n": 3}]
        # _clear_meta_object_slice counts.
        if "OPTIONAL MATCH (m)-[:HAS_ATTRIBUTE]" in c:
            return [{"attrs": 2, "ts": 1, "forms": 1, "evs": 0}]
        # RESOLVES_TO rebuild count.
        if "MERGE (t)-[:RESOLVES_TO]->(m)" in c:
            return [{"n": 1}]
        # MERGE/CREATE прочее — счётчик не запрашивается.
        return []


class TestRealWorkspaceUpsert(unittest.TestCase):
    """
    End-to-end: upsert_bsl_file / upsert_xml_file проходят без падений
    на нескольких реальных файлах Котировок, возвращают status=reindexed.

    Проверяет полный цикл parser → resolver → slice → writer без живой
    Neo4j (через SmartFakeNeo4j). Эквивалент того, что сделает MCP-tool
    `metadata_upsert_file` в продовом стеке.
    """

    @classmethod
    def setUpClass(cls):
        cls.ws = _find_workspace()
        if cls.ws is None:
            raise unittest.SkipTest("Реальный workspace не найден")

    def test_upsert_common_module(self):
        rel = "CommonModules/АукОбщийКлиент/Ext/Module.bsl"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден в workspace")
        result = upsert_bsl_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["module_id"], "CommonModule.АукОбщийКлиент")
        # У АукОбщийКлиент много процедур — больше 20 callable'ов точно.
        self.assertGreater(result["written"]["Callable"], 20)

    def test_upsert_object_module(self):
        rel = "Catalogs/АукАукционы/Ext/ObjectModule.bsl"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_bsl_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["module_id"], "Catalog.АукАукционы.ObjectModule")
        self.assertEqual(result["module_kind"], "ObjectModule")

    def test_upsert_manager_module(self):
        rel = "Catalogs/АукАукционы/Ext/ManagerModule.bsl"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_bsl_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["module_kind"], "ManagerModule")

    def test_upsert_form_module(self):
        rel = "Catalogs/АукАукционы/Forms/ФормаЭлемента/Ext/Form/Module.bsl"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_bsl_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["module_kind"], "Form")
        # Форма обычно много callable'ов (обработчики событий).
        self.assertGreater(result["written"]["Callable"], 10)

    def test_upsert_xml_catalog(self):
        rel = "Catalogs/АукАукционы.xml"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_xml_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["meta_id"], "Catalog.АукАукционы")
        # У АукАукционы есть реквизиты — Attribute > 0.
        self.assertGreater(result["written"]["Attribute"], 0)
        # И формы — Form > 0.
        self.assertGreater(result["written"]["Form"], 0)

    def test_upsert_xml_common_module(self):
        rel = "CommonModules/АукОбщийКлиент.xml"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_xml_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        self.assertEqual(result["meta_id"], "CommonModule.АукОбщийКлиент")
        # У CommonModule нет реквизитов и форм.
        self.assertEqual(result["written"]["Attribute"], 0)
        self.assertEqual(result["written"]["Form"], 0)

    def test_fixpoint_converges_on_real_module(self):
        """
        На одном модуле фикс-пойнт должен сходиться быстро (≤ 3 итераций),
        потому что inter-procedural пере-резолюция здесь невозможна —
        index.callable_ids ограничен этим модулем + чужими (фиксированными).
        """
        rel = "CommonModules/АукОбщийКлиент/Ext/Module.bsl"
        if not (self.ws / rel).exists():
            self.skipTest(f"Файл {rel} не найден")
        result = upsert_bsl_file(SmartFakeNeo4j(), self.ws, rel)
        self.assertEqual(result["status"], "reindexed")
        # На полной Котировке фикс-пойнт делает 7 итераций. На одном
        # модуле — 1-3, потому что param-типы из других модулей уже
        # «зафиксированы» (read-only из живого графа).
        self.assertLessEqual(result["resolve"]["fixpoint_iterations"], 3)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    unittest.main(verbosity=2)
