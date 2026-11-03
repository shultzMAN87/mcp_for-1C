"""
Тесты SRC-8/12 — инструмент `metadata_refresh` и блок `source`.
================================================================

Инструменты регистрируются на заглушке MCP (`FakeMCP` просто собирает
функции), выгрузка — настоящий временный git-репозиторий, хранилище
состояния — MemoryStore, граф — запись вызовов. Так проверяется то, что
видит агент: форма ответа, статусы путей, маршрут старых
`metadata_upsert_file` / `metadata_remove_file` через sync в режиме git и
прежнее поведение в режиме legacy.

Нужен git. Без него набор пропускается целиком (CI-5: `ПУСТО`).

Запуск:
    python tests_refresh_tool.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "mcp-metadata-graph-neo4j"))
sys.path.insert(0, str(HERE.parent))

import server_v3_watch_tools as wt                       # noqa: E402
from source_state import MemoryStore                      # noqa: E402
from source_sync import mark_indexed                      # noqa: E402
from tests_git_source import sh, write                    # noqa: E402
from tests_source_sync import RecordingOps                # noqa: E402

HAVE_GIT = shutil.which("git") is not None

ENV_KEYS = ("METADATA_SRC_DIR", "METADATA_REPO_DIR", "SOURCE_MODE",
            "METADATA_CONFIG_SUBDIR", "WATCHER_ENABLED", "SOURCE_POLL_SEC")


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@unittest.skipUnless(HAVE_GIT, "git не установлен — набор не проверяет ничего")
class ToolCase(unittest.TestCase):

    git = True

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.cfg = self.root / "src"
        write(self.cfg, "Configuration.xml", "<c/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat/>\n")
        write(self.cfg, "Catalogs/Товары/Ext/ObjectModule.bsl", "// объект\n")
        write(self.root, "AGENTS.md", "служебное\n")
        if self.git:
            sh(self.root, "init", "-q", "-b", "main")
            sh(self.root, "config", "core.autocrlf", "false")
            sh(self.root, "add", "-A")
            sh(self.root, "commit", "-q", "-m", "первый")
        os.environ.update({"METADATA_SRC_DIR": str(self.cfg),
                           "METADATA_REPO_DIR": str(self.root),
                           "SOURCE_MODE": "auto",
                           "METADATA_CONFIG_SUBDIR": "src",
                           "WATCHER_ENABLED": "false",
                           "SOURCE_POLL_SEC": "60"})
        self.store = MemoryStore()
        self.ops = RecordingOps()
        wt._STORE_FACTORY = lambda: self.store
        wt._OPS_FACTORY = lambda ctx: self.ops
        wt._SOURCE_CACHE.update(ctx=None, at=0.0, error="")
        self.mcp = FakeMCP()
        wt.register_v3_watch_tools(self.mcp, str(self.cfg), "http://нет:7474",
                                   "neo4j", "x", lambda: True,
                                   graph_state=lambda: ("ok", ""))

    def tearDown(self):
        wt._STORE_FACTORY = None
        wt._OPS_FACTORY = None
        wt._SOURCE_CACHE.update(ctx=None, at=0.0, error="")
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def call(self, name, *args, **kw) -> dict:
        return json.loads(self.mcp.tools[name](*args, **kw))

    def adopt(self):
        ctx = wt._source_context(force=True)
        head = ctx.repo.head()
        self.store.acquire("indexer")
        mark_indexed(ctx, self.store, "indexer", head, full=True)
        self.store.release("indexer")


class TestRefreshTool(ToolCase):

    def test_registered(self):
        self.assertEqual(set(self.mcp.tools),
                         {"metadata_refresh", "metadata_upsert_file",
                          "metadata_remove_file"})

    def test_no_state(self):
        out = self.call("metadata_refresh")
        self.assertEqual(out["status"], "no_state")
        self.assertIn("индекс", out["source"]["hint"])

    def test_refresh_with_paths(self):
        self.adopt()
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        write(self.cfg, rel, "// правка агента\n")
        out = self.call("metadata_refresh",
                        paths=[str(self.cfg / rel), "AGENTS.md"])
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["paths"][rel], "reindexed")
        self.assertEqual(out["paths"]["AGENTS.md"], "outside_config")
        self.assertEqual(out["dirty_count"], 1)
        self.assertEqual(out["source"]["dirty_count"], 1)
        self.assertEqual(self.ops.calls, [("upsert", rel)])

    def test_refresh_without_args_after_commit(self):
        self.adopt()
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='2'/>\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "pull")
        out = self.call("metadata_refresh")
        self.assertEqual(out["status"], "ok", out)
        self.assertTrue(out["source"]["in_sync"])
        self.assertIn(("upsert", "Catalogs/Товары.xml"), self.ops.calls)

    def test_dry_run(self):
        self.adopt()
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='3'/>\n")
        out = self.call("metadata_refresh", paths=["src/Catalogs/Товары.xml"],
                        dry_run=True)
        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(out["planned"], ["Catalogs/Товары.xml"])
        self.assertEqual(self.ops.calls, [])

    def test_busy(self):
        self.adopt()
        self.store.acquire("indexer", 3600)
        out = self.call("metadata_refresh", paths=["Catalogs/Товары.xml"])
        self.assertEqual(out["status"], "busy")
        self.assertEqual(out["lease_holder"], "indexer")

    def test_old_tools_go_through_sync_in_git_mode(self):
        """
        Без этого metadata_upsert_file обновлял бы граф, но не dirty — и
        следующий возврат канона остался бы незамеченным.
        """
        self.adopt()
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        write(self.cfg, rel, "// через старый инструмент\n")
        out = self.call("metadata_upsert_file", rel)
        self.assertEqual(out["status"], "ok", out)
        self.assertIn(rel, self.store.read().dirty)

    def test_stats_block(self):
        self.adopt()
        b = wt.source_stats_block()
        self.assertEqual(b["mode"], "git")
        self.assertTrue(b["in_sync"])
        self.assertFalse(b["watcher"]["enabled"])
        self.assertIn("сверял", b["hint"])         # после полной — needs_reconcile
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='4'/>\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "pull")
        b = wt.source_stats_block()
        self.assertFalse(b["in_sync"])
        self.assertIn("отстаёт", b["hint"])
        self.assertIn("WATCHER_ENABLED", b["hint"])
        os.environ["WATCHER_ENABLED"] = "true"
        self.assertIn("watcher включён", wt.source_stats_block()["hint"])

    def test_stats_block_never_raises(self):
        def broken():
            raise RuntimeError("Neo4j лежит")
        wt._STORE_FACTORY = broken
        b = wt.source_stats_block()
        self.assertEqual(b["mode"], "unknown")
        self.assertIn("Neo4j лежит", b["error"])


class TestLegacyMode(ToolCase):
    """Каталог без git: всё работает, как до SRC."""

    git = False

    def setUp(self):
        super().setUp()
        self.calls = []
        import incremental
        self._saved = (incremental.upsert_file, incremental.remove_file)

        def up(neo, root, p):
            self.calls.append(("upsert", p))
            return {"status": "reindexed"}
        # register_v3_watch_tools импортировал функции при регистрации —
        # подменяем и перерегистрируем.
        incremental.upsert_file = up
        self.mcp = FakeMCP()
        wt.register_v3_watch_tools(self.mcp, str(self.cfg), "http://нет:7474",
                                   "neo4j", "x", lambda: True,
                                   graph_state=lambda: ("ok", ""))

    def tearDown(self):
        import incremental
        incremental.upsert_file, incremental.remove_file = self._saved
        super().tearDown()

    def test_refresh_in_legacy(self):
        out = self.call("metadata_refresh", paths=["Catalogs/Товары.xml"])
        self.assertEqual(out["status"], "legacy")
        self.assertEqual(out["paths"], {"Catalogs/Товары.xml": "reindexed"})
        self.assertEqual(self.calls, [("upsert", "Catalogs/Товары.xml")])

    def test_upsert_file_in_legacy_is_unchanged(self):
        out = self.call("metadata_upsert_file", "Catalogs/Товары.xml")
        self.assertEqual(out, {"status": "reindexed"})

    def test_stats_block_legacy(self):
        b = wt.source_stats_block()
        self.assertEqual(b["mode"], "legacy")
        self.assertIn("apply_changes", b["hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
