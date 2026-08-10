"""
Запись графа метаданных в Neo4j через HTTP /db/neo4j/tx/commit.

Без зависимости от драйвера neo4j-python — используем urllib, как соседний indexer.py.
Запись батчами через UNWIND $rows (по 500 узлов/рёбер за запрос).

Дизайн:
  ensure_schema()             — CREATE CONSTRAINT / INDEX (идемпотентно)
  fingerprint_workspace()     — sha256 от отсортированного списка (path, sha256(content))
  fingerprint_get(NEO4J)      — читает текущий fingerprint из Neo4j (None если нет)
  fingerprint_write(NEO4J, …) — обновляет fingerprint
  clear_metadata_layer()      — удаляет только слой графа метаданных
                                (узлы :MetadataObject/:Attribute/:Type/...),
                                не трогая будущий слой графа вызовов
  write_graph(NEO4J, graph)   — UNWIND-батчи всех узлов и рёбер

Совместимость:
  Оставляем существующее свойство `attributes_json` на узлах :MetadataObject
  для обратной совместимости с metadata_object_details — старые клиенты
  продолжают работать. Новые tools используют узлы :Attribute напрямую.

Сборка тестируется через testcontainers Neo4j (см. tests_graph_writer.py),
но саму запись на mock'е не имитируем — она проверяется на смоук-стеке.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# ─── Neo4j HTTP-клиент ────────────────────────────────────────────────────


class Neo4j:
    """Минимальный HTTP-клиент к Neo4j /db/neo4j/tx/commit."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def query(self, cypher: str, parameters: Optional[dict] = None) -> dict:
        payload = json.dumps({
            "statements": [{
                "statement": cypher,
                "parameters": parameters or {},
            }]
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/db/neo4j/tx/commit",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read())
        except urllib.error.URLError as e:
            log.warning("Neo4j transport error: %s; cypher head: %s",
                        e, (cypher or "")[:200].replace("\n", " "))
            raise
        errors = result.get("errors", [])
        if errors:
            # Логируем подробно перед raise, иначе writer молча падает на
            # длинных UNWIND'ах, и непонятно, какой именно батч сломался.
            log.warning("Neo4j query error: %s; cypher head: %s",
                        errors, (cypher or "")[:200].replace("\n", " "))
            raise RuntimeError(f"Neo4j: {errors}")
        return result

    def rows(self, cypher: str, parameters: Optional[dict] = None) -> list[dict]:
        r = self.query(cypher, parameters)
        cols = r["results"][0].get("columns", [])
        out = []
        for data in r["results"][0].get("data", []):
            out.append({c: data["row"][i] for i, c in enumerate(cols)})
        return out

    def wait(self, timeout: float = 120.0) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(self.url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(3)
        raise RuntimeError(f"Neo4j недоступен по {self.url} после {timeout}с")


# ─── Схема ────────────────────────────────────────────────────────────────


CONSTRAINTS = [
    # Уникальность id на каждом типе узла. Используем generic-ключ `id`.
    "CREATE CONSTRAINT meta_id IF NOT EXISTS "
    "FOR (n:MetadataObject) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT attr_id IF NOT EXISTS "
    "FOR (n:Attribute) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ts_id IF NOT EXISTS "
    "FOR (n:TabularSection) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT form_id IF NOT EXISTS "
    "FOR (n:Form) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ev_id IF NOT EXISTS "
    "FOR (n:EnumValue) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT type_id IF NOT EXISTS "
    "FOR (n:Type) REQUIRE n.id IS UNIQUE",
    # Уникальный fingerprint (одна служебная нода)
    "CREATE CONSTRAINT fp_kind IF NOT EXISTS "
    "FOR (n:Fingerprint) REQUIRE n.kind IS UNIQUE",
    # Слой 2: код. См. CODE_LAYER_LABELS ниже.
    "CREATE CONSTRAINT callable_id IF NOT EXISTS "
    "FOR (n:Callable)  REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT param_id IF NOT EXISTS "
    "FOR (n:Parameter) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT callsite_id IF NOT EXISTS "
    "FOR (n:CallSite)  REQUIRE n.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX meta_full_name_eng IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.full_name_eng)",
    "CREATE INDEX meta_full_name_ru  IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.full_name_ru)",
    "CREATE INDEX meta_name          IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.name)",
    "CREATE INDEX meta_kind_eng      IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.kind_eng)",
    "CREATE INDEX type_kind_target   IF NOT EXISTS "
    "FOR (n:Type) ON (n.kind, n.target)",
    # Слой 2.
    "CREATE INDEX callable_full_name IF NOT EXISTS "
    "FOR (n:Callable) ON (n.full_name)",
    "CREATE INDEX callable_module_id IF NOT EXISTS "
    "FOR (n:Callable) ON (n.module_id)",
    "CREATE INDEX callable_name      IF NOT EXISTS "
    "FOR (n:Callable) ON (n.name)",
]


def ensure_schema(neo: Neo4j) -> None:
    for c in CONSTRAINTS:
        try:
            neo.query(c)
        except RuntimeError as e:
            # Старые версии Neo4j отдают warning'и как errors — игнорируем
            # "EquivalentSchemaRuleAlreadyExists" и подобные.
            if "EquivalentSchemaRule" not in str(e):
                log.warning("constraint failed: %s", e)
    for i in INDEXES:
        try:
            neo.query(i)
        except RuntimeError as e:
            if "EquivalentSchemaRule" not in str(e):
                log.warning("index failed: %s", e)


# ─── Fingerprint ──────────────────────────────────────────────────────────


def _sha256_file(p: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fingerprint_workspace_files(root: Path, suffix: str) -> str:
    """
    sha256 от отсортированного списка `(relpath, sha256(content))` всех файлов
    с указанным расширением (например, `.xml` или `.bsl`).

    Используется для отдельных fingerprint'ов на слой 1 (XML) и слой 2 (BSL).
    """
    items = []
    suffix = suffix if suffix.startswith(".") else "." + suffix
    for p in sorted(root.rglob(f"*{suffix}")):
        rel = p.relative_to(root).as_posix()
        items.append(f"{rel}\t{_sha256_file(p)}")
    h = hashlib.sha256("\n".join(items).encode("utf-8"))
    return h.hexdigest()


def fingerprint_workspace(root: Path) -> str:
    """Backward-compat: sha256 от всех XML. См. fingerprint_workspace_files."""
    return fingerprint_workspace_files(root, ".xml")


def fingerprint_get(neo: Neo4j, kind: str = "metadata_xml") -> Optional[str]:
    rows = neo.rows(
        "MATCH (n:Fingerprint {kind: $kind}) RETURN n.value AS v",
        {"kind": kind},
    )
    return rows[0]["v"] if rows else None


def fingerprint_write(neo: Neo4j, value: str, kind: str = "metadata_xml") -> None:
    neo.query(
        """
        MERGE (n:Fingerprint {kind: $kind})
        SET n.value = $value, n.updated_at = timestamp()
        """,
        {"kind": kind, "value": value},
    )


# ─── Очистка слоя ─────────────────────────────────────────────────────────


META_LAYER_LABELS = (
    "MetadataObject", "Attribute", "TabularSection", "Form",
    "EnumValue", "Type",
)

# Слой 2 (call graph): :Callable + конкретная метка :Procedure/:Function,
# :Parameter, :CallSite. :MetadataObject:Module формально остаётся в слое 1
# (это структурная единица), стирается через clear_metadata_layer.
CODE_LAYER_LABELS = (
    "Callable", "Procedure", "Function", "Parameter", "CallSite",
)


def clear_metadata_layer(neo: Neo4j) -> dict:
    """
    Удаляет только узлы слоя метаданных. Узлы графа вызовов (:Procedure,
    :Function, :Parameter), а также :Fingerprint остаются.

    Возвращает {'deleted_nodes': N}.
    """
    label_match = " OR ".join(f"'{l}' IN labels(n)" for l in META_LAYER_LABELS)
    # Параметризировать имя метки нельзя — собираем строку из known-constants.
    rows = neo.rows(
        f"MATCH (n) WHERE {label_match} "
        f"WITH n, count(n) AS _ "
        f"DETACH DELETE n "
        f"RETURN count(*) AS deleted"
    )
    return {"deleted_nodes": rows[0]["deleted"] if rows else 0}


def clear_code_layer(neo: Neo4j) -> dict:
    """
    Удаляет только узлы слоя вызовов (:Callable + :Parameter + :CallSite).
    Узлы слоя 1 (:MetadataObject и потомки) НЕ затрагиваются.
    :Fingerprint НЕ затрагивается.

    ВАЖНО: :MetadataObject:Module-узлы НЕ удаляются здесь — они формально
    слой 1 (имеют label :MetadataObject), но пишутся в фазе 2. Их сносит
    clear_metadata_layer (при переиндексации XML фаза 2 пересоздаст).

    Возвращает {'deleted_nodes': N}.
    """
    label_match = " OR ".join(f"'{l}' IN labels(n)" for l in CODE_LAYER_LABELS)
    rows = neo.rows(
        f"MATCH (n) WHERE {label_match} "
        f"WITH n, count(n) AS _ "
        f"DETACH DELETE n "
        f"RETURN count(*) AS deleted"
    )
    return {"deleted_nodes": rows[0]["deleted"] if rows else 0}


# ─── Запись узлов и рёбер ─────────────────────────────────────────────────


def _chunks(seq, size):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _safe_label(s: str) -> str:
    """Очищаем kind_eng для использования как метки Neo4j (буквы/цифры/_)."""
    out = []
    for ch in s or "":
        if ch.isalnum() or ch == "_":
            out.append(ch)
    return "".join(out) or "MetadataObject"


def write_meta_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    """
    Узлы метаобъектов. Двойная метка :MetadataObject + :<KindEng> (Catalog,
    Document, ...). Дополнительная метка :KindRu (Справочник, Документ) для
    backwards compatibility с metadata_search старого формата.
    """
    # Группируем по kind_eng — для одного CALL apoc-free дин-метки в Cypher
    # нельзя. Решение: один UNWIND-запрос на kind.
    by_kind: dict[str, list[dict]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind_eng"], []).append(n)

    total = 0
    for kind_eng, group in by_kind.items():
        label_eng = _safe_label(kind_eng)
        # Метку kind_ru тоже навешиваем
        kind_ru = group[0].get("kind_ru") or ""
        label_ru = _safe_label(kind_ru)
        # Cypher: метки задаются на этапе компиляции, поэтому формируем
        # строку запроса под каждую группу.
        labels = "MetadataObject"
        if label_eng and label_eng != "MetadataObject":
            labels += f":{label_eng}"
        if label_ru and label_ru not in (label_eng, "MetadataObject"):
            labels += f":{label_ru}"

        cypher = (
            f"UNWIND $rows AS r "
            f"MERGE (n:{labels} {{id: r.id}}) "
            f"SET n.name           = r.name, "
            f"    n.synonym        = r.synonym, "
            f"    n.comment        = r.comment, "
            f"    n.uuid           = r.uuid, "
            f"    n.kind_eng       = r.kind_eng, "
            f"    n.kind_ru        = r.kind_ru, "
            f"    n.kind_ru_plural = r.kind_ru_plural, "
            f"    n.full_name_eng  = r.full_name_eng, "
            f"    n.full_name_ru   = r.full_name_ru, "
            f"    n.source_xml     = r.source_xml, "
            f"    n.properties_json = r.properties_json, "
            f"    n.attributes_json = r.attributes_json"
        )
        for chunk in _chunks(group, batch):
            rows = []
            for n in chunk:
                # attributes_json — старый формат, нужен для metadata_object_details
                # (поле сохраняем для обратной совместимости).
                attrs_compat = []
                for a in n.get("_attrs_for_compat", []):
                    attrs_compat.append({
                        "name":    a["name"],
                        "synonym": a.get("synonym", ""),
                        "type":    a.get("type_compat", ""),
                        "category": {"attribute": "Реквизиты",
                                     "dimension": "Измерения",
                                     "resource":  "Ресурсы"}.get(a.get("role"), "Реквизиты"),
                    })
                rows.append({
                    "id":             n["id"],
                    "name":           n["name"],
                    "synonym":        n.get("synonym", ""),
                    "comment":        n.get("comment", ""),
                    "uuid":           n.get("uuid", ""),
                    "kind_eng":       n["kind_eng"],
                    "kind_ru":        n.get("kind_ru", ""),
                    "kind_ru_plural": n.get("kind_ru_plural", ""),
                    "full_name_eng":  n["full_name_eng"],
                    "full_name_ru":   n["full_name_ru"],
                    "source_xml":     n.get("source_xml", ""),
                    "properties_json": json.dumps(n.get("properties", {}), ensure_ascii=False),
                    "attributes_json": json.dumps(attrs_compat, ensure_ascii=False),
                })
            neo.query(cypher, {"rows": rows})
            total += len(rows)
    return total


def write_attribute_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Attribute {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, "
        "    n.role = r.role, n.is_master = r.is_master, "
        "    n.indexing = r.indexing, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":        n["id"],
            "name":      n["name"],
            "synonym":   n.get("synonym", ""),
            "role":      n.get("role", "attribute"),
            "is_master": n.get("is_master", False),
            "indexing":  n.get("indexing", ""),
            "parent":    n["parent"],
        } for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_tabular_section_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:TabularSection {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "synonym": n.get("synonym", ""), "parent": n["parent"]}
                for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_form_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Form {id: r.id}) "
        "SET n.name = r.name, n.is_main = r.is_main, "
        "    n.main_kind = r.main_kind, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "is_main": n.get("is_main", False),
                 "main_kind": n.get("main_kind", ""),
                 "parent": n["parent"]} for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_enum_value_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:EnumValue {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "synonym": n.get("synonym", ""), "parent": n["parent"]}
                for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_type_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Type {id: r.id}) "
        "SET n.kind = r.kind, n.target = r.target"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "kind": n["kind"], "target": n.get("target")}
                for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


# Карта rel-имени → Cypher для создания ребра. APOC недоступен, поэтому
# на каждый тип ребра — свой запрос с фиксированным именем.
EDGE_QUERIES: dict[str, str] = {
    "HAS_ATTRIBUTE": (
        "UNWIND $rows AS r "
        "MATCH (a {id: r.src}), (b:Attribute {id: r.dst}) "
        "MERGE (a)-[e:HAS_ATTRIBUTE]->(b) "
        "SET e.role = r.role"
    ),
    "HAS_TABULAR_SECTION": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:TabularSection {id: r.dst}) "
        "MERGE (a)-[:HAS_TABULAR_SECTION]->(b)"
    ),
    "HAS_FORM": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:Form {id: r.dst}) "
        "MERGE (a)-[e:HAS_FORM]->(b) "
        "SET e.is_main = r.is_main, e.main_kind = r.main_kind"
    ),
    "HAS_VALUE": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:EnumValue {id: r.dst}) "
        "MERGE (a)-[:HAS_VALUE]->(b)"
    ),
    "OF_TYPE": (
        "UNWIND $rows AS r "
        "MATCH (a:Attribute {id: r.src}), (b:Type {id: r.dst}) "
        "MERGE (a)-[:OF_TYPE]->(b)"
    ),
    "RESOLVES_TO": (
        "UNWIND $rows AS r "
        "MATCH (a:Type {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:RESOLVES_TO]->(b)"
    ),
    "CONTAINS": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:CONTAINS]->(b)"
    ),
    "PARENT_OF": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:PARENT_OF]->(b)"
    ),
    "OWNED_BY": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:OWNED_BY]->(b)"
    ),
    "BASED_ON": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:BASED_ON]->(b)"
    ),
    "REGISTERS": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:REGISTERS]->(b)"
    ),
    # ─── Слой 2 (call graph) ─────────────────────────────────────────
    "HAS_METHOD": (
        "UNWIND $rows AS r "
        "MATCH (m:MetadataObject {id: r.src}), (c:Callable {id: r.dst}) "
        "MERGE (m)-[e:HAS_METHOD]->(c) "
        "SET e.kind = r.kind"  # 'procedure' | 'function'
    ),
    "HAS_PARAM": (
        "UNWIND $rows AS r "
        "MATCH (c:Callable {id: r.src}), (p:Parameter {id: r.dst}) "
        "MERGE (c)-[e:HAS_PARAM]->(p) "
        "SET e.position = r.position"
    ),
    "CALLS": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:Callable {id: r.dst}) "
        "MERGE (a)-[e:CALLS]->(b) "
        "SET e.line = r.line, e.callsite = r.callsite"
    ),
    "CALL_SITE": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:CallSite {id: r.dst}) "
        "MERGE (a)-[:CALL_SITE]->(b)"
    ),
    "RESOLVES_TO_CALLEE": (
        "UNWIND $rows AS r "
        "MATCH (a:CallSite {id: r.src}), (b:Callable {id: r.dst}) "
        "MERGE (a)-[:RESOLVES_TO_CALLEE]->(b)"
    ),
    "OPERATES_ON": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[e:OPERATES_ON]->(b) "
        "SET e.via = r.via, e.access = r.access"
    ),
    "INFERRED_TYPE": (
        # Пока не используется (задел на 4.6.4 inter-procedural).
        "UNWIND $rows AS r "
        "MATCH (a:Parameter {id: r.src}), (b:Type {id: r.dst}) "
        "MERGE (a)-[e:INFERRED_TYPE]->(b) "
        "SET e.confidence = r.confidence, e.source = r.source"
    ),
}


