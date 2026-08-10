"""
Тесты парсера запросов и сверки с метаданными (FEAT-1 / FEAT-2 / QOPT-1).

    cd 1c-mcp-suite/mcp-query-builder
    python -m unittest tests_query_parser

Ни FastMCP, ни Neo4j не требуются: провайдер метаданных здесь фейковый,
собран из тех же структур, что отдаёт граф.
"""
import unittest

from query_parser import parse_batch, tokenize, TOK_STRING, TOK_PARAM
from query_check import (AttrInfo, MetadataProvider, ObjectInfo,
                         check_query, resolve_source, standard_fields_for,
                         temp_table_columns)
from query_optimize_rules import analyze


# ─── Фейковый провайдер ──────────────────────────────────────────────────

# id узла в графе — английский (`Catalog.Номенклатура`), именно на него
# ссылается target у ссылочного типа. kind_ru — как пишут в запросе.
_KIND_ENG = {
    "Справочник": "Catalog",
    "Документ": "Document",
    "Перечисление": "Enum",
    "РегистрСведений": "InformationRegister",
    "РегистрНакопления": "AccumulationRegister",
    "РегистрБухгалтерии": "AccountingRegister",
}


def _obj(kind, name, attrs=(), ts=None):
    return ObjectInfo(
        id=f"{_KIND_ENG[kind]}.{name}", name=name, kind_ru=kind,
        attributes=[AttrInfo(*a) if isinstance(a, tuple) else a for a in attrs],
        tabular_sections=ts or {},
    )


NOMENKLATURA = _obj(
    "Справочник", "Номенклатура",
    attrs=[
        AttrInfo("Артикул", "attribute", [("String", None)]),
        AttrInfo("БазоваяЕдиница", "attribute",
                 [("CatalogRef", "Catalog.ЕдиницыИзмерения")]),
        AttrInfo("Поставщик", "attribute",
                 [("CatalogRef", "Catalog.Контрагенты"),
                  ("CatalogRef", "Catalog.Организации")]),
    ],
    ts={"ДопРеквизиты": [AttrInfo("Свойство", "attribute", [("String", None)]),
                         AttrInfo("Значение", "attribute", [("String", None)])]},
)
EDINICY = _obj("Справочник", "ЕдиницыИзмерения",
               attrs=[AttrInfo("Коэффициент", "attribute", [("Number", None)])])
KONTRAGENTY = _obj("Справочник", "Контрагенты",
                   attrs=[AttrInfo("ИНН", "attribute", [("String", None)])])
TOVARY = _obj(
    "РегистрНакопления", "ТоварыНаСкладах",
    attrs=[
        AttrInfo("Номенклатура", "dimension",
                 [("CatalogRef", "Catalog.Номенклатура")]),
        AttrInfo("Склад", "dimension", [("CatalogRef", "Catalog.Склады")]),
        AttrInfo("Количество", "resource", [("Number", None)]),
    ],
)
CENY = _obj(
    "РегистрСведений", "ЦеныНоменклатуры",
    attrs=[
        AttrInfo("Номенклатура", "dimension",
                 [("CatalogRef", "Catalog.Номенклатура")]),
        AttrInfo("Цена", "resource", [("Number", None)]),
        AttrInfo("Источник", "attribute", [("String", None)]),
    ],
)
PROVODKI = _obj("РегистрБухгалтерии", "Хозрасчетный",
                attrs=[AttrInfo("Сумма", "resource", [("Number", None)])])

ALL_OBJECTS = [NOMENKLATURA, EDINICY, KONTRAGENTY, TOVARY, CENY, PROVODKI]


class FakeProvider(MetadataProvider):
    def __init__(self, objects=ALL_OBJECTS):
        self.by_id = {o.id: o for o in objects}
        self.by_kind = {(o.kind_ru.lower(), o.name.lower()): o for o in objects}

    def get(self, kind_ru, name):
        return self.by_kind.get((kind_ru.lower(), name.lower()))

    def get_by_id(self, full_name_eng):
        return self.by_id.get(full_name_eng)

    def suggest(self, name, limit=3):
        low = name.lower()
        return sorted(f"{o.kind_ru}.{o.name}" for o in self.by_id.values()
                      if low[:4] in o.name.lower())[:limit]


P = FakeProvider()


def errors(text, provider=P):
    return check_query(text, provider)["errors"]


def is_valid(text, provider=P):
    return check_query(text, provider)["valid"]


