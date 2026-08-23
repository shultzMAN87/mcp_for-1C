"""
EVAL-7. Журнал вызовов и его разбор.
=====================================

Что проверяется и почему именно это
────────────────────────────────────
Журнал заводится ради одной фразы из чек-листа: «один вызов
`explain_diagnostics` со всем списком кодов» против «пять вызовов
подряд». Значит, главная проверка здесь — что читатель различает эти два
случая, и что строка чужого сервера читается наравне со своей.

Формат строки повторяет чужой (`ts`, `tool`), и это не стилистика: если
бы форматы разошлись, читателю пришлось бы знать, кто писал строку, а
`v8std` мы не правим и подстроить его под себя не можем.

Рычаг проверяется отдельно (`TestLeversAreWired`) — по тому же уроку, что
`FIX-30` и `CFG-4.1`: механизм, описанный в документации и не
пробрасываемый в контейнер, выглядит работающим ровно до первой попытки
им воспользоваться.

Запуск:  python3 scripts/tests_journal_report.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "1c-mcp-suite"))

import journal_report  # noqa: E402
import tool_journal  # noqa: E402

T0 = datetime(2026, 8, 23, 9, 0, 0, tzinfo=timezone.utc)


def _ours(directory: Path, entries) -> None:
    """Строки, как их пишет наш `tool_journal`."""
    with (directory / "calls.jsonl").open("w", encoding="utf-8") as fh:
        for server, tool, shift in entries:
            fh.write(tool_journal.line_for(
                server, tool, (T0 + timedelta(seconds=shift)).timestamp()) + "\n")


def _foreign(directory: Path, entries) -> None:
    """Строки, как их пишет чужой сервер (`McpToolUsageLogger.record`)."""
    with (directory / "v8std.jsonl").open("w", encoding="utf-8") as fh:
        for tool, shift in entries:
            fh.write(json.dumps({
                "ts": (T0 + timedelta(seconds=shift))
                      .replace(microsecond=0).isoformat(),
                "tool": tool,
                "system": "cursor",
            }, ensure_ascii=False, sort_keys=True) + "\n")


class TestBothHalvesAreRead(unittest.TestCase):

    def test_our_line_and_the_foreign_one_land_in_one_list(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _ours(d, [("bsl-checker", "bsl_check_code", 1)])
            _foreign(d, [("v8std_explain_diagnostics", 2)])
            calls, complaints = journal_report.read_journal(d)
        self.assertEqual([c["tool"] for c in calls],
                         ["bsl_check_code", "v8std_explain_diagnostics"])
        self.assertEqual(journal_report.servers_seen(calls),
                         ["bsl-checker", "v8std"])
        self.assertEqual(complaints, [])

    def test_server_of_a_foreign_line_comes_from_the_tool_name(self):
        """У чужой строки поля `server` нет — сервер выводится по имени."""
        self.assertEqual(
            journal_report.server_of({"tool": "v8std_get_page"}), "v8std")
        self.assertEqual(
            journal_report.server_of({"tool": "code_callers"}),
            "metadata-graph")
        # Своё поле важнее догадки: сервер может переехать, префикс — нет.
        self.assertEqual(
            journal_report.server_of({"tool": "v8std_get_page",
                                      "server": "иной"}), "иной")

    def test_broken_line_is_named_out_loud(self):
        """
        Журнал, тихо потерявший строки, хуже отсутствующего: по нему
        сделают вывод «инструмент не звали».
        """
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "calls.jsonl").write_text("не json\n", encoding="utf-8")
            calls, complaints = journal_report.read_journal(d)
        self.assertEqual(calls, [])
        self.assertTrue(complaints)


class TestTheQuestionTheJournalWasMadeFor(unittest.TestCase):
    """
    Один вызов со всем списком против пяти подряд. Ради этой пары журнал
    и заводился: в тексте ответа они неразличимы.
    """

    def _tally(self, entries) -> str:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _foreign(d, entries)
            calls, _ = journal_report.read_journal(d)
        return journal_report.tally(calls)

    def test_one_call_with_the_whole_list(self):
        self.assertEqual(self._tally([("v8std_explain_diagnostics", 1)]),
                         "v8std_explain_diagnostics ×1")

    def test_five_calls_in_a_row(self):
        self.assertEqual(
            self._tally([("v8std_explain_diagnostics", i) for i in range(5)]),
            "v8std_explain_diagnostics ×5")


class TestScenarioWindows(unittest.TestCase):

    def _windows(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _ours(d, [
                ("metadata-graph", "metadata_search", 5),    # до меток
                ("metadata-graph", "metadata_search", 65),   # сценарий 7
                ("bsl-checker", "bsl_check_code", 70),       # сценарий 7
                ("v8std", "v8std_explain_diagnostics", 130),  # сценарий 8
            ])
            (d / journal_report.MARKS_NAME).write_text(
                json.dumps({"ts": (T0 + timedelta(seconds=60)).isoformat(),
                            "name": "7"}, ensure_ascii=False) + "\n" +
                json.dumps({"ts": (T0 + timedelta(seconds=120)).isoformat(),
                            "name": "8"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            calls, _ = journal_report.read_journal(d)
            marks = journal_report.read_marks(d)
            return journal_report.split_by_marks(calls, marks)

    def test_calls_land_in_the_scenario_they_belong_to(self):
        windows = self._windows()
        by_name = {w["name"]: journal_report.tally(w["calls"]) for w in windows}
        self.assertEqual(by_name["7"],
                         "bsl_check_code ×1, metadata_search ×1")
        self.assertEqual(by_name["8"], "v8std_explain_diagnostics ×1")

    def test_calls_before_the_first_mark_are_not_lost(self):
        """
        Прогрев и проверка стенда — тоже вызовы. Молча выбросив их, мы бы
        получили итог, который не сходится с числом строк в журнале, и
        разбираться с этим пришлось бы посреди прогона.
        """
        windows = self._windows()
        names = [w["name"] for w in windows]
        self.assertEqual(names[0], "до первой метки")
        total = sum(len(w["calls"]) for w in windows)
        self.assertEqual(total, 4)

    def test_without_marks_everything_is_one_window(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _ours(d, [("metadata-graph", "metadata_search", 1)])
            calls, _ = journal_report.read_journal(d)
            windows = journal_report.split_by_marks(calls, [])
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["name"], "весь журнал")

    def test_mark_is_appended_and_read_back(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            journal_report.add_mark(d, "7")
            journal_report.add_mark(d, "8")
            marks = journal_report.read_marks(d)
        self.assertEqual([m["name"] for m in marks], ["7", "8"])


class TestOnButIdleIsNotAFailure(unittest.TestCase):
    """
    Прогон 23 августа: журнал включён, серверы перезапущены, агента ещё не
    спрашивали — и отчёт напечатал «Вызовов нет» с тремя шагами починки.
    Чинить было нечего. Норма, притворившаяся отказом, — тот же дефект, с
    которым борются `FIX-3` и `OBS-1`, только вывернутый наизнанку.

    Различить два состояния можно только по следу, который сервер
    оставляет при старте: до первого вызова файла журнала не существует
    вовсе, и «выключен» от «включён, но простаивает» неотличимо.
    """

    def _run(self, td: str):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = journal_report.main(["--dir", td])
        return rc, buf.getvalue()

    def test_started_but_no_calls_yet(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            os.environ["MCP_TOOL_JOURNAL"] = str(Path(td) / "calls.jsonl")
            try:
                tool_journal.note_start("bsl-checker", 4)
                tool_journal.note_start("metadata-graph", 29)
            finally:
                os.environ.pop("MCP_TOOL_JOURNAL", None)
            rc, out = self._run(td)
        self.assertEqual(rc, 0, out)
        self.assertIn("вызовов пока нет", out)
        self.assertIn("bsl-checker", out)
        # Никаких шагов починки: чинить нечего.
        self.assertNotIn("force-recreate", out)

    def test_start_line_is_not_counted_as_a_call(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            os.environ["MCP_TOOL_JOURNAL"] = str(Path(td) / "calls.jsonl")
            try:
                tool_journal.note_start("bsl-checker", 4)
                tool_journal.record("bsl-checker", "bsl_check_code")
            finally:
                os.environ.pop("MCP_TOOL_JOURNAL", None)
            calls, complaints = journal_report.read_journal(Path(td))
        self.assertEqual([c["tool"] for c in calls], ["bsl_check_code"])
        # И не жалобой: строка старта законна, а не «нет имени инструмента».
        self.assertEqual(complaints, [])


class TestEmptyJournalExplainsItself(unittest.TestCase):
    """
    Пустой журнал и выключенный журнал — разные вещи, а выглядят
    одинаково. Разбираться с этим придётся посреди прогона, поэтому
    скрипт обязан сказать, что именно проверить.
    """

    def test_says_what_to_check_and_returns_nonzero(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = journal_report.main(["--dir", td])
            out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("MCP_TOOL_JOURNAL", out)
        self.assertIn("force-recreate", out)
        # Здесь шаги починки уместны: следов старта нет ни одного, значит
        # серверы журнал не ведут.
        self.assertNotIn("вызовов пока нет", out)


class TestJournalWritesNothingWhenOff(unittest.TestCase):
    """
    Выключенный журнал — умолчание. Иначе первый же прогон агента начал бы
    писать файл, о котором никто не просил.
    """

    def test_off_by_default(self):
        import os
        saved = os.environ.pop("MCP_TOOL_JOURNAL", None)
        try:
            self.assertEqual(tool_journal.journal_path(), "")
            self.assertFalse(tool_journal.record("bsl-checker", "bsl_stats"))
        finally:
            if saved is not None:
                os.environ["MCP_TOOL_JOURNAL"] = saved

    def test_writes_a_readable_line_when_on(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "calls.jsonl"
            os.environ["MCP_TOOL_JOURNAL"] = str(path)
            try:
                self.assertTrue(
                    tool_journal.record("bsl-checker", "bsl_check_code", ms=12))
            finally:
                os.environ.pop("MCP_TOOL_JOURNAL", None)
            calls, complaints = journal_report.read_journal(path.parent)
        self.assertEqual(complaints, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "bsl_check_code")

    def test_a_failed_call_is_still_a_call(self):
        line = json.loads(tool_journal.line_for(
            "bsl-checker", "bsl_check_code", T0.timestamp(), ms=5, ok=False))
        self.assertIs(line["ok"], False)
        # Успешный вызов поля не несёт: журнал читают глазами, и лишнее
        # поле в каждой строке мешает видеть редкое.
        self.assertNotIn("ok", json.loads(tool_journal.line_for(
            "bsl-checker", "bsl_check_code", T0.timestamp())))


class TestLeversAreWired(unittest.TestCase):
    """
    `FIX-30` и `CFG-4.1` случились одинаково: рычаг описан, рычага нет.
    Проверяем текстом то, что уходит наружу, — как в
    `tests_graph_contract.py`.
    """

    def setUp(self):
        self.compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def test_every_own_server_gets_the_journal_variable(self):
        # Четыре своих сервера: metadata-graph, bsl-checker, platform-help,
        # query-builder. Пятый — чужой, у него свой ключ (ниже).
        self.assertEqual(
            self.compose.count("- MCP_TOOL_JOURNAL=${MCP_TOOL_JOURNAL:-}"), 4,
            "переменная журнала доезжает не до всех своих серверов",
        )

    def test_foreign_server_gets_its_own_key(self):
        self.assertIn("- V8STD_USAGE_LOG=${V8STD_USAGE_LOG:-}", self.compose)
        entry = (ROOT / "1c-mcp-suite" / "v8std_entrypoint.py").read_text(
            encoding="utf-8")
        self.assertIn('"--usage-log"', entry,
                      "ключ журнала чужому серверу не передаётся")

    def test_journal_directory_is_mounted_into_all_five(self):
        self.assertEqual(self.compose.count("- ./evals/journal:/journal"), 5,
                         "каталог журнала смонтирован не во все пять серверов")

    def test_bsl_checker_also_counts_its_tools(self):
        """
        TOOL-1 ставился в `start.py`, а у bsl-checker своя точка входа —
        то есть счётчик там не стоял ни разу, и `usage` в `bsl_stats` был
        пуст всегда. Нашлось при разборе EVAL-7 и починено рядом.
        """
        server = (ROOT / "1c-mcp-suite" / "mcp-bsl-checker"
                  / "server.py").read_text(encoding="utf-8")
        self.assertIn("wrap_registered_tools(mcp)", server)
        self.assertIn("install_journal(mcp, 'bsl-checker')", server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
