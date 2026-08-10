"""
MCP-сервер: Справка по платформе 1С (с Qdrant)
================================================
Семантический поиск по справке через Qdrant + эмбеддинги.
Если Qdrant недоступен — фолбэк на текстовый поиск.
"""

import os
import json
import re
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict
import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C Platform Help")
logger = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "platform_help")
ITS_COLLECTION = os.environ.get("ITS_COLLECTION", "its_articles")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
BM25_MODEL_NAME = os.environ.get("BM25_MODEL", "Qdrant/bm25")

# ─── Dense модель (ленивая загрузка) ──────────────────────────────────────

_model = None
_model_loaded = False


def _get_model():
    global _model, _model_loaded
    if not _model_loaded:
        try:
            from sentence_transformers import SentenceTransformer
            print(f"Загрузка модели {EMBEDDING_MODEL_NAME}...")
            _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            print(f"  ✓ Модель загружена (dim={_model.get_sentence_embedding_dimension()})")
            _model_loaded = True
        except Exception as e:
            print(f"  ⚠ Не удалось загрузить модель: {e}")
            _model_loaded = True  # Не пробуем повторно
    return _model


def _embed_query(query):
    """Создаёт dense-эмбеддинг для запроса."""
    model = _get_model()
    if model is None:
        return None
    is_e5 = "e5" in EMBEDDING_MODEL_NAME.lower()
    text = f"query: {query}" if is_e5 else query
    emb = model.encode([text], normalize_embeddings=True)
    return emb[0].tolist()


# ─── Sparse BM25 модель (ленивая загрузка, для гибридного ИТС-поиска) ────

_sparse_model = None
_sparse_loaded = False


def _get_sparse_model():
    global _sparse_model, _sparse_loaded
    if not _sparse_loaded:
        try:
            from fastembed import SparseTextEmbedding
            print(f"Загрузка BM25-модели {BM25_MODEL_NAME}...")
            _sparse_model = SparseTextEmbedding(model_name=BM25_MODEL_NAME)
            print("  ✓ BM25-модель загружена")
        except Exception as e:
            print(f"  ⚠ Не удалось загрузить BM25-модель: {e}")
        _sparse_loaded = True
    return _sparse_model


def _embed_query_sparse(query):
    """Создаёт sparse BM25-эмбеддинг для запроса. Возвращает (indices, values) или None."""
    model = _get_sparse_model()
    if model is None:
        return None
    try:
        emb = next(iter(model.query_embed([query])))
        return emb.indices.tolist(), emb.values.tolist()
    except Exception as e:
        print(f"  ⚠ BM25 query embed failed: {e}")
        return None


# ─── Qdrant client (ленивая загрузка) ────────────────────────────────────

_qclient = None
_qclient_loaded = False


def _get_qclient():
    """Возвращает qdrant_client.QdrantClient или None."""
    global _qclient, _qclient_loaded
    if not _qclient_loaded:
        try:
            from qdrant_client import QdrantClient
            _qclient = QdrantClient(url=QDRANT_URL, timeout=15)
        except Exception as e:
            print(f"  ⚠ qdrant-client недоступен: {e}")
        _qclient_loaded = True
    return _qclient


# ─── Qdrant клиент ────────────────────────────────────────────────────────

def _qdrant_available():
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION_NAME}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            count = data.get("result", {}).get("points_count", 0)
            return count > 0
    except Exception:
        return False


# ─── Детекция схемы коллекции platform_help ──────────────────────────────
# Коллекция создаётся hbk_indexer.py с двумя именованными векторами
# ("dense" + "sparse"), но возможно, что старая коллекция ещё висит в
# плоской схеме от старого indexer.py. Отделяем случаи — чтобы не упасть.
# Кешируем результат: клиент не меняет схему во время одной сессии.

_help_collection_kind = None  # "hybrid" | "legacy_dense" | "missing"
_help_collection_kind_at = 0.0

# FIX-12: "missing" кешируется только на MISSING_RECHECK_SEC.
#
# Раньше результат запоминался один раз за жизнь процесса. На практике это
# значило вот что: сервер поднимается вместе со стеком, help-indexer в этот
# момент ещё строит индекс, коллекция пуста — и сервер запоминает "missing"
# навсегда. Индексатор доработал, данные на месте, а platform_help_* до
# перезапуска контейнера отвечает "поиск недоступен". Снаружи это
# неотличимо от реально лежащего Qdrant.
#
# Тот же класс, что FIX-9 у bsl-checker: состояние определяется один раз, а
# наружу отдаётся как факт. Положительный результат кешируем навсегда —
# формат коллекции на ходу не меняется. Отрицательный перепроверяем: он
# почти всегда временный.
MISSING_RECHECK_SEC = int(os.environ.get("HELP_MISSING_RECHECK_SEC", "30"))


