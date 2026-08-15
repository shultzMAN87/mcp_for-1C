"""
Тесты OBS-1: единый словарь отказа.

Проверяется не наличие полей, а различение трёх состояний, ради которого
всё затевалось:

  • ответ есть, нашлось;
  • ответ есть, не нашлось (`found: false` — это ОТВЕТ);
  • ответа нет (`answerable: false`).

Отдельно — симметрия: поле стоит и на успехе. Урок `FIX-19`: проверка на
поле, которого нет в удачной ветке, получает `null` там, где всё хорошо, и
отличить «поля нет, потому что норма» от «поля нет, потому что сервер
старой версии» становится невозможно.

Запуск:  python3 tests_refusal.py
"""

import json
import threading
import unittest

from refusal import (
    ANSWERABLE,
    DEGRADED,
    MEANING_TOOL_DOWN,
    begin_call,
    degradation_reasons,
    install_answerable_field,
    is_degraded,
    mark,
    note_degraded,
    refusal,
    wrap_tool,
)


class TestRefusalPayload(unittest.TestCase):

    def test_refusal_is_not_answerable(self):
        p = refusal("neo4j_unavailable", "Neo4j не отвечает")
        self.assertFalse(p[ANSWERABLE])
        self.assertTrue(p[DEGRADED])
        self.assertEqual(p["error"], "neo4j_unavailable")

    def test_meaning_is_present_by_default(self):
        """
        `meaning` — главное поле отказа: именно оно останавливает модель от
        вывода «значит, такого объекта нет». Отказ без него бесполезен.
        """
        p = refusal("linter_missing", "BSL Language Server не найден")
        self.assertEqual(p["meaning"], MEANING_TOOL_DOWN)
        self.assertIn("НЕ значит", p["meaning"])

    def test_hint_is_optional(self):
        self.assertNotIn("hint", refusal("x", "y"))
        self.assertIn("hint", refusal("x", "y", hint="сделай так"))

    def test_extra_fields_pass_through(self):
        p = refusal("x", "y", file_path="/data/1c-src/M.bsl")
        self.assertEqual(p["file_path"], "/data/1c-src/M.bsl")

    def test_refusal_can_be_undegraded(self):
        """
        Отказ по форме запроса — штатный исход, а не поломка. Помечать его
        деградацией значило бы поднимать ложную тревогу на каждом кривом
        вводе пользователя.
        """
        p = refusal("bad_request", "имя пустое", degraded=False)
        self.assertFalse(p[ANSWERABLE])
        self.assertFalse(p[DEGRADED])


class TestMarkSymmetry(unittest.TestCase):

    def setUp(self):
        begin_call()

    def test_success_gets_the_field_too(self):
        p = mark({"found": True, "name": "Контрагенты"})
        self.assertTrue(p[ANSWERABLE])
        self.assertFalse(p[DEGRADED])

    def test_explicit_value_wins(self):
        """Инструмент знает про свой ответ больше, чем обёртка."""
        p = mark({ANSWERABLE: False, "error": "x"})
        self.assertFalse(p[ANSWERABLE])

    def test_found_false_stays_answerable(self):
        """
        Главное различение всего OBS-1. «Такого объекта нет» — это ОТВЕТ,
        и опираться на него можно. Если бы `found: false` помечался
        `answerable: false`, модель перестала бы верить честному «нет».
        """
        p = mark({"found": False, "query": "ТакогоНет"})
        self.assertTrue(p[ANSWERABLE])
        self.assertFalse(p[DEGRADED])

    def test_non_dict_is_untouched(self):
        self.assertEqual(mark("строка"), "строка")
        self.assertEqual(mark([1, 2]), [1, 2])


class TestDegradationNotes(unittest.TestCase):

    def setUp(self):
        begin_call()

    def test_note_is_picked_up(self):
        note_degraded("счётчик связей не досчитался")
        p = mark({"total": 0})
        self.assertTrue(p[DEGRADED])
        self.assertIn("счётчик связей не досчитался", p["degradation_reasons"])

    def test_answer_stays_usable(self):
        """
        Деградация — не отказ. Ответ беднее обычного, но опираться на него
        можно, поэтому `answerable` остаётся true.
        """
        note_degraded("причина")
        self.assertTrue(mark({"items": [1]})[ANSWERABLE])

    def test_reasons_are_deduplicated(self):
        note_degraded("одно и то же")
        note_degraded("одно и то же")
        self.assertEqual(degradation_reasons(), ["одно и то же"])

    def test_begin_call_resets(self):
        note_degraded("прошлый вызов")
        begin_call()
        self.assertFalse(is_degraded())
        self.assertEqual(degradation_reasons(), [])

    def test_state_does_not_leak_between_threads(self):
        """
        FastMCP исполняет синхронные инструменты в рабочем потоке на запрос.
        Глобальная переменная протекла бы: пометка от одного запроса
        всплыла бы в ответе другого — и «частичный» ответ приехал бы
        пользователю, который ничего такого не спрашивал.
        """
        begin_call()
        note_degraded("поток A")
        seen = {}

        def other():
            begin_call()
            seen["degraded"] = is_degraded()

        t = threading.Thread(target=other)
        t.start()
        t.join()

        self.assertFalse(seen["degraded"], "состояние протекло в другой поток")
        self.assertTrue(is_degraded(), "своё состояние потерялось")


