"""
Парсер BSL (1С) — слой 2 графа метаданных (4.6.2).
======================================================

Источник правды — .bsl файлы XML-выгрузки. Парсер reuse-free от Neo4j,
тестируется на синтетических BSL-сниппетах (см. tests_bsl_parser.py).

Что парсит:
  • Декларации: `Процедура` и `Функция`, с параметрами/Знач/default/Экспорт.
  • Директивы: `&НаКлиенте`, `&НаСервере`, `&НаСервереБезКонтекста`, …
  • Body двух типов: `body_text` (после препроцессора — литералы и комменты
    затёрты пробелами, длины и переносы строк сохраняются) и `body_text_raw`
    (оригинал для `ПредопределенноеЗначение("…")`).
  • Cross-module call: `Модуль.Метод(…)`.
  • Локальный call: `Метод(…)` без префикса.
  • Прямой доступ к метаданным: `Справочники.X`, `Документы.X`, …
  • ПредопределенноеЗначение("Перечисление.X.Y") — из оригинала.
  • Присваивание-источник типа (для базового dataflow):
    `Х = Справочники.Y.СоздатьЭлемент()` и т.п.
  • 4.6.4: конструкторы `Х = Новый ТаблицаЗначений` (iter_new_assigns),
    цепочки `а = б` / `а = б.Поле` (iter_var_assigns),
    присваивания из вызова `х = Модуль.Функция(...)` (iter_call_assigns),
    текст аргументов callsite'а `ParsedCall.args_text` (split_args).
  • FIX-4: множество локальных имён области видимости — параметры, `Перем`,
    LHS присваиваний, переменные циклов (collect_local_names) и `Перем`
    уровня модуля (collect_module_var_names). Резолверу это нужно, чтобы
    отличить `РезультатЗапроса.Выбрать()` (метод объекта платформы) от
    `ОбщегоНазначения.Метод()` (действительно ненайденный модуль).

Не парсит:
  • `Перем X;` не создаёт узел :Variable в графе — имя попадает только
    в множество из collect_local_names (FIX-4).
  • Inline-лямбды (в BSL их нет).
  • `#Если` не «дереференсятся» — обе ветки парсятся как есть.
  • Динамические вызовы `Выполнить(…)`, `Вычислить(…)` — оставляются как обычный call,
    резолвер пометит как `dynamic`.

API:
  ParsedParameter, ParsedProcedure, ParsedModule, ParsedCall, ParsedMetaAccess,
  ParsedPredef, ParsedAssign, ParsedNewAssign, ParsedVarAssign,
  ParsedCallAssign — dataclasses.

  walk_workspace_bsl(root, modules_info)  → list[ParsedModule]
  parse_bsl_module(path, module_id, module_kind, parent_metadata_id, context)
                                          → ParsedModule
  iter_calls(body_text_pre, line_offset)  → Iterator[ParsedCall]
  iter_metadata_access(body_text_pre)     → Iterator[ParsedMetaAccess]
  iter_predef(body_text_raw)              → Iterator[ParsedPredef]
  iter_assign_refs(body_text_pre)         → Iterator[ParsedAssign]
  iter_new_assigns(body_text_pre)         → Iterator[ParsedNewAssign]   (4.6.4 A1)
  iter_var_assigns(body_text_pre)         → Iterator[ParsedVarAssign]   (4.6.4 A2)
  iter_call_assigns(body_text_pre)        → Iterator[ParsedCallAssign]  (4.6.4 A2)
  split_args(args_text)                   → list[str]                  (4.6.4 B1)
  collect_local_names(proc)               → set[str]                   (FIX-4)
  collect_module_var_names(text)          → set[str]                   (FIX-4)
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional


from progress_log import ProgressLogger

# A-2: общий счётчик «вход против выхода» (см. shortfall.py).
try:
    from shortfall import Tally
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shortfall import Tally

log = logging.getLogger(__name__)


# ─── DataClasses ──────────────────────────────────────────────────────────


@dataclass
class ParsedParameter:
    name: str
    position: int           # 0-based
    is_by_value: bool       # есть ли "Знач" перед именем
    default_value: str = "" # текст RHS после "=", без вычисления; "" если default отсутствует


@dataclass
class ParsedProcedure:
    name: str
    kind: str                       # "Procedure" | "Function"
    is_export: bool
    directive: str                  # "" | "НаКлиенте" | "НаСервере" | "НаСервереБезКонтекста" | ...
    parameters: list[ParsedParameter] = field(default_factory=list)
    body_text: str = ""             # preprocessed (литералы/комменты затёрты)
    body_text_raw: str = ""         # оригинал
    line_start: int = 0             # 1-based строка декларации
    line_end: int = 0               # 1-based строка КонецПроцедуры/КонецФункции


@dataclass
class ParsedModule:
    module_id: str                  # "CommonModule.АукОбщийКлиент" | "Catalog.X.ObjectModule" | ...
    module_kind: str                # "CommonModule" | "ObjectModule" | "ManagerModule" | "Form"
    parent_metadata_id: Optional[str]  # "Catalog.АукАукционы" для не-CommonModule
    source_path: str                # POSIX relpath от workspace
    is_server: bool                 # из флагов CommonModule.xml или по контексту (ObjectModule=server)
    is_client: bool
    procedures: list[ParsedProcedure] = field(default_factory=list)
    module_vars: set[str] = field(default_factory=set)
    """FIX-4: имена `Перем` уровня модуля (объявлены до первой процедуры).

    Такая переменная видна во всех процедурах модуля, поэтому резолвер
    объединяет это множество с локальными именами каждой процедуры.
    Пустое множество — валидное состояние (модуль без модульных переменных
    или ParsedModule, собранный в тесте напрямую).
    Имена в нижнем регистре (FIX-4.1).
    """
    context_names: set[str] = field(default_factory=set)
    """FIX-4.1: имена, доступные без объявления — их даёт платформа.

    Свойства контекста модуля (`Ссылка` в модуле объекта, `Параметры` и
    `Элементы` в модуле формы) плюс реквизиты управляемой формы из
    `Ext/Form.xml`. От `module_vars` отделены ради диагностики: резолвер
    помечает такие вызовы отдельным reason `context_property`, и в отчёте
    видно, какая из подсистем FIX-4 сработала.
    Имена в нижнем регистре.
    """


@dataclass
class ParsedCall:
    """Распарсенный callsite (без резолва)."""
    module_ref: str                 # "АукОбщийВызовСервера" | "" если локальный
    method_name: str
    line: int                       # 1-based в файле
    col: int                        # 0-based смещение в строке
    is_local: bool                  # True если RE_LOCAL_CALL
    args_text: str = ""             # 4.6.4 B1: сырой текст между скобок вызова
                                    # (с балансировкой); "" если скобки пусты
                                    # или не удалось сбалансировать.


@dataclass
class ParsedMetaAccess:
    """Обращение к коллекции метаданных: `Справочники.X` (без вызова метода)."""
    plural: str                     # "Справочники", "Документы", ...
    name: str                       # "АукАукционы"
    line: int
    col: int


@dataclass
class ParsedPredef:
    """ПредопределенноеЗначение("Перечисление.X.Y[.Z]")."""
    ref: str                        # "Перечисление.АукСтатусыАукционов.НовыйЭтап"
    line: int
    col: int


@dataclass
class ParsedAssign:
    """Присваивание-источник типа: `var = Plural.Name.Method(...)`."""
    var: str                        # имя переменной
    plural: str                     # "Справочники" / "Документы" / ...
    name: str                       # "АукАукционы"
    method: str                     # "СоздатьЭлемент" / "НайтиПоКоду" / ...
    line: int
    col: int


@dataclass
class ParsedNewAssign:
    """Присваивание конструктора: `var = Новый Класс[(...)]`.

    Введено в 4.6.4 (этап A1) — даёт тип коллекций/платформенных объектов
    (`ТаблицаЗначений`, `Структура`, `Массив`, `Запрос`, …). Резолвер
    использует его, чтобы методы на таких переменных уходили в `skip`
    (`collection_method`), а не в `unresolved`.
    """
    var: str                        # имя переменной
    class_name: str                 # "ТаблицаЗначений" / "Структура" / "Запрос" / ...
    line: int
    col: int


@dataclass
class ParsedVarAssign:
    """Присваивание из другой переменной (цепочка): `var = other` или
    `var = other.Поле`.

    Введено в 4.6.4 (этап A2). `src_var` — имя RHS-переменной (первый
    сегмент, если RHS — это `other.Поле.Поле`). `is_plain` True только
    для чистого `var = other` (без точек) — такой случай безопасен для
    наследования типа; для `var = other.Поле` тип RHS не равен типу
    `other`, поэтому `is_plain=False` и резолвер его игнорирует в v1.
    """
    var: str                        # LHS-переменная
    src_var: str                    # первый сегмент RHS
    is_plain: bool                  # True если RHS — чистый идентификатор без точек
    line: int
    col: int


@dataclass
class ParsedCallAssign:
    """Присваивание из вызова: `var = [Модуль.]Функция(...)`.

    Введено в 4.6.4 (этап A2) — для вывода типа через return-тип функции.
    `module_ref` пуст для локального вызова. Резолвер сопоставляет
    `(module_ref, method)` со своим реестром return-типов.
    """
    var: str                        # LHS-переменная
    module_ref: str                 # "" для локального вызова, иначе имя модуля/переменной
    method: str                     # имя вызываемой функции
    line: int
    col: int


# ─── Препроцессор ─────────────────────────────────────────────────────────


# Строковый литерал BSL: "..." с поддержкой удвоенных кавычек "" внутри
# и многострочных продолжений через "...|...". На самом деле многострочный
# литерал в BSL — это просто одна последовательность от " до " со всеми
# переносами строк и | внутри. Регулярка ниже обрабатывает корректно.
RE_STRING_LITERAL = re.compile(r'"(?:[^"]|"")*"')
RE_LINE_COMMENT   = re.compile(r'//[^\r\n]*')
# FIX-36. Строки и комментарии — ОДНИМ проходом слева направо: что началось
# раньше, то и затирается. Прежде было два прохода — сначала все строки,
# потом все комментарии, — и кавычка внутри комментария
# (`//… по ключу, "ДатаТранзакции, …`) открывала «строку», которая тянулась
# через переносы до следующей кавычки где-то в коде. Код между ними
# пропадал из разбора; если в эту дыру попадал `КонецПроцедуры`, процедура
# выпадала целиком («декларация без закрывающего КонецПроцедуры»), если
# нет — пропадали молча её вызовы.
#
# FIX-38. Литерал описан по правилам платформы, а не «от кавычки до
# кавычки»: начинается `"`, на следующих строках продолжается только
# строками, начинающимися с `|`, а между ними допускаются строки-комментарии
# `//…` — закомментированный кусок текста запроса, и кавычки в нём
# (`//"ВЫБРАТЬ`, `//|  ТОГДА ""КБТ""`) литерал не закрывают. Кавычка,
# не закрытая в своей строке и без продолжения `|`, литералом не считается:
# дальше своей строки она ничего не затирает и код до следующей кавычки
# не съест.
_STR_LINE = r'(?:[^"\r\n]|"")*'
RE_STRING_OR_COMMENT = re.compile(
    r'"' + _STR_LINE
    + r'(?:\r?\n(?:[ \t]*//[^\r\n]*\r?\n)*[ \t]*\|' + _STR_LINE + r')*"'
    r'|//[^\r\n]*'
)


def _preprocess(text: str) -> str:
    """
    Затирает строковые литералы и однострочные комментарии пробелами,
    сохраняя позиции (по байтам и переносы строк).

    Пример:
        А = "ab"
        // foo
        Б = 1
    →
        А = "  "
        ------
        Б = 1
    Где "------" — пробелы той же длины что и `// foo`.
    """
    def _blank(m: re.Match) -> str:
        return "".join(" " if c != "\n" else "\n" for c in m.group(0))
    return RE_STRING_OR_COMMENT.sub(_blank, text)


# ─── Регулярки ────────────────────────────────────────────────────────────


# Декларация процедуры/функции.
# Захватывает (опционально) предшествующую строку с директивой `&НаКлиенте` и т.п.
# Имена в BSL — кириллица/латиница/цифры/_; начинаются с буквы или _.
RE_DECL = re.compile(
    r'(?:^[ \t]*&(?P<dir>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)[ \t]*\r?\n)?'
    r'^[ \t]*(?P<kind>Процедура|Функция)[ \t]+'
    r'(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)[ \t]*'
    r'\((?P<params>[^)]*)\)'
    r'(?:[ \t]+(?P<export>Экспорт))?',
    re.MULTILINE,
)

# Конец процедуры/функции
# FIX-37. Не обязательно с начала строки: в BSL законно
# `… Отказ); КонецПроцедуры` — оператор и конец процедуры на одной строке
# (Catalogs/ОбъектыКВыгрузкеВКликХаус, ПередЗаписью). Ищем отдельное слово в
# тексте после препроцессора (строки и комментарии там уже затёрты):
# не часть идентификатора и не свойство после точки.
RE_END = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])(?P<end>КонецПроцедуры|КонецФункции)'
    r'(?![А-Яа-яЁёA-Za-z0-9_])'
)

# Параметр в списке (применяется к содержимому скобок декларации).
# default_value может содержать вложенные вызовы — `f(...)` — но не запятые
# верхнего уровня, поэтому используем простой подход: до конца строки или
# до запятой верхнего уровня. Проще через расщепление с учётом скобок —
# см. _split_params() ниже.
RE_PARAM_SINGLE = re.compile(
    r'^\s*(?:(?P<byval>Знач)\s+)?'
    r'(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*'
    r'(?:=\s*(?P<default>.+?))?\s*$',
    re.DOTALL,
)

# Cross-module call: `Имя.метод(`
# Lookbehind не пропускает `.` перед `Имя` — иначе `A.B.C(` будет матчить
# и `A.B.` и `B.C.` (последнее — это вызов метода объекта, не модуля).
RE_CROSSMODULE_CALL = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<module>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\.\s*'
    r'(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\(',
)

# Локальный вызов: `Метод(` (без префикса).
RE_LOCAL_CALL = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\(',
)

# Прямое обращение к коллекции метаданных: `Справочники.X` без вызова.
# Применяется к preprocessed-тексту (литералы уже затёрты — `ПредопределенноеЗначение`
# не даст false-positive).
# Списки плюралей синхронизированы с PLURAL_TO_KIND в bsl_resolver.py.
RE_METADATA_ACCESS = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_])'
    r'(?P<plural>Справочники|Документы|Перечисления|Обработки|Отчеты|'
    r'РегистрыСведений|РегистрыНакопления|РегистрыБухгалтерии|РегистрыРасчета|'
    r'Константы|ПланыВидовХарактеристик|ПланыСчетов|ПланыВидовРасчета|'
    r'ПланыОбмена|БизнесПроцессы|Задачи|ЖурналыДокументов)'
    r'\s*\.\s*(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)',
)

# ПредопределенноеЗначение("…") — применяется к ОРИГИНАЛЬНОМУ тексту
# (после препроцессора содержимое литерала будет затёрто).
RE_PREDEF = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_])'
    r'ПредопределенноеЗначение\s*\(\s*"(?P<ref>[^"]+)"\s*\)',
)

# Присваивание-источник типа: var = Plural.Name.Method(
# Применяется к preprocessed (правая часть не должна попасть в строковый литерал).
RE_ASSIGN_REF = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*=\s*'
    r'(?P<plural>Справочники|Документы|Перечисления|РегистрыСведений|РегистрыНакопления)'
    r'\s*\.\s*(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'\s*\.\s*(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\(',
)

# --- 4.6.4 этап A1: конструктор `var = Новый Класс` ---
# Ловим `var = Новый ТаблицаЗначений` и `var = Новый Структура("a,b", 1, 2)`.
# `Новый Класс` без скобок (без аргументов) — валидный BSL, поэтому `(` опционально.
# `(?![.\w])` после класса — чтобы не схватить `Новый Объект.Метод` (такого в BSL
# нет, но защищаемся). Класс — один идентификатор; обобщённые типы вроде
# `Новый Массив(3)` тоже сюда попадают (class_name = "Массив").
RE_NEW_ASSIGN = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*=\s*'
    r'Новый\s+(?P<class>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'(?![А-Яа-яЁёA-Za-z0-9_.])',
)

# --- 4.6.4 этап A2: цепочка `var = other` или `var = other.Поле` ---
# RHS — идентификатор, опционально с `.Поле.Поле…`. Останавливаемся на
# `;`, конце строки, операторе или открывающей скобке (то — вызов, не наш
# случай: вызовы ловит RE_CALL_ASSIGN). `is_plain` определяем постпроцессингом.
RE_VAR_ASSIGN = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*=\s*'
    r'(?P<rhs>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*'
    r'(?:\s*\.\s*[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)*)'
    r'\s*(?P<after>[;\r\n)]|$)',
    re.MULTILINE,
)

# --- 4.6.4 этап A2: присваивание из вызова `var = [Модуль.]Функция(...)` ---
# Cross-module: `var = МойМодуль.НайтиЗаказ(...)`.
# Локальный:    `var = НайтиЗаказ(...)`.
# Регистрируем оба; module_ref="" означает локальный вызов.
RE_CALL_ASSIGN = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*=\s*'
    r'(?:(?P<module>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\.\s*)?'
    r'(?P<method>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*\(',
)

# --- FIX-4: имена, объявленные в области видимости ---
#
# Задача — детерминированно понять, что `РезультатЗапроса` в
# `РезультатЗапроса.Выбрать()` — это переменная, а не модуль конфигурации.
# Никаких догадок по префиксам имён: смотрим только на то, что объявлено.

# `Перем А, Б Экспорт;` — и в теле процедуры, и на уровне модуля.
RE_VAR_DECL = re.compile(
    r'^[ \t]*Перем[ \t]+(?P<names>[^;\r\n]+)',
    re.MULTILINE,
)

# LHS присваивания. `=` в BSL — это и присваивание, и сравнение, поэтому
# требуем, чтобы идентификатор стоял в начале оператора: начало строки или
# сразу после `;`. Так `Если А = 1 Тогда` не даёт ложного «объявления А»,
# а `А = 1;` и `А = 1; Б = 2;` — дают.
RE_ASSIGN_LHS = re.compile(
    r'(?:^|;)[ \t]*'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'[ \t]*=(?!=)',
    re.MULTILINE,
)

# `Для Каждого ТекДанные Из … Цикл` — переменная объявляется циклом.
RE_FOREACH_VAR = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])Для[ \t]+Каждого[ \t]+'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'[ \t]+Из(?![А-Яа-яЁёA-Za-z0-9_])',
)

# `Для Сч = 1 По N Цикл`. `Для Каждого Х Из` сюда не попадает: после
# `Каждого` идёт идентификатор, а не `=`.
RE_FOR_VAR = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])Для[ \t]+'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)'
    r'[ \t]*=(?!=)',
)


# ─── FIX-7: регистронезависимость ключевых слов ──────────────────────────
#
# BSL регистронезависим: `Конецфункции`, `КОНЕЦФУНКЦИИ` и `КонецФункции` —
# одно и то же для платформы. Регулярки выше писались с literal-написанием
# ключевых слов и без re.IGNORECASE, то есть видели только канонический
# регистр.
#
# На маленькой конфигурации это не проявлялось: код там писан однообразно.
# На боевой (230 796 процедур) сразу всплыли 28 предупреждений «декларация
# без закрывающего КонецПроцедуры/Функции» — у всех конец написан со
# строчной буквы. И это была видимая часть: пропущенный КОНЕЦ хотя бы
# предупреждает, а пропущенное ОБЪЯВЛЕНИЕ или вызов исчезают молча — они не
# попадают в граф, и об этом никто не узнаёт.
#
# Тот же урок, что FIX-4.1 («BSL регистронезависим»), просто применённый
# тогда только к сравнению имён, а не к их поиску.
#
# Флаг добавляется одним местом, а не правкой семнадцати определений:
# причина у всех одна, и держать её в одном месте честнее. Классы символов
# в шаблонах и так покрывают оба регистра, поэтому IGNORECASE влияет ровно
# на literal-части — то есть на ключевые слова, чего мы и добиваемся.
#
# RE_STRING_OR_COMMENT (и его части) не трогаем: там нет ключевых слов,
# только кавычки и слэши.

_KEYWORD_PATTERNS = (
    "RE_DECL", "RE_END", "RE_PARAM_SINGLE",
    "RE_CROSSMODULE_CALL", "RE_LOCAL_CALL", "RE_METADATA_ACCESS",
    "RE_PREDEF", "RE_ASSIGN_REF", "RE_NEW_ASSIGN", "RE_VAR_ASSIGN",
    "RE_CALL_ASSIGN", "RE_VAR_DECL", "RE_ASSIGN_LHS",
    "RE_FOREACH_VAR", "RE_FOR_VAR",
)

for _name in _KEYWORD_PATTERNS:
    _rx = globals()[_name]
    if not _rx.flags & re.IGNORECASE:
        globals()[_name] = re.compile(_rx.pattern, _rx.flags | re.IGNORECASE)
del _name, _rx

# Первый идентификатор в элементе списка `Перем` (`А Экспорт` → `А`).
_RE_FIRST_IDENT = re.compile(
    r'^[ \t]*(?P<name>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)')


# ─── Защита от ложных матчей ──────────────────────────────────────────────


# Декларации — частая «жертва» RE_CROSSMODULE_CALL: правая часть `Имя(` совпадает.
# Префикс перед матчем проверяется на 15-символовом окне.
_RE_KEYWORD_BEFORE = re.compile(r'(Процедура|Функция)\s*$')


def _is_declaration_at(text: str, pos: int) -> bool:
    """True, если в 15-байтном окне слева от pos стоит ключевое слово
    `Процедура` или `Функция` (т.е. этот матч — не вызов, а декларация)."""
    left = text[max(0, pos - 30): pos]
    return _RE_KEYWORD_BEFORE.search(left) is not None


# FIX-4.1: `Новый ЗаписьJSON(...)` — конструктор класса платформы, а не вызов
# процедуры конфигурации. RE_LOCAL_CALL матчил имя класса как локальный вызов,
# и резолвер честно не находил такой процедуры → unknown_local_method. Само
# слово `Новый` уже отсекается через _BSL_KEYWORDS, но имя класса за ним — нет.
_RE_NEW_BEFORE = re.compile(r'(?<![А-Яа-яЁёA-Za-z0-9_.])Новый\s+$')


def _is_constructor_at(text: str, pos: int) -> bool:
    """True, если слева от pos стоит `Новый` — значит это имя класса."""
    left = text[max(0, pos - 30): pos]
    return _RE_NEW_BEFORE.search(left) is not None


# ─── Помощники ────────────────────────────────────────────────────────────


def _line_of(text: str, offset: int) -> int:
    """1-based номер строки, в которой находится байт по `offset`."""
    return text.count("\n", 0, offset) + 1


def _col_of(text: str, offset: int) -> int:
    """0-based смещение в строке (от ближайшего \\n до offset)."""
    nl = text.rfind("\n", 0, offset)
    return offset - nl - 1 if nl >= 0 else offset


def _split_params(s: str) -> list[str]:
    """
    Делит строку параметров по запятым верхнего уровня
    (игнорирует запятые внутри скобок default-выражений).
    """
    if not s.strip():
        return []
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _extract_args(text: str, open_paren_pos: int) -> tuple[str, int]:
    """
    4.6.4 B1: извлекает сбалансированное содержимое скобок вызова.

    `open_paren_pos` — индекс символа `(` в `text`. Возвращает
    `(args_text, close_pos)`, где `args_text` — текст между скобок (без
    самих скобок), `close_pos` — индекс закрывающей `)`.

    Если баланс не сходится (обрыв файла, незакрытая скобка) — возвращает
    `("", -1)`: резолвер тогда работает как раньше, без аргументов.

    Текст уже preprocessed (литералы/комменты затёрты пробелами), поэтому
    скобки внутри строк не мешают балансировке.
    """
    assert text[open_paren_pos] == "("
    depth = 0
    i = open_paren_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_pos + 1:i], i
        i += 1
    return "", -1


def split_args(args_text: str) -> list[str]:
    """
    4.6.4 B1: делит текст аргументов callsite'а по запятым верхнего уровня.

    Переиспользует логику `_split_params` (балансировка скобок). Пустые
    аргументы (пропущенные позиционные, `f(a,,c)`) сохраняются как ""
    — позиция важна для сопоставления с параметрами callee.
    Чистый `f()` → `[]`.
    """
    if not args_text.strip():
        return []
    return [a.strip() for a in _split_params(args_text)]


def _parse_params(params_text: str) -> list[ParsedParameter]:
    """Разбирает содержимое скобок декларации в список ParsedParameter."""
    result: list[ParsedParameter] = []
    for i, raw in enumerate(_split_params(params_text)):
        m = RE_PARAM_SINGLE.match(raw)
        if not m:
            # Странный синтаксис — пропускаем тихо. Логи только на DEBUG.
            log.debug("Не удалось разобрать параметр: %r", raw)
            continue
        result.append(ParsedParameter(
            name=m.group("name"),
            position=i,
            is_by_value=bool(m.group("byval")),
            default_value=(m.group("default") or "").strip(),
        ))
    return result


# ─── Итераторы по preprocessed-тексту ─────────────────────────────────────


def iter_calls(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedCall]:
    """
    Итерирует callsite'ы по preprocessed-тексту тела процедуры.

    Сначала ищет все cross-module матчи и собирает их позиции; затем
    локальные — но только в позициях, не покрытых cross-module-матчем.

    Защита от false-positive на декларации (`Процедура X(`) — отбрасываем
    матч, если в 30-байтном окне слева есть ключевое слово.

    Параметр `line_offset` добавляется к локальным номерам строк — это нужно
    чтобы `.line` ParsedCall был абсолютным номером строки в файле, а не
    относительным к телу.
    """
    consumed: list[tuple[int, int]] = []  # [(start, end)] cross-module матчей

    for m in RE_CROSSMODULE_CALL.finditer(body_text_pre):
        if _is_declaration_at(body_text_pre, m.start()):
            continue
        consumed.append((m.start(), m.end()))
        # Позиция `(` — последний символ матча (регэкс кончается на `\(`).
        args_text, _ = _extract_args(body_text_pre, m.end() - 1)
        yield ParsedCall(
            module_ref=m.group("module"),
            method_name=m.group("method"),
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
            is_local=False,
            args_text=args_text,
        )

    for m in RE_LOCAL_CALL.finditer(body_text_pre):
        # Пропускаем cross-module позиции (там уже выдали матч).
        start = m.start("method")
        if any(s <= start < e for s, e in consumed):
            continue
        if _is_declaration_at(body_text_pre, start):
            continue
        # FIX-4.1: `Новый ТаблицаЗначений(...)` — имя класса, не вызов.
        if _is_constructor_at(body_text_pre, start):
            continue
        # Пропускаем ключевые слова BSL, которые внешне похожи на вызов,
        # но не являются им (Если/Тогда/Иначе/... в BSL без скобок).
        # Но `Если(...)` действительно невозможно — Если без скобок,
        # а `Возврат(…)` сейчас встретится как локальный вызов. Это
        # синтаксически совпадает с обычным методом, и тут проще оставить —
        # резолвер увидит «не нашёл» и не создаст :CALLS.
        method = m.group("method")
        if method in _BSL_KEYWORDS:
            continue
        # `m.end()` указывает сразу за `(` (регэкс кончается на `\(`).
        args_text, _ = _extract_args(body_text_pre, m.end() - 1)
        yield ParsedCall(
            module_ref="",
            method_name=method,
            line=_line_of(body_text_pre, start) + line_offset,
            col=_col_of(body_text_pre, start),
            is_local=True,
            args_text=args_text,
        )


# Ключевые слова BSL, которые не являются вызовами, но синтаксически могут
# матчиться `Имя(`. Большинство в BSL — без скобок, но `Новый()` — синтаксический
# конструктор и не «вызов» в смысле графа. Этот фильтр — на стороне парсера;
# резолвер уже знает свой `BUILTIN_FUNCS` для дополнительной фильтрации.
_BSL_KEYWORDS = {
    # Управляющие — обычно без скобок, но на всякий случай:
    "Если", "Тогда", "Иначе", "ИначеЕсли", "КонецЕсли",
    "Цикл", "КонецЦикла", "Для", "Пока",
    "Возврат", "Прервать", "Продолжить",
    "Попытка", "Исключение", "КонецПопытки", "ВызватьИсключение",
    "Перем", "Экспорт", "Знач",
    "Каждого", "Из", "По",
    "И", "Или", "Не",
    "Истина", "Ложь", "Неопределено",
    # Объявление функций/процедур (от защиты от false-positive)
    "Процедура", "Функция", "КонецПроцедуры", "КонецФункции",
}


def iter_metadata_access(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedMetaAccess]:
    """Прямые обращения к коллекциям метаданных (`Справочники.X`)."""
    for m in RE_METADATA_ACCESS.finditer(body_text_pre):
        yield ParsedMetaAccess(
            plural=m.group("plural"),
            name=m.group("name"),
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
        )


def iter_predef(body_text_raw: str, line_offset: int = 0) -> Iterator[ParsedPredef]:
    """ПредопределенноеЗначение из ОРИГИНАЛЬНОГО текста."""
    for m in RE_PREDEF.finditer(body_text_raw):
        yield ParsedPredef(
            ref=m.group("ref"),
            line=_line_of(body_text_raw, m.start()) + line_offset,
            col=_col_of(body_text_raw, m.start()),
        )


def iter_assign_refs(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedAssign]:
    """Присваивание-источник типа: `Х = Справочники.Y.Метод(...)`."""
    for m in RE_ASSIGN_REF.finditer(body_text_pre):
        yield ParsedAssign(
            var=m.group("var"),
            plural=m.group("plural"),
            name=m.group("name"),
            method=m.group("method"),
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
        )


def iter_new_assigns(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedNewAssign]:
    """4.6.4 A1: присваивания конструктора `var = Новый Класс`."""
    for m in RE_NEW_ASSIGN.finditer(body_text_pre):
        yield ParsedNewAssign(
            var=m.group("var"),
            class_name=m.group("class"),
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
        )


def iter_var_assigns(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedVarAssign]:
    """4.6.4 A2: присваивания-цепочки `var = other` / `var = other.Поле`.

    `is_plain` True только для чистого `var = other` без точек — резолвер
    наследует тип только в этом случае.
    """
    for m in RE_VAR_ASSIGN.finditer(body_text_pre):
        rhs = m.group("rhs")
        # Первый сегмент RHS (до первой точки) — потенциальный источник типа.
        src = re.split(r'\s*\.\s*', rhs, maxsplit=1)[0]
        is_plain = "." not in rhs
        # Защита: RHS не должен быть ключевым словом BSL (Истина, Ложь, …).
        if src in _BSL_KEYWORDS:
            continue
        yield ParsedVarAssign(
            var=m.group("var"),
            src_var=src,
            is_plain=is_plain,
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
        )


def iter_call_assigns(body_text_pre: str, line_offset: int = 0) -> Iterator[ParsedCallAssign]:
    """4.6.4 A2: присваивания из вызова `var = [Модуль.]Функция(...)`."""
    for m in RE_CALL_ASSIGN.finditer(body_text_pre):
        method = m.group("method")
        # Декларация процедуры/функции выглядит как `Функция Имя(` — но слева
        # от неё нет `var =`, так что RE_CALL_ASSIGN её не схватит. Доп. защита:
        # method не должен быть ключевым словом.
        if method in _BSL_KEYWORDS:
            continue
        yield ParsedCallAssign(
            var=m.group("var"),
            module_ref=m.group("module") or "",
            method=method,
            line=_line_of(body_text_pre, m.start()) + line_offset,
            col=_col_of(body_text_pre, m.start()),
        )


# ─── FIX-4: имена области видимости ───────────────────────────────────────


def _fold(names: Iterable[str]) -> set[str]:
    """FIX-4.1: нормализация имён BSL к нижнему регистру.

    BSL регистронезависим: `ПулЛогеров`, `пуллогеров` и `ПУЛЛОГЕРОВ` — одна
    и та же переменная. Все множества имён области видимости хранятся
    свёрнутыми, резолвер сворачивает `module_ref` перед проверкой.
    """
    return {n.lower() for n in names if n}


_FOLDED_KEYWORDS: frozenset[str] = frozenset(k.lower() for k in _BSL_KEYWORDS)


def _decl_names(raw: str) -> Iterator[str]:
    """Имена из списка после `Перем`: `А, Б Экспорт` → `А`, `Б`."""
    for piece in raw.split(","):
        m = _RE_FIRST_IDENT.match(piece)
        if m:
            yield m.group("name")


def collect_local_names(proc: ParsedProcedure) -> set[str]:
    """
    FIX-4: множество имён, объявленных в области видимости процедуры.

    Источники (все детерминированные, без эвристик по написанию имени):
      • параметры процедуры;
      • `Перем А, Б;` в теле;
      • LHS присваивания в начале оператора (`РезультатЗапроса = …`);
      • переменная цикла `Для Каждого Х Из …` и `Для Сч = 1 По …`.

    Используется резолвером: если `module_ref` неразрешённого вызова лежит
    в этом множестве, вызов — это обращение к методу объекта (`object_method`),
    а не пробел в графе (`unknown_module`).

    FIX-4.1: имена возвращаются в нижнем регистре. BSL регистронезависим —
    `Перем ПулЛогеров` и вызов `пуллогеров.Вставить()` это одна переменная.
    Сравнение множеств строк было регистрозависимым и такие вызовы упускало.
    Резолвер приводит `module_ref` к нижнему регистру перед проверкой.

    Работает по `body_text` (после препроцессора), поэтому присваивания
    внутри строковых литералов и комментариев сюда не попадают.
    """
    names: set[str] = {p.name for p in proc.parameters}

    body = proc.body_text
    for m in RE_VAR_DECL.finditer(body):
        names.update(_decl_names(m.group("names")))
    for m in RE_ASSIGN_LHS.finditer(body):
        names.add(m.group("var"))
    for m in RE_FOREACH_VAR.finditer(body):
        names.add(m.group("var"))
    for m in RE_FOR_VAR.finditer(body):
        names.add(m.group("var"))

    # Ключевые слова в множество имён не пускаем: они не могут быть
    # переменной, а совпасть с LHS-регуляркой теоретически способны.
    return _fold(names) - _FOLDED_KEYWORDS


def collect_module_var_names(text: str) -> set[str]:
    """
    FIX-4: `Перем` уровня модуля — объявления до первой процедуры/функции.

    Такие переменные видны во всех процедурах модуля, поэтому резолвер
    объединяет это множество с `collect_local_names` каждой процедуры.

    `text` — ОРИГИНАЛЬНЫЙ текст модуля; препроцессор применяется внутри.
    Имена возвращаются в нижнем регистре (FIX-4.1).
    """
    preprocessed = _preprocess(text)
    first_decl = RE_DECL.search(preprocessed)
    head = preprocessed[:first_decl.start()] if first_decl else preprocessed

    names: set[str] = set()
    for m in RE_VAR_DECL.finditer(head):
        names.update(_decl_names(m.group("names")))
    return _fold(names) - _FOLDED_KEYWORDS


# ─── FIX-4.1: свойства контекста модуля ──────────────────────────────────
#
# Имена, доступные в модуле без объявления — их не найти ни в `Перем`, ни в
# присваиваниях, потому что их даёт платформа. `Ссылка.ПолучитьОбъект()` в
# модуле объекта и `Параметры.Свойство()` в модуле формы выглядят как вызов
# модуля, хотя слева стоит свойство контекста.
#
# Список фиксированный и определяется ролью модуля — никаких догадок.

_CONTEXT_COMMON = {"ЭтотОбъект"}

CONTEXT_PROPERTIES_BY_ROLE: dict[str, set[str]] = {
    "Form": _CONTEXT_COMMON | {
        "ЭтаФорма", "Форма", "Элементы", "Команды", "Параметры",
        "УсловноеОформление", "РеквизитыФормы", "КомандаПараметр",
        "Объект",           # основной реквизит формы объекта
        "ВладелецФормы",    # форма-владелец при открытии из другой формы
    },
    "ObjectModule": _CONTEXT_COMMON | {
        "Ссылка", "ДополнительныеСвойства", "ОбменДанными",
        "Движения",         # модуль объекта документа
        "Отбор",            # модуль набора записей регистра
    },
    "ManagerModule": _CONTEXT_COMMON,
    "CommonModule": set(),
    # FIX-34. Роли, которые раньше в граф не попадали.
    "RecordSetModule": _CONTEXT_COMMON | {
        "Отбор", "ДополнительныеСвойства", "ОбменДанными",
    },
    "ValueManagerModule": _CONTEXT_COMMON | {
        "Значение", "ДополнительныеСвойства", "ОбменДанными",
    },
    "CommonForm": _CONTEXT_COMMON | {
        "ЭтаФорма", "Форма", "Элементы", "Команды", "Параметры",
        "УсловноеОформление", "РеквизитыФормы", "КомандаПараметр",
        "ВладелецФормы",
    },
    "Command": set(),
    "CommonCommand": set(),
    "HTTPService": set(),
    "WebService": set(),
    "ConfigurationModule": set(),
}

_CONTEXT_PROPERTIES_FOLDED: dict[str, frozenset[str]] = {
    role: frozenset(_fold(names))
    for role, names in CONTEXT_PROPERTIES_BY_ROLE.items()
}


def context_property_names(module_kind: str) -> frozenset[str]:
    """FIX-4.1: свойства контекста, доступные в модуле данной роли.

    Имена в нижнем регистре. Неизвестная роль → пустое множество.
    """
    return _CONTEXT_PROPERTIES_FOLDED.get(module_kind, frozenset())


# ─── FIX-4.1: реквизиты формы из Form.xml ────────────────────────────────


# Имя XML-атрибута с именем реквизита. В выгрузке управляемой формы это
# `<Attributes><Attribute name="ТекстЗапроса" id="2">…`.
_FORM_ATTR_NAME_KEYS = ("name", "Name")


def _xml_local_tag(elem) -> str:
    """Локальное имя тега без namespace-префикса."""
    t = elem.tag
    if not isinstance(t, str):
        return ""
    return t.split("}", 1)[1] if "}" in t else t


def collect_form_attribute_names(form_xml_path: Path) -> set[str]:
    """
    FIX-4.1: имена реквизитов управляемой формы из `Ext/Form.xml`.

    Реквизиты формы объявлены в XML, а не в BSL, поэтому
    `collect_local_names` их не видит: `ТекстЗапроса.ПолучитьТекст()`
    попадал в `unknown_module` наравне с настоящим пробелом.

    Разбор намеренно устойчив к схеме: ищем любой элемент с локальным тегом
    `Attributes` и берём имена его прямых потомков — из XML-атрибута `name`
    либо из дочернего элемента `Name`. Элементы формы (`Items`) не трогаем:
    они доступны только через `Элементы.X`, а `Элементы` уже в свойствах
    контекста.

    Файл отсутствует, не читается или не содержит секции `Attributes` →
    пустое множество, поведение как до FIX-4.1. Диагностика того, сколько
    реквизитов собралось, есть в `calibrate_inference.py`.

    Имена возвращаются в нижнем регистре.
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(str(form_xml_path)).getroot()
    except Exception as e:  # noqa: BLE001 — битый/чужой XML не должен ронять обход
        log.debug("Form.xml не разобран (%s): %s", form_xml_path, e)
        return set()

    names: set[str] = set()
    for container in root.iter():
        if _xml_local_tag(container) != "Attributes":
            continue
        for attr in container:
            name = ""
            for key in _FORM_ATTR_NAME_KEYS:
                if key in attr.attrib:
                    name = (attr.attrib[key] or "").strip()
                    break
            if not name:
                for child in attr:
                    if _xml_local_tag(child).lower() == "name":
                        name = (child.text or "").strip()
                        break
            if name:
                names.add(name)

    return _fold(names)


