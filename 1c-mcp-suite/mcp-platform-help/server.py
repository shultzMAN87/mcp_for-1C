"""
MCP-сервер: Справка по платформе 1С (с Qdrant)
================================================
Семантический поиск по справке через Qdrant + эмбеддинги.
Если Qdrant недоступен — фолбэк на текстовый поиск.

DATA-1 закрыта, но не так, как задумывалась
────────────────────────────────────────────
Здесь была вторая коллекция (`its_articles`) и два инструмента поверх неё:
`its_search` и `search_all`. Оба висели вырезанными в `mcp_tool_filter.py`
с формулировкой «нет данных» с Захода 1, а задача DATA-1 — наполнить эту
коллекцию — переносилась из плана в план.

Наполнять её не будем. Источником стандартов стал отдельный сервер
`v8std-mcp` (порт 8765): у него свой корпус, свой цикл обновления и свои
пять инструментов. Значит, коллекция `its_articles` не появится никогда, а
код, который её читает, — мёртвый по построению, а не «пока без данных».
Поэтому он удалён: ~200 строк, переменная `ITS_COLLECTION` и поля `its_*`
в статистике.

Отдельно про `search_all`. Он обещал в докстринге объединение и ранжирование
по релевантности, а складывал два списка в разные секции — то есть врал.
Восстанавливать его поверх двух серверов нельзя: скоры разных источников
несопоставимы, а межпроцессное слияние потребовало бы сетевого вызова из
одного MCP-сервера в другой — ровно то, чего в наборе не делают.
Разграничение источников теперь живёт там, где им пользуются, — в
.cursor/rules/mcp-tools.mdc.
"""

import os
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict
import logging

from mcp.server.fastmcp import FastMCP

# OBS-1: единый словарь отказа.
# B-4: единый словарь постраничности. Справка листаться не умеет и не
# должна — см. no_pagination ниже, — но контракт обязана соблюдать, иначе
# правило «видишь has_more: true — запроси следующую страницу» получает
# исключение, а исключения в правилах агенты роняют первыми.
try:
    from refusal import install_answerable_field
    from mcp_pagination import no_pagination
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import install_answerable_field
    from mcp_pagination import no_pagination

# Прореживание и перемешивание выдачи (см. help_ranking.py).
# Импорт намеренно без try/except: если файл забыли положить в образ,
# сервер должен упасть на старте, а не тихо отдавать выдачу без обработки —
# ровно этот класс отказов и разбирался в HBK-1.
from help_ranking import diversify_hits

# PERF-6: модели грузятся ровно один раз, кто бы ни попросил.
from model_warmup import build_warmup

# FAIL-2. Диагностическое сообщение не должно ронять то, что диагностирует.
#
# Приёмка 15 августа, Windows: `_get_model()` не смог загрузить модель,
# поймал исключение — и упал на печати сообщения об этом. В консоли была
# cp1251, а в строке стоял знак ⚠, которого в cp1251 нет. Наружу вместо
# «модель недоступна, работаем без неё» полетел UnicodeEncodeError, и
# вызов инструмента развалился целиком.
#
# Обработчик был написан правильно: поймал, сообщил, поехал дальше. Убило
# его именно «сообщил». Внутри контейнера этого не видно — там UTF-8, — и
# ровно поэтому дефект дожил до хоста.
#
# Лечение на весь модуль разом: просим потоки заменять непредставимые
# символы вместо исключения. Печать становится best-effort, чем ей и
# положено быть: галочка в логе не стоит упавшего запроса.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        # Поток подменён (тесты, перехват вывода) или не текстовый —
        # молчим: настройка вывода не может быть условием работы сервера.
        pass


