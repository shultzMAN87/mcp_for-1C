"""
Тесты PERF-6 — полнотекстовый поиск.
=====================================

Проверяется `search_fulltext`: подготовка запроса Lucene и распознавание
«индекса нет». Обе вещи — чистые функции без Neo4j и без fastmcp, поэтому
проверяются офлайн; сам `server.py` офлайн не импортируется.

Запуск:
    python tests_search_fulltext.py -v
"""
from __future__ import annotations

import unittest

from search_fulltext import (FULLTEXT_COUNT_CYPHER, FULLTEXT_CYPHER,
                             build_fulltext_query, escape_lucene,
                             fulltext_where, is_missing_index_error)


class TestEscaping(unittest.TestCase):
    """
    Строка приходит от пользователя как текст, а не как выражение Lucene.
    Без экранирования запрос «Контрагенты (ЕГРЮЛ)» или «счёт: фактура»
    уронил бы поиск синтаксической ошибкой на пустом месте.
    """

    def test_parentheses_escaped(self):
        self.assertEqual(escape_lucene("(ЕГРЮЛ)"), r"\(ЕГРЮЛ\)")

    def test_colon_escaped(self):
        self.assertIn(r"\:", escape_lucene("счёт:фактура"))

    def test_quotes_and_brackets_escaped(self):
        out = escape_lucene('"Заказ" [1]')
        for ch in ('\\"', r"\[", r"\]"):
            self.assertIn(ch, out)

    def test_plain_cyrillic_untouched(self):
        self.assertEqual(escape_lucene("Контрагенты"), "Контрагенты")

    def test_empty_input(self):
        self.assertEqual(escape_lucene(""), "")
        self.assertEqual(escape_lucene(None), "")


class TestQueryBuilding(unittest.TestCase):

    def test_prefix_search_for_each_word(self):
        """
        Поиск по началу слова — самый частый способ искать объект. Без
        префикса полнотекстовый индекс совпадал бы только по целым токенам,
        и «контраг» не находило бы «Контрагенты».
        """
        q = build_fulltext_query("контраг")
        self.assertIn("контраг*", q)

    def test_exact_match_weighted_higher(self):
        q = build_fulltext_query("Контрагенты")
        self.assertIn("Контрагенты^2", q)

    def test_words_joined_by_or(self):
        """
        OR, а не AND: «заказ клиента» должно находить и «ЗаказКлиента», и
        «ЗаказПоставщику», отдав первому больший score. AND отсёк бы
        половину полезного.
        """
        q = build_fulltext_query("заказ клиента")
        self.assertIn(" OR ", q)
        self.assertIn("заказ*", q)
        self.assertIn("клиента*", q)

    def test_fuzzy_has_lower_weight(self):
        """Опечаточные совпадения не должны перебивать точные."""
        q = build_fulltext_query("Контрагенты")
        self.assertIn("~1^0.5", q)
        self.assertLess(q.index("^2"), q.index("~1^0.5") + len(q))

    def test_short_words_get_no_fuzzy(self):
        """На трёх буквах опечаточный поиск даёт шум, а не пользу."""
        self.assertNotIn("~", build_fulltext_query("ТТН"))

    def test_fuzzy_can_be_disabled(self):
        self.assertNotIn("~", build_fulltext_query("Контрагенты", fuzzy=False))

    def test_empty_query_returns_empty(self):
        """Пустая строка — сигнал вызывающему не ходить в индекс."""
        for bad in ("", "   ", None, " , ; "):
            self.assertEqual(build_fulltext_query(bad), "")

    def test_special_chars_survive_building(self):
        q = build_fulltext_query("Контрагенты (ЕГРЮЛ)")
        self.assertIn(r"\(ЕГРЮЛ\)", q)

    def test_separators_split_words(self):
        q = build_fulltext_query("заказ, клиента; товар")
        for w in ("заказ", "клиента", "товар"):
            self.assertIn(f"{w}*", q)


class TestWhereClause(unittest.TestCase):

    def test_modules_excluded_by_default(self):
        """
        Узлы модулей объекта и менеджера тоже несут метку :MetadataObject
        (их 5 843), но объектами метаданных в смысле поиска не являются:
        их name — «ObjectModule» / «ManagerModule».
        """
        self.assertIn("NOT n:Module", fulltext_where())

    def test_kind_filter_added(self):
        w = fulltext_where("Справочник")
        self.assertIn("$kind", w)
        self.assertIn("NOT n:Module", w)
        self.assertIn(" AND ", w)

    def test_no_kind_no_and(self):
        self.assertNotIn(" AND ", fulltext_where(""))

    def test_can_include_modules(self):
        self.assertEqual(fulltext_where("", exclude_modules=False), "")

    def test_cypher_templates_accept_where(self):
        for tpl in (FULLTEXT_CYPHER, FULLTEXT_COUNT_CYPHER):
            built = tpl.format(where=fulltext_where("Справочник"))
            self.assertIn("db.index.fulltext.queryNodes", built)
            self.assertIn("NOT n:Module", built)
            self.assertNotIn("{where}", built)

    def test_page_query_orders_by_score(self):
        built = FULLTEXT_CYPHER.format(where="")
        self.assertIn("ORDER BY score DESC", built)
        self.assertIn("SKIP $offset", built)
        self.assertIn("LIMIT $limit", built)


class TestMissingIndexDetection(unittest.TestCase):
    """
    Отличать «нет индекса» от любой другой ошибки критично: если Neo4j лёг
    или запрос синтаксически неверен, поиск должен сказать об этом, а не
    тихо уйти на запасной путь и вернуть результат похуже, притворившись,
    что всё в порядке.
    """

    def test_recognizes_missing_index(self):
        for msg in (
            "There is no such fulltext schema index: meta_fulltext",
            "No such fulltext index: meta_fulltext",
            "Neo.ClientError.Procedure.ProcedureNotFound: unknown procedure",
            "IndexNotFound",
        ):
            self.assertTrue(is_missing_index_error(msg), msg)

    def test_does_not_swallow_other_errors(self):
        for msg in (
            "Neo.TransientError.General.MemoryPoolOutOfMemoryError",
            "Connection refused",
            "Neo.ClientError.Security.Unauthorized",
            "Invalid input 'MATCH'",
        ):
            self.assertFalse(is_missing_index_error(msg), msg)

    def test_accepts_exception_objects(self):
        err = RuntimeError("Neo4j: There is no such fulltext schema index")
        self.assertTrue(is_missing_index_error(err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