def form_xml_for_module(bsl_path: Path) -> Optional[Path]:
    """
    Путь к `Form.xml` рядом с модулем формы.

    Модуль лежит в `…/Forms/<Имя>/Ext/Form/Module.bsl`, описание формы —
    в `…/Forms/<Имя>/Ext/Form.xml`. Возвращает None, если файла нет или
    путь не похож на модуль формы.
    """
    parent = bsl_path.parent            # …/Ext/Form
    if parent.name != "Form":
        return None
    candidate = parent.parent / "Form.xml"   # …/Ext/Form.xml
    return candidate if candidate.is_file() else None


# ─── Парсинг файла ────────────────────────────────────────────────────────


# LOG-2. Декларации без закрывающего КонецПроцедуры/Функции за прогон:
# [(путь, строка, имя)]. Раньше предупреждение называло только имя
# процедуры — найти место в выгрузке из 17 тысяч модулей было нечем.
# Читает и сбрасывает walk_workspace_bsl (сводка в конце разбора).
UNCLOSED_DECLS: list[tuple[str, int, str]] = []


def parse_bsl_text(text: str, source: str = "") -> list[ParsedProcedure]:
    """
    Парсит текст BSL-модуля в список ParsedProcedure.
    Не привязан к файлу — нужен для unit-тестов.

    `text` — оригинал с переносами строк; preprocessor применяется внутри.
    `source` — путь модуля для диагностики (LOG-2); в разбор не влияет.
    """
    preprocessed = _preprocess(text)
    # Снимаем BOM, если он попал в начало (не влияет на счёт строк).
    # _preprocess сохраняет длину и переносы — позиции совпадают побайтово.
    procs: list[ParsedProcedure] = []

    for decl in RE_DECL.finditer(preprocessed):
        # Защита: если декларация внутри строкового литерала, она бы была
        # затёрта препроцессором. Дополнительной проверки не требуется.
        body_start = decl.end()
        end_match = RE_END.search(preprocessed, pos=body_start)
        if not end_match:
            line = preprocessed.count("\n", 0, decl.start()) + 1
            UNCLOSED_DECLS.append((source, line, decl.group("name")))
            log.warning("Декларация %r без закрывающего КонецПроцедуры/Функции — "
                        "пропуск (%s:%d)", decl.group("name"),
                        source or "<текст>", line)
            continue

        # FIX-7: сравнение в нижнем регистре. После того как RE_DECL стала
        # регистронезависимой, `процедура` со строчной буквы совпадала с
        # шаблоном, но не с этой строкой — и объявление молча становилось
        # функцией. Такая ошибка хуже пропуска: узел в графе есть, вид у
        # него неверный.
        kind = ("Procedure" if decl.group("kind").lower() == "процедура"
                else "Function")
        # Согласование: 'Процедура' → 'КонецПроцедуры', 'Функция' → 'КонецФункции'.
        # При несовпадении (например, `Функция X() … КонецПроцедуры`) — это
        # синтаксически неправильный BSL, но парсер не падает: берёт ближайший
        # `КонецX`, и резолвер потом отметит. На реальных Котировках такого не
        # встретилось.
        body_end = end_match.start()

        procs.append(ParsedProcedure(
            name=decl.group("name"),
            kind=kind,
            is_export=bool(decl.group("export")),
            directive=decl.group("dir") or "",
            parameters=_parse_params(decl.group("params") or ""),
            body_text=preprocessed[body_start:body_end],
            body_text_raw=text[body_start:body_end],
            line_start=_line_of(preprocessed, decl.start()),
            line_end=_line_of(preprocessed, end_match.start()),
        ))

    return procs


