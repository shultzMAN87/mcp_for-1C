"""
Тесты TOOL-2 — размер ответа query_fields.
===========================================

Проверяется `trim_fields_payload` из query_check: чистая функция, которой
не нужны ни Neo4j, ни fastmcp. Сам инструмент `query_fields` протестировать
офлайн нельзя — server.py тянет mcp.server.fastmcp, — поэтому вся логика
обрезки вынесена туда, где её можно проверить.

Главное, что здесь проверяется, — не «обрезали ли», а «сказали ли, что
обрезали». Молча усечённый список хуже полного: агент примет его за
исчерпывающий и построит запрос по несуществующему набору полей.

Запуск:
    python tests_tool2_fields.py -v
"""
from __future__ import annotations

import unittest

from query_check import (FIELDS_DEFAULT_LIMIT, FIELDS_MAX_LIMIT,
                         trim_fields_payload)


def _payload(n_attrs: int = 60, ts: dict | None = None) -> dict:
    """Ответ query_fields до обрезки."""
    return {
        "table_name": "Справочник.Контрагенты",
        "object": "Catalog.Контрагенты",
        "kind": "Справочник",
        "synonym": "Контрагенты",
        "standard_fields": ["Ссылка", "Код", "Наименование", "ПометкаУдаления"],
        "attributes": [
            {"name": f"Реквизит{i}", "role": "attribute",
             "type": "Строка", "synonym": ""}
            for i in range(n_attrs)
        ],
        "tabular_sections": ts if ts is not None else {
            "КонтактныеДанные": [{"name": "Вид"}, {"name": "Значение"}],
            "БанковскиеРеквизиты": [{"name": "Банк"}, {"name": "Счёт"},
                                    {"name": "Валюта"}],
        },
        "virtual_tables": [],
    }


class TestAttributePaging(unittest.TestCase):

    def test_default_limit_applied(self):
        out = trim_fields_payload(_payload(60))
        self.assertEqual(len(out["attributes"]), FIELDS_DEFAULT_LIMIT)
        self.assertEqual(out["attributes_total"], 60)

    def test_truncation_is_announced(self):
        """
        Ключевой тест. Обрезка без пометки — это тихая ложь о составе
        объекта, и агент по такому ответу построит неверный запрос.
        """
        out = trim_fields_payload(_payload(60), attributes_limit=10)
        self.assertTrue(out["attributes_has_more"])
        self.assertEqual(out["attributes_next_offset"], 10)
        self.assertIn("10 из 60", out["note_attributes"])

    def test_no_note_when_everything_fits(self):
        out = trim_fields_payload(_payload(5), attributes_limit=50)
        self.assertNotIn("attributes_has_more", out)
        self.assertNotIn("note_attributes", out)
        self.assertEqual(out["attributes_total"], 5)

    def test_offset_reads_next_page(self):
        out = trim_fields_payload(_payload(60), attributes_limit=25,
                                  attributes_offset=25)
        names = [a["name"] for a in out["attributes"]]
        self.assertEqual(names[0], "Реквизит25")
        self.assertEqual(len(names), 25)
        self.assertEqual(out["attributes_next_offset"], 50)

    def test_last_page_has_no_more(self):
        out = trim_fields_payload(_payload(60), attributes_limit=25,
                                  attributes_offset=50)
        self.assertEqual(len(out["attributes"]), 10)
        self.assertNotIn("attributes_has_more", out)

    def test_pages_cover_everything_without_gaps(self):
        """Страницы обязаны склеиваться обратно в исходный список."""
        src = _payload(137)
        collected, offset = [], 0
        while True:
            out = trim_fields_payload(src, attributes_limit=20,
                                      attributes_offset=offset)
            collected += [a["name"] for a in out["attributes"]]
            if not out.get("attributes_has_more"):
                break
            offset = out["attributes_next_offset"]
        self.assertEqual(collected, [a["name"] for a in src["attributes"]])

    def test_zero_limit_returns_all(self):
        out = trim_fields_payload(_payload(200), attributes_limit=0)
        self.assertEqual(len(out["attributes"]), 200)
        self.assertNotIn("attributes_has_more", out)

    def test_limit_is_capped(self):
        out = trim_fields_payload(_payload(2000), attributes_limit=99999)
        self.assertEqual(len(out["attributes"]), FIELDS_MAX_LIMIT)

    def test_negative_values_do_not_crash(self):
        out = trim_fields_payload(_payload(60), attributes_limit=-5,
                                  attributes_offset=-10)
        self.assertEqual(len(out["attributes"]), FIELDS_DEFAULT_LIMIT)
        self.assertEqual(out["attributes"][0]["name"], "Реквизит0")

    def test_offset_past_end_is_empty_not_error(self):
        out = trim_fields_payload(_payload(10), attributes_offset=500)
        self.assertEqual(out["attributes"], [])
        self.assertEqual(out["attributes_total"], 10)

    def test_object_without_attributes(self):
        p = _payload(0, ts={})
        out = trim_fields_payload(p)
        self.assertEqual(out["attributes"], [])
        self.assertEqual(out["attributes_total"], 0)


