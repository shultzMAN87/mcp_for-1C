"""
Юнит-тесты диагностики состояния графа (FIX-3).

Главное, что здесь проверяется: «Neo4j недоступна» и «граф пуст» дают
РАЗНЫЕ ответы, и в обоих есть явный запрет домысливать содержимое
конфигурации. До FIX-3 оба случая давали одинаковое «Neo4j недоступен»,
и агент на этом основании сообщал, что искомого объекта в конфигурации нет.

Тестируется `graph_state.py` — модуль без зависимости от FastMCP. Сам
`server.py` юнит-тестами не покрыть: при импорте он поднимает FastMCP и
требует NEO4J_PASSWORD, а `mcp` на хосте обычно не установлен.

Запуск (с хоста, без Docker):
    python tests_graph_state.py
или
    python -m unittest tests_graph_state -v
"""
from __future__ import annotations

import json
import unittest

from graph_state import (
    GRAPH_OK, GRAPH_EMPTY, GRAPH_UNAVAILABLE,
    graph_state, graph_error, make_guard,
    NON_CONFIG_CALL_REASONS, NON_CONFIG_REASONS_CYPHER,
)


def fake_query(count=None, exc=None, empty_rows=False):
    """Подменяет _neo4j_query: отдаёт заданный count или бросает ошибку."""
    def query(cypher, parameters=None):
        if exc is not None:
            raise exc
        data = [] if empty_rows else [{"row": [count]}]
        return {"results": [{"columns": ["cnt"], "data": data}], "errors": []}
    return query


class TestGraphState(unittest.TestCase):

    def test_populated_graph_is_ok(self):
        state, detail = graph_state(fake_query(count=102))
        self.assertEqual(state, GRAPH_OK)
        self.assertEqual(detail, "")

    def test_zero_objects_is_empty_not_unavailable(self):
        # Ключевое различие FIX-3: пустой граф — это НЕ «Neo4j недоступна».
        state, _ = graph_state(fake_query(count=0))
        self.assertEqual(state, GRAPH_EMPTY)

    def test_no_rows_is_empty(self):
        state, _ = graph_state(fake_query(empty_rows=True))
        self.assertEqual(state, GRAPH_EMPTY)

    def test_transport_error_is_unavailable(self):
        state, detail = graph_state(fake_query(exc=OSError("connection refused")))
        self.assertEqual(state, GRAPH_UNAVAILABLE)
        self.assertIn("connection refused", detail)

    def test_cypher_error_is_unavailable(self):
        state, _ = graph_state(fake_query(exc=RuntimeError("Neo4j: [SyntaxError]")))
        self.assertEqual(state, GRAPH_UNAVAILABLE)

    def test_malformed_response_is_unavailable_not_crash(self):
        # Ответ неожиданной формы не должен ронять сервер.
        def broken(cypher, parameters=None):
            return {"unexpected": True}
        state, _ = graph_state(broken)
        self.assertEqual(state, GRAPH_UNAVAILABLE)


class TestGraphError(unittest.TestCase):

    def _payload(self, state, detail=""):
        return json.loads(graph_error(state, detail))

    def test_empty_and_unavailable_differ(self):
        empty = self._payload(GRAPH_EMPTY)
        unavail = self._payload(GRAPH_UNAVAILABLE)
        self.assertNotEqual(empty["error"], unavail["error"])
        self.assertEqual(empty["error"], "graph_empty")
        self.assertEqual(unavail["error"], "neo4j_unavailable")

    def test_both_forbid_concluding_absence(self):
        # Ради этого вся задача: агент не должен решить, что объекта нет.
        for state in (GRAPH_EMPTY, GRAPH_UNAVAILABLE):
            p = self._payload(state)
            self.assertFalse(p["answerable"], state)
            self.assertIn("НЕ значит", p["meaning"], state)

    def test_state_is_machine_readable(self):
        for state in (GRAPH_EMPTY, GRAPH_UNAVAILABLE):
            self.assertEqual(self._payload(state)["graph_state"], state)

    def test_both_carry_actionable_hint(self):
        for state in (GRAPH_EMPTY, GRAPH_UNAVAILABLE):
            self.assertTrue(self._payload(state)["hint"].strip(), state)

    def test_empty_hint_points_at_indexer(self):
        self.assertIn("metadata-indexer", self._payload(GRAPH_EMPTY)["hint"])

    def test_unavailable_hint_points_at_container(self):
        self.assertIn("neo4j", self._payload(GRAPH_UNAVAILABLE)["hint"])

    def test_detail_included_only_when_present(self):
        self.assertIn("detail", self._payload(GRAPH_UNAVAILABLE, "OSError: boom"))
        self.assertNotIn("detail", self._payload(GRAPH_UNAVAILABLE))

    def test_output_is_valid_json(self):
        for state in (GRAPH_EMPTY, GRAPH_UNAVAILABLE):
            json.loads(graph_error(state))   # не должно бросить