def parse_bsl_module(
    path: Path,
    module_id: str,
    module_kind: str,
    parent_metadata_id: Optional[str],
    source_path: str,
    is_server: bool,
    is_client: bool,
) -> ParsedModule:
    """Парсит один .bsl-файл в ParsedModule."""
    # 1С пишет BSL в UTF-8 с BOM. Снимаем BOM.
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    procs = parse_bsl_text(raw, source=source_path)

    # FIX-4.1: свойства контекста по роли модуля + реквизиты формы из
    # соседнего Form.xml. Для не-форм второе слагаемое пусто.
    context = set(context_property_names(module_kind))
    form_xml = form_xml_for_module(path)
    if form_xml is not None:
        context |= collect_form_attribute_names(form_xml)

    return ParsedModule(
        module_id=module_id,
        module_kind=module_kind,
        parent_metadata_id=parent_metadata_id,
        source_path=source_path,
        is_server=is_server,
        is_client=is_client,
        procedures=procs,
        module_vars=collect_module_var_names(raw),   # FIX-4
        context_names=context,                       # FIX-4.1
    )


def report_unclosed(limit: int = 10) -> None:
    """LOG-2: одна сводка вместо россыпи — сколько и где, первые `limit` мест."""
    if not UNCLOSED_DECLS:
        return
    modules = {src for src, _line, _name in UNCLOSED_DECLS}
    log.warning("Деклараций без КонецПроцедуры/Функции: %d в %d модулях "
                "(пропущены, в граф не попали). Первые места:",
                len(UNCLOSED_DECLS), len(modules))
    for src, line, name in UNCLOSED_DECLS[:limit]:
        log.warning("    %s:%d  %s", src or "<текст>", line, name)
    if len(UNCLOSED_DECLS) > limit:
        log.warning("    … ещё %d", len(UNCLOSED_DECLS) - limit)


