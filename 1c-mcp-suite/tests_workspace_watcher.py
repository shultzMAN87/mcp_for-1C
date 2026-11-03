"""
Тесты SRC-9 — настройки и расписание watcher'а в режиме git.
============================================================

Сам цикл watcher'а — это `source_sync.sync()` по таймеру; его логику
проверяет tests_source_sync.py. Здесь — то, что живёт только в watcher'е:

  • SOURCE_POLL_SEC: 30…3600, вне диапазона — ошибка, а не молчаливая
    подстановка (урок WATCHER_POLLING_INTERVAL);
  • расписание сверки («ЧЧ:ММ») и плановой полной индексации («день ЧЧ:ММ»):
    разбор и «пора ли» — с учётом пропущенного запуска (машина была
    выключена) и окна, после которого догонять уже поздно.

Запуск:
    python tests_workspace_watcher.py -v
"""
from __future__ import annotations

import os
import time
import unittest

try:
    import workspace_watcher as ww
except ImportError as e:   # нет пакетов mcp / watchdog на машине прогона
    ww = None
    _WHY = str(e)
else:
    _WHY = ""


def ts(y, mo, d, h, mi) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


@unittest.skipIf(ww is None, f"workspace_watcher не импортируется: {_WHY}")
class TestPollSec(unittest.TestCase):

    def test_valid(self):
        self.assertEqual(ww.parse_poll_sec("60"), 60)
        self.assertEqual(ww.parse_poll_sec("300"), 300)
        self.assertEqual(ww.parse_poll_sec(None), 60)
        self.assertEqual(ww.parse_poll_sec(" 30 "), 30)
        self.assertEqual(ww.parse_poll_sec("3600"), 3600)

    def test_out_of_range_is_an_error(self):
        for raw in ("5", "29", "3601", "0", "-60"):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as cm:
                ww.parse_poll_sec(raw)
            self.assertIn("30", str(cm.exception))

    def test_garbage(self):
        with self.assertRaises(ValueError):
            ww.parse_poll_sec("минута")