def write_edges(neo: Neo4j, edges: list[dict], batch: int = 500) -> dict[str, int]:
    """Запись всех рёбер. Возвращает счётчик по типам."""
    by_rel: dict[str, list[dict]] = {}
    for e in edges:
        by_rel.setdefault(e["rel"], []).append(e)

    counters: dict[str, int] = {}
    for rel, group in by_rel.items():
        cypher = EDGE_QUERIES.get(rel)
        if cypher is None:
            log.warning("Неизвестный тип ребра, пропускаем: %s (%d шт)", rel, len(group))
            continue
        for chunk in _chunks(group, batch):
            rows = []
            for e in chunk:
                row = {"src": e["src"], "dst": e["dst"]}
                row.update(e.get("props") or {})
                rows.append(row)
            neo.query(cypher, {"rows": rows})
        counters[rel] = len(group)
    return counters


# ─── Пишет конфигурационный узел ──────────────────────────────────────────


def write_configuration_node(neo: Neo4j, name: str, stats: dict) -> None:
    neo.query(
        "MERGE (c:Configuration {name: $name}) "
        "SET c.objects = $objects, c.edges = $edges, c.updated_at = timestamp()",
        {"name": name, "objects": stats["meta_objects"],
         "edges": stats["edges_total"]},
    )