# ─── Сопоставление путей → module_id/module_kind ─────────────────────────


# Соответствие папки в выгрузке английскому KindEng (4.6.1 уже зашит этот
# маппинг в metadata_xml.py; здесь дублируем нужное подмножество чтобы
# модуль был самодостаточным). Сторож совпадения с metadata_xml.KINDS —
# tests_bsl_parser.TestDirTableMatchesKinds.
DIR_TO_KIND_ENG = {
    "Catalogs":                 "Catalog",
    "Documents":                "Document",
    "Enums":                    "Enum",
    "DataProcessors":           "DataProcessor",
    "Reports":                  "Report",
    "InformationRegisters":     "InformationRegister",
    "AccumulationRegisters":    "AccumulationRegister",
    "AccountingRegisters":      "AccountingRegister",
    "CalculationRegisters":     "CalculationRegister",
    "Constants":                "Constant",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "ChartsOfAccounts":         "ChartOfAccounts",
    "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
    "ExchangePlans":            "ExchangePlan",
    "BusinessProcesses":        "BusinessProcess",
    "Tasks":                    "Task",
    "DocumentJournals":         "DocumentJournal",
    "CommonModules":            "CommonModule",
    # FIX-34: виды, чьи модули раньше шли мимо графа (175 Module.bsl плюс
    # формы и менеджеры этих объектов). Объекты в слое 1 у них есть.
    "CommonForms":              "CommonForm",
    "CommonCommands":           "CommonCommand",
    "HTTPServices":             "HTTPService",
    "WebServices":              "WebService",
    "SettingsStorages":         "SettingsStorage",
    "FilterCriteria":           "FilterCriterion",
    "Sequences":                "Sequence",
}

