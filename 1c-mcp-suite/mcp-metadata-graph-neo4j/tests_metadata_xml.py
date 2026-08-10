"""
Unit-тесты парсера XML-метаданных.

Покрывают:
  - parse_v8_type на всех вариантах строк типов
  - parse_synonym (с ru-локалью и без)
  - composite types
  - Owners / BasedOn / Subsystem.Content
  - Enum.EnumValue
  - CommonModule.properties (флаги)
  - build_graph: подсчёт узлов и рёбер на синтетическом minimal-наборе

Запуск:  python3 tests_metadata_xml.py
"""
from __future__ import annotations

import io
import unittest

import metadata_xml as mx
import xml.etree.ElementTree as ET
from pathlib import Path

from metadata_xml import (
    parse_v8_type, TypeRef, MetaObject, Attribute, TabularSection, FormInfo,
    _parse_synonym, _parse_md_object_refs, _parse_type_block,
    _parse_object, build_graph, NS,
)


# ─── Маленькие XML-фикстуры ────────────────────────────────────────────────

NSDECL = (
    'xmlns="http://v8.1c.ru/8.3/MDClasses" '
    'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
    'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)

CATALOG_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <Catalog uuid="uuid-cat-1">
    <Properties>
      <Name>АукАукционы</Name>
      <Synonym>
        <v8:item><v8:lang>ru</v8:lang><v8:content>Аукционы</v8:content></v8:item>
      </Synonym>
      <Comment>Тестовый</Comment>
      <Owners>
        <xr:Item xsi:type="xr:MDObjectRef">ChartOfCharacteristicTypes.AnyChart</xr:Item>
      </Owners>
      <DefaultObjectForm>Catalog.АукАукционы.Form.ФормаЭлемента</DefaultObjectForm>
    </Properties>
    <ChildObjects>
      <Attribute>
        <Properties>
          <Name>ВидАукциона</Name>
          <Synonym>
            <v8:item><v8:lang>ru</v8:lang><v8:content>Вид</v8:content></v8:item>
          </Synonym>
          <Type><v8:Type>cfg:CatalogRef.АукВидыАукционов</v8:Type></Type>
        </Properties>
      </Attribute>
      <Attribute>
        <Properties>
          <Name>Комментарий</Name>
          <Type>
            <v8:Type>xs:string</v8:Type>
            <v8:StringQualifiers><v8:Length>100</v8:Length></v8:StringQualifiers>
          </Type>
        </Properties>
      </Attribute>
      <TabularSection>
        <Properties>
          <Name>Участники</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Уч-ки</v8:content></v8:item></Synonym>
        </Properties>
        <ChildObjects>
          <Attribute>
            <Properties>
              <Name>Участник</Name>
              <Type><v8:Type>cfg:CatalogRef.АукВидыАукционов</v8:Type></Type>
            </Properties>
          </Attribute>
        </ChildObjects>
      </TabularSection>
      <Form>ФормаЭлемента</Form>
    </ChildObjects>
  </Catalog>
</MetaDataObject>
"""

CATALOG_TARGET_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <Catalog uuid="uuid-cat-2">
    <Properties>
      <Name>АукВидыАукционов</Name>
    </Properties>
    <ChildObjects/>
  </Catalog>
</MetaDataObject>
"""

ENUM_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <Enum uuid="uuid-enum-1">
    <Properties>
      <Name>Статусы</Name>
    </Properties>
    <ChildObjects>
      <EnumValue>
        <Properties>
          <Name>Активен</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Активен</v8:content></v8:item></Synonym>
        </Properties>
      </EnumValue>
      <EnumValue>
        <Properties>
          <Name>Закрыт</Name>
        </Properties>
      </EnumValue>
    </ChildObjects>
  </Enum>
</MetaDataObject>
"""

REGISTER_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <InformationRegister uuid="uuid-ir-1">
    <Properties>
      <Name>ШаблоныСообщений</Name>
    </Properties>
    <ChildObjects>
      <Dimension>
        <Properties>
          <Name>Получатель</Name>
          <Type><v8:Type>cfg:CatalogRef.АукВидыАукционов</v8:Type></Type>
          <Master>true</Master>
        </Properties>
      </Dimension>
      <Resource>
        <Properties>
          <Name>Текст</Name>
          <Type><v8:Type>xs:string</v8:Type></Type>
        </Properties>
      </Resource>
      <Attribute>
        <Properties>
          <Name>Комментарий</Name>
          <Type><v8:Type>xs:string</v8:Type></Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </InformationRegister>
</MetaDataObject>
"""

SUBSYSTEM_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <Subsystem uuid="uuid-sub-1">
    <Properties>
      <Name>Аукционы</Name>
      <Content>
        <xr:Item xsi:type="xr:MDObjectRef">Catalog.АукАукционы</xr:Item>
        <xr:Item xsi:type="xr:MDObjectRef">Catalog.АукВидыАукционов</xr:Item>
        <xr:Item xsi:type="xr:MDObjectRef">Enum.Статусы</xr:Item>
      </Content>
    </Properties>
    <ChildObjects/>
  </Subsystem>
</MetaDataObject>
"""

COMMON_MODULE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <CommonModule uuid="uuid-cm-1">
    <Properties>
      <Name>АукОбщийКлиент</Name>
      <Global>false</Global>
      <Server>false</Server>
      <ClientManagedApplication>true</ClientManagedApplication>
      <ServerCall>false</ServerCall>
      <Privileged>false</Privileged>
      <ReturnValuesReuse>DontUse</ReturnValuesReuse>
    </Properties>
  </CommonModule>
</MetaDataObject>
"""

COMPOSITE_TYPE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NSDECL}>
  <Catalog uuid="uuid-cat-comp">
    <Properties>
      <Name>X</Name>
    </Properties>
    <ChildObjects>
      <Attribute>
        <Properties>
          <Name>Значение</Name>
          <Type>
            <v8:Type>cfg:CatalogRef.АукВидыАукционов</v8:Type>
            <v8:Type>xs:string</v8:Type>
            <v8:Type>xs:decimal</v8:Type>
          </Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </Catalog>