class TestTabularSections(unittest.TestCase):

    def test_collapsed_by_default(self):
        """
        На документах ERP состав ТЧ — главный источник разрастания ответа.
        По умолчанию отдаём имена и размеры.
        """
        out = trim_fields_payload(_payload())
        self.assertEqual(
            out["tabular_sections"],
            {"БанковскиеРеквизиты": {"attributes_count": 3},
             "КонтактныеДанные": {"attributes_count": 2}},
        )
        self.assertIn("tabular_section", out["note_tabular_sections"])

    def test_expand_one_section(self):
        out = trim_fields_payload(_payload(), tabular_section="КонтактныеДанные")
        self.assertEqual(list(out["tabular_sections"]), ["КонтактныеДанные"])
        self.assertEqual(len(out["tabular_sections"]["КонтактныеДанные"]), 2)
        self.assertEqual(out["tabular_sections_expanded"], "КонтактныеДанные")

    def test_expand_is_case_insensitive(self):
        """Имена ТЧ в 1С регистронезависимы — требовать точного написания нельзя."""
        out = trim_fields_payload(_payload(), tabular_section="контактныеданные")
        self.assertEqual(list(out["tabular_sections"]), ["КонтактныеДанные"])

    def test_unknown_section_lists_available(self):
        """
        На опечатку отвечаем не пустотой, а списком того, что есть, —
        иначе агент решит, что у объекта нет табличных частей.
        """
        out = trim_fields_payload(_payload(), tabular_section="НетТакой")
        self.assertIn("error_tabular_section", out)
        self.assertIn("КонтактныеДанные", out["error_tabular_section"])
        self.assertIn("БанковскиеРеквизиты", out["error_tabular_section"])

    def test_object_without_sections(self):
        out = trim_fields_payload(_payload(5, ts={}))
        self.assertEqual(out["tabular_sections"], {})
        self.assertNotIn("note_tabular_sections", out)


class TestUntouchedParts(unittest.TestCase):
    """Что резать нельзя."""

    def test_standard_fields_and_virtual_tables_survive(self):
        p = _payload(200)
        p["virtual_tables"] = ["РегистрСведений.Х.СрезПоследних"]
        out = trim_fields_payload(p, attributes_limit=5)
        self.assertEqual(out["standard_fields"], p["standard_fields"])
        self.assertEqual(out["virtual_tables"], p["virtual_tables"])

    def test_identity_fields_survive(self):
        out = trim_fields_payload(_payload(), attributes_limit=1)
        for key in ("table_name", "object", "kind", "synonym"):
            self.assertEqual(out[key], _payload()[key])

    def test_register_key_is_independent_of_limit(self):
        """
        Измерения и ресурсы считаются по полному списку реквизитов. Если бы
        они зависели от attributes_limit, состав ключа регистра менялся бы
        от параметра пагинации — и запрос к регистру собирался бы неверно.
        """
        p = _payload(60)
        p["dimensions"] = ["Изм1", "Изм2"]
        p["resources"] = ["Рес1"]
        out = trim_fields_payload(p, attributes_limit=1)
        self.assertEqual(out["dimensions"], ["Изм1", "Изм2"])
        self.assertEqual(out["resources"], ["Рес1"])

    def test_input_is_not_mutated(self):
        p = _payload(60)
        before = len(p["attributes"])
        trim_fields_payload(p, attributes_limit=5)
        self.assertEqual(len(p["attributes"]), before)
        self.assertIsInstance(p["tabular_sections"]["КонтактныеДанные"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