class TestGuard(unittest.TestCase):

    def test_guard_passes_on_populated_graph(self):
        self.assertIsNone(make_guard(fake_query(count=1))())

    def test_guard_blocks_on_empty_graph(self):
        err = json.loads(make_guard(fake_query(count=0))())
        self.assertEqual(err["error"], "graph_empty")

    def test_guard_blocks_on_unavailable(self):
        err = json.loads(make_guard(fake_query(exc=OSError("boom")))())
        self.assertEqual(err["error"], "neo4j_unavailable")

    def test_guard_reflects_state_change(self):
        # Guard не кэширует: граф наполнили — инструменты заработали.
        calls = {"n": 0}

        def query(cypher, parameters=None):
            calls["n"] += 1
            count = 0 if calls["n"] == 1 else 5
            return {"results": [{"columns": ["cnt"],
                                 "data": [{"row": [count]}]}], "errors": []}

        guard = make_guard(query)
        self.assertIsNotNone(guard())
        self.assertIsNone(guard())


class TestNonConfigReasons(unittest.TestCase):
    """Константа server-side; парная лежит в indexer-side bsl_resolver.py."""

    def test_covers_fix4_and_fix41_reasons(self):
        for reason in ("object_method", "context_property", "platform_global",
                       "collection_unknown_method",
                       "dataflow_kind_no_module_role"):
            self.assertIn(reason, NON_CONFIG_CALL_REASONS)

    def test_real_gap_reasons_are_not_hidden(self):
        for reason in ("unknown_module", "unknown_local_method",
                       "unknown_method_in_common_module",
                       "method_not_in_resolved_module",
                       "stale_after_incremental"):
            self.assertNotIn(reason, NON_CONFIG_CALL_REASONS)

    def test_cypher_literal_is_valid_list(self):
        self.assertTrue(NON_CONFIG_REASONS_CYPHER.startswith("["))
        self.assertTrue(NON_CONFIG_REASONS_CYPHER.endswith("]"))
        for reason in NON_CONFIG_CALL_REASONS:
            self.assertIn(f"'{reason}'", NON_CONFIG_REASONS_CYPHER)

    def test_in_sync_with_resolver(self):
        """Server-side и indexer-side копии не должны разъезжаться.

        Пропускается, если indexer-side рядом нет (в контейнере
        mcp-metadata-graph они лежат в одном /app, в репозитории — в
        соседних каталогах).
        """
        import sys
        from pathlib import Path
        candidates = [
            Path("/app"),
            Path(__file__).resolve().parent.parent / "mcp-metadata-graph-neo4j",
        ]
        for path in candidates:
            if (path / "bsl_resolver.py").is_file():
                sys.path.insert(0, str(path))
                break
        else:
            self.skipTest("bsl_resolver.py рядом не найден")
        from bsl_resolver import NON_CONFIG_CALL_REASONS as indexer_side
        self.assertEqual(set(NON_CONFIG_CALL_REASONS), set(indexer_side))


if __name__ == "__main__":
    unittest.main(verbosity=2)