# ─── Слой 2: write-функции (Module/Callable/Parameter/CallSite) ──────────


def write_module_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    """
    Узлы-модули для крепления :Callable. Две существенно разных ситуации:

    1) Form (module_role="Form"). Узел УЖЕ существует в графе 1 как
       :MetadataObject:Form с тем же id (например, Catalog.X.Form.Y). Здесь
       просто навешиваем метку :Module и SET BSL-специфичные свойства.
       `MERGE` с новой меткой :Module сломал бы constraint form_id IS UNIQUE.

    2) ObjectModule / ManagerModule (и другие будущие роли). Узлов в графе 1
       нет — создаём новые с MERGE по id и набором меток
       :MetadataObject:Module:<Role>.

    CommonModule пропускается на стороне индексера (узел из графа 1 уже
    содержит :CommonModule, нам ничего добавлять не надо).
    """
    by_role: dict[str, list[dict]] = {}
    for n in nodes:
        role = n.get("module_role", "Module")
        by_role.setdefault(role, []).append(n)

    total = 0
    for role, group in by_role.items():
        label_role = _safe_label(role)
        if role == "Form":
            # Узел уже существует с метками :MetadataObject:Form (id уникален
            # по constraint form_id). MATCH по :Form гарантирует, что мы
            # обновляем именно тот узел, а не создаём дубль.
            cypher = (
                "UNWIND $rows AS r "
                "MATCH (n:Form {id: r.id}) "
                "SET n:Module, "
                "    n.module_role        = r.module_role, "
                "    n.parent_metadata_id = r.parent_metadata_id, "
                "    n.source_path        = r.source_path, "
                "    n.is_server          = r.is_server, "
                "    n.is_client          = r.is_client"
                # name / kind_eng / full_name_eng не трогаем — фаза 1 их уже
                # задала. SET name → пустой бы стёр осмысленное «ФормаЭлемента».
            )
        else:
            # Новый узел — :MetadataObject:Module:<Role>.
            labels = "MetadataObject:Module"
            if label_role and label_role != "Module":
                labels += f":{label_role}"
            cypher = (
                f"UNWIND $rows AS r "
                f"MERGE (n:{labels} {{id: r.id}}) "
                f"SET n.name             = r.name, "
                f"    n.kind_eng         = r.kind_eng, "
                f"    n.module_role      = r.module_role, "
                f"    n.parent_metadata_id = r.parent_metadata_id, "
                f"    n.source_path      = r.source_path, "
                f"    n.is_server        = r.is_server, "
                f"    n.is_client        = r.is_client, "
                f"    n.full_name_eng    = r.full_name_eng"
            )

        for chunk in _chunks(group, batch):
            rows = []
            for n in chunk:
                rows.append({
                    "id":                  n["id"],
                    "name":                n["name"],
                    "kind_eng":            n.get("kind_eng", "Module"),
                    "module_role":         n.get("module_role", "Module"),
                    "parent_metadata_id":  n.get("parent_metadata_id"),
                    "source_path":         n.get("source_path", ""),
                    "is_server":           bool(n.get("is_server", False)),
                    "is_client":           bool(n.get("is_client", False)),
                    "full_name_eng":       n.get("full_name_eng", n["id"]),
                })
            neo.query(cypher, {"rows": rows})
            total += len(rows)
    return total


