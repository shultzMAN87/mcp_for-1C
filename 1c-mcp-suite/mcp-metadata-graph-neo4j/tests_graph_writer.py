"""
Интеграционные тесты writer'а + всех v3 Cypher-запросов на живом Neo4j.

ВАЖНО ДЛЯ БЕЗОПАСНОСТИ:
  Предыдущая версия (v1) случайно затёрла production-граф через
  clear_metadata_layer без проверки флага. Новая стратегия — три
  независимых слоя защиты:

  СЛОЙ 1: Все тестовые узлы получают дополнительный label :TestNode.
          Cleanup стирает только узлы с этим label. Production-узлы
          таким label не помечены — не пострадают.

  СЛОЙ 2: Тесты с wipe-семантикой (clear_metadata_layer) физически не
          запускаются без env NEO4J_TEST_ALLOW_WIPE=1. На дефолтном
          прогоне — unittest.SkipTest. Это защита от случайного клика.

  СЛОЙ 3: Pre-flight в setUpClass: если в Neo4j есть :MetadataObject
          без label :TestNode, требуется явное разрешение
          NEO4J_TEST_ALLOW_WRITE_PROD=1. Иначе тесты скипаются.

Запуск:
  $env:NEO4J_TEST_URL  = "http://localhost:7474"
  $env:NEO4J_TEST_USER = "neo4j"
  $env:NEO4J_TEST_PASS = "..."
  $env:NEO4J_TEST_ALLOW_WRITE_PROD = "1"   # подтверждаем: знаем что в БД есть data
  python tests_graph_writer.py -v
"""
from __future__ import annotations

import os
import time
import unittest
import uuid
from pathlib import Path
from typing import Optional

from metadata_xml import (
    walk_workspace, build_graph, MetaObject, Attribute, TabularSection,
    TypeRef, FormInfo,
)
from graph_writer import (
    Neo4j, ensure_schema, fingerprint_workspace, fingerprint_get,
    fingerprint_write, clear_metadata_layer, write_graph,
)


def _select_neo4j() -> tuple[Optional[Neo4j], Optional[object]]:
    url = os.environ.get("NEO4J_TEST_URL")
    if url:
        usr = os.environ.get("NEO4J_TEST_USER", "neo4j")
        pwd = os.environ.get("NEO4J_TEST_PASS", "neo4j")
        neo = Neo4j(url, usr, pwd, timeout=30)
        try:
            neo.wait(timeout=10)
            return neo, None
        except Exception as e:
            print(f"[WARN] NEO4J_TEST_URL задан, но недоступен: {e}")
            return None, None

    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:
        return None, None

    try:
        container = Neo4jContainer("neo4j:5-community").with_env(
            "NEO4J_AUTH", "neo4j/testpass"
        )
        container.start()
        host = container.get_container_host_ip()
        http_port = container.get_exposed_port(7474)
        neo = Neo4j(f"http://{host}:{http_port}", "neo4j", "testpass", timeout=30)
        neo.wait(timeout=60)
        return neo, container
    except Exception as e:
        print(f"[WARN] testcontainers Neo4j не запустился: {e}")
        return None, None


TEST_LABEL = "TestNode"


def _post_label_test_nodes(neo: Neo4j, run_id: str) -> int:
    """Помечает все узлы с run_id в .id дополнительным label :TestNode."""
    rows = neo.rows(
        f"MATCH (n) WHERE n.id CONTAINS $rid SET n:{TEST_LABEL} RETURN count(n) AS c",
        {"rid": run_id},
    )
    return rows[0]["c"] if rows else 0


def _cleanup_test_nodes(neo: Neo4j) -> int:
    """Удаляет ВСЕ узлы с label :TestNode."""
    rows = neo.rows(
        f"MATCH (n:{TEST_LABEL}) "
        f"WITH n, count(n) AS _ "
        f"DETACH DELETE n RETURN count(*) AS c"
    )
    return rows[0]["c"] if rows else 0


