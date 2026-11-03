"""
Тесты SRC-6 — состояние источника и аренда записи.
==================================================

Две части.

1. `MemoryStore` и сериализация `SourceState` — офлайн, всегда. На
   MemoryStore держатся тесты логики (tests_source_sync.py), поэтому его
   семантика аренды обязана совпадать с боевой — это и проверяется здесь
   одним и тем же набором сценариев для обоих хранилищ.

2. `Neo4jStore` против живой базы — только при заданном `NEO4J_TEST_URL`
   (как tests_graph_writer.py). Ключ узла — случайный `test-…`, рабочий
   `main` не трогается; узел удаляется в tearDown. Отдельно — гонка:
   два потока берут аренду одновременно, выиграть обязан ровно один.

Запуск на стенде (PowerShell):
    $env:NEO4J_TEST_URL  = "http://localhost:7474"
    $env:NEO4J_TEST_USER = "neo4j"
    $env:NEO4J_TEST_PASS = "<пароль из .env>"
    python tests_source_state.py -v
"""
from __future__ import annotations

import os
import threading
import time
import unittest
import uuid

from source_state import (DELETED, LeaseLost, MemoryStore, Neo4jStore,
                          SourceState)


class TestSerialization(unittest.TestCase):

    def test_round_trip(self):
        st = SourceState(indexed_sha="abc", config_subdir="src",
                         dirty={"Catalogs/Х.xml": "h1", "Documents/Д.xml": DELETED},
                         needs_full=True, needs_full_reason="причина",
                         last_change={"files": 3, "by": "watcher"},
                         last_tick_at=123.5)
        props = st.to_props()
        self.assertIsInstance(props["dirty_json"], str)
        self.assertNotIn("dirty", props)
        back = SourceState.from_props(props)
        self.assertEqual(back, st)

    def test_broken_json_means_reconcile(self):
        """Битый dirty не должен читаться как «граф = коммит»."""
        props = SourceState(indexed_sha="abc").to_props()
        props["dirty_json"] = "{не json"
        st = SourceState.from_props(props)
        self.assertEqual(st.dirty, {})
        self.assertTrue(st.needs_reconcile)

    def test_unknown_and_null_props_are_tolerated(self):
        st = SourceState.from_props({"mode": "git", "indexed_sha": None,
                                     "lock_owner": "x", "что-то": 1})
        self.assertEqual(st.indexed_sha, "")


class LeaseContract:
    """Сценарии аренды — одни и те же для MemoryStore и Neo4jStore."""

    store = None   # задаёт наследник

    def test_acquire_release(self):
        a = self.store.acquire("watcher", 60)
        self.assertTrue(a.ok)
        b = self.store.acquire("refresh", 60)
        self.assertFalse(b.ok)
        self.assertEqual(b.owner, "watcher")
        self.store.release("watcher")
        self.assertTrue(self.store.acquire("refresh", 60).ok)

    def test_same_owner_extends(self):
        self.assertTrue(self.store.acquire("watcher", 60).ok)
        self.assertTrue(self.store.acquire("watcher", 600).ok)

    def test_expired_is_taken_over(self):
        self.assertTrue(self.store.acquire("упавший", 60, now=time.time() - 120).ok)
        self.assertTrue(self.store.acquire("watcher", 60).ok)

    def test_release_by_stranger_is_ignored(self):
        self.store.acquire("watcher", 60)
        self.store.release("refresh")
        self.assertFalse(self.store.acquire("refresh", 60).ok)

    def test_write_requires_live_lease(self):
        with self.assertRaises(LeaseLost):
            self.store.write(SourceState(indexed_sha="x"), "никто")
        self.store.acquire("watcher", 60)
        self.store.write(SourceState(indexed_sha="x", dirty={"a": "b"}), "watcher")
        got = self.store.read()
        self.assertEqual((got.indexed_sha, got.dirty), ("x", {"a": "b"}))
        with self.assertRaises(LeaseLost):
            self.store.write(SourceState(indexed_sha="y"), "refresh")
        self.assertEqual(self.store.read().indexed_sha, "x")

    def test_lease_alone_is_not_state(self):
        """Узел, созданный одной арендой, — это «состояния нет»."""
        self.store.acquire("watcher", 60)
        self.assertIsNone(self.store.read())

    def test_force_unlock(self):
        self.store.acquire("indexer", 3600)
        self.store.force_unlock()
        self.assertTrue(self.store.acquire("watcher", 60).ok)

    def test_holder(self):
        self.store.acquire("indexer", 3600)
        h = self.store.holder()
        self.assertTrue(h.ok)
        self.assertEqual(h.owner, "indexer")


class TestMemoryStore(LeaseContract, unittest.TestCase):

    def setUp(self):
        self.store = MemoryStore()

    def test_read_returns_a_copy(self):
        self.store.acquire("w", 60)
        self.store.write(SourceState(dirty={"a": "1"}), "w")
        st = self.store.read()
        st.dirty["b"] = "2"
        self.assertEqual(self.store.read().dirty, {"a": "1"})


# ─── Живой Neo4j ──────────────────────────────────────────────────────────


def _live_neo():
    url = os.environ.get("NEO4J_TEST_URL")
    if not url:
        return None
    from graph_writer import Neo4j
    neo = Neo4j(url, os.environ.get("NEO4J_TEST_USER", "neo4j"),
                os.environ.get("NEO4J_TEST_PASS", "neo4j"), timeout=30)
    try:
        neo.rows("RETURN 1 AS x")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] NEO4J_TEST_URL задан, но база не отвечает: {e}")
        return None
    return neo


LIVE = _live_neo()


@unittest.skipUnless(LIVE, "NEO4J_TEST_URL не задан — Cypher-часть не проверяется")
class TestNeo4jStore(LeaseContract, unittest.TestCase):

    def setUp(self):
        self.key = f"test-{uuid.uuid4().hex[:12]}"
        self.store = Neo4jStore(LIVE, key=self.key)
        self.store.ensure_schema()

    def tearDown(self):
        LIVE.rows("MATCH (s:SourceState {key: $k}) DELETE s", {"k": self.key})

    def test_constraint_is_idempotent(self):
        self.store.ensure_schema()
        self.store.ensure_schema()

    def test_state_round_trip_through_neo4j(self):
        self.store.acquire("w", 60)
        st = SourceState(indexed_sha="abc", config_subdir="src",
                         dirty={"Справочники/Х.xml": DELETED},
                         last_change={"files": 2})
        self.store.write(st, "w")
        got = self.store.read()
        self.assertEqual(got.dirty, st.dirty)
        self.assertEqual(got.last_change, {"files": 2})
        self.assertEqual(got.config_subdir, "src")

    def test_race_has_exactly_one_winner(self):
        """
        Двадцать потоков берут аренду одновременно. Без захвата узла до
        чтения (`SET s._lock_probe`) несколько из них прочитали бы
        «свободно» и взяли бы аренду все.
        """
        results: list = []
        barrier = threading.Barrier(20)

        def worker(i: int):
            barrier.wait()
            results.append(self.store.acquire(f"w{i}", 60).ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(sum(results), 1, results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