def write_callable_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    """
    :Callable-узлы. Двойная метка :Callable:Procedure / :Callable:Function
    задаётся через поле `kind` ('Procedure' | 'Function').
    """
    by_kind: dict[str, list[dict]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind"], []).append(n)

    total = 0
    for kind, group in by_kind.items():
        label = _safe_label(kind)  # "Procedure" | "Function"
        labels = "Callable"
        if label and label != "Callable":
            labels += f":{label}"
        cypher = (
            f"UNWIND $rows AS r "
            f"MERGE (n:{labels} {{id: r.id}}) "
            f"SET n.name        = r.name, "
            f"    n.full_name   = r.full_name, "
            f"    n.module_id   = r.module_id, "
            f"    n.kind        = r.kind, "
            f"    n.is_export   = r.is_export, "
            f"    n.directive   = r.directive, "
            f"    n.line_start  = r.line_start, "
            f"    n.line_end    = r.line_end, "
            f"    n.source_path = r.source_path"
        )
        for chunk in _chunks(group, batch):
            rows = [{
                "id":          n["id"],
                "name":        n["name"],
                "full_name":   n.get("full_name", n["id"]),
                "module_id":   n["module_id"],
                "kind":        n["kind"],
                "is_export":   bool(n.get("is_export", False)),
                "directive":   n.get("directive", ""),
                "line_start":  int(n.get("line_start", 0)),
                "line_end":    int(n.get("line_end", 0)),
                "source_path": n.get("source_path", ""),
            } for n in chunk]
            neo.query(cypher, {"rows": rows})
            total += len(rows)
    return total


def write_parameter_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Parameter {id: r.id}) "
        "SET n.name           = r.name, "
        "    n.position       = r.position, "
        "    n.is_by_value    = r.is_by_value, "
        "    n.has_default    = r.has_default, "
        "    n.default_value  = r.default_value, "
        "    n.callable_id    = r.callable_id"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":            n["id"],
            "name":          n["name"],
            "position":      int(n.get("position", 0)),
            "is_by_value":   bool(n.get("is_by_value", False)),
            "has_default":   bool(n.get("has_default", False)),
            "default_value": n.get("default_value", ""),
            "callable_id":   n["callable_id"],
        } for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_callsite_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    """
    :CallSite-узлы.

    `resolved` (bool) и `reason` (текст) проставляются резолвером. Если
    callsite разрешён в callee, рёбра :CALLS и :RESOLVES_TO_CALLEE пишутся
    отдельно через write_edges (см. EDGE_QUERIES).
    """
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:CallSite {id: r.id}) "
        "SET n.caller_id   = r.caller_id, "
        "    n.module_ref  = r.module_ref, "
        "    n.method_name = r.method_name, "
        "    n.line        = r.line, "
        "    n.col         = r.col, "
        "    n.resolved    = r.resolved, "
        "    n.reason      = r.reason"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":          n["id"],
            "caller_id":   n["caller_id"],
            "module_ref":  n.get("module_ref", ""),
            "method_name": n["method_name"],
            "line":        int(n.get("line", 0)),
            "col":         int(n.get("col", 0)),
            "resolved":    bool(n.get("resolved", False)),
            "reason":      n.get("reason", ""),
        } for n in chunk]
        neo.query(cypher, {"rows": rows})
        total += len(rows)
    return total