# ─── Лексер ──────────────────────────────────────────────────────────────

class TestTokenizer(unittest.TestCase):

    def test_comment_is_dropped(self):
        toks = tokenize("ВЫБРАТЬ // это комментарий\n Т.А")
        self.assertNotIn("комментарий", [t.text for t in toks])

    def test_string_literal_is_one_token(self):
        toks = tokenize('ВЫБРАТЬ "текст с ИЗ и ГДЕ внутри"')
        strings = [t for t in toks if t.kind == TOK_STRING]
        self.assertEqual(len(strings), 1)

    def test_doubled_quote_inside_string(self):
        toks = tokenize('ВЫБРАТЬ "он сказал ""да"" вчера" КАК Т')
        strings = [t for t in toks if t.kind == TOK_STRING]
        self.assertEqual(len(strings), 1)
        self.assertIn("да", strings[0].text)

    def test_unterminated_string_does_not_crash(self):
        toks = tokenize('ВЫБРАТЬ "не закрыта')
        self.assertTrue(toks)

    def test_parameter_token(self):
        toks = tokenize("ГДЕ Т.Дата > &НачалоПериода")
        params = [t for t in toks if t.kind == TOK_PARAM]
        self.assertEqual([p.text for p in params], ["&НачалоПериода"])

    def test_line_numbers(self):
        toks = tokenize("ВЫБРАТЬ\n\nТ.Поле")
        last = toks[-1]
        self.assertEqual(last.line, 3)

    def test_two_char_operators(self):
        toks = tokenize("ГДЕ А <> Б И В >= Г")
        self.assertIn("<>", [t.text for t in toks])
        self.assertIn(">=", [t.text for t in toks])


# ─── Структура запроса ───────────────────────────────────────────────────

class TestParseStructure(unittest.TestCase):

    def test_simple_select(self):
        b = parse_batch("ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т")
        self.assertEqual(len(b.statements), 1)
        st = b.statements[0]
        self.assertEqual(st.kind, "select")
        self.assertEqual(st.main.sources[0].parts, ["Справочник", "Номенклатура"])
        self.assertEqual(st.main.sources[0].alias, "Т")

    def test_batch_split_by_semicolon(self):
        b = parse_batch("ВЫБРАТЬ 1 КАК А; ВЫБРАТЬ 2 КАК Б")
        self.assertEqual(len(b.statements), 2)

    def test_semicolon_inside_string_does_not_split(self):
        b = parse_batch('ВЫБРАТЬ "а;б" КАК А ИЗ Справочник.Номенклатура КАК Т')
        self.assertEqual(len(b.statements), 1)

    def test_distinct_and_first(self):
        st = parse_batch(
            "ВЫБРАТЬ РАЗЛИЧНЫЕ ПЕРВЫЕ 5 Т.Код ИЗ Справочник.Номенклатура КАК Т"
        ).statements[0]
        self.assertTrue(st.main.distinct)
        self.assertEqual(st.main.first_n, 5)

    def test_place_into_temp_table(self):
        st = parse_batch(
            "ВЫБРАТЬ Т.Код КАК Код ПОМЕСТИТЬ ВТКоды ИЗ Справочник.Номенклатура КАК Т"
        ).statements[0]
        self.assertEqual(st.temp_table, "ВТКоды")

    def test_temp_table_name_is_not_a_field_ref(self):
        st = parse_batch(
            "ВЫБРАТЬ Т.Код КАК Код ПОМЕСТИТЬ ВТКоды ИЗ Справочник.Номенклатура КАК Т"
        ).statements[0]
        self.assertNotIn("ВТКоды", [r.text for r in st.main.refs])

    def test_drop_statement(self):
        st = parse_batch("УНИЧТОЖИТЬ ВТКоды").statements[0]
        self.assertEqual(st.kind, "drop")
        self.assertEqual(st.drop_table, "ВТКоды")

    def test_join_is_parsed(self):
        st = parse_batch("""
            ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т
            ЛЕВОЕ СОЕДИНЕНИЕ Справочник.ЕдиницыИзмерения КАК Е
                ПО Т.БазоваяЕдиница = Е.Ссылка
        """).statements[0]
        self.assertEqual(len(st.main.sources), 2)
        self.assertIn("СОЕДИНЕНИЕ", st.main.sources[1].join_type)
        self.assertEqual(st.main.sources[1].alias, "Е")

    def test_join_condition_refs_collected(self):
        st = parse_batch("""
            ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т
            ВНУТРЕННЕЕ СОЕДИНЕНИЕ Справочник.ЕдиницыИзмерения КАК Е
                ПО Т.БазоваяЕдиница = Е.Ссылка
        """).statements[0]
        join_refs = [r.text for r in st.main.refs if r.section == "join"]
        self.assertIn("Т.БазоваяЕдиница", join_refs)
        self.assertIn("Е.Ссылка", join_refs)

    def test_virtual_table_with_params(self):
        st = parse_batch(
            "ВЫБРАТЬ Т.Склад ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки"
            "(&Дата, Склад = &Склад) КАК Т"
        ).statements[0]
        src = st.main.sources[0]
        self.assertEqual(src.parts[-1], "Остатки")
        self.assertTrue(src.has_params)

    def test_virtual_table_without_params(self):
        st = parse_batch(
            "ВЫБРАТЬ Т.Склад ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки КАК Т"
        ).statements[0]
        self.assertFalse(st.main.sources[0].has_params)

    def test_subquery_as_source(self):
        st = parse_batch("""
            ВЫБРАТЬ П.Код ИЗ
                (ВЫБРАТЬ Т.Код КАК Код ИЗ Справочник.Номенклатура КАК Т) КАК П
        """).statements[0]
        self.assertEqual(st.main.sources[0].kind, "subquery")
        self.assertEqual(st.main.sources[0].alias, "П")

    def test_union_creates_two_parts(self):
        st = parse_batch("""
            ВЫБРАТЬ Т.Код КАК Код ИЗ Справочник.Номенклатура КАК Т
            ОБЪЕДИНИТЬ ВСЕ
            ВЫБРАТЬ Е.Ссылка КАК Код ИЗ Справочник.ЕдиницыИзмерения КАК Е
        """).statements[0]
        self.assertEqual(len(st.parts), 2)

    def test_where_marker(self):
        st = parse_batch(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.Артикул = &А"
        ).statements[0]
        self.assertTrue(st.main.has_where)

    def test_unbalanced_parens_reported(self):
        b = parse_batch("ВЫБРАТЬ Т.Код ИЗ (ВЫБРАТЬ 1 КАК Код КАК Т")
        self.assertTrue(b.parse_errors)

    def test_garbage_statement_reported(self):
        b = parse_batch("УДАЛИТЬ ВСЁ")
        self.assertTrue(b.parse_errors)
        self.assertEqual(b.statements, [])


