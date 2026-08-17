"""
Тесты API-1: фильтр набора инструментов падает, а не жалуется.

Почему набор появился только сейчас
───────────────────────────────────
`mcp_tool_filter.py` жил с Захода 1 без единого теста — при том, что это
единственное место проекта, где цена ошибки не «плохой ответ», а открытый
разрушающий инструмент. Причина обычная: модуль выглядит как список имён,
а списки тестировать вроде бы нечего.

Тестировать здесь надо не список, а два исхода, которые до `API-1` были
одним: «фильтр отработал» и «фильтр не нашёл, где фильтровать». Второй
писал строчку в stderr и возвращал управление, после чего сервер поднимался
с `metadata_reload` и `metadata_cypher` в наборе.

Поэтому главные проверки набора — не «удалилось ли», а «падает ли»:

  • реестра нет           → ToolRegistryUnavailable;
  • реестр не dict        → ToolRegistryUnavailable;
  • опасное осталось      → ToolRegistryUnavailable (сверка входа с выходом);
  • профиль `full`        → дубли остаются, опасное всё равно уходит.

Отдельно проверяется `start.py` — по исходнику, а не по запуску: вторая
половина того же дефекта сидела там (`except Exception` вокруг вызова), и
поймать её можно только вопросом «глушится ли исключение».

Запуск:  python3 tests_tool_filter.py
"""

import os
import re
import unittest
from pathlib import Path

from mcp_tool_filter import (
    DESTRUCTIVE,
    RAW_CYPHER,
    V2_SUPERSEDED,
    WATCH_TOOLS,
    ToolRegistryUnavailable,
    apply_profile,
)

HERE = Path(__file__).resolve().parent


class FakeToolManager:
    def __init__(self, tools):
        self._tools = tools


class FakeMCP:
    """Минимальный двойник FastMCP: важен только путь до реестра."""

    def __init__(self, names):
        self._tool_manager = FakeToolManager({n: object() for n in names})

    @property
    def names(self):
        return set(self._tool_manager._tools)


class NoRegistryMCP:
    """SDK переименовал приватное поле — ровно тот случай, ради которого API-1."""


class WrongTypeMCP:
    def __init__(self):
        self._tool_manager = FakeToolManager(None)
        self._tool_manager._tools = ["metadata_reload"]  # список, не словарь