def _detect_help_collection_kind():
    """Формат коллекции platform_help: hybrid / legacy_dense / missing."""
    global _help_collection_kind, _help_collection_kind_at
    if _help_collection_kind is not None:
        if _help_collection_kind != "missing":
            return _help_collection_kind
        if time.monotonic() - _help_collection_kind_at < MISSING_RECHECK_SEC:
            return _help_collection_kind
        logger.info("platform_help: перепроверяю коллекцию после 'missing'")

    _help_collection_kind_at = time.monotonic()

    client = _get_qclient()
    if client is None:
        # Без qdrant_client гибрид всё равно не сделать — пробуем
        # определить через сырой HTTP только наличие данных.
        try:
            req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION_NAME}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                if data.get("result", {}).get("points_count", 0) > 0:
                    _help_collection_kind = "legacy_dense"
                    return _help_collection_kind
        except Exception:
            logger.debug("игнорируем исключение", exc_info=True)
        _help_collection_kind = "missing"
        return _help_collection_kind

    try:
        info = client.get_collection(COLLECTION_NAME)
    except Exception:
        _help_collection_kind = "missing"
        return _help_collection_kind

    if (info.points_count or 0) <= 1:
        # В коллекции может быть только fingerprint-точка — считаем пустой.
        _help_collection_kind = "missing"
        return _help_collection_kind

    try:
        params = info.config.params
        vectors = params.vectors
        sparse = getattr(params, "sparse_vectors", None)
        is_hybrid = (
            isinstance(vectors, dict)
            and "dense" in vectors
            and bool(sparse)
            and "sparse" in sparse
        )
        _help_collection_kind = "hybrid" if is_hybrid else "legacy_dense"
    except Exception:
        _help_collection_kind = "legacy_dense"

    return _help_collection_kind


# ─── Hybrid-поиск по platform_help (dense + BM25 sparse + RRF) ───────────

def _format_help_hits(points):
    """
    Унифицированно форматирует результаты для MCP-ответа.
    Принимает список qdrant_client.ScoredPoint.
    Отрезает служебную точку с fingerprint'ом (на всякий случай).
    """
    hits = []
    for point in points:
        p = point.payload or {}
        if p.get("_type") == "fingerprint" or p.get("chunk_type") == "fingerprint":
            continue
        hits.append({
            "score": round(point.score or 0, 4),
            "chunk_type": p.get("chunk_type", ""),
            "kind": p.get("kind", ""),
            "name_ru": p.get("name_ru", ""),
            "name_en": p.get("name_en", ""),
            "parent_ru": p.get("parent_ru", ""),
            "parent_en": p.get("parent_en", ""),
            "full_name": p.get("full_name", ""),
            "since_version": p.get("since_version", ""),
            "deprecated": p.get("deprecated", False),
            "availability": p.get("availability", ""),
            "returns": p.get("returns", ""),
            "text": (p.get("text") or "")[:1200],
            "hbk_file": p.get("hbk_file", ""),
            "file_path": p.get("file_path", ""),
        })
    return hits


def _build_kind_filter(kind: str):
    """
    Фильтр по полю payload.kind (method/property/event/object_type/category/table).
    Возвращает qdrant_client.models.Filter или None.
    """
    if not kind:
        return None
    try:
        from qdrant_client import models
        return models.Filter(
            must=[
                models.FieldCondition(key="kind", match=models.MatchValue(value=kind))
            ]
        )
    except Exception:
        return None


def _help_search_hybrid(query: str, limit: int = 10, kind_filter: str = ""):
    """
    Гибридный поиск по platform_help: dense (e5) + sparse (BM25) + RRF.
    Возвращает список hit-объектов или None в случае ошибки транспорта.
    """
    client = _get_qclient()
    if client is None:
        return None

    try:
        from qdrant_client import models
    except Exception:
        return None

    dense_vec = _embed_query(query)
    sparse_pair = _embed_query_sparse(query)
    if not dense_vec and not sparse_pair:
        return None

    # Префетч шире финального лимита — даёт RRF больше кандидатов.
    prefetch_limit = max(limit * 4, 20)
    prefetch = []
    if dense_vec:
        prefetch.append(
            models.Prefetch(query=dense_vec, using="dense", limit=prefetch_limit)
        )
    if sparse_pair:
        indices, values = sparse_pair
        prefetch.append(
            models.Prefetch(
                query=models.SparseVector(indices=indices, values=values),
                using="sparse",
                limit=prefetch_limit,
            )
        )

    try:
        result = client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=_build_kind_filter(kind_filter),
            limit=limit,
            with_payload=True,
        )
    except Exception as e:
        logger.debug(f"hybrid help search failed: {e}")
        return None

    return _format_help_hits(result.points)


