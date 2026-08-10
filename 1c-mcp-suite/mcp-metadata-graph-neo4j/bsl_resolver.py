"""
Резолвер BSL (слой 2 графа, 4.6.2 + 4.6.4 type inference v2).
==============================================================

Принимает list[ParsedModule] (выход bsl_parser) + индекс существующих
узлов из Neo4j, собирает полный code_graph для writer'а:

  • :Callable + :Parameter + :CallSite узлы
  • :Module узлы (для ObjectModule/ManagerModule/Form)
  • :Type узлы слоя 2 (4.6.4 — выведенные типы параметров)
  • :HAS_METHOD, :HAS_PARAM, :CALL_SITE
  • :CALLS, :RESOLVES_TO_CALLEE (из резолва)
  • :OPERATES_ON (из metadata access + predef + manager calls)
  • :INFERRED_TYPE (4.6.4 — (:Parameter)->(:Type) из inter-procedural вывода)

Стратегия резолва (по PLAN_4_6_2.md):

  1. Прямой call в CommonModule: `АукОбщийКлиент.Факториал(...)`
     → CALLS на :Callable "CommonModule.АукОбщийКлиент.Факториал"
  2. Метаданные через menеджер: `Справочники.X.СоздатьЭлемент()`
     → :OPERATES_ON на :MetadataObject "Catalog.X" via=Справочники
     → если в Catalog.X.ManagerModule есть такой метод — ещё и :CALLS
  3. Локальный dataflow:
     `тз = Справочники.Y.СоздатьЭлемент(); тз.метод(...)` → CALLS в
     ObjectModule (или ManagerModule для ссылочных типов).
  4. Self-reference внутри модуля: `<последний сегмент module_id>.X(...)`
     → CALLS внутри того же модуля.

Built-in функции (см. BUILTIN_FUNCS) НЕ создают :CallSite — DEBUG-лог.
Это резко уменьшает мусор в графе.

--- 4.6.4: type inference v2 (PLAN_4_6_4.md) ---
Поверх 4.6.2 добавлено:
  A1. Коллекционные типы: `var = Новый ТаблицаЗначений/Структура/...` → тип;
      методы коллекций (`Вставить`/`Добавить`/...) уходят в skip
      (`collection_method`), а не в unresolved — очистка метрики.
  A2. Расширенный локальный dataflow: цепочки присваиваний (`a = b`),
      return-типы функций (`infer_return_type`).
  B.  Inter-procedural propagation: тип аргумента на резолвнутом callsite
      пробрасывается в параметр callee (`_collect_arg_param_facts`).
  C.  Фикс-пойнт-движок: build_call_graph гоняет _resolve_pass до
      стабилизации реестров param_types/return_types (≤ MAX_ITERATIONS).
      Завершение гарантировано инвариантом монотонности (_merge_type_fact:
      реестры только растут / уходят в AMBIGUOUS).
  D.  :Type-узлы слоя 2 + :INFERRED_TYPE-рёбра в выходе build_call_graph.

--- FIX-4: классификация неразрешённых вызовов ---
Вызов вида `РезультатЗапроса.Выбрать()` до FIX-4 попадал в `unknown_module`
и портил метрику: 861 из 913 неразрешённых на Котировках были такими.
Теперь резолвер получает множество имён области видимости (параметры,
`Перем`, LHS присваиваний, переменные циклов — см. collect_local_names) и
ставит таким вызовам `reason="object_method"`. Покрытие
(`resolve_coverage_pct` в stats) считается без них.

API:
  @dataclass Index           — индексы для резолва.
  @dataclass TypeRef         — выведенный тип (+ confidence/source в 4.6.4).
  build_index_from_modules() — собрать индекс по списку ParsedModule (для тестов).
  build_index_from_neo4j()   — собрать индекс из живой Neo4j.
  build_call_graph(modules, index) → code_graph dict (формат write_code_graph).
  infer_local_types(proc, ...) → dict var_name → TypeRef (v2: опц. контекст).
  infer_return_type(proc, var_types) → TypeRef | None (4.6.4).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from bsl_parser import (
    ParsedModule, ParsedProcedure, ParsedCall,
    iter_calls, iter_metadata_access, iter_predef, iter_assign_refs,
    iter_new_assigns, iter_var_assigns, iter_call_assigns, split_args,
    collect_local_names,
)


from progress_log import ProgressLogger, human_sec

log = logging.getLogger(__name__)


# ─── Константы ────────────────────────────────────────────────────────────


# Маппинг русских plural'ей к английскому KindEng (та же таблица из 4.6.1).
PLURAL_TO_KIND_ENG: dict[str, str] = {
    "Справочники":             "Catalog",
    "Документы":               "Document",
    "Перечисления":            "Enum",
    "Обработки":               "DataProcessor",
    "Отчеты":                  "Report",
    "РегистрыСведений":        "InformationRegister",
    "РегистрыНакопления":      "AccumulationRegister",
    "РегистрыБухгалтерии":     "AccountingRegister",
    "РегистрыРасчета":         "CalculationRegister",
    "Константы":               "Constant",
    "ПланыВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланыСчетов":             "ChartOfAccounts",
    "ПланыВидовРасчета":       "ChartOfCalculationTypes",
    "ПланыОбмена":             "ExchangePlan",
    "БизнесПроцессы":          "BusinessProcess",
    "Задачи":                  "Task",
    "ЖурналыДокументов":       "DocumentJournal",
}

# Русские singular'и в `ПредопределенноеЗначение("Перечисление.X.Y")`.
PREDEF_RU_TO_KIND_ENG: dict[str, str] = {
    "Перечисление":             "Enum",
    "Справочник":               "Catalog",
    "Документ":                 "Document",
    "ПланВидовХарактеристик":   "ChartOfCharacteristicTypes",
    "ПланСчетов":               "ChartOfAccounts",
    "ПланВидовРасчета":         "ChartOfCalculationTypes",
    "ПланОбмена":               "ExchangePlan",
    "БизнесПроцесс":            "BusinessProcess",
    "Задача":                   "Task",
}

# Метод → kind, который он возвращает (для dataflow).
# `<kind>` — placeholder, подставится из plural'я в left-hand side.
METHOD_TO_KIND: dict[str, str] = {
    "СоздатьЭлемент":       "<kind>Object",        # Catalog/Document Object
    "СоздатьГруппу":        "CatalogObject",       # только для Catalog
    "СоздатьДокумент":      "DocumentObject",
    "ПустаяСсылка":         "<kind>Ref",
    "НайтиПоКоду":          "<kind>Ref",
    "НайтиПоНаименованию":  "<kind>Ref",
    "НайтиПоРеквизиту":     "<kind>Ref",
    "НайтиПоНомеру":        "<kind>Ref",
    "ПолучитьСсылку":       "<kind>Ref",
    "СоздатьНаборЗаписей":  "<kind>RecordSet",
    "СоздатьМенеджерЗаписи": "<kind>RecordManager",
    "Выбрать":              "<kind>Selection",
    "ВыбратьИерархически":  "<kind>Selection",
}


# Из этих kind'ов резолвер пытается резолвить вызов в Module:
#   *Object → ObjectModule
#   *Ref    → ManagerModule (тип Ref статически — это менеджер)
#   *RecordManager / *RecordSet → ManagerModule (для регистров)
KIND_TO_MODULE_ROLE: dict[str, str] = {
    "CatalogObject":               "ObjectModule",
    "DocumentObject":              "ObjectModule",
    "CatalogRef":                  "ManagerModule",
    "DocumentRef":                 "ManagerModule",
    "EnumRef":                     "ManagerModule",
    "InformationRegisterRecordSet":     "ManagerModule",
    "InformationRegisterRecordManager": "ManagerModule",
    "AccumulationRegisterRecordSet":    "ManagerModule",
    "AccumulationRegisterRecordManager": "ManagerModule",
}


# ─── FIX-5: контекстный объект модуля ────────────────────────────────────
#
# Обоснование — PLAN-3 §2.1. `Объект.МойМетод()` в модуле формы справочника
# до сих пор классифицировался как `context_property`: слева от точки —
# основной реквизит формы, в пробелы не идёт, метрику не портит. Но адресат
# у такого вызова ЕСТЬ: роль модуля известна, `parent_metadata_id` тоже,
# значит метод надо искать в ObjectModule родителя. Ребро :CALLS не
# строилось, и `code_callers` для процедуры модуля объекта не показывал
# вызовы из форм этого объекта — то есть основной сценарий инструмента
# отвечал неполно.
#
# Реализация намеренно узкая. Кандидат строится напрямую из известных
# идентификаторов модулей, без новых типов и без правки
# KIND_TO_MODULE_ROLE: трогать общую карту типов значило бы менять поведение
# ветки dataflow, а её результат — тот самый инвариант `callsites_resolved`,
# по которому сверяется сохранность при каждой правке.
#
# Что НЕ делается, хотя записано в PLAN-3:
#   `ЭтотОбъект` в модуле ФОРМЫ — это сама форма, а не объект (и в
#   управляемых, и в обычных формах). Отобразить его в ObjectModule значило
#   бы строить заведомо ложные рёбра.
#   `Ссылка` в модуле объекта — это ссылка, а не объект. Методы модуля
#   объекта на ссылке в 1С не вызываются, такое ребро тоже было бы ложным.
# Оба случая остаются `context_property`.

def context_self_modules(module_kind: str,
                         module_id: str,
                         parent_metadata_id: Optional[str]) -> dict[str, str]:
    """Имена контекста → id модуля, в котором искать метод.

    Ключи в нижнем регистре (BSL регистронезависим — грабля FIX-4.1).
    Пустой словарь для ролей и модулей, где вывод недетерминирован.
    """
    out: dict[str, str] = {}
    if module_kind == "Form" and parent_metadata_id:
        # Основной реквизит формы объекта. Тип выводится детерминированно:
        # форма принадлежит объекту, значит `Объект` — этот объект.
        out["объект"] = f"{parent_metadata_id}.ObjectModule"
    elif module_kind in ("ObjectModule", "ManagerModule"):
        # `ЭтотОбъект.Метод()` — то же, что локальный вызов.
        out["этотобъект"] = module_id
    return out



# ─── 4.6.4 этап A1: коллекционные / платформенные типы ───────────────────
#
# `var = Новый ТаблицаЗначений` → у `var` тип-коллекция. Методы на коллекциях
# (`Вставить`, `Добавить`, `Количество`, …) — это НЕ вызовы процедур конфигурации,
# их некуда резолвить как :CALLS. Но и засчитывать их в `unresolved` нечестно:
# это «шум», искусственно занижающий coverage. Решение (см. PLAN_4_6_4.md 3.3 п.4):
# если `module_ref` — переменная коллекционного типа, а метод ∈ COLLECTION_METHODS,
# резолвер возвращает skip(reason="collection_method") — не :CallSite, не unresolved.

# Имя класса из `Новый <Класс>` → канонический kind коллекционного типа.
# kind'ы здесь намеренно совпадают с тем, что слой 1 пишет для платформенных
# типов реквизитов (см. metadata_xml.parse_v8_type — "ValueTable" и т.п.),
# чтобы :Type-узлы переиспользовались.
COLLECTION_TYPES: dict[str, str] = {
    "ТаблицаЗначений":          "ValueTable",
    "ДеревоЗначений":           "ValueTree",
    "Структура":                "Structure",
    "ФиксированнаяСтруктура":   "FixedStructure",
    "Соответствие":             "Map",
    "ФиксированноеСоответствие": "FixedMap",
    "Массив":                   "Array",
    "ФиксированныйМассив":      "FixedArray",
    "СписокЗначений":           "ValueList",
    "Запрос":                   "Query",
    "ТекстовыйДокумент":        "TextDocument",
    "ТабличныйДокумент":        "SpreadsheetDocument",
    "Файл":                     "File",
    "ОписаниеТипов":            "TypeDescription",
    "ХранилищеЗначения":        "ValueStorage",
    "ЧтениеXML":                "XMLReader",
    "ЗаписьXML":                "XMLWriter",
    "ЧтениеJSON":               "JSONReader",
    "ЗаписьJSON":               "JSONWriter",
    "ЧтениеТекста":             "TextReader",
    "ЗаписьТекста":             "TextWriter",
    "ПостроительЗапроса":       "QueryBuilder",
    "ПостроительОтчета":        "ReportBuilder",
    "СхемаКомпоновкиДанных":    "DataCompositionSchema",
    "СхемаЗапроса":             "QuerySchema",
    "ТаблицаЗначенийКолонка":   "ValueTableColumn",
}

# Методы, которые вызываются на коллекционных типах. Если `module_ref` —
# переменная одного из COLLECTION_TYPES и метод здесь — это collection_method,
# не unresolved. Список покрывает топ unresolved-методов из калибровки
# (`Вставить` 568, `Добавить` 363, `УстановитьПараметр` 117, `Количество` 109, …).
COLLECTION_METHODS: set[str] = {
    # Общие для коллекций
    "Вставить", "Добавить", "Удалить", "Очистить", "Количество", "Получить",
    "Найти", "НайтиСтроки", "НайтиПоЗначению",
    "Свойство", "Содержит",
    "Индекс", "Сдвинуть",
    # ТаблицаЗначений / ДеревоЗначений
    "ДобавитьСтроку", "ПолучитьСтроку", "УдалитьСтроку", "ВставитьСтроку",
    "КоличествоСтрок", "Итог", "Свернуть", "Сортировать", "ВыбратьСтроку",
    "ЗагрузитьКолонку", "ВыгрузитьКолонку", "ЗаполнитьЗначения",
    "СкопироватьКолонки", "Скопировать",
    # Массив / СписокЗначений
    "ВГраница", "Выгрузить", "ЗагрузитьЗначения", "СортироватьПоЗначению",
    "СортироватьПоПредставлению", "ВыгрузитьЗначения",
    # Запрос
    "Выполнить", "УстановитьПараметр", "ВыполнитьПакет",
    "ВыполнитьПакетСПромежуточнымиДанными",
    # Результат запроса / выборка
    "Выбрать", "Следующий", "СледующийПоЗначениюПоля", "Пустой",
    "Выгрузить", "ПолучитьЭлементы",
    # ТекстовыйДокумент
    "ПолучитьТекст", "УстановитьТекст", "ДобавитьСтроку", "Записать",
    "Прочитать", "КоличествоСтрок", "ПолучитьСтроку", "ЗаменитьСтроку",
    "ВставитьСтроку", "УдалитьСтроку",
    # XML/JSON чтение-запись
    "УстановитьСтроку", "Закрыть", "ПрочитатьАтрибут",
    # Файл
    "Существует",
    # ОписаниеТипов
    "СодержитТип", "ПривестиЗначение",
    # ХранилищеЗначения
    "Получить",
}

# Множество kind'ов коллекционных типов (значения COLLECTION_TYPES) — для
# быстрой проверки `var_type.kind in _COLLECTION_KIND_SET` в _resolve_call.
_COLLECTION_KIND_SET: frozenset[str] = frozenset(COLLECTION_TYPES.values())


# ─── FIX-4: причины, которые не являются пробелом в графе ────────────────
#
# Эти вызовы не претендуют быть обращением к коду конфигурации: слева от
# точки стоит переменная, а не модуль. Такой вызов некуда резолвить в
# :CALLS — и засчитывать его в знаменатель покрытия нечестно.
#
# До FIX-4 861 из 913 неразрешённых вызовов на Котировках имели reason
# `unknown_module` — это были `РезультатЗапроса.Выбрать()`, `Выборка.Следующий()`
# и подобное. Метрика показывала 72.66% при фактическом покрытии за 90%,
# а `code_unresolved_callsites` тонул в шуме и был бесполезен.
#
# :CallSite при этом СОЗДАЁТСЯ (в отличие от skip): узел нужен, чтобы
# `code_unresolved_callsites(include_object_methods=True)` мог показать
# полную картину.
NON_CONFIG_CALL_REASONS: frozenset[str] = frozenset({
    # module_ref — объявленная в области видимости переменная (FIX-4).
    "object_method",
    # module_ref — свойство контекста модуля или реквизит формы (FIX-4.1):
    # `Ссылка` в модуле объекта, `Параметры` / `ТекстЗапроса` в модуле формы.
    "context_property",
    # module_ref — глобальный объект платформы (FIX-4.1):
    # `Метаданные`, `ФоновыеЗадания`, `СериализаторXDTO`.
    "platform_global",
    # Переменная коллекционного типа, метода нет в COLLECTION_METHODS (4.6.4).
    "collection_unknown_method",
    # У выведенного типа переменной нет модуля, куда резолвить (4.6.4).
    "dataflow_kind_no_module_role",
})


# Полный whitelist встроенных функций BSL. Их матчи в iter_calls
# НЕ становятся :CallSite-узлами (DEBUG-лог).
BUILTIN_FUNCS: set[str] = {
    # Конструкторы / типы
    "Новый", "Тип", "ТипЗнч", "Строка", "Число", "Дата", "Булево",
    "Структура", "Массив", "Соответствие", "СписокЗначений", "ТаблицаЗначений",
    "ДеревоЗначений", "Запрос", "ОписаниеТипов", "ПостроительОтчета",
    "ПостроительЗапроса", "СхемаКомпоновкиДанных",
    "ФиксированнаяСтруктура", "ФиксированноеСоответствие", "ФиксированныйМассив",
    "ПолеКомпоновкиДанных", "ОтборКомпоновкиДанных",
    "ОписаниеОповещения", "ПараметрВыбора",

    # Строки
    "СтрНайти", "СтрНайтиВсе", "СтрЗаменить", "СтрШаблон", "СтрДлина",
    "СтрРазделить", "СтрСоединить", "СтрЧислоВхождений", "СтрСравнить",
    "СтрНачинаетсяС", "СтрЗаканчиваетсяНа",
    "СокрЛП", "СокрЛ", "СокрП",
    "НРег", "ВРег", "ТРег",
    "Лев", "Прав", "Сред", "Найти",
    "Символ", "КодСимвола",
    "НСтр",

    # Числа
    "Цел", "Окр", "Лог", "Лог10", "Sin", "Cos", "Tan", "ASin", "ACos", "ATan",
    "Exp", "Pow", "Sqrt", "Min", "Max",

    # Даты
    "ТекущаяДата", "ТекущаяДатаСеанса", "ТекущаяУниверсальнаяДата",
    "Год", "Месяц", "День", "Час", "Минута", "Секунда", "ДеньНедели",
    "ДеньГода", "НеделяГода",
    "НачалоДня", "КонецДня", "НачалоНедели", "КонецНедели",
    "НачалоМесяца", "КонецМесяца", "НачалоКвартала", "КонецКвартала",
    "НачалоГода", "КонецГода",
    "НачалоЧаса", "КонецЧаса", "НачалоМинуты", "КонецМинуты",
    "ДобавитьМесяц",

    # Проверки
    "ЗначениеЗаполнено", "ЗначениеНеЗаполнено",
    "ЭтоНовый",  # метод объекта, но на практике встречается без префикса
                  # (вызывается на ЭтотОбъект-неявном)

    # Платформа / формы
    "ЗначениеВДанныеФормы", "ДанныеФормыВЗначение", "КопироватьДанныеФормы",
    "РеквизитФормыВЗначение", "ЗначениеВРеквизитФормы",
    "ПолучитьМакет", "ПолучитьФорму", "ОткрытьФорму", "ЗакрытьФорму",
    "ВыполнитьПроверкуЗаполнения", "ПроверитьЗаполнение",
    "ЗаполнитьЗначенияСвойств",
    "ПоказатьВопрос", "ПоказатьПредупреждение", "ПоказатьЗначение",
    "ПоказатьВводЗначения", "ПоказатьВводЧисла", "ПоказатьВводСтроки",
    "ПоказатьВводДаты", "ПоказатьПодключениеВнешнейКомпоненты",
    "ВыполнитьОбработкуОповещения",
    "ПодключитьОбработчикОжидания", "ОтключитьОбработчикОжидания",
    "ОповеститьОбИзменении", "Оповестить",
    "Записать", "Закрыть",  # методы формы/объекта, неявно на ЭтотОбъект

    # Сериализация
    "СериализаторXDTO", "XMLТип", "XMLЗначение", "XMLСтрока",

    # Сообщения / ошибки
    "Сообщить", "СообщитьПользователю",
    "ВызватьИсключение",
    "ПодробноеПредставлениеОшибки", "ПредставлениеОшибки", "ИнформацияОбОшибке",

    # Вычисление / выполнение
    "Вычислить", "Выполнить",

    # Системные проверки
    "ЭтоСсылочныйТип", "ЭтоНеопределено",

    # Спец. вызовы платформы
    "ПредопределенноеЗначение",  # обрабатывается отдельно как :OPERATES_ON

    # Дополнительно — частые встроенные, найденные на реальной выгрузке
    "Формат", "ФорматированнаяСтрока", "Шрифт", "Цвет",
    "ОписаниеОшибки", "ПустаяСтрока",
    "ТекущаяУниверсальнаяДатаВМиллисекундах",
    "ПолучитьИзВременногоХранилища", "ПоместитьВоВременноеХранилище",
    "УдалитьИзВременногоХранилища",
    "Файл", "НайтиФайлы", "КаталогВременныхФайлов", "КаталогПрограммы",
    "НачатьУдалениеФайлов", "УдалитьФайлы", "СоздатьКаталог",
    "УникальныйИдентификатор", "РеквизитФормы",
    "НачатьТранзакцию", "ЗафиксироватьТранзакцию", "ОтменитьТранзакцию",
    "ТранзакцияАктивна",
    "УстановитьБезопасныйРежим", "БезопасныйРежим",
    "ПолучитьРазделительПутиСервера", "ПолучитьРазделительПутиКлиента",
    "СтрНайтиВсеПоРегулярномуВыражению", "СтрЗаменитьПоРегулярномуВыражению",
    "СтрПодобнаПоРегулярномуВыражению",
    "СтрокаСЧислом", "ЧислоИзСтроки",
    "Состояние", "ОчиститьСообщения",
    "ХранилищеЗначения",
    "ПодключитьВнешнююКомпоненту", "НачатьПодключениеВнешнейКомпоненты",
    "ПолучитьИмяВременногоФайла",
    "СтрСодержит", "СтрПоиск",
    "ПолучитьСтруктуруХранения",
    "МоментВремени", "Граница",
    "ЗаписатьJSON", "ПрочитатьJSON",
    "ЧтениеТекста", "ЗаписьТекста", "ЧтениеXML", "ЗаписьXML",
    "ЧтениеДанных", "ЗаписьДанных",
    "Макс", "Мин", "Log10",
    "ИмяПользователя", "ПолноеИмяПользователя",
    "МестноеВремя", "УниверсальноеВремя",
    "ПараметрКомпоновкиДанных", "СвязьПоТипу", "СвязьПараметраВыбора",
    "КвалификаторыЧисла", "КвалификаторыСтроки", "КвалификаторыДаты",
    "ДиалогВыбораФайла", "ДиалогВыбораЦвета", "ДиалогВыбораШрифта",
    "ИЛИ", "И", "НЕ",  # синтаксис, но иногда матчится как локальный вызов

    # FIX-4.1: добор по факту. Все эти имена нашлись в остатке
    # `unknown_local_method` после первой проверки FIX-4 на живом графе —
    # глобальные методы платформы, которых не хватало в списке.
    "УстановитьПривилегированныйРежим", "ПривилегированныйРежим",
    "УстановитьБезопасныйРежимРазделенияДанных",
    "БезопасныйРежимРазделенияДанных",
    "ОбработкаПрерыванияПользователя",
    "КраткоеПредставлениеОшибки",
    "СтрНайтиПоРегулярномуВыражению",
    "ПолучитьНавигационнуюСсылку", "ПолучитьВнешнююНавигационнуюСсылку",
    "ПолучитьНавигационнуюСсылкуИнформационнойБазы",
    "НачатьПеремещениеФайла", "ПереместитьФайл", "КопироватьФайл",
    "НайтиНедопустимыеСимволыXML", "ИмпортМоделиXDTO",
    "ЗначениеВСтрокуВнутр", "ЗначениеИзСтрокиВнутр",
    "ЗначениеВФайл", "ЗначениеИзФайла",
    "ПолучитьОбщийМакет", "ПолучитьОбщуюФорму",
    "ПолучитьЗаголовокСистемы", "УстановитьЗаголовокСистемы",
    "ПолучитьСообщенияПользователю",
    "НачатьПомещениеФайла", "ПоместитьФайл",
    "ТекущийВариантОсновногоШрифтаКлиентскогоПриложения",
    "ПодключитьОбработчикОповещенияОДействияхПользователя",
    "ЗапуститьПриложение", "НачатьЗапускПриложения",
    "КодироватьСтроку", "РаскодироватьСтроку",
    "Base64Значение", "Base64Строка",
    "ХешированиеДанных", "ЗаписьТекстаБезРазделителей",

    # FIX-4.2: добор по разбору оставшихся 22 пробелов на живом графе.
    # Глобальные методы платформы:
    "СтрокаСоединенияИнформационнойБазы",
    "ПолучитьСтруктуруХраненияБазыДанных",
    "НачатьПодключениеРасширенияРаботыСФайлами",
    "НачатьУстановкуРасширенияРаботыСФайлами",
    "НачатьПолучениеРабочегоКаталогаДанныхПользователя",
    "ПодключитьРасширениеРаботыСФайлами",
    "УстановитьРасширениеРаботыСФайлами",
    "ПолучитьРабочийКаталогДанныхПользователя",
    # Методы объекта/формы, вызываемые неявно на ЭтотОбъект — как уже
    # лежащие здесь `Записать`, `Закрыть`, `ЭтоНовый`. Ограничение приёма
    # известно: если в конфигурации появится СВОЯ процедура с таким именем,
    # её вызов уйдёт в skip вместо :CALLS. Для этих имён риск ничтожен.
    "Метаданные",           # ЭтотОбъект.Метаданные() в модуле объекта
    "ИзменитьРеквизиты",    # ЭтаФорма.ИзменитьРеквизиты() в модуле формы
    "Индекс",               # Менеджер перечисления: Индекс(ЗначениеПеречисления)
}


# ─── FIX-4.1: глобальные объекты платформы ───────────────────────────────
#
# `Метаданные.НайтиПоТипу(...)`, `ФоновыеЗадания.Выполнить(...)` —
# слева от точки не модуль конфигурации и не переменная, а свойство
# глобального контекста. Резолвить некуда, пробелом в графе не является.
#
# Отличается от BUILTIN_FUNCS тем, что это ОБЪЕКТЫ (стоят слева от точки),
# а не функции: `module_ref`, а не `method_name`. Список закрытый —
# платформа новых глобальных объектов между релизами почти не добавляет.
PLATFORM_GLOBAL_OBJECTS: set[str] = {
    "Метаданные",
    "ФоновыеЗадания", "РегламентныеЗадания",
    "СериализаторXDTO", "ФабрикаXDTO",
    "ПараметрыСеанса",
    "ПользователиИнформационнойБазы",
    "ВнешниеОбработки", "ВнешниеОтчеты",
    "РасширенияКонфигурации",
    "КритерииОтбора",
    "ПолнотекстовыйПоиск",
    "ХранилищеСистемныхНастроек", "ХранилищеОбщихНастроек",
    "ХранилищеПользовательскихНастроекДинамическихСписков",
    "ХранилищеВариантовОтчетов", "ХранилищеНастроекДанныхФорм",
    "БиблиотекаКартинок", "БиблиотекаМакетовОформленияКомпоновкиДанных",
    "БиблиотекаСтилей",
    "WSСсылки", "WebСервисы", "HTTPСервисы",
    "ГлавныйИнтерфейс", "ГлавныйСтиль",
    "ОбновлениеКонфигурацииБазыДанных",
    "ПланыОбменаСервер",
    "СредстваКриптографии",
    "ЖурналРегистрации",
    "Символы",
    "Шрифты", "Цвета", "Рамки", "Картинки", "Стили",
}

_PLATFORM_GLOBALS_FOLDED: frozenset[str] = frozenset(
    n.lower() for n in PLATFORM_GLOBAL_OBJECTS)

# Имена обработчиков формы (для exclude_handlers в code_dead_procedures).
# Эти процедуры «мёртвы» только статически — их зовёт платформа.
FORM_HANDLERS: set[str] = {
    "ПриСозданииНаСервере", "ПриОткрытии", "ПередЗакрытием", "ПередЗаписью",
    "ПриЗаписи", "ПередЗаписьюНаСервере", "ПриЗаписиНаСервере",
    "ПослеЗаписиНаСервере", "ПослеЗаписи",
    "ОбработкаПроверкиЗаполнения", "ОбработкаПроверкиЗаполненияНаСервере",
    "ОбработкаВыбора", "ОбработкаОповещения",
    "ПриЧтенииНаСервере", "ПриЗагрузкеИзНастроекНаСервере",
    "ПриСохраненииДанныхВНастройкахНаСервере",
    "ПриПолученииДанныхНаСервере",
    "ПриАктивизацииСтроки", "ПриАктивизацииЯчейки",
    "ПриИзменении", "ПриВыборе", "ПередНачаломДобавления",
    "АвтоПодборЗначения", "Очистка", "НачалоВыбора", "ОкончаниеВводаТекста",
    "Регулирование", "ОбработкаКомандыФормы",
    # Подписки на события
    "ПередЗаписью_Справочник", "ПриЗаписи_Справочник",
    "Справочник_ПриСозданииНаСервере", "Справочник_ПриОткрытии",
    "Справочник_ПередЗакрытием", "Справочник_ПослеСозданияНаСервере",
    "Справочник_ПослеОткрытия",
    # 1С-стандарт YAxUnit
    "ИсполняемыеСценарии",
}


# Доп. regex для резолвера: `Plural.Name.method(`.
# Парсер дня 1 не выдаёт этот матч из iter_calls (lookbehind отрезает),
# поэтому пытаемся найти его отдельно — это manager-вызов вида
# `Справочники.АукАукционы.СведенияПоЭтапуАукциона(...)`.
RE_MANAGER_CALL = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<plural>Справочники|Документы|Перечисления|Обработки|Отчеты|'
    r'РегистрыСведений|РегистрыНакопления|РегистрыБухгалтерии|РегистрыРасчета|'
    r'Константы|ПланыВидовХарактеристик|ПланыСчетов|ПланыВидовРасчета|'
    r'ПланыОбмена|БизнесПроцессы|Задачи|ЖурналыДокументов)'
    r'\s*\.\s*(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'\s*\.\s*(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\('
)


# ─── DataClasses ──────────────────────────────────────────────────────────


@dataclass
class TypeRef:
    """Выведенный тип локальной переменной / параметра / return-значения.

    4.6.4 расширил dataclass полями `confidence` и `source` — они нужны
    writer'у для `:INFERRED_TYPE`-рёбер и движку фикс-пойнта для разрешения
    конфликтов (см. PLAN_4_6_4.md разделы 3.2, 3.5).
    """
    kind: str            # "CatalogObject", "DocumentRef", "ValueTable", ...
    target: str          # "Catalog.АукАукционы" (full_name_eng); "" для коллекций
    confidence: float = 1.0          # 1.0 — прямой вывод; <1 — пробросанный/цепочка
    source: str = "local_assign"     # "local_assign" | "constructor" | "chain" |
                                     # "return_type" | "param_propagated" | "ambiguous"

    def type_id(self) -> str:
        """id соответствующего :Type-узла в графе. Формат СИНХРОНИЗИРОВАН
        со слоем 1 (`metadata_xml.py`: `Type:{kind}:{target}` либо `Type:{kind}`),
        чтобы слой 2 переиспользовал узлы слоя 1, а не плодил дубли."""
        if self.target:
            return f"Type:{self.kind}:{self.target}"
        return f"Type:{self.kind}"


# Сентинел «тип параметра конфликтует на разных callsite'ах». Монотонность
# фикс-пойнта: раз параметр стал AMBIGUOUS — он таким и остаётся (решётка
# конечна → завершение гарантировано). AMBIGUOUS-параметр не даёт :CALLS и
# не пишет :INFERRED_TYPE (сознательное упрощение v1, см. план 3.5).
AMBIGUOUS = TypeRef(kind="<ambiguous>", target="", confidence=0.0, source="ambiguous")


@dataclass
class Index:
    """Сводный индекс для резолва."""
    common_modules: set[str] = field(default_factory=set)
    # Все известные id'ы Callable-узлов (после построения skeleton'а).
    callable_ids: set[str] = field(default_factory=set)
    # short name → full_name_eng (`АукАукционы` → `Catalog.АукАукционы`).
    # Если одно короткое имя коллизирует между типами (например, и Catalog.X
    # и Enum.X с одинаковым именем), храним сами full_name_eng, и не пытаемся
    # резолвить short без plural'я.
    metadata_objects: dict[str, str] = field(default_factory=dict)
    # full_name_eng → kind_eng ("Catalog.АукАукционы" → "Catalog").
    # Это набор всех известных :MetadataObject — для проверки existence в OPERATES_ON.
    metadata_full_set: set[str] = field(default_factory=set)
    # Список plural'ей (синхрон с PLURAL_TO_KIND_ENG).
    metadata_kinds_plural: dict[str, str] = field(default_factory=dict)
    # Built-in whitelist.
    builtin_funcs: set[str] = field(default_factory=set)
    # Свойства :CommonModule из Neo4j (для is_server/is_client).
    common_module_props: dict[str, dict] = field(default_factory=dict)
    # FIX-5 часть 2: full_name_eng объекта → имена его реквизитов и
    # табличных частей в нижнем регистре. Данные слоя 1, лежат в графе как
    # :Attribute / :TabularSection. Нужны, чтобы `СохраняемыеСостояния.Свойство()`
    # в модуле объекта обработки перестал считаться ненайденным модулем:
    # слева от точки реквизит объекта, а не модуль.
    object_attributes: dict[str, frozenset[str]] = field(default_factory=dict)


# ─── Сборка индекса ───────────────────────────────────────────────────────


def build_index_from_modules(
    modules: list[ParsedModule],
    metadata_objects: Optional[dict[str, str]] = None,
    common_module_props: Optional[dict[str, dict]] = None,
) -> Index:
    """
    Собирает Index из ParsedModule-листа. Используется в юнит-тестах,
    где Neo4j недоступен.

    `metadata_objects`: short_name → full_name_eng. В тестах задаётся вручную.
    В production используется build_index_from_neo4j().
    """
    idx = Index(
        metadata_kinds_plural=dict(PLURAL_TO_KIND_ENG),
        builtin_funcs=set(BUILTIN_FUNCS),
        common_module_props=dict(common_module_props or {}),
    )

    # Имена общих модулей: "АукОбщийКлиент" (без префикса CommonModule.)
    for m in modules:
        if m.module_kind == "CommonModule":
            short = m.module_id.split(".", 1)[1]
            idx.common_modules.add(short)

    # callable_ids — собираем заранее, до резолва.
    for m in modules:
        for p in m.procedures:
            idx.callable_ids.add(f"{m.module_id}.{p.name}")

    # metadata_objects
    if metadata_objects:
        idx.metadata_objects = dict(metadata_objects)
        idx.metadata_full_set = set(metadata_objects.values())

    return idx


def build_index_from_neo4j(
    neo,
    modules: list[ParsedModule],
) -> Index:
    """
    Собирает Index, читая metadata_objects из живой Neo4j и
    callable_ids из переданного списка модулей (резолвер их сам создаст).
    """
    # 1. Свойства всех CommonModule (для is_server/is_client).
    import json as _json
    rows = neo.rows(
        "MATCH (m:MetadataObject:CommonModule) "
        "RETURN m.id AS id, m.properties_json AS props"
    )
    common_module_props: dict[str, dict] = {}
    for r in rows:
        try:
            props = _json.loads(r.get("props") or "{}")
        except (TypeError, ValueError):
            props = {}
        common_module_props[r["id"]] = {
            "is_server": bool(props.get("Server", True)),
            "is_client": bool(
                props.get("ClientManagedApplication", False)
                or props.get("ClientOrdinaryApplication", False)
            ),
        }

    # 2. Метаданные. Берём всё, что относится к KindEng из PLURAL_TO_KIND_ENG
    # (Catalog/Document/Enum/...). Берём короткое имя `name` для словаря short→full.
    target_kinds = sorted(set(PLURAL_TO_KIND_ENG.values()))
    rows = neo.rows(
        "MATCH (m:MetadataObject) "
        "WHERE m.kind_eng IN $kinds "
        "RETURN m.id AS id, m.name AS name, m.kind_eng AS kind",
        {"kinds": target_kinds},
    )
    short_to_full: dict[str, str] = {}
    full_set: set[str] = set()
    for r in rows:
        full = r["id"]
        full_set.add(full)
        short = r["name"]
        # Если уже есть это короткое имя — оставляем то, что было (или можем
        # перетереть, не критично — резолвер для menager-вызовов работает
        # через plural и не использует short_to_full в одиночку).
        short_to_full.setdefault(short, full)

    # 3. Сборка через build_index_from_modules + дополнительные поля.
    idx = build_index_from_modules(
        modules,
        metadata_objects=short_to_full,
        common_module_props=common_module_props,
    )
    # 4. FIX-5 часть 2: реквизиты и табличные части объектов слоя 1.
    # Читаются одним запросом на весь граф — объектов десятки, обращений
    # к ним тысячи, поэтому дешевле собрать всё сразу.
    rows = neo.rows(
        "MATCH (m:MetadataObject)-[:HAS_ATTRIBUTE|HAS_TABULAR_SECTION]->(a) "
        "RETURN m.id AS id, collect(a.name) AS names"
    )
    object_attributes: dict[str, frozenset[str]] = {}
    for r in rows:
        obj_id = r.get("id")
        names = {n.lower() for n in (r.get("names") or []) if n}
        if obj_id and names:
            object_attributes[obj_id] = frozenset(names)
    idx.object_attributes = object_attributes

    return idx


# ─── Локальный dataflow (v2 — 4.6.4) ─────────────────────────────────────


# `Возврат <выражение>` — для вывода return-типа функции (этап A2).
# Применяется к preprocessed body_text.
RE_RETURN = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_])Возврат\b[ \t]*(?P<expr>[^;\r\n]*)',
)


def _classify_assign_ref(a) -> Optional[TypeRef]:
    """`var = Plural.Name.Method(...)` → TypeRef (или None)."""
    kind = PLURAL_TO_KIND_ENG.get(a.plural)
    if not kind:
        return None
    target_full = f"{kind}.{a.name}"
    pattern = METHOD_TO_KIND.get(a.method)
    if not pattern:
        return None
    type_kind = pattern.replace("<kind>", kind) if "<kind>" in pattern else pattern
    return TypeRef(kind=type_kind, target=target_full,
                   confidence=1.0, source="local_assign")


def _classify_new_assign(a) -> Optional[TypeRef]:
    """`var = Новый Класс` → TypeRef коллекционного типа (или None)."""
    coll_kind = COLLECTION_TYPES.get(a.class_name)
    if not coll_kind:
        return None
    # У коллекций нет target (нет конкретного :MetadataObject) — target="".
    return TypeRef(kind=coll_kind, target="",
                   confidence=1.0, source="constructor")


def infer_local_types(
    proc: ParsedProcedure,
    return_types: Optional[dict[str, "TypeRef"]] = None,
    param_types: Optional[dict[str, "TypeRef"]] = None,
    index: Optional["Index"] = None,
    caller_module_id: Optional[str] = None,
) -> dict[str, TypeRef]:
    """
    Выводит типы локальных переменных процедуры. v2 (4.6.4).

    Сигнатура расширена опциональными аргументами — БЕЗ них поведение
    обратно совместимо с 4.6.2 (паттерн `var = Plural.X.Method()`), плюс
    добавлены конструкторы коллекций и цепочки присваиваний, не требующие
    внешнего контекста. С аргументами включаются inter-procedural источники:

      • `return_types`     — `callable_id → TypeRef`: тип, возвращаемый функцией.
                             Включает паттерн `x = МойМодуль.НайтиЗаказ(...)`.
      • `param_types`      — `param_id → TypeRef`: выведенные типы параметров
                             ЭТОЙ процедуры (предзаполняют var_types — параметр
                             с известным типом ведёт себя как типизированный локал).
      • `index`            — для резолва `module_ref` → `callable_id`
                             (cross-module и self вызовы в `x = ...()`).
      • `caller_module_id` — id модуля `proc` (для локальных `x = Функция(...)`).

    Все присваивания обрабатываются В ПОРЯДКЕ ПОЯВЛЕНИЯ в тексте (по line/col),
    семантика «последний победил» сохранена, но теперь цепочка `b = ...; a = b`
    тоже разрешается: к моменту `a = b` тип `b` уже в словаре.

    Возвращает `dict var_name → TypeRef`.
    """
    return_types = return_types or {}
    param_types = param_types or {}

    var_types: dict[str, TypeRef] = {}

    # 0. Seed: типы параметров этой процедуры (inter-procedural вход).
    #    param_id = "<caller_id>.Param.<name>"; нам нужен var_name.
    if param_types and caller_module_id is not None:
        caller_id = f"{caller_module_id}.{proc.name}"
        for prm in proc.parameters:
            pid = f"{caller_id}.Param.{prm.name}"
            t = param_types.get(pid)
            if t is not None and t is not AMBIGUOUS:
                # Помечаем источник как проброшенный — confidence от param_types.
                var_types[prm.name] = TypeRef(
                    kind=t.kind, target=t.target,
                    confidence=t.confidence, source="param_propagated",
                )

    # 1. Собираем все события присваивания с позициями, чтобы обработать
    #    их строго в порядке появления (нужно для цепочек `a = b`).
    #    event = (line, col, kind_tag, payload)
    events: list[tuple[int, int, str, object]] = []
    for a in iter_assign_refs(proc.body_text, line_offset=proc.line_start):
        events.append((a.line, a.col, "ref", a))
    for a in iter_new_assigns(proc.body_text, line_offset=proc.line_start):
        events.append((a.line, a.col, "new", a))
    for a in iter_var_assigns(proc.body_text, line_offset=proc.line_start):
        events.append((a.line, a.col, "var", a))
    for a in iter_call_assigns(proc.body_text, line_offset=proc.line_start):
        events.append((a.line, a.col, "call", a))

    events.sort(key=lambda e: (e[0], e[1]))

    for _line, _col, tag, payload in events:
        if tag == "ref":
            t = _classify_assign_ref(payload)
            if t is not None:
                var_types[payload.var] = t

        elif tag == "new":
            t = _classify_new_assign(payload)
            if t is not None:
                var_types[payload.var] = t

        elif tag == "var":
            # Цепочка `a = b` (is_plain) — наследуем тип b, если он уже выведен.
            # `a = b.Поле` (not is_plain) — НЕ наследуем (тип поля ≠ тип b).
            if payload.is_plain:
                src_t = var_types.get(payload.src_var)
                if src_t is not None and src_t is not AMBIGUOUS:
                    var_types[payload.var] = TypeRef(
                        kind=src_t.kind, target=src_t.target,
                        confidence=max(0.0, src_t.confidence - 0.05),
                        source="chain",
                    )

        elif tag == "call":
            # `x = [Модуль.]Функция(...)` — return-тип функции, если известен.
            callee_id = _resolve_call_assign_target(
                payload, proc, var_types, index, caller_module_id,
            )
            if callee_id is not None:
                rt = return_types.get(callee_id)
                if rt is not None and rt is not AMBIGUOUS:
                    var_types[payload.var] = TypeRef(
                        kind=rt.kind, target=rt.target,
                        confidence=max(0.0, rt.confidence - 0.05),
                        source="return_type",
                    )

    return var_types


def _resolve_call_assign_target(
    a,                              # ParsedCallAssign
    proc: ParsedProcedure,
    var_types: dict[str, TypeRef],
    index: Optional["Index"],
    caller_module_id: Optional[str],
) -> Optional[str]:
    """
    По `var = [Модуль.]Метод(...)` определяет `callable_id` вызываемой функции.

    Возвращает id для трёх случаев:
      • cross-module call в CommonModule;
      • local call (без module_ref) в том же модуле;
      • call на типизированной переменной (`x = объект.Метод()` где объект
        имеет dataflow-тип) — резолв в Object/Manager Module.
    Иначе None.
    """
    if index is None:
        return None
    mod = a.module_ref
    name = a.method

    if not mod:
        # Локальный вызов в том же модуле.
        if caller_module_id is None:
            return None
        candidate = f"{caller_module_id}.{name}"
        return candidate if candidate in index.callable_ids else None

    # cross-module → CommonModule.
    if mod in index.common_modules:
        candidate = f"CommonModule.{mod}.{name}"
        return candidate if candidate in index.callable_ids else None

    # module_ref — типизированная переменная.
    var_t = var_types.get(mod)
    if var_t is not None and var_t is not AMBIGUOUS:
        role = KIND_TO_MODULE_ROLE.get(var_t.kind)
        if role:
            candidate = f"{var_t.target}.{role}.{name}"
            return candidate if candidate in index.callable_ids else None

    return None


def infer_return_type(
    proc: ParsedProcedure,
    var_types: dict[str, TypeRef],
) -> Optional[TypeRef]:
    """
    Выводит тип, возвращаемый функцией, из её `Возврат <выражение>`.

    v1-эвристика (сознательное упрощение): тип определяется, только если
    ВСЕ `Возврат`-выражения с выводимым типом согласованы (один kind+target).
    Если выражения дают разные типы — возвращаем None (return-тип не выводим).
    Если ни одно выражение не типизируемо — None.

    `var_types` — уже выведенные типы локальных переменных этой функции
    (результат `infer_local_types`).

    Поддерживаемые выражения `Возврат`:
      • `Возврат перем;`              — тип локальной переменной;
      • `Возврат Справочники.X.M();`   — прямой dataflow-паттерн;
      • `Возврат Новый ТаблицаЗначений;` — конструктор коллекции.
    Сложные выражения (`Возврат а + б`, `Возврат ?(...)`) — игнорируются.
    """
    if proc.kind != "Function":
        return None

    found: Optional[TypeRef] = None
    for m in RE_RETURN.finditer(proc.body_text):
        expr = m.group("expr").strip()
        if not expr:
            continue
        t = _type_of_expr(expr, var_types)
        if t is None:
            continue
        if found is None:
            found = t
        elif (found.kind, found.target) != (t.kind, t.target):
            # Конфликтующие return-выражения — не выводим.
            return None
    if found is None:
        return None
    # Return-тип — производный факт: чуть ниже confidence, source маркируем.
    return TypeRef(kind=found.kind, target=found.target,
                   confidence=max(0.0, found.confidence - 0.05),
                   source="return_type")


# Простые выражения для `Возврат` / RHS, чей тип можно определить «на месте».
_RE_BARE_IDENT = re.compile(r'^[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*$')
_RE_NEW_EXPR = re.compile(
    r'^Новый\s+(?P<class>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\b')
_RE_PLURAL_METHOD_EXPR = re.compile(
    r'^(?P<plural>[А-Яа-яЁёA-Za-z]+)\s*\.\s*'
    r'(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\.\s*'
    r'(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\(')


def _type_of_expr(expr: str, var_types: dict[str, TypeRef]) -> Optional[TypeRef]:
    """Тип простого выражения (для `Возврат`/RHS). None если не определяется."""
    expr = expr.strip()
    # Чистый идентификатор → тип локальной переменной.
    if _RE_BARE_IDENT.match(expr):
        t = var_types.get(expr)
        return t if (t is not None and t is not AMBIGUOUS) else None
    # `Новый Класс` → коллекционный тип.
    mn = _RE_NEW_EXPR.match(expr)
    if mn:
        coll = COLLECTION_TYPES.get(mn.group("class"))
        if coll:
            return TypeRef(kind=coll, target="", confidence=1.0,
                           source="constructor")
        return None
    # `Plural.Name.Method(` → dataflow-паттерн.
    mp = _RE_PLURAL_METHOD_EXPR.match(expr)
    if mp:
        kind = PLURAL_TO_KIND_ENG.get(mp.group("plural"))
        if not kind:
            return None
        pattern = METHOD_TO_KIND.get(mp.group("method"))
        if not pattern:
            return None
        type_kind = pattern.replace("<kind>", kind) if "<kind>" in pattern else pattern
        return TypeRef(kind=type_kind, target=f"{kind}.{mp.group('name')}",
                       confidence=1.0, source="local_assign")
    return None


# ─── Резолв одного callsite'а ────────────────────────────────────────────


@dataclass
class ResolveResult:
    """Результат попытки резолва."""
    callee_id: Optional[str] = None     # id Callable если разрешён, иначе None
    reason: str = ""                    # машинная причина (для unresolved CallSite)
    skip: bool = False                  # True если эту запись CallSite не нужно писать
                                        # (built-in, metadata-access без call'а)


def _resolve_call(
    c: ParsedCall,
    caller_module_id: str,
    caller_module_role: str,
    local_vars: dict[str, TypeRef],
    index: Index,
    local_names: Optional[set[str]] = None,
    context_names: Optional[set[str]] = None,
    context_self: Optional[dict[str, str]] = None,
) -> ResolveResult:
    """
    Решает, что делать с одним callsite'ом.

    Решение:
      - Built-in → skip (CallSite не пишется вовсе).
      - `Plural.X` без последующего `.method(...)` (поверх RE_LOCAL_CALL не
        пересекается, но из iter_calls module_ref может оказаться `Справочники`
        — это вспомогательный матч, отбрасываем).
      - cross-module call в CommonModule → CALLS.
      - cross-module call с module_ref = известная локальная переменная
        с dataflow-типом → CALLS в Object/Manager Module.
      - self-reference: module_ref == last segment of caller_module_id → CALLS
        внутрь того же модуля.
      - local call: ищем в callable'ах того же модуля.
      - иначе unresolved.

    `local_names` (FIX-4) — имена, объявленные в области видимости вызывающей
    процедуры (collect_local_names + модульные `Перем`), в нижнем регистре.
    Если `module_ref` лежит в этом множестве, но тип переменной вывести не
    удалось, — это вызов метода объекта платформы, а не ненайденный модуль:
    `reason="object_method"` вместо `unknown_module`.

    `context_names` (FIX-4.1) — имена, которые даёт платформа без объявления:
    свойства контекста модуля и реквизиты формы из `Ext/Form.xml`. Отдельный
    reason `context_property` — чтобы в отчёте было видно, какая подсистема
    сработала. Плюс глобальные объекты платформы (`PLATFORM_GLOBAL_OBJECTS`)
    → `platform_global`; для них внешнее множество не нужно, список закрытый.

    Оба множества по умолчанию None — так удобнее вызывать из тестов, где
    области видимости нет; поведение тогда как до FIX-4.
    """
    name = c.method_name
    mod = c.module_ref  # пустой для local-call

    # 1. Built-in (даже для cross-module — например, `Объект.НСтр(...)`?
    # `НСтр` всегда global, без module_ref. Так что фильтруем only local).
    if not mod and name in index.builtin_funcs:
        return ResolveResult(skip=True, reason="builtin")

    # 2. Если module_ref — это plural метаданных (Справочники/Документы/...)
    # то это либо metadata access (Справочники.X) либо manager call
    # (Справочники.X.метод()), но во втором случае iter_calls дал нам
    # «module=Справочники, method=X» — это паразитный матч, не вызов.
    if mod in index.metadata_kinds_plural:
        return ResolveResult(skip=True, reason="metadata_access_not_call")

    # 3. cross-module → CommonModule.
    if mod and mod in index.common_modules:
        candidate = f"CommonModule.{mod}.{name}"
        if candidate in index.callable_ids:
            return ResolveResult(callee_id=candidate)
        return ResolveResult(reason="unknown_method_in_common_module")

    # 4. cross-module → локальная переменная с dataflow-типом.
    if mod and mod in local_vars:
        var_type = local_vars[mod]
        # 4a. Переменная коллекционного типа (`Новый ТаблицаЗначений` и т.п.).
        #     Метод коллекции — не вызов процедуры конфигурации: skip, не unresolved.
        #     Это «очистка» метрики (см. PLAN_4_6_4.md 3.3 п.4): топ unresolved
        #     `Вставить`/`Добавить`/`Количество`/… — почти все здесь.
        if var_type.kind in _COLLECTION_KIND_SET:
            if name in COLLECTION_METHODS:
                return ResolveResult(skip=True, reason="collection_method")
            # Метод не из known-списка на коллекции — всё равно не резолвимо
            # в :CALLS (у коллекции нет ObjectModule). Но это и не «честный»
            # unresolved — помечаем отдельным reason, не скрываем.
            return ResolveResult(reason="collection_unknown_method")
        # 4b. Ссылочный / объектный тип — резолвим в Object/Manager Module.
        role = KIND_TO_MODULE_ROLE.get(var_type.kind)
        if role:
            candidate = f"{var_type.target}.{role}.{name}"
            if candidate in index.callable_ids:
                return ResolveResult(callee_id=candidate)
            return ResolveResult(reason="method_not_in_resolved_module")
        return ResolveResult(reason="dataflow_kind_no_module_role")

    # 5. self-reference: `Модуль.Метод()` где Модуль = последний сегмент
    # имени текущего модуля. На реальной Котировке встречается, например
    # `АукОбщийКлиент` внутри `АукОбщийКлиент` (через `Новый
    # ОписаниеОповещения(..., АукОбщийКлиент, ...)` — параметр модуля).
    # Но в нашей семантике это не вызов метода, а сам модуль как значение.
    # Поэтому ограничимся честным matching: если `Модуль.Метод(` и есть
    # callable с таким именем — CALLS.
    if mod:
        # Попробуем построить candidate из последнего сегмента caller_module_id.
        # Это поможет, если в коде используется явный полный путь:
        # `Catalog.X.ManagerModule.Метод()` — but it doesn't happen in BSL.
        # Поэтому реально мы попадаем сюда только для unknown_module.
        # См. план: «локальный alias — пропускаем в первой версии».
        #
        # FIX-4: перед тем как объявить модуль ненайденным, проверяем область
        # видимости. `РезультатЗапроса.Выбрать()` — это метод объекта
        # платформы: имя объявлено в процедуре, модуля с таким именем не
        # существует и существовать не должно. Тип переменной мы вывести не
        # смогли (иначе сработала бы ветка 4), но факт объявления —
        # достаточное основание не считать это пробелом в графе.
        #
        # FIX-4.1: сравнение регистронезависимое (BSL таков), плюс две
        # категории имён, которых нет ни в `Перем`, ни в присваиваниях:
        # свойства контекста / реквизиты формы и глобальные объекты платформы.
        mod_folded = mod.lower()
        if local_names and mod_folded in local_names:
            return ResolveResult(reason="object_method")

        # FIX-5: контекстный объект модуля. Стоит ПОСЛЕ проверки области
        # видимости — локальная переменная с именем `Объект` перекрывает
        # реквизит формы, и это не наша догадка, а правило языка.
        #
        # Ненайденный метод сознательно НЕ превращается в пробел, а падает
        # ниже, в `context_property`. Причина: на контекстном объекте зовут
        # ещё и методы платформы — `Объект.Записать()`,
        # `Объект.Модифицированность()`, `Объект.Прочитать()`. У них нет
        # :Callable-адресата, и объявить их пробелами значило бы вернуть
        # ровно тот шум, ради вычистки которого делались FIX-4 и FIX-4.1.
        # Поэтому эта ветка умеет только ДОБАВЛЯТЬ рёбра, но не умеет
        # добавлять пробелы.
        if context_self and mod_folded in context_self:
            candidate = f"{context_self[mod_folded]}.{name}"
            if candidate in index.callable_ids:
                return ResolveResult(callee_id=candidate)

        if context_names and mod_folded in context_names:
            return ResolveResult(reason="context_property")
        if mod_folded in _PLATFORM_GLOBALS_FOLDED:
            return ResolveResult(reason="platform_global")
        return ResolveResult(reason="unknown_module")

    # 6. local call (без module_ref).
    # Резолвится в callable того же модуля.
    candidate = f"{caller_module_id}.{name}"
    if candidate in index.callable_ids:
        return ResolveResult(callee_id=candidate)
    return ResolveResult(reason="unknown_local_method")


# ─── Главная функция: build_call_graph ────────────────────────────────────


# Жёсткая отсечка фикс-пойнта (PLAN_4_6_4.md 3.5). Калибровка на Котировках:
# реестры стабилизируются на 7-й итерации (changed=False), причём поздние
# итерации дают единицы фактов — основная масса приходит за 2-3 прохода.
# Ставим 8 с запасом: фикс-пойнт завершается естественно (по стабилизации),
# отсечка лишь страхует от патологий. Инвариант монотонности (_merge_type_fact:
# реестры только растут / уходят в AMBIGUOUS, решётка конечна) гарантирует
# завершение и без отсечки.
MAX_ITERATIONS = 8


def _merge_type_fact(
    registry: dict[str, TypeRef],
    key: str,
    new_t: TypeRef,
) -> bool:
    """
    Монотонно вливает факт `key → new_t` в реестр (param_types или return_types).

    Правила (инвариант монотонности — гарантия завершения фикс-пойнта):
      • ключа нет           → пишем new_t, возвращаем True (реестр изменился);
      • уже AMBIGUOUS       → не трогаем, False (sticky-дно решётки);
      • тот же (kind,target)→ не трогаем, False (факт повторился);
      • другой (kind,target)→ ставим AMBIGUOUS, True (конфликт → дно).

    Возвращает True, если запись изменилась (нужно для детекта стабилизации).
    """
    old = registry.get(key)
    if old is None:
        registry[key] = new_t
        return True
    if old is AMBIGUOUS:
        return False
    if (old.kind, old.target) == (new_t.kind, new_t.target):
        return False
    # Конфликт — уходим в AMBIGUOUS и больше не двигаемся.
    registry[key] = AMBIGUOUS
    return True


def _collect_arg_param_facts(
    proc: ParsedProcedure,
    caller_module_id: str,
    local_vars: dict[str, TypeRef],
    resolved_calls: list[tuple[ParsedCall, str]],
    params_by_callable: dict[str, list],
) -> list[tuple[str, TypeRef]]:
    """
    B2: из резолвнутых вызовов извлекает факты «аргумент → параметр callee».

    `resolved_calls` — список `(ParsedCall, callee_id)` для этой процедуры.
    Для каждого вызова сопоставляет позиционные аргументы с параметрами
    callee (`HAS_PARAM.position`) и, если тип аргумента выводится из
    `local_vars`, эмитит факт `(callee_param_id, TypeRef)`.

    Возвращает список фактов; агрегация/конфликты — в _merge_type_fact.
    """
    facts: list[tuple[str, TypeRef]] = []
    for c, callee_id in resolved_calls:
        callee_params = params_by_callable.get(callee_id)
        if not callee_params:
            continue
        args = split_args(c.args_text)
        for pos, arg_expr in enumerate(args):
            if pos >= len(callee_params):
                break  # лишние аргументы (вариативность) — пропускаем
            t = _type_of_expr(arg_expr, local_vars)
            if t is None:
                continue
            prm = callee_params[pos]
            param_id = f"{callee_id}.Param.{prm.name}"
            # Источник — проброс из аргумента; чуть снижаем confidence.
            facts.append((param_id, TypeRef(
                kind=t.kind, target=t.target,
                confidence=max(0.0, t.confidence - 0.1),
                source="param_propagated",
            )))
    return facts


def build_call_graph(modules: list[ParsedModule], index: Index) -> dict:
    """
    Собирает полный code_graph (формат write_code_graph).

    Архитектура 4.6.4 — фикс-пойнт вокруг резолва:
      Skeleton (раз):   :Module / :Callable / :Parameter + HAS_METHOD/HAS_PARAM.
      Фикс-пойнт-цикл:  до MAX_ITERATIONS раз гоняем _resolve_pass с текущими
                        реестрами param_types / return_types; из свежих резолвов
                        собираем новые факты «аргумент→параметр» и return-типы;
                        монотонно вливаем в реестры; повторяем пока реестры
                        растут. Инвариант монотонности (_merge_type_fact)
                        гарантирует завершение.
      Финал (раз):      берём результат последнего прохода + :Type-узлы и
                        :INFERRED_TYPE-рёбра из выведенных param_types.

    Внутри _resolve_pass — те же 6 этапов, что в 4.6.2 (CallSite-резолв,
    OPERATES_ON ×2, manager-call), плюс сбор фактов для inter-procedural.

    Возвращает dict с module_nodes / callable_nodes / parameter_nodes /
    callsite_nodes / type_nodes / edges / stats.
    """
    _t_start = time.monotonic()
    n_procs_total = sum(len(m.procedures) for m in modules)

    module_nodes: list[dict] = []
    callable_nodes: list[dict] = []
    parameter_nodes: list[dict] = []

    # ─── 1. Module-узлы (НЕ для CommonModule) ────────────────────────
    seen_module_ids: set[str] = set()
    for m in modules:
        if m.module_kind == "CommonModule":
            continue
        if m.module_id in seen_module_ids:
            continue
        seen_module_ids.add(m.module_id)

        if m.module_kind == "Form":
            name = ".".join(m.module_id.split(".")[-2:])
        else:
            name = m.module_id.split(".")[-1]

        kind_eng = m.module_id.split(".")[0]
        module_nodes.append({
            "id":                  m.module_id,
            "name":                name,
            "kind_eng":            kind_eng,
            "module_role":         m.module_kind,
            "parent_metadata_id":  m.parent_metadata_id,
            "source_path":         m.source_path,
            "is_server":           m.is_server,
            "is_client":           m.is_client,
            "full_name_eng":       m.module_id,
        })

    # ─── 2. Callable + Parameter, рёбра HAS_METHOD/HAS_PARAM ─────────
    # skeleton_edges собираются один раз — фикс-пойнт их не трогает.
    skeleton_edges: list[dict] = []
    # params_by_callable: callee_id → [ParsedParameter] (для B2-сопоставления).
    params_by_callable: dict[str, list] = {}

    for m in modules:
        for proc in m.procedures:
            callable_id = f"{m.module_id}.{proc.name}"
            params_by_callable[callable_id] = proc.parameters

            callable_nodes.append({
                "id":          callable_id,
                "name":        proc.name,
                "full_name":   callable_id,
                "module_id":   m.module_id,
                "kind":        proc.kind,
                "is_export":   proc.is_export,
                "directive":   proc.directive,
                "line_start":  proc.line_start,
                "line_end":    proc.line_end,
                "source_path": m.source_path,
            })

            skeleton_edges.append({
                "rel":   "HAS_METHOD",
                "src":   m.module_id,
                "dst":   callable_id,
                "props": {"kind": proc.kind.lower()},
            })

            for prm in proc.parameters:
                param_id = f"{callable_id}.Param.{prm.name}"
                parameter_nodes.append({
                    "id":            param_id,
                    "name":          prm.name,
                    "position":      prm.position,
                    "is_by_value":   prm.is_by_value,
                    "has_default":   bool(prm.default_value),
                    "default_value": prm.default_value,
                    "callable_id":   callable_id,
                })
                skeleton_edges.append({
                    "rel":   "HAS_PARAM",
                    "src":   callable_id,
                    "dst":   param_id,
                    "props": {"position": prm.position},
                })

    # PERF-5. До Захода 4 build_call_graph не писал в лог ничего до самого
    # конца. На малой конфигурации это тридцать секунд и незаметно; на
    # боевой — десятки минут, в течение которых снаружи нельзя отличить
    # работу от зависания, и диагностика сводится к `docker stats`.
    log.info("build_call_graph: скелет собран — модулей %d, callable %d, "
             "параметров %d (за %s)",
             len(module_nodes), len(callable_nodes), len(parameter_nodes),
             human_sec(time.monotonic() - _t_start))

    # Дозаполняем индекс свежими callable'ами (см. комментарий в 4.6.2).
    fresh_callable_ids = {n["id"] for n in callable_nodes}
    if not index.callable_ids.issuperset(fresh_callable_ids):
        index.callable_ids = index.callable_ids | fresh_callable_ids

    # ─── 3. Фикс-пойнт-цикл ──────────────────────────────────────────
    param_types: dict[str, TypeRef] = {}    # param_id → TypeRef (накопительный)
    return_types: dict[str, TypeRef] = {}   # callable_id → TypeRef (накопительный)

    pass_result: dict = {}
    iteration = 0
    while True:
        iteration += 1
        _t_iter = time.monotonic()

        # PERF-5: прогресс внутри одного прохода — по процедурам. Один проход
        # на боевой конфигурации это 230 тысяч процедур и 723 тысячи
        # callsite'ов, и таких проходов до MAX_ITERATIONS штук.
        iter_prog = ProgressLogger(
            log, f"резолв, итерация {iteration}", total=n_procs_total,
            every_sec=20.0, unit="проц",
        )
        pass_result = _resolve_pass(
            modules, index, params_by_callable,
            param_types, return_types,
            progress=iter_prog,
        )

        # Монотонно вливаем свежие факты в реестры.
        changed = False
        n_new_param = 0
        n_new_return = 0
        for param_id, t in pass_result["param_facts"]:
            if _merge_type_fact(param_types, param_id, t):
                changed = True
                n_new_param += 1
        for callable_id, t in pass_result["return_facts"]:
            if _merge_type_fact(return_types, callable_id, t):
                changed = True
                n_new_return += 1

        log.info(
            "build_call_graph: итерация %d/%d за %s — callsites %d "
            "(resolved %d, unresolved %d); новых типов: параметров %d, "
            "возвратов %d; реестры: param_types %d, return_types %d",
            iteration, MAX_ITERATIONS, human_sec(time.monotonic() - _t_iter),
            len(pass_result["callsite_nodes"]),
            pass_result["stats_resolved"], pass_result["stats_unresolved"],
            n_new_param, n_new_return, len(param_types), len(return_types),
        )

        if not changed:
            break
        # Предупреждаем НА ПОДХОДЕ к пределу, а не по факту его достижения:
        # по факту делать уже нечего, а за две итерации до — видно, что
        # реестры не сходятся, и прогон можно не досиживать.
        if iteration == MAX_ITERATIONS - 1:
            log.warning(
                "build_call_graph: следующая итерация последняя "
                "(MAX_ITERATIONS=%d), а реестры типов ещё растут. Если "
                "остановка произойдёт по пределу — вывод типов недонасыщен.",
                MAX_ITERATIONS,
            )
        if iteration >= MAX_ITERATIONS:
            log.warning("build_call_graph: фикс-пойнт остановлен по MAX_ITERATIONS=%d "
                        "(реестры ещё росли — возможна недонасыщенность)", MAX_ITERATIONS)
            break

    # ─── 4. Финал: :Type-узлы + :INFERRED_TYPE-рёбра (этап D) ────────
    # Из выведенных param_types строим :Type-узлы (переиспользуя формат id
    # слоя 1) и :INFERRED_TYPE-рёбра (:Parameter)-[:INFERRED_TYPE]->(:Type).
    # AMBIGUOUS-параметры пропускаем (сознательное упрощение v1).
    type_nodes_by_id: dict[str, dict] = {}
    inferred_type_edges: list[dict] = []
    for param_id, t in sorted(param_types.items()):
        if t is AMBIGUOUS:
            continue
        tid = t.type_id()
        if tid not in type_nodes_by_id:
            type_nodes_by_id[tid] = {
                "id":     tid,
                "kind":   t.kind,
                "target": t.target or None,
            }
        inferred_type_edges.append({
            "rel":   "INFERRED_TYPE",
            "src":   param_id,
            "dst":   tid,
            "props": {
                "confidence": round(float(t.confidence), 3),
                "source":     t.source,
            },
        })

    type_nodes = list(type_nodes_by_id.values())

    # ─── 5. Сборка финального результата ─────────────────────────────
    edges = skeleton_edges + pass_result["edges"] + inferred_type_edges

    # FIX-4: покрытие считаем только по вызовам, которые претендуют быть
    # обращением к коду конфигурации. `object_method` и родственные причины
    # (NON_CONFIG_CALL_REASONS) — вызовы методов объектов платформы, у них
    # нет и не может быть :Callable-адресата; в знаменателе они занижали
    # метрику примерно на 18 пунктов.
    _resolved = pass_result["stats_resolved"]
    _non_config = pass_result["stats_non_config"]
    _gaps = pass_result["stats_unresolved"] - _non_config
    _denom = _resolved + _gaps

    stats = {
        "module_nodes":    len(module_nodes),
        "callable_nodes":  len(callable_nodes),
        "parameter_nodes": len(parameter_nodes),
        "callsite_nodes":  len(pass_result["callsite_nodes"]),
        "type_nodes":      len(type_nodes),
        "edges_total":     len(edges),
        "resolved":        _resolved,
        "unresolved":      pass_result["stats_unresolved"],
        # FIX-4: разбивка unresolved на настоящие пробелы и методы объектов.
        "unresolved_gaps":          _gaps,
        "unresolved_object_method": _non_config,
        "resolve_coverage_pct": round(100.0 * _resolved / _denom, 2) if _denom else 0.0,
        "skipped":         pass_result["stats_skipped"],
        "reason_counts":   pass_result["reason_counts"],
        "inferred_types":  len(inferred_type_edges),
        "fixpoint_iterations": iteration,
    }

    log.info("build_call_graph: готово за %s — итераций %d, рёбер %d, "
             "покрытие %.2f%%",
             human_sec(time.monotonic() - _t_start), iteration, len(edges),
             stats["resolve_coverage_pct"])

    return {
        "module_nodes":    module_nodes,
        "callable_nodes":  callable_nodes,
        "parameter_nodes": parameter_nodes,
        "callsite_nodes":  pass_result["callsite_nodes"],
        "type_nodes":      type_nodes,
        "edges":           edges,
        "stats":           stats,
    }


def _resolve_pass(
    modules: list[ParsedModule],
    index: Index,
    params_by_callable: dict[str, list],
    param_types: dict[str, TypeRef],
    return_types: dict[str, TypeRef],
    progress=None,
) -> dict:
    """
    Один полный проход резолва (внутренность фикс-пойнта).

    Резолвит все callsite'ы с учётом текущих реестров `param_types` /
    `return_types`, попутно собирая новые факты для следующей итерации:
      • `param_facts`  — [(param_id, TypeRef)] из сопоставления аргументов
                         резолвнутых вызовов с параметрами callee (B2);
      • `return_facts` — [(callable_id, TypeRef)] из `Возврат`-выражений (A2).

    Возвращает dict: callsite_nodes, edges (только не-skeleton: CALL_SITE,
    CALLS, RESOLVES_TO_CALLEE, OPERATES_ON), stats_*, reason_counts,
    param_facts, return_facts.

    НЕ мутирует param_types / return_types — только читает их.
    """
    callsite_nodes: list[dict] = []
    edges: list[dict] = []
    stats_resolved = 0
    stats_unresolved = 0
    stats_skipped = 0
    # FIX-4: часть unresolved, которая вообще не претендует быть обращением
    # к коду конфигурации (см. NON_CONFIG_CALL_REASONS). Считается отдельно
    # и исключается из знаменателя покрытия.
    stats_non_config = 0
    reason_counts: dict[str, int] = {}

    param_facts: list[tuple[str, TypeRef]] = []
    return_facts: list[tuple[str, TypeRef]] = []

    for m in modules:
        for proc in m.procedures:
            caller_id = f"{m.module_id}.{proc.name}"
            # Локальный dataflow v2 — с inter-procedural контекстом.
            local_vars = infer_local_types(
                proc, return_types, param_types, index, m.module_id,
            )
            # FIX-4: имена области видимости — параметры/Перем/LHS/циклы
            # процедуры плюс модульные `Перем`. Нужны, чтобы отличить
            # метод объекта от ненайденного модуля.
            local_names = collect_local_names(proc) | m.module_vars

            # FIX-5: имена контекста, у которых адресат выводится
            # детерминированно, и реквизиты родительского объекта из слоя 1.
            context_self = context_self_modules(
                m.module_kind, m.module_id, m.parent_metadata_id)
            context_names = m.context_names
            if m.parent_metadata_id:
                extra = index.object_attributes.get(m.parent_metadata_id)
                if extra:
                    context_names = context_names | extra

            # Накопитель резолвнутых вызовов этой процедуры — для B2.
            resolved_calls: list[tuple[ParsedCall, str]] = []

            for c in iter_calls(proc.body_text, line_offset=proc.line_start):
                res = _resolve_call(c, m.module_id, m.module_kind, local_vars,
                                    index, local_names, context_names,
                                    context_self)

                if res.skip:
                    stats_skipped += 1
                    reason_counts[res.reason] = reason_counts.get(res.reason, 0) + 1
                    continue

                cs_id = f"{caller_id}:{c.line}:{c.col}"
                callsite_nodes.append({
                    "id":          cs_id,
                    "caller_id":   caller_id,
                    "module_ref":  c.module_ref,
                    "method_name": c.method_name,
                    "line":        c.line,
                    "col":         c.col,
                    "resolved":    res.callee_id is not None,
                    "reason":      "" if res.callee_id else res.reason,
                })
                edges.append({
                    "rel":   "CALL_SITE",
                    "src":   caller_id,
                    "dst":   cs_id,
                    "props": {},
                })

                if res.callee_id:
                    stats_resolved += 1
                    resolved_calls.append((c, res.callee_id))
                    edges.append({
                        "rel":   "CALLS",
                        "src":   caller_id,
                        "dst":   res.callee_id,
                        "props": {"line": c.line, "callsite": cs_id},
                    })
                    edges.append({
                        "rel":   "RESOLVES_TO_CALLEE",
                        "src":   cs_id,
                        "dst":   res.callee_id,
                        "props": {},
                    })
                else:
                    stats_unresolved += 1
                    if res.reason in NON_CONFIG_CALL_REASONS:
                        stats_non_config += 1
                    reason_counts[res.reason] = reason_counts.get(res.reason, 0) + 1

            # ── B2: факты «аргумент → параметр callee» ──
            param_facts.extend(_collect_arg_param_facts(
                proc, m.module_id, local_vars, resolved_calls, params_by_callable,
            ))

            # ── A2: return-тип этой процедуры (если функция) ──
            rt = infer_return_type(proc, local_vars)
            if rt is not None:
                return_facts.append((caller_id, rt))

            # ── 4. OPERATES_ON: metadata access (`Справочники.X`) ──
            for ma in iter_metadata_access(proc.body_text, line_offset=proc.line_start):
                kind_eng = PLURAL_TO_KIND_ENG.get(ma.plural)
                if not kind_eng:
                    continue
                target_full = f"{kind_eng}.{ma.name}"
                if target_full in index.metadata_full_set:
                    edges.append({
                        "rel":   "OPERATES_ON",
                        "src":   caller_id,
                        "dst":   target_full,
                        "props": {"via": ma.plural, "access": "manager_collection"},
                    })

            # ── 5. OPERATES_ON: ПредопределенноеЗначение ──
            for pd in iter_predef(proc.body_text_raw, line_offset=proc.line_start):
                parts = pd.ref.split(".")
                if len(parts) < 2:
                    continue
                kind_eng = PREDEF_RU_TO_KIND_ENG.get(parts[0])
                if not kind_eng:
                    continue
                target_full = f"{kind_eng}.{parts[1]}"
                if target_full in index.metadata_full_set:
                    edges.append({
                        "rel":   "OPERATES_ON",
                        "src":   caller_id,
                        "dst":   target_full,
                        "props": {"via": "predefined_value", "access": "read"},
                    })

            # ── 6. Manager call: Plural.Name.Method() → CALLS ──
            for mc in RE_MANAGER_CALL.finditer(proc.body_text):
                kind_eng = PLURAL_TO_KIND_ENG.get(mc.group("plural"))
                if not kind_eng:
                    continue
                target = f"{kind_eng}.{mc.group('name')}"
                if target not in index.metadata_full_set:
                    continue
                candidate = f"{target}.ManagerModule.{mc.group('method')}"
                if candidate in index.callable_ids:
                    line = proc.body_text.count("\n", 0, mc.start()) + 1 + proc.line_start
                    nl = proc.body_text.rfind("\n", 0, mc.start())
                    col = mc.start() - nl - 1 if nl >= 0 else mc.start()
                    cs_id = f"{caller_id}:{line}:{col}"
                    if not any(n["id"] == cs_id for n in callsite_nodes):
                        callsite_nodes.append({
                            "id":          cs_id,
                            "caller_id":   caller_id,
                            "module_ref":  f"{mc.group('plural')}.{mc.group('name')}",
                            "method_name": mc.group('method'),
                            "line":        line,
                            "col":         col,
                            "resolved":    True,
                            "reason":      "",
                        })
                        edges.append({
                            "rel":   "CALL_SITE",
                            "src":   caller_id,
                            "dst":   cs_id,
                            "props": {},
                        })
                    stats_resolved += 1
                    edges.append({
                        "rel":   "CALLS",
                        "src":   caller_id,
                        "dst":   candidate,
                        "props": {"line": line, "callsite": cs_id},
                    })
                    edges.append({
                        "rel":   "RESOLVES_TO_CALLEE",
                        "src":   cs_id,
                        "dst":   candidate,
                        "props": {},
                    })

            # PERF-5: шаг прогресса — по процедуре, а не по модулю. Модулей
            # 14 тысяч, процедур 230 тысяч, и на модуле с сотней процедур
            # шаг по модулю даёт рваную картину.
            if progress is not None:
                progress.step(
                    extra=f"разрешено {stats_resolved}, пробелов "
                          f"{stats_unresolved - stats_non_config}"
                )

    return {
        "callsite_nodes":   callsite_nodes,
        "edges":            edges,
        "stats_resolved":   stats_resolved,
        "stats_unresolved": stats_unresolved,
        "stats_skipped":    stats_skipped,
        "stats_non_config": stats_non_config,
        "reason_counts":    reason_counts,
        "param_facts":      param_facts,
        "return_facts":     return_facts,
    }
