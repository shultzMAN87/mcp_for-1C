"""
Сверка запроса 1С с метаданными конфигурации (FEAT-1 / FEAT-2).
================================================================

Работает поверх `query_parser` и провайдера метаданных. Провайдер —
интерфейс, а не Neo4j: в проде его реализует `server.py` поверх графа, в
тестах — словарь. Причина та же, что у `graph_state.py`: всё, что должно
быть покрыто тестами, не должно тянуть за собой ни FastMCP, ни базу.

Принцип разбора ошибок
----------------------
Ошибка ставится только там, где утверждение доказуемо: объект есть в графе,
состав его полей известен, поля с таким именем среди них нет. Везде, где
состав полей неизвестен (подзапрос с `*`, виртуальная таблица регистра
бухгалтерии, составной тип реквизита), проверка молчит. Инструмент, который
врёт на корректном запросе, хуже инструмента, который молчит на битом — это
тот же вывод, что дали FIX-2 и QOPT-1.

Точка входа: `check_query(text, provider) -> dict`.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from query_parser import TABLE_PREFIXES, Source, parse_batch


# ─── Модель метаданных, которую ждёт проверка ────────────────────────────

@dataclass
class AttrInfo:
    name: str
    role: str = "attribute"                     # attribute | dimension | resource
    types: list[tuple[str, str | None]] = field(default_factory=list)  # [(kind, target)]
    synonym: str = ""

    @property
    def ref_target(self) -> str | None:
        """Единственный ссылочный тип реквизита, если он один.

        Составной тип не разворачиваем: какое из нескольких определений
        имел в виду автор запроса, из текста не следует.
        """
        refs = [t for k, t in self.types if k.endswith("Ref") and t]
        return refs[0] if len(refs) == 1 else None


@dataclass
class ObjectInfo:
    id: str                                     # Catalog.АукАукционы
    name: str
    kind_ru: str                                # Справочник
    attributes: list[AttrInfo] = field(default_factory=list)
    tabular_sections: dict[str, list[AttrInfo]] = field(default_factory=dict)
    synonym: str = ""
    properties: dict = field(default_factory=dict)
    """FEAT-1.1: структурные признаки из properties_json (Hierarchical и др.).

    Пустой словарь — признаки неизвестны. Это валидное состояние: граф мог
    быть построен индексатором до FEAT-1.1. Проверка тогда работает как
    раньше, то есть разрешает всё.
    """
    has_owner: bool = False
    """FEAT-1.1: есть ребро OWNED_BY, то есть справочник подчинён владельцу."""


class MetadataProvider:
    """Интерфейс. Реализации: Neo4jProvider в server.py, фейк в тестах."""

    def get(self, kind_ru: str, name: str) -> ObjectInfo | None:
        raise NotImplementedError

    def get_by_id(self, full_name_eng: str) -> ObjectInfo | None:
        raise NotImplementedError

    def suggest(self, name: str, limit: int = 3) -> list[str]:
        return []


# ─── Стандартные реквизиты ───────────────────────────────────────────────
#
# Список намеренно избыточен: сюда включены поля, которые есть не у всякого
# объекта вида (Родитель — только у иерархического справочника, Владелец —
# только у подчинённого). Признаки лежат в properties объекта, но брать их
# ради того, чтобы начать ругаться на `Т.Родитель`, — плохой размен: цена
# ложной ошибки выше цены пропущенной.

STANDARD_FIELDS = {
    "Справочник": ["Ссылка", "Код", "Наименование", "Родитель", "Владелец",
                   "ЭтоГруппа", "ПометкаУдаления", "Предопределенный",
                   "ПредопределенноеИмяЗначения", "Представление"],
    "Документ": ["Ссылка", "Номер", "Дата", "Проведен", "ПометкаУдаления",
                 "Представление"],
    "Перечисление": ["Ссылка", "Порядок", "Представление"],
    "ПланВидовХарактеристик": ["Ссылка", "Код", "Наименование", "ТипЗначения",
                               "Родитель", "ЭтоГруппа", "ПометкаУдаления",
                               "Предопределенный", "ПредопределенноеИмяЗначения",
                               "Представление"],
    "ПланСчетов": ["Ссылка", "Код", "Наименование", "Вид", "Забалансовый",
                   "Порядок", "ПометкаУдаления", "Предопределенный",
                   "Представление"],
    "ПланВидовРасчета": ["Ссылка", "Код", "Наименование", "ПометкаУдаления",
                         "Предопределенный", "Представление"],
    "ПланОбмена": ["Ссылка", "Код", "Наименование", "ПометкаУдаления",
                   "ЭтотУзел", "НомерОтправленного", "НомерПринятого",
                   "Представление"],
    "БизнесПроцесс": ["Ссылка", "Номер", "Дата", "Стартован", "Завершен",
                      "ПометкаУдаления", "Представление"],
    "Задача": ["Ссылка", "Номер", "Дата", "Наименование", "Выполнена",
               "ПометкаУдаления", "Представление"],
    "РегистрСведений": ["Период", "Регистратор", "Активность", "НомерСтроки"],
    "РегистрНакопления": ["Период", "Регистратор", "Активность", "НомерСтроки",
                          "ВидДвижения"],
    "РегистрБухгалтерии": ["Период", "Регистратор", "Активность", "НомерСтроки",
                           "Счет", "Организация", "СуммаДт", "СуммаКт"],
    "РегистрРасчета": ["Период", "Регистратор", "Активность", "НомерСтроки",
                       "ВидРасчета", "ПериодДействияНачало", "ПериодДействияКонец",
                       "ПериодРегистрации"],
    "ЖурналДокументов": ["Ссылка", "Номер", "Дата", "Тип", "Представление"],
    "Константа": ["Значение"],
}

# Поля таблицы табличной части.
TS_STANDARD_FIELDS = ["Ссылка", "НомерСтроки"]

# FEAT-1.1: поля, которые есть не у всякого объекта своего вида, и признак,
# от которого зависит их наличие.
_CONDITIONAL_FIELDS = {
    "родитель":  "hierarchical",
    "этогруппа": "hierarchical",
    "владелец":  "owner",
}


def standard_fields_for(obj: ObjectInfo) -> list[str]:
    """Стандартные реквизиты объекта с учётом его структуры.

    До FEAT-1.1 список подставлялся по виду объекта целиком, и `Т.Родитель`
    у плоского справочника проходил проверку молча — то есть проверка
    пропускала ровно ту ошибку, ради которой существует. Признак
    `Hierarchical` лежит в графе с Захода 3, подчинённость видна по ребру
    OWNED_BY.

    Разрешительный по умолчанию: признак не задан → поле остаётся. Пустой
    `properties` бывает у графа, построенного старым индексатором, и
    начинать в этом случае ругаться на корректные запросы нельзя.
    """
    base = STANDARD_FIELDS.get(obj.kind_ru, [])
    hierarchical = obj.properties.get("Hierarchical")
    out = []
    for name in base:
        need = _CONDITIONAL_FIELDS.get(name.lower())
        if need == "hierarchical" and hierarchical == "false":
            continue
        if need == "owner" and obj.properties and not obj.has_owner:
            continue
        out.append(name)
    return out


# ─── Виртуальные таблицы ─────────────────────────────────────────────────
#
# Значение None означает «имя допустимо, состав полей не выводим». Для
# регистра бухгалтерии и регистра расчёта состав зависит от плана счетов,
# видов субконто и графика — вывести его из выгрузки нельзя, а угадывать
# нельзя тем более.

VIRTUAL_TABLES = {
    "РегистрНакопления": {"Остатки", "Обороты", "ОстаткиИОбороты"},
    "РегистрСведений": {"СрезПоследних", "СрезПервых"},
    "РегистрБухгалтерии": {"Остатки", "Обороты", "ОстаткиИОбороты",
                           "ДвиженияССубконто", "ОборотыДтКт",
                           "ОстаткиИОборотыДтКт", "СубконтоСубконто"},
    "РегистрРасчета": {"ДанныеГрафика", "БазаТекущийРегистр",
                       "ФактическийПериодДействия",
                       "ПериодДействияПоФактическомуПериодуДействия"},
    "ПланВидовРасчета": {"ВедущиеВидыРасчета", "ВытесняющиеВидыРасчета",
                         "ПоследователиВидыРасчета", "БазовыеВидыРасчета"},
    "Справочник": set(),
    "Документ": set(),
}


def _vt_fields(obj: ObjectInfo, vt: str) -> list[AttrInfo] | None:
    """Состав полей виртуальной таблицы или None, если не выводится."""
    dims = [a for a in obj.attributes if a.role == "dimension"]
    res = [a for a in obj.attributes if a.role == "resource"]
    props = [a for a in obj.attributes if a.role == "attribute"]

    def plain(names):
        return [AttrInfo(n) for n in names]

    if obj.kind_ru == "РегистрСведений":
        if vt in ("СрезПоследних", "СрезПервых"):
            return plain(["Период"]) + dims + res + props
        return None
    if obj.kind_ru == "РегистрНакопления":
        if vt == "Остатки":
            out = list(dims)
            for r in res:
                out.append(AttrInfo(f"{r.name}Остаток", types=r.types))
            return out
        if vt == "Обороты":
            out = plain(["Период", "Регистратор"]) + list(dims)
            for r in res:
                out.append(AttrInfo(f"{r.name}Оборот", types=r.types))
                out.append(AttrInfo(f"{r.name}Приход", types=r.types))
                out.append(AttrInfo(f"{r.name}Расход", types=r.types))
            return out
        if vt == "ОстаткиИОбороты":
            out = plain(["Период", "Регистратор"]) + list(dims)
            for r in res:
                for suffix in ("НачальныйОстаток", "КонечныйОстаток",
                               "Приход", "Расход", "Оборот"):
                    out.append(AttrInfo(f"{r.name}{suffix}", types=r.types))
            return out
    return None


# ─── Разрешение источника ────────────────────────────────────────────────

@dataclass
class ResolvedSource:
    src: Source
    label: str                                  # как показывать в сообщениях
    obj: ObjectInfo | None = None
    fields: dict[str, AttrInfo] | None = None   # None — состав неизвестен
    error: str = ""

    def find(self, name: str) -> AttrInfo | None:
        return self.fields.get(name.lower()) if self.fields else None


def _index(attrs: list[AttrInfo], extra: list[str] = ()) -> dict[str, AttrInfo]:
    idx = {a.name.lower(): a for a in attrs}
    for n in extra:
        idx.setdefault(n.lower(), AttrInfo(n))
    return idx


def resolve_source(src: Source, provider: MetadataProvider,
                   temp_tables: dict[str, list[str] | None]) -> ResolvedSource:
    """Источник из секции ИЗ → объект метаданных и состав его полей."""
    if src.kind == "subquery":
        cols, known = (src.subquery.columns() if src.subquery else ([], False))
        rs = ResolvedSource(src, "подзапрос")
        rs.fields = _index([AttrInfo(c) for c in cols]) if known else None
        return rs

    parts = src.parts
    if len(parts) == 1:
        name = parts[0]
        key = name.lower()
        if key not in temp_tables:
            return ResolvedSource(
                src, name,
                error=f"Временная таблица '{name}' не объявлена выше в пакете "
                      f"(нет запроса с ПОМЕСТИТЬ {name}).")
        cols = temp_tables[key]
        rs = ResolvedSource(src, f"временная таблица {name}")
        rs.fields = _index([AttrInfo(c) for c in cols]) if cols is not None else None
        return rs

    prefix = TABLE_PREFIXES.get(parts[0].upper())
    if not prefix:
        return ResolvedSource(src, src.text,
                              error=f"'{parts[0]}' — не вид объекта метаданных "
                                    f"и не временная таблица.")

    obj = provider.get(prefix, parts[1])
    if obj is None:
        hint = ""
        similar = provider.suggest(parts[1])
        if similar:
            hint = f" Возможно: {', '.join(similar)}."
        return ResolvedSource(src, src.text,
                              error=f"Таблица '{parts[0]}.{parts[1]}' не найдена "
                                    f"в метаданных конфигурации.{hint}")

    if len(parts) == 2:
        rs = ResolvedSource(src, src.text, obj=obj)
        # Имя табличной части — тоже поле объекта: `Т.Товары` в запросе даёт
        # вложенную таблицу. Спускаться по нему на второй уровень нельзя,
        # и мы не спускаемся — типа у такого поля нет.
        rs.fields = _index(obj.attributes,
                           standard_fields_for(obj)
                           + list(obj.tabular_sections))
        return rs

    third = parts[2]
    ts_key = {k.lower(): k for k in obj.tabular_sections}
    if third.lower() in ts_key:
        attrs = obj.tabular_sections[ts_key[third.lower()]]
        rs = ResolvedSource(src, src.text, obj=obj)
        rs.fields = _index(attrs, TS_STANDARD_FIELDS)
        return rs

    allowed = VIRTUAL_TABLES.get(obj.kind_ru, set())
    match = {v.lower(): v for v in allowed}.get(third.lower())
    if match:
        rs = ResolvedSource(src, src.text, obj=obj)
        vt = _vt_fields(obj, match)
        rs.fields = _index(vt) if vt is not None else None
        return rs

    variants = sorted(allowed) + sorted(obj.tabular_sections)
    hint = f" Допустимо: {', '.join(variants)}." if variants else ""
    return ResolvedSource(
        src, src.text,
        error=f"'{third}' — не виртуальная таблица и не табличная часть "
              f"объекта {obj.kind_ru}.{obj.name}.{hint}")


# ─── Проверка ────────────────────────────────────────────────────────────

def _suggest_field(name: str, fields: dict[str, AttrInfo]) -> str:
    close = difflib.get_close_matches(name.lower(), list(fields), n=2, cutoff=0.7)
    if not close:
        return ""
    return " Возможно: " + ", ".join(fields[c].name for c in close) + "."


def _check_part(part, provider, temp_tables, errors, warnings, info, counters):
    resolved: list[ResolvedSource] = []
    by_alias: dict[str, ResolvedSource] = {}

    for src in part.sources:
        rs = resolve_source(src, provider, temp_tables)
        resolved.append(rs)
        counters["tables"] += 1
        if rs.error:
            errors.append(f"Строка {src.line}: {rs.error}")
        else:
            info.append(f"✓ {rs.label}" + (f" КАК {src.alias}" if src.alias else ""))
        # Псевдоним регистрируется и у неразрешённого источника: состав полей
        # у него неизвестен (fields is None), проверка полей молчит. Иначе на
        # одну опечатку в имени таблицы сыпался каскад «псевдоним не объявлен»,
        # и настоящая ошибка тонула в следствиях.
        for key in filter(None, {src.alias.lower(), src.ref_name.lower(),
                                 src.text.lower()}):
            by_alias.setdefault(key, rs)

    select_aliases = {it.alias.lower() for it in part.items if it.alias}
    only = resolved[0] if len(resolved) == 1 and not resolved[0].error else None

    for ref in part.refs:
        head = ref.path[0]
        # обращение к результату выборки по псевдониму колонки (УПОРЯДОЧИТЬ ПО, ИТОГИ)
        if len(ref.path) == 1 and head.lower() in select_aliases:
            continue
        rs = by_alias.get(head.lower())
        if rs is None:
            if len(ref.path) == 1:
                if only is not None:
                    rs, rest = only, ref.path
                else:
                    warnings.append(
                        f"Строка {ref.line}: поле '{ref.text}' без указания "
                        f"источника — при нескольких таблицах платформа "
                        f"такое не примет.")
                    continue
            elif head.upper() in TABLE_PREFIXES:
                continue        # Перечисление.X.Y и подобное — имя типа
            else:
                errors.append(
                    f"Строка {ref.line}: псевдоним '{head}' не объявлен "
                    f"в секции ИЗ.")
                continue
        else:
            rest = ref.path[1:]
        if not rest:
            continue
        _check_path(rs, rest, ref, provider, errors, warnings, counters)


def _check_path(rs: ResolvedSource, path: list[str], ref, provider,
                errors, warnings, counters):
    """Проверить `Поле[.Поле]` относительно разрешённого источника."""
    if rs.fields is None:
        return                                  # состав полей неизвестен — молчим
    counters["fields"] += 1
    attr = rs.find(path[0])
    if attr is None:
        errors.append(
            f"Строка {ref.line}: у источника '{rs.label}' нет поля "
            f"'{path[0]}'.{_suggest_field(path[0], rs.fields)}")
        return
    if len(path) == 1:
        return
    # спуск на второй уровень через ссылочный тип
    target = attr.ref_target
    if target is None and rs.obj is not None and path[0].lower() in ("ссылка", "родитель"):
        target = rs.obj.id
    if target is None:
        return
    nxt = provider.get_by_id(target)
    if nxt is None:
        return
    fields = _index(nxt.attributes, standard_fields_for(nxt))
    counters["fields"] += 1
    if path[1].lower() not in fields:
        errors.append(
            f"Строка {ref.line}: у {nxt.kind_ru}.{nxt.name} "
            f"(тип поля '{path[0]}') нет поля "
            f"'{path[1]}'.{_suggest_field(path[1], fields)}")


def check_query(text: str, provider: MetadataProvider) -> dict:
    """Проверить текст запроса (в том числе пакет) по метаданным."""
    batch = parse_batch(text)
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    counters = {"tables": 0, "fields": 0}
    warnings.extend(batch.parse_errors)

    # FEAT-2: временные таблицы копятся по ходу пакета; значение None —
    # таблица объявлена, но состав колонок из текста не выводится.
    temp_tables: dict[str, list[str] | None] = {}
    temp_report: list[dict] = []

    for st in batch.statements:
        if st.kind == "drop":
            key = st.drop_table.lower()
            if key and key not in temp_tables:
                warnings.append(
                    f"Строка {st.line}: УНИЧТОЖИТЬ {st.drop_table} — такая "
                    f"временная таблица в пакете не создавалась.")
            temp_tables.pop(key, None)
            continue
        for part in st.parts:
            _check_part(part, provider, temp_tables, errors, warnings, info, counters)
        if st.temp_table:
            cols, known = st.columns()
            temp_tables[st.temp_table.lower()] = cols if known else None
            temp_report.append({
                "name": st.temp_table,
                "columns": cols,
                "columns_known": known,
            })
            if not known:
                warnings.append(
                    f"Строка {st.line}: у временной таблицы {st.temp_table} "
                    f"не все колонки имеют имя (нет КАК или используется *) — "
                    f"проверить поля при обращении к ней не получится.")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "tables_checked": counters["tables"],
        "fields_checked": counters["fields"],
        "temp_tables": temp_report,
        "statements": len(batch.statements),
    }


# ─── FEAT-2: состав полей временной таблицы для query_fields ─────────────

def temp_table_columns(text: str) -> dict[str, list[str] | None]:
    """Все временные таблицы пакета → состав колонок (None — не выводится)."""
    out: dict[str, list[str] | None] = {}
    for st in parse_batch(text).statements:
        if st.kind == "drop":
            for key in [k for k in out if k.lower() == st.drop_table.lower()]:
                out.pop(key)
            continue
        if st.temp_table:
            cols, known = st.columns()
            out[st.temp_table] = cols if known else None
    return out
