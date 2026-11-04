"""
Тесты SRC-10 — индексер в режиме git: догнать, а не пересобрать.
================================================================

Проверяется решение, которое индексер принимает при старте, и запись
состояния после прогона. Фазы индексации (`_run_phases`) подменены записью
вызова: их тело не менялось и покрыто tests_indexer_phases.py. Хранилище
состояния — MemoryStore, граф — RecordingOps, проверка владения — заглушка.

| Состояние                          | Ожидаемое решение                     |
|------------------------------------|---------------------------------------|
| выгрузка не в git                  | прежний путь (None)                   |
| состояния нет                      | прежний путь по отпечаткам + запись   |
| HEAD = проиндексированный          | выход без фаз                         |
| HEAD ушёл вперёд, ≤ порога         | доиндексация, выход без фаз           |
| больше порога / форс               | полная ('full')                       |
| владение не в порядке              | только фаза 2 ('phase2')              |
| слоя метаданных нет                | полная                                |
| аренда занята дольше ожидания      | выход с кодом 1                       |

Нужен git. Без него набор пропускается целиком (CI-5: `ПУСТО`).

Запуск:
    python tests_indexer_git.py -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import indexer
import source_state
import source_sync
from source_state import MemoryStore
from tests_git_source import sh, write
from tests_source_sync import RecordingOps

HAVE_GIT = shutil.which("git") is not None

ENV = ("METADATA_SRC_DIR", "METADATA_REPO_DIR", "SOURCE_MODE",
       "METADATA_MAX_PARTIAL", "SOURCE_INDEXER_WAIT_SEC", "NEO4J_PASSWORD",
       "METADATA_FORCE_REINDEX")


def ownership(state="ok", objects=10):
    return {"state": state, "objects": objects, "message": state,
            "callables": 1, "modules": 1, "has_method": 1}


class FakeNeo:
    def wait(self, timeout=0):
        pass


@unittest.skipUnless(HAVE_GIT, "git не установлен — набор не проверяет ничего")
class IndexerGitCase(unittest.TestCase):

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
        self.c1 = self.commit("первый")
        os.environ.update({"METADATA_SRC_DIR": str(self.cfg),
                           "METADATA_REPO_DIR": str(self.root),
                           "SOURCE_MODE": "auto",
                           "SOURCE_INDEXER_WAIT_SEC": "0",
                           "NEO4J_PASSWORD": "x"})
        os.environ.pop("METADATA_MAX_PARTIAL", None)
        os.environ.pop("METADATA_FORCE_REINDEX", None)

        self.store = MemoryStore()
        self.ops = RecordingOps()
        self.own = ownership()
        self.patches = [
            (source_state, "Neo4jStore", lambda neo, key="main": self.store),
            (source_sync, "Neo4jGraphOps", lambda neo, root: self.ops),
            (indexer, "check_ownership", lambda neo: self.own),
            (indexer, "log_ownership", lambda o, lg: None),
        ]
        self.saved = [(m, n, getattr(m, n)) for m, n, _ in self.patches]
        for m, n, v in self.patches:
            setattr(m, n, v)
        indexer._PHASES_RAN[:] = []

    def tearDown(self):
        while indexer._STOP_BEAT:          # пульс FIX-39 не должен течь между тестами
            indexer._STOP_BEAT.pop()()
        for m, n, v in self.saved:
            setattr(m, n, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def commit(self, msg: str) -> str:
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "--allow-empty", "-m", msg)
        return sh(self.root, "rev-parse", "HEAD").strip()

    def adopt(self, sha=None):
        ctx = source_sync.context_from_env()
        self.store.acquire("indexer")
        source_sync.mark_indexed(ctx, self.store, "indexer", sha or self.c1,
                                 full=True)
        self.store.release("indexer")


class TestDecision(IndexerGitCase):

    def test_legacy(self):
        os.environ["SOURCE_MODE"] = "legacy"
        self.assertIsNone(indexer._git_start(FakeNeo(), forced=False))

    def test_forced_git_without_repo_exits(self):
        os.environ["SOURCE_MODE"] = "git"
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        os.environ["METADATA_SRC_DIR"] = os.environ["METADATA_REPO_DIR"] = str(plain)
        with self.assertRaises(SystemExit):
            indexer._git_start(FakeNeo(), forced=False)

    def test_no_state_goes_the_old_way_and_records_state(self):
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertIsNone(run.plan)
        self.assertTrue(run.mark)
        self.assertEqual(self.store.lock_owner, indexer.INDEXER_OWNER)
        # отпечатки сошлись, фазы не гонялись — граф принят как есть
        indexer._git_finish(FakeNeo(), run, 0)
        st = self.store.read()
        self.assertEqual(st.indexed_sha, self.c1)
        self.assertTrue(st.needs_reconcile)
        self.assertEqual(st.last_full_at, 0)
        self.assertEqual(self.store.lock_owner, "")

    def test_no_state_with_full_run_marks_full(self):
        run = indexer._git_start(FakeNeo(), forced=False)
        indexer._PHASES_RAN[:] = ["XML", "BSL"]
        indexer._git_finish(FakeNeo(), run, 0)
        self.assertGreater(self.store.read().last_full_at, 0)

    def test_in_sync_exits_without_phases(self):
        self.adopt()
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertTrue(run.done)
        self.assertEqual(run.rc, 0)
        self.assertEqual(self.ops.calls, [])

    def test_catch_up_by_git(self):
        self.adopt()
        write(self.cfg, "Documents/Заказ.xml", "<doc v='2'/>\n")
        c2 = self.commit("pull")
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertTrue(run.done, run.plan)
        self.assertEqual(self.ops.calls, [("upsert", "Documents/Заказ.xml")])
        self.assertEqual(self.store.read().indexed_sha, c2)
        indexer._git_finish(FakeNeo(), run, run.rc)
        self.assertEqual(self.store.lock_owner, "")

    def test_over_threshold_is_full(self):
        self.adopt()
        os.environ["METADATA_MAX_PARTIAL"] = "1"
        write(self.cfg, "Documents/Заказ.xml", "<doc v='3'/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='3'/>\n")
        self.commit("pull")
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertEqual(run.plan, "full")
        self.assertTrue(run.mark)

    def test_forced(self):
        self.adopt()
        run = indexer._git_start(FakeNeo(), forced=True)
        self.assertEqual(run.plan, "full")

    def test_ownership_broken_is_phase2(self):
        self.adopt()
        self.own = ownership(state="broken")
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertEqual(run.plan, "phase2")
        self.assertTrue(run.flag_reconcile)

    def test_empty_metadata_layer_is_full(self):
        self.adopt()
        self.own = ownership(state="broken", objects=0)
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertEqual(run.plan, "full")

    def test_git_failure_after_lease_releases_it(self):
        """
        Сбой git сразу после взятия аренды не должен оставить граф занятым
        до конца аренды (INDEXER_LEASE_SEC), и пульс тоже снимается.
        """
        from git_source import GitError
        saved = indexer._git_decide

        def broken(*a, **k):
            raise GitError("репозиторий недоступен")
        indexer._git_decide = broken
        try:
            with self.assertRaises(GitError):
                indexer._git_start(FakeNeo(), forced=False)
        finally:
            indexer._git_decide = saved
        self.assertEqual(self.store.lock_owner, "", "аренда осталась висеть")
        self.assertEqual(indexer._STOP_BEAT, [], "пульс остался")

    def test_busy_beyond_wait_exits(self):
        self.store.acquire("watcher", ttl=3600)
        with self.assertRaises(SystemExit):
            indexer._git_start(FakeNeo(), forced=False)
        self.assertEqual(self.store.lock_owner, "watcher")


class TestBslPathsFromGit(IndexerGitCase):
    """PERF-14: список .bsl для фазы 2 — индекс git плюс правки агента."""

    def test_index_plus_dirty(self):
        write(self.cfg, "CommonModules/М/Ext/Module.bsl", "// в git\n")
        self.commit("модуль")
        write(self.cfg, "CommonModules/Новый/Ext/Module.bsl", "// агент, не в git\n")
        self.adopt()
        st = self.store.read()
        st.dirty = {"CommonModules/Новый/Ext/Module.bsl": "h",
                    "CommonModules/Удалён/Ext/Module.bsl": "deleted",
                    "Catalogs/Товары.xml": "h"}
        self.store.state = st
        run = indexer._git_start(FakeNeo(), forced=True)
        try:
            paths = indexer._git_bsl_paths(run)
        finally:
            indexer._git_finish(FakeNeo(), run, 1)
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(set(paths), {"CommonModules/Новый/Ext/Module.bsl",
                                      "CommonModules/М/Ext/Module.bsl"})


class TestFinish(IndexerGitCase):

    def test_failed_run_records_nothing(self):
        run = indexer._git_start(FakeNeo(), forced=True)
        indexer._git_finish(FakeNeo(), run, 3)
        self.assertIsNone(self.store.read())
        self.assertEqual(self.store.lock_owner, "")

    def test_broken_graph_records_nothing(self):
        run = indexer._git_start(FakeNeo(), forced=True)
        self.own = ownership(state="broken")
        indexer._git_finish(FakeNeo(), run, 0)
        self.assertIsNone(self.store.read())

    def test_head_is_taken_before_the_run(self):
        """
        Полная индексация идёт часы. Если за это время пришёл pull, в
        состоянии должен остаться HEAD на момент СТАРТА — следующий такт
        догонит разницу. HEAD на момент конца означал бы «учтено», а это
        неправда.
        """
        run = indexer._git_start(FakeNeo(), forced=True)
        write(self.cfg, "Documents/Заказ.xml", "<doc v='4'/>\n")
        self.commit("pull во время индексации")
        indexer._PHASES_RAN[:] = ["XML", "BSL"]
        indexer._git_finish(FakeNeo(), run, 0)
        self.assertEqual(self.store.read().indexed_sha, self.c1)

    def test_phase2_sets_reconcile_flag(self):
        self.adopt()
        st = self.store.read()
        st.needs_reconcile = False
        self.store.state = st
        self.own = ownership(state="broken")
        run = indexer._git_start(FakeNeo(), forced=False)
        self.own = ownership()
        indexer._git_finish(FakeNeo(), run, 0)
        self.assertTrue(self.store.read().needs_reconcile)


class TestMainWiring(IndexerGitCase):
    """`_main` передаёт решение в фазы и не зовёт их, когда догнал по git."""

    def setUp(self):
        super().setUp()
        self.phases = []

        def fake_phases(neo, src_dir, cfg_name, **kw):
            self.phases.append(kw["git_plan"])
            indexer._PHASES_RAN[:] = ["XML", "BSL"]
            return 0
        self.saved += [(indexer, "_run_phases", indexer._run_phases),
                       (indexer, "Neo4j", indexer.Neo4j)]
        indexer._run_phases = fake_phases
        indexer.Neo4j = lambda *a, **k: FakeNeo()

    def test_forced_full(self):
        os.environ["METADATA_FORCE_REINDEX"] = "true"
        self.assertEqual(indexer._main(), 0)
        self.assertEqual(self.phases, ["full"])
        self.assertEqual(self.store.read().indexed_sha, self.c1)

    def test_in_sync_skips_phases(self):
        self.adopt()
        self.assertEqual(indexer._main(), 0)
        self.assertEqual(self.phases, [])

    def test_first_run_uses_fingerprints(self):
        self.assertEqual(indexer._main(), 0)
        self.assertEqual(self.phases, [None])
        self.assertIsNotNone(self.store.read())


class TestIndexerLeaseFix39(IndexerGitCase):
    """
    FIX-39. Боевой случай 3 ноября: `docker compose up -d` поднял служебный
    metadata-indexer, пока шла полная индексация из graph-refresh.ps1 -Full,
    и оба писали в граф: у всех индексеров было одно имя владельца
    `indexer`, а аренда своему владельцу выдаётся повторно.
    """

    def test_owner_is_per_process(self):
        import socket
        self.assertNotEqual(indexer.INDEXER_OWNER, "indexer")
        self.assertIn(socket.gethostname(), indexer.INDEXER_OWNER)
        self.assertTrue(indexer.INDEXER_OWNER.endswith(f":{os.getpid()}"))

    def test_second_indexer_waits_for_first(self):
        # Чужой индексер (другой контейнер) держит аренду — этот не входит.
        self.store.acquire("indexer:другой-контейнер:1", ttl=3600)
        with self.assertRaises(SystemExit):
            indexer._git_start(FakeNeo(), forced=True)
        self.assertEqual(self.store.lock_owner, "indexer:другой-контейнер:1")

    def test_stale_lease_of_dead_indexer_expires(self):
        # Упавший индексер: его аренда истекла — новый берёт граф сам,
        # без --force-unlock.
        self.store.acquire("indexer:упавший:7", ttl=-1)
        run = indexer._git_start(FakeNeo(), forced=True)
        self.assertEqual(self.store.lock_owner, indexer.INDEXER_OWNER)
        indexer._git_finish(FakeNeo(), run, 1)

    def test_heartbeat_renews_and_stops(self):
        import progress_log
        from source_state import INDEXER_LEASE_SEC
        run = indexer._git_start(FakeNeo(), forced=True)
        self.store.lock_until = time.time() + 5          # почти истекла
        progress_log.beat(time.monotonic() + 3600)        # строка прогресса через час
        self.assertGreater(self.store.lock_until, time.time() + INDEXER_LEASE_SEC - 60)
        indexer._git_finish(FakeNeo(), run, 0)
        self.assertEqual(self.store.lock_owner, "")
        self.assertEqual(indexer._STOP_BEAT, [])
        n = len(progress_log._BEATS)
        progress_log.beat(time.monotonic() + 7200)
        self.assertEqual(self.store.lock_owner, "", "после финиша пульс не берёт аренду снова")
        self.assertEqual(len(progress_log._BEATS), n)

    def test_heartbeat_reports_hijack(self):
        import progress_log
        run = indexer._git_start(FakeNeo(), forced=True)
        self.store.lock_owner, self.store.lock_until = "watcher", time.time() + 600
        with self.assertLogs(indexer.log, "ERROR") as cm:
            progress_log.beat(time.monotonic() + 3600)
        self.assertIn("перехватил", "\n".join(cm.output))
        self.assertEqual(self.store.lock_owner, "watcher", "чужую аренду не отбираем")
        indexer._git_finish(FakeNeo(), run, 1)
        self.assertEqual(self.store.lock_owner, "watcher", "чужую аренду не отпускаем")


class TestEmptyGraphGoesFullFix43(IndexerGitCase):
    """FIX-43. Чистый подъём стека 4 ноября: состояния нет, граф пуст — а
    индексер 12 минут считал отпечатки выгрузки, чтобы прийти к той же
    полной индексации."""

    def test_empty_graph_no_state_is_full_without_fingerprints(self):
        self.own = ownership(state="empty", objects=0)
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertEqual(run.plan, "full")
        self.assertTrue(run.mark)
        indexer._git_finish(FakeNeo(), run, 1)

    def test_non_empty_graph_without_state_still_checks_fingerprints(self):
        run = indexer._git_start(FakeNeo(), forced=False)
        self.assertIsNone(run.plan, "граф не пуст — прежний путь, по отпечаткам")
        indexer._git_finish(FakeNeo(), run, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
