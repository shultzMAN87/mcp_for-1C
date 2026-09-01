#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Тесты чтения поля affects из спеки.

Смысл набора — сторож против возврата привязки к инструменту. Контур не
знает и не должен знать, чем порождена спека: генератором фреймворка,
шаблоном в вики или руками. Он знает одно поле и три способа его прочитать.

Поэтому ниже перечислены ФОРМАТЫ записи, а не продукты. Появление в этом
файле имени конкретного инструмента — признак того, что привязка вернулась.

Запуск: python scripts\\tests_docs_affects.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docs_affects import extract_affects  # noqa: E402

EXPECTED = ["Catalog.Контрагенты", "CommonModule.ОбменМП"]


class TestExtraction(unittest.TestCase):

    def test_yaml_frontmatter(self):
        text = ("---\n"
                "id: SPEC-142\n"
                "affects:\n"
                "  - Catalog.Контрагенты\n"
                "  - CommonModule.ОбменМП\n"
                "---\n\n"
                "# Спека\n")
        names, how = extract_affects(text)
        self.assertEqual(names, EXPECTED)
        self.assertEqual(how, "фронтматтер")

    def test_whole_file_json(self):
        text = '{"id": "SPEC-142", "affects": ["Catalog.Контрагенты", "CommonModule.ОбменМП"]}'
        names, how = extract_affects(text)
        self.assertEqual(names, EXPECTED)
        self.assertEqual(how, "файл целиком")

    def test_whole_file_yaml(self):
        text = "id: SPEC-142\naffects:\n  - Catalog.Контрагенты\n  - CommonModule.ОбменМП\n"
        names, _ = extract_affects(text)
        self.assertEqual(names, EXPECTED)

    def test_markdown_section_list(self):
        """Формат без фронтматтера — только заголовок и список."""
        text = ("# Задача\n\nОписание.\n\n"
                "## Затронутые объекты\n\n"
                "- Catalog.Контрагенты\n"
                "- CommonModule.ОбменМП\n\n"
                "## Дальше\n\n- это уже не affects\n")
        names, how = extract_affects(text)
        self.assertEqual(names, EXPECTED)
        self.assertEqual(how, "раздел в markdown")

    def test_markdown_section_stops_at_next_heading(self):
        """Пункты следующего раздела не должны утечь в affects."""
        text = ("## Affects\n- Catalog.Контрагенты\n\n"
                "## Критерии приёмки\n- тесты зелёные\n")
        names, _ = extract_affects(text)
        self.assertEqual(names, ["Catalog.Контрагенты"])

    def test_english_heading_works_too(self):
        text = "## Affects\n\n- Catalog.Контрагенты\n- CommonModule.ОбменМП\n"
        names, _ = extract_affects(text)
        self.assertEqual(names, EXPECTED)

    def test_fenced_block_in_section(self):
        text = ("## Затронутые объекты\n\n"
                "```yaml\n"
                "Catalog.Контрагенты\n"
                "CommonModule.ОбменМП\n"
                "```\n")
        names, _ = extract_affects(text)
        self.assertEqual(names, EXPECTED)

    def test_comma_separated_string(self):
        text = "---\naffects: Catalog.Контрагенты, CommonModule.ОбменМП\n---\n"
        names, _ = extract_affects(text)
        self.assertEqual(names, EXPECTED)

    def test_missing_field_is_empty_not_error(self):
        """Отсутствие поля — сигнал вызывающему, а не исключение."""
        names, how = extract_affects("# Спека без affects\n\nтекст\n")
        self.assertEqual(names, [])
        self.assertEqual(how, "не найдено")


if __name__ == "__main__":
    unittest.main(verbosity=2)
