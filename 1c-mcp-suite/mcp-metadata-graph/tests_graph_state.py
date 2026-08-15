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
    graph_state, graph_error, make_guard, make_state_probe,
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



class TestGuardFastFail(unittest.TestCase):
    """
    B-1: быстрый отказ, когда уже известно, что Neo4j лежит.

    Замер 15 августа при остановленной Neo4j: восемнадцать примеров по
    3 850 мс каждый, потому что guard опрашивал базу на каждом вызове.
    У platform-help то же самое было починено `FAIL-1` (7 916 мс → 102 мс),
    сюда не дошло.

    Проверяется не время (оно зависит от машины), а факт обращения к базе.
    """

    def _counting_query(self, exc=None, count=5):
        calls = {"n": 0}

        def query(cypher, parameters=None):
            calls["n"] += 1
            if exc is not None:
                raise exc
            return {"results": [{"columns": ["cnt"],
                                 "data": [{"row": [count]}]}], "errors": []}
        return query, calls

    def test_unavailable_is_probed_once_then_cached(self):
        query, calls = self._counting_query(exc=OSError("нет связи"))
        guard = make_guard(query, recheck_sec=30)

        for _ in range(5):
            err = json.loads(guard())
            self.assertEqual(err["error"], "neo4j_unavailable")

        self.assertEqual(calls["n"], 1,
                         "каждый вызов снова идёт в сеть — четыре секунды "
                         "тишины на инструмент вернулись")

    def test_cached_refusal_says_it_is_cached(self):
        """
        Урок приёмки 15 августа: `metadata_stats` отдавал из кеша картину
        здоровья работающего графа в момент, когда графа не было, и по
        ответу это было никак не видно. Кешированный ответ обязан называть
        себя кешированным.
        """
        query, _ = self._counting_query(exc=OSError("нет связи"))
        guard = make_guard(query, recheck_sec=30)
        guard()
        err = json.loads(guard())
        self.assertIn("кеша", err.get("detail", ""))

    def test_window_expiry_reprobes(self):
        """
        Кеш не навсегда: иначе поднявшаяся Neo4j осталась бы незамеченной
        до перезапуска контейнера — дефект FIX-12, только наоборот.
        """
        query, calls = self._counting_query(exc=OSError("нет связи"))
        guard = make_guard(query, recheck_sec=0)  # окно нулевое
        guard()
        guard()
        self.assertEqual(calls["n"], 2, "окно истекло, а проверки не было")

    def test_recovery_without_restart(self):
        """Neo4j вернулась — guard обязан пропустить, а не держать отказ."""
        state = {"alive": False}

        def query(cypher, parameters=None):
            if not state["alive"]:
                raise OSError("нет связи")
            return {"results": [{"columns": ["cnt"],
                                 "data": [{"row": [5]}]}], "errors": []}

        guard = make_guard(query, recheck_sec=0)
        self.assertIsNotNone(guard())
        state["alive"] = True
        self.assertIsNone(guard(), "база вернулась, а guard всё ещё отказывает")

    def test_ok_is_never_cached(self):
        """
        Кешировать удачную пробу нельзя: сервер ослеп бы к падению Neo4j на
        всё окно. Проба на живой базе стоит миллисекунды.
        """
        query, calls = self._counting_query(count=5)
        guard = make_guard(query, recheck_sec=30)
        for _ in range(3):
            self.assertIsNone(guard())
        self.assertEqual(calls["n"], 3, "удачное состояние закешировалось")

    def test_empty_is_never_cached(self):
        """
        Пустой граф тоже не кешируем: Neo4j отвечает, проба дешёвая, а кеш
        задержал бы момент, когда индексация закончилась.
        """
        query, calls = self._counting_query(count=0)
        guard = make_guard(query, recheck_sec=30)
        for _ in range(3):
            self.assertEqual(json.loads(guard())["error"], "graph_empty")
        self.assertEqual(calls["n"], 3, "пустое состояние закешировалось")

    def test_probe_and_guard_share_one_cache(self):
        """
        B-1, вторая половина. Замер 15 августа: после первой правки поиск
        стал отвечать за 15 мс, а `metadata_stats` продолжал платить
        3 850 мс — он спрашивает состояние напрямую, а не через guard,
        потому что ему нужно различать пустой граф и мёртвую базу.

        Кеш был написан, а трое потребителей из четырёх им не пользовались.
        Проверяем, что теперь он один на всех.
        """
        query, calls = self._counting_query(exc=OSError("нет связи"))
        probe = make_state_probe(query, recheck_sec=30)
        guard = make_guard(probe=probe)

        probe()
        guard()
        probe()
        guard()

        self.assertEqual(calls["n"], 1,
                         "у пробы и guard-а разные кеши — инструменты "
                         "состояния снова ждут таймаут")

    def test_probe_returns_state_not_json(self):
        """
        Проба отдаёт пару, а не готовый ответ: тем, кто её зовёт, нужно
        РАЗЛИЧАТЬ пустой граф и мёртвую базу, а guard блокирует оба.
        """
        query, _ = self._counting_query(count=0)
        state, _detail = make_state_probe(query)()
        self.assertEqual(state, GRAPH_EMPTY)

    def test_reset_clears_the_cache(self):
        query, calls = self._counting_query(exc=OSError("нет связи"))
        guard = make_guard(query, recheck_sec=30)
        guard()
        guard.reset()
        guard()
        self.assertEqual(calls["n"], 2)


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