class Neo4jIntegrationBase(unittest.TestCase):
    neo: Optional[Neo4j] = None
    cleanup_handle = None
    test_run_id: str = ""

    @classmethod
    def setUpClass(cls):
        cls.neo, cls.cleanup_handle = _select_neo4j()
        if cls.neo is None:
            raise unittest.SkipTest(
                "Нет доступного Neo4j. Запусти с NEO4J_TEST_URL=... либо "
                "установи testcontainers[neo4j]."
            )

        # Pre-flight: смотрим что не на production без явного разрешения
        prod_count = cls.neo.rows(
            "MATCH (n) WHERE (n:MetadataObject OR n:Attribute OR n:Type) "
            "AND NOT 'TestNode' IN labels(n) RETURN count(n) AS c"
        )[0]["c"]
        if prod_count > 0 and os.environ.get("NEO4J_TEST_ALLOW_WRITE_PROD") != "1":
            raise unittest.SkipTest(
                f"В Neo4j {prod_count} production-узлов без label :TestNode. "
                f"Тесты безопасно изолируют свои данные label :TestNode, но "
                f"для дополнительной защиты требуется явное разрешение. "
                f"Запусти: $env:NEO4J_TEST_ALLOW_WRITE_PROD = '1'"
            )

        cls.test_run_id = f"test_{uuid.uuid4().hex[:8]}"

        # Подчищаем артефакты от прежних прогонов (например, упавших на assert)
        try:
            cleaned = _cleanup_test_nodes(cls.neo)
            if cleaned:
                print(f"[setUpClass] подчищено старых :TestNode: {cleaned}")
        except Exception as e:
            print(f"[WARN] pre-cleanup не удался: {e}")

    @classmethod
    def tearDownClass(cls):
        if cls.neo is not None:
            try:
                deleted = _cleanup_test_nodes(cls.neo)
                print(f"[tearDownClass] удалено :TestNode: {deleted}")
            except Exception as e:
                print(f"[WARN] cleanup failed: {e}")
        if cls.cleanup_handle is not None:
            try:
                cls.cleanup_handle.stop()
            except Exception:
                pass


def _build_synthetic_objects(run_id: str) -> list[MetaObject]:
    cat_main = MetaObject(
        kind_eng="Catalog", kind_ru="Справочник", kind_ru_plural="Справочники",
        name=f"{run_id}_Main",
        attributes=[
            Attribute(name="ВидРесурса",
                       types=[TypeRef("CatalogRef", f"Catalog.{run_id}_Targets")],
                       role="attribute"),
            Attribute(name="Описание",
                       types=[TypeRef("String")], role="attribute"),
        ],
        tabular_sections=[
            TabularSection(name="Строки", attributes=[
                Attribute(name="ВидРесурса",
                           types=[TypeRef("CatalogRef", f"Catalog.{run_id}_Targets")],
                           role="attribute"),
            ]),
        ],
        forms=[FormInfo(name="ФормаЭлемента", is_main=True,
                         main_kind="DefaultObjectForm")],
    )
    cat_target = MetaObject(
        kind_eng="Catalog", kind_ru="Справочник", kind_ru_plural="Справочники",
        name=f"{run_id}_Targets",
    )
    reg = MetaObject(
        kind_eng="InformationRegister", kind_ru="РегистрСведений",
        kind_ru_plural="РегистрыСведений",
        name=f"{run_id}_Reg",
        attributes=[
            Attribute(name="ВидРесурса",
                       types=[TypeRef("CatalogRef", f"Catalog.{run_id}_Targets")],
                       role="dimension", is_master=True),
            Attribute(name="Значение", types=[TypeRef("Number")], role="resource"),
            Attribute(name="Комментарий", types=[TypeRef("String")], role="attribute"),
        ],
    )
    enum = MetaObject(
        kind_eng="Enum", kind_ru="Перечисление", kind_ru_plural="Перечисления",
        name=f"{run_id}_Status",
        enum_values=[
            {"name": "Active", "synonym": "Активен"},
            {"name": "Closed", "synonym": "Закрыт"},
        ],
    )
    sub = MetaObject(
        kind_eng="Subsystem", kind_ru="Подсистема", kind_ru_plural="Подсистемы",
        name=f"{run_id}_Subsystem",
        contained=[
            f"Catalog.{run_id}_Main",
            f"InformationRegister.{run_id}_Reg",
        ],
    )
    return [cat_main, cat_target, reg, enum, sub]


def _write_test_graph(neo: Neo4j, run_id: str) -> dict:
    objects = _build_synthetic_objects(run_id)
    graph = build_graph(objects)
    summary = write_graph(neo, graph, config_name=f"TestCfg_{run_id}")
    n = _post_label_test_nodes(neo, run_id)
    print(f"[_write_test_graph] помечено :TestNode у {n} узлов")
    return summary