def _say(message: str, err: bool = False) -> None:
    """
    Диагностическое сообщение, которое не может уронить вызывающего.

    Настройки потоков выше хватает ровно до тех пор, пока `sys.stdout` не
    подменили после импорта — а его подменяют и тесты, и обёртки запуска.
    Поэтому вторая линия: печать в try, при отказе — ASCII-приближение,
    при повторном отказе молчание. Потерять галочку в логе можно,
    уронить запрос нельзя.
    """
    stream = sys.stderr if err else sys.stdout
    try:
        print(message, file=stream, flush=True)
    except Exception:
        try:
            print(message.encode("ascii", "replace").decode("ascii"),
                  file=stream, flush=True)
        except Exception:
            pass


mcp = FastMCP("1C Platform Help")

# OBS-1. У этого сервера поле `degraded` появилось раньше других (заход
# A-3), но оно отвечает на другой вопрос: «ответ хуже штатного». На вопрос
# «можно ли на ответ опереться» отвечает `answerable`, и вот его не было.
# Разница видна на dense-only: выдача беднее обычной (degraded), но
# пользоваться ею можно (answerable).
install_answerable_field(mcp)

logger = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "platform_help")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
BM25_MODEL_NAME = os.environ.get("BM25_MODEL", "Qdrant/bm25")

# ─── Модели: грузятся ровно один раз (PERF-6) ────────────────────────────
#
# Здесь стояли две пары «глобальная переменная + флаг», и обе имели один и
# тот же дефект:
#
#     if not _model_loaded:
#         _model = SentenceTransformer(...)   # 10+ секунд
#         _model_loaded = True
#
# Флаг ставился ПОСЛЕ загрузки. Пока поток прогрева грузил модель, флаг
# оставался ложным — и пришедший запрос заходил в ту же ветку и начинал
# грузить вторую копию тех же двух гигабайтов. Отсюда 23 секунды на первый
# `ph-001` вместо примерно двенадцати: две загрузки конкурировали за память
# и диск.
#
# Флаг отвечал на вопрос «уже загружено?» и во время загрузки честно
# отвечал «нет»; из этого «нет» второй поток делал вывод «значит, надо
# грузить». Спрашивать надо было другое — «этим уже кто-то занят?», — и на
# это отвечает замок, а не флаг. Он теперь в model_warmup.LazyModel.
_models = build_warmup()


def _get_model():
    return _models.models["dense"].get()


def _embed_query(query):
    """Создаёт dense-эмбеддинг для запроса."""
    model = _get_model()
    if model is None:
        return None
    is_e5 = "e5" in EMBEDDING_MODEL_NAME.lower()
    text = f"query: {query}" if is_e5 else query
    emb = model.encode([text], normalize_embeddings=True)
    return emb[0].tolist()


def _get_sparse_model():
    return _models.models["sparse"].get()


def _embed_query_sparse(query):
    """Создаёт sparse BM25-эмбеддинг для запроса. Возвращает (indices, values) или None."""
    model = _get_sparse_model()
    if model is None:
        return None
    try:
        emb = next(iter(model.query_embed([query])))
        return emb.indices.tolist(), emb.values.tolist()
    except Exception as e:
        _say(f"  ⚠ BM25 query embed failed: {e}")
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
            _qclient = QdrantClient(url=QDRANT_URL, timeout=QDRANT_TIMEOUT_SEC)
        except Exception as e:
            _say(f"  ⚠ qdrant-client недоступен: {e}")
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