def _help_search_legacy_dense(query: str, limit: int = 10, kind_filter: str = ""):
    """
    Fallback для старой single-vector коллекции: сырой HTTP POST /points/search.
    Вернёт None если и это не сработало.
    """
    vector = _embed_query(query)
    if not vector:
        return None

    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    if kind_filter:
        payload["filter"] = {
            "must": [{"key": "kind", "match": {"value": kind_filter}}]
        }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points/search",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        logger.debug(f"legacy dense help search failed: {e}")
        return None

    hits = []
    for point in result.get("result", []):
        p = point.get("payload", {})
        if p.get("_type") == "fingerprint":
            continue
        hits.append({
            "score": round(point.get("score", 0), 4),
            # старая схема использовала поля element_name/category — подставляем
            "chunk_type": p.get("chunk_type", ""),
            "kind": p.get("kind", ""),
            "name_ru": p.get("name_ru") or p.get("element_name", ""),
            "name_en": p.get("name_en", ""),
            "parent_ru": p.get("parent_ru") or p.get("parent_object", ""),
            "parent_en": p.get("parent_en", ""),
            "full_name": p.get("full_name", ""),
            "since_version": p.get("since_version", ""),
            "deprecated": p.get("deprecated", False),
            "availability": p.get("availability", ""),
            "returns": p.get("returns", ""),
            "text": (p.get("text") or "")[:1200],
            "hbk_file": p.get("hbk_file", ""),
            "file_path": p.get("file_path") or p.get("html_path", ""),
        })
    return hits


def _help_search(query: str, limit: int = 10, kind_filter: str = ""):
    """
    Главный диспетчер поиска по platform_help.
    Возвращает (hits, search_type) — search_type: 'hybrid' | 'dense_only' | 'unavailable'
    """
    kind = _detect_help_collection_kind()
    if kind == "hybrid":
        hits = _help_search_hybrid(query, limit, kind_filter)
        if hits is not None:
            return hits, "hybrid"
    if kind in ("hybrid", "legacy_dense"):
        hits = _help_search_legacy_dense(query, limit, kind_filter)
        if hits is not None:
            return hits, "dense_only"
    return [], "unavailable"


# ─── Встроенная справка (фолбэк) ─────────────────────────────────────────

BUILTIN = {}


def _load_builtin():
    core = [
        {"name": "СтрДлина", "name_en": "StrLen", "category": "Строковые функции",
         "syntax": "СтрДлина(<Строка>)", "returns": "Число",
         "description": "Количество символов в строке."},
        {"name": "Лев", "name_en": "Left", "category": "Строковые функции",
         "syntax": "Лев(<Строка>, <ЧислоСимволов>)", "returns": "Строка",
         "description": "Первые символы строки."},
        {"name": "СтрЗаменить", "name_en": "StrReplace", "category": "Строковые функции",
         "syntax": "СтрЗаменить(<Строка>, <Поиск>, <Замена>)", "returns": "Строка",
         "description": "Замена подстроки."},
        {"name": "СтрРазделить", "name_en": "StrSplit", "category": "Строковые функции",
         "syntax": "СтрРазделить(<Строка>, <Разделитель>)", "returns": "Массив",
         "description": "Разделение строки."},
        {"name": "ВРег", "name_en": "Upper", "category": "Строковые функции",
         "syntax": "ВРег(<Строка>)", "returns": "Строка", "description": "Верхний регистр."},
        {"name": "НРег", "name_en": "Lower", "category": "Строковые функции",
         "syntax": "НРег(<Строка>)", "returns": "Строка", "description": "Нижний регистр."},
        {"name": "ТекущаяДата", "name_en": "CurrentDate", "category": "Функции дат",
         "syntax": "ТекущаяДата()", "returns": "Дата", "description": "Текущая дата/время."},
        {"name": "НачалоМесяца", "name_en": "BegOfMonth", "category": "Функции дат",
         "syntax": "НачалоМесяца(<Дата>)", "returns": "Дата", "description": "Начало месяца."},
        {"name": "Макс", "name_en": "Max", "category": "Математика",
         "syntax": "Макс(<Зн1>, <Зн2>)", "description": "Максимум."},
        {"name": "Мин", "name_en": "Min", "category": "Математика",
         "syntax": "Мин(<Зн1>, <Зн2>)", "description": "Минимум."},
        {"name": "ТипЗнч", "name_en": "TypeOf", "category": "Типы",
         "syntax": "ТипЗнч(<Значение>)", "returns": "Тип", "description": "Тип значения."},
        {"name": "Массив", "name_en": "Array", "category": "Коллекции",
         "syntax": "Новый Массив", "description": "Упорядоченная коллекция."},
        {"name": "Структура", "name_en": "Structure", "category": "Коллекции",
         "syntax": "Новый Структура", "description": "Пары ключ-значение."},
        {"name": "ТаблицаЗначений", "name_en": "ValueTable", "category": "Коллекции",
         "syntax": "Новый ТаблицаЗначений", "description": "Таблица с колонками."},
        {"name": "Запрос", "name_en": "Query", "category": "Запросы",
         "syntax": "Новый Запрос(<Текст>)", "description": "Запросы к БД."},
        {"name": "НачатьТранзакцию", "name_en": "BeginTransaction", "category": "Транзакции",
         "syntax": "НачатьТранзакцию()", "description": "Начало транзакции."},
        {"name": "ЗафиксироватьТранзакцию", "name_en": "CommitTransaction", "category": "Транзакции",
         "syntax": "ЗафиксироватьТранзакцию()", "description": "Фиксация транзакции."},
        {"name": "ОтменитьТранзакцию", "name_en": "RollbackTransaction", "category": "Транзакции",
         "syntax": "ОтменитьТранзакцию()", "description": "Откат транзакции."},
        {"name": "Сообщить", "name_en": "Message", "category": "Диалог",
         "syntax": "Сообщить(<Текст>)", "description": "Сообщение пользователю."},
        {"name": "Формат", "name_en": "Format", "category": "Форматирование",
         "syntax": "Формат(<Значение>, <Формат>)", "returns": "Строка", "description": "Форматирование."},
    ]
    for item in core:
        BUILTIN[item["name"].lower()] = item
        if "name_en" in item:
            BUILTIN[item["name_en"].lower()] = item