# Имя файла .bsl в `<Вид>/<Объект>/Ext/` → роль модуля.
FILENAME_TO_ROLE = {
    "ObjectModule.bsl":  "ObjectModule",
    "ManagerModule.bsl": "ManagerModule",
    # FIX-34: набор записей регистра и последовательности (886 модулей на
    # боевой конфигурации) и менеджер значения константы.
    "RecordSetModule.bsl":    "RecordSetModule",
    "ValueManagerModule.bsl": "ValueManagerModule",
    # `Forms/<Y>/Ext/Form/Module.bsl` и `Commands/<Y>/Ext/CommandModule.bsl` —
    # специальные кейсы, обрабатываются ниже.
}

# FIX-34. Модули, чей id СОВПАДАЕТ с id объекта слоя 1: у объекта один
# модуль, и он сам и есть «модуль». Как у общего модуля: отдельный узел
# :Module не создаётся (метка :Module спрятала бы объект из metadata_search,
# там фильтр `NOT n:Module`), процедуры висят на :MetadataObject объекта.
#   каталог вида → (путь модуля внутри объекта, вид модуля)
SAME_ID_MODULES: dict[str, tuple[tuple[str, ...], str]] = {
    "CommonModules":  (("Ext", "Module.bsl"),         "CommonModule"),
    "CommonForms":    (("Ext", "Form", "Module.bsl"), "CommonForm"),
    "CommonCommands": (("Ext", "CommandModule.bsl"),  "CommonCommand"),
    "HTTPServices":   (("Ext", "Module.bsl"),         "HTTPService"),
    "WebServices":    (("Ext", "Module.bsl"),         "WebService"),
}
SAME_ID_KINDS = frozenset(kind for _path, kind in SAME_ID_MODULES.values())