</MetaDataObject>
"""


# ─── Утилиты ──────────────────────────────────────────────────────────────

def write_xml(tmp_root: Path, dirname: str, filename: str, content: str) -> Path:
    """Записывает XML-фикстуру в tmp_root/dirname/filename, возвращает путь."""
    d = tmp_root / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(content, encoding="utf-8")
    return p


def parse_string_as(xml_text: str, kind_eng: str, kind_ru: str = "Х", kind_ru_plural: str = "Хы"):
    """Парсит XML-строку через _parse_object (через файл в /tmp)."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml_text)
        tmp = Path(f.name)
    try:
        return _parse_object(tmp, kind_eng, kind_ru, kind_ru_plural, source_rel=tmp.name)
    finally:
        tmp.unlink(missing_ok=True)


# ─── Тесты parse_v8_type ──────────────────────────────────────────────────

class TestParseV8Type(unittest.TestCase):
    def test_catalog_ref(self):
        t = parse_v8_type("cfg:CatalogRef.АукВидыАукционов")
        self.assertEqual(t.kind, "CatalogRef")
        self.assertEqual(t.target, "Catalog.АукВидыАукционов")

    def test_document_ref(self):
        t = parse_v8_type("cfg:DocumentRef.Заказ")
        self.assertEqual(t.kind, "DocumentRef")
        self.assertEqual(t.target, "Document.Заказ")

    def test_enum_ref(self):
        t = parse_v8_type("cfg:EnumRef.Статусы")
        self.assertEqual(t.kind, "EnumRef")
        self.assertEqual(t.target, "Enum.Статусы")

    def test_information_register_record_set(self):
        t = parse_v8_type("cfg:InformationRegisterRecordSet.Х")
        self.assertEqual(t.kind, "InformationRegisterRecordSet")
        self.assertEqual(t.target, "InformationRegister.Х")

    def test_chart_of_characteristic_types_ref(self):
        t = parse_v8_type("cfg:ChartOfCharacteristicTypesRef.Y")
        self.assertEqual(t.kind, "ChartOfCharacteristicTypesRef")
        self.assertEqual(t.target, "ChartOfCharacteristicTypes.Y")

    def test_unknown_cfg_prefix(self):
        """AnyRef и подобные → Reference"""
        t = parse_v8_type("cfg:AnyRef")
        self.assertEqual(t.kind, "Reference")

    def test_xs_string(self):
        t = parse_v8_type("xs:string")
        self.assertEqual(t.kind, "String")
        self.assertIsNone(t.target)

    def test_xs_decimal(self):
        self.assertEqual(parse_v8_type("xs:decimal").kind, "Number")

    def test_xs_datetime(self):
        self.assertEqual(parse_v8_type("xs:dateTime").kind, "Date")

    def test_xs_boolean(self):
        self.assertEqual(parse_v8_type("xs:boolean").kind, "Boolean")

    def test_xs_base64_binary(self):
        self.assertEqual(parse_v8_type("xs:base64Binary").kind, "ValueStorage")

    def test_v8_uuid(self):
        self.assertEqual(parse_v8_type("v8:UUID").kind, "UUID")

    def test_v8_value_storage(self):
        self.assertEqual(parse_v8_type("v8:ValueStorage").kind, "ValueStorage")

    def test_empty(self):
        self.assertEqual(parse_v8_type("").kind, "Unknown")

    def test_whitespace(self):
        t = parse_v8_type("  cfg:CatalogRef.X  ")
        self.assertEqual(t.kind, "CatalogRef")
        self.assertEqual(t.target, "Catalog.X")

    def test_ref_priority_longer_prefix_first(self):
        """CatalogRef.X должен парситься как CatalogRef, не как Catalog (объект)."""
        t = parse_v8_type("cfg:CatalogRef.X")
        self.assertEqual(t.kind, "CatalogRef")


