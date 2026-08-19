"""
TOOL-1. Тесты счётчика вызовов инструментов.
=============================================

Главный риск этой правки не в арифметике, а в обёртке. Она встаёт на
регистрацию инструмента, а FastMCP по функции строит схему: имя, описание,
типы параметров. Обёртка, потерявшая сигнатуру, не сломала бы ни одного
теста — она сломала бы список инструментов у агента, то есть ровно то, ради
чего TOOL-1 и затевался.

Поэтому здесь две группы проверок: что считается правильно и что после
обёртывания инструмент выглядит для регистрации по-прежнему.

Запуск:  python3 tests_tool_usage.py
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tool_usage import (  # noqa: E402
    count_call, reset_usage, tool_names, usage_snapshot, wrap_registered_tools,
)


class FakeTool:
    """Запись реестра FastMCP: имя, функция и построенная по ней схема."""

    def __init__(self, fn):
        self.fn = fn
        # Схема строится ОДИН РАЗ, при регистрации, из исходной функции —
        # ровно так же, как это делает FastMCP. Поэтому подмена `fn` после
        # регистрации на неё не влияет, а обёртка на регистрации влияла.
        self.schema = {
            "name": fn.__name__,
            "doc": fn.__doc__,
            "params": list(inspect.signature(fn).parameters),
            "annotations": dict(getattr(fn, "__annotations__", {})),
        }


class FakeMcp:
    """Двойник FastMCP: помнит, что зарегистрировали, и чем именно."""

    class Manager:
        def __init__(self):
            self._tools = {}

    def __init__(self):
        self._tool_manager = FakeMcp.Manager()

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self._tool_manager._tools[fn.__name__] = FakeTool(fn)
            return fn
        return decorator

    def call(self, name, *a, **kw):
        return self._tool_manager._tools[name].fn(*a, **kw)


class UsageCase(unittest.TestCase):

    def setUp(self):
        reset_usage()
        self.mcp = FakeMcp()

    def wrap(self):
        """Инструменты оборачиваются ПОСЛЕ регистрации — как в start.py."""
        return wrap_registered_tools(self.mcp)

    def tearDown(self):
        reset_usage()


class TestCounting(UsageCase):

    def test_calls_are_counted_by_name(self):
        @self.mcp.tool()
        def metadata_search(query: str, limit: int = 20) -> str:
            """Поиск объектов."""
            return "ok"

        self.wrap()
        self.mcp.call("metadata_search", "контрагенты")
        self.mcp.call("metadata_search", "склад")
        snap = usage_snapshot()
        self.assertEqual(snap["by_tool"]["metadata_search"], 2)
        self.assertEqual(snap["total_calls"], 2)

    def test_failed_call_is_still_a_call(self):
        """
        Инструмент, который зовут и который всегда падает, надо видеть —
        иначе он неотличим от незваного, а лечится он совсем иначе.
        """
        @self.mcp.tool()
        def broken() -> str:
            raise RuntimeError("нет базы")

        self.wrap()
        with self.assertRaises(RuntimeError):
            self.mcp.call("broken")
        snap = usage_snapshot()
        self.assertEqual(snap["by_tool"]["broken"], 1)
        self.assertEqual(snap["errors_by_tool"]["broken"], 1)

    def test_return_value_is_untouched(self):
        @self.mcp.tool()
        def echo(text: str) -> str:
            return text.upper()

        self.wrap()
        self.assertEqual(self.mcp.call("echo", "да"), "ДА")

    def test_never_called_is_the_point(self):
        """
        Незваный инструмент следов не оставляет — его видно только по
        списку зарегистрированных. Без него счётчик отвечает на вопрос,
        который никто не задавал.
        """
        @self.mcp.tool()
        def used() -> str:
            return "1"

        @self.mcp.tool()
        def unused() -> str:
            return "2"

        self.wrap()
        self.mcp.call("used")
        snap = usage_snapshot(tool_names(self.mcp))
        self.assertEqual(snap["never_called"], ["unused"])
        self.assertEqual(snap["registered"], 2)

    def test_snapshot_says_it_forgets_on_restart(self):
        """
        Счётчик живёт в памяти процесса. Умолчать об этом значило бы
        позволить прочитать «ноль вызовов» как «инструмент не нужен» сразу
        после перезапуска контейнера.
        """
        self.assertIn("перезапуске", usage_snapshot()["note"])

    def test_counting_directly_works_too(self):
        count_call("вручную")
        self.assertEqual(usage_snapshot()["by_tool"]["вручную"], 1)


class TestSchemaSurvives(UsageCase):
    """
    Здесь проверяется урок, который стоил живого запуска.

    Первая версия ставила обёртку на РЕГИСТРАЦИЮ. FastMCP строит схему
    инструмента из самой функции, разрешая аннотации в её глобальном
    пространстве; обёртка живёт в другом модуле, где нет ни `Optional`, ни
    прочих имён сервера, — и регистрация v3-инструментов упала с
    «Optional is not defined». Ни один тест этого не поймал: в двойнике
    аннотации были простые.

    Обёртка после регистрации такого класса дефектов не имеет вовсе: схема
    уже построена. Проверяем именно это свойство.
    """

    def setUp(self):
        super().setUp()

        @self.mcp.tool()
        def query_fields(object_name: str, query_text: str = "",
                         limit: int = 50) -> str:
            """Поля объекта для запроса."""
            return "ok"

        self.before = dict(self.mcp._tool_manager._tools["query_fields"].schema)
        self.wrap()
        self.after = self.mcp._tool_manager._tools["query_fields"].schema

    def test_schema_is_the_same_object_as_before_wrapping(self):
        self.assertEqual(self.before, self.after)

    def test_params_and_defaults_intact(self):
        self.assertEqual(self.after["params"],
                         ["object_name", "query_text", "limit"])

    def test_docstring_intact(self):
        self.assertEqual(self.after["doc"], "Поля объекта для запроса.")

    def test_wrapper_keeps_a_way_back_to_the_original(self):
        fn = self.mcp._tool_manager._tools["query_fields"].fn
        self.assertTrue(hasattr(fn, "__wrapped__"))
        self.assertEqual(fn.__name__, "query_fields")

    def test_wrapping_twice_does_not_double_count(self):
        self.wrap()
        self.mcp.call("query_fields", "Справочник.Контрагенты")
        self.assertEqual(usage_snapshot()["by_tool"]["query_fields"], 1)


class TestWiring(unittest.TestCase):
    """
    Счётчик должен быть подключён там, где его видно, и не подключён там,
    где его показать негде. Второе — решение, а не забывчивость, и потому
    тоже проверяется: молчаливое расхождение списков в этом проекте
    случалось шесть раз.
    """

    SUITE = Path(__file__).resolve().parent

    def src(self, path: str) -> str:
        return (self.SUITE / path).read_text(encoding="utf-8")

    def test_servers_with_stats_report_usage(self):
        for server, stats in (
            ("mcp-metadata-graph/server.py", "metadata_stats"),
            ("mcp-platform-help/server.py", "platform_help_stats"),
            ("mcp-bsl-checker/server.py", "bsl_stats"),
        ):
            text = self.src(server)
            self.assertIn("usage_snapshot(tool_names(mcp))", text,
                          f"{server}: счётчик считает, а {stats} молчит")

    def test_counter_is_installed_once_for_all_servers(self):
        """
        Установка живёт в start.py, рядом с обёрткой метриками. Если она
        переедет в серверы по одной строке на файл, вернётся список,
        который надо помнить.
        """
        text = self.src("start.py")
        self.assertIn("_wrap_tools_with_usage(mcp_obj, name)", text)
        self.assertIn("wrap_registered_tools", text)

    def test_query_builder_explains_why_it_has_none(self):
        text = self.src("mcp-query-builder/server.py")
        self.assertNotIn("usage_snapshot", text)
        self.assertIn("TOOL-1 здесь НЕ ставится", text,
                      "пропуск без объяснения неотличим от забытого COPY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