# ─── Извлечение обращений к полям ────────────────────────────────────────

class TestFieldRefs(unittest.TestCase):

    def _refs(self, text):
        return [r.text for r in parse_batch(text).statements[0].main.refs]

    def test_alias_after_kak_is_not_a_field(self):
        refs = self._refs(
            "ВЫБРАТЬ Т.Код КАК МойКод ИЗ Справочник.Номенклатура КАК Т")
        self.assertIn("Т.Код", refs)
        self.assertNotIn("МойКод", refs)

    def test_function_name_is_not_a_field(self):
        refs = self._refs(
            "ВЫБРАТЬ СУММА(Т.Артикул) ИЗ Справочник.Номенклатура КАК Т")
        self.assertIn("Т.Артикул", refs)
        self.assertNotIn("СУММА", refs)

    def test_value_function_content_skipped(self):
        refs = self._refs(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т "
            "ГДЕ Т.Артикул = ЗНАЧЕНИЕ(Перечисление.Статусы.Новый)")
        self.assertNotIn("Перечисление.Статусы.Новый", refs)

    def test_cast_type_is_not_a_field(self):
        refs = self._refs(
            "ВЫБРАТЬ ВЫРАЗИТЬ(Т.Поставщик КАК Справочник.Контрагенты).ИНН "
            "ИЗ Справочник.Номенклатура КАК Т")
        self.assertNotIn("Справочник.Контрагенты", refs)

    def test_three_level_path(self):
        refs = self._refs(
            "ВЫБРАТЬ Т.БазоваяЕдиница.Коэффициент ИЗ Справочник.Номенклатура КАК Т")
        self.assertIn("Т.БазоваяЕдиница.Коэффициент", refs)

    def test_keywords_are_not_fields(self):
        refs = self._refs(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т "
            "ГДЕ Т.Артикул ЕСТЬ НЕ NULL И ИСТИНА")
        for kw in ("ЕСТЬ", "NULL", "ИСТИНА", "И", "НЕ"):
            self.assertNotIn(kw, refs)