# ─── Тесты парсера одного объекта ─────────────────────────────────────────

class TestParseObject(unittest.TestCase):
    def test_catalog_basic(self):
        obj = parse_string_as(CATALOG_XML, "Catalog")
        self.assertIsNotNone(obj)
        self.assertEqual(obj.name, "АукАукционы")
        self.assertEqual(obj.synonym, "Аукционы")
        self.assertEqual(obj.full_name_eng, "Catalog.АукАукционы")
        self.assertEqual(obj.full_name_ru, "Хы.АукАукционы")
        self.assertEqual(obj.comment, "Тестовый")
        self.assertEqual(obj.uuid, "uuid-cat-1")
        # 2 атрибута
        self.assertEqual(len(obj.attributes), 2)
        self.assertEqual(obj.attributes[0].name, "ВидАукциона")
        self.assertEqual(obj.attributes[0].synonym, "Вид")
        self.assertEqual(obj.attributes[0].role, "attribute")
        # тип реквизита
        self.assertEqual(len(obj.attributes[0].types), 1)
        self.assertEqual(obj.attributes[0].types[0].kind, "CatalogRef")
        self.assertEqual(obj.attributes[0].types[0].target, "Catalog.АукВидыАукционов")

    def test_catalog_tabular_section(self):
        obj = parse_string_as(CATALOG_XML, "Catalog")
        self.assertEqual(len(obj.tabular_sections), 1)
        ts = obj.tabular_sections[0]
        self.assertEqual(ts.name, "Участники")
        self.assertEqual(ts.synonym, "Уч-ки")
        self.assertEqual(len(ts.attributes), 1)
        self.assertEqual(ts.attributes[0].name, "Участник")

    def test_catalog_owners(self):
        obj = parse_string_as(CATALOG_XML, "Catalog")
        self.assertEqual(obj.owners, ["ChartOfCharacteristicTypes.AnyChart"])

    def test_catalog_form(self):
        obj = parse_string_as(CATALOG_XML, "Catalog")
        self.assertEqual(len(obj.forms), 1)
        f = obj.forms[0]
        self.assertEqual(f.name, "ФормаЭлемента")
        self.assertTrue(f.is_main)
        self.assertEqual(f.main_kind, "DefaultObjectForm")

    def test_enum_values(self):
        obj = parse_string_as(ENUM_XML, "Enum")
        self.assertEqual(obj.name, "Статусы")
        self.assertEqual(len(obj.enum_values), 2)
        self.assertEqual(obj.enum_values[0]["name"], "Активен")
        self.assertEqual(obj.enum_values[0]["synonym"], "Активен")
        self.assertEqual(obj.enum_values[1]["name"], "Закрыт")
        self.assertEqual(obj.enum_values[1]["synonym"], "")

    def test_register_roles(self):
        """Размеры (Dimension) и ресурсы (Resource) — это атрибуты с разным role."""
        obj = parse_string_as(REGISTER_XML, "InformationRegister")
        self.assertEqual(len(obj.attributes), 3)
        roles = {a.name: a.role for a in obj.attributes}
        self.assertEqual(roles["Получатель"], "dimension")
        self.assertEqual(roles["Текст"], "resource")
        self.assertEqual(roles["Комментарий"], "attribute")
        # Master флаг
        master_dim = [a for a in obj.attributes if a.role == "dimension"][0]
        self.assertTrue(master_dim.is_master)

    def test_subsystem(self):
        obj = parse_string_as(SUBSYSTEM_XML, "Subsystem")
        self.assertEqual(obj.name, "Аукционы")
        self.assertEqual(len(obj.contained), 3)
        self.assertIn("Catalog.АукАукционы", obj.contained)
        self.assertIn("Enum.Статусы", obj.contained)
        self.assertEqual(obj.sub_subsystems, [])

    def test_common_module_flags(self):
        obj = parse_string_as(COMMON_MODULE_XML, "CommonModule")
        self.assertEqual(obj.name, "АукОбщийКлиент")
        self.assertEqual(obj.properties.get("ClientManagedApplication"), "true")
        self.assertEqual(obj.properties.get("Server"), "false")
        self.assertEqual(obj.properties.get("ServerCall"), "false")

    def test_composite_type(self):
        obj = parse_string_as(COMPOSITE_TYPE_XML, "Catalog")
        self.assertEqual(len(obj.attributes), 1)
        types = obj.attributes[0].types
        self.assertEqual(len(types), 3)
        kinds = sorted(t.kind for t in types)
        self.assertEqual(kinds, ["CatalogRef", "Number", "String"])
        # CatalogRef нашёл target
        catref = [t for t in types if t.kind == "CatalogRef"][0]
        self.assertEqual(catref.target, "Catalog.АукВидыАукционов")