class EnvSandbox(unittest.TestCase):
    """Каждый тест видит чистое окружение: профиль и флаги — глобальные."""

    ENV_KEYS = (
        "MCP_TOOL_PROFILE",
        "ALLOW_DESTRUCTIVE_TOOLS",
        "ALLOW_RAW_CYPHER",
        "ENABLE_WATCH_TOOLS",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ─── главное: разделение исходов ─────────────────────────────────────────


class TestFailClosed(EnvSandbox):

    def test_registry_missing_raises(self):
        """
        Нет приватного поля — сервер не стартует.

        До правки здесь была строчка в stderr и `return`. Разница видна не в
        этом тесте, а в следующем: набор при этом оставался нетронутым.
        """
        with self.assertRaises(ToolRegistryUnavailable):
            apply_profile(NoRegistryMCP(), "metadata-graph")

    def test_registry_missing_does_not_silently_keep_destructive(self):
        """
        Тот же случай, но проверяем последствие, а не исключение.

        Смысл `API-1` целиком: раньше отсутствие реестра означало «набор
        оставлен как есть», то есть `metadata_reload` на месте. Тест
        формулирует это как утверждение о наборе, чтобы оно осталось верным,
        даже если завтра поменяется тип исключения.
        """
        mcp = NoRegistryMCP()
        raised = False
        try:
            apply_profile(mcp, "metadata-graph")
        except ToolRegistryUnavailable:
            raised = True
        self.assertTrue(
            raised,
            "фильтр вернул управление, не приведя набор к целевому — "
            "значит, сервер поднимется с разрушающим инструментом",
        )

    def test_registry_wrong_type_raises(self):
        with self.assertRaises(ToolRegistryUnavailable):
            apply_profile(WrongTypeMCP(), "metadata-graph")

    def test_message_names_what_to_do(self):
        """
        Отказ на старте читает человек в логе контейнера, и читает он его
        один раз. Сообщение обязано называть и причину, и следующий шаг —
        иначе оно ничем не лучше прежней строки «набор оставлен как есть».
        """
        with self.assertRaises(ToolRegistryUnavailable) as ctx:
            apply_profile(NoRegistryMCP(), "metadata-graph")
        text = str(ctx.exception)
        self.assertIn("metadata-graph", text)
        self.assertIn("metadata_reload", text)
        self.assertIn("lock", text.lower())


class TestShortfallOnTools(EnvSandbox):
    """
    Сверка входа с выходом, тот же приём, что `shortfall.py` для данных.

    Между «мы вызвали del» и «инструмента в наборе нет» помещается всё, что
    делает защиту бумажной. Двойник ниже изображает реестр, из которого
    удаление не срабатывает, — так выглядел бы второй реестр или повторная
    регистрация после фильтра.
    """

    class StubbornDict(dict):
        def __delitem__(self, key):
            return  # молча не удаляем

    def test_destructive_left_behind_raises(self):
        mcp = FakeMCP([])
        mcp._tool_manager._tools = self.StubbornDict(
            {"metadata_reload": object(), "metadata_search": object()})
        with self.assertRaises(ToolRegistryUnavailable) as ctx:
            apply_profile(mcp, "metadata-graph")
        self.assertIn("metadata_reload", str(ctx.exception))

    def test_raw_cypher_left_behind_raises(self):
        mcp = FakeMCP([])
        mcp._tool_manager._tools = self.StubbornDict(
            {"metadata_cypher": object()})
        with self.assertRaises(ToolRegistryUnavailable) as ctx:
            apply_profile(mcp, "metadata-graph")
        self.assertIn("metadata_cypher", str(ctx.exception))

    def test_duplicates_left_behind_do_not_raise(self):
        """
        Дубль v2/v3, оставшийся в наборе, — плохой набор, а не открытая
        дверь. Падать на нём значит уравнять неудобство с разрушающим
        инструментом; ровно от такого уравнивания страдал `check_publish.py`
        до разделения FAIL и WARN.
        """
        mcp = FakeMCP([])
        mcp._tool_manager._tools = self.StubbornDict(
            {"metadata_list_kinds": object()})
        apply_profile(mcp, "metadata-graph")  # без исключения


# ─── обычная работа ──────────────────────────────────────────────────────


class TestProfileCore(EnvSandbox):

    def _apply(self, extra=()):
        names = (list(V2_SUPERSEDED) + list(DESTRUCTIVE) + list(RAW_CYPHER)
                 + list(WATCH_TOOLS) + ["metadata_search"] + list(extra))
        mcp = FakeMCP(names)
        apply_profile(mcp, "metadata-graph")
        return mcp.names

    def test_core_removes_duplicates_and_dangerous(self):
        left = self._apply()
        self.assertNotIn("metadata_reload", left)
        self.assertNotIn("metadata_cypher", left)
        self.assertFalse(left & set(V2_SUPERSEDED))
        self.assertIn("metadata_search", left)

    def test_watch_tools_stay_by_default(self):
        """Они служебные, но workspace-watcher без них не работает."""
        self.assertTrue(set(WATCH_TOOLS) <= self._apply())

    def test_watch_tools_can_be_switched_off(self):
        os.environ["ENABLE_WATCH_TOOLS"] = "0"
        self.assertFalse(set(WATCH_TOOLS) & self._apply())

    def test_idempotent(self):
        mcp = FakeMCP(list(V2_SUPERSEDED) + list(DESTRUCTIVE) + ["metadata_search"])
        apply_profile(mcp, "metadata-graph")
        first = mcp.names
        apply_profile(mcp, "metadata-graph")
        self.assertEqual(first, mcp.names)

    def test_unknown_profile_behaves_as_core(self):
        """
        Опечатка в `MCP_TOOL_PROFILE` не имеет права раскрывать набор.
        Всё, что не `full`, — это `core`.
        """
        os.environ["MCP_TOOL_PROFILE"] = "cor"
        left = self._apply()
        self.assertFalse(left & set(V2_SUPERSEDED))
        self.assertNotIn("metadata_reload", left)


class TestProfileFull(EnvSandbox):

    def test_full_keeps_duplicates_but_not_dangerous(self):
        """
        `full` — про дубли v2/v3, а не про разрушающий инструмент.

        Иначе «покажи всё, что есть» становилось бы способом вернуть
        `metadata_reload`, не написав `ALLOW_DESTRUCTIVE_TOOLS=1`, — то есть
        флаг, который человек ставит осознанно, обходился бы флагом, который
        ставят из любопытства.
        """
        os.environ["MCP_TOOL_PROFILE"] = "full"
        mcp = FakeMCP(list(V2_SUPERSEDED) + list(DESTRUCTIVE) + list(RAW_CYPHER))
        apply_profile(mcp, "metadata-graph")
        self.assertTrue(set(V2_SUPERSEDED) <= mcp.names)
        self.assertNotIn("metadata_reload", mcp.names)
        self.assertNotIn("metadata_cypher", mcp.names)

    def test_flags_return_dangerous_tools(self):
        os.environ["ALLOW_DESTRUCTIVE_TOOLS"] = "1"
        os.environ["ALLOW_RAW_CYPHER"] = "yes"
        mcp = FakeMCP(list(DESTRUCTIVE) + list(RAW_CYPHER))
        apply_profile(mcp, "metadata-graph")
        self.assertIn("metadata_reload", mcp.names)
        self.assertIn("metadata_cypher", mcp.names)


# ─── вторая половина дефекта: вызывающая сторона ─────────────────────────


class TestStartPyDoesNotSwallow(unittest.TestCase):
    """
    Проверка по исходнику, а не по запуску.

    `start.py` импортируется только внутри контейнера (он сам это
    проверяет), поэтому единственный способ спросить «глушится ли отказ
    фильтра» — прочитать текст. Тот же приём, что в
    `tests_graph_contract.py`: сверять не поведение, а написанное.
    """

    def setUp(self):
        self.text = (HERE / "start.py").read_text(encoding="utf-8")

    def _apply_block(self) -> str:
        start = self.text.index("apply_profile(mcp_obj, name)")
        return self.text[max(0, start - 400):start + 400]

    def test_no_bare_except_around_apply_profile(self):
        block = self._apply_block()
        self.assertNotRegex(
            block, r"except\s+Exception[^\n]*:\s*\n\s*sys\.stderr\.write",
            "отказ фильтра снова глушится записью в stderr — сервер "
            "поднимется без фильтра",
        )

    def test_exits_with_ex_config(self):
        block = self._apply_block()
        self.assertIn("ToolRegistryUnavailable", block)
        self.assertRegex(
            block, r"SystemExit\(78\)",
            "отказ фильтра обязан давать EX_CONFIG, как пустой "
            "MCP_SHARED_SECRET (SEC-3)",
        )


class TestDeliveredToImages(unittest.TestCase):

    def test_delivered_everywhere_it_is_imported(self):
        from tests_delivery import assert_delivered
        assert_delivered(self, "mcp_tool_filter.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
