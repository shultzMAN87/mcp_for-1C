"""
Тесты SCALE-1 (границы подсистемы) и FIX-16 (сломанные запросы подсистем).
===========================================================================

Проверяется `subsystem_scope` — чистые функции без Neo4j и без fastmcp, —
и то, что в `server.py` не осталось запросов к ребру с русским именем.

Запуск:
    python tests_subsystem_scope.py -v
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from subsystem_scope import (ENV_DEFAULT_SUBSYSTEM, SCOPE_ALL,
                             SUBSYSTEM_MAX_DEPTH, apply_scope,
                             normalize_subsystem_name, resolve_subsystem,
                             scope_note, subsystem_scope_cypher)


class TestResolve(unittest.TestCase):
    """Приоритет: явный параметр → окружение → вся база."""

    def setUp(self):
        self._saved = os.environ.pop(ENV_DEFAULT_SUBSYSTEM, None)

    def tearDown(self):
        os.environ.pop(ENV_DEFAULT_SUBSYSTEM, None)
        if self._saved is not None:
            os.environ[ENV_DEFAULT_SUBSYSTEM] = self._saved

    def test_nothing_set_means_whole_base(self):
        self.assertEqual(resolve_subsystem(), "")

    def test_env_is_the_default(self):
        os.environ[ENV_DEFAULT_SUBSYSTEM] = "Продажи"
        self.assertEqual(resolve_subsystem(), "Продажи")

    def test_explicit_overrides_env(self):
        os.environ[ENV_DEFAULT_SUBSYSTEM] = "Продажи"
        self.assertEqual(resolve_subsystem("Закупки"), "Закупки")

    def test_star_cancels_env(self):
        """
        Без этого, задав METADATA_DEFAULT_SUBSYSTEM, нельзя было бы разово
        поискать по всей конфигурации — а это обычная нужда.
        """
        os.environ[ENV_DEFAULT_SUBSYSTEM] = "Продажи"
        self.assertEqual(resolve_subsystem(SCOPE_ALL), "")

    def test_whitespace_is_not_a_subsystem(self):
        self.assertEqual(resolve_subsystem("   "), "")
        os.environ[ENV_DEFAULT_SUBSYSTEM] = "  "
        self.assertEqual(resolve_subsystem(), "")

    def test_surrounding_spaces_trimmed(self):
        self.assertEqual(resolve_subsystem("  Продажи  "), "Продажи")


class TestNameForms(unittest.TestCase):
    """
    Агент видит подсистему то как `Продажи`, то как `Subsystem.Продажи` —
    в зависимости от того, какой инструмент её показал. Требовать угадывать
    форму значит возвращать пустоту на верный запрос.
    """

    def test_prefixes_stripped(self):
        for raw in ("Subsystem.Продажи", "Подсистема.Продажи",
                    "subsystem.Продажи", "ПОДСИСТЕМА.Продажи"):
            self.assertEqual(normalize_subsystem_name(raw), "Продажи", raw)

    def test_bare_name_untouched(self):
        self.assertEqual(normalize_subsystem_name("Продажи"), "Продажи")

    def test_dotted_name_is_not_a_prefix(self):
        """Точка в имени сама по себе не означает префикс вида."""
        self.assertEqual(normalize_subsystem_name("Продажи.Опт"), "Продажи.Опт")

    def test_empty(self):
        self.assertEqual(normalize_subsystem_name(""), "")
        self.assertEqual(normalize_subsystem_name(None), "")


class TestScopeCypher(unittest.TestCase):

    def test_nested_subsystems_included(self):
        """
        Главное свойство. «Продажи» в 1С — обычно верхний уровень с десятком
        дочерних, и объекты висят на дочерних. Фильтр без обхода вложенности
        вернул бы почти пустоту и выглядел бы как «в подсистеме ничего нет».
        """
        c = subsystem_scope_cypher()
        self.assertIn(f"PARENT_OF*0..{SUBSYSTEM_MAX_DEPTH}", c)

    def test_zero_depth_includes_root_itself(self):
        """`*0..N` — объекты самой подсистемы тоже должны попадать."""
        self.assertIn("*0..", subsystem_scope_cypher())

    def test_uses_english_contains(self):
        """
        FIX-16: ребро называется CONTAINS. Русского ребра в графе нет, и
        запрос с ним молча вернул бы пустоту.
        """
        c = subsystem_scope_cypher()
        self.assertIn("[:CONTAINS]", c)
        self.assertNotIn("СОДЕРЖИТ", c)

    def test_all_name_forms_matched(self):
        c = subsystem_scope_cypher()
        for prop in ("sub.name", "sub.full_name_eng", "sub.full_name_ru"):
            self.assertIn(prop, c)

    def test_alias_and_param_are_honoured(self):
        c = subsystem_scope_cypher(alias="m", param="ss")
        self.assertIn("->(m) }", c)
        self.assertIn("$ss", c)

    def test_apply_scope_adds_nothing_when_empty(self):
        base = ["n.kind = 'Catalog'"]
        self.assertEqual(apply_scope(base, ""), base)

    def test_apply_scope_appends(self):
        out = apply_scope(["a"], "Продажи")
        self.assertEqual(len(out), 2)
        self.assertIn("CONTAINS", out[1])

    def test_apply_scope_does_not_mutate_input(self):
        base = ["a"]
        apply_scope(base, "Продажи")
        self.assertEqual(base, ["a"])


class TestScopeNote(unittest.TestCase):
    """
    Ограниченная выдача без пометки — тихая ложь: агент решит, что объектов
    больше нет. Тот же принцип, что в TOOL-2 и FIX-15.
    """

    def test_no_note_without_scope(self):
        self.assertEqual(scope_note("", 10, True), {})

    def test_note_states_the_limit_and_the_escape(self):
        out = scope_note("Продажи", 5, True)
        self.assertEqual(out["subsystem_scope"], "Продажи")
        self.assertIn("Продажи", out["scope_note"])
        self.assertIn(SCOPE_ALL, out["scope_note"])

    def test_empty_result_explains_both_causes(self):
        """
        Пусто может означать «в подсистеме правда нет» или «имя неверное».
        Различить их отсюда нельзя, поэтому честнее назвать обе причины.
        """
        out = scope_note("Прдажи", 0, False)
        self.assertIn("scope_warning", out)
        self.assertIn("неверно", out["scope_warning"])
        self.assertIn("metadata_subsystems", out["scope_warning"])

    def test_no_warning_when_something_found(self):
        self.assertNotIn("scope_warning", scope_note("Продажи", 3, True))


class TestFix16NoRussianEdge(unittest.TestCase):
    """
    FIX-16. В server.py запросы к подсистемам использовали ребро с русским
    именем, которого в графе нет: writer пишет CONTAINS. Neo4j на
    несуществующий тип ребра отвечает пустым результатом, а не ошибкой, —
    поэтому три инструмента молча возвращали пустоту.
    """

    def _server_source(self) -> str:
        return (Path(__file__).with_name("server.py")).read_text(encoding="utf-8")

    def test_no_russian_relationship_in_cypher(self):
        src = self._server_source()
        self.assertIsNone(
            re.search(r"\[:\s*СОДЕРЖИТ", src),
            "запрос к ребру с русским именем вернёт пустоту молча",
        )

    def test_subsystem_queries_use_contains(self):
        src = self._server_source()
        self.assertGreaterEqual(src.count("[:CONTAINS]"), 3,
                                "три инструмента подсистем должны ходить по CONTAINS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