class TestSchemaAndWriter(Neo4jIntegrationBase):
    def setUp(self):
        ensure_schema(self.neo)
        self.summary = _write_test_graph(self.neo, self.test_run_id)

    def test_meta_nodes_written(self):
        rows = self.neo.rows("MATCH (n:MetadataObject:TestNode) RETURN count(n) AS c")
        self.assertEqual(rows[0]["c"], 5)

    def test_attribute_nodes_written(self):
        rows = self.neo.rows("MATCH (n:Attribute:TestNode) RETURN count(n) AS c")
        self.assertEqual(rows[0]["c"], 6)

    def test_role_persisted_on_edge(self):
        dims = self.neo.rows(
            "MATCH (m:MetadataObject:TestNode)-[r:HAS_ATTRIBUTE {role: 'dimension'}]->(a:TestNode) "
            "RETURN count(r) AS c"
        )
        self.assertEqual(dims[0]["c"], 1)
        resources = self.neo.rows(
            "MATCH (m:MetadataObject:TestNode)-[r:HAS_ATTRIBUTE {role: 'resource'}]->(a:TestNode) "
            "RETURN count(r) AS c"
        )
        self.assertEqual(resources[0]["c"], 1)

    def test_resolves_to_built(self):
        target_id = f"Catalog.{self.test_run_id}_Targets"
        rows = self.neo.rows(
            "MATCH (t:Type:TestNode)-[:RESOLVES_TO]->(m:MetadataObject:TestNode {id: $tid}) "
            "RETURN count(t) AS c",
            {"tid": target_id},
        )
        self.assertEqual(rows[0]["c"], 1)

    def test_subsystem_contains(self):
        rows = self.neo.rows(
            "MATCH (s:MetadataObject:TestNode {kind_eng:'Subsystem'})-[:CONTAINS]->(m:TestNode) "
            "RETURN count(m) AS c"
        )
        self.assertEqual(rows[0]["c"], 2)

    @unittest.skipUnless(
        os.environ.get("NEO4J_TEST_ALLOW_WIPE") == "1",
        "clear_metadata_layer стирает production-данные. "
        "NEO4J_TEST_ALLOW_WIPE=1 для проверки."
    )
    def test_clear_layer_idempotent(self):
        clear_metadata_layer(self.neo)
        rows = self.neo.rows("MATCH (n:Attribute:TestNode) RETURN count(n) AS c")
        self.assertEqual(rows[0]["c"], 0)
        _write_test_graph(self.neo, self.test_run_id)
        rows = self.neo.rows("MATCH (n:Attribute:TestNode) RETURN count(n) AS c")
        self.assertEqual(rows[0]["c"], 6)


