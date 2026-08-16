"""
MCP-сервер: Конструктор запросов 1С
=====================================
Строит и проверяет запросы на языке 1С по реальным метаданным конфигурации
из Neo4j.

Инструменты:
  - query_build       — построить запрос по описанию задачи
  - query_join_hint   — подсказать как соединить две таблицы
  - query_fields      — поля таблицы (или колонки временной таблицы пакета)
  - query_validate    — проверить имена таблиц и полей в запросе
  - query_optimize    — предложить оптимизации

Зависимости:
  - Neo4j с проиндексированными метаданными (mcp-metadata-graph)

───────────────────────────────────────────────────────────────────────────
FIX-6 (Заход 3). Всё, что этот сервер спрашивал у графа, возвращало null.

Индексатор пишет у :MetadataObject свойства `id` / `full_name_eng` /
`full_name_ru` / `kind_eng` / `kind_ru`, а тип реквизита хранит отдельным
узлом :Type через `-[:OF_TYPE]->`. Сервер же спрашивал `o.full_name`,
`o.kind` и `a.type` — таких свойств в графе нет ни у одного узла.

Последствия были не «часть данных теряется», а «данных нет»:
`_get_object_info` не находил ничего никогда, поэтому query_fields,
query_join_hint и query_build отвечали «объект не найден» на любой вход, а
query_validate проверял только существование таблицы и ошибочно ругался на
любую табличную часть в трёхчастном имени.

Схема выровнена по graph_writer.py. Чтобы такое не повторилось молча,
доступ к графу собран в один класс Neo4jProvider: он один знает имена
свойств, и он же — единственное, что придётся править при следующем
изменении схемы.
───────────────────────────────────────────────────────────────────────────
"""

import json
import logging
import os
import re
import sys
import base64
import urllib.request
import urllib.error
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# FIX-3: три состояния графа вместо одного «Neo4j недоступен». В образе все
# модули лежат плоско в /app, при запуске из репозитория — в соседнем каталоге.
try:
    # B-1: после перехода на общую точку сам graph_state()/graph_error()
    # здесь больше не зовутся — их вызывает make_guard внутри себя.
    from graph_state import make_guard
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(
        0, str(Path(__file__).resolve().parent.parent / "mcp-metadata-graph"))
    from graph_state import make_guard

# OBS-1: единый словарь отказа. Тот же приём с путём, что и у graph_state.
try:
    from refusal import install_answerable_field
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import install_answerable_field

from query_check import (AttrInfo, MetadataProvider, ObjectInfo, VIRTUAL_TABLES,
                         trim_fields_payload,
                         check_query, standard_fields_for, temp_table_columns)
from query_optimize_rules import analyze
from query_parser import TABLE_PREFIXES

mcp = FastMCP("1C Query Builder")

# OBS-1. Отказы графа этот сервер уже отдавал в общем формате через
# graph_error() — не хватало симметрии: на удачных ответах поля не было,
# и проверка на него получала null там, где всё хорошо (урок FIX-19).
install_answerable_field(mcp)

logger = logging.getLogger(__name__)

NEO4J_URL = os.environ.get("NEO4J_URL", "http://neo4j:7474")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")

# SEC-2: дефолта у пароля больше нет. Раньше запуск мимо docker-compose
# молча уходил на "password1c" и, если такая база где-то существовала,
# сервер работал не с той Neo4j.
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_PASS")
if not NEO4J_PASS:
    raise SystemExit(
        "NEO4J_PASSWORD не задан — сервер не стартует (SEC-2).\n"
        "Задайте пароль в .env; дефолтного значения больше нет."
    )


# ─── Neo4j клиент ─────────────────────────────────────────────────────────

# B-2. Таймаут запроса к Neo4j.
#
# FAIL-1 опустил таймауты platform-help до четырёх секунд с обоснованием:
# локальный контейнер в docker-сети либо отвечает за доли секунды, либо не
# отвечает вовсе. Здесь оно применимо не целиком — тяжёлые запросы к графу
# идут секундами (metadata_stats замерен на 2,1 с), и резать их таймаут
# опасно: на большем графе или под нагрузкой сломается то, что работает.
#
# Поэтому таймаута два. Рабочие запросы остаются на десяти секундах,
# короткий берёт только проба живости — ей отвечает count(*) с меткой, и
# если он не уложился в три секунды, база всё равно непригодна.
NEO4J_TIMEOUT_SEC = int(os.environ.get("NEO4J_HTTP_TIMEOUT_SEC", "10"))
NEO4J_PROBE_TIMEOUT_SEC = int(os.environ.get("NEO4J_PROBE_TIMEOUT_SEC", "3"))


