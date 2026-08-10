"""
Парсер XML-метаданных 1С → структура узлов и рёбер графа.

Чистый Python без зависимостей от Neo4j и от файловой системы вне walker'а.
Тестируется на синтетических XML-сниппетах.

Дизайн:
  walk_workspace()        — обходит каталог workspace, возвращает список MetaObject.
  build_graph(objects)    — превращает список объектов в (nodes, edges).
  parse_v8_type(s)        — кор-функция: строка "cfg:CatalogRef.X" → (kind, target).

Канонические имена:
  - full_name_eng    "Catalog.АукАукционы"        — основной идентификатор
  - full_name_ru     "Справочники.АукАукционы"    — для обратной совместимости с агентом

Узлы графа:
  :MetadataObject :Catalog :Document :Enum :InformationRegister ...   (двойная метка)
  :Attribute   (свойства: name, synonym, role ∈ {attribute|dimension|resource})
  :TabularSection
  :Type        (свойства: kind, target, qualifiers)
  :EnumValue
  :Form        (свойства: name, isMain)

Рёбра:
  :HAS_ATTRIBUTE        Object →  Attribute, TabularSection → Attribute
  :HAS_TABULAR_SECTION  Object →  TabularSection
  :HAS_FORM             Object →  Form
  :HAS_VALUE            Enum   →  EnumValue
  :OF_TYPE              Attribute → Type
  :RESOLVES_TO          Type   →  MetadataObject  (когда target известен)
  :CONTAINS             Subsystem → MetadataObject
  :PARENT_OF            Subsystem → Subsystem (вложенные подсистемы)
  :OWNED_BY             Catalog/CharType → MetadataObject (Owners)
  :BASED_ON             Document → MetadataObject (BasedOn)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ─── Неймспейсы 1С ────────────────────────────────────────────────────────

NS = {
    "md": "http://v8.1c.ru/8.3/MDClasses",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


# ─── Карта типов метаданных ───────────────────────────────────────────────

# (dir_name, english_kind, russian_kind_singular, russian_kind_plural)
# Расширяемая — сюда же ляжет ExchangePlans, BusinessProcesses, Tasks и пр.
KINDS = [
    ("Catalogs",                    "Catalog",                     "Справочник",              "Справочники"),
    ("Documents",                   "Document",                    "Документ",                "Документы"),
    ("Enums",                       "Enum",                        "Перечисление",            "Перечисления"),
    ("InformationRegisters",        "InformationRegister",         "РегистрСведений",         "РегистрыСведений"),
    ("AccumulationRegisters",       "AccumulationRegister",        "РегистрНакопления",       "РегистрыНакопления"),
    ("AccountingRegisters",         "AccountingRegister",          "РегистрБухгалтерии",      "РегистрыБухгалтерии"),
    ("CalculationRegisters",        "CalculationRegister",         "РегистрРасчета",          "РегистрыРасчета"),
    ("ChartsOfCharacteristicTypes", "ChartOfCharacteristicTypes",  "ПланВидовХарактеристик",  "ПланыВидовХарактеристик"),
    ("ChartsOfAccounts",            "ChartOfAccounts",             "ПланСчетов",              "ПланыСчетов"),
    ("ChartsOfCalculationTypes",    "ChartOfCalculationTypes",     "ПланВидовРасчета",        "ПланыВидовРасчета"),
    ("DocumentJournals",            "DocumentJournal",             "ЖурналДокументов",        "ЖурналыДокументов"),
    ("CommonModules",               "CommonModule",                "ОбщийМодуль",             "ОбщиеМодули"),
    ("DataProcessors",              "DataProcessor",               "Обработка",               "Обработки"),
    ("Reports",                     "Report",                      "Отчет",                   "Отчеты"),
    ("Subsystems",                  "Subsystem",                   "Подсистема",              "Подсистемы"),
    ("CommonCommands",              "CommonCommand",               "ОбщаяКоманда",            "ОбщиеКоманды"),
    ("CommonForms",                 "CommonForm",                  "ОбщаяФорма",              "ОбщиеФормы"),
    ("ExchangePlans",               "ExchangePlan",                "ПланОбмена",              "ПланыОбмена"),
    ("BusinessProcesses",           "BusinessProcess",             "БизнесПроцесс",           "БизнесПроцессы"),
    ("Tasks",                       "Task",                        "Задача",                  "Задачи"),
    ("Constants",                   "Constant",                    "Константа",               "Константы"),
    ("HTTPServices",                "HTTPService",                 "HTTPСервис",              "HTTPСервисы"),
    ("WebServices",                 "WebService",                  "WebСервис",               "WebСервисы"),
    ("ScheduledJobs",               "ScheduledJob",                "РегламентноеЗадание",     "РегламентныеЗадания"),
    ("SettingsStorages",            "SettingsStorage",             "ХранилищеНастроек",       "ХранилищаНастроек"),
    ("FilterCriteria",              "FilterCriterion",             "КритерийОтбора",          "КритерииОтбора"),
    ("SessionParameters",           "SessionParameter",            "ПараметрСеанса",          "ПараметрыСеанса"),
    ("CommonAttributes",            "CommonAttribute",             "ОбщийРеквизит",           "ОбщиеРеквизиты"),
    ("CommonPictures",              "CommonPicture",               "ОбщаяКартинка",           "ОбщиеКартинки"),
    ("CommonTemplates",             "CommonTemplate",              "ОбщийМакет",              "ОбщиеМакеты"),
    ("FunctionalOptions",           "FunctionalOption",            "ФункциональнаяОпция",     "ФункциональныеОпции"),
    ("DefinedTypes",                "DefinedType",                 "ОпределяемыйТип",         "ОпределяемыеТипы"),
    ("Roles",                       "Role",                        "Роль",                    "Роли"),
    ("Languages",                   "Language",                    "Язык",                    "Языки"),
    ("EventSubscriptions",          "EventSubscription",           "ПодпискаНаСобытие",       "ПодпискиНаСобытия"),
]

# Быстрый поиск
KIND_BY_DIR = {k[0]: k for k in KINDS}
KIND_BY_ENG = {k[1]: k for k in KINDS}


# Префиксы ссылок в cfg:XxxRef.Y → kind_eng объекта-цели
TYPE_REF_PREFIX_TO_KIND = {
    "CatalogRef":                    "Catalog",
    "DocumentRef":                   "Document",
    "EnumRef":                       "Enum",
    "ChartOfCharacteristicTypesRef": "ChartOfCharacteristicTypes",
    "ChartOfAccountsRef":            "ChartOfAccounts",
    "ChartOfCalculationTypesRef":    "ChartOfCalculationTypes",
    "BusinessProcessRef":            "BusinessProcess",
    "TaskRef":                       "Task",
    "ExchangePlanRef":               "ExchangePlan",
    "DocumentJournalRef":            "DocumentJournal",
    # Объектные виды (NOT Ref) — это значит «передаётся объект, а не ссылка»;
    # для целей графа поведение такое же — это указание на тот же объект.
    "CatalogObject":                 "Catalog",
    "DocumentObject":                "Document",
    # Записи регистров (RecordKey, RecordSet) → InformationRegister/AccumulationRegister.
    # Префикс не однозначен (один и тот же может быть для разных регистров),
    # но в строке всегда InformationRegisterRecordKey.X / AccumulationRegisterRecordKey.X.
    "InformationRegisterRecordKey":  "InformationRegister",
    "InformationRegisterRecordSet":  "InformationRegister",
    "InformationRegisterRecordManager": "InformationRegister",
    "AccumulationRegisterRecordKey": "AccumulationRegister",
    "AccumulationRegisterRecordSet": "AccumulationRegister",
}

# Префиксы примитивов
XS_PRIMITIVES = {
    "string": "String",
    "decimal": "Number",
    "dateTime": "Date",
    "boolean": "Boolean",
    "base64Binary": "ValueStorage",
    "anyURI": "String",
}


# ─── Домен-объекты ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TypeRef:
    """Ссылка на тип. (kind, target) идентифицирует :Type узел уникально."""
    kind: str                          # "CatalogRef" | "String" | "Number" | "UUID" | "Reference" | "Unknown" | ...
    target: Optional[str] = None       # для *Ref — "Catalog.X"; для примитивов — None
    qualifiers: Optional[tuple] = None  # сериализуем dict в tuple для hashability

    @property
    def is_reference(self) -> bool:
        return self.target is not None and "." in (self.target or "")


@dataclass
class Attribute:
    name: str
    synonym: str = ""
    types: list[TypeRef] = field(default_factory=list)
    role: str = "attribute"            # "attribute" | "dimension" | "resource"
    is_master: bool = False            # для измерений: ведущее измерение
    indexing: str = ""                 # "DontIndex" | "Index" | "IndexWithAdditionalOrder"


@dataclass
class TabularSection:
    name: str
    synonym: str = ""
    attributes: list[Attribute] = field(default_factory=list)


@dataclass
class FormInfo:
    name: str                          # "ФормаЭлемента"
    is_main: bool = False              # один из Default*Form
    main_kind: str = ""                # "DefaultObjectForm" | "DefaultListForm" | ...


@dataclass
class MetaObject:
    """Один объект конфигурации после парсинга XML."""
    kind_eng: str                      # "Catalog"
    kind_ru: str                       # "Справочник"
    kind_ru_plural: str                # "Справочники"
    name: str                          # "АукАукционы"
    synonym: str = ""
    comment: str = ""
    uuid: str = ""
    source_xml: str = ""               # relative path для отладки

    # Структурные дочерние (используется только для конкретных видов)
    attributes:        list[Attribute]      = field(default_factory=list)
    tabular_sections:  list[TabularSection] = field(default_factory=list)
    forms:             list[FormInfo]       = field(default_factory=list)
    enum_values:       list[dict]           = field(default_factory=list)

    # Списки ссылок на другие meta-объекты (по строке "Catalog.X" / "Document.Y" / ...)
    owners:    list[str] = field(default_factory=list)     # для Catalog/CharacteristicTypes
    based_on:  list[str] = field(default_factory=list)     # для Document
    contained: list[str] = field(default_factory=list)     # для Subsystem
    sub_subsystems: list[str] = field(default_factory=list) # для Subsystem (вложенные)
    registrations:  list[str] = field(default_factory=list) # для DocumentJournal: RegisteredDocuments

    # Произвольные свойства (для CommonModule — Server/Client/Privileged/ReturnValuesReuse)
    properties: dict = field(default_factory=dict)

    @property
    def full_name_eng(self) -> str:
        return f"{self.kind_eng}.{self.name}"

    @property
    def full_name_ru(self) -> str:
        return f"{self.kind_ru_plural}.{self.name}"


# ─── Парсер строк типов ───────────────────────────────────────────────────

def parse_v8_type(s: str) -> TypeRef:
    """
    'cfg:CatalogRef.АукВидыАукционов'                  → TypeRef("CatalogRef", "Catalog.АукВидыАукционов")
    'cfg:DocumentObject.Заказ'                         → TypeRef("DocumentObject", "Document.Заказ")
    'xs:string'                                        → TypeRef("String")
    'xs:decimal'                                       → TypeRef("Number")
    'xs:dateTime'                                      → TypeRef("Date")
    'xs:boolean'                                       → TypeRef("Boolean")
    'v8:UUID'                                          → TypeRef("UUID")
    'v8:ValueStorage'                                  → TypeRef("ValueStorage")
    ''                                                 → TypeRef("Unknown")
    """
    if not s:
        return TypeRef("Unknown")
    s = s.strip()
    if s.startswith("cfg:"):
        rest = s[4:]
        # Перебираем известные префиксы. CatalogRef должен резолвиться раньше Catalog,
        # т.к. CatalogRef.X.startswith("Catalog") вернёт True. Сортируем по длине убыв.
        for prefix in sorted(TYPE_REF_PREFIX_TO_KIND, key=len, reverse=True):
            if rest.startswith(prefix + "."):
                target_kind = TYPE_REF_PREFIX_TO_KIND[prefix]
                target_name = rest[len(prefix) + 1:]
                return TypeRef(prefix, f"{target_kind}.{target_name}")
        # Не угадали префикс — это AnyRef или Characteristic или ещё что-то.
        # Сохраняем как Reference с raw target.
        return TypeRef("Reference", rest)
    if s.startswith("xs:"):
        prim = s[3:]
        return TypeRef(XS_PRIMITIVES.get(prim, prim.capitalize()))
    if s.startswith("v8:"):
        return TypeRef(s[3:])
    return TypeRef("Unknown", s)


# ─── Утилиты ET ───────────────────────────────────────────────────────────

def _text(elem) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _find(elem, tag: str):
    return elem.find(f"md:{tag}", NS) if elem is not None else None


def _findall(elem, tag: str):
    return elem.findall(f"md:{tag}", NS) if elem is not None else []


def _local_tag(elem) -> str:
    t = elem.tag
    return t.split("}", 1)[1] if "}" in t else t


def _parse_synonym(props) -> str:
    """<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>X</v8:content></v8:item></Synonym>"""
    syn = _find(props, "Synonym")
    if syn is None:
        return ""
    for item in syn.findall("v8:item", NS):
        lang = item.find("v8:lang", NS)
        if lang is not None and (lang.text or "").strip() == "ru":
            content = item.find("v8:content", NS)
            return _text(content)
    # Если ru нет — берём первый
    for item in syn.findall("v8:item", NS):
        content = item.find("v8:content", NS)
        if content is not None and (content.text or "").strip():
            return _text(content)
    return ""


def _parse_md_object_refs(parent_elem) -> list[str]:
    """
    <Owners>
      <xr:Item xsi:type="xr:MDObjectRef">ChartOfCharacteristicTypes.X</xr:Item>
      ...
    </Owners>
    Возвращает список строк "Kind.Name".
    """
    if parent_elem is None:
        return []
    out = []
    for item in parent_elem.findall("xr:Item", NS):
        s = _text(item)
        if s:
            out.append(s)
    return out


def _parse_type_block(type_elem) -> list[TypeRef]:
    """
    <Type>
      <v8:Type>cfg:CatalogRef.X</v8:Type>
      <v8:Type>xs:string</v8:Type>          ← composite
      <v8:StringQualifiers>…</v8:StringQualifiers>
    </Type>
    """
    if type_elem is None:
        return [TypeRef("Unknown")]
    types: list[TypeRef] = []
    for t in type_elem.findall("v8:Type", NS):
        types.append(parse_v8_type(_text(t)))
    if not types:
        return [TypeRef("Unknown")]
    return types


def _parse_attribute(elem, role: str = "attribute") -> Optional[Attribute]:
    props = _find(elem, "Properties")
    if props is None:
        return None
    name = _text(_find(props, "Name"))
    if not name:
        return None
    a = Attribute(
        name=name,
        synonym=_parse_synonym(props),
        types=_parse_type_block(_find(props, "Type")),
        role=role,
        indexing=_text(_find(props, "Indexing")),
    )
    if role == "dimension":
        master = _find(props, "Master")
        if master is not None and _text(master).lower() == "true":
            a.is_master = True
    return a


def _parse_forms_section(inner_elem, props) -> list[FormInfo]:
    """
    Формы:
      - <Form>ФормаЭлемента</Form> внутри <ChildObjects> — список имён.
      - DefaultObjectForm, DefaultListForm, DefaultFolderForm, DefaultChoiceForm,
        DefaultFolderChoiceForm — указатели на главные.
    Возвращает список FormInfo, ровно по одному на каждое уникальное имя формы.
    """
    forms_by_name: dict[str, FormInfo] = {}

    # Имена форм
    child_objects = _find(inner_elem, "ChildObjects")
    if child_objects is not None:
        for f_el in _findall(child_objects, "Form"):
            name = _text(f_el)
            if name:
                forms_by_name.setdefault(name, FormInfo(name=name))

    # Default*Form ссылки — выделяем главные
    DEFAULT_FORM_TAGS = (
        "DefaultObjectForm", "DefaultListForm", "DefaultFolderForm",
        "DefaultChoiceForm", "DefaultFolderChoiceForm",
        "DefaultRecordForm",                       # для регистров
        "AuxiliaryObjectForm", "AuxiliaryListForm",  # вспомогательные — не отмечаем как main
    )
    if props is not None:
        for tag in DEFAULT_FORM_TAGS:
            el = _find(props, tag)
            if el is None:
                continue
            ref = _text(el)            # "Catalog.X.Form.ФормаЭлемента"
            if not ref:
                continue
            form_name = ref.split(".")[-1]
            if not form_name:
                continue
            fi = forms_by_name.setdefault(form_name, FormInfo(name=form_name))
            if tag.startswith("Default"):
                fi.is_main = True
                if not fi.main_kind:
                    fi.main_kind = tag

    return list(forms_by_name.values())


# ─── Парсер по типу объекта ───────────────────────────────────────────────

def _parse_subsystem(inner, props) -> dict:
    """Subsystem: Content + вложенные подсистемы (ChildObjects/Subsystem)."""
    contained = _parse_md_object_refs(_find(props, "Content"))
    sub_subs = []
    child_objects = _find(inner, "ChildObjects")
    if child_objects is not None:
        for sub in _findall(child_objects, "Subsystem"):
            sub_props = _find(sub, "Properties")
            sub_name = _text(_find(sub_props, "Name")) if sub_props is not None else ""
            if sub_name:
                sub_subs.append(f"Subsystem.{sub_name}")
    return {"contained": contained, "sub_subsystems": sub_subs}


def _parse_enum(inner, props) -> list[dict]:
    """Enum: ChildObjects/EnumValue."""
    values = []
    child_objects = _find(inner, "ChildObjects")
    if child_objects is None:
        return values
    for ev in _findall(child_objects, "EnumValue"):
        ev_props = _find(ev, "Properties")
        if ev_props is None:
            continue
        name = _text(_find(ev_props, "Name"))
        if name:
            values.append({
                "name": name,
                "synonym": _parse_synonym(ev_props),
            })
    return values


def _parse_common_module_flags(props) -> dict:
    """Свойства общего модуля: Global, Server, ClientManagedApplication, ServerCall, Privileged."""
    out = {}
    for tag in ("Global", "Server", "ClientManagedApplication",
                "ClientOrdinaryApplication", "ExternalConnection",
                "ServerCall", "Privileged", "ReturnValuesReuse"):
        el = _find(props, tag)
        if el is not None:
            out[tag] = _text(el)
    return out


# ─── FEAT-1.1: структурные признаки объекта ──────────────────────────────
#
# Нужны, чтобы `query_validate` перестал молча принимать `Т.Родитель` у
# неиерархического справочника и `Т.Владелец` у неподчинённого. Сейчас
# стандартные реквизиты подставляются списком по виду объекта, и список
# намеренно избыточен — проверять было нечем.
#
# Собирается здесь, а не на стороне сервера, по той же причине, что и
# остальной слой 1: признак лежит в XML выгрузки, сервер XML не читает.
# Кладётся в `properties`, откуда graph_writer уже пишет его в
# `properties_json` — менять запись в граф не потребовалось.
#
# Владелец в properties не дублируется: подчинённость и так видна по рёбрам
# OWNED_BY, которые строятся из `obj.owners`.

_STRUCTURAL_FLAGS = ("Hierarchical", "HierarchyType", "Numerator",
                     "Autonumbering", "Periodicity", "WriteMode",
                     "InformationRegisterPeriodicity")


def _parse_structural_flags(props) -> dict:
    """Признаки, влияющие на состав стандартных реквизитов объекта.

    Отсутствующий тег — не ошибка: у части видов объектов его и не бывает.
    Значение возвращается строкой, как лежит в XML ("true" / "false").
    """
    out = {}
    if props is None:
        return out
    for tag in _STRUCTURAL_FLAGS:
        el = _find(props, tag)
        if el is not None:
            out[tag] = _text(el)
    return out


def _parse_object(path: Path, kind_eng: str, kind_ru: str, kind_ru_plural: str,
                  source_rel: str) -> Optional[MetaObject]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, FileNotFoundError, PermissionError):
        return None
    root = tree.getroot()
    inner = root.find(f"md:{kind_eng}", NS)
    if inner is None:
        return None
    props = _find(inner, "Properties")
    if props is None:
        return None

    name = _text(_find(props, "Name"))
    if not name:
        return None

    obj = MetaObject(
        kind_eng=kind_eng,
        kind_ru=kind_ru,
        kind_ru_plural=kind_ru_plural,
        name=name,
        synonym=_parse_synonym(props),
        comment=_text(_find(props, "Comment")),
        uuid=inner.get("uuid", ""),
        source_xml=source_rel,
    )

    # ─ Подсистема ─
    if kind_eng == "Subsystem":
        sub = _parse_subsystem(inner, props)
        obj.contained = sub["contained"]
        obj.sub_subsystems = sub["sub_subsystems"]
        return obj

    # ─ Перечисление ─
    if kind_eng == "Enum":
        obj.enum_values = _parse_enum(inner, props)
        return obj

    # ─ Общий модуль ─ (без BSL — это 4.6.2)
    if kind_eng == "CommonModule":
        obj.properties = _parse_common_module_flags(props)
        return obj

    # FEAT-1.1: структурные признаки. Ставятся до разбора ChildObjects,
    # чтобы попасть в объект независимо от того, есть у него реквизиты
    # или нет.
    obj.properties = _parse_structural_flags(props)

    # ─ Любой объект с реквизитами / измерениями / ресурсами / ТЧ / формами ─
    child_objects = _find(inner, "ChildObjects")
    if child_objects is not None:
        for ch in child_objects:
            tag = _local_tag(ch)
            if tag == "Attribute":
                a = _parse_attribute(ch, role="attribute")
                if a: obj.attributes.append(a)
            elif tag == "Dimension":
                a = _parse_attribute(ch, role="dimension")
                if a: obj.attributes.append(a)
            elif tag == "Resource":
                a = _parse_attribute(ch, role="resource")
                if a: obj.attributes.append(a)
            elif tag == "TabularSection":
                ts_props = _find(ch, "Properties")
                ts_name = _text(_find(ts_props, "Name")) if ts_props is not None else ""
                ts_synonym = _parse_synonym(ts_props) if ts_props is not None else ""
                ts_attrs: list[Attribute] = []
                ts_child = _find(ch, "ChildObjects")
                if ts_child is not None:
                    for tch in _findall(ts_child, "Attribute"):
                        ta = _parse_attribute(tch, role="attribute")
                        if ta: ts_attrs.append(ta)
                if ts_name:
                    obj.tabular_sections.append(TabularSection(
                        name=ts_name, synonym=ts_synonym, attributes=ts_attrs,
                    ))
            elif tag == "Form":
                # Имена форм собирает _parse_forms_section ниже
                pass

    obj.forms = _parse_forms_section(inner, props)

    # ─ Owners (Catalog, ChartOfCharacteristicTypes) ─
    obj.owners = _parse_md_object_refs(_find(props, "Owners"))

    # ─ BasedOn (Document) ─
    obj.based_on = _parse_md_object_refs(_find(props, "BasedOn"))

    # ─ Registered documents (DocumentJournal) ─
    obj.registrations = _parse_md_object_refs(_find(props, "RegisteredDocuments"))

    return obj


# ─── Walker ───────────────────────────────────────────────────────────────

def walk_workspace(root: Path) -> list[MetaObject]:
    """
    Обходит workspace, парсит все верхнеуровневые XML.
    Файлы в подкаталогах (Forms/*.xml, Templates/*.xml) НЕ трогает —
    они уже описаны через <Form> / <Template> в верхнем XML.
    """
    out: list[MetaObject] = []
    for dir_name, kind_eng, kind_ru, kind_ru_plural in KINDS:
        d = root / dir_name
        if not d.is_dir():
            continue
        for xml_path in sorted(d.glob("*.xml")):
            rel = xml_path.relative_to(root).as_posix()
            obj = _parse_object(xml_path, kind_eng, kind_ru, kind_ru_plural, rel)
            if obj is not None:
                out.append(obj)
    return out


# ─── Сборка графа: список объектов → узлы и рёбра ─────────────────────────

def build_graph(objects: list[MetaObject]) -> dict:
    """
    Возвращает:
      {
        "meta_nodes":   [...],   # узлы :MetadataObject (по одному на объект)
        "attr_nodes":   [...],   # узлы :Attribute   (с парент-ссылкой)
        "ts_nodes":     [...],   # узлы :TabularSection
        "form_nodes":   [...],   # узлы :Form
        "enum_value_nodes": [...], # узлы :EnumValue
        "type_nodes":   [...],   # узлы :Type, уникальные по (kind, target)
        "edges":        [...],   # все рёбра
        "stats":        {...},   # сводка
        "unresolved":   {...},   # для отладки
      }
    """
    by_fn = {o.full_name_eng: o for o in objects}

    meta_nodes = []
    attr_nodes = []
    ts_nodes = []
    form_nodes = []
    enum_value_nodes = []
    type_nodes_set: set[tuple] = set()
    edges = []
    unresolved_targets: Counter = Counter()

    def add_edge(rel, src_id, dst_id, _src_label=None, **props):
        # PERF-4: `_src_label` — метка узла-источника. Нужна writer'у, чтобы
        # подставить MATCH с меткой, а значит, с индексом. Задаётся только
        # там, где у одного типа ребра источники разных типов (:HAS_ATTRIBUTE
        # идёт и от :MetadataObject, и от :TabularSection). Подчёркивание в
        # имени — чтобы не столкнуться с именем свойства ребра из **props.
        e = {"rel": rel, "src": src_id, "dst": dst_id, "props": props or {}}
        if _src_label:
            e["src_label"] = _src_label
        edges.append(e)

    def type_id(t: TypeRef) -> str:
        # Стабильный id для узла типа
        if t.target:
            return f"Type:{t.kind}:{t.target}"
        return f"Type:{t.kind}"

    def resolve_type(t: TypeRef, src_id: str, **edge_props):
        type_nodes_set.add((t.kind, t.target))
        t_id = type_id(t)
        add_edge("OF_TYPE", src_id, t_id, **edge_props)
        # RESOLVES_TO появляется при втором проходе (после того как мы знаем все meta_nodes)

    for o in objects:
        meta_id = o.full_name_eng        # совпадает с естественным ключом
        meta_nodes.append({
            "id":             meta_id,
            "kind_eng":       o.kind_eng,
            "kind_ru":        o.kind_ru,
            "kind_ru_plural": o.kind_ru_plural,
            "name":           o.name,
            "synonym":        o.synonym,
            "comment":        o.comment,
            "uuid":           o.uuid,
            "full_name_eng":  o.full_name_eng,
            "full_name_ru":   o.full_name_ru,
            "source_xml":     o.source_xml,
            "properties":     o.properties,
        })

        # ─ Атрибуты, измерения, ресурсы (на верхнем уровне) ─
        for a in o.attributes:
            a_id = f"{meta_id}.Attr.{a.name}"
            attr_nodes.append({
                "id":      a_id,
                "name":    a.name,
                "synonym": a.synonym,
                "role":    a.role,
                "is_master": a.is_master,
                "indexing": a.indexing,
                "parent":  meta_id,
            })
            add_edge("HAS_ATTRIBUTE", meta_id, a_id,
                     _src_label="MetadataObject", role=a.role)
            for t in a.types:
                resolve_type(t, a_id)

        # ─ Табличные части ─
        for ts in o.tabular_sections:
            ts_id = f"{meta_id}.TS.{ts.name}"
            ts_nodes.append({
                "id":      ts_id,
                "name":    ts.name,
                "synonym": ts.synonym,
                "parent":  meta_id,
            })
            add_edge("HAS_TABULAR_SECTION", meta_id, ts_id)
            for a in ts.attributes:
                a_id = f"{ts_id}.Attr.{a.name}"
                attr_nodes.append({
                    "id":      a_id,
                    "name":    a.name,
                    "synonym": a.synonym,
                    "role":    "attribute",
                    "parent":  ts_id,
                })
                add_edge("HAS_ATTRIBUTE", ts_id, a_id,
                         _src_label="TabularSection", role="attribute")
                for t in a.types:
                    resolve_type(t, a_id)

        # ─ Формы ─
        for f in o.forms:
            f_id = f"{meta_id}.Form.{f.name}"
            form_nodes.append({
                "id":        f_id,
                "name":      f.name,
                "is_main":   f.is_main,
                "main_kind": f.main_kind,
                "parent":    meta_id,
            })
            add_edge("HAS_FORM", meta_id, f_id, is_main=f.is_main, main_kind=f.main_kind)

        # ─ Значения перечислений ─
        for ev in o.enum_values:
            ev_id = f"{meta_id}.Value.{ev['name']}"
            enum_value_nodes.append({
                "id":      ev_id,
                "name":    ev["name"],
                "synonym": ev.get("synonym", ""),
                "parent":  meta_id,
            })
            add_edge("HAS_VALUE", meta_id, ev_id)

        # ─ CONTAINS (Подсистема → объект) ─
        for ref in o.contained:
            if ref in by_fn:
                add_edge("CONTAINS", meta_id, ref)
            else:
                unresolved_targets[f"CONTAINS→{ref}"] += 1

        # ─ PARENT_OF (вложенные подсистемы) ─
        for ref in o.sub_subsystems:
            if ref in by_fn:
                add_edge("PARENT_OF", meta_id, ref)
            else:
                unresolved_targets[f"PARENT_OF→{ref}"] += 1

        # ─ OWNED_BY (справочник подчинён владельцу) ─
        for ref in o.owners:
            if ref in by_fn:
                add_edge("OWNED_BY", meta_id, ref)
            else:
                unresolved_targets[f"OWNED_BY→{ref}"] += 1

        # ─ BASED_ON (документ на основании) ─
        for ref in o.based_on:
            if ref in by_fn:
                add_edge("BASED_ON", meta_id, ref)
            else:
                unresolved_targets[f"BASED_ON→{ref}"] += 1

        # ─ Journal REGISTERS documents ─
        for ref in o.registrations:
            if ref in by_fn:
                add_edge("REGISTERS", meta_id, ref)
            else:
                unresolved_targets[f"REGISTERS→{ref}"] += 1

    # ─ Второй проход: RESOLVES_TO от Type-узлов к meta-узлам ─
    type_nodes = []
    for (kind, target) in sorted(type_nodes_set, key=lambda x: (x[0], x[1] or "")):
        tid = f"Type:{kind}:{target}" if target else f"Type:{kind}"
        type_nodes.append({
            "id":     tid,
            "kind":   kind,
            "target": target,
        })
        if target and "." in target and target in by_fn:
            edges.append({"rel": "RESOLVES_TO", "src": tid, "dst": target, "props": {}})
        elif target and "." in target:
            unresolved_targets[f"RESOLVES_TO→{target}"] += 1

    # ─ Подсчёты ─
    edge_kinds = Counter(e["rel"] for e in edges)
    stats = {
        "meta_objects":     len(meta_nodes),
        "attributes":       len(attr_nodes),
        "tabular_sections": len(ts_nodes),
        "forms":            len(form_nodes),
        "enum_values":      len(enum_value_nodes),
        "type_nodes":       len(type_nodes),
        "edges_total":      len(edges),
        "edges_by_kind":    dict(edge_kinds),
        "unresolved_refs":  sum(unresolved_targets.values()),
    }

    return {
        "meta_nodes":       meta_nodes,
        "attr_nodes":       attr_nodes,
        "ts_nodes":         ts_nodes,
        "form_nodes":       form_nodes,
        "enum_value_nodes": enum_value_nodes,
        "type_nodes":       type_nodes,
        "edges":            edges,
        "stats":            stats,
        "unresolved":       dict(unresolved_targets),
    }