def _fallback_search(query, limit=10):
    q = query.lower()
    scored = []
    for key, item in BUILTIN.items():
        score = 0
        name = item.get("name", "").lower()
        name_en = item.get("name_en", "").lower()
        if q == name or q == name_en:
            score = 100
        elif name.startswith(q) or name_en.startswith(q):
            score = 50
        elif q in name or q in name_en:
            score = 30
        elif q in item.get("description", "").lower():
            score = 10
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    seen = set()
    results = []
    for _, item in scored:
        n = item["name"]
        if n not in seen:
            seen.add(n)
            results.append(item)
        if len(results) >= limit:
            break
    return results


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
def platform_help_search(query: str, limit: int = 10, kind: str = "") -> str:
    """
    Семантический поиск по справке платформы 1С (hybrid: dense + BM25 + RRF).

    query — строка поиска (например "как разделить строку", "НайтиПоНаименованию")
    limit — максимум результатов (1-50)
    kind  — фильтр по типу страницы (необязательный):
              method, property, event, object_type, category, table

    Возвращает найденные чанки с payload: имена (RU/EN), kind, since_version,
    deprecated, availability, returns, text. Для точного поиска по имени
    используй platform_help_lookup.
    """
    limit = max(1, min(int(limit), 50))

    hits, mode = _help_search(query, limit, kind or "")

    if not hits:
        # Fallback на встроенную справку (20 функций типа СтрДлина, Лев, …).
        # Это защита от полного "не знаю ничего", если Qdrant ещё не поднялся.
        bi_hits = _fallback_search(query, limit)
        if bi_hits:
            return json.dumps({
                "search_type": "builtin_fallback",
                "query": query,
                "results": bi_hits,
                "note": "Qdrant не содержит справку; показаны встроенные функции. "
                        "Возможно, help-indexer ещё не отработал или упал.",
            }, ensure_ascii=False, indent=2)
        return json.dumps({
            "search_type": mode,
            "query": query,
            "results": [],
            "note": "Ничего не найдено. Если справка должна быть — "
                    "проверь help-indexer и статус коллекции platform_help.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "search_type": mode,  # "hybrid" | "dense_only"
        "query": query,
        "filter": {"kind": kind} if kind else None,
        "results_count": len(hits),
        "results": hits,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def platform_help_lookup(name: str, limit: int = 10) -> str:
    """
    Точный lookup карточки по имени метода / свойства / события.
    Ищет точное совпадение по name_ru и name_en (без векторов, детерминированно).
    Быстрее и точнее семантического поиска, когда известно имя.

    name  — имя без родителя, например "НайтиПоНаименованию" или "FindByDescription",
            или полное имя через точку, например "ПоляСхемыЗапроса.Добавить".
    limit — максимум совпадений (у одного метода бывает много реализаций —
            Catalog, Document, ChartOfCalculationTypes и т.д.)
    """
    limit = max(1, min(int(limit), 50))
    name = name.strip()
    if not name:
        return json.dumps({"error": "name is empty"}, ensure_ascii=False)

    # Если передано "Parent.Name" — ищем оба: name_ru и parent_ru.
    parent = ""
    plain_name = name
    if "." in name:
        parent, _, plain_name = name.rpartition(".")
        parent = parent.strip()
        plain_name = plain_name.strip()

    client = _get_qclient()
    if client is None:
        return json.dumps({
            "error": "qdrant_client недоступен",
            "hint": "платформа не поднята или коллекция пуста",
        }, ensure_ascii=False)

    try:
        from qdrant_client import models
    except Exception as e:
        return json.dumps({"error": f"qdrant_client import failed: {e}"}, ensure_ascii=False)

    # Хотим ТОЛЬКО карточки (chunk_type=card), чтобы не размножать результат
    # на card+params+syntax для одного метода.
    card_cond = models.FieldCondition(
        key="chunk_type", match=models.MatchValue(value="card")
    )

    # Ищем по name_ru OR name_en. Qdrant: "or" делается через should
    # ВНУТРИ отдельного Filter, который ставится в must верхнего Filter.
    # Тогда получается "A AND (B OR C)" — ровно то что нам нужно.
    name_or_filter = models.Filter(
        should=[
            models.FieldCondition(key="name_ru", match=models.MatchValue(value=plain_name)),
            models.FieldCondition(key="name_en", match=models.MatchValue(value=plain_name)),
        ]
    )

    must_conditions = [card_cond, name_or_filter]

    # Если указан parent — добавляем ещё одно "OR" между parent_ru и parent_en
    if parent:
        parent_or_filter = models.Filter(
            should=[
                models.FieldCondition(key="parent_ru", match=models.MatchValue(value=parent)),
                models.FieldCondition(key="parent_en", match=models.MatchValue(value=parent)),
            ]
        )
        must_conditions.append(parent_or_filter)

    scroll_filter = models.Filter(must=must_conditions)

    try:
        result, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        return json.dumps({
            "error": f"lookup failed: {type(e).__name__}: {e}",
            "name": name,
        }, ensure_ascii=False)

    hits = []
    for point in result:
        p = point.payload or {}
        if p.get("_type") == "fingerprint":
            continue
        hits.append({
            "kind": p.get("kind", ""),
            "name_ru": p.get("name_ru", ""),
            "name_en": p.get("name_en", ""),
            "parent_ru": p.get("parent_ru", ""),
            "parent_en": p.get("parent_en", ""),
            "full_name": p.get("full_name", ""),
            "since_version": p.get("since_version", ""),
            "deprecated": p.get("deprecated", False),
            "deprecated_version": p.get("deprecated_version", ""),
            "availability": p.get("availability", ""),
            "returns": p.get("returns", ""),
            "text": p.get("text", ""),
            "file_path": p.get("file_path", ""),
        })

    return json.dumps({
        "lookup": name,
        "parent_filter": parent or None,
        "results_count": len(hits),
        "results": hits,
        "hint": (
            "Если ничего не найдено, попробуй platform_help_search — "
            "он работает по смыслу и морфологии."
            if not hits else None
        ),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def platform_help_details(name: str) -> str:
    """
    Полная информация об элементе справки: карточка + параметры + синтаксис +
    примеры. Внутри — сначала точный lookup по имени, потом подтягивает
    все связанные чанки (params/syntax/example) того же file_path.

    name — имя метода/свойства или "Parent.Name".
    """
    # Шаг 1: находим карточку через lookup
    lookup_raw = platform_help_lookup(name, limit=5)
    lookup = json.loads(lookup_raw)
    cards = lookup.get("results", [])

    if not cards:
        # fallback: встроенная справка (для СтрДлина и пр.)
        item = BUILTIN.get(name.lower())
        if item:
            return json.dumps(
                {"source": "builtin", "item": item},
                ensure_ascii=False, indent=2,
            )
        return json.dumps({
            "error": f"'{name}' не найден",
            "hint": "Попробуй platform_help_search для нечёткого поиска.",
        }, ensure_ascii=False)

    # Шаг 2: для лучшего совпадения берём связанные чанки (params, syntax, example)
    best = cards[0]
    file_path = best.get("file_path", "")

    related = {"params": "", "syntax": "", "example": "", "description": ""}
    if file_path:
        client = _get_qclient()
        if client is not None:
            try:
                from qdrant_client import models
                fp_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key="file_path", match=models.MatchValue(value=file_path)
                        )
                    ]
                )
                result, _ = client.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=fp_filter,
                    limit=10,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in result:
                    p = point.payload or {}
                    ct = p.get("chunk_type", "")
                    if ct in related and not related[ct]:
                        related[ct] = p.get("text", "")
            except Exception as e:
                logger.debug(f"related chunks lookup failed: {e}")

    return json.dumps({
        "source": "platform_help",
        "card": best,
        "alternatives_count": max(0, len(cards) - 1),
        "alternatives": [
            {
                "full_name": c.get("full_name", ""),
                "kind": c.get("kind", ""),
                "file_path": c.get("file_path", ""),
            }
            for c in cards[1:]
        ],
        "syntax": related.get("syntax", ""),
        "params": related.get("params", ""),
        "example": related.get("example", ""),
        "description_extra": related.get("description", ""),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def platform_help_kinds() -> str:
    """
    Список типов страниц справки и их количество в индексе.
    Полезно, чтобы понять, что доступно для фильтра в platform_help_search.
    """
    client = _get_qclient()
    if client is None:
        return json.dumps({"error": "qdrant_client недоступен"}, ensure_ascii=False)
    try:
        from qdrant_client import models
        counts = {}
        for kind in ("method", "property", "event", "object_type", "category", "table"):
            result = client.count(
                collection_name=COLLECTION_NAME,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="chunk_type", match=models.MatchValue(value="card")),
                        models.FieldCondition(key="kind", match=models.MatchValue(value=kind)),
                    ]
                ),
                exact=True,
            )
            counts[kind] = result.count
        return json.dumps({
            "collection": COLLECTION_NAME,
            "kinds": counts,
            "hint": "передай 'kind' в platform_help_search чтобы отфильтровать",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@mcp.tool()
def platform_help_stats() -> str:
    """Статистика: Qdrant доступен? Сколько документов? Какая схема коллекции?"""
    qdrant_ok = _qdrant_available()
    points_count = 0

    if qdrant_ok:
        try:
            req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION_NAME}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                points_count = data.get("result", {}).get("points_count", 0)
        except Exception:
            logger.debug("игнорируем исключение", exc_info=True)

    model = _get_model()
    help_kind = _detect_help_collection_kind()
    its_kind = _detect_its_collection_kind()

    # Проверяем коллекцию ИТС
    its_count = 0
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{ITS_COLLECTION}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            its_count = data.get("result", {}).get("points_count", 0)
    except Exception:
        logger.debug("игнорируем исключение", exc_info=True)

    return json.dumps({
        "qdrant_available": qdrant_ok,
        "qdrant_url": QDRANT_URL,
        "platform_help_collection": COLLECTION_NAME,
        "platform_help_points": points_count,
        "platform_help_collection_kind": help_kind,  # hybrid | legacy_dense | missing
        # FIX-12: сколько секунд назад определён формат. Без этого по
        # ответу "unavailable" не отличить лежащий Qdrant от кеша,
        # который ещё не перепроверялся.
        "collection_kind_checked_ago_sec": round(time.monotonic() - _help_collection_kind_at, 1),
        "platform_help_search_mode": (
            "hybrid (dense + BM25 + RRF)" if help_kind == "hybrid"
            else ("dense only (legacy)" if help_kind == "legacy_dense" else "unavailable")
        ),
        "its_collection": ITS_COLLECTION,
        "its_points": its_count,
        "its_collection_kind": its_kind,
        "its_search_mode": (
            "hybrid (dense + BM25 + RRF)" if its_kind == "hybrid"
            else ("dense only" if its_kind == "legacy_dense" else "unavailable")
        ),
        "embedding_model": EMBEDDING_MODEL_NAME,
        "bm25_model": BM25_MODEL_NAME,
        "model_loaded": model is not None,
        "fallback_items": len(set(i["name"] for i in BUILTIN.values())),
    }, ensure_ascii=False, indent=2)


# ─── Поиск по ИТС (гибридный: dense + BM25 sparse + RRF) ────────────────
#
# Коллекция ИТС создаётся its_indexer.py в гибридном формате с двумя
# именованными векторами:
#   - "dense"  — multilingual-e5-base (cosine), поиск по смыслу
#   - "sparse" — fastembed Qdrant/bm25, поиск по точным словам
#
# Здесь мы делаем гибридный запрос через qdrant_client.query_points с
# prefetch + RRF (Reciprocal Rank Fusion) — нативный механизм Qdrant 1.10+,
# который объединяет два списка результатов в один сбалансированный.
#
# Если коллекция ещё в старом (плоском) формате — graceful fallback на
# чисто dense-поиск через сырой HTTP, чтобы не ломать работающие установки
# до того, как пользователь перезапустит its-indexer.

# Кэш типа коллекции, чтобы не дёргать get_collection на каждый запрос
_its_collection_kind = None  # "hybrid" | "legacy_dense" | "missing"


def _detect_its_collection_kind():
    """Определяет формат коллекции ИТС: hybrid, legacy_dense или missing."""
    global _its_collection_kind
    if _its_collection_kind is not None:
        return _its_collection_kind

    client = _get_qclient()
    if client is None:
        # Fallback: смотрим через сырой HTTP, точно ли коллекция есть
        try:
            req = urllib.request.Request(f"{QDRANT_URL}/collections/{ITS_COLLECTION}")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                if data.get("result", {}).get("points_count", 0) > 0:
                    _its_collection_kind = "legacy_dense"
                    return _its_collection_kind
        except Exception:
            logger.debug("игнорируем исключение", exc_info=True)
        _its_collection_kind = "missing"
        return _its_collection_kind

    try:
        info = client.get_collection(ITS_COLLECTION)
    except Exception:
        _its_collection_kind = "missing"
        return _its_collection_kind

    if (info.points_count or 0) <= 0:
        _its_collection_kind = "missing"
        return _its_collection_kind

    try:
        params = info.config.params
        vectors = params.vectors
        sparse = getattr(params, "sparse_vectors", None)
        is_hybrid = (
            isinstance(vectors, dict)
            and "dense" in vectors
            and bool(sparse)
            and "sparse" in sparse
        )
        _its_collection_kind = "hybrid" if is_hybrid else "legacy_dense"
    except Exception:
        _its_collection_kind = "legacy_dense"

    return _its_collection_kind


def _its_available():
    return _detect_its_collection_kind() != "missing"


def _format_its_hits(points):
    """Унифицированно форматирует результаты ИТС-поиска (qdrant_client ScoredPoint)."""
    hits = []
    for point in points:
        p = point.payload or {}
        hits.append({
            "score": round(point.score or 0, 4),
            "title": p.get("title", ""),
            "category": p.get("category", ""),
            "std_number": p.get("std_number", ""),
            "filename": p.get("filename", ""),
            "text": p.get("text", "")[:500],
        })
    return hits


def _its_search_hybrid(query, limit=10, category_filter=None):
    """
    Гибридный поиск через qdrant_client.query_points + RRF.

    Шаги:
      1) Считаем dense-вектор запроса (e5).
      2) Считаем sparse BM25-вектор запроса.
      3) Отправляем в Qdrant ОДИН запрос с двумя prefetch-ветками
         (dense → top-K, sparse → top-K) и финальным fusion=RRF.
      4) Qdrant сам объединит и переранжирует результаты.
    """
    client = _get_qclient()
    if client is None:
        return None

    from qdrant_client import models

    dense_vec = _embed_query(query)
    sparse_pair = _embed_query_sparse(query)

    if not dense_vec and not sparse_pair:
        return None

    # Префетч примерно в 4 раза шире финального лимита — это даёт RRF
    # достаточно кандидатов для качественного объединения.
    prefetch_limit = max(limit * 4, 20)
    prefetch = []

    if dense_vec:
        prefetch.append(
            models.Prefetch(
                query=dense_vec,
                using="dense",
                limit=prefetch_limit,
            )
        )

    if sparse_pair:
        indices, values = sparse_pair
        prefetch.append(
            models.Prefetch(
                query=models.SparseVector(indices=indices, values=values),
                using="sparse",
                limit=prefetch_limit,
            )
        )

    # Если по какой-то причине осталась только одна ветка — query_points
    # отработает и без fusion (просто вернёт результаты этой ветки).
    query_filter = None
    if category_filter:
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="category",
                    match=models.MatchValue(value=category_filter),
                )
            ]
        )

    try:
        result = client.query_points(
            collection_name=ITS_COLLECTION,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    except Exception as e:
        print(f"  ⚠ Hybrid search failed: {e}")
        return None

    return _format_its_hits(result.points)


def _its_search_legacy_dense(query, limit=10, category_filter=None):
    """
    Fallback: чисто dense-поиск через сырой HTTP — для коллекций,
    созданных старым (v2) индексатором с одним вектором без имени.
    """
    vector = _embed_query(query)
    if not vector:
        return None

    payload = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    if category_filter:
        payload["filter"] = {
            "must": [{"key": "category", "match": {"value": category_filter}}]
        }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{ITS_COLLECTION}/points/search",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        print(f"  ⚠ Legacy dense search failed: {e}")
        return None

    hits = []
    for point in result.get("result", []):
        p = point.get("payload", {})
        hits.append({
            "score": round(point.get("score", 0), 4),
            "title": p.get("title", ""),
            "category": p.get("category", ""),
            "std_number": p.get("std_number", ""),
            "filename": p.get("filename", ""),
            "text": p.get("text", "")[:500],
        })
    return hits


def _its_search(query, limit=10, category_filter=None):
    """Главный диспетчер ИТС-поиска: hybrid если возможно, иначе legacy."""
    kind = _detect_its_collection_kind()
    if kind == "hybrid":
        hits = _its_search_hybrid(query, limit, category_filter)
        if hits is not None:
            return hits, "hybrid"
    if kind in ("hybrid", "legacy_dense"):
        hits = _its_search_legacy_dense(query, limit, category_filter)
        if hits is not None:
            return hits, "dense_only"
    return [], "unavailable"


@mcp.tool()
def its_search(query: str, limit: int = 10, category: str = "") -> str:
    """
    Гибридный поиск по статьям ИТС (its.1c.ru): dense (e5) + BM25 + RRF.
    Находит стандарты разработки, методические рекомендации, документацию.

    query    — строка поиска (например "правила именования переменных",
               "обработка ошибок", "#std466")
    limit    — максимум результатов
    category — необязательный фильтр по категории
               ("Стандарты разработки", "Методические рекомендации", ...)
    """
    if not _its_available():
        return json.dumps(
            {"message": "Коллекция ИТС пуста или недоступна. Запустите its-indexer."},
            ensure_ascii=False,
        )

    hits, mode = _its_search(query, limit, category or None)
    if not hits:
        return json.dumps(
            {"message": "Ничего не найдено", "search_type": mode},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "search_type": mode,  # "hybrid" | "dense_only"
            "source": "its.1c.ru",
            "query": query,
            "results": hits,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def search_all(query: str, limit: int = 5) -> str:
    """
    Поиск по ВСЕМ источникам: справка платформы + статьи ИТС.
    Объединяет результаты и сортирует по релевантности.

    query — строка поиска
    limit — максимум результатов из каждого источника
    """
    results = {"query": query, "sources": []}

    # Справка платформы — через новый hybrid-диспетчер
    help_hits, help_mode = _help_search(query, limit)
    if help_hits:
        results["sources"].append({
            "source": "Справка платформы",
            "search_type": help_mode,
            "results": help_hits,
        })

    # ИТС
    if _its_available():
        its_hits, mode = _its_search(query, limit)
        if its_hits:
            results["sources"].append({
                "source": "ИТС (its.1c.ru)",
                "search_type": mode,
                "results": its_hits,
            })

    if not results["sources"]:
        # Фолбэк на встроенную справку
        fb = _fallback_search(query, limit)
        if fb:
            results["sources"].append({
                "source": "Встроенная справка",
                "results": fb,
            })

    return json.dumps(results, ensure_ascii=False, indent=2)


# ─── Инициализация ────────────────────────────────────────────────────────

_load_builtin()


# FIX-14: прогрев модели в фоне при старте.
#
# `_get_model()` тянет e5 в память при первом обращении. В прогоне датасета
# это выглядело безобидно — одна аномалия на десять примеров, — но цифра
# была 31 950 мс против 120–700 мс у остальных.
#
# В чате это тридцать секунд тишины на первый вопрос про платформу после
# каждого рестарта контейнера. Достаточно, чтобы агент счёл инструмент
# неотвечающим и пошёл отвечать по памяти, — то есть ровно тот исход,
# ради предотвращения которого весь набор и строился.
#
# Греем в фоновом потоке (daemon), а не синхронно: сервер должен принимать
# соединения сразу, иначе healthcheck не дождётся старта. Первый запрос,
# если он придёт раньше прогрева, просто подождёт на том же месте, что и
# раньше — хуже не станет.
#
# Отключается HELP_WARMUP=0: в тестах и при отладке лишняя загрузка модели
# на 2 ГБ ни к чему.


def _warmup() -> None:
    """Загружает модель и делает один холостой прогон."""
    try:
        model = _get_model()
        if model is None:
            return
        # Прогреваем не только загрузку весов, но и первый forward: ленивая
        # инициализация внутри torch тоже стоит секунд.
        model.encode(["query: прогрев"], show_progress_bar=False)
        print("  ✓ Модель прогрета, поиск готов", flush=True)
    except Exception as exc:
        # Прогрев — оптимизация, а не условие работы. Упал — просто вернулись
        # к ленивой загрузке, о чём и сообщаем.
        print(f"  ⚠ Прогрев не удался ({type(exc).__name__}), "
              f"модель загрузится при первом запросе", flush=True)


if os.environ.get("HELP_WARMUP", "1").strip().lower() not in ("0", "false", "no"):
    threading.Thread(target=_warmup, name="model-warmup", daemon=True).start()
