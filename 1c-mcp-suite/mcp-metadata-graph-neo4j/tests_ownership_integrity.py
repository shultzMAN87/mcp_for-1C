"""
FIX-27 / FIX-28 / FIX-29. Три способа потерять данные молча.
=============================================================

Общее у всех трёх — не потеря сама по себе, а её незаметность:

  FIX-27  точечное обновление XML сносит узел объекта вместе с рёбрами
          владения; код остаётся в графе, но без хозяина;
  FIX-28  631 модуль не доходит до узлов `:Module` — отсев законный, но
          необъявленный, и потому неотличим от дыры;
  FIX-29  писатель отчитывается о 231 102 рёбрах, в базе 231 101: `MERGE`
          схлопывает одинаковые пары, и норму надо назвать вслух.

Запуск:  python3 tests_ownership_integrity.py
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from bsl_parser import ParsedModule, ParsedProcedure  # noqa: E402
from bsl_resolver import build_call_graph, build_index_from_modules  # noqa: E402
from graph_writer import _report_merge_dedup, write_edges  # noqa: E402
from incremental import relink_code_ownership  # noqa: E402
from tests_incremental import FakeNeo4j  # noqa: E402


def _module(module_id: str, kind: str, procs: list[str],
            parent: str | None = None) -> ParsedModule:
    return ParsedModule(
        module_id=module_id,
        module_kind=kind,
        parent_metadata_id=parent,
        source_path=f"{module_id}.bsl",
        is_server=True,
        is_client=False,
        procedures=[
            ParsedProcedure(name=p, kind="Procedure", is_export=True,
                            directive="", parameters=[],
                            line_start=1, line_end=2)
            for p in procs
        ],
    )


# ─── FIX-27: вернуть коду владельца ──────────────────────────────────────


class TestRelinkOwnership(unittest.TestCase):
    """
    После сноса среза объекта `HAS_METHOD` восстанавливается по `module_id`,
    который у `:Callable` никуда не девался. Заново разбирать BSL не нужно.
    """

    def setUp(self):
        self.neo = FakeNeo4j()
        self.neo.replies = [[{"n": 12}], [{"n": 3}]]
        self.result = relink_code_ownership(self.neo, "Catalog.Контрагенты")

    def test_counts_both_kinds_of_module(self):
        self.assertEqual(self.result["has_method"], 15)

    def test_two_queries_because_labels_differ(self):
        """
        Модуль объекта — `:MetadataObject`, модуль формы — `:Form`. Один
        запрос без метки нашёл бы оба и не использовал бы индекс: ровно
        грабля PERF-4, случавшаяся в проекте трижды.
        """
        self.assertEqual(len(self.neo.calls), 2)
        self.assertTrue(self.neo.has_call("MATCH (m:MetadataObject {id:"))
        self.assertTrue(self.neo.has_call("MATCH (m:Form {id:"))

    def test_form_node_gets_module_label_back(self):
        """
        Метку `:Module` модулю формы дописывает фаза 2; после пересоздания
        узла writer'ом она теряется. Без неё узел перестаёт считаться
        модулем в `metadata_stats` — это и был `modules: 0`.
        """
        self.assertTrue(self.neo.has_call("MATCH (m:Form {id:", "SET m:Module"))

    def test_scoped_by_object_prefix(self):
        params = self.neo.calls[0].params
        self.assertEqual(params["id"], "Catalog.Контрагенты")
        self.assertEqual(params["prefix"], "Catalog.Контрагенты.")

    def test_merge_not_create(self):
        """
        Повторный вызов не должен плодить рёбра: часть связей могла уцелеть.
        """
        for call in self.neo.calls:
            self.assertIn("MERGE (m)-[e:HAS_METHOD]->(c)", call.cypher)
            self.assertNotIn("CREATE (m)-[", call.cypher)


class TestUpsertXmlRelinks(unittest.TestCase):
    """
    Перепривязка обязана происходить внутри `upsert_xml_file`, а не только в
    `apply_changes`: инструмент `metadata_upsert_file` вызывают и поодиночке,
    и тогда пары «XML, следом BSL» не будет.
    """

    def test_upsert_xml_file_calls_relink(self):
        src = (HERE / "incremental.py").read_text(encoding="utf-8")
        body = src[src.index("def upsert_xml_file("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("relink_code_ownership(neo, meta_id)", body)
        self.assertIn("ownership_relinked", body,
                      "результат перепривязки должен попадать в ответ — "
                      "иначе о ней нельзя узнать со стороны")


# ─── FIX-28: объявленный отсев модулей ───────────────────────────────────


class TestModuleShortfallIsExplained(unittest.TestCase):

    def setUp(self):
        modules = [
            _module("CommonModule.Общий1", "CommonModule", ["А"]),
            _module("CommonModule.Общий2", "CommonModule", ["Б"]),
            _module("Catalog.Тест.ObjectModule", "ObjectModule", ["В"],
                    parent="Catalog.Тест"),
            _module("Catalog.Тест.Form.Форма", "Form", ["Г"],
                    parent="Catalog.Тест"),
            # Дубль: тот же module_id из tests-extension.
            _module("Catalog.Тест.ObjectModule", "ObjectModule", ["Д"],
                    parent="Catalog.Тест"),
        ]
        index = build_index_from_modules(modules)
        self.stats = build_call_graph(modules, index)["stats"]

    def test_arithmetic_adds_up(self):
        """
        Вход = выход + объявленный отсев. Невязка в этом равенстве и есть
        «потери, о которых никто не знает», — на боевой она была 631.
        """
        total = (self.stats["module_nodes"]
                 + self.stats["module_nodes_common"]
                 + self.stats["module_nodes_duplicate"])
        self.assertEqual(total, 5)

    def test_common_modules_counted_separately(self):
        self.assertEqual(self.stats["module_nodes_common"], 2)

    def test_duplicates_counted_and_named(self):
        self.assertEqual(self.stats["module_nodes_duplicate"], 1)
        self.assertIn("Catalog.Тест.ObjectModule",
                      self.stats["module_duplicate_examples"])

    def test_common_modules_still_own_their_methods(self):
        """
        Отсев касается только узла `:Module`: узел общего модуля уже есть в
        слое 1. Ребро `HAS_METHOD` при этом обязано быть — иначе «отсев»
        означал бы настоящую потерю.
        """
        edges = [e for e in build_call_graph(
            [_module("CommonModule.Общий1", "CommonModule", ["А"])],
            build_index_from_modules(
                [_module("CommonModule.Общий1", "CommonModule", ["А"])]),
        )["edges"] if e["rel"] == "HAS_METHOD"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["src"], "CommonModule.Общий1")
        self.assertEqual(edges[0]["src_label"], "MetadataObject")


class TestIndexerDeclaresTheDrop(unittest.TestCase):
    """Счётчики из resolver'а должны доезжать до сводки потерь."""

    def test_indexer_wires_both_reasons(self):
        src = (HERE / "indexer.py").read_text(encoding="utf-8")
        body = src[src.index("mod_tally = TALLIES.stage"):]
        body = body[:body.index("proc_tally")]
        self.assertIn("module_nodes_common", body)
        self.assertIn("module_nodes_duplicate", body)
        self.assertIn("alarm=True", body,
                      "дубль module_id — единственный тревожный из двух "
                      "выходов: код одного файла остаётся под именем двух")


