"""
B-4. Один словарь постраничности на четыре сервера.
====================================================

Что было
────────
Словарей было три с половиной:

  metadata-graph  — правильный: total/returned/offset/limit/has_more/next_offset
  bsl-checker     — свой: `shown` вместо `returned`, а поля `total` не было
                    вовсе — его место занимало `total_issues`, считающее
                    ЗАМЕЧАНИЯ, а не строки списка
  query-builder   — огрызок: `attributes_total` есть, `has_more` нет ни под
                    каким именем
  platform-help   — только `limit`

Цена не в дубликате кода. Правила Cursor не могли сказать «видишь
`has_more: true` — запроси следующую страницу», потому что у двух серверов
из четырёх такого поля нет вовсе. Правило с исключениями агенты роняют
первыми, поэтому исключения быть не должно.

Что проверяется здесь
─────────────────────
Не «поля называются одинаково» — это следствие. Проверяется, что второго
места, где эти имена рождаются, не существует: любой литерал `"has_more"`
вне `mcp_pagination.py` и есть начало четвёртого словаря.

Такой запрет держится сам. Список серверов ему не нужен: новый сервер
попадает под проверку по факту появления файла.

Запуск:  python3 tests_pagination.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SUITE = Path(__file__).resolve().parent
sys.path.insert(0, str(SUITE))

from mcp_pagination import (  # noqa: E402
    PAGINATION_NONE,
    PAGINATION_PAGED,
    PaginationParams,
    no_pagination,
    page_fields,
    paginate,
    truncate_text_window,
)

# Единственный файл, которому позволено произносить эти имена.
VOCABULARY_HOME = "mcp_pagination.py"

# Поля, которые обязаны быть в ЛЮБОМ ответе с коллекцией — и в
# положительной ветке, и в отрицательной.
CONTRACT = ("pagination", "has_more", "next_offset", "returned")


def server_sources() -> list[Path]:
    """Исходники своих серверов. Список не ведётся руками — он вычисляется."""
    return sorted(
        p for p in SUITE.rglob("*.py")
        if p.name != VOCABULARY_HOME
        and not p.name.startswith(("tests_", "calibrate_"))
        and "v8std" not in str(p)          # чужой сервер, не наш контракт
    )


class TestVocabulary(unittest.TestCase):

    def test_paged_block_is_complete(self):
        block = page_fields(total=100, offset=0, limit=20, returned=20)
        for field in CONTRACT:
            self.assertIn(field, block)
        self.assertEqual(block["pagination"], PAGINATION_PAGED)
        self.assertTrue(block["has_more"])
        self.assertEqual(block["next_offset"], 20)

    def test_next_offset_is_present_when_there_is_no_next(self):
        """
        Главная договорённость. Раньше поле просто отсутствовало на
        последней странице — и «страниц больше нет» становилось
        неотличимо от «сервер про страницы не знает». Тот же дефект, что
        FIX-19 чинил у поля `found`.
        """
        block = page_fields(total=20, offset=0, limit=20, returned=20)
        self.assertIn("next_offset", block)
        self.assertIsNone(block["next_offset"])
        self.assertFalse(block["has_more"])

    def test_last_page_is_not_promised_more(self):
        block = page_fields(total=25, offset=20, limit=20, returned=5)
        self.assertFalse(block["has_more"])
        self.assertIsNone(block["next_offset"])

    def test_short_page_still_trusts_total(self):
        """
        Отдали меньше, чем просили, а `total` говорит, что есть ещё.

        Я писал этот тест с ожиданием `has_more: false` — «короткая
        страница значит конец». Реализация сказала `true`, и права она.

        Две причины. Первая: `total` и строки приходят разными запросами
        (COUNT и SKIP/LIMIT), и короткая страница может означать не конец,
        а отфильтрованные строки. Вторая важнее — цена ошибки
        несимметрична. Лишний `has_more: true` стоит одного пустого
        вызова. Ошибочный `false` стоит молча потерянных данных, и
        заметить это некому: ответ выглядит полным.

        Поэтому при расхождении верим `total`.
        """
        block = page_fields(total=100, offset=0, limit=20, returned=7)
        self.assertTrue(block["has_more"])
        self.assertEqual(block["next_offset"], 7,
                         "следующий offset считается от отданного, "
                         "а не от запрошенного лимита")

    def test_alias_matches_canonical(self):
        """
        Префиксные имена для вложенной коллекции считаются из того же
        расчёта, а не по второй формуле, — иначе это опять два словаря,
        просто внутри одного ответа.
        """
        block = page_fields(200, 10, 50, 50, alias="attributes")
        self.assertEqual(block["attributes_total"], block["total"])
        self.assertEqual(block["attributes_has_more"], block["has_more"])
        self.assertEqual(block["attributes_next_offset"], block["next_offset"])
        self.assertEqual(block["paginated_field"], "attributes")

    def test_none_block_keeps_the_contract(self):
        """
        «Страниц нет» — это ответ в том же словаре, а не отсутствие полей.
        Правило про has_more остаётся без исключений: поле есть у всех,
        просто у справки оно всегда false.
        """
        block = no_pagination(10, limit=10, reason="ранжированная выдача",
                              instead="уточните запрос")
        for field in CONTRACT:
            self.assertIn(field, block)
        self.assertEqual(block["pagination"], PAGINATION_NONE)
        self.assertFalse(block["has_more"])
        self.assertIsNone(block["next_offset"])

    def test_none_block_says_what_to_do_instead(self):
        """
        Сказать «страниц нет» и не сказать, что делать, — значит оставить
        агента там, откуда он пришёл. Он попробует limit побольше и получит
        хвост выдачи с падающей релевантностью.
        """
        block = no_pagination(10, reason="почему", instead="что делать")
        self.assertIn("pagination_reason", block)
        self.assertIn("pagination_instead", block)

    def test_paginate_uses_the_same_block(self):
        out = paginate(list(range(30)), PaginationParams(limit=10, offset=0))
        for field in CONTRACT:
            self.assertIn(field, out)
        self.assertEqual(out["next_offset"], 10)
        self.assertEqual(out["items"], list(range(10)))

    def test_text_window_is_symmetric_too(self):
        """Листание текста модуля — та же договорённость."""
        for text, offset in (("абв", 0), ("", 0), ("абв", 99)):
            with self.subTest(text=text, offset=offset):
                out = truncate_text_window(text, offset=offset, window=1000)
                self.assertIn("next_offset", out)
                self.assertIn("has_more", out)

    def test_negative_input_does_not_produce_negative_offsets(self):
        block = page_fields(total=-5, offset=-3, limit=10, returned=-1)
        self.assertEqual((block["total"], block["offset"], block["returned"]),
                         (0, 0, 0))


class TestNoSecondVocabulary(unittest.TestCase):
    """
    Запрет на четвёртый словарь. Держится без списка серверов: под проверку
    попадает всё, что лежит в наборе.
    """

    def test_nobody_spells_the_fields_by_hand(self):
        offenders = []
        for path in server_sources():
            text = path.read_text(encoding="utf-8")
            for num, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r'"(has_more|next_offset)"\s*:', code):
                    offenders.append(f"{path.relative_to(SUITE)}:{num}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "поля постраничности собраны вручную — это начало четвёртого "
            "словаря; соберите блок через page_fields/no_pagination:\n  "
            + "\n  ".join(offenders),
        )

    def test_shown_is_not_used_instead_of_returned(self):
        """
        `shown` жил в bsl-checker и значил ровно то же, что `returned` у
        остальных. Синоним хуже отсутствия: агент читает знакомое слово и
        не замечает, что читает другой словарь.
        """
        offenders = [
            f"{p.relative_to(SUITE)}"
            for p in server_sources()
            if re.search(r'"shown"\s*:', p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_every_server_speaks_the_vocabulary(self):
        """
        Четыре своих сервера обязаны пользоваться модулем. Если сервер
        отдаёт коллекции и не импортирует mcp_pagination — он либо не
        листает ничего, либо завёл свой словарь молча.
        """
        servers = {
            "mcp-metadata-graph": "server.py",
            "mcp-bsl-checker": "server.py",
            "mcp-query-builder": "query_check.py",
            "mcp-platform-help": "server.py",
        }
        for folder, name in servers.items():
            with self.subTest(server=folder):
                path = SUITE / folder / name
                self.assertTrue(path.exists(), f"нет {path}")
                self.assertIn("mcp_pagination",
                              path.read_text(encoding="utf-8"),
                              f"{folder} не пользуется общим словарём")


if __name__ == "__main__":
    unittest.main(verbosity=2)