class TestWrapTool(unittest.TestCase):

    def test_json_string_gets_fields(self):
        fn = wrap_tool(lambda: json.dumps({"a": 1}, ensure_ascii=False))
        data = json.loads(fn())
        self.assertTrue(data[ANSWERABLE])
        self.assertFalse(data[DEGRADED])

    def test_refusal_survives_the_wrapper(self):
        fn = wrap_tool(lambda: json.dumps(refusal("x", "y"), ensure_ascii=False))
        self.assertFalse(json.loads(fn())[ANSWERABLE])

    def test_state_is_reset_between_calls(self):
        """
        Без сброса вторая выдача унаследовала бы деградацию первой — и
        нормальный ответ поехал бы с чужой пометкой.
        """
        calls = {"n": 0}

        def tool():
            calls["n"] += 1
            if calls["n"] == 1:
                note_degraded("только первый вызов")
            return json.dumps({"n": calls["n"]}, ensure_ascii=False)

        fn = wrap_tool(tool)
        self.assertTrue(json.loads(fn())[DEGRADED])
        self.assertFalse(json.loads(fn())[DEGRADED])

    def test_non_json_output_is_untouched(self):
        """
        Обёртка, способная испортить нормальный ответ, хуже отсутствующей.
        """
        for value in ("просто текст", "[1, 2, 3]", "{сломанный json", ""):
            self.assertEqual(wrap_tool(lambda v=value: v)(), value)

    def test_top_level_list_is_untouched(self):
        self.assertEqual(wrap_tool(lambda: "[{\"a\": 1}]")(), "[{\"a\": 1}]")

    def test_signature_is_preserved(self):
        """
        FastMCP строит схему инструмента по сигнатуре. Потеряем её —
        инструмент зарегистрируется без параметров и молча перестанет
        принимать аргументы.
        """
        import inspect

        def tool(name: str, limit: int = 5) -> str:
            """Док-строка."""
            return "{}"

        wrapped = wrap_tool(tool)
        self.assertEqual(str(inspect.signature(wrapped)),
                         "(name: str, limit: int = 5) -> str")
        self.assertEqual(wrapped.__doc__, "Док-строка.")
        self.assertEqual(wrapped.__name__, "tool")


class TestInstall(unittest.TestCase):
    """Установка обёртки на объект сервера — одна строка вместо правки всех."""

    class FakeMCP:
        def __init__(self):
            self.registered = []

        def tool(self, *a, **kw):
            def deco(fn):
                self.registered.append(fn)
                return fn
            return deco

    def test_all_tools_get_wrapped(self):
        mcp = self.FakeMCP()
        install_answerable_field(mcp)

        @mcp.tool()
        def one() -> str:
            return json.dumps({"x": 1})

        @mcp.tool()
        def two() -> str:
            return json.dumps({"y": 2})

        self.assertEqual(len(mcp.registered), 2)
        for fn in mcp.registered:
            self.assertIn(ANSWERABLE, json.loads(fn()))

    def test_future_tools_are_covered_too(self):
        """
        Смысл установки на объект, а не на каждую функцию: инструмент,
        добавленный завтра, получает поле без правки списка. Списки,
        которые надо пополнять руками, в этом проекте расходились
        четырежды — трижды с COPY в Dockerfile и раз с генераторами
        лок-файлов.
        """
        mcp = self.FakeMCP()
        install_answerable_field(mcp)

        @mcp.tool()
        def added_later() -> str:
            return json.dumps({"z": 3})

        self.assertIn(ANSWERABLE, json.loads(mcp.registered[0]()))


class TestDeliveredToImages(unittest.TestCase):
    """
    B-6: перечень из трёх Dockerfile отсюда убран — его теперь незачем
    держать в голове. Карту «кто импортирует → в каком образе лежит» строит
    tests_delivery.py по исходникам, здесь остаётся вопрос про этот модуль.
    """

    def test_delivered_everywhere_it_is_imported(self):
        from tests_delivery import assert_delivered
        assert_delivered(self, "refusal.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
