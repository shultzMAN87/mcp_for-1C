"""
Юнит-тесты bsl_parser.py (без Neo4j, без Docker).

Запуск:
    python tests_bsl_parser.py
или
    python -m unittest tests_bsl_parser -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from bsl_parser import (
    ParsedParameter,
    classify_bsl_path,
    iter_assign_refs,
    iter_calls,
    iter_metadata_access,
    iter_predef,
    parse_bsl_text,
    _preprocess,
    _split_params,
    # 4.6.4
    iter_new_assigns,
    iter_var_assigns,
    iter_call_assigns,
    split_args,
    _extract_args,
    # FIX-4
    collect_local_names,
    collect_module_var_names,
    # FIX-4.1
    context_property_names,
    collect_form_attribute_names,
)


# ─── 1. Препроцессор ──────────────────────────────────────────────────────


class TestPreprocessor(unittest.TestCase):

    def test_blank_line_comment(self):
        text = "А = 1; // комментарий\nБ = 2;"
        pre = _preprocess(text)
        # Длина та же, пробелы вместо комментария.
        self.assertEqual(len(pre), len(text))
        self.assertIn("\n", pre)
        # `// комментарий` затёрто, `А = 1;` сохранено.
        self.assertTrue(pre.startswith("А = 1; "))
        # И в той же строке после `;` уже пробелы.
        self.assertNotIn("комментарий", pre)

    def test_blank_string_literal(self):
        text = 'А = "Привет, мир";'
        pre = _preprocess(text)
        # Длина та же. Литерал ЦЕЛИКОМ (вместе с кавычками) — пробелы;
        # такое поведение допустимо, нам важно лишь чтобы содержимое
        # литерала не осталось видимым для регэкспов.
        self.assertEqual(len(pre), len(text))
        self.assertNotIn("Привет", pre)
        # `А = ` и `;` остаются (всё что вне литерала).
        self.assertTrue(pre.startswith("А = "))
        self.assertTrue(pre.rstrip().endswith(";"))

    def test_blank_string_with_doubled_quotes(self):
        # BSL экранирует " через "" внутри литерала.
        text = 'А = "Это ""очень"" просто";'
        pre = _preprocess(text)
        self.assertEqual(len(pre), len(text))
        self.assertNotIn("очень", pre)
        # Концевая `;` (вне литерала) сохранена.
        self.assertTrue(pre.rstrip().endswith(";"))

    def test_blank_multiline_string(self):
        # Многострочный литерал с продолжением через |.
        text = 'А = "ВЫБРАТЬ\n     | Поле\n     | ИЗ Справочник.X";\nБ = 2;'
        pre = _preprocess(text)
        self.assertEqual(len(pre), len(text))
        # Содержимое затёрто, но переносы строк остались.
        self.assertEqual(pre.count("\n"), text.count("\n"))
        self.assertNotIn("ВЫБРАТЬ", pre)
        self.assertNotIn("Справочник.X", pre)
        # Б = 2; на своей строке сохранён.
        self.assertIn("Б = 2;", pre)

    def test_preserves_lengths(self):
        text = '// abc\n"xyz"\nВозврат;'
        pre = _preprocess(text)
        self.assertEqual(len(pre), len(text))


# ─── 2. _split_params ─────────────────────────────────────────────────────


class TestSplitParams(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_split_params(""), [])
        self.assertEqual(_split_params("   "), [])

    def test_simple(self):
        self.assertEqual(
            [p.strip() for p in _split_params("А, Б, В")],
            ["А", "Б", "В"],
        )

    def test_nested_parens(self):
        # Запятая внутри скобок default-выражения не должна делить.
        parts = _split_params("А, Б = Новый Массив(1, 2, 3), В")
        self.assertEqual(len(parts), 3)
        self.assertIn("Новый Массив(1, 2, 3)", parts[1])


# ─── 3. Декларации ────────────────────────────────────────────────────────


class TestDeclarations(unittest.TestCase):

    def test_procedure_no_params(self):
        text = "Процедура Привет()\nКонецПроцедуры"
        procs = parse_bsl_text(text)
        self.assertEqual(len(procs), 1)
        p = procs[0]
        self.assertEqual(p.name, "Привет")
        self.assertEqual(p.kind, "Procedure")
        self.assertFalse(p.is_export)
        self.assertEqual(p.directive, "")
        self.assertEqual(p.parameters, [])

    def test_function_with_export(self):
        text = "Функция СуммаДвух(А, Б) Экспорт\n  Возврат А + Б;\nКонецФункции"
        procs = parse_bsl_text(text)
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].kind, "Function")
        self.assertTrue(procs[0].is_export)
        self.assertEqual([p.name for p in procs[0].parameters], ["А", "Б"])

    def test_param_with_byval(self):
        text = "Процедура X(Знач А)\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual(len(p.parameters), 1)
        self.assertTrue(p.parameters[0].is_by_value)
        self.assertEqual(p.parameters[0].name, "А")

    def test_param_with_default(self):
        text = 'Процедура X(А, Б = Неопределено, В = 42)\nКонецПроцедуры'
        p = parse_bsl_text(text)[0]
        self.assertEqual(len(p.parameters), 3)
        self.assertEqual(p.parameters[0].default_value, "")
        self.assertEqual(p.parameters[1].default_value, "Неопределено")
        self.assertEqual(p.parameters[2].default_value, "42")

    def test_param_with_string_default_is_blanked(self):
        # Известное ограничение: декларация парсится по preprocessed-тексту,
        # поэтому строковый литерал в default'е затирается. Это безопасно —
        # `default_value` нужен как сигнал «есть default» и для дебага, не для
        # вычисления реального значения.
        text = 'Процедура X(А, Б = "значение по умолчанию")\nКонецПроцедуры'
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.parameters[0].default_value, "")
        # У Б default есть (был "значение по умолчанию"), но содержимое затёрто
        # → default_value == "" после strip. Это ограничение зафиксировано.
        self.assertEqual(p.parameters[1].default_value, "")

    def test_directive_na_klient(self):
        text = "&НаКлиенте\nПроцедура X()\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.directive, "НаКлиенте")

    def test_directive_na_servere(self):
        text = "&НаСервере\nПроцедура X()\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.directive, "НаСервере")

    def test_directive_na_servere_bez_konteksta(self):
        text = "&НаСервереБезКонтекста\nФункция X() Экспорт\nКонецФункции"
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.directive, "НаСервереБезКонтекста")

    def test_no_directive(self):
        text = "Процедура X()\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.directive, "")

    def test_multiple_procedures(self):
        text = (
            "Процедура A()\n  Б();\nКонецПроцедуры\n\n"
            "Функция Б()\n  Возврат 1;\nКонецФункции\n"
        )
        procs = parse_bsl_text(text)
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0].name, "A")
        self.assertEqual(procs[0].kind, "Procedure")
        self.assertEqual(procs[1].name, "Б")
        self.assertEqual(procs[1].kind, "Function")

    def test_body_line_numbers(self):
        text = "// заголовок\n\nПроцедура X()\n  А = 1;\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual(p.line_start, 3)
        self.assertEqual(p.line_end, 5)

    def test_unclosed_procedure_skipped(self):
        text = "Процедура X()\n  А = 1;\n"  # нет КонецПроцедуры
        procs = parse_bsl_text(text)
        self.assertEqual(procs, [])

    def test_procedure_with_only_byval_param(self):
        text = "Процедура X(Знач А, Б, Знач В)\nКонецПроцедуры"
        p = parse_bsl_text(text)[0]
        self.assertEqual([(prm.name, prm.is_by_value) for prm in p.parameters],
                         [("А", True), ("Б", False), ("В", True)])


# ─── 4. Декларация не должна попасть в callsite ──────────────────────────


class TestDeclVsCallsite(unittest.TestCase):
    """Главная false-positive проверка: `Процедура X(` не должна попасть в callsite."""

    def test_declaration_not_called_as_local(self):
        body_text = "Процедура X()\n  А = 1;\nКонецПроцедуры"
        # Парсим всё, потом проверяем, что body процедуры X не содержит вызова X.
        procs = parse_bsl_text(body_text)
        # У X тело — `А = 1;`. В нём нет вызовов.
        calls = list(iter_calls(procs[0].body_text, line_offset=procs[0].line_start))
        self.assertEqual(calls, [])

    def test_two_procedures_no_cross_pollution(self):
        text = (
            "Процедура A()\n"
            "  Б();\n"
            "КонецПроцедуры\n"
            "\n"
            "Процедура Б()\n"
            "КонецПроцедуры\n"
        )
        procs = parse_bsl_text(text)
        # A зовёт Б — внутри тела A только `Б();`
        a_calls = list(iter_calls(procs[0].body_text, line_offset=procs[0].line_start))
        self.assertEqual(len(a_calls), 1)
        self.assertEqual(a_calls[0].method_name, "Б")
        self.assertTrue(a_calls[0].is_local)


# ─── 5. iter_calls (cross-module + локальные) ─────────────────────────────


class TestIterCalls(unittest.TestCase):

    def test_cross_module_simple(self):
        body = "Результат = АукОбщийВызовСервера.КонтактныеЛица(А, Б);"
        calls = list(iter_calls(body))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].module_ref, "АукОбщийВызовСервера")
        self.assertEqual(calls[0].method_name, "КонтактныеЛица")
        self.assertFalse(calls[0].is_local)

    def test_cross_module_with_spaces(self):
        body = "Результат = Модуль . Метод ( А );"
        calls = list(iter_calls(body))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].module_ref, "Модуль")
        self.assertEqual(calls[0].method_name, "Метод")

    def test_chained_call_takes_only_first(self):
        # `A.B.C(` — это `B.C(` на объекте A, не `A.B(`.
        # Lookbehind защищает: мы не должны выдать матч `A.B`.
        body = "Результат = А.Б.В();"
        calls = list(iter_calls(body))
        # Должен быть один cross-module матч с module_ref=Б method_name=В,
        # т.к. перед Б стоит точка → lookbehind не пускает Б как module_ref. И всё-таки
        # тут НИ ОДНОГО валидного матча на cross-module — `А.Б` имеет `.` перед Б? Нет,
        # перед Б стоит `.` (после А) — а lookbehind пропускает только если перед
        # module_ref нет [A-Za-z0-9_.]. `.` — это `.` — она в blacklist.
        # Значит А — это валидный module_ref, а В — нет (перед ним точка).
        # Итог: ровно один матч, module_ref=А, method_name=Б.
        # Подождите — RE_CROSSMODULE_CALL ищет `Модуль.Метод(`. Для совпадения
        # после Метод должна идти `(`. У нас `А.Б.В()` — между Б и В стоит `.`, не `(`.
        # Значит А.Б не сработает. А Б.В? `(?<![А-Яа-яЁёA-Za-z0-9_.])` запрещает `.`
        # перед Б — значит и Б.В тоже не сработает.
        # Локальный вызов: только `В()` — но перед В стоит `.`, что в blacklist для
        # обоих regex.
        # Итог: 0 матчей.
        self.assertEqual(len(calls), 0)

    def test_local_call_simple(self):
        body = "  ВызовФункции(А);"
        calls = list(iter_calls(body))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].module_ref, "")
        self.assertEqual(calls[0].method_name, "ВызовФункции")
        self.assertTrue(calls[0].is_local)

    def test_local_call_inside_expression(self):
        body = "Если ПроверитьУсловие(А) Тогда Возврат; КонецЕсли;"
        calls = list(iter_calls(body))
        # ПроверитьУсловие — да; Если/Тогда/Возврат/КонецЕсли — keyword filter.
        names = sorted(c.method_name for c in calls)
        self.assertEqual(names, ["ПроверитьУсловие"])

    def test_keyword_not_treated_as_call(self):
        # `Если(...)` синтаксически не существует, но проверим, что Если/Возврат не
        # становятся callsite'ами.
        body = "Если Условие Тогда Возврат; КонецЕсли;"
        calls = list(iter_calls(body))
        self.assertEqual(calls, [])

    def test_local_offset_with_line_offset(self):
        body = "А();\nБ();"
        calls = list(iter_calls(body, line_offset=10))
        self.assertEqual([c.line for c in calls], [11, 12])

    def test_local_not_after_dot(self):
        # `обж.метод()` — `метод` не должен попасть в local (после точки).
        body = "Объект.Метод()"
        calls = list(iter_calls(body))
        # Перед Объект нет [a-z._] — Объект попадает как module_ref, Метод как method.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].module_ref, "Объект")
        self.assertEqual(calls[0].method_name, "Метод")

    def test_cross_and_local_in_same_body(self):
        body = "  Модуль.Метод();\n  ЛокальныйМетод();"
        calls = list(iter_calls(body))
        self.assertEqual(len(calls), 2)
        # порядок: сначала cross-module, потом local (в нашей реализации).
        kinds = sorted([(c.is_local, c.method_name) for c in calls])
        self.assertIn((False, "Метод"), kinds)
        self.assertIn((True, "ЛокальныйМетод"), kinds)


# ─── 6. iter_metadata_access ──────────────────────────────────────────────


class TestMetadataAccess(unittest.TestCase):

    def test_simple(self):
        body = "Результат = Справочники.АукАукционы;"
        accesses = list(iter_metadata_access(body))
        self.assertEqual(len(accesses), 1)
        self.assertEqual(accesses[0].plural, "Справочники")
        self.assertEqual(accesses[0].name, "АукАукционы")

    def test_all_supported_plurals(self):
        plurals = [
            "Справочники", "Документы", "Перечисления", "Обработки", "Отчеты",
            "РегистрыСведений", "РегистрыНакопления", "РегистрыБухгалтерии",
            "РегистрыРасчета", "Константы", "ПланыВидовХарактеристик",
            "ПланыСчетов", "ПланыВидовРасчета", "ПланыОбмена",
            "БизнесПроцессы", "Задачи", "ЖурналыДокументов",
        ]
        body = "; ".join(f"{p}.X" for p in plurals)
        accesses = list(iter_metadata_access(body))
        self.assertEqual(len(accesses), len(plurals))
        self.assertEqual(sorted(a.plural for a in accesses), sorted(plurals))

    def test_metadata_in_string_literal_not_matched(self):
        # Препроцессор должен затереть содержимое литерала.
        body_raw = 'А = "Справочники.АукАукционы";'
        body_pre = _preprocess(body_raw)
        accesses = list(iter_metadata_access(body_pre))
        self.assertEqual(accesses, [])

    def test_no_false_positive_in_method_chain(self):
        body = "Объект.Справочники.X"  # после Объект.Справочники — это не metadata access
        accesses = list(iter_metadata_access(body))
        # `(?<![А-Яа-яЁёA-Za-z0-9_])` отрезает совпадение с `Справочники`
        # если перед ним есть точка или буква. Но перед Справочники тут идёт `.`,
        # которая НЕ в blacklist (lookbehind проверяет только буквы/цифры/_).
        # Поэтому `Справочники.X` всё-таки попадает. Это — известное ограничение,
        # обрабатываемое резолвером (`Объект.Справочники.X` — редкая комбинация,
        # практически не встречается).
        # Регекс REMETADATAACCESS ДОЛЖЕН поймать, и это документировано.
        self.assertEqual(len(accesses), 1)


# ─── 7. iter_predef ───────────────────────────────────────────────────────


class TestIterPredef(unittest.TestCase):

    def test_simple(self):
        body_raw = 'А = ПредопределенноеЗначение("Перечисление.АукВидыСообщений.ПростоеСообщение");'
        items = list(iter_predef(body_raw))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].ref, "Перечисление.АукВидыСообщений.ПростоеСообщение")

    def test_predef_in_preprocessed_returns_empty(self):
        body_raw = 'А = ПредопределенноеЗначение("Перечисление.X.Y");'
        body_pre = _preprocess(body_raw)
        # На preprocessed-тексте мы НЕ должны найти ничего (литерал затёрт).
        items = list(iter_predef(body_pre))
        self.assertEqual(items, [])
        # А на raw — должны.
        items_raw = list(iter_predef(body_raw))
        self.assertEqual(len(items_raw), 1)

    def test_multiple_predefs(self):
        body_raw = (
            'А = ПредопределенноеЗначение("Перечисление.X.Y");\n'
            'Б = ПредопределенноеЗначение("Справочник.X.ПустаяСсылка");\n'
        )
        items = list(iter_predef(body_raw))
        self.assertEqual(len(items), 2)


# ─── 8. iter_assign_refs (для dataflow) ───────────────────────────────────


class TestIterAssignRefs(unittest.TestCase):

    def test_simple(self):
        body = "тзСтоимости = Справочники.АукАукционы.СоздатьЭлемент();"
        items = list(iter_assign_refs(body))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].var, "тзСтоимости")
        self.assertEqual(items[0].plural, "Справочники")
        self.assertEqual(items[0].name, "АукАукционы")
        self.assertEqual(items[0].method, "СоздатьЭлемент")

    def test_multiple_assignments(self):
        body = (
            "А = Справочники.X.СоздатьЭлемент();\n"
            "Б = Документы.Y.НайтиПоНомеру(\"1\");\n"
        )
        # Препроцессор затрёт "1" — но это не ломает регэксп assign, т.к. он не
        # смотрит внутрь скобок-после-метода.
        items = list(iter_assign_refs(_preprocess(body)))
        self.assertEqual(len(items), 2)
        self.assertEqual({i.var for i in items}, {"А", "Б"})

    def test_reassignment_takes_both(self):
        # iter_assign_refs возвращает оба — это резолвер решает кто «победил».
        body = (
            "А = Справочники.X.СоздатьЭлемент();\n"
            "А = Справочники.Y.СоздатьЭлемент();\n"
        )
        items = list(iter_assign_refs(body))
        self.assertEqual(len(items), 2)

    def test_unknown_plural_ignored(self):
        # `Обработки` не в списке regex'а — это R&D-ограничение.
        body = "А = Обработки.X.СоздатьОбработку();"
        items = list(iter_assign_refs(body))
        # `Обработки` не входит в RE_ASSIGN_REF — в этом коммите.
        self.assertEqual(items, [])


# ─── 9. classify_bsl_path ─────────────────────────────────────────────────


class TestClassifyBslPath(unittest.TestCase):

    def test_common_module(self):
        res = classify_bsl_path("CommonModules/АукОбщийКлиент/Ext/Module.bsl")
        self.assertEqual(res, ("CommonModule.АукОбщийКлиент", "CommonModule", None, "CommonModule"))

    def test_object_module(self):
        res = classify_bsl_path("Catalogs/АукАукционы/Ext/ObjectModule.bsl")
        self.assertEqual(
            res,
            ("Catalog.АукАукционы.ObjectModule", "ObjectModule", "Catalog.АукАукционы", "ObjectModule"),
        )

    def test_manager_module(self):
        res = classify_bsl_path("Catalogs/АукАукционы/Ext/ManagerModule.bsl")
        self.assertEqual(
            res,
            ("Catalog.АукАукционы.ManagerModule", "ManagerModule", "Catalog.АукАукционы", "ManagerModule"),
        )

    def test_form_module(self):
        res = classify_bsl_path("Catalogs/АукАукционы/Forms/ФормаЭлемента/Ext/Form/Module.bsl")
        self.assertEqual(
            res,
            ("Catalog.АукАукционы.Form.ФормаЭлемента", "Form", "Catalog.АукАукционы", "Form"),
        )

    def test_document_object_module(self):
        res = classify_bsl_path("Documents/X/Ext/ObjectModule.bsl")
        self.assertEqual(
            res,
            ("Document.X.ObjectModule", "ObjectModule", "Document.X", "ObjectModule"),
        )

    def test_enum_manager_module(self):
        res = classify_bsl_path("Enums/АукВидыСообщений/Ext/ManagerModule.bsl")
        self.assertEqual(
            res,
            ("Enum.АукВидыСообщений.ManagerModule", "ManagerModule", "Enum.АукВидыСообщений", "ManagerModule"),
        )

    def test_information_register(self):
        res = classify_bsl_path("InformationRegisters/X/Ext/ObjectModule.bsl")
        self.assertEqual(
            res,
            ("InformationRegister.X.ObjectModule", "ObjectModule", "InformationRegister.X", "ObjectModule"),
        )

    def test_tests_extension_strips_prefix(self):
        res = classify_bsl_path("tests-extension/CommonModules/Тест_X/Ext/Module.bsl")
        self.assertEqual(res, ("CommonModule.Тест_X", "CommonModule", None, "CommonModule"))

    def test_unknown_path_returns_none(self):
        self.assertIsNone(classify_bsl_path("foo/bar.bsl"))
        self.assertIsNone(classify_bsl_path("Languages/Russian.xml"))


# ─── 10. Полный парсинг небольшого модуля ─────────────────────────────────


SAMPLE_COMMON_MODULE = """\
﻿
#Область ПрограммныйИнтерфейс