def _neo4j_query(cypher, parameters=None, timeout=None):
    auth = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASS}".encode()).decode()
    payload = json.dumps({
        "statements": [{
            "statement": cypher,
            "parameters": parameters or {},
        }]
    }).encode()
    req = urllib.request.Request(
        f"{NEO4J_URL}/db/neo4j/tx/commit",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    with urllib.request.urlopen(
            req, timeout=timeout or NEO4J_TIMEOUT_SEC) as resp:
        result = json.loads(resp.read())
    errors = result.get("errors", [])
    if errors:
        raise RuntimeError(f"Neo4j: {errors}")
    return result


def _neo4j_rows(cypher, params=None):
    result = _neo4j_query(cypher, params)
    columns = result["results"][0].get("columns", [])
    rows = []
    for data in result["results"][0].get("data", []):
        row = {}
        for i, col in enumerate(columns):
            row[col] = data["row"][i]
        rows.append(row)
    return rows


# B-1. Здесь лежала копия тела make_guard(), слово в слово. Из-за неё
# правка общей точки этот сервер бы не покрыла — общая точка была
# написана, а один из двух её потребителей ею не пользовался.
#
# Теперь guard берётся оттуда же, откуда у metadata-graph, и вместе с ним
# приезжает кеш вердикта «Neo4j недоступна»: без него каждый вызов
# query_fields / query_validate / query_build стоил четырёх секунд, пока
# база лежит.
def _neo4j_probe(cypher, parameters=None):
    """Проба живости: тот же запрос, но с коротким таймаутом (B-2)."""
    return _neo4j_query(cypher, parameters, timeout=NEO4J_PROBE_TIMEOUT_SEC)


_guard = make_guard(_neo4j_probe)


# ─── Провайдер метаданных поверх графа ────────────────────────────────────

class Neo4jProvider(MetadataProvider):
    """Единственное место, которое знает имена свойств узлов графа.

    Кэш живёт ровно один вызов инструмента: инкрементальная индексация
    (metadata_upsert_file) может поменять граф между вызовами, а внутри
    одной проверки запроса один и тот же справочник спрашивают до десятка раз.
    """

    def __init__(self):
        self._by_id: dict[str, ObjectInfo | None] = {}
        self._by_kind: dict[tuple, ObjectInfo | None] = {}

    # ─ низкоуровневые запросы ─

    @staticmethod
    def _load(where: str, params: dict) -> ObjectInfo | None:
        rows = _neo4j_rows(
            f"MATCH (o:MetadataObject) WHERE {where} "
            "RETURN o.id AS id, o.name AS name, o.kind_ru AS kind_ru, "
            "       o.kind_eng AS kind_eng, o.synonym AS synonym, "
            "       o.properties_json AS properties_json, "
            "       exists((o)-[:OWNED_BY]->()) AS has_owner LIMIT 1",
            params)
        if not rows:
            return None
        r = rows[0]
        # FEAT-1.1: структурные признаки. Отсутствие или битый JSON — не
        # ошибка: граф мог быть построен индексатором до Захода 3, и тогда
        # проверка стандартных реквизитов работает как раньше.
        try:
            properties = json.loads(r.get("properties_json") or "{}")
        except (TypeError, ValueError):
            properties = {}
        obj = ObjectInfo(id=r["id"], name=r["name"],
                         kind_ru=r.get("kind_ru") or r.get("kind_eng") or "",
                         synonym=r.get("synonym") or "",
                         properties=properties if isinstance(properties, dict) else {},
                         has_owner=bool(r.get("has_owner")))
        obj.attributes = Neo4jProvider._attributes(obj.id)
        obj.tabular_sections = Neo4jProvider._tabular_sections(obj.id)
        return obj

    @staticmethod
    def _attributes(obj_id: str) -> list:
        rows = _neo4j_rows(
            "MATCH (o:MetadataObject {id: $id})-[:HAS_ATTRIBUTE]->(a:Attribute) "
            "OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type) "
            "RETURN a.name AS name, a.role AS role, a.synonym AS synonym, "
            "       collect({kind: t.kind, target: t.target}) AS types "
            "ORDER BY a.name",
            {"id": obj_id})
        return [_attr_from_row(r) for r in rows]

    @staticmethod
    def _tabular_sections(obj_id: str) -> dict:
        rows = _neo4j_rows(
            "MATCH (o:MetadataObject {id: $id})"
            "-[:HAS_TABULAR_SECTION]->(ts:TabularSection) "
            "OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(a:Attribute) "
            "OPTIONAL MATCH (a)-[:OF_TYPE]->(t:Type) "
            "RETURN ts.name AS ts_name, a.name AS name, a.role AS role, "
            "       a.synonym AS synonym, "
            "       collect({kind: t.kind, target: t.target}) AS types "
            "ORDER BY ts.name, a.name",
            {"id": obj_id})
        out: dict[str, list] = {}
        for r in rows:
            attrs = out.setdefault(r["ts_name"], [])
            if r.get("name"):
                attrs.append(_attr_from_row(r))
        return out

    # ─ интерфейс MetadataProvider ─

    def get(self, kind_ru: str, name: str):
        key = (kind_ru.lower(), name.lower())
        if key not in self._by_kind:
            obj = self._load(
                "(toLower(o.kind_ru) = $kind OR toLower(o.kind_eng) = $kind) "
                "AND toLower(o.name) = $name",
                {"kind": kind_ru.lower(), "name": name.lower()})
            self._by_kind[key] = obj
            if obj:
                self._by_id[obj.id] = obj
        return self._by_kind[key]

    def get_by_id(self, full_name_eng: str):
        if full_name_eng not in self._by_id:
            self._by_id[full_name_eng] = self._load(
                "o.id = $v OR o.full_name_eng = $v OR o.full_name_ru = $v",
                {"v": full_name_eng})
        return self._by_id[full_name_eng]

    def find(self, name: str):
        """Свободный поиск по имени — для инструментов, где вид не указан."""
        rows = self.search(name, limit=1)
        return self.get_by_id(rows[0]["id"]) if rows else None

    def search(self, name: str, limit: int = 10, kind_ru: str = "") -> list:
        cypher = (
            "MATCH (o:MetadataObject) "
            "WHERE (toLower(o.name) CONTAINS $q OR toLower(o.synonym) CONTAINS $q) "
            + ("AND toLower(o.kind_ru) = $kind " if kind_ru else "")
            + "RETURN o.id AS id, o.name AS name, o.kind_ru AS kind_ru, "
              "       o.synonym AS synonym "
              "ORDER BY size(o.name) LIMIT $lim"
        )
        params = {"q": name.lower(), "lim": limit}
        if kind_ru:
            params["kind"] = kind_ru.lower()
        return _neo4j_rows(cypher, params)

    def suggest(self, name: str, limit: int = 3) -> list:
        """
        Похожие имена для ответа «объект не найден».

        FIX-21. Сюда приходит имя ровно в том виде, в каком его написал
        агент, — то есть чаще всего с префиксом вида: `Справочник.Номенклат`.
        А `search` ищет подстроку в `o.name`, где префикса нет никогда:
        в графе лежит `Номенклатура`, а не `Справочник.Номенклатура`.

        Значит `CONTAINS "справочник.номенклат"` не совпадал НИ С ЧЕМ, и
        подсказки не работали вовсе — ни на опечатке, ни на чём-либо ещё.
        Наружу это выходило пустым списком, то есть выглядело как «похожих
        имён нет», а не как «искали не то».

        Дефект нашёлся примером qb-005 (опечатка в настоящем имени) на
        следующий же день после того, как пример появился, — до этого
        датасет проверял только заведомо бессмысленное имя, где пустой
        ответ законен, и различить эти два случая было нечем.

        Если вид указан, ищем ещё и в его пределах: `Справочник.Заказ`
        должен подсказывать справочники, а не документы.
        """
        head, _, tail = name.partition(".")
        kind_ru = ""
        if tail:
            # TABLE_PREFIXES отдаёт вид в каноническом написании
            # («СПРАВОЧНИК» → «Справочник»), а не то, как его набрал агент.
            kind_ru = TABLE_PREFIXES.get(head.upper(), "")
            bare = tail.split(".")[0]
        else:
            bare = name

        rows = self.search(bare, limit, kind_ru=kind_ru) if kind_ru else []
        if not rows:
            # Вид не опознан или в его пределах пусто — ищем по всей базе.
            rows = self.search(bare, limit)
        return [f"{r['kind_ru']}.{r['name']}" for r in rows]


def _attr_from_row(r: dict) -> AttrInfo:
    types = [(t.get("kind"), t.get("target"))
             for t in (r.get("types") or []) if t and t.get("kind")]
    return AttrInfo(name=r["name"], role=r.get("role") or "attribute",
                    types=types, synonym=r.get("synonym") or "")


def _query_table_name(obj: ObjectInfo) -> str:
    """Имя таблицы в языке запросов: Справочник.Номенклатура."""
    return f"{obj.kind_ru}.{obj.name}"


def _type_text(attr: AttrInfo) -> str:
    return ", ".join(target or kind for kind, target in attr.types)


# ─── Инструменты ──────────────────────────────────────────────────────────

@mcp.tool()
def query_fields(object_name: str, query_text: str = "",
                 attributes_limit: int = 50, attributes_offset: int = 0,
                 tabular_section: str = "") -> str:
    """
    Получить доступные поля таблицы для использования в запросе:
    реквизиты, стандартные реквизиты, табличные части, виртуальные таблицы.

    Параметры:
      object_name       — имя объекта ("Справочник.Номенклатура" или
                          "Номенклатура"), либо имя временной таблицы, если
                          передан query_text
      query_text        — (опционально) текст пакета запросов. Если
                          object_name — временная таблица, объявленная в нём
                          через ПОМЕСТИТЬ, вернётся состав её колонок.
      attributes_limit  — сколько реквизитов вернуть (по умолчанию 50,
                          0 — все). Ответ сообщает общее число и смещение
                          для следующей страницы.
      attributes_offset — смещение для постраничного чтения реквизитов
      tabular_section   — развернуть состав одной табличной части. По
                          умолчанию ТЧ отдаются списком имён с числом
                          реквизитов: на документах ERP их состав — главный
                          источник разрастания ответа.
    """
    # FEAT-2: временная таблица ищется до похода в граф — её в графе нет.
    if query_text:
        for name, cols in temp_table_columns(query_text).items():
            if name.lower() == object_name.lower():
                return json.dumps({
                    "table_name": name,
                    "kind": "временная таблица",
                    "columns_known": cols is not None,
                    "columns": cols or [],
                    "note": ("Состав колонок выведен из секции ВЫБРАТЬ запроса, "
                             "который её создаёт." if cols is not None else
                             "Не у всех колонок есть имя (нет КАК либо "
                             "используется *) — состав определить нельзя."),
                }, ensure_ascii=False, indent=2)

    blocked = _guard()
    if blocked:
        return blocked

    provider = Neo4jProvider()
    obj = None
    if "." in object_name:
        head, _, tail = object_name.partition(".")
        kind = TABLE_PREFIXES.get(head.upper())
        obj = provider.get(kind, tail.split(".")[0]) if kind else None
        if obj is None:
            obj = provider.get_by_id(object_name)
    if obj is None:
        obj = provider.find(object_name)

    if obj is None:
        return json.dumps({
            "error": f"Объект '{object_name}' не найден в метаданных",
            "suggestions": provider.suggest(object_name),
            "hint": ("Если это временная таблица, передайте текст пакета "
                     "в параметре query_text."),
        }, ensure_ascii=False, indent=2)

    table = _query_table_name(obj)
    vt = sorted(VIRTUAL_TABLES.get(obj.kind_ru, set()))
    result = {
        "table_name": table,
        "object": obj.id,
        "kind": obj.kind_ru,
        "synonym": obj.synonym,
        "standard_fields": standard_fields_for(obj),
        "attributes": [
            {"name": a.name, "role": a.role, "type": _type_text(a),
             "synonym": a.synonym}
            for a in obj.attributes
        ],
        "tabular_sections": {
            ts: [{"name": a.name, "type": _type_text(a)} for a in attrs]
            for ts, attrs in obj.tabular_sections.items()
        },
        "virtual_tables": [f"{table}.{v}" for v in vt],
    }
    if obj.kind_ru.startswith("Регистр"):
        # Измерения и ресурсы — имена, их немного, и они нужны целиком:
        # без них не построить ни одного запроса к регистру. Считаем по
        # ПОЛНОМУ списку реквизитов, а не по обрезанной странице, иначе
        # состав ключа регистра зависел бы от attributes_limit.
        result["dimensions"] = [a.name for a in obj.attributes
                                if a.role == "dimension"]
        result["resources"] = [a.name for a in obj.attributes
                               if a.role == "resource"]

    # TOOL-2: обрезка до разумного размера — см. trim_fields_payload.
    result = trim_fields_payload(
        result,
        attributes_limit=attributes_limit,
        attributes_offset=attributes_offset,
        tabular_section=tabular_section,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_join_hint(table1: str, table2: str) -> str:
    """
    Подсказать как соединить две таблицы в запросе 1С.
    Ищет ссылочные связи между объектами по узлам типов графа.

    Параметры:
      table1 — первая таблица ("Справочник.Номенклатура" или "Номенклатура")
      table2 — вторая таблица
    """
    blocked = _guard()
    if blocked:
        return blocked

    provider = Neo4jProvider()

    def resolve(name):
        if "." in name:
            head, _, tail = name.partition(".")
            kind = TABLE_PREFIXES.get(head.upper())
            if kind:
                obj = provider.get(kind, tail.split(".")[0])
                if obj:
                    return obj
            obj = provider.get_by_id(name)
            if obj:
                return obj
        return provider.find(name)

    obj1, obj2 = resolve(table1), resolve(table2)
    missing = [n for n, o in ((table1, obj1), (table2, obj2)) if o is None]
    if missing:
        return json.dumps({
            "error": f"Не найдены объекты: {', '.join(missing)}",
            "suggestions": {n: provider.suggest(n) for n in missing},
        }, ensure_ascii=False, indent=2)

    tbl1, tbl2 = _query_table_name(obj1), _query_table_name(obj2)
    joins = []
    # Сколько пар реквизитов рассмотрено. Пустой `joins` без этого числа
    # означает сразу две разные вещи — «связи между таблицами нет» и
    # «связи не искали» (у объектов не разобрались реквизиты, граф отдал
    # пустой список, инструмент отработал вхолостую). Различить их по
    # ответу было нечем, и soft-предикат на `joins` поэтому ничего не
    # измерял: он краснел одинаково в обоих случаях.
    examined = 0

    def scan(src_obj, dst_obj, src_alias, dst_alias, src_tbl, dst_tbl):
        nonlocal examined
        for a in src_obj.attributes:
            examined += 1
            if any(t == dst_obj.id for _, t in a.types):
                joins.append({
                    "type": "ЛЕВОЕ СОЕДИНЕНИЕ",
                    "explanation": f"{src_tbl}.{a.name} ссылается на {dst_tbl}",
                    "query_fragment":
                        f"ЛЕВОЕ СОЕДИНЕНИЕ {dst_tbl} КАК {dst_alias}\n"
                        f"\tПО {src_alias}.{a.name} = {dst_alias}.Ссылка",
                })
        for ts_name, attrs in src_obj.tabular_sections.items():
            for a in attrs:
                examined += 1
                if any(t == dst_obj.id for _, t in a.types):
                    joins.append({
                        "type": "ЧЕРЕЗ ТАБЛИЧНУЮ ЧАСТЬ",
                        "explanation":
                            f"{src_tbl}.{ts_name}.{a.name} ссылается на {dst_tbl}",
                        "query_fragment":
                            f"ЛЕВОЕ СОЕДИНЕНИЕ {src_tbl}.{ts_name} КАК ТЧ\n"
                            f"\tПО ТЧ.Ссылка = {src_alias}.Ссылка\n"
                            f"ЛЕВОЕ СОЕДИНЕНИЕ {dst_tbl} КАК {dst_alias}\n"
                            f"\tПО ТЧ.{a.name} = {dst_alias}.Ссылка",
                    })

    scan(obj1, obj2, "Т1", "Т2", tbl1, tbl2)
    scan(obj2, obj1, "Т2", "Т1", tbl2, tbl1)

    result = {"table1": tbl1, "table2": tbl2,
              "joins_found": len(joins), "joins": joins,
              "candidates_examined": examined}
    if not joins:
        result["hint"] = (
            "Прямая ссылочная связь не найдена. Варианты: соединение через "
            "промежуточную таблицу (регистр или справочник-связку); "
            "соединение по значению реквизита, а не по ссылке; подзапрос."
        )
        result["meaning"] = (
            f"Рассмотрено {examined} реквизитов обеих таблиц, ссылки друг на "
            f"друга ни у одного нет. Это достоверный ответ «связи нет», а не "
            f"отказ. Ноль рассмотренных означал бы обратное: реквизиты не "
            f"разобрались, и про связь не известно ничего."
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_build(
    description: str,
    tables: str = "",
    fields: str = "",
    conditions: str = "",
    group_by: bool = False,
) -> str:
    """
    Построить запрос 1С по описанию задачи, подставляя реальные имена
    из метаданных конфигурации.

    Параметры:
      description — что нужно получить ("остатки товаров на складе")
      tables      — (опционально) таблицы через запятую
      fields      — (опционально) поля результата через запятую
      conditions  — (опционально) условия отбора
      group_by    — нужна ли группировка
    """
    blocked = _guard()
    if blocked:
        return blocked

    provider = Neo4jProvider()
    resolved = []

    if tables:
        for t in [x.strip() for x in tables.split(",") if x.strip()]:
            head, _, tail = t.partition(".")
            kind = TABLE_PREFIXES.get(head.upper())
            obj = provider.get(kind, tail.split(".")[0]) if kind else None
            obj = obj or provider.find(t)
            if obj:
                resolved.append(obj)
    else:
        seen = set()
        for kw in [w for w in re.split(r"[\s,]+", description) if len(w) > 3]:
            for r in provider.search(kw, limit=3):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                obj = provider.get_by_id(r["id"])
                if obj:
                    resolved.append(obj)

    if not resolved:
        return json.dumps({
            "error": "Не удалось определить таблицы из описания. "
                     "Укажите их явно в параметре tables.",
            "hint": "Например: tables='Справочник.Номенклатура, "
                    "РегистрНакопления.ТоварыНаСкладах'",
        }, ensure_ascii=False, indent=2)

    main = resolved[0]
    main_tbl = _query_table_name(main)
    desc = description.lower()

    # Виртуальная таблица по смыслу описания
    use_virtual = ""
    if main.kind_ru == "РегистрНакопления":
        wants_rest = any(w in desc for w in ("остат", "баланс", "наличи"))
        wants_turn = any(w in desc for w in ("оборот", "движен", "приход", "расход"))
        if wants_rest and wants_turn:
            use_virtual = f"{main_tbl}.ОстаткиИОбороты"
        elif wants_rest:
            use_virtual = f"{main_tbl}.Остатки"
        elif wants_turn:
            use_virtual = f"{main_tbl}.Обороты"
    elif main.kind_ru == "РегистрСведений":
        if any(w in desc for w in ("последн", "актуальн", "текущ")):
            use_virtual = f"{main_tbl}.СрезПоследних"
        elif any(w in desc for w in ("перв", "начальн")):
            use_virtual = f"{main_tbl}.СрезПервых"

    source = f"{use_virtual}(&Период)" if use_virtual else main_tbl

    if fields:
        select = [f"Т.{f.strip()}" for f in fields.split(",") if f.strip()]
    else:
        select = [f"Т.{a.name}" for a in main.attributes[:10]]
        if main.kind_ru in ("Справочник", "Документ"):
            select = ["Т.Ссылка"] + select
        if not select:
            select = ["Т.Ссылка"]

    joins = []
    idx = 2
    for other in resolved[1:]:
        other_tbl = _query_table_name(other)
        link = next((a.name for a in main.attributes
                     if any(t == other.id for _, t in a.types)), None)
        if link:
            joins.append(f"ЛЕВОЕ СОЕДИНЕНИЕ {other_tbl} КАК Т{idx}\n"
                         f"\tПО Т.{link} = Т{idx}.Ссылка")
            idx += 1
            continue
        back = next((a.name for a in other.attributes
                     if any(t == main.id for _, t in a.types)), None)
        if back:
            joins.append(f"ЛЕВОЕ СОЕДИНЕНИЕ {other_tbl} КАК Т{idx}\n"
                         f"\tПО Т{idx}.{back} = Т.Ссылка")
            idx += 1

    lines = ["ВЫБРАТЬ"]
    for i, f in enumerate(select):
        lines.append(f"\t{f}" + ("," if i < len(select) - 1 else ""))
    lines += ["ИЗ", f"\t{source} КАК Т"]
    lines += joins
    if conditions:
        lines += ["ГДЕ", f"\t{conditions}"]
    if group_by:
        group_fields = [f for f in select
                        if not re.match(r"\s*(СУММА|КОЛИЧЕСТВО|МАКСИМУМ|"
                                        r"МИНИМУМ|СРЕДНЕЕ)\s*\(",
                                        f, re.IGNORECASE)]
        if group_fields:
            lines.append("СГРУППИРОВАТЬ ПО")
            for i, g in enumerate(group_fields):
                lines.append(f"\t{g}" + ("," if i < len(group_fields) - 1 else ""))

    query_text = "\n".join(lines)

    # Построенный запрос сразу прогоняется через ту же проверку, что
    # query_validate: инструмент не должен отдавать агенту текст с полями,
    # которых нет. Если проверка что-то нашла — это видно в ответе.
    validation = check_query(query_text, provider)

    result = {
        "query": query_text,
        "source_table": source,
        "is_virtual_table": bool(use_virtual),
        "tables_used": [_query_table_name(o) for o in resolved],
        "self_check": {
            "valid": validation["valid"],
            "errors": validation["errors"],
        },
        "available_fields": {
            _query_table_name(o): {
                "standard": standard_fields_for(o),
                "attributes": [a.name for a in o.attributes],
                "tabular_sections": list(o.tabular_sections),
            } for o in resolved
        },
        "hints": [],
    }
    if use_virtual:
        result["hints"].append(
            f"Использована виртуальная таблица {use_virtual}. Замените "
            f"&Период на нужные параметры и перенесите в них условия отбора."
        )
    if main.kind_ru.startswith("Регистр") and not use_virtual:
        result["hints"].append(
            "Для регистра почти всегда быстрее виртуальная таблица "
            "(Остатки, Обороты, СрезПоследних), а не основная таблица движений."
        )
    if len(resolved) > 1 and not joins:
        result["hints"].append(
            "Ссылочные связи между таблицами не найдены — условия соединения "
            "нужно задать вручную."
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_validate(query_text: str) -> str:
    """
    Проверить запрос 1С по метаданным конфигурации: существование таблиц,
    виртуальных таблиц, табличных частей и КАЖДОГО поля, включая точечные
    пути и колонки временных таблиц пакета.

    Параметры:
      query_text — текст запроса или пакета запросов на языке 1С
    """
    blocked = _guard()
    if blocked:
        return blocked

    result = check_query(query_text, Neo4jProvider())
    result["meaning"] = (
        "errors — доказанные расхождения с метаданными: объект в графе есть, "
        "состав его полей известен, такого имени среди них нет. "
        "warnings — места, где проверка не смогла ничего утверждать."
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_optimize(query_text: str) -> str:
    """
    Предложить оптимизации для запроса 1С.
    Анализирует структуру запроса: источники, их параметры, наличие
    ограничений выборки.

    Параметры:
      query_text — текст запроса
    """
    recs = analyze(query_text)
    if not recs:
        recs = [{
            "priority": "INFO",
            "rule": "Общая оценка",
            "issue": "Явных проблем с производительностью не найдено",
            "fix": "Для глубокого анализа смотрите план запроса "
                   "(технологический журнал либо Анализ запросов в Конфигураторе)",
        }]
    return json.dumps({
        "recommendations_count": len(recs),
        "recommendations": recs,
    }, ensure_ascii=False, indent=2)


# ─── Запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Штатный запуск — через start.py внутри контейнера. Этот блок оставлен
    # для отладки одного сервера в одиночку (тест 6.3 из плана).
    from mcp_http import run as run_http

    run_http(
        mcp,
        server_name="query-builder",
        port=int(os.environ.get("MCP_PORT", 8009)),
    )
