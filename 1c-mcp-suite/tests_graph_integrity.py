"""
FIX-27. Тесты проверки целостности владения кодом.
===================================================

Что здесь проверяется и почему именно это
─────────────────────────────────────────
Дефект, ради которого всё затевалось, выглядел так: граф потерял связь
«модуль → его методы», а все проверки остались зелёными. Значит, проверять
надо не «правильно ли считает функция» (это тоже, но это малая часть), а
**узнаёт ли она разрушенное состояние по тем числам, которые реально были
на стенде 18 августа**.

Поэтому в наборе есть пример с настоящими числами боевой конфигурации:
231 129 процедур, 0 модулей, 0 рёбер. Если однажды правка «упростит»
пороги так, что это состояние станет нормой, тест скажет об этом.

Запуск:  python3 tests_graph_integrity.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_integrity import (  # noqa: E402
    HAS_METHOD_COUNT_CYPHER, ORPHAN_ALARM_PCT,
    STATE_BROKEN, STATE_EMPTY, STATE_NO_CODE_LAYER, STATE_OK, STATE_PARTIAL,
    log_ownership, ownership_line, ownership_report,
)


class TestBrokenIsRecognised(unittest.TestCase):
    """Состояние 18 августа, число в число."""

    def setUp(self):
        # `metadata_stats` на стенде: modules 0 при 231 129 :Callable.
        self.report = ownership_report(
            callables=231_129, modules=0, has_method=0, objects=10_747)

    def test_state_is_broken(self):
        self.assertEqual(self.report["state"], STATE_BROKEN)

    def test_not_answerable(self):
        self.assertFalse(
            self.report["answerable"],
            "разрушенное владение обязано быть неотвечаемым: иначе агент "
            "сообщит, что у объекта нет процедур",
        )

    def test_meaning_forbids_the_wrong_conclusion(self):
        """
        Главное поле ответа — не диагноз, а запрет на неверный вывод.
        Формулировка «это НЕ значит» пришла из FIX-3 и доказала пользу.
        """
        self.assertIn("НЕ значит", self.report.get("meaning", ""))

    def test_message_names_all_three_numbers(self):
        for number in ("231129", "0"):
            self.assertIn(number, self.report["message"].replace(" ", ""))

    def test_hint_says_how_to_repair(self):
        self.assertIn("METADATA_FORCE_BSL", self.report.get("hint", ""))

    def test_report_carries_object_count(self):
        """
        По этому числу вызывающий выбирает починку: слой 1 цел — хватит
        фазы 2; слоя 1 нет — нужен полный прогон. Пока `objects` оставался
        аргументом и не попадал в отчёт, решение опиралось на ключ,
        которого в словаре нет, и на пустом графе выбирало не ту починку.
        """
        self.assertEqual(self.report["objects"], 10_747)


class TestHealthyGraph(unittest.TestCase):
    """Состояние после успешного полного прогона 18 августа."""

    def setUp(self):
        # Известный штатный остаток: 28 процедур шести модулей форм
        # перечислений, которых нет в слое 1.
        self.report = ownership_report(
            callables=231_129, modules=14_042, has_method=231_101,
            objects=10_747)

    def test_state_is_ok(self):
        self.assertEqual(self.report["state"], STATE_OK)

    def test_answerable_and_not_degraded(self):
        self.assertTrue(self.report["answerable"])
        self.assertFalse(self.report["degraded"])

    def test_known_remainder_is_counted_not_hidden(self):
        self.assertEqual(self.report["without_owner"], 28)
        self.assertEqual(self.report["with_owner"], 231_101)

    def test_no_meaning_when_nothing_is_wrong(self):
        """
        В норме `meaning` не кладётся вовсе. Пустая строка в каждом ответе —
        это текст, который модель читает при каждом вызове и на который
        тратит внимание.
        """
        self.assertNotIn("meaning", self.report)


class TestPartialLoss(unittest.TestCase):

    def test_above_threshold_is_degraded_but_answerable(self):
        report = ownership_report(callables=1000, modules=100,
                                  has_method=900, objects=50)
        self.assertEqual(report["state"], STATE_PARTIAL)
        self.assertTrue(report["answerable"],
                        "часть данных на месте — это ухудшение, а не отказ")
        self.assertTrue(report["degraded"])
        self.assertEqual(report["orphan_pct"], 10.0)

    def test_below_threshold_is_ok(self):
        report = ownership_report(callables=100_000, modules=100,
                                  has_method=99_999, objects=50)
        self.assertEqual(report["state"], STATE_OK)

    def test_threshold_boundary_belongs_to_degraded(self):
        """Ровно на пороге — уже ухудшение: границу надо назвать явно."""
        callables = 1000
        orphans = int(callables * ORPHAN_ALARM_PCT / 100)
        report = ownership_report(callables=callables, modules=10,
                                  has_method=callables - orphans, objects=5)
        self.assertEqual(report["state"], STATE_PARTIAL)


class TestEmptyAndMissingLayers(unittest.TestCase):

    def test_empty_graph(self):
        report = ownership_report(0, 0, 0, objects=0)
        self.assertEqual(report["state"], STATE_EMPTY)
        self.assertFalse(report["answerable"])

    def test_metadata_without_code(self):
        """
        Слой 1 есть, кода нет. Отличать от пустого графа обязательно: это
        разные починки (полный прогон против одной фазы 2).
        """
        report = ownership_report(0, 0, 0, objects=10_747)
        self.assertEqual(report["state"], STATE_NO_CODE_LAYER)
        self.assertIn("10747", report["message"].replace(" ", ""))

    def test_modules_present_but_no_edges_is_broken(self):
        """
        Половинчатый случай: узлы модулей записались, рёбра — нет. Ровно так
        выглядел FIX-14 (69 % HAS_METHOD потеряно), и он обязан считаться
        поломкой, а не «частичной потерей».
        """
        report = ownership_report(231_129, 14_042, 0, objects=10_747)
        self.assertEqual(report["state"], STATE_BROKEN)


class TestArithmeticIsDefensive(unittest.TestCase):

    def test_more_edges_than_callables_does_not_go_negative(self):
        """
        Такого быть не может — владелец у процедуры один. Но если случится,
        отчёт обязан остаться читаемым, а не показать минус.
        """
        report = ownership_report(10, 5, 12, objects=1)
        self.assertEqual(report["without_owner"], 0)
        self.assertEqual(report["orphan_pct"], 0.0)

    def test_none_values_are_survivable(self):
        report = ownership_report(None, None, None, objects=None)
        self.assertEqual(report["state"], STATE_EMPTY)


class TestCypherIsCheap(unittest.TestCase):
    """
    Вся идея проверки держится на том, что она бесплатна. Счётчик по
    КОНКРЕТНОМУ типу ребра берётся из счётчиков хранилища; стоит указать
    метки узлов — и O(1) превратится в обход, то есть проверка начнёт
    стоить ровно того, от чего PERF-12 избавлялся.
    """

    def test_counts_a_single_relationship_type(self):
        self.assertIn(":HAS_METHOD", HAS_METHOD_COUNT_CYPHER)

    def test_no_node_labels_in_the_counting_query(self):
        head = HAS_METHOD_COUNT_CYPHER.split("RETURN")[0]
        self.assertNotIn(":Module", head)
        self.assertNotIn(":Callable", head)


class TestLogOutput(unittest.TestCase):

    def test_line_contains_the_three_numbers(self):
        line = ownership_line(ownership_report(100, 10, 90, objects=5))
        for chunk in ("100", "10", "10.0"):
            self.assertIn(chunk, line)

    def test_log_returns_verdict(self):
        import logging
        log = logging.getLogger("test-integrity")
        log.addHandler(logging.NullHandler())
        self.assertTrue(log_ownership(ownership_report(100, 10, 100, 5), log))
        self.assertFalse(log_ownership(ownership_report(100, 0, 0, 5), log))


if __name__ == "__main__":
    unittest.main(verbosity=2)
