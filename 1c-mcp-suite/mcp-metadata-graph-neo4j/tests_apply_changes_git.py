"""
Тесты SRC-12 — apply_changes.py в режиме git (ручная доиндексация).
===================================================================

Ручной запуск обязан делать ровно то же, что watcher и metadata_refresh(),
и отвечать понятным кодом выхода — по нему работает graph-refresh.ps1.
Режим без git обязан остаться прежним (возврат None — «работай как раньше»).

Нужен git. Без него набор пропускается целиком (CI-5: `ПУСТО`).

Запуск:
    python tests_apply_changes_git.py -v
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import apply_changes
import source_state
import source_sync
from source_state import MemoryStore
from tests_git_source import sh, write
from tests_source_sync import RecordingOps

HAVE_GIT = shutil.which("git") is not None
ENV = ("METADATA_SRC_DIR", "METADATA_REPO_DIR", "SOURCE_MODE",
       "METADATA_MAX_PARTIAL")


class FakeNeo:
    def __init__(self, alive=True):
        self.alive = alive

    def rows(self, cypher, params=None):
        if not self.alive:
            raise OSError("connection refused")
        return [{"ok": 1}]


def args(**kw) -> argparse.Namespace:
    base = dict(dry_run=False, reconcile=False, full=False, yes=False,
                force_unlock=False, max_partial=300)
    base.update(kw)
    return argparse.Namespace(**base)


@unittest.skipUnless(HAVE_GIT, "git не установлен — набор не проверяет ничего")
class TestApplyChangesGit(unittest.TestCase):

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.cfg = self.root / "src"
        write(self.cfg, "Configuration.xml", "<c/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat/>\n")
        write(self.cfg, "Documents/Заказ.xml", "<doc/>\n")
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "core.autocrlf", "false")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "первый")
        os.environ.update({"METADATA_SRC_DIR": str(self.cfg),
                           "METADATA_REPO_DIR": str(self.root),
                           "SOURCE_MODE": "auto"})
        os.environ.pop("METADATA_MAX_PARTIAL", None)
        self.store = MemoryStore()
        self.ops = RecordingOps()
        self.neo = FakeNeo()
        self.saved = [(apply_changes, "_neo_from_env", apply_changes._neo_from_env),
                      (source_state, "Neo4jStore", source_state.Neo4jStore),
                      (source_sync, "Neo4jGraphOps", source_sync.Neo4jGraphOps)]
        apply_changes._neo_from_env = lambda: self.neo
        source_state.Neo4jStore = lambda neo, key="main": self.store
        source_sync.Neo4jGraphOps = lambda neo, root: self.ops

    def tearDown(self):
        for m, n, v in self.saved:
            setattr(m, n, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def adopt(self):
        ctx = source_sync.context_from_env()
        self.store.acquire("indexer")
        source_sync.mark_indexed(ctx, self.store, "indexer", ctx.repo.head(),
                                 full=True)
        self.store.release("indexer")

    def commit(self, msg):
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", msg)

    def test_legacy_returns_none(self):
        os.environ["SOURCE_MODE"] = "legacy"
        self.assertIsNone(apply_changes._git_main(args()))

    def test_no_state(self):
        self.assertEqual(apply_changes._git_main(args()), 3)

    def test_unchanged(self):
        self.adopt()
        self.assertEqual(apply_changes._git_main(args()), 0)
        self.assertEqual(self.ops.calls, [])

    def test_catch_up(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='2'/>\n")
        self.commit("pull")
        self.assertEqual(apply_changes._git_main(args()), 0)
        self.assertEqual(self.ops.calls, [("upsert", "Documents/Заказ.xml")])

    def test_dry_run(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='3'/>\n")
        self.commit("pull")
        self.assertEqual(apply_changes._git_main(args(dry_run=True)), 0)
        self.assertEqual(self.ops.calls, [])

    def test_reconcile(self):
        self.adopt()
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='руками'/>\n")
        self.assertEqual(apply_changes._git_main(args(reconcile=True)), 0)
        self.assertEqual(self.ops.calls, [("upsert", "Catalogs/Товары.xml")])

    def test_busy_and_force_unlock(self):
        self.adopt()
        self.store.acquire("watcher", ttl=3600)
        self.assertEqual(apply_changes._git_main(args()), 4)
        self.assertEqual(apply_changes._git_main(args(force_unlock=True)), 0)
        self.assertEqual(self.store.lock_owner, "")
        self.assertEqual(apply_changes._git_main(args()), 0)

    def test_needs_full(self):
        self.adopt()
        os.environ["METADATA_MAX_PARTIAL"] = "1"
        write(self.cfg, "Documents/Заказ.xml", "<doc v='4'/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='4'/>\n")
        self.commit("pull")
        self.assertEqual(apply_changes._git_main(args()), 3)

    def test_failure(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='5'/>\n")
        self.commit("pull")
        self.ops.fail_on = {"Documents/Заказ.xml"}
        self.assertEqual(apply_changes._git_main(args()), 1)

    def test_neo4j_down(self):
        self.neo.alive = False
        self.assertEqual(apply_changes._git_main(args()), 2)

    def test_full_asks_and_can_be_cancelled(self):
        import builtins
        orig = builtins.input
        builtins.input = lambda prompt="": "n"
        try:
            self.assertEqual(apply_changes._run_full(assume_yes=False), 0)
        finally:
            builtins.input = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