# FAIL-1. Отказ должен быть не только громким, но и быстрым.
#
# Приёмка 15 августа: остановили Qdrant, и каждый вызов platform_help_search
# стал занимать 7,9 с вместо 190 мс. Разбор: формат коллекции кешируется
# положительно НАВСЕГДА («на ходу не меняется»), поэтому после смерти Qdrant
# сервер продолжал считать коллекцию гибридной и честно шёл полным
# маршрутом — сначала таймаут qdrant-client (15 с номинально, ~4 с до
# отказа соединения), потом таймаут сырого HTTP у legacy-ветки (~4 с), и
# только затем «unavailable».
#
# В чате это восемь секунд тишины на КАЖДЫЙ вопрос про платформу, пока
# стенд лежит. FIX-14 чинил ровно этот исход — тридцать секунд на первый
# запрос — и обоснование было такое же: агент считает инструмент
# неотвечающим и уходит отвечать по памяти. Разница лишь в том, что там
# это случалось однажды после старта, а здесь — на каждом вызове и без
# конца.
#
# Лечение: считать подряд идущие отказы транспорта и после порога сбрасывать
# кеш формата в "missing". Тогда третий и последующие вызовы возвращаются
# мгновенно, а через MISSING_RECHECK_SEC сервер сам перепроверит и поднимет
# режим обратно, когда Qdrant вернётся. Проверка живости остаётся
# автоматической — руками перезапускать сервер не нужно.
QDRANT_TIMEOUT_SEC = int(os.environ.get("HELP_QDRANT_TIMEOUT_SEC", "4"))
TRANSPORT_FAILS_BEFORE_GIVING_UP = int(
    os.environ.get("HELP_TRANSPORT_FAILS_BEFORE_GIVING_UP", "2")
)

_transport_fails = 0


def _note_transport_failure(where: str) -> None:
    """Отказ транспорта. После порога роняем кеш формата в 'missing'."""
    global _transport_fails, _help_collection_kind, _help_collection_kind_at
    _transport_fails += 1
    if _transport_fails < TRANSPORT_FAILS_BEFORE_GIVING_UP:
        return
    if _help_collection_kind in (None, "missing"):
        return
    _say(f"  ⚠ platform_help: {_transport_fails} отказа транспорта подряд "
         f"({where}) — считаю коллекцию недоступной и перестаю ждать "
         f"таймаутов. Перепроверю через {MISSING_RECHECK_SEC} с.", err=True)
    _help_collection_kind = "missing"
    _help_collection_kind_at = time.monotonic()


# Дешёвая проба вместо тяжёлого клиента. qdrant_client на мёртвом адресе
# ретраится внутри себя: замер 15 августа дал 11,8 с на одну перепроверку
# при номинальном таймауте 4 с. Сырой HTTP такого не делает — один заход,
# один таймаут, предсказуемая цена диагноза.
QDRANT_PROBE_TIMEOUT_SEC = int(os.environ.get("HELP_QDRANT_PROBE_TIMEOUT_SEC", "2"))


def _note_transport_success() -> None:
    """Любой удавшийся поход в Qdrant обнуляет счётчик отказов."""
    global _transport_fails
    _transport_fails = 0


def _qdrant_down_now() -> bool:
    """
    Известно ли прямо сейчас, что Qdrant недоступен.

    FAIL-1, вторая половина. Первая правка научила быстро отказывать
    поиск — и только его. Замер 15 августа показал остальное: поиск стал
    отвечать за 30 мс, а platform_help_lookup продолжал платить 3,9 с и
    platform_help_stats 7,8 с на каждый вызов. Они ходят в Qdrant своими
    маршрутами и про кеш формата ничего не знали.

    Чинить надо было не поиск, а вопрос «жив ли Qdrant» — он один на все
    маршруты. Здесь он и живёт.

    True означает: последняя проверка сказала «нет» и окно перепроверки
    ещё не истекло, поэтому в сеть ходить незачем. По истечении окна
    возвращается False — и следующий вызов честно проверит заново.
    """
    if _help_collection_kind != "missing":
        return False
    return (time.monotonic() - _help_collection_kind_at) < MISSING_RECHECK_SEC