# FIX-34. Модули конфигурации — `Ext/<Имя>.bsl` в корне выгрузки. Владелец —
# конфигурация; узел :Module с id `Configuration.<Роль>`.
CONFIGURATION_MODULES = frozenset({
    "SessionModule", "ManagedApplicationModule",
    "OrdinaryApplicationModule", "ExternalConnectionModule",
})

# FIX-34: где выполняется модуль, если директивы у процедуры нет.
#   (is_server, is_client); (False, False) — смотреть директиву процедуры.
_SERVER = (True, False)
_CLIENT = (False, True)
_BY_DIRECTIVE = (False, False)
_CONTEXT_BY_ROLE: dict[str, tuple[bool, bool]] = {
    "ObjectModule": _SERVER, "ManagerModule": _SERVER,
    "RecordSetModule": _SERVER, "ValueManagerModule": _SERVER,
    "HTTPService": _SERVER, "WebService": _SERVER,
    "SessionModule": _SERVER, "ExternalConnectionModule": _SERVER,
    "ManagedApplicationModule": _CLIENT, "OrdinaryApplicationModule": _CLIENT,
    "Form": _BY_DIRECTIVE, "CommonForm": _BY_DIRECTIVE,
    "Command": _BY_DIRECTIVE, "CommonCommand": _BY_DIRECTIVE,
}