def write_code_graph(neo: Neo4j, code_graph: dict) -> dict:
    """
    Пишет полный слой 2 (call graph) из bsl_resolver.build_call_graph().

    Ожидаемый формат `code_graph`:
      {
        "module_nodes":   [...],   # :MetadataObject:Module узлы
        "callable_nodes": [...],   # :Callable узлы
        "parameter_nodes": [...],  # :Parameter узлы
        "callsite_nodes": [...],   # :CallSite узлы
        "type_nodes":     [...],   # :Type узлы слоя 2 (4.6.4) — MERGE по id,
                                   #   переиспользуют :Type слоя 1 где совпадает id
        "edges":          [...],   # рёбра с rel ∈ HAS_METHOD, HAS_PARAM, CALLS,
                                   #   ..., INFERRED_TYPE (4.6.4)
        "stats":          {...},
      }

    Строгий порядок: сначала Module-узлы (т.к. :HAS_METHOD ссылается на них),
    затем Callable, Parameter, CallSite, Type, потом всё остальное через
    write_edges (включая :INFERRED_TYPE — ему нужны и :Parameter, и :Type).
    """
    ensure_schema(neo)

    n_module    = write_module_nodes(neo, code_graph.get("module_nodes", []))
    n_callable  = write_callable_nodes(neo, code_graph.get("callable_nodes", []))
    n_parameter = write_parameter_nodes(neo, code_graph.get("parameter_nodes", []))
    n_callsite  = write_callsite_nodes(neo, code_graph.get("callsite_nodes", []))
    # 4.6.4: :Type-узлы слоя 2. MERGE по id — если узел уже есть из слоя 1
    # (XML-фаза пишет ссылочные типы реквизитов), он переиспользуется, а не
    # дублируется. Недостающие типы (например CatalogObject, которого слой 1
    # мог не писать) — досоздаются. Должны быть записаны ДО write_edges, т.к.
    # :INFERRED_TYPE матчит (:Parameter)-(:Type).
    n_type      = write_type_nodes(neo, code_graph.get("type_nodes", []))

    edge_counters = write_edges(neo, code_graph.get("edges", []))

    return {
        "nodes_written": {
            "Module":    n_module,
            "Callable":  n_callable,
            "Parameter": n_parameter,
            "CallSite":  n_callsite,
            "Type":      n_type,
        },
        "edges_written": edge_counters,
        "stats": code_graph.get("stats", {}),
    }