@unittest.skipIf(ww is None, f"workspace_watcher не импортируется: {_WHY}")
class TestSchedule(unittest.TestCase):

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_parse(self):
        self.assertIsNone(ww.parse_schedule("", weekly=False))
        self.assertIsNone(ww.parse_schedule(None, weekly=True))
        self.assertEqual(ww.parse_schedule("03:00", weekly=False), (None, 3, 0))
        self.assertEqual(ww.parse_schedule("sat 02:30", weekly=True), (5, 2, 30))
        self.assertEqual(ww.parse_schedule("Сб 02:30", weekly=True), (5, 2, 30))
        self.assertEqual(ww.parse_schedule("пн 23:59", weekly=False), (0, 23, 59))

    def test_parse_errors(self):
        for raw, weekly in (("02:00", True), ("суббота 02:00", True),
                            ("25:00", False), ("3 часа", False),
                            ("sat 2", True), ("a b c", False)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ww.parse_schedule(raw, weekly=weekly)

    def test_daily(self):
        spec = (None, 3, 0)
        # 2026-11-03 — вторник
        before = ts(2026, 11, 3, 2, 59)
        after = ts(2026, 11, 3, 3, 1)
        self.assertEqual(ww.last_slot(spec, after), ts(2026, 11, 3, 3, 0))
        self.assertEqual(ww.last_slot(spec, before), ts(2026, 11, 2, 3, 0))
        # в 03:01 пора, если сегодня не запускали
        self.assertTrue(ww.is_due(spec, after, last_run=ts(2026, 11, 2, 3, 5)))
        # уже запускали после 03:00 — не пора
        self.assertFalse(ww.is_due(spec, after, last_run=ts(2026, 11, 3, 3, 0)))

    def test_missed_run_is_caught_up_within_grace(self):
        spec = (None, 3, 0)
        last = ts(2026, 11, 2, 3, 1)
        # машину включили в 09:00 — догоняем
        self.assertTrue(ww.is_due(spec, ts(2026, 11, 3, 9, 0), last))
        # включили в 16:00 — 13 часов спустя, поздно: ждём следующей ночи
        self.assertFalse(ww.is_due(spec, ts(2026, 11, 3, 16, 0), last))

    def test_weekly(self):
        spec = (5, 2, 0)            # суббота 02:00
        sat = ts(2026, 11, 7, 2, 30)
        self.assertEqual(ww.last_slot(spec, sat), ts(2026, 11, 7, 2, 0))
        self.assertTrue(ww.is_due(spec, sat, last_run=0))
        self.assertFalse(ww.is_due(spec, sat, last_run=ts(2026, 11, 7, 2, 10)))
        tue = ts(2026, 11, 10, 12, 0)
        self.assertEqual(ww.last_slot(spec, tue), ts(2026, 11, 7, 2, 0))
        self.assertFalse(ww.is_due(spec, tue, last_run=0), "окно догона прошло")

    def test_disabled(self):
        self.assertFalse(ww.is_due(None, time.time(), 0))


@unittest.skipIf(ww is None, f"workspace_watcher не импортируется: {_WHY}")
class TestQuiet(unittest.TestCase):

    def test_repeats_are_suppressed(self):
        said = []
        orig = ww._log
        ww._log = said.append
        try:
            q = ww._Quiet(period=3600)
            q.say("k", "busy: indexer")
            q.say("k", "busy: indexer")
            q.say("k", "busy: refresh")
        finally:
            ww._log = orig
        self.assertEqual(said, ["busy: indexer", "busy: refresh"])


@unittest.skipIf(ww is None, f"workspace_watcher не импортируется: {_WHY}")
class TestStartup(unittest.TestCase):
    """
    Ошибка конфигурации — выход с кодом 78 ДО обращения к Neo4j: контейнер
    не делает вид, что работает.
    """

    def setUp(self):
        import shutil
        import sys
        import tempfile
        from pathlib import Path
        if shutil.which("git") is None:
            self.skipTest("git не установлен")
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here / "mcp-metadata-graph-neo4j"))
        from tests_git_source import sh, write
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        write(root, "src/Configuration.xml", "<c/>\n")
        sh(root, "init", "-q")
        sh(root, "add", "-A")
        sh(root, "commit", "-q", "-m", "x")
        self.saved = {k: os.environ.get(k) for k in (
            "METADATA_SRC_DIR", "METADATA_REPO_DIR", "SOURCE_POLL_SEC",
            "SOURCE_MODE", "NEO4J_URL")}
        os.environ.update({"METADATA_SRC_DIR": str(root / "src"),
                           "METADATA_REPO_DIR": str(root),
                           "SOURCE_MODE": "auto",
                           "NEO4J_URL": "http://127.0.0.1:9"})
        self.attrs = (ww.WATCHER_ENABLED, ww.INITIAL_DELAY_SEC, ww.WATCHER_MODE)
        ww.WATCHER_ENABLED, ww.INITIAL_DELAY_SEC, ww.WATCHER_MODE = True, 0, "git"

    def tearDown(self):
        ww.WATCHER_ENABLED, ww.INITIAL_DELAY_SEC, ww.WATCHER_MODE = self.attrs
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _quiet_main(self) -> int:
        orig = ww._log
        ww._log = lambda msg: None
        try:
            return ww.main()
        finally:
            ww._log = orig

    def test_bad_interval_exits_78(self):
        os.environ["SOURCE_POLL_SEC"] = "5"
        self.assertEqual(self._quiet_main(), ww.EX_CONFIG)

    def test_bad_schedule_exits_78(self):
        os.environ["SOURCE_POLL_SEC"] = "60"
        os.environ["SOURCE_FULL_REINDEX_AT"] = "02:00"   # без дня недели
        try:
            self.assertEqual(self._quiet_main(), ww.EX_CONFIG)
        finally:
            os.environ.pop("SOURCE_FULL_REINDEX_AT", None)

    def test_bad_mode_exits_78(self):
        ww.WATCHER_MODE = "inotify"
        self.assertEqual(self._quiet_main(), ww.EX_CONFIG)

    def test_git_mode_without_repo_exits_78(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["METADATA_SRC_DIR"] = d
            os.environ["METADATA_REPO_DIR"] = d
            self.assertEqual(self._quiet_main(), ww.EX_CONFIG)


@unittest.skipIf(ww is None, f"workspace_watcher не импортируется: {_WHY}")
class TestFullRetryGuard(unittest.TestCase):
    """
    Упавшая полная индексация не пишет last_full_at. Без отметки попытки
    watcher перезапускал бы её на каждом такте — по два часа подряд.
    """

    def test_failed_full_is_not_retried_every_tick(self):
        import sys
        import tempfile
        from pathlib import Path
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here / "mcp-metadata-graph-neo4j"))
        import graph_writer
        import source_state
        import source_sync
        from source_state import MemoryStore, SourceState

        store = MemoryStore()
        store.acquire("t")
        store.write(SourceState(indexed_sha="x", needs_full=True,
                                needs_full_reason="тест"), "t")
        store.release("t")

        class Neo:
            def __init__(self, *a, **k): pass
            def wait(self, timeout=0): pass

        runs, ticks = [], []

        def fake_sync(*a, **k):
            ticks.append(1)
            if len(ticks) >= 3:
                raise KeyboardInterrupt
            return source_sync.SyncResult("needs_full", "тест")

        saved = [(graph_writer, "Neo4j", graph_writer.Neo4j),
                 (source_state, "Neo4jStore", source_state.Neo4jStore),
                 (source_sync, "sync", source_sync.sync),
                 (source_sync, "Neo4jGraphOps", source_sync.Neo4jGraphOps),
                 (ww, "_run_full_index", ww._run_full_index),
                 (ww, "_log", ww._log), (ww.time, "sleep", ww.time.sleep)]
        graph_writer.Neo4j = Neo
        source_state.Neo4jStore = lambda neo: store
        source_sync.sync = fake_sync
        source_sync.Neo4jGraphOps = lambda neo, root: None
        def failing_full(why):
            runs.append(why)
            if len(runs) > 1:          # без сторожа цикл крутил бы её вечно
                raise KeyboardInterrupt
            return 1                   # полная индексация упала
        ww._run_full_index = failing_full
        ww._log = lambda msg: None
        ww.time.sleep = lambda s: None
        env = {k: os.environ.get(k) for k in ("SOURCE_AUTO_FULL", "SOURCE_POLL_SEC")}
        os.environ.update(SOURCE_AUTO_FULL="true", SOURCE_POLL_SEC="60")
        try:
            ctx = source_sync.SourceContext("git", "тест", Path(tempfile.gettempdir()),
                                            Path(tempfile.gettempdir()))
            with self.assertRaises(KeyboardInterrupt):
                ww._main_git(ctx)
        finally:
            for m, n, v in saved:
                setattr(m, n, v)
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(len(runs), 1, "полную перезапустили без паузы")
        self.assertGreaterEqual(len(ticks), 3)


class TestEnvReachesContainers(unittest.TestCase):
    """
    Сторож урока WATCHER_POLLING_INTERVAL: README советовал переменную, а
    docker-compose.yml её в контейнер не передавал — опрос шёл впятеро
    чаще обещанного, без единого слова. Каждая настройка watcher'а и
    источника, объявленная в .env.example, обязана подставляться в
    docker-compose.yml.
    """

    def test_every_watcher_and_source_setting_is_wired(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        env = (root / ".env.example").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        declared = re.findall(
            r"^((?:WATCHER_|SOURCE_|CONFIG_SUBDIR|TZ\b)[A-Z_]*)=", env, re.M)
        self.assertIn("SOURCE_POLL_SEC", declared, "разбор .env.example сломан")
        missing = [v for v in declared if "${" + v not in compose]
        self.assertFalse(missing, f"в .env.example есть, в контейнер не "
                                  f"передаётся: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