# ─── Тесты build_graph ────────────────────────────────────────────────────

class TestBuildGraph(unittest.TestCase):
    def setUp(self):
        # Соберём minimal-набор объектов
        self.cat_a = parse_string_as(CATALOG_XML, "Catalog")
        self.cat_b = parse_string_as(CATALOG_TARGET_XML, "Catalog")
        self.enum = parse_string_as(ENUM_XML, "Enum")
        self.register = parse_string_as(REGISTER_XML, "InformationRegister",
                                         kind_ru="РегСвед", kind_ru_plural="РегистрыСведений")
        self.subsystem = parse_string_as(SUBSYSTEM_XML, "Subsystem")
        self.common = parse_string_as(COMMON_MODULE_XML, "CommonModule")
        self.objects = [self.cat_a, self.cat_b, self.enum, self.register,
                        self.subsystem, self.common]
        self.g = build_graph(self.objects)

    def test_meta_node_count(self):
        self.assertEqual(self.g["stats"]["meta_objects"], 6)

    def test_attributes_count(self):
        # cat_a: 2 + 1 (в ТЧ) = 3
        # cat_b: 0
        # register: 3 (1 dim + 1 res + 1 attr)
        # subsystem/common/enum: 0
        self.assertEqual(self.g["stats"]["attributes"], 6)

    def test_tabular_sections(self):
        self.assertEqual(self.g["stats"]["tabular_sections"], 1)

    def test_forms(self):
        self.assertEqual(self.g["stats"]["forms"], 1)

    def test_enum_values(self):
        self.assertEqual(self.g["stats"]["enum_values"], 2)

    def test_has_attribute_edges(self):
        edges = [e for e in self.g["edges"] if e["rel"] == "HAS_ATTRIBUTE"]
        # 2 (прямые в cat_a) + 1 (в ТЧ) + 3 (в register: dim+res+attr) = 6
        self.assertEqual(len(edges), 6)
        # Проверяем что роль пробрасывается
        roles = [e["props"].get("role") for e in edges]
        self.assertIn("dimension", roles)
        self.assertIn("resource", roles)

    def test_resolves_to(self):
        # cat_a.ВидАукциона: CatalogRef → Catalog.АукВидыАукционов  ✓
        # cat_a.ТЧ.Участник: CatalogRef → Catalog.АукВидыАукционов  ✓ (тот же target)
        # register.Получатель: CatalogRef → Catalog.АукВидыАукционов ✓
        # Всё это резолвится в один и тот же узел типа,
        # и от него — одно ребро RESOLVES_TO.
        edges = [e for e in self.g["edges"] if e["rel"] == "RESOLVES_TO"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["dst"], "Catalog.АукВидыАукционов")

    def test_of_type_edges(self):
        edges = [e for e in self.g["edges"] if e["rel"] == "OF_TYPE"]
        # 6 атрибутов × 1 тип каждый = 6
        self.assertEqual(len(edges), 6)

    def test_contains(self):
        edges = [e for e in self.g["edges"] if e["rel"] == "CONTAINS"]
        # Subsystem.Аукционы → Catalog.АукАукционы, Catalog.АукВидыАукционов, Enum.Статусы
        self.assertEqual(len(edges), 3)
        src = {e["src"] for e in edges}
        self.assertEqual(src, {"Subsystem.Аукционы"})

    def test_owned_by(self):
        # cat_a имеет OWNED_BY → ChartOfCharacteristicTypes.AnyChart,
        # но такого узла нет, поэтому ребро не создаётся, идёт в unresolved
        edges = [e for e in self.g["edges"] if e["rel"] == "OWNED_BY"]
        self.assertEqual(len(edges), 0)
        self.assertIn("OWNED_BY→ChartOfCharacteristicTypes.AnyChart", self.g["unresolved"])

    def test_has_form(self):
        edges = [e for e in self.g["edges"] if e["rel"] == "HAS_FORM"]
        self.assertEqual(len(edges), 1)
        self.assertTrue(edges[0]["props"].get("is_main"))

    def test_has_value(self):
        edges = [e for e in self.g["edges"] if e["rel"] == "HAS_VALUE"]
        # Enum.Статусы имеет 2 значения
        self.assertEqual(len(edges), 2)

    def test_type_nodes_unique(self):
        # Несмотря на 6 OF_TYPE рёбер, уникальных типов должно быть:
        # CatalogRef→Catalog.АукВидыАукционов  (используется трижды)
        # String                                (используется дважды)
        # → 2 узла Type
        self.assertEqual(self.g["stats"]["type_nodes"], 2)

    def test_no_unresolved_known_refs(self):
        # Содержание подсистемы должно полностью разрешиться
        unresolved_contains = [k for k in self.g["unresolved"] if k.startswith("CONTAINS→")]
        self.assertEqual(len(unresolved_contains), 0)