def _probe_collection_http() -> "dict | None":
    """
    Один заход к коллекции сырым HTTP. None — Qdrant не ответил.

    Возвращает разобранный `result` коллекции: по нему видно и что сервис
    жив, и сколько в коллекции точек.
    """
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION_NAME}")
        with urllib.request.urlopen(req, timeout=QDRANT_PROBE_TIMEOUT_SEC) as resp:
            return json.loads(resp.read()).get("result") or {}
    except urllib.error.HTTPError as exc:
        # Ответ есть — значит, транспорт жив, просто коллекции нет.
        _note_transport_success()
        logger.debug("коллекция не найдена: %s", exc)
        return {}
    except Exception as exc:
        logger.debug("Qdrant не отвечает: %s", exc)
        return None


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

    # FAIL-1: сначала дешёвая проба. Если Qdrant не отвечает, тяжёлый
    # клиент с его внутренними ретраями трогать незачем — именно он дал
    # 11,8 с на перепроверку при номинальном таймауте 4 с.
    probe = _probe_collection_http()
    if probe is None:
        _note_transport_failure("проба коллекции")
        _help_collection_kind = "missing"
        return _help_collection_kind
    _note_transport_success()

    if (probe.get("points_count") or 0) <= 1:
        # Пусто или только служебная точка с fingerprint.
        _help_collection_kind = "missing"
        return _help_collection_kind

    client = _get_qclient()
    if client is None:
        # Без qdrant_client гибрид не сделать, но данные есть — значит,
        # доступен хотя бы плотный поиск сырым HTTP.
        _help_collection_kind = "legacy_dense"
        return _help_collection_kind

    try:
        info = client.get_collection(COLLECTION_NAME)
    except Exception:
        _note_transport_failure("get_collection")
        _help_collection_kind = "missing"
        return _help_collection_kind
    _note_transport_success()

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
        # FAIL-1: сюда попадают и логические ошибки запроса, и мёртвый
        # транспорт. Различать их по типу исключения ненадёжно (клиент
        # заворачивает всё в свои), поэтому считаем любую неудачу похода в
        # Qdrant — счётчик всё равно обнуляется первым успехом.
        _note_transport_failure("hybrid")
        return None

    _note_transport_success()
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
        # FAIL-1: было 10 с. Локальный контейнер в docker-сети либо отвечает
        # за доли секунды, либо не отвечает вовсе; десять секунд ожидания не
        # улучшают ни один исход, а складываются с таймаутом гибридной ветки
        # в те самые восемь секунд на вызов.
        with urllib.request.urlopen(req, timeout=QDRANT_TIMEOUT_SEC) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        logger.debug(f"legacy dense help search failed: {e}")
        _note_transport_failure("legacy_dense")
        return None

    _note_transport_success()
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


# A-4: сколько раз за жизнь процесса гибрид отваливался на dense-only.
# Само переключение печатается один раз, чтобы не залить лог, а счётчик
# уезжает в platform_help_stats — там его увидит человек, который пришёл
# разбираться, почему выдача стала хуже без единой ошибки в логе.
_degrade_counts = {"hybrid_to_dense": 0, "empty": 0}
_degrade_announced: set[str] = set()


def _announce_degrade(kind: str, message: str) -> None:
    """Первое падение режима говорим громко, дальше только считаем."""
    _degrade_counts[kind] = _degrade_counts.get(kind, 0) + 1
    if kind in _degrade_announced:
        return
    _degrade_announced.add(kind)
    _say(f"  ⚠ platform_help: {message}", err=True)


