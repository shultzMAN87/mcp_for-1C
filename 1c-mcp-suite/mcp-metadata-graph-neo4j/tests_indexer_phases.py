"""
FIX-27. Когда индексер имеет право сказать «данные актуальны».
==============================================================

Разбор дефекта
──────────────
18 августа граф оказался без слоя владения кодом: `:Module` — ноль,
`HAS_METHOD` — ни одного ребра, при 231 129 процедурах. Слой XML был
переиндексирован 17-го, слой BSL — 12-го.

Как это получилось, видно по двум строкам старого кода:

  1. `run_xml_phase` сносит `:MetadataObject`-узлы, среди которых и модули.
     Сразу после неё стоял `fingerprint_write(... "metadata_xml" ...)` —
     слой объявлялся актуальным ДО того, как фаза 2 пересоздаст снесённое.
  2. Если фаза 2 после этого не отрабатывала (упала, была снята по памяти,
     пропущена `METADATA_SKIP_BSL`), отпечаток BSL оставался от прошлого
     успешного прогона. Файлы .bsl с тех пор не менялись — значит, при
     следующем старте оба отпечатка совпадали, и индексер выходил со
     словами «данные актуальны».

То есть отпечаток отвечает на вопрос «менялась ли выгрузка», а трактовался
как ответ на вопрос «построен ли граф». Разные вопросы.

Что проверяется здесь
─────────────────────
Не арифметика, а последовательность решений `main()`: какие фазы прошли,
какие отпечатки записаны и в каком порядке. Живой Neo4j для этого не нужен
и вреден — нужен именно перебор состояний, включая те, которые на стенде
воспроизводятся раз в неделю.

Запуск:  python3 tests_indexer_phases.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

os.environ.setdefault("NEO4J_PASSWORD", "тест-без-соединения")

import indexer  # noqa: E402
from graph_integrity import ownership_report  # noqa: E402


class FakeNeo:
    """Ничего не умеет: до реальных запросов в этих тестах не доходит."""

    def __init__(self, *a, **kw):
        pass

    def wait(self, timeout=120.0):
        return None

    def rows(self, cypher, parameters=None):
        return []

    def query(self, cypher, parameters=None):
        return {"results": [{"columns": [], "data": []}], "errors": []}


class PhaseCase(unittest.TestCase):
    """
    Общая обвязка: подменяем всё, что ходит наружу, и записываем события.

    `events` — лента того, что произошло, в порядке происшествия. Именно
    порядок здесь и есть предмет проверки: «отпечаток XML записан ПОСЛЕ
    фазы 2» — утверждение о последовательности, а не о факте.
    """

    #: состояние графа, которое видит индексер при старте
    ownership_before = None
    #: чем кончится фаза 2
    bsl_rc = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name)
        (src / "Configuration.xml").write_text("<x/>", encoding="utf-8")
        self.src = src

        self.events: list[str] = []
        self.saved = {}
        for name in ("Neo4j", "run_xml_phase", "run_bsl_phase",
                     "fingerprint_write", "fingerprint_workspace_multi",
                     "fingerprint_get_meta", "fingerprint_matches",
                     "check_ownership", "relations_snapshot_write"):
            self.saved[name] = getattr(indexer, name)

        indexer.Neo4j = FakeNeo
        indexer.run_xml_phase = self._xml_phase
        indexer.run_bsl_phase = self._bsl_phase
        indexer.fingerprint_write = self._fp_write
        indexer.fingerprint_workspace_multi = lambda *a, **kw: (
            {".xml": "xml-new", ".bsl": "bsl-new"},
            {"mode": "stat", "files": 2, "by_suffix": {".xml": 1, ".bsl": 1},
             "bytes": 10, "elapsed_sec": 0.01, "newest_mtime": 0},
        )
        indexer.fingerprint_get_meta = lambda neo, kind: {"value": kind,
                                                          "mode": "stat"}
        indexer.fingerprint_matches = self._fp_matches
        indexer.check_ownership = lambda neo: (
            self.ownership_before or ownership_report(100, 10, 100, objects=5))
        indexer.relations_snapshot_write = self._snapshot

        for var in ("METADATA_FORCE_REINDEX", "METADATA_FORCE_XML",
                    "METADATA_FORCE_BSL", "METADATA_SKIP_BSL"):
            os.environ.pop(var, None)
        os.environ["METADATA_SRC_DIR"] = str(src)

        # По умолчанию оба отпечатка совпали: это и есть путь, на котором
        # разрушенный граф жил незамеченным.
        self.xml_same = True
        self.bsl_same = True

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(indexer, name, value)
        os.environ.pop("METADATA_SRC_DIR", None)
        for var in ("METADATA_FORCE_XML", "METADATA_FORCE_BSL",
                    "METADATA_SKIP_BSL", "METADATA_FORCE_REINDEX"):
            os.environ.pop(var, None)
        self.tmp.cleanup()

    # ─ подменённые шаги ─

    def _xml_phase(self, neo, src_dir, cfg_name):
        self.events.append("phase1")
        return 0

    def _bsl_phase(self, neo, src_dir):
        self.events.append("phase2")
        return self.bsl_rc

    def _fp_write(self, neo, value, kind="metadata_xml", mode="stat"):
        self.events.append(f"fp:{kind}")

    def _fp_matches(self, old, new_value, new_mode):
        same = self.xml_same if (old or {}).get("value") == "metadata_xml" \
            else self.bsl_same
        return same, "fingerprint совпал" if same else "fingerprint изменился"

    def _snapshot(self, neo):
        self.events.append("relations_snapshot")
        return {"CALLS": 1}

    def run_main(self) -> int:
        return indexer.main()


class TestHealthyNoWork(PhaseCase):

    def test_matching_fingerprints_and_healthy_graph_do_nothing(self):
        rc = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.events, [],
                         "здоровый граф и неизменная выгрузка — работы нет")


class TestBrokenGraphHeals(PhaseCase):
    """Главный сценарий FIX-27."""

    def setUp(self):
        super().setUp()
        self.ownership_before = ownership_report(231_129, 0, 0, objects=10_747)

    def test_phase2_runs_despite_matching_fingerprints(self):
        rc = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn(
            "phase2", self.events,
            "оба отпечатка совпали, но слоя владения в графе нет — "
            "выходить с «данные актуальны» нельзя (FIX-27)",
        )

    def test_xml_is_not_reparsed(self):
        """
        Чинить надо ровно сломанное. Слой 1 в этом состоянии цел (объекты
        на месте), и перечитывать выгрузку значило бы платить два часа за
        работу, которой не требуется.
        """
        self.run_main()
        self.assertNotIn("phase1", self.events)

    def test_empty_graph_needs_both_phases(self):
        """
        Другое дело — когда снесён и слой 1: фазе 2 не на что опереться,
        она сама откажется работать. Тогда нужен полный прогон.
        """
        self.ownership_before = ownership_report(0, 0, 0, objects=0)
        self.run_main()
        self.assertEqual(self.events[:2], ["phase1", "phase2"])


class TestFingerprintOrder(PhaseCase):
    """
    Отпечаток слоя XML записывается ПОСЛЕ фазы 2 — в этом вся правка.
    """

    def setUp(self):
        super().setUp()
        self.xml_same = False  # выгрузка XML изменилась

    def test_xml_fingerprint_written_after_phase2(self):
        self.run_main()
        self.assertIn("fp:metadata_xml", self.events)
        self.assertLess(
            self.events.index("phase2"),
            self.events.index("fp:metadata_xml"),
            "отпечаток XML сохранён до фазы 2 — значит, граф без слоя "
            "владения объявлен актуальным (FIX-27)",
        )

    def test_phase1_pulls_phase2_behind_it(self):
        self.run_main()
        self.assertEqual(self.events[:2], ["phase1", "phase2"])

    def test_failed_phase2_leaves_no_fingerprints(self):
        """
        Сбой посреди прогона обязан стоить повторной работы, а не тихой
        потери данных: ни один отпечаток не сохраняется.
        """
        self.bsl_rc = 7
        rc = self.run_main()
        self.assertEqual(rc, 7)
        self.assertNotIn("fp:metadata_xml", self.events)
        self.assertNotIn("fp:bsl_source", self.events)

    def test_snapshot_refreshed_on_success(self):
        """PERF-12: снимок счётчиков рёбер обновляется в конце прогона."""
        self.run_main()
        self.assertIn("relations_snapshot", self.events)


class TestSkipBslLeavesNoLandmine(PhaseCase):
    """
    `METADATA_SKIP_BSL=true` — R&D-режим, и он имеет право оставить граф без
    слоя кода. Чего он права не имеет — так это записать отпечаток: тогда
    обычный следующий запуск сказал бы «данные актуальны» и починить это
    штатным способом стало бы нельзя.
    """

    def setUp(self):
        super().setUp()
        self.xml_same = False
        os.environ["METADATA_SKIP_BSL"] = "true"

    def test_no_xml_fingerprint_when_phase2_skipped(self):
        rc = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.events, ["phase1"])
        self.assertNotIn("fp:metadata_xml", self.events)


class TestForceFlags(PhaseCase):

    def test_force_bsl_alone_does_not_touch_xml(self):
        os.environ["METADATA_FORCE_BSL"] = "true"
        self.run_main()
        self.assertNotIn("phase1", self.events)
        self.assertIn("phase2", self.events)
        self.assertIn("fp:bsl_source", self.events)
        self.assertNotIn("fp:metadata_xml", self.events)

    def test_force_xml_still_pulls_phase2(self):
        os.environ["METADATA_FORCE_XML"] = "true"
        self.run_main()
        self.assertEqual(self.events[:2], ["phase1", "phase2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