# ─── Тесты walk_workspace на временной структуре ──────────────────────────

class TestWalkWorkspace(unittest.TestCase):
    def test_full_pipeline(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_xml(root, "Catalogs", "АукАукционы.xml", CATALOG_XML)
            write_xml(root, "Catalogs", "АукВидыАукционов.xml", CATALOG_TARGET_XML)
            write_xml(root, "Enums", "Статусы.xml", ENUM_XML)
            write_xml(root, "Subsystems", "Аукционы.xml", SUBSYSTEM_XML)

            from metadata_xml import walk_workspace
            objs = walk_workspace(root)
            self.assertEqual(len(objs), 4)
            names = {o.full_name_eng for o in objs}
            self.assertIn("Catalog.АукАукционы", names)
            self.assertIn("Catalog.АукВидыАукционов", names)
            self.assertIn("Enum.Статусы", names)
            self.assertIn("Subsystem.Аукционы", names)

            g = build_graph(objs)
            # Подсистема ссылается на 3 объекта — все должны разрешиться
            contains = [e for e in g["edges"] if e["rel"] == "CONTAINS"]
            self.assertEqual(len(contains), 3)
            # RESOLVES_TO от типа АукВидыАукционов → к объекту
            resolves = [e for e in g["edges"]
                        if e["rel"] == "RESOLVES_TO" and e["dst"] == "Catalog.АукВидыАукционов"]
            self.assertEqual(len(resolves), 1)


# ─── FEAT-1.1: структурные признаки ──────────────────────────────────────


class TestStructuralFlags(unittest.TestCase):
    """Признаки, от которых зависит состав стандартных реквизитов."""

    CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Catalog uuid="u1">
    <Properties>
      <Name>Плоский</Name>
      <Synonym/>
      <Hierarchical>false</Hierarchical>
      <Autonumbering>true</Autonumbering>
    </Properties>
    <ChildObjects/>
  </Catalog>
</MetaDataObject>
"""

    HIER = CATALOG.replace("<Hierarchical>false</Hierarchical>",
                           "<Hierarchical>true</Hierarchical>")

    NO_FLAGS = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Catalog uuid="u2">
    <Properties><Name>БезПризнаков</Name></Properties>
    <ChildObjects/>
  </Catalog>
</MetaDataObject>
"""

    def _parse(self, xml, name="Х.xml"):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_text(xml, encoding="utf-8")
            return mx._parse_object(p, "Catalog", "Справочник",
                                    "Справочники", name)

    def test_flat_catalog(self):
        obj = self._parse(self.CATALOG)
        self.assertEqual(obj.properties.get("Hierarchical"), "false")

    def test_hierarchical_catalog(self):
        obj = self._parse(self.HIER)
        self.assertEqual(obj.properties.get("Hierarchical"), "true")

    def test_missing_flags_is_not_an_error(self):
        obj = self._parse(self.NO_FLAGS)
        self.assertIsNotNone(obj)
        self.assertNotIn("Hierarchical", obj.properties)

    def test_flags_survive_into_graph_node(self):
        obj = self._parse(self.CATALOG)
        node = mx.build_graph([obj])["meta_nodes"][0]
        self.assertEqual(node["properties"].get("Hierarchical"), "false")

    def test_common_module_flags_not_overwritten(self):
        # У общего модуля своя ветка разбора — она возвращается раньше,
        # и структурные признаки её не затирают.
        from pathlib import Path
        import tempfile
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <CommonModule uuid="u3">
    <Properties><Name>М</Name><Server>true</Server></Properties>
  </CommonModule>
</MetaDataObject>
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "М.xml"
            p.write_text(xml, encoding="utf-8")
            obj = mx._parse_object(p, "CommonModule", "ОбщийМодуль",
                                   "ОбщиеМодули", "М.xml")
        self.assertEqual(obj.properties.get("Server"), "true")


if __name__ == "__main__":
    unittest.main(verbosity=2)
