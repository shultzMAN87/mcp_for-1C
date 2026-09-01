#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Тесты блока H: разбор storage format и классификатор утверждений.

Сети здесь нет и быть не должно: набор гоняется в общем прогоне
run_all_tests.py, а он обязан работать на машине без Confluence и без Neo4j.
Проверяется то, что ломается молча:

  • страница, которая не разобралась, помечена как неразобранная, а не как
    пустая (R16 — потеря содержимого происходит именно так);
  • неподдерживаемые конструкции перечислены поимённо, а не выброшены;
  • «не проверяемо» не теряется при перефразировке (R15);
  • имя объекта опознаётся во всех трёх формах и регистронезависимо (FIX-18).

Запуск: python scripts\\tests_docs_migration.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from confluence_storage import parse_storage, statements  # noqa: E402
from docs_classify import (CONFIRMED, MISSING, UNVERIFIABLE, Names,  # noqa: E402
                           build_card, check_preserved, find_candidates)

NAMES_FIXTURE = {
    "canonical": ["Catalog.Контрагенты", "CommonModule.ОбменМП",
                  "InformationRegister.СостоянияЗаказов"],
    "aliases": {
        "catalog.контрагенты": "Catalog.Контрагенты",
        "справочник.контрагенты": "Catalog.Контрагенты",
        "справочники.контрагенты": "Catalog.Контрагенты",
        "commonmodule.обменмп": "CommonModule.ОбменМП",
        "общиймодуль.обменмп": "CommonModule.ОбменМП",
        "общиемодули.обменмп": "CommonModule.ОбменМП",
        "informationregister.состояниязаказов": "InformationRegister.СостоянияЗаказов",
        "регистрсведений.состояниязаказов": "InformationRegister.СостоянияЗаказов",
        "регистрысведений.состояниязаказов": "InformationRegister.СостоянияЗаказов",
    },
}


def make_names() -> Names:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(NAMES_FIXTURE, fh, ensure_ascii=False)
        path = Path(fh.name)
    return Names.load(path)


class TestStorageParsing(unittest.TestCase):

    def test_named_entities_do_not_break_parsing(self):
        """&nbsp; и &mdash; в XML не определены — штатный парсер на них падает."""
        parsed = parse_storage("<p>раз&nbsp;два &mdash; три</p>")
        self.assertTrue(parsed.ok)
        self.assertIn("три", parsed.text)

    def test_unparsed_page_is_not_empty_page(self):
        """Главное различие блока H: не разобралось ≠ пусто."""
        parsed = parse_storage("<p>тег не закрыт")
        self.assertFalse(parsed.ok)
        self.assertTrue(parsed.error)

    def test_unsupported_macros_are_listed_not_dropped(self):
        """R16: молча терять нельзя. Конструкция обязана быть названа."""
        parsed = parse_storage(
            '<ac:structured-macro ac:name="drawio">'
            '<ac:parameter ac:name="diagramName">схема</ac:parameter>'
            '</ac:structured-macro>')
        self.assertIn("drawio", parsed.unsupported)
        self.assertTrue(any(b.kind == "unsupported" for b in parsed.blocks))

    def test_code_macro_body_survives(self):
        parsed = parse_storage(
            '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
            '<![CDATA[Процедура Х() КонецПроцедуры]]>'
            '</ac:plain-text-body></ac:structured-macro>')
        self.assertIn("КонецПроцедуры", parsed.text)

    def test_list_item_is_one_statement(self):
        """Пункт списка режется по точке — теряется смысл, который на нём держится."""
        parsed = parse_storage("<ul><li>Токен живёт 24 часа. Продлевать нельзя.</li></ul>")
        self.assertEqual(len(statements(parsed)), 1)


class TestClassifier(unittest.TestCase):

    def setUp(self):
        self.names = make_names()

    def test_all_three_name_forms_recognised(self):
        for form in ("Catalog.Контрагенты", "Справочник.Контрагенты",
                     "Справочники.Контрагенты", "catalog.КОНТРАГЕНТЫ"):
            found, unknown = find_candidates(f"Читаем из {form} по коду.", self.names)
            self.assertEqual(found, ["Catalog.Контрагенты"], form)
            self.assertEqual(unknown, [], form)

    def test_missing_object_is_flagged(self):
        found, unknown = find_candidates(
            "Пишем в РегистрСведений.ОчередьОбмена.", self.names)
        self.assertEqual(unknown, ["РегистрСведений.ОчередьОбмена"])

    def test_unknown_kind_is_not_flagged(self):
        """Сознательная осторожность: «Решение.Принято» — не объект метаданных.

        Кандидат помечается несуществующим только если ВИД объекта в
        конфигурации есть, а объекта с таким именем нет. Иначе любая точка
        в предложении порождала бы ложное расхождение, а ложные расхождения
        в карточках — самый быстрый способ отучить человека их читать.
        """
        found, unknown = find_candidates("Решение.Принято в марте.", self.names)
        self.assertEqual((found, unknown), ([], []))

    def test_intent_is_unverifiable_not_wrong(self):
        """Замысел без имён объектов — «не проверяемо», а не ошибка."""
        parsed = parse_storage("<p>От очереди отказались: нагрузка не оправдала.</p>")
        card = build_card({"id": "1", "title": "т"}, parsed, self.names)
        self.assertEqual(card.counts()[UNVERIFIABLE], 1)
        self.assertEqual(card.counts()[MISSING], 0)

    def test_existing_object_is_confirmed(self):
        parsed = parse_storage("<p>Данные лежат в Справочник.Контрагенты целиком.</p>")
        card = build_card({"id": "1", "title": "т"}, parsed, self.names)
        self.assertEqual(card.counts()[CONFIRMED], 1)


class TestPreservation(unittest.TestCase):
    """R15 — центральный риск блока H."""

    def setUp(self):
        self.names = make_names()
        parsed = parse_storage(
            "<p>Обмен придумали ради маркетплейса, иначе заказы заводят руками.</p>")
        self.card = build_card({"id": "1", "title": "т"}, parsed, self.names)

    def test_verbatim_presence_passes(self):
        draft = ("## Зачем\nОбмен придумали ради маркетплейса, "
                 "иначе заказы заводят руками.\n")
        self.assertEqual(check_preserved(self.card, draft), [])

    def test_paraphrase_is_rejected(self):
        """Именно так ценное и исчезает: аккуратной перефразировкой."""
        draft = "## Зачем\nОбмен нужен для работы с маркетплейсом.\n"
        self.assertEqual(len(check_preserved(self.card, draft)), 1)

    def test_whitespace_differences_are_tolerated(self):
        draft = ("Обмен  придумали   ради маркетплейса,\nиначе заказы "
                 "заводят руками.")
        self.assertEqual(check_preserved(self.card, draft), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