# ─── FEAT-2: временные таблицы ───────────────────────────────────────────

class TestTempTables(unittest.TestCase):

    BATCH = """
    ВЫБРАТЬ
        Т.Ссылка КАК Номенклатура,
        Т.Артикул КАК Артикул
    ПОМЕСТИТЬ ВТНоменклатура
    ИЗ Справочник.Номенклатура КАК Т
    ;
    ВЫБРАТЬ В.Номенклатура, В.Артикул ИЗ ВТНоменклатура КАК В
    """

    def test_columns_from_place_section(self):
        cols = temp_table_columns(self.BATCH)
        self.assertEqual(cols["ВТНоменклатура"], ["Номенклатура", "Артикул"])

    def test_reading_temp_table_is_valid(self):
        self.assertTrue(is_valid(self.BATCH))

    def test_unknown_column_of_temp_table_is_error(self):
        text = self.BATCH.replace("В.Артикул ИЗ", "В.НетТакой ИЗ")
        self.assertIn("НетТакой", " ".join(errors(text)))

    def test_undeclared_temp_table_is_error(self):
        text = "ВЫБРАТЬ В.Поле ИЗ ВТНеОбъявлена КАК В"
        self.assertIn("не объявлена", " ".join(errors(text)))

    def test_temp_table_name_is_case_insensitive(self):
        text = self.BATCH.replace("ИЗ ВТНоменклатура КАК В", "ИЗ втноменклатура КАК В")
        self.assertTrue(is_valid(text))

    def test_column_name_falls_back_to_last_path_segment(self):
        cols = temp_table_columns(
            "ВЫБРАТЬ Т.Артикул ПОМЕСТИТЬ ВТ ИЗ Справочник.Номенклатура КАК Т")
        self.assertEqual(cols["ВТ"], ["Артикул"])

    def test_expression_without_alias_makes_columns_unknown(self):
        cols = temp_table_columns(
            "ВЫБРАТЬ СУММА(Т.Артикул) ПОМЕСТИТЬ ВТ ИЗ Справочник.Номенклатура КАК Т")
        self.assertIsNone(cols["ВТ"])

    def test_unknown_columns_suppress_field_errors(self):
        text = ("ВЫБРАТЬ СУММА(Т.Артикул) ПОМЕСТИТЬ ВТ "
                "ИЗ Справочник.Номенклатура КАК Т;"
                "ВЫБРАТЬ В.ЧтоУгодно ИЗ ВТ КАК В")
        self.assertEqual(errors(text), [])

    def test_unknown_columns_produce_warning(self):
        text = ("ВЫБРАТЬ СУММА(Т.Артикул) ПОМЕСТИТЬ ВТ "
                "ИЗ Справочник.Номенклатура КАК Т")
        self.assertTrue(check_query(text, P)["warnings"])

    def test_star_makes_columns_unknown(self):
        cols = temp_table_columns(
            "ВЫБРАТЬ * ПОМЕСТИТЬ ВТ ИЗ Справочник.Номенклатура КАК Т")
        self.assertIsNone(cols["ВТ"])

    def test_drop_removes_temp_table(self):
        text = (self.BATCH.rsplit(";", 1)[0] + "; УНИЧТОЖИТЬ ВТНоменклатура;"
                " ВЫБРАТЬ В.Артикул ИЗ ВТНоменклатура КАК В")
        self.assertIn("не объявлена", " ".join(errors(text)))

    def test_drop_of_unknown_table_warns(self):
        res = check_query("УНИЧТОЖИТЬ ВТНикогдаНеБыло", P)
        self.assertTrue(res["warnings"])

    def test_columns_of_union_taken_from_first_part(self):
        cols = temp_table_columns("""
            ВЫБРАТЬ Т.Артикул КАК А ПОМЕСТИТЬ ВТ ИЗ Справочник.Номенклатура КАК Т
            ОБЪЕДИНИТЬ ВСЕ
            ВЫБРАТЬ Е.Коэффициент КАК Б ИЗ Справочник.ЕдиницыИзмерения КАК Е
        """)
        self.assertEqual(cols["ВТ"], ["А"])


# ─── FEAT-1: проверка полей ──────────────────────────────────────────────