def module_context(module_kind: str, role: Optional[str],
                   common_module_info: Optional[dict] = None) -> tuple[bool, bool]:
    """
    `(is_server, is_client)` модуля. Одна функция для полной индексации
    (walk_workspace_bsl) и точечного обновления (incremental) — раньше у
    каждого была своя копия ветвлений, и новый вид модуля пришлось бы
    вписывать дважды.

    Общий модуль — по свойствам из слоя 1 (`Server`, `Client…`).
    """
    if module_kind == "CommonModule":
        info = common_module_info or {}
        return (bool(info.get("is_server", True)), bool(info.get("is_client", False)))
    return _CONTEXT_BY_ROLE.get(role or module_kind,
                                _CONTEXT_BY_ROLE.get(module_kind, _BY_DIRECTIVE))


def is_module_owner(owner: str) -> bool:
    """
    Владелец частичного обновления — модуль (а не объект метаданных)?

    Для модулей с тем же id, что у объекта (общий модуль, общая форма…),
    ответ «нет»: их владелец — объект, и обновляется он вместе с XML.
    Используют partial_fingerprint и apply_changes (PERF-8): раньше там
    стоял свой список суффиксов, и модуль команды был бы принят за объект —
    с удалением не того среза.
    """
    if owner.startswith("Configuration."):
        return True
    parts = owner.split(".")
    if len(parts) >= 3 and parts[-1] in FILENAME_TO_ROLE.values():
        return True
    return len(parts) >= 4 and parts[-2] in ("Form", "Command")


def classify_bsl_path(rel_path_posix: str) -> Optional[tuple[str, str, Optional[str], Optional[str]]]:
    """
    По относительному POSIX-пути BSL-файла определяет
    `(module_id, module_kind, parent_metadata_id, role)` или None.

    Примеры:
      CommonModules/АукОбщийКлиент/Ext/Module.bsl
        → ("CommonModule.АукОбщийКлиент", "CommonModule", None, "CommonModule")
      Catalogs/АукАукционы/Ext/ObjectModule.bsl
        → ("Catalog.АукАукционы.ObjectModule", "ObjectModule", "Catalog.АукАукционы", "ObjectModule")
      Catalogs/АукАукционы/Ext/ManagerModule.bsl
        → ("Catalog.АукАукционы.ManagerModule", "ManagerModule", "Catalog.АукАукционы", "ManagerModule")
      Catalogs/АукАукционы/Forms/ФормаЭлемента/Ext/Form/Module.bsl
        → ("Catalog.АукАукционы.Form.ФормаЭлемента", "Form", "Catalog.АукАукционы", "Form")
      tests-extension/CommonModules/Тест_X/Ext/Module.bsl
        → ("CommonModule.Тест_X", "CommonModule", None, "CommonModule")

    FIX-34:
      AccumulationRegisters/Остатки/Ext/RecordSetModule.bsl
        → ("AccumulationRegister.Остатки.RecordSetModule", "RecordSetModule", …)
      Catalogs/Номенклатура/Commands/ПечатьЭтикеток/Ext/CommandModule.bsl
        → ("Catalog.Номенклатура.Command.ПечатьЭтикеток", "Command",
           "Catalog.Номенклатура", "Command")
      CommonForms/Выбор/Ext/Form/Module.bsl
        → ("CommonForm.Выбор", "CommonForm", None, "CommonForm")
      Ext/SessionModule.bsl
        → ("Configuration.SessionModule", "ConfigurationModule", None, "SessionModule")
    """
    parts = rel_path_posix.split("/")

    # Сначала отрезаем префикс tests-extension/ если он есть.
    extension = bool(parts) and parts[0] == "tests-extension"
    if extension:
        parts = parts[1:]

    # Модули конфигурации: Ext/<Роль>.bsl. У расширения свои модули
    # конфигурации, и их id совпал бы с основными — не берём.
    if len(parts) == 2 and parts[0] == "Ext" and parts[1].endswith(".bsl"):
        role = parts[1][:-4]
        if role in CONFIGURATION_MODULES and not extension:
            return (f"Configuration.{role}", "ConfigurationModule", None, role)
        return None

    if len(parts) < 4:
        return None

    top = parts[0]
    if top not in DIR_TO_KIND_ENG:
        return None
    kind_eng = DIR_TO_KIND_ENG[top]

    obj_name = parts[1]

    # Модуль с тем же id, что у объекта: CommonModules/X/Ext/Module.bsl и т.п.
    if top in SAME_ID_MODULES:
        tail, module_kind = SAME_ID_MODULES[top]
        if tuple(parts[2:]) == tail:
            module_id = f"{kind_eng}.{obj_name}"
            return (module_id, module_kind, None, module_kind)
        return None

    parent_id = f"{kind_eng}.{obj_name}"

    # X/Y/Ext/ObjectModule.bsl, ManagerModule.bsl, RecordSetModule.bsl, …
    if len(parts) == 4 and parts[2] == "Ext" and parts[3] in FILENAME_TO_ROLE:
        role = FILENAME_TO_ROLE[parts[3]]
        module_id = f"{parent_id}.{role}"
        return (module_id, role, parent_id, role)

    # X/Y/Forms/Z/Ext/Form/Module.bsl
    if (
        len(parts) == 7
        and parts[2] == "Forms"
        and parts[4] == "Ext"
        and parts[5] == "Form"
        and parts[6] == "Module.bsl"
    ):
        form_name = parts[3]
        module_id = f"{parent_id}.Form.{form_name}"
        return (module_id, "Form", parent_id, "Form")

    # FIX-34, часть Б: X/Y/Commands/Z/Ext/CommandModule.bsl. Имя команды —
    # в id модуля: у всех команд процедура `ОбработкаКоманды`, и без имени
    # команды их процедуры слились бы в одну (решение владельца, PLAN-12).
    if (
        len(parts) == 6
        and parts[2] == "Commands"
        and parts[4] == "Ext"
        and parts[5] == "CommandModule.bsl"
    ):
        module_id = f"{parent_id}.Command.{parts[3]}"
        return (module_id, "Command", parent_id, "Command")

    return None


# ─── Обход workspace ──────────────────────────────────────────────────────


# ─── PERF-15: параллельность ──────────────────────────────────────────────
#
# Процессы, а не потоки: разбор и резолв — чистый Python, и потоки упёрлись
# бы в GIL. Процессы стартуют через fork (только Linux, то есть контейнер):
# дочерний получает модули и индекс без копирования (copy-on-write). На
# Windows-хосте (тесты разработчика) fork нет — работаем последовательно.

PARALLEL_MIN_ITEMS = 2000      # меньше — накладные расходы дороже выигрыша


def resolve_workers(requested: Optional[int] = None) -> int:
    """
    Сколько процессов использовать. `requested` None — из METADATA_WORKERS:
    число или `auto` (= min(4, ядра − 1)). 1 — прежний последовательный путь.
    Без fork (Windows) — всегда 1.
    """
    import multiprocessing as mp
    import os
    if "fork" not in mp.get_all_start_methods():
        return 1
    if requested is None:
        raw = (os.environ.get("METADATA_WORKERS", "auto") or "auto").strip().lower()
        if raw == "auto":
            requested = min(4, max(1, (os.cpu_count() or 2) - 1))
        else:
            try:
                requested = int(raw)
            except ValueError:
                log.warning("METADATA_WORKERS=%r — не число; работаю в 1 процесс", raw)
                requested = 1
    return max(1, int(requested))


class frozen_for_fork:
    """
    `gc.freeze()` перед fork: объекты родителя уходят в «вечное» поколение,
    и сборщик мусора в дочерних процессах их не трогает — иначе он
    переписывал бы их заголовки, и copy-on-write копировал бы страницы
    памяти в каждый процесс. Если заморожено раньше (METADATA_GC_FREEZE) —
    не трогаем; иначе размораживаем на выходе.
    """

    def __enter__(self):
        import gc
        self._ours = gc.get_freeze_count() == 0
        if self._ours:
            gc.freeze()
        return self

    def __exit__(self, *exc):
        import gc
        if self._ours:
            gc.unfreeze()
        return False