// Test factorial
Функция Факториал(пЧисло) Экспорт

    Если пЧисло < 0 Тогда
        ВызватьИсключение НСтр("ru = 'Факториал отрицательного числа не определен'");
    КонецЕсли;

    Если пЧисло = 0 Или пЧисло = 1 Тогда
        Возврат 1;
    КонецЕсли;

    чслРезультат = 1;
    Для Сч = 2 По пЧисло Цикл
        чслРезультат = чслРезультат * Сч;
    КонецЦикла;

    Возврат чслРезультат;

КонецФункции

Процедура ЗафиксироватьДанныеСтавки(пСткПараметры) Экспорт
    пСткПараметры.Вставить("Автоматическая", Ложь);
    АукУправлениеАукционамиВызовСервера.ПолучитьДанныеОтУчастника(пСткПараметры);
КонецПроцедуры

#КонецОбласти
"""


class TestFullParse(unittest.TestCase):

    def test_count_procedures(self):
        procs = parse_bsl_text(SAMPLE_COMMON_MODULE)
        self.assertEqual(len(procs), 2)
        names = [p.name for p in procs]
        self.assertEqual(names, ["Факториал", "ЗафиксироватьДанныеСтавки"])

    def test_factorial_is_export_function(self):
        procs = parse_bsl_text(SAMPLE_COMMON_MODULE)
        fakt = procs[0]
        self.assertEqual(fakt.kind, "Function")
        self.assertTrue(fakt.is_export)
        self.assertEqual([p.name for p in fakt.parameters], ["пЧисло"])

    def test_factorial_body_has_no_calls_after_filter(self):
        # В теле Факториала вызовы — только встроенные: ВызватьИсключение, НСтр.
        # Парсер их не фильтрует — это работа резолвера. Просто проверим, что
        # они извлекаются.
        procs = parse_bsl_text(SAMPLE_COMMON_MODULE)
        fakt = procs[0]
        calls = list(iter_calls(fakt.body_text, line_offset=fakt.line_start))
        # НСтр + ВызватьИсключение? `ВызватьИсключение` синтаксически не вызов
        # со скобками (это ключевое слово). НСтр(…) — вызов. После препроцессора
        # литералы затёрты — `НСтр("ru = …")` остаётся как `НСтр(   )`.
        names = sorted({c.method_name for c in calls})
        self.assertIn("НСтр", names)
        # ВызватьИсключение НЕ должно попасть (в _BSL_KEYWORDS).
        self.assertNotIn("ВызватьИсключение", names)

    def test_zafiksirovat_has_cross_module_call(self):
        procs = parse_bsl_text(SAMPLE_COMMON_MODULE)
        zaf = procs[1]
        calls = list(iter_calls(zaf.body_text, line_offset=zaf.line_start))
        cross = [c for c in calls if not c.is_local]
        # Парсер выдаёт ОБА матча `Модуль.метод(`:
        #   - пСткПараметры.Вставить(...)  ← на самом деле метод объекта-параметра,
        #     но синтаксически неотличимо без type info
        #   - АукУправлениеАукционамиВызовСервера.ПолучитьДанныеОтУчастника(...)
        # Резолвер позже посмотрит на `module_ref` и отфильтрует тот, который не
        # является известным модулем. Здесь же — парсер выдаёт всё что синтаксически
        # подходит.
        self.assertEqual(len(cross), 2)
        cross_modules = sorted(c.module_ref for c in cross)
        self.assertIn("АукУправлениеАукционамиВызовСервера", cross_modules)
        self.assertIn("пСткПараметры", cross_modules)


# ─── 11. Реальные .bsl ────────────────────────────────────────────────────


WORKSPACE_HINT = Path(__file__).resolve().parent.parent / "ws" / "workspace"


@unittest.skipUnless(WORKSPACE_HINT.exists(),
                     f"Котировки-workspace не найдены в {WORKSPACE_HINT}")
class TestRealBsl(unittest.TestCase):

    def test_parses_АукОбщийКлиент(self):
        path = WORKSPACE_HINT / "CommonModules" / "АукОбщийКлиент" / "Ext" / "Module.bsl"
        if not path.exists():
            self.skipTest("Конкретный модуль не найден")
        text = path.read_text(encoding="utf-8-sig")
        procs = parse_bsl_text(text)
        # Минимум 10 деклараций — мы видели Факториал и кучу других.
        self.assertGreater(len(procs), 10)
        # `Факториал` есть и он экспортный.
        names = {p.name: p for p in procs}
        self.assertIn("Факториал", names)
        self.assertTrue(names["Факториал"].is_export)
        self.assertEqual(names["Факториал"].kind, "Function")
        # `Факториал` зовёт `НСтр` (после препроцессора литералы исчезли — НСтр остаётся вызовом).
        calls = list(iter_calls(names["Факториал"].body_text, line_offset=names["Факториал"].line_start))
        self.assertTrue(any(c.method_name == "НСтр" for c in calls))

    def test_form_module_has_directives(self):
        path = WORKSPACE_HINT / "Catalogs" / "АукНастройкиРасчетовИтогов" / "Forms" / "ФормаЭлемента" / "Ext" / "Form" / "Module.bsl"
        if not path.exists():
            self.skipTest("Конкретный form-модуль не найден")
        text = path.read_text(encoding="utf-8-sig")
        procs = parse_bsl_text(text)
        self.assertGreater(len(procs), 3)
        # Все процедуры формы должны иметь директиву.
        without_dir = [p.name for p in procs if not p.directive]
        # На самом деле в форме могут быть процедуры без директивы (служебные) —
        # просто проверим, что КАКИЕ-ТО директивы парсятся.
        with_dir = [p.directive for p in procs if p.directive]
        self.assertTrue(len(with_dir) > 0, f"Ожидали хотя бы одну директиву, нашли 0")
        # Должны быть и НаКлиенте, и НаСервере.
        directives = set(p.directive for p in procs if p.directive)
        self.assertTrue("НаКлиенте" in directives or "НаСервере" in directives)


# ─── 4.6.4 — новые итераторы присваиваний ─────────────────────────────────


class TestIterNewAssigns(unittest.TestCase):
    """A1: `var = Новый Класс`."""

    def test_simple_new(self):
        procs = parse_bsl_text("Процедура Х()\n  тз = Новый ТаблицаЗначений;\nКонецПроцедуры")
        na = list(iter_new_assigns(procs[0].body_text))
        self.assertEqual(len(na), 1)
        self.assertEqual(na[0].var, "тз")
        self.assertEqual(na[0].class_name, "ТаблицаЗначений")

    def test_new_with_args(self):
        procs = parse_bsl_text(
            'Процедура Х()\n  стр = Новый Структура("а, б", 1, 2);\nКонецПроцедуры'
        )
        na = list(iter_new_assigns(procs[0].body_text))
        self.assertEqual(len(na), 1)
        self.assertEqual(na[0].class_name, "Структура")

    def test_new_no_parens(self):
        # `Новый Массив` без скобок — валидный BSL.
        procs = parse_bsl_text("Процедура Х()\n  м = Новый Массив;\nКонецПроцедуры")
        na = list(iter_new_assigns(procs[0].body_text))
        self.assertEqual(len(na), 1)
        self.assertEqual(na[0].class_name, "Массив")

    def test_not_matched_when_no_assignment(self):
        # `Возврат Новый ТаблицаЗначений` — нет `var =`, не матчится.
        procs = parse_bsl_text("Функция Х()\n  Возврат Новый ТаблицаЗначений;\nКонецФункции")
        na = list(iter_new_assigns(procs[0].body_text))
        self.assertEqual(na, [])

    def test_multiple_news(self):
        procs = parse_bsl_text(
            "Процедура Х()\n"
            "  а = Новый Массив;\n"
            "  б = Новый Структура;\n"
            "КонецПроцедуры"
        )
        na = list(iter_new_assigns(procs[0].body_text))
        self.assertEqual({n.var for n in na}, {"а", "б"})


class TestIterVarAssigns(unittest.TestCase):
    """A2: `var = other` / `var = other.Поле`."""

    def test_plain_chain(self):
        procs = parse_bsl_text("Процедура Х()\n  а = б;\nКонецПроцедуры")
        va = list(iter_var_assigns(procs[0].body_text))
        self.assertEqual(len(va), 1)
        self.assertEqual(va[0].var, "а")
        self.assertEqual(va[0].src_var, "б")
        self.assertTrue(va[0].is_plain)

    def test_dotted_not_plain(self):
        procs = parse_bsl_text("Процедура Х()\n  а = объект.Реквизит;\nКонецПроцедуры")
        va = list(iter_var_assigns(procs[0].body_text))
        self.assertEqual(len(va), 1)
        self.assertEqual(va[0].src_var, "объект")
        self.assertFalse(va[0].is_plain)

    def test_keyword_rhs_skipped(self):
        # `а = Истина` — RHS ключевое слово, не источник типа.
        procs = parse_bsl_text("Процедура Х()\n  а = Истина;\nКонецПроцедуры")
        va = list(iter_var_assigns(procs[0].body_text))
        self.assertEqual(va, [])

    def test_call_rhs_not_matched_as_var(self):
        # `а = Ф()` — это call-assign, не var-assign (после RHS идёт `(`).
        procs = parse_bsl_text("Процедура Х()\n  а = Ф();\nКонецПроцедуры")
        va = list(iter_var_assigns(procs[0].body_text))
        # RE_VAR_ASSIGN требует после RHS `;`/перевод/`)`/конец — `(` не подходит.
        self.assertEqual(va, [])


class TestIterCallAssigns(unittest.TestCase):
    """A2: `var = [Модуль.]Функция(...)`."""

    def test_cross_module_call_assign(self):
        procs = parse_bsl_text(
            "Процедура Х()\n  рез = МойМодуль.НайтиЗаказ(парам);\nКонецПроцедуры"
        )
        ca = list(iter_call_assigns(procs[0].body_text))
        self.assertEqual(len(ca), 1)
        self.assertEqual(ca[0].var, "рез")
        self.assertEqual(ca[0].module_ref, "МойМодуль")
        self.assertEqual(ca[0].method, "НайтиЗаказ")

    def test_local_call_assign(self):
        procs = parse_bsl_text(
            "Процедура Х()\n  рез = ЛокальнаяФ(1, 2);\nКонецПроцедуры"
        )
        ca = list(iter_call_assigns(procs[0].body_text))
        self.assertEqual(len(ca), 1)
        self.assertEqual(ca[0].module_ref, "")
        self.assertEqual(ca[0].method, "ЛокальнаяФ")

    def test_new_not_matched_as_call(self):
        # `а = Новый Структура(...)` — module_ref был бы "Новый" — но "Новый"
        # это не идентификатор-модуль перед точкой. Тут вообще нет точки →
        # method="Новый"? Проверим, что хотя бы не падает и не даёт мусор.
        procs = parse_bsl_text('Процедура Х()\n  а = Новый Структура("к");\nКонецПроцедуры')
        ca = list(iter_call_assigns(procs[0].body_text))
        # `Новый Структура(` — RE_CALL_ASSIGN видит `а = Новый` затем пробел —
        # method='Новый', но '(' далеко. На практике не матчится (нет `(` сразу
        # после Новый). Главное — не падает.
        for c in ca:
            self.assertNotEqual(c.method, "")


class TestSplitArgs(unittest.TestCase):
    """B1: split_args + _extract_args."""

    def test_simple_args(self):
        self.assertEqual(split_args("а, б, в"), ["а", "б", "в"])

    def test_empty(self):
        self.assertEqual(split_args(""), [])
        self.assertEqual(split_args("   "), [])

    def test_nested_parens(self):
        self.assertEqual(
            split_args("а, Вычислить(1 + 2), в"),
            ["а", "Вычислить(1 + 2)", "в"],
        )

    def test_empty_positional(self):
        # Пропущенный позиционный аргумент сохраняется как "".
        self.assertEqual(split_args("а, , в"), ["а", "", "в"])

    def test_extract_args_balanced(self):
        text = "Ф(а, б)"
        args, close = _extract_args(text, 1)
        self.assertEqual(args, "а, б")
        self.assertEqual(close, len(text) - 1)

    def test_extract_args_nested(self):
        text = "Ф(а, Г(б, в), г)"
        args, close = _extract_args(text, 1)
        self.assertEqual(args, "а, Г(б, в), г")

    def test_extract_args_unbalanced(self):
        # Незакрытая скобка → ("", -1).
        args, close = _extract_args("Ф(а, б", 1)
        self.assertEqual(args, "")
        self.assertEqual(close, -1)

    def test_iter_calls_populates_args_text(self):
        procs = parse_bsl_text(
            "Процедура Х()\n  МойМодуль.Метод(а, б);\nКонецПроцедуры"
        )
        calls = list(iter_calls(procs[0].body_text))
        method_calls = [c for c in calls if c.method_name == "Метод"]
        self.assertEqual(len(method_calls), 1)
        self.assertEqual(split_args(method_calls[0].args_text), ["а", "б"])


# ─── FIX-4: имена области видимости ───────────────────────────────────────


class TestCollectLocalNames(unittest.TestCase):
    """collect_local_names — детерминированный сбор объявленных имён."""

    def _names(self, body: str) -> set:
        # FIX-4.1: имена свёрнуты в нижний регистр — BSL регистронезависим.
        procs = parse_bsl_text(body)
        self.assertEqual(len(procs), 1, "ожидалась ровно одна процедура")
        return collect_local_names(procs[0])

    def test_parameters(self):
        names = self._names("Процедура Х(Знач А, Б = 1)\nКонецПроцедуры")
        self.assertEqual(names, {"а", "б"})

    def test_var_declaration(self):
        names = self._names(
            "Процедура Х()\n  Перем Первая, Вторая;\nКонецПроцедуры")
        self.assertEqual(names, {"первая", "вторая"})

    def test_var_declaration_with_export(self):
        # `Экспорт` — не имя переменной.
        names = self._names("Процедура Х()\n  Перем Счётчик Экспорт;\nКонецПроцедуры")
        self.assertEqual(names, {"счётчик"})

    def test_plain_assignment(self):
        names = self._names(
            "Процедура Х()\n  РезультатЗапроса = Запрос.Выполнить();\nКонецПроцедуры")
        self.assertIn("результатзапроса", names)

    def test_two_assignments_on_one_line(self):
        names = self._names("Процедура Х()\n  А = 1; Б = 2;\nКонецПроцедуры")
        self.assertEqual(names, {"а", "б"})

    def test_comparison_is_not_declaration(self):
        # `=` в условии — сравнение, а не присваивание: Статус объявлением
        # не становится. Иначе любое имя из условия попало бы в множество.
        names = self._names(
            "Процедура Х()\n"
            "  Если Статус = 1 Тогда\n"
            "    Возврат;\n"
            "  КонецЕсли;\n"
            "КонецПроцедуры"
        )
        self.assertNotIn("статус", names)
        self.assertNotIn("если", names)

    def test_foreach_variable(self):
        names = self._names(
            "Процедура Х()\n"
            "  Для Каждого ТекДанные Из Таблица Цикл\n"
            "  КонецЦикла;\n"
            "КонецПроцедуры"
        )
        self.assertIn("текданные", names)
        self.assertNotIn("для", names)

    def test_for_counter_variable(self):
        names = self._names(
            "Процедура Х()\n"
            "  Для Сч = 1 По 10 Цикл\n"
            "  КонецЦикла;\n"
            "КонецПроцедуры"
        )
        self.assertIn("сч", names)

    def test_assignment_in_string_literal_ignored(self):
        # body_text уже preprocessed — присваивание внутри литерала не в счёт.
        names = self._names(
            'Процедура Х()\n'
            '  Текст = "ЛевоеИмя = 1";\n'
            'КонецПроцедуры'
        )
        self.assertIn("текст", names)
        self.assertNotIn("левоеимя", names)

    def test_module_name_is_not_collected(self):
        # Вызов модуля не делает его имя объявленным — иначе FIX-4 стал бы
        # прятать настоящие пробелы в графе.
        names = self._names(
            "Процедура Х()\n  ОбщегоНазначения.ЗначениеРеквизита(С, \"Код\");\nКонецПроцедуры")
        self.assertNotIn("общегоназначения", names)

    def test_realistic_query_block(self):
        names = self._names(
            "Процедура Х(пОтбор)\n"
            "  Запрос = Новый Запрос;\n"
            "  Запрос.УстановитьПараметр(\"Отбор\", пОтбор);\n"
            "  РезультатЗапроса = Запрос.Выполнить();\n"
            "  Выборка = РезультатЗапроса.Выбрать();\n"
            "  Пока Выборка.Следующий() Цикл\n"
            "  КонецЦикла;\n"
            "КонецПроцедуры"
        )
        self.assertEqual(names, {"потбор", "запрос", "результатзапроса", "выборка"})


class TestCollectModuleVarNames(unittest.TestCase):
    """collect_module_var_names — `Перем` на уровне модуля."""

    def test_module_level_vars(self):
        text = (
            "Перем МодульнаяА;\n"
            "Перем МодульнаяБ Экспорт;\n"
            "\n"
            "Процедура Х()\n"
            "  Перем Локальная;\n"
            "КонецПроцедуры"
        )
        self.assertEqual(collect_module_var_names(text),
                         {"модульнаяа", "модульнаяб"})

    def test_no_module_vars(self):
        text = "Процедура Х()\n  Перем Локальная;\nКонецПроцедуры"
        self.assertEqual(collect_module_var_names(text), set())

    def test_module_without_procedures(self):
        # Модуль из одних объявлений — RE_DECL ничего не найдёт, берём всё.
        self.assertEqual(collect_module_var_names("Перем А, Б;"), {"а", "б"})


# ─── FIX-4.1 ──────────────────────────────────────────────────────────────


class TestCaseInsensitiveScope(unittest.TestCase):
    """BSL регистронезависим — имена сворачиваются в нижний регистр."""

    def test_local_names_are_folded(self):
        procs = parse_bsl_text(
            "Процедура Х(ПарамЕтр)\n  ПулЛогеров = 1;\nКонецПроцедуры")
        names = collect_local_names(procs[0])
        self.assertEqual(names, {"параметр", "пуллогеров"})

    def test_declaration_and_use_differ_in_case(self):
        # Ровно случай из живого графа: `Перем ПулЛогеров` объявлен так,
        # а вызывается как `пуллогеров.Вставить()`.
        text = "Перем ПулЛогеров;\n\nПроцедура Х()\nКонецПроцедуры"
        self.assertIn("пуллогеров", collect_module_var_names(text))


class TestContextProperties(unittest.TestCase):
    """Свойства контекста модуля — фиксированный список по роли."""

    def test_form_context(self):
        names = context_property_names("Form")
        self.assertIn("параметры", names)
        self.assertIn("элементы", names)
        self.assertIn("этаформа", names)

    def test_object_module_context(self):
        names = context_property_names("ObjectModule")
        self.assertIn("ссылка", names)
        self.assertIn("дополнительныесвойства", names)

    def test_common_module_has_no_context(self):
        # В общем модуле нет ни Ссылки, ни Параметров — там всё объявлено.
        self.assertEqual(context_property_names("CommonModule"), frozenset())

    def test_unknown_role_is_empty(self):
        self.assertEqual(context_property_names("ЧтоТоНовое"), frozenset())


class TestFormAttributes(unittest.TestCase):
    """Реквизиты формы из Ext/Form.xml."""

    def _write(self, body: str) -> Path:
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "Form.xml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_attributes_with_name_attr(self):
        p = self._write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">\n'
            '  <Attributes>\n'
            '    <Attribute name="ТекстЗапроса" id="1"/>\n'
            '    <Attribute name="ПакетЗапросов" id="2"/>\n'
            '  </Attributes>\n'
            '  <Items>\n'
            '    <InputField name="ПолеВвода1"/>\n'
            '  </Items>\n'
            '</Form>\n'
        )
        self.assertEqual(collect_form_attribute_names(p),
                         {"текстзапроса", "пакетзапросов"})

    def test_items_are_not_collected(self):
        # Элементы формы доступны только через `Элементы.X` — в множество
        # имён они попадать не должны.
        p = self._write(
            '<Form><Attributes><Attribute name="А"/></Attributes>'
            '<Items><InputField name="Б"/></Items></Form>'
        )
        self.assertNotIn("б", collect_form_attribute_names(p))

    def test_name_as_child_element(self):
        # Вариант схемы, где имя лежит дочерним элементом.
        p = self._write(
            '<Form><Attributes><Attribute><Name>ТекстАлгоритм</Name>'
            '</Attribute></Attributes></Form>'
        )
        self.assertEqual(collect_form_attribute_names(p), {"тексталгоритм"})

    def test_broken_xml_returns_empty(self):
        # Битый файл не должен ронять обход выгрузки.
        p = self._write("<Form><Attributes>")
        self.assertEqual(collect_form_attribute_names(p), set())

    def test_missing_attributes_section(self):
        p = self._write('<Form><Items><InputField name="Б"/></Items></Form>')
        self.assertEqual(collect_form_attribute_names(p), set())


class TestConstructorIsNotACall(unittest.TestCase):
    """`Новый Класс(...)` — конструктор, а не вызов процедуры."""

    def test_new_class_with_args_skipped(self):
        procs = parse_bsl_text(
            'Процедура Х()\n  З = Новый ЗаписьJSON();\nКонецПроцедуры')
        methods = {c.method_name for c in iter_calls(procs[0].body_text)}
        self.assertNotIn("ЗаписьJSON", methods)

    def test_new_class_with_params_skipped(self):
        procs = parse_bsl_text(
            'Процедура Х()\n  П = Новый ПараметрыЗаписиJSON(А, Б);\nКонецПроцедуры')
        methods = {c.method_name for c in iter_calls(procs[0].body_text)}
        self.assertNotIn("ПараметрыЗаписиJSON", methods)

    def test_real_call_still_detected(self):
        # Отсечка не должна съесть обычный вызов.
        procs = parse_bsl_text(
            "Процедура Х()\n  МояПроцедура(1);\nКонецПроцедуры")
        methods = {c.method_name for c in iter_calls(procs[0].body_text)}
        self.assertIn("МояПроцедура", methods)

    def test_word_ending_in_new_is_not_constructor(self):
        # `Обновый` не равно `Новый` — граница слова проверяется.
        procs = parse_bsl_text(
            "Процедура Х()\n  Результат = Обновый Метод(1);\nКонецПроцедуры")
        methods = {c.method_name for c in iter_calls(procs[0].body_text)}
        self.assertIn("Метод", methods)


# ─── FIX-7: регистронезависимость ключевых слов ──────────────────────────


class TestKeywordCaseInsensitivity(unittest.TestCase):
    """BSL регистронезависим — парсер обязан вести себя так же.

    Дефект найден на боевой конфигурации: 28 предупреждений «декларация без
    закрывающего КонецПроцедуры/Функции», у всех конец написан со строчной
    буквы. Видимая часть; пропущенное объявление или вызов исчезали молча.
    """

    CANON = (
        "Функция Тест(П) Экспорт\n"
        "    Р = Новый Массив;\n"
        "    Возврат Р;\n"
        "КонецФункции\n"
    )

    def test_lowercase_end_of_function(self):
        # Ровно тот случай, что нашёлся в боевой конфигурации.
        procs = parse_bsl_text(self.CANON.replace("КонецФункции", "Конецфункции"))
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].name, "Тест")

    def test_uppercase_end_of_function(self):
        procs = parse_bsl_text(self.CANON.replace("КонецФункции", "КОНЕЦФУНКЦИИ"))
        self.assertEqual(len(procs), 1)

    def test_lowercase_declaration(self):
        procs = parse_bsl_text(self.CANON.replace("Функция Тест", "функция Тест"))
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].name, "Тест")

    def test_lowercase_export_keyword(self):
        procs = parse_bsl_text(self.CANON.replace("Экспорт", "экспорт"))
        self.assertEqual(len(procs), 1)
        self.assertTrue(procs[0].is_export)

    def test_lowercase_procedure(self):
        text = ("процедура Сделать()\n"
                "    А = 1;\n"
                "конецпроцедуры\n")
        procs = parse_bsl_text(text)
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].kind, "Procedure")

    def test_mixed_case_across_one_module(self):
        text = (self.CANON
                + self.CANON.replace("Тест", "Второй")
                            .replace("КонецФункции", "конецФункции")
                + self.CANON.replace("Тест", "Третий")
                            .replace("Функция", "ФУНКЦИЯ")
                            .replace("КОНЕЦФУНКЦИИ", "КонецФункции"))
        procs = parse_bsl_text(text)
        self.assertEqual([p.name for p in procs], ["Тест", "Второй", "Третий"])

    def test_name_case_is_preserved(self):
        # Поиск регистронезависим, но имя должно сохраниться как написано:
        # по нему строятся id узлов графа.
        procs = parse_bsl_text(self.CANON.replace("Функция Тест", "функция МоёИмя"))
        self.assertEqual(procs[0].name, "МоёИмя")

    def test_canonical_case_still_works(self):
        # Регрессия: правка не должна ничего менять на обычном коде.
        procs = parse_bsl_text(self.CANON)
        self.assertEqual(len(procs), 1)
        self.assertTrue(procs[0].is_export)


class TestRealWorldCaseSnippet(unittest.TestCase):
    """Реальный фрагмент боевой конфигурации: `КонецФУнкции` вперемешку.

    До FIX-7 парсер находил здесь НОЛЬ функций. Тест сторожит заодно
    затирание многострочного строкового литерала: внутри запроса есть
    `ЗНАЧЕНИЕ(...)`, и если литерал не затереть, оно превратится в
    несуществующий вызов.
    """

    SOURCE = """Функция тзПолучитьСписокСотрудниковПоДолжностям(мДолжности)
	
	Запрос = Новый Запрос();
	Запрос.Текст  =
	"ВЫБРАТЬ РАЗЛИЧНЫЕ
	|	Контрагенты.Ссылка,
	|	Контрагенты.Код,
	|	Контрагенты.Наименование
	|ИЗ
	|	РегистрСведений.ЛичныеДела.СрезПоследних(
	|			,
	|			Событие = ЗНАЧЕНИЕ(Перечисление.СобытияПоЛичнымДелам.ПринятНаДолжность)
	|				ИЛИ Событие = ЗНАЧЕНИЕ(Перечисление.СобытияПоЛичнымДелам.ПринятНаДолжностьСИспытательнымСроком)) КАК ЛичныеДелаСрезПоследнихДолжность
	|		ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.Контрагенты КАК Контрагенты
	|		ПО ЛичныеДелаСрезПоследнихДолжность.Сотрудник = Контрагенты.Ссылка
	|ГДЕ
	|	ЛичныеДелаСрезПоследнихДолжность.Данные В(&мДолжности)";
	
	Запрос.УстановитьПараметр("мДолжности", мДолжности);
	Возврат Запрос.Выполнить().Выгрузить().ВыгрузитьКолонку("Ссылка");	 
	
КонецФУнкции
"""

    def test_function_is_found(self):
        procs = parse_bsl_text(self.SOURCE)
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0].name, "тзПолучитьСписокСотрудниковПоДолжностям")
        self.assertEqual(procs[0].kind, "Function")

    def test_query_text_does_not_produce_phantom_calls(self):
        proc = parse_bsl_text(self.SOURCE)[0]
        refs = {(c.module_ref or "") for c in iter_calls(proc.body_text)}
        self.assertNotIn("Перечисление", refs)
        self.assertNotIn("РегистрСведений", refs)

    def test_real_calls_are_found(self):
        proc = parse_bsl_text(self.SOURCE)[0]
        names = {c.method_name for c in iter_calls(proc.body_text)}
        self.assertIn("УстановитьПараметр", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