class TestV3ToolsQueries(Neo4jIntegrationBase):
    """Cypher-запросы из v3-tools. Ограничены :TestNode — не видят production."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_schema(cls.neo)
        cls.summary = _write_test_graph(cls.neo, cls.test_run_id)
        cls.target_id = f"Catalog.{cls.test_run_id}_Targets"
        cls.main_id   = f"Catalog.{cls.test_run_id}_Main"
        cls.reg_id    = f"InformationRegister.{cls.test_run_id}_Reg"

    def test_attribute_type_query(self):
        rows = self.neo.rows(
            """
            MATCH (m:MetadataObject:TestNode)
            WHERE m.full_name_eng = $fn OR m.full_name_ru = $fn OR m.id = $fn
            RETURN m.id AS id LIMIT 1
            """,
            {"fn": self.main_id},
        )
        self.assertEqual(len(rows), 1)
        attr_rows = self.neo.rows(
            """
            MATCH (m:MetadataObject:TestNode {id: $oid})-[:HAS_ATTRIBUTE]->(a:Attribute:TestNode)
            WHERE a.name = $an
            RETURN a.id AS aid, a.role AS role LIMIT 1
            """,
            {"oid": self.main_id, "an": "ВидРесурса"},
        )
        self.assertEqual(len(attr_rows), 1)
        type_rows = self.neo.rows(
            """
            MATCH (a:Attribute:TestNode {id: $aid})-[:OF_TYPE]->(t:Type:TestNode)
            OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject:TestNode)
            RETURN t.kind AS kind, t.target AS target, o.full_name_eng AS resolved
            """,
            {"aid": attr_rows[0]["aid"]},
        )
        self.assertEqual(len(type_rows), 1)
        self.assertEqual(type_rows[0]["kind"], "CatalogRef")
        self.assertEqual(type_rows[0]["resolved"], self.target_id)

    def test_find_link_path_query(self):
        rows = self.neo.rows(
            """
            MATCH (a:MetadataObject:TestNode), (b:MetadataObject:TestNode)
            WHERE (a.full_name_eng = $a OR a.id = $a)
              AND (b.full_name_eng = $b OR b.id = $b)
            WITH a, b LIMIT 1
            MATCH p = shortestPath((a)-[:HAS_ATTRIBUTE|HAS_TABULAR_SECTION|OF_TYPE|RESOLVES_TO*1..4]-(b))
            RETURN length(p) AS len, [r IN relationships(p) | type(r)] AS rels
            """,
            {"a": self.main_id, "b": self.target_id},
        )
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["len"], 0)

    def test_referrers_query_DOES_NOT_RAISE(self):
        """ГЛАВНЫЙ ТЕСТ — поймал бы баг "Variable t not defined"."""
        items = self.neo.rows(
            """
            MATCH (t:Type:TestNode)-[:RESOLVES_TO]->(target:MetadataObject:TestNode {id: $tid})
            MATCH (a:Attribute:TestNode)-[:OF_TYPE]->(t)
            MATCH (parent:TestNode)-[:HAS_ATTRIBUTE]->(a)
            OPTIONAL MATCH (owner:MetadataObject:TestNode)-[:HAS_TABULAR_SECTION]->(parent)
            WITH a, parent, owner, t,
                 CASE WHEN owner IS NULL THEN parent ELSE owner END AS root
            RETURN root.full_name_eng AS owner_object,
                   root.kind_eng       AS owner_kind,
                   a.name              AS attr_name,
                   a.role              AS attr_role,
                   CASE WHEN owner IS NULL THEN null ELSE parent.name END AS in_tabular_section,
                   t.kind              AS type_kind
            ORDER BY root.full_name_eng, a.name
            """,
            {"tid": self.target_id},
        )
        self.assertEqual(len(items), 3)
        self.assertEqual({i["attr_name"] for i in items}, {"ВидРесурса"})
        self.assertIn("Строки", [i["in_tabular_section"] for i in items])

    def test_referrers_count_query(self):
        rows = self.neo.rows(
            """
            MATCH (t:Type:TestNode)-[:RESOLVES_TO]->(target:MetadataObject:TestNode {id: $tid})
            MATCH (a:Attribute:TestNode)-[:OF_TYPE]->(t)
            MATCH (parent:TestNode)-[:HAS_ATTRIBUTE]->(a)
            RETURN count(a) AS c
            """,
            {"tid": self.target_id},
        )
        self.assertEqual(rows[0]["c"], 3)

    def test_object_attributes_query(self):
        rows = self.neo.rows(
            """
            MATCH (m:MetadataObject:TestNode {id: $oid})-[:HAS_ATTRIBUTE]->(a:Attribute:TestNode)
            OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type:TestNode)
            OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject:TestNode)
            WITH a, collect({kind: t.kind, target: t.target,
                              resolved: o.full_name_eng}) AS types
            RETURN a.name AS name, a.role AS role, types ORDER BY a.name
            """,
            {"oid": self.reg_id},
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["role"] for r in rows},
                         {"dimension", "resource", "attribute"})

    def test_object_attributes_with_role_filter(self):
        rows = self.neo.rows(
            """
            MATCH (m:MetadataObject:TestNode {id: $oid})-[:HAS_ATTRIBUTE]->(a:Attribute:TestNode)
            WHERE a.role = $role
            OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type:TestNode)
            OPTIONAL MATCH (t)-[:RESOLVES_TO]->(o:MetadataObject:TestNode)
            WITH a, collect({kind: t.kind, target: t.target,
                              resolved: o.full_name_eng}) AS types
            RETURN a.name AS name, a.role AS role, types ORDER BY a.name
            """,
            {"oid": self.reg_id, "role": "dimension"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "dimension")

    def test_subsystem_tree_query(self):
        rows = self.neo.rows(
            """
            MATCH (s:MetadataObject:TestNode {kind_eng: 'Subsystem'})
            OPTIONAL MATCH (parent:MetadataObject:TestNode)-[:PARENT_OF]->(s)
            WITH s, parent WHERE parent IS NULL
            MATCH p = (s)-[:PARENT_OF*0..5]->(child:MetadataObject:TestNode)
            RETURN child.name AS name, length(p) AS depth, s.full_name_eng AS root
            """
        )
        self.assertGreaterEqual(len(rows), 1)

    def test_dead_metadata_query(self):
        rows = self.neo.rows(
            """
            MATCH (m:MetadataObject:TestNode)
            WHERE NOT EXISTS { (:MetadataObject:TestNode)-[:CONTAINS]->(m) }
              AND m.kind_eng <> 'Subsystem'
            RETURN m.full_name_eng AS full_name, m.kind_eng AS kind
            ORDER BY m.kind_eng, m.name
            """
        )
        self.assertEqual({r["kind"] for r in rows}, {"Catalog", "Enum"})

    def test_v3_stats_aggregations(self):
        for label in ("MetadataObject", "Attribute", "TabularSection",
                       "Form", "EnumValue", "Type"):
            rows = self.neo.rows(
                f"MATCH (n:{label}:TestNode) RETURN count(n) AS c"
            )
            self.assertGreaterEqual(rows[0]["c"], 0)

        rows = self.neo.rows(
            """
            MATCH (t:Type:TestNode)-[:RESOLVES_TO]->(m:MetadataObject:TestNode)
            MATCH (a:Attribute:TestNode)-[:OF_TYPE]->(t)
            WITH m, count(a) AS refs
            RETURN m.full_name_eng AS object, refs
            ORDER BY refs DESC LIMIT 10
            """
        )
        found = False
        for r in rows:
            if r["object"] == self.target_id:
                self.assertEqual(r["refs"], 3)
                found = True
                break
        self.assertTrue(found, f"target {self.target_id} не в hot_targets")


class TestNeo4jErrorHandling(Neo4jIntegrationBase):
    """Узлов не пишет — проверяет правильную обработку ошибок Neo4j."""

    def test_syntax_error_raises_runtime(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.neo.query("MATCH (x ZZZZ INVALID SYNTAX")
        self.assertIn("Neo4j", str(ctx.exception))

    def test_undefined_variable_in_with_raises(self):
        """Точная регрессия 4.6.1."""
        broken = """
        MATCH (t:Type)
        WITH t LIMIT 1
        RETURN t.kind, t.target, broken_var.id
        """
        with self.assertRaises(RuntimeError) as ctx:
            self.neo.query(broken)
        self.assertIn("broken_var", str(ctx.exception))

    def test_fingerprint_roundtrip(self):
        """Fingerprint в отдельном namespace — kind с префиксом test_."""
        kind = f"test_fp_{self.test_run_id}"
        value = f"fp_{int(time.time())}"
        fingerprint_write(self.neo, value, kind=kind)
        got = fingerprint_get(self.neo, kind=kind)
        self.assertEqual(got, value)
        self.neo.query("MATCH (n:Fingerprint {kind: $k}) DETACH DELETE n", {"k": kind})


class TestFingerprintWorkspace(unittest.TestCase):
    """Не требует Neo4j."""

    def test_empty_workspace(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fp = fingerprint_workspace(Path(tmp))
            self.assertEqual(fp,
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_changes_on_file_added(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fp_empty = fingerprint_workspace(Path(tmp))
            (Path(tmp) / "test.xml").write_text("<root/>", encoding="utf-8")
            fp_one = fingerprint_workspace(Path(tmp))
            self.assertNotEqual(fp_empty, fp_one)

    def test_stable_on_reread(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.xml").write_text("<a/>", encoding="utf-8")
            (Path(tmp) / "b.xml").write_text("<b/>", encoding="utf-8")
            fp1 = fingerprint_workspace(Path(tmp))
            fp2 = fingerprint_workspace(Path(tmp))
            self.assertEqual(fp1, fp2)


# ────────────────────────────────────────────────────────────────────────
# Слой 2 (call graph) — интеграционные тесты для 4.6.2.
# ────────────────────────────────────────────────────────────────────────


def _build_synthetic_code_graph(run_id: str) -> dict:
    """
    Минимальный code_graph для интеграционных тестов.

    Структура:
      Module "CommonModule.<run_id>_X"    (берётся из layer-1 :MetadataObject,
                                            создаётся через _seed_layer1_for_code)
        ├─ Callable :Procedure "<run_id>_Proc1"  (1 параметр)
        ├─ Callable :Function  "<run_id>_Func1"  (1 параметр)
        │     └─ CALLS → Proc1                    (рекурсия — Proc1 зовёт сам себя через CallSite)
      Plus:
        CallSite resolved=true (Proc1:10:0 → RESOLVES_TO_CALLEE Func1)
        CallSite resolved=false (Proc1:11:5 reason='unknown_module')
        OPERATES_ON Func1 → Catalog.<run_id>_T  (via=predefined_value)
        Type-узел Catalog.<run_id>_T (4.6.4) + INFERRED_TYPE Proc1.Param.p1 → Type
    """
    module_id = f"CommonModule.{run_id}_X"
    proc1_id = f"{module_id}.{run_id}_Proc1"
    func1_id = f"{module_id}.{run_id}_Func1"
    target_id = f"Catalog.{run_id}_T"

    cs_resolved_id = f"{proc1_id}:10:0"
    cs_unresolved_id = f"{proc1_id}:11:5"

    # 4.6.4: :Type-узел слоя 2 для выведенного типа параметра p1.
    # id-формат синхронизирован со слоем 1 (Type:{kind}:{target}).
    type_id = f"Type:CatalogObject:{target_id}"
    param_p1_id = f"{proc1_id}.Param.p1"

    # NB: module_nodes пустой — CommonModule создаётся отдельно в _seed_layer1_for_code
    # (как и в production: фаза 1 создаёт CommonModule, фаза 2 крепится к нему).
    return {
        "module_nodes": [],
        "callable_nodes": [
            {
                "id": proc1_id, "name": f"{run_id}_Proc1",
                "full_name": proc1_id, "module_id": module_id,
                "kind": "Procedure", "is_export": True, "directive": "",
                "line_start": 1, "line_end": 12, "source_path": "test",
            },
            {
                "id": func1_id, "name": f"{run_id}_Func1",
                "full_name": func1_id, "module_id": module_id,
                "kind": "Function", "is_export": False, "directive": "",
                "line_start": 14, "line_end": 20, "source_path": "test",
            },
        ],
        "parameter_nodes": [
            {
                "id": param_p1_id, "name": "p1", "position": 0,
                "is_by_value": False, "has_default": False, "default_value": "",
                "callable_id": proc1_id,
            },
        ],
        "callsite_nodes": [
            {
                "id": cs_resolved_id, "caller_id": proc1_id,
                "module_ref": "", "method_name": f"{run_id}_Func1",
                "line": 10, "col": 0, "resolved": True, "reason": "",
            },
            {
                "id": cs_unresolved_id, "caller_id": proc1_id,
                "module_ref": "пОбъект", "method_name": "Вставить",
                "line": 11, "col": 5, "resolved": False,
                "reason": "unknown_module",
            },
        ],
        "type_nodes": [
            {"id": type_id, "kind": "CatalogObject", "target": target_id},
        ],
        "edges": [
            {"rel": "HAS_METHOD", "src": module_id, "dst": proc1_id,
             "props": {"kind": "procedure"}},
            {"rel": "HAS_METHOD", "src": module_id, "dst": func1_id,
             "props": {"kind": "function"}},
            {"rel": "HAS_PARAM", "src": proc1_id, "dst": param_p1_id,
             "props": {"position": 0}},
            {"rel": "CALL_SITE", "src": proc1_id, "dst": cs_resolved_id, "props": {}},
            {"rel": "CALL_SITE", "src": proc1_id, "dst": cs_unresolved_id, "props": {}},
            {"rel": "CALLS", "src": proc1_id, "dst": func1_id,
             "props": {"line": 10, "callsite": cs_resolved_id}},
            {"rel": "RESOLVES_TO_CALLEE", "src": cs_resolved_id, "dst": func1_id,
             "props": {}},
            {"rel": "OPERATES_ON", "src": func1_id, "dst": target_id,
             "props": {"via": "predefined_value", "access": "read"}},
            # 4.6.4: :INFERRED_TYPE (:Parameter)-[:INFERRED_TYPE]->(:Type).
            {"rel": "INFERRED_TYPE", "src": param_p1_id, "dst": type_id,
             "props": {"confidence": 0.9, "source": "param_propagated"}},
        ],
        "stats": {},
    }


def _seed_layer1_for_code(neo: Neo4j, run_id: str) -> None:
    """
    Создаёт минимум :MetadataObject из слоя 1 для интеграционных тестов code-слоя.
    Узлы:
      :MetadataObject:CommonModule    {id: CommonModule.<run_id>_X}
      :MetadataObject:Catalog          {id: Catalog.<run_id>_T}     (для OPERATES_ON)
    """
    cm_id = f"CommonModule.{run_id}_X"
    cat_id = f"Catalog.{run_id}_T"
    neo.query(
        "MERGE (n:MetadataObject:CommonModule {id: $id}) "
        "SET n.name = $name, n.kind_eng = 'CommonModule', n:TestNode",
        {"id": cm_id, "name": f"{run_id}_X"},
    )
    neo.query(
        "MERGE (n:MetadataObject:Catalog {id: $id}) "
        "SET n.name = $name, n.kind_eng = 'Catalog', n:TestNode",
        {"id": cat_id, "name": f"{run_id}_T"},
    )


class TestCodeLayer(Neo4jIntegrationBase):
    """
    Интеграционные тесты для слоя 2 (4.6.2 — Callable / Parameter / CallSite,
    CALLS / OPERATES_ON / RESOLVES_TO_CALLEE).

    Каждый тест создаёт свой run_id, пишет slim code_graph через write_code_graph,
    проверяет инварианты, ничего больше не трогает. Cleanup в tearDownClass
    стирает всё через :TestNode (унаследовано от base).
    """

    def setUp(self):
        self.run_id = f"{self.test_run_id}_{uuid.uuid4().hex[:6]}"
        _seed_layer1_for_code(self.neo, self.run_id)
        self.code_graph = _build_synthetic_code_graph(self.run_id)
        from graph_writer import write_code_graph
        write_code_graph(self.neo, self.code_graph)
        # Помечаем всё, что создалось, как TestNode (для cleanup):
        _post_label_test_nodes(self.neo, self.run_id)

    # 1. Двойная метка :Callable:Procedure / :Callable:Function.
    def test_write_callable_double_labels(self):
        rows = self.neo.rows(
            "MATCH (c:Callable) WHERE c.id CONTAINS $rid "
            "RETURN c.id AS id, labels(c) AS labels, c.kind AS kind "
            "ORDER BY c.id",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 2)
        kinds = {r["kind"] for r in rows}
        self.assertEqual(kinds, {"Procedure", "Function"})
        for r in rows:
            # Каждый имеет :Callable + конкретную метку.
            self.assertIn("Callable", r["labels"])
            self.assertTrue("Procedure" in r["labels"] or "Function" in r["labels"])

    # 2. CallSite с RESOLVES_TO_CALLEE и CALLS.
    def test_callsite_with_resolution(self):
        # Должны быть 2 CallSite — один resolved, один нет.
        rows = self.neo.rows(
            "MATCH (cs:CallSite) WHERE cs.id CONTAINS $rid "
            "RETURN cs.resolved AS r, count(cs) AS n",
            {"rid": self.run_id},
        )
        # Сгруппируем по resolved.
        by_res = {r["r"]: r["n"] for r in rows}
        self.assertEqual(by_res.get(True, 0), 1)
        self.assertEqual(by_res.get(False, 0), 1)

        # У resolved CallSite — должно быть :RESOLVES_TO_CALLEE → Callable.
        rows = self.neo.rows(
            "MATCH (cs:CallSite)-[:RESOLVES_TO_CALLEE]->(c:Callable) "
            "WHERE cs.id CONTAINS $rid "
            "RETURN cs.id AS cs_id, c.id AS callee_id",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 1)
        # И прямое :CALLS от caller к callee тоже есть.
        rows = self.neo.rows(
            "MATCH (a:Callable)-[:CALLS]->(b:Callable) "
            "WHERE a.id CONTAINS $rid AND b.id CONTAINS $rid "
            "RETURN a.id AS src, b.id AS dst",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["src"].endswith("_Proc1"))
        self.assertTrue(rows[0]["dst"].endswith("_Func1"))

    # 3. clear_code_layer стирает только code-слой, :MetadataObject остаются.
    def test_clear_code_layer_preserves_metadata(self):
        # Перед очисткой — есть Callable + MetadataObject.
        before = self.neo.rows(
            "MATCH (n:Callable) WHERE n.id CONTAINS $rid RETURN count(n) AS n",
            {"rid": self.run_id},
        )[0]["n"]
        self.assertGreater(before, 0)

        # Запускаем clear_code_layer — должен снести только :Callable etc.
        # ВАЖНО: эта операция стирает ВСЕ :Callable в БД, не только наши тестовые.
        # На прод-данных это уберёт реальный граф. Поэтому добавим явный гарду:
        # тест работает только когда явно разрешено wipe-семантикой.
        if os.environ.get("NEO4J_TEST_ALLOW_WIPE") != "1":
            self.skipTest(
                "clear_code_layer стирает все :Callable в БД. "
                "Для запуска: $env:NEO4J_TEST_ALLOW_WIPE='1'"
            )
        from graph_writer import clear_code_layer
        deleted = clear_code_layer(self.neo)
        self.assertGreater(deleted["deleted_nodes"], 0)

        # После — Callable нет.
        after_callable = self.neo.rows(
            "MATCH (n:Callable) WHERE n.id CONTAINS $rid RETURN count(n) AS n",
            {"rid": self.run_id},
        )[0]["n"]
        self.assertEqual(after_callable, 0)

        # А наши тестовые :MetadataObject — остались (CommonModule + Catalog).
        after_meta = self.neo.rows(
            "MATCH (n:MetadataObject) WHERE n.id CONTAINS $rid RETURN count(n) AS n",
            {"rid": self.run_id},
        )[0]["n"]
        self.assertEqual(after_meta, 2, f"Expected 2 :MetadataObject test nodes, got {after_meta}")

    # 4. clear_metadata_layer стирает MetadataObject, но :Callable остаётся.
    def test_clear_metadata_layer_preserves_code(self):
        if os.environ.get("NEO4J_TEST_ALLOW_WIPE") != "1":
            self.skipTest(
                "clear_metadata_layer стирает все :MetadataObject в БД. "
                "Для запуска: $env:NEO4J_TEST_ALLOW_WIPE='1'"
            )
        from graph_writer import clear_metadata_layer
        before_callable = self.neo.rows(
            "MATCH (n:Callable) WHERE n.id CONTAINS $rid RETURN count(n) AS n",
            {"rid": self.run_id},
        )[0]["n"]
        self.assertGreater(before_callable, 0)

        clear_metadata_layer(self.neo)

        after_callable = self.neo.rows(
            "MATCH (n:Callable) WHERE n.id CONTAINS $rid RETURN count(n) AS n",
            {"rid": self.run_id},
        )[0]["n"]
        self.assertEqual(after_callable, before_callable)

    # 5. Рекурсия — Callable, вызывающий сам себя через CALLS.
    def test_calls_with_recursion(self):
        # Создаём рекурсивный кейс отдельно: одна Callable, CALLS на саму себя.
        rec_id = f"CommonModule.{self.run_id}_X.{self.run_id}_Recur"
        cs_id = f"{rec_id}:5:0"
        self.neo.query(
            "MERGE (c:Callable:Function {id: $cid}) "
            "SET c.name = $name, c.module_id = $mid, c.kind='Function', c:TestNode "
            "WITH c "
            "MERGE (cs:CallSite {id: $csid}) "
            "SET cs.caller_id = $cid, cs.method_name = $name, cs.resolved = true, cs:TestNode "
            "WITH c, cs "
            "MERGE (c)-[:CALL_SITE]->(cs) "
            "MERGE (c)-[:CALLS {line: 5}]->(c) "
            "MERGE (cs)-[:RESOLVES_TO_CALLEE]->(c)",
            {"cid": rec_id, "csid": cs_id, "name": f"{self.run_id}_Recur",
             "mid": f"CommonModule.{self.run_id}_X"},
        )
        # Проверка: shortestPath(self → self) длиной 1.
        rows = self.neo.rows(
            "MATCH p=shortestPath((c:Callable {id: $id})-[:CALLS*1..3]->(c)) "
            "RETURN length(p) AS len",
            {"id": rec_id},
        )
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["len"], 1)

    # 6. OPERATES_ON с via=predefined_value.
    def test_operates_on_with_predef(self):
        rows = self.neo.rows(
            "MATCH (c:Callable)-[r:OPERATES_ON]->(m:MetadataObject) "
            "WHERE c.id CONTAINS $rid AND r.via = 'predefined_value' "
            "RETURN c.id AS caller, m.id AS target, r.via AS via, r.access AS access",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["caller"].endswith("_Func1"))
        self.assertTrue(rows[0]["target"].endswith("_T"))
        self.assertEqual(rows[0]["via"], "predefined_value")
        self.assertEqual(rows[0]["access"], "read")

    # 8. 4.6.4: :Type-узлы слоя 2 + :INFERRED_TYPE-рёбра.
    def test_type_nodes_and_inferred_type(self):
        # :Type-узел записан, kind/target проставлены.
        rows = self.neo.rows(
            "MATCH (t:Type) WHERE t.id CONTAINS $rid "
            "RETURN t.id AS id, t.kind AS kind, t.target AS target",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 1, "ожидался ровно один :Type-узел слоя 2")
        self.assertEqual(rows[0]["kind"], "CatalogObject")
        self.assertTrue(rows[0]["target"].endswith("_T"))

        # :INFERRED_TYPE ребро (:Parameter)->(:Type) с confidence/source.
        rows = self.neo.rows(
            "MATCH (p:Parameter)-[r:INFERRED_TYPE]->(t:Type) "
            "WHERE p.id CONTAINS $rid "
            "RETURN p.id AS pid, t.id AS tid, r.confidence AS conf, r.source AS src",
            {"rid": self.run_id},
        )
        self.assertEqual(len(rows), 1, "ожидалось ровно одно :INFERRED_TYPE ребро")
        self.assertTrue(rows[0]["pid"].endswith(".Param.p1"))
        self.assertEqual(rows[0]["src"], "param_propagated")
        self.assertAlmostEqual(float(rows[0]["conf"]), 0.9, places=3)

    # 7. Бонус: отдельный namespace fingerprint'а.
    def test_fingerprint_bsl_namespace(self):
        kind_xml = f"test_xml_{self.run_id}"
        kind_bsl = f"test_bsl_{self.run_id}"
        fingerprint_write(self.neo, "fp_xml_value", kind=kind_xml)
        fingerprint_write(self.neo, "fp_bsl_value", kind=kind_bsl)
        self.assertEqual(fingerprint_get(self.neo, kind=kind_xml), "fp_xml_value")
        self.assertEqual(fingerprint_get(self.neo, kind=kind_bsl), "fp_bsl_value")
        # Cleanup отдельных fingerprint узлов.
        self.neo.query(
            "MATCH (n:Fingerprint) WHERE n.kind IN [$k1, $k2] DETACH DELETE n",
            {"k1": kind_xml, "k2": kind_bsl},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