def balanced_slices(sizes: list[int], n_chunks: int) -> list[tuple[int, int, int]]:
    """
    Разрезать последовательность на `n_chunks` подряд идущих кусков примерно
    равного веса: `[(начало, конец, вес)]`. Порядок сохраняется — на нём
    держится совпадение результата с последовательным проходом.
    """
    total = sum(sizes)
    if not sizes:
        return []
    n_chunks = max(1, min(n_chunks, len(sizes)))
    target = total / n_chunks if total else 1
    out: list[tuple[int, int, int]] = []
    lo = acc = 0
    for i, w in enumerate(sizes):
        acc += w
        if acc >= target and len(out) < n_chunks - 1:
            out.append((lo, i + 1, acc))
            lo, acc = i + 1, 0
    if lo < len(sizes):
        out.append((lo, len(sizes), acc))
    return out


_PARSE_STATE: dict = {}


def _parse_chunk(bounds: tuple[int, int]) -> list:
    """Дочерний процесс: разобрать файлы [lo, hi) списка. Результат по файлу:
    (rel, 'ok'|'skip'|'missing'|'error', модуль|причина), и места
    деклараций без конца этого куска."""
    st = _PARSE_STATE
    lo, hi = bounds
    UNCLOSED_DECLS.clear()
    out = [_parse_one(st["root"], rel, st["modules_info"])
           for rel in st["rel_paths"][lo:hi]]
    return [out, list(UNCLOSED_DECLS)]


def _parse_one(root: Path, rel: str, modules_info: dict) -> tuple:
    classified = classify_bsl_path(rel)
    if not classified:
        return (rel, "skip", "вне схемы путей")
    module_id, module_kind, parent_metadata_id, role = classified
    is_server, is_client = module_context(module_kind, role, modules_info.get(module_id))
    try:
        return (rel, "ok", parse_bsl_module(
            path=root / rel, module_id=module_id, module_kind=module_kind,
            parent_metadata_id=parent_metadata_id, source_path=rel,
            is_server=is_server, is_client=is_client))
    except FileNotFoundError:
        return (rel, "missing", "нет на диске")
    except Exception as e:  # noqa: BLE001 — один файл не роняет разбор
        return (rel, "error", f"{type(e).__name__}: {e}")


def list_bsl_files(root: Path, progress_every_sec: float = 20.0) -> list[str]:
    """
    Все `*.bsl` под `root` — пути от корня, POSIX, отсортированы.

    PERF-14: запасной путь. Список берётся из git (`ls-files`) или из обхода
    для отпечатков; сюда приходят, только когда ни того ни другого нет. Но и
    тогда — с прогрессом: прежний `sorted(root.rglob("*.bsl"))` на боевой
    выгрузке молчал 16 минут, и снаружи зависший процесс не отличался от
    работающего.
    """
    import os
    import time as _time
    out: list[str] = []
    t0 = last = _time.monotonic()
    dirs = 0
    stack = [(str(root), "")]
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        dirs += 1
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, f"{prefix}{e.name}/"))
                elif e.name.lower().endswith(".bsl"):
                    out.append(f"{prefix}{e.name}")
            except OSError:
                continue
        now = _time.monotonic()
        if now - last >= progress_every_sec:
            last = now
            log.info("  [поиск *.bsl] найдено %d, каталогов %d, прошло %.0f с",
                     len(out), dirs, now - t0)
    out.sort()
    log.info("  поиск *.bsl обходом каталога: %d файлов за %.1f с",
             len(out), _time.monotonic() - t0)
    return out


def walk_workspace_bsl(
    root: Path,
    modules_info: Optional[dict[str, dict]] = None,
    tally: Optional[Tally] = None,
    paths: Optional[Iterable[str]] = None,
    workers: Optional[int] = None,
) -> list[ParsedModule]:
    """
    Обходит все *.bsl в `root`, парсит, возвращает список ParsedModule.

    `modules_info` — словарь `module_id` → `{is_server: bool, is_client: bool}`,
    полученный из Neo4j-индекса свойств :CommonModule. Если отсутствует —
    server/client определяется по умолчанию (ObjectModule/ManagerModule = server,
    Form = по директиве позже, при анализе процедур; в самом ParsedModule
    оставляем False/False — конкретность не нужна для парсера, нужна для
    резолвера).

    `paths` — PERF-14: готовый список файлов (пути от `root`), из git или из
    обхода для отпечатков. Не передан — список строится обходом
    (`list_bsl_files`, с прогрессом). Файл из списка, которого нет на диске
    (удалён, но ещё в индексе git), отсеивается с причиной «нет на диске».
    """
    modules_info = modules_info or {}
    modules: list[ParsedModule] = []
    UNCLOSED_DECLS.clear()

    if paths is None:
        rel_paths = list_bsl_files(root)
    else:
        rel_paths = sorted({p.replace("\\", "/") for p in paths
                            if p.lower().endswith(".bsl")})

    # PERF-5: на боевой конфигурации это 14 тысяч модулей и 6,6 минуты
    # без единой строки в логе. Список файлов и так материализуется
    # сортировкой, поэтому общее число известно заранее — можно показывать
    # проценты и ETA, а не голый счётчик.
    prog = ProgressLogger(log, "разбор BSL", total=len(rel_paths),
                          every_sec=20.0, unit="файл") if len(rel_paths) >= 500 else None

    # A-1: число файлов известно здесь (оно нужно прогресс-бару), а наружу
    # уходило только число модулей. Файл, не подошедший под известную схему
    # путей, уезжал в log.debug — при штатном INFO это молчание. Смена
    # раскладки каталогов обрушила бы классификацию тысяч модулей, а лог
    # сказал бы «✓ модулей: 8000» и ни слова о том, что файлов было 14 000.
    own_tally = tally is None
    t = tally or Tally("разбор BSL", unit="файл", log=log,
                       # Ниже девяти десятых на реальной выгрузке не бывает:
                       # схема путей покрывает все штатные размещения модулей.
                       min_keep_ratio=0.9)
    t.see(len(rel_paths))

    def account(result: tuple) -> None:
        rel, status, payload = result
        if status == "ok":
            modules.append(payload)
            t.keep()
        elif status == "skip":
            log.debug("BSL вне известной схемы путей — пропуск: %s", rel)
            t.drop("вне схемы путей", example=rel)
        elif status == "missing":
            # Список из git может отстать от диска (файл удалён, а в индексе
            # ещё есть). Отдельный stat на каждый файл стоил бы ~1,5 минуты
            # через границу Docker — ловим при чтении.
            t.drop("нет на диске", example=rel)
        else:
            log.warning("Ошибка парсинга %s: %s", rel, payload)
            t.drop("ошибка разбора", example=rel, alarm=True)

    n_workers = resolve_workers(workers)
    if n_workers > 1 and len(rel_paths) >= PARALLEL_MIN_ITEMS:
        # PERF-15: файлы независимы — режем список на куски, порядок
        # сохраняем (imap), результат тот же, что последовательно.
        import multiprocessing as mp
        slices = balanced_slices([1] * len(rel_paths), n_workers * 16)
        _PARSE_STATE.update(root=root, rel_paths=rel_paths, modules_info=modules_info)
        log.info("  разбор BSL: %d процессов, кусков %d", n_workers, len(slices))
        try:
            with frozen_for_fork(), mp.get_context("fork").Pool(n_workers) as pool:
                for (out, unclosed), (_lo, _hi, n) in zip(
                        pool.imap(_parse_chunk, [(a, b) for a, b, _w in slices]),
                        slices):
                    for result in out:
                        account(result)
                    UNCLOSED_DECLS.extend(unclosed)
                    if prog:
                        prog.step(n)
        finally:
            _PARSE_STATE.clear()
    else:
        for rel in rel_paths:
            if prog:
                prog.step()
            account(_parse_one(root, rel, modules_info))

    if prog:
        prog.done(extra=f"модулей в разборе {len(modules)}")
    report_unclosed()
    if own_tally:
        t.report(hint="схема путей — classify_bsl_path(); если выгрузка "
                      "сменила раскладку каталогов, править надо там")
    return modules