# ─── FIX-29: MERGE схлопывает пары ───────────────────────────────────────


class TestMergeDedupIsAnnounced(unittest.TestCase):

    def setUp(self):
        self.log = logging.getLogger("graph_writer")
        self.records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record.getMessage())

        self.handler = Catcher()
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def test_duplicate_pair_is_named(self):
        group = [
            {"src": "CommonModule.X", "dst": "CommonModule.X.Проц"},
            {"src": "CommonModule.X", "dst": "CommonModule.X.Проц"},
            {"src": "CommonModule.X", "dst": "CommonModule.X.Другая"},
        ]
        _report_merge_dedup("HAS_METHOD:MetadataObject", group, written=3)
        text = " ".join(self.records)
        self.assertIn("дублей по паре 1", text)
        self.assertIn("в базе будет 2", text)
        self.assertIn("CommonModule.X.Проц", text,
                      "дубль надо НАЗВАТЬ: иначе счётчик сходится, а "
                      "процедура остаётся потерянной")

    def test_silence_when_there_are_no_duplicates(self):
        group = [{"src": "A", "dst": "B"}, {"src": "A", "dst": "C"}]
        _report_merge_dedup("HAS_METHOD", group, written=2)
        self.assertEqual(self.records, [],
                         "в норме этот отчёт молчит — иначе он превратится "
                         "в строку, которую перестают читать")

    def test_write_edges_reports_duplicates(self):
        """Сквозная проверка: дубли объявляются на настоящем пути записи."""
        neo = FakeNeo4j()
        neo.replies = [[{"written": 2}]]
        write_edges(neo, [
            {"rel": "HAS_METHOD", "src": "CommonModule.X",
             "dst": "CommonModule.X.Проц", "src_label": "MetadataObject"},
            {"rel": "HAS_METHOD", "src": "CommonModule.X",
             "dst": "CommonModule.X.Проц", "src_label": "MetadataObject"},
        ], log_progress=False)
        self.assertIn("дублей по паре 1", " ".join(self.records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