def _help_search(query: str, limit: int = 10, kind_filter: str = ""):
    """
    Главный диспетчер поиска по platform_help.
    Возвращает (hits, search_type) — search_type: 'hybrid' | 'dense_only' | 'unavailable'

    Берём из Qdrant с запасом: дальше выдача прореживается (дубли чанков
    одной страницы) и перемешивается по объектам, и без запаса результатов
    после прореживания стало бы меньше, чем просил вызывающий.

    A-4: деградация гибрид → dense → пусто происходила молча. Поле
    `search_type` в ответе показывало режим, но его читает модель, а не
    человек; `logger.debug` при штатном уровне логирования не виден.
    Качество выдачи падало, а метрики — нет: eval меряет попадание в топ-k,
    и на простых запросах dense-only справляется.
    """
    fetch = min(max(limit * 3, limit + 10), 100)
    kind = _detect_help_collection_kind()
    if kind == "hybrid":
        hits = _help_search_hybrid(query, fetch, kind_filter)
        if hits is not None:
            return diversify_hits(hits, limit), "hybrid"
        _announce_degrade(
            "hybrid_to_dense",
            "гибридный поиск не отработал, перехожу на dense-only — "
            "выдача станет хуже, ошибки при этом не будет. "
            "Причина в логе уровня DEBUG (LOG_LEVEL=DEBUG).",
        )
    if kind in ("hybrid", "legacy_dense"):
        hits = _help_search_legacy_dense(query, fetch, kind_filter)
        if hits is not None:
            return diversify_hits(hits, limit), "dense_only"
    _announce_degrade(
        "empty",
        f"поиск не вернул ничего (режим коллекции: {kind}) — "
        f"проверьте help-indexer и коллекцию platform_help",
    )
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

    Здесь описано, КАК РАБОТАЕТ платформа. Требования к тому, как положено
    писать код, — в другом сервере: v8std_search / v8std_get_page.
    """
    limit = max(1, min(int(limit), 50))

    hits, mode = _help_search(query, limit, kind or "")

    if not hits:
        # A-3. Фолбэк на встроенную справку из 20 функций.
        #
        # 14 августа коллекция была пуста два часа, и этого не поймал никто:
        # предикат non_empty проходил, поиск по точному имени проходил,
        # галочки стояли. Диагноз занял четыре итерации, причём неверные
        # гипотезы строились именно на «поиск же отвечает».
        #
        # Двадцать функций против индекса на 26 892 страницы — это не режим
        # работы, а авария стенда. Поэтому:
        #   • строка уезжает в stderr, а не только в поле ответа, которое
        #     читает модель, а не человек;
        #   • в ответе стоит degraded: true — по нему ловушка в датасете
        #     отличает «нашлось мало» от «поиска нет».
        bi_hits = _fallback_search(query, limit)
        if bi_hits:
            _announce_degrade(
                "empty",
                f"ОТВЕЧАЮ ВСТРОЕННЫМ СПИСКОМ из {len(set(i['name'] for i in BUILTIN.values()))} "
                f"функций вместо индекса справки. Это авария, а не режим: "
                f"проверьте help-indexer и коллекцию {COLLECTION_NAME}.",
            )
            return json.dumps({
                "search_type": "builtin_fallback",
                "degraded": True,
                # OBS-1: индекса справки нет, отвечаем списком из двадцати
                # встроенных функций. Опираться на это нельзя: отсутствие
                # метода в списке ничего не говорит о платформе.
                "answerable": False,
                "query": query,
                **no_pagination(
                    len(bi_hits), limit,
                    reason=("выдача ранжирована по смыслу: за пределами топа "
                                    "релевантность падает, и следующая страница почти "
                                    "всегда мусор — страниц здесь нет по существу"),
                    instead=("уточните запрос или поднимите limit; для точного "
                                     "имени метода — platform_help_lookup"),
                ),
                "results": bi_hits,
                "note": "Индекс справки недоступен или пуст; показаны 20 встроенных "
                        "функций. Это НЕ полная справка платформы — ответы, "
                        "построенные на этой выдаче, считать неполными. "
                        "Причина: help-indexer не отработал или коллекция пуста.",
            }, ensure_ascii=False, indent=2)
        return json.dumps({
            "search_type": mode,
            # Пустой результат при живом поиске — законный ответ, а при
            # мёртвом — отказ. Отличаются они здесь и больше нигде.
            "degraded": mode == "unavailable",
            # OBS-1: и ровно та же граница по `answerable`. «Ничего не
            # нашлось» — достоверный ответ; «поиск не работает» — нет.
            "answerable": mode != "unavailable",
            "meaning": ("Поиск не работает — это НЕ значит, что в справке "
                        "ничего нет. Про платформу сейчас не известно "
                        "ничего; не отвечай по памяти."
                        if mode == "unavailable" else
                        "Совпадений нет — это достоверный ответ поиска."),
            "query": query,
            "results": [],
            "results_count": 0,
        **no_pagination(
            len([]), limit,
            reason=("выдача ранжирована по смыслу: за пределами топа "
                    "релевантность падает, и следующая страница почти "
                    "всегда мусор — страниц здесь нет по существу"),
            instead=("уточните запрос или поднимите limit; для точного "
                     "имени метода — platform_help_lookup"),
        ),
            "note": ("Поиск недоступен: коллекция пуста или Qdrant не отвечает. "
                     "Проверь help-indexer и platform_help_stats."
                     if mode == "unavailable" else
                     "Поиск отработал, совпадений нет. Попробуй другие слова "
                     "или platform_help_lookup для точного имени."),
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "search_type": mode,  # "hybrid" | "dense_only"
        # FIX-19 научил: поле, по которому вызывающий отличает норму от
        # отказа, должно присутствовать в ОБЕИХ ветках. Иначе проверка на
        # него получает null там, где всё хорошо.
        "degraded": mode != "hybrid",
        # OBS-1: dense-only — выдача беднее обычной, но пригодная. Два
        # поля, два разных вопроса.
        "answerable": True,
        "query": query,
        "filter": {"kind": kind} if kind else None,
        "results_count": len(hits),
        **no_pagination(
            len(hits), limit,
            reason=("выдача ранжирована по смыслу: за пределами топа "
                    "релевантность падает, и следующая страница почти "
                    "всегда мусор — страниц здесь нет по существу"),
            instead=("уточните запрос или поднимите limit; для точного "
                     "имени метода — platform_help_lookup"),
        ),
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

    # FAIL-1: замер 15 августа — 3,9 с на каждый вызов при лежащем Qdrant,
    # тогда как поиск к тому моменту отвечал за 30 мс. Этот маршрут не
    # спрашивал, жив ли сервис, и честно выжидал таймаут снова и снова.
    if _qdrant_down_now():
        return json.dumps({
            "error": "поиск по справке недоступен",
            "degraded": True,
            "answerable": False,
            "meaning": ("Это НЕ значит, что такого метода нет в платформе. "
                        "Индекс справки недоступен — не отвечай по памяти."),
            "found": False,
            "results": [],
            "hint": "Qdrant не отвечает или коллекция пуста; "
                    "проверь help-indexer и platform_help_stats",
        }, ensure_ascii=False)

    client = _get_qclient()
    if client is None:
        return json.dumps({
            "error": "qdrant_client недоступен",
            "degraded": True,
            "answerable": False,
            "meaning": ("Это НЕ значит, что такого метода нет в платформе."),
            "found": False,
            "results": [],
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
            # A-7: без hbk_file пара «контейнер + путь» не собирается, и
            # фильтр связанных чанков ниже вырождается обратно в поиск по
            # одному пути — то есть правка была бы косметической.
            "hbk_file": p.get("hbk_file", ""),
            "file_path": p.get("file_path", ""),
        })

    return json.dumps({
        "lookup": name,
        "parent_filter": parent or None,
        "results_count": len(hits),
        **no_pagination(
            len(hits), limit,
            reason=("выдача ранжирована по смыслу: за пределами топа "
                    "релевантность падает, и следующая страница почти "
                    "всегда мусор — страниц здесь нет по существу"),
            instead=("уточните запрос или поднимите limit; для точного "
                     "имени метода — platform_help_lookup"),
        ),
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
    hbk_file = best.get("hbk_file", "")

    related = {"params": "", "syntax": "", "example": "", "description": ""}
    if file_path:
        client = _get_qclient()
        if client is not None:
            try:
                from qdrant_client import models
                # A-7. Фильтр стоял по одному `file_path`, и это был самый
                # дорогой из трёх его случаев: 11 путей заняты дважды, и на
                # них карточка собирала синтаксис, параметры и пример ЧУЖОЙ
                # страницы — из другого контейнера справки.
                #
                # Наружу это выходило не отказом, а неверным ответом:
                # правдоподобная карточка с параметрами не того метода.
                # Отличить такую от верной по ответу нельзя.
                #
                # Пара «контейнер + путь» уникальна; `hbk_file` лежит в
                # payload с самого начала.
                must = [
                    models.FieldCondition(
                        key="file_path", match=models.MatchValue(value=file_path)
                    )
                ]
                if hbk_file:
                    must.append(models.FieldCondition(
                        key="hbk_file", match=models.MatchValue(value=hbk_file)
                    ))
                fp_filter = models.Filter(must=must)
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
                "hbk_file": c.get("hbk_file", ""),   # A-7: путь без контейнера неоднозначен
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


# OBS-2. Служебная точка id=0, которую пишет hbk_indexer: fingerprint
# корпуса и время индексации. Читается сырым HTTP — qdrant_client здесь не
# нужен, а лишняя зависимость в диагностическом пути только мешает.
FINGERPRINT_POINT_ID = 0


def _read_index_fingerprint() -> dict:
    """
    Чем и когда собран индекс справки.

    Вопрос «почему поиск не находит X» в заходе по PLAN-5 занял четыре
    итерации ровно потому, что ответить на него мог только человек с
    доступом в контейнер. Данные всё это время лежали в коллекции —
    индексатор пишет их в точку id=0, — но наружу не выходили.
    """
    out = {"fingerprint": "", "indexed_at": None, "indexed_at_iso": "",
           "age_hours": None, "error": ""}
    try:
        body = json.dumps({"ids": [FINGERPRINT_POINT_ID], "with_payload": True}).encode()
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    points = (data.get("result") or [])
    if not points:
        out["error"] = ("служебной точки с fingerprint нет — коллекция собрана "
                        "старым индексатором или не до конца")
        return out

    payload = points[0].get("payload") or {}
    out["fingerprint"] = payload.get("fingerprint", "")
    ts = payload.get("indexed_at")
    if isinstance(ts, (int, float)) and ts > 0:
        out["indexed_at"] = int(ts)
        out["indexed_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(ts))
        out["age_hours"] = round((time.time() - ts) / 3600.0, 1)
    return out


@mcp.tool()
def platform_help_stats() -> str:
    """
    Состояние поиска по справке: доступен ли Qdrant, сколько точек в
    коллекции, какая у неё схема, чем и когда собран индекс.

    OBS-2: блок `index` отвечает на вопрос «почему поиск не находит X» без
    раскопок в контейнере — fingerprint корпуса, дата индексации и её
    возраст. Свежий индекс с чужим fingerprint означает, что справка
    собрана другим набором .hbk или другой версией пайплайна разбора.
    """
    # FAIL-1: 7,8 с на вызов при лежащем Qdrant — два таймаута подряд
    # (_qdrant_available и чтение fingerprint). Диагностический инструмент,
    # который сам висит на восемь секунд, приходит на помощь последним.
    if _qdrant_down_now():
        return json.dumps({
            "qdrant_available": False,
            "qdrant_url": QDRANT_URL,
            "platform_help_collection": COLLECTION_NAME,
            "platform_help_points": 0,
            "platform_help_collection_kind": "missing",
            "degraded": True,
            "answerable": False,
            "platform_help_search_mode": "unavailable",
            "collection_kind_checked_ago_sec": round(
                time.monotonic() - _help_collection_kind_at, 1),
            "index": {"fingerprint": "", "indexed_at": None,
                      "indexed_at_iso": "", "age_hours": None,
                      "error": "Qdrant недоступен"},
            "degradations": dict(_degrade_counts),
            "transport_fails_in_a_row": _transport_fails,
            "qdrant_timeout_sec": QDRANT_TIMEOUT_SEC,
            "note": ("Qdrant признан недоступным, в сеть не ходим ещё "
                     f"{max(0, round(MISSING_RECHECK_SEC - (time.monotonic() - _help_collection_kind_at)))} с. "
                     "Это кешированный ответ, а не свежая проверка."),
            "embedding_model": EMBEDDING_MODEL_NAME,
            "bm25_model": BM25_MODEL_NAME,
            # Состояние моделей от Qdrant не зависит: они грузятся из
            # образа. В быстрой ветке отказа оно тем более уместно —
            # именно здесь выясняют, что вообще происходит.
            "warmup": _models.state(),
            "fallback_items": len(set(i["name"] for i in BUILTIN.values())),
        }, ensure_ascii=False, indent=2)

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
    index = _read_index_fingerprint() if qdrant_ok else {
        "fingerprint": "", "indexed_at": None, "indexed_at_iso": "",
        "age_hours": None, "error": "Qdrant недоступен",
    }

    # A-3/A-4: degraded — одно поле, по которому видно, что поиск работает
    # не в полную силу. Считается здесь, а не выводится читателем из трёх
    # других полей: выводить приходилось человеку, и он выводил неверно.
    degraded = (not qdrant_ok) or help_kind != "hybrid" or points_count <= 1

    return json.dumps({
        "qdrant_available": qdrant_ok,
        "qdrant_url": QDRANT_URL,
        "platform_help_collection": COLLECTION_NAME,
        "platform_help_points": points_count,
        "platform_help_collection_kind": help_kind,  # hybrid | legacy_dense | missing
        "degraded": degraded,
        # FIX-12: сколько секунд назад определён формат. Без этого по
        # ответу "unavailable" не отличить лежащий Qdrant от кеша,
        # который ещё не перепроверялся.
        "collection_kind_checked_ago_sec": round(time.monotonic() - _help_collection_kind_at, 1),
        "platform_help_search_mode": (
            "hybrid (dense + BM25 + RRF)" if help_kind == "hybrid"
            else ("dense only (legacy)" if help_kind == "legacy_dense" else "unavailable")
        ),
        # OBS-2
        "index": index,
        # A-4: сколько раз за жизнь процесса поиск сваливался в режим хуже
        # штатного. Ненулевые числа здесь объясняют «выдача стала хуже, а
        # ошибок нет».
        "degradations": dict(_degrade_counts),
        # FAIL-1: отказы транспорта подряд. Ненулевое значение при
        # работающем поиске означает, что Qdrant отвечает через раз.
        "transport_fails_in_a_row": _transport_fails,
        "qdrant_timeout_sec": QDRANT_TIMEOUT_SEC,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "bm25_model": BM25_MODEL_NAME,
        "model_loaded": model is not None,
        # PERF-6: из чего складывается цена первого запроса и на каком она
        # этапе прямо сейчас. Без этого «двадцать три секунды» — всё, что
        # знал наблюдатель, а выбирать по такому числу, что чинить, нельзя.
        "warmup": _models.state(),
        "fallback_items": len(set(i["name"] for i in BUILTIN.values())),
    }, ensure_ascii=False, indent=2)


# ─── Инициализация ────────────────────────────────────────────────────────

_load_builtin()


# FIX-14 + PERF-6: прогрев моделей в фоне при старте.
#
# FIX-14 завёл прогрев, потому что первый запрос после рестарта стоил
# 31 950 мс против 120–700 мс у остальных — тридцать секунд тишины,
# достаточных, чтобы агент счёл инструмент неотвечающим и пошёл отвечать по
# памяти. Ровно тот исход, ради предотвращения которого весь набор строился.
#
# PERF-6 добавил к этому две вещи, без которых прогрев работал вполсилы:
#
#   • загрузка стала происходить один раз (см. комментарий у _models выше);
#     до этого прогрев и первый запрос грузили по копии одновременно;
#   • греется не только dense, но и BM25. Поиск гибридный и зовёт их
#     подряд, так что загрузка sparse целиком лежала на первом запросе — и
#     не была видна за спиной более крупной проблемы.
#
# Греем в фоновом потоке (daemon), а не синхронно: сервер должен принимать
# соединения сразу, иначе healthcheck не дождётся старта. Запрос, пришедший
# во время прогрева, теперь ЖДЁТ его, а не запускает вторую загрузку.
#
# Отключается HELP_WARMUP=0: при отладке лишняя загрузка модели ни к чему.

if os.environ.get("HELP_WARMUP", "1").strip().lower() not in ("0", "false", "no"):
    _models.start_background(_say)