# ─── Главная функция записи ───────────────────────────────────────────────


def write_graph(neo: Neo4j, graph: dict, config_name: str = "Конфигурация",
                build_compat_attrs: bool = True) -> dict:
    """
    Пишет полный граф из build_graph() в Neo4j.

    Если build_compat_attrs=True, attribute_nodes используются для построения
    attributes_json на :MetadataObject (обратная совместимость с metadata_object_details).
    """
    stats = graph["stats"]
    log.info("write_graph: %d meta + %d attrs + %d ts + %d forms + %d ev + %d types; %d edges",
             stats["meta_objects"], stats["attributes"], stats["tabular_sections"],
             stats["forms"], stats["enum_values"], stats["type_nodes"], stats["edges_total"])

    # Готовим compat-атрибуты на узлах метаобъектов
    if build_compat_attrs:
        # Соберём строку типа для compat-формата (как в старом indexer.py).
        # При composite — соединяем через '; '.
        type_kind_to_compat = {  # обратный маппинг
            "CatalogRef":                  "СправочникСсылка",
            "DocumentRef":                 "ДокументСсылка",
            "EnumRef":                     "ПеречислениеСсылка",
            "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
            "ChartOfAccountsRef":          "ПланСчетовСсылка",
            "ChartOfCalculationTypesRef":  "ПланВидовРасчетаСсылка",
            "BusinessProcessRef":          "БизнесПроцессСсылка",
            "TaskRef":                     "ЗадачаСсылка",
            "ExchangePlanRef":             "ПланОбменаСсылка",
            "DocumentJournalRef":          "ЖурналДокументовСсылка",
            "CatalogObject":               "СправочникОбъект",
            "DocumentObject":              "ДокументОбъект",
            "String": "Строка", "Number": "Число", "Date": "Дата",
            "Boolean": "Булево", "UUID": "УникальныйИдентификатор",
            "ValueStorage": "ХранилищеЗначения",
            "Reference": "Ссылка", "Unknown": "",
        }
        type_node_by_id = {t["id"]: t for t in graph["type_nodes"]}
        # of_type для каждого attr
        of_type_by_attr: dict[str, list[str]] = {}
        for e in graph["edges"]:
            if e["rel"] == "OF_TYPE":
                of_type_by_attr.setdefault(e["src"], []).append(e["dst"])
        # компиляция compat-строки типа
        def compat_type(attr_id: str) -> str:
            type_ids = of_type_by_attr.get(attr_id, [])
            parts = []
            for tid in type_ids:
                t = type_node_by_id.get(tid)
                if not t: continue
                ru = type_kind_to_compat.get(t["kind"], t["kind"])
                if t.get("target"):
                    # "Catalog.X" → "Х" (имя без префикса)
                    short = t["target"].split(".", 1)[-1]
                    parts.append(f"{ru}.{short}" if ru else short)
                else:
                    parts.append(ru)
            return "; ".join([p for p in parts if p])
        # Прицепим compat-атрибуты к meta_nodes
        attr_by_parent: dict[str, list[dict]] = {}
        for a in graph["attr_nodes"]:
            ac = dict(a)
            ac["type_compat"] = compat_type(a["id"])
            attr_by_parent.setdefault(a["parent"], []).append(ac)
        # Сольём ТЧ-реквизиты в attributes_json родительского объекта
        # (старый indexer хранил их в tabular_sections_json, но это для нас сейчас
        # лишняя сложность — оставим _attrs_for_compat пустым для родителей ТЧ,
        # т.е. для самих TS-узлов attributes_json не сохраним. metadata_object_details
        # их и не показывает в attributes-блоке).
        for n in graph["meta_nodes"]:
            attrs = attr_by_parent.get(n["id"], [])
            n["_attrs_for_compat"] = [a for a in attrs if a.get("role") != "_internal"]

    ensure_schema(neo)

    write_type_nodes(neo, graph["type_nodes"])
    write_meta_nodes(neo, graph["meta_nodes"])
    write_attribute_nodes(neo, graph["attr_nodes"])
    write_tabular_section_nodes(neo, graph["ts_nodes"])
    write_form_nodes(neo, graph["form_nodes"])
    write_enum_value_nodes(neo, graph["enum_value_nodes"])

    edge_counters = write_edges(neo, graph["edges"])

    write_configuration_node(neo, config_name, stats)

    return {
        "nodes_written": {
            "MetadataObject": stats["meta_objects"],
            "Attribute":      stats["attributes"],
            "TabularSection": stats["tabular_sections"],
            "Form":           stats["forms"],
            "EnumValue":      stats["enum_values"],
            "Type":           stats["type_nodes"],
        },
        "edges_written":   edge_counters,
        "stats":           stats,
    }