class TestFieldValidation(unittest.TestCase):

    def test_valid_query_has_no_errors(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Ссылка, Т.Артикул ИЗ Справочник.Номенклатура КАК Т"))

    def test_missing_field_is_error(self):
        self.assertIn("Артикулл", " ".join(errors(
            "ВЫБРАТЬ Т.Артикулл ИЗ Справочник.Номенклатура КАК Т")))

    def test_missing_field_suggests_close_name(self):
        msg = " ".join(errors(
            "ВЫБРАТЬ Т.Артикулл ИЗ Справочник.Номенклатура КАК Т"))
        self.assertIn("Возможно", msg)

    def test_field_check_is_case_insensitive(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.артикул ИЗ Справочник.Номенклатура КАК Т"))

    def test_standard_field_accepted(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.ПометкаУдаления, Т.Код ИЗ Справочник.Номенклатура КАК Т"))

    def test_missing_table_is_error(self):
        self.assertIn("не найдена", " ".join(errors(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.НетТакого КАК Т")))

    def test_missing_table_does_not_cascade_into_alias_error(self):
        errs = errors("ВЫБРАТЬ Т.Код, Т.Поле2 ИЗ Справочник.НетТакого КАК Т")
        self.assertEqual(len(errs), 1)

    def test_unknown_alias_is_error(self):
        self.assertIn("не объявлен", " ".join(errors(
            "ВЫБРАТЬ Х.Код ИЗ Справочник.Номенклатура КАК Т")))

    def test_field_checked_in_where(self):
        self.assertIn("НетПоля", " ".join(errors(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.НетПоля = 1")))

    def test_field_checked_in_join_condition(self):
        self.assertIn("НетПоля", " ".join(errors("""
            ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т
            ЛЕВОЕ СОЕДИНЕНИЕ Справочник.ЕдиницыИзмерения КАК Е
                ПО Т.НетПоля = Е.Ссылка
        """)))

    def test_field_checked_in_group_by(self):
        self.assertIn("НетПоля", " ".join(errors(
            "ВЫБРАТЬ СУММА(Т.Артикул) ИЗ Справочник.Номенклатура КАК Т "
            "СГРУППИРОВАТЬ ПО Т.НетПоля")))

    def test_select_alias_usable_in_order_by(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Артикул КАК МойАртикул ИЗ Справочник.Номенклатура КАК Т "
            "УПОРЯДОЧИТЬ ПО МойАртикул"))

    def test_two_level_path_checked(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.БазоваяЕдиница.Коэффициент ИЗ Справочник.Номенклатура КАК Т"))

    def test_two_level_path_broken_second_segment(self):
        self.assertIn("Коэффициентт", " ".join(errors(
            "ВЫБРАТЬ Т.БазоваяЕдиница.Коэффициентт "
            "ИЗ Справочник.Номенклатура КАК Т")))

    def test_composite_type_second_segment_not_checked(self):
        # Поставщик — составной тип: какой из двух справочников имелся в виду,
        # из текста запроса не следует, поэтому молчим.
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Поставщик.ЧтоУгодно ИЗ Справочник.Номенклатура КАК Т"))

    def test_reference_field_descends_to_own_object(self):
        self.assertIn("НетТакого", " ".join(errors(
            "ВЫБРАТЬ Т.Ссылка.НетТакого ИЗ Справочник.Номенклатура КАК Т")))

    def test_bare_field_with_single_source_checked(self):
        self.assertIn("НетПоля", " ".join(errors(
            "ВЫБРАТЬ НетПоля ИЗ Справочник.Номенклатура КАК Т")))

    def test_bare_field_with_two_sources_is_warning_not_error(self):
        res = check_query("""
            ВЫБРАТЬ Артикул ИЗ Справочник.Номенклатура КАК Т
            ЛЕВОЕ СОЕДИНЕНИЕ Справочник.ЕдиницыИзмерения КАК Е
                ПО Т.БазоваяЕдиница = Е.Ссылка
        """, P)
        self.assertTrue(res["valid"])
        self.assertTrue(res["warnings"])

    def test_source_without_alias_referenced_by_name(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Номенклатура.Артикул ИЗ Справочник.Номенклатура"))

    def test_subquery_columns_checked(self):
        self.assertIn("НетТакой", " ".join(errors("""
            ВЫБРАТЬ П.НетТакой ИЗ
                (ВЫБРАТЬ Т.Артикул КАК Арт ИЗ Справочник.Номенклатура КАК Т) КАК П
        """)))

    def test_subquery_with_star_suppresses_checks(self):
        self.assertTrue(is_valid("""
            ВЫБРАТЬ П.ЧтоУгодно ИЗ
                (ВЫБРАТЬ * ИЗ Справочник.Номенклатура КАК Т) КАК П
        """))

    def test_both_union_parts_checked(self):
        self.assertIn("НетПоля", " ".join(errors("""
            ВЫБРАТЬ Т.Артикул КАК А ИЗ Справочник.Номенклатура КАК Т
            ОБЪЕДИНИТЬ ВСЕ
            ВЫБРАТЬ Е.НетПоля КАК А ИЗ Справочник.ЕдиницыИзмерения КАК Е
        """)))

    def test_enum_literal_prefix_not_treated_as_alias(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Код ИЗ Справочник.Номенклатура КАК Т "
            "ГДЕ Т.Артикул <> ЗНАЧЕНИЕ(Перечисление.Статусы.Новый)"))


# ─── Табличные части и виртуальные таблицы ───────────────────────────────

class TestTablesAndVirtual(unittest.TestCase):

    def test_tabular_section_as_source(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Ссылка, Т.НомерСтроки, Т.Свойство "
            "ИЗ Справочник.Номенклатура.ДопРеквизиты КАК Т"))

    def test_unknown_field_of_tabular_section(self):
        self.assertIn("НетПоля", " ".join(errors(
            "ВЫБРАТЬ Т.НетПоля ИЗ Справочник.Номенклатура.ДопРеквизиты КАК Т")))

    def test_tabular_section_name_is_a_field_of_object(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.ДопРеквизиты ИЗ Справочник.Номенклатура КАК Т"))

    def test_wrong_third_part_is_error(self):
        msg = " ".join(errors(
            "ВЫБРАТЬ Т.Ссылка ИЗ Справочник.Номенклатура.НетТакой КАК Т"))
        self.assertIn("не виртуальная таблица", msg)

    def test_virtual_table_of_wrong_kind_is_error(self):
        msg = " ".join(errors(
            "ВЫБРАТЬ Т.Номенклатура ИЗ РегистрСведений.ЦеныНоменклатуры.Остатки КАК Т"))
        self.assertIn("не виртуальная таблица", msg)
        self.assertIn("СрезПоследних", msg)

    def test_sliceoflast_fields(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Период, Т.Номенклатура, Т.Цена, Т.Источник "
            "ИЗ РегистрСведений.ЦеныНоменклатуры.СрезПоследних(&Дата) КАК Т"))

    def test_sliceoflast_unknown_field(self):
        self.assertIn("НетПоля", " ".join(errors(
            "ВЫБРАТЬ Т.НетПоля "
            "ИЗ РегистрСведений.ЦеныНоменклатуры.СрезПоследних(&Дата) КАК Т")))

    def test_balance_resource_gets_suffix(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Склад, Т.КоличествоОстаток "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки(&Дата) КАК Т"))

    def test_balance_bare_resource_name_is_error(self):
        self.assertIn("Количество", " ".join(errors(
            "ВЫБРАТЬ Т.Количество "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки(&Дата) КАК Т")))

    def test_turnover_resource_suffixes(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.КоличествоПриход, Т.КоличествоРасход, Т.КоличествоОборот "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.Обороты(&Н, &К) КАК Т"))

    def test_balance_and_turnover_suffixes(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.КоличествоНачальныйОстаток, Т.КоличествоКонечныйОстаток "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.ОстаткиИОбороты(&Н, &К) КАК Т"))

    def test_accounting_virtual_table_fields_not_checked(self):
        # Состав полей регистра бухгалтерии зависит от плана счетов и
        # субконто — вывести его из выгрузки нельзя, поэтому молчим.
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.ЧтоУгодно "
            "ИЗ РегистрБухгалтерии.Хозрасчетный.Остатки(&Дата) КАК Т"))

    def test_main_register_table_fields(self):
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Период, Т.Номенклатура, Т.Цена "
            "ИЗ РегистрСведений.ЦеныНоменклатуры КАК Т"))

    def test_unknown_prefix_is_error(self):
        self.assertIn("не вид объекта", " ".join(errors(
            "ВЫБРАТЬ Т.Поле ИЗ НечтоСтранное.Объект КАК Т")))


# ─── QOPT-1 и остальные правила оптимизации ──────────────────────────────

class TestOptimizeRules(unittest.TestCase):

    def _rules(self, text):
        return {r["rule"] for r in analyze(text)}

    def test_qopt1_virtual_table_with_params_is_not_unlimited(self):
        rules = self._rules(
            "ВЫБРАТЬ Т.КоличествоОстаток "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки(&Дата, Склад = &С) КАК Т")
        self.assertNotIn("Отсутствие ограничения выборки", rules)

    def test_unlimited_select_from_catalog_is_flagged(self):
        rules = self._rules("ВЫБРАТЬ Т.Ссылка ИЗ Справочник.Номенклатура КАК Т")
        self.assertIn("Отсутствие ограничения выборки", rules)

    def test_where_removes_unlimited_flag(self):
        rules = self._rules(
            "ВЫБРАТЬ Т.Ссылка ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.Артикул = &А")
        self.assertNotIn("Отсутствие ограничения выборки", rules)

    def test_first_removes_unlimited_flag(self):
        rules = self._rules(
            "ВЫБРАТЬ ПЕРВЫЕ 10 Т.Ссылка ИЗ Справочник.Номенклатура КАК Т")
        self.assertNotIn("Отсутствие ограничения выборки", rules)

    def test_temp_table_source_is_not_unlimited(self):
        rules = self._rules(
            "ВЫБРАТЬ Т.Артикул КАК А ПОМЕСТИТЬ ВТ "
            "ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.Артикул = &А;"
            "ВЫБРАТЬ В.А ИЗ ВТ КАК В")
        self.assertNotIn("Отсутствие ограничения выборки", rules)

    def test_virtual_table_without_params_flagged(self):
        rules = self._rules(
            "ВЫБРАТЬ Т.КоличествoОстаток "
            "ИЗ РегистрНакопления.ТоварыНаСкладах.Остатки КАК Т ГДЕ Т.Склад = &С")
        self.assertIn("Параметры виртуальных таблиц", rules)

    def test_fix2_long_virtual_table_name_with_params_not_flagged(self):
        # Регрессия FIX-2: «Остатки» не должно матчиться внутри «ОстаткиИОбороты».
        rules = self._rules(
            "ВЫБРАТЬ Т.Склад ИЗ "
            "РегистрНакопления.ТоварыНаСкладах.ОстаткиИОбороты(&Н, &К) КАК Т")
        self.assertNotIn("Параметры виртуальных таблиц", rules)

    def test_main_register_table_flagged(self):
        rules = self._rules(
            "ВЫБРАТЬ Т.Количество ИЗ РегистрНакопления.ТоварыНаСкладах КАК Т "
            "ГДЕ Т.Склад = &С")
        self.assertIn("Виртуальные таблицы", rules)

    def test_star_is_flagged(self):
        self.assertIn("Выборка всех полей", self._rules(
            "ВЫБРАТЬ * ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.Артикул = &А"))

    def test_function_in_where_flagged(self):
        self.assertIn("Функции в условиях", self._rules(
            "ВЫБРАТЬ Т.Ссылка ИЗ Справочник.Номенклатура КАК Т "
            "ГДЕ ПОДСТРОКА(Т.Артикул, 1, 3) = &А"))

    def test_subquery_in_join_flagged(self):
        self.assertIn("Соединение с подзапросом", self._rules("""
            ВЫБРАТЬ Т.Ссылка ИЗ Справочник.Номенклатура КАК Т
            ЛЕВОЕ СОЕДИНЕНИЕ (ВЫБРАТЬ Е.Ссылка КАК С
                              ИЗ Справочник.ЕдиницыИзмерения КАК Е) КАК П
                ПО Т.БазоваяЕдиница = П.С
            ГДЕ Т.Артикул = &А
        """))

    def test_clean_query_gets_no_recommendations(self):
        rules = self._rules(
            "ВЫБРАТЬ ПЕРВЫЕ 10 Т.Ссылка, Т.Артикул "
            "ИЗ Справочник.Номенклатура КАК Т ГДЕ Т.Артикул = &А")
        self.assertEqual(rules, set())


# ─── Устойчивость ────────────────────────────────────────────────────────

class TestRobustness(unittest.TestCase):

    def test_empty_text(self):
        res = check_query("", P)
        self.assertEqual(res["statements"], 0)

    def test_whitespace_only(self):
        self.assertTrue(check_query("   \n  ", P)["valid"])

    def test_english_keywords(self):
        st = parse_batch(
            "SELECT T.Код FROM Справочник.Номенклатура AS T").statements[0]
        self.assertEqual(st.main.sources[0].alias, "T")

    def test_lowercase_keywords(self):
        self.assertTrue(is_valid(
            "выбрать Т.Артикул из Справочник.Номенклатура как Т"))

    def test_long_batch_does_not_crash(self):
        text = ";".join(
            "ВЫБРАТЬ Т.Артикул ИЗ Справочник.Номенклатура КАК Т"
            for _ in range(50))
        self.assertEqual(check_query(text, P)["statements"], 50)


# ─── FEAT-1.1: стандартные реквизиты по структуре объекта ────────────────


def _catalog(name, properties=None, has_owner=False):
    return ObjectInfo(id=f"Catalog.{name}", name=name, kind_ru="Справочник",
                      attributes=[AttrInfo("Артикул", "attribute",
                                           [("String", None)])],
                      properties=properties or {}, has_owner=has_owner)


class TestStandardFieldsByStructure(unittest.TestCase):
    """Родитель/ЭтоГруппа/Владелец есть не у всякого справочника."""

    def test_flat_catalog_has_no_parent(self):
        fields = standard_fields_for(_catalog("Плоский",
                                              {"Hierarchical": "false"}))
        self.assertNotIn("Родитель", fields)
        self.assertNotIn("ЭтоГруппа", fields)

    def test_hierarchical_catalog_has_parent(self):
        fields = standard_fields_for(_catalog("Иерарх",
                                              {"Hierarchical": "true"}))
        self.assertIn("Родитель", fields)
        self.assertIn("ЭтоГруппа", fields)

    def test_subordinate_catalog_has_owner(self):
        fields = standard_fields_for(_catalog("Подчинённый",
                                              {"Hierarchical": "false"},
                                              has_owner=True))
        self.assertIn("Владелец", fields)

    def test_independent_catalog_has_no_owner(self):
        fields = standard_fields_for(_catalog("Сам",
                                              {"Hierarchical": "false"}))
        self.assertNotIn("Владелец", fields)

    def test_unknown_properties_stay_permissive(self):
        # Граф, построенный индексатором до Захода 3: признаков нет.
        # Начинать ругаться на корректные запросы в этом случае нельзя.
        fields = standard_fields_for(_catalog("Старый"))
        self.assertIn("Родитель", fields)
        self.assertIn("Владелец", fields)

    def test_core_fields_always_present(self):
        for props in ({}, {"Hierarchical": "false"}, {"Hierarchical": "true"}):
            fields = standard_fields_for(_catalog("Х", props))
            for name in ("Ссылка", "Код", "Наименование", "ПометкаУдаления"):
                self.assertIn(name, fields)

    def test_register_kinds_unaffected(self):
        reg = ObjectInfo(id="InformationRegister.Р", name="Р",
                         kind_ru="РегистрСведений",
                         properties={"WriteMode": "Independent"})
        self.assertIn("Период", standard_fields_for(reg))


class TestStructureAwareValidation(unittest.TestCase):
    """Та же проверка, но через check_query — как её увидит агент."""

    def _provider(self, obj):
        return FakeProvider([obj])

    def test_parent_on_flat_catalog_is_error(self):
        p = self._provider(_catalog("Плоский", {"Hierarchical": "false"}))
        errs = errors("ВЫБРАТЬ Т.Родитель ИЗ Справочник.Плоский КАК Т", p)
        self.assertIn("Родитель", " ".join(errs))

    def test_parent_on_hierarchical_catalog_is_valid(self):
        p = self._provider(_catalog("Иерарх", {"Hierarchical": "true"}))
        self.assertTrue(is_valid("ВЫБРАТЬ Т.Родитель ИЗ Справочник.Иерарх КАК Т", p))

    def test_owner_on_independent_catalog_is_error(self):
        p = self._provider(_catalog("Сам", {"Hierarchical": "false"}))
        errs = errors("ВЫБРАТЬ Т.Владелец ИЗ Справочник.Сам КАК Т", p)
        self.assertIn("Владелец", " ".join(errs))

    def test_owner_on_subordinate_catalog_is_valid(self):
        p = self._provider(_catalog("Подч", {"Hierarchical": "false"},
                                    has_owner=True))
        self.assertTrue(is_valid("ВЫБРАТЬ Т.Владелец ИЗ Справочник.Подч КАК Т", p))

    def test_old_graph_without_properties_accepts_both(self):
        p = self._provider(_catalog("Старый"))
        self.assertTrue(is_valid(
            "ВЫБРАТЬ Т.Родитель, Т.Владелец ИЗ Справочник.Старый КАК Т", p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
