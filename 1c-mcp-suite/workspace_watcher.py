"""
Workspace watcher — задача 2.3, режим git — SRC-9.

Два режима (WATCHER_MODE):

  git  — раз в SOURCE_POLL_SEC делает ТАКТ: спрашивает git, сдвинулся ли
         HEAD, и проверяет файлы, которые агент правил поверх коммита
         (source_sync.sync). Читаются .git и несколько конкретных файлов —
         миллисекунды, поэтому включённый watcher почти ничего не стоит.
         Ровно то же делает ручной запуск (apply_changes.py,
         metadata_refresh()) — логика одна.
  poll — прежний режим ниже: watchdog опрашивает каталог, каждый цикл —
         полный обход выгрузки. Для каталога без git.
  auto — git, если выгрузка в git-репозитории, иначе poll.

Главный выключатель — WATCHER_ENABLED, по умолчанию false.

Следит за изменениями BSL-кода и XML-метаданных и инкрементально обновляет
граф Neo4j. Работает поверх уже существующего MCP-сервера:

  • .bsl / .os / .xml → mcp-metadata-graph: tools `metadata_upsert_file` /
                        `metadata_remove_file`. Включается через
                        METADATA_WATCH_ENABLED=true.

HYG-4: здесь был второй адресат — `mcp-code-rag` (коллекция Qdrant по коду).
Сервер удалён из проекта вместе с пятью другими в Заходе 2, поэтому ветка
`target='code'` убрана целиком, а не оставлена под выключенным флагом.
Выключенный флаг к несуществующему сервису читается как недоделка и
заставляет каждого следующего читателя выяснять, чего тут не хватает;
ответ — ничего, решение принято и исполнено. Если код-RAG когда-нибудь
вернётся, вернуть надо будет сервер, а не эти девять строк.

Из-за того же решения `target` у события стал единственным. Поле оставлено:
дедупликация ведётся по паре `(path, target)`, и это правильный ключ на
случай второго адресата — но fan-out одного события в два сервиса сейчас
не используется.

ВАЖНО про рентабельность. На боевой выгрузке (56 410 файлов) слежение
оказалось невыгодным: события с диска Windows в контейнер не доходят,
watcher работает опросом, один цикл опроса = полный обход каталога, то есть
те же минуты, что и явный запуск обновления. В `.env` он выключен
(WATCHER_ENABLED=false), и это осознанно — см. раздел про обновление
выгрузки в README.

Ключевые свойства:
  • Debounce: серия событий по одному файлу (IDE сохраняет → linter → formatter
    → IDE снова сохраняет) схлопывается в одну переиндексацию.
  • Очередь с дедупликацией по пути: только последнее событие на файл имеет
    значение. Обработка строго последовательная, чтобы не плодить параллельных
    embed-запросов и не ловить гонки в Qdrant.
  • Пропускаем скрытые файлы, временные файлы редакторов (~, .swp, .tmp, #)
    и мусор типа .git/, node_modules/, __pycache__/.
  • Kill-switch через WATCHER_ENABLED=false — контейнер стартует, печатает
    сообщение и спит. Не использует CPU.

Подключение к MCP-серверам — тот же способ, что в code_reindex_trigger.py:
штатный SSE-клиент из пакета `mcp`. Соединения открываются на каждый вызов
(короткие, дешёвые) — это проще и надёжнее, чем держать долгоживущий pipe.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

# ─── Конфиг ──────────────────────────────────────────────────────────────

WATCHER_ENABLED = os.environ.get("WATCHER_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

CODE_DIR = os.environ.get("WATCH_CODE_DIR", "/workspace")
XML_DIR = os.environ.get("WATCH_XML_DIR", "/data/1c-src")

# TR-1: транспорт Streamable HTTP, эндпоинт /mcp. Старые имена переменных
# (*_SSE_URL) читаются как запасной вариант, чтобы не ломать чужие .env,
# но /sse у серверов больше нет — путь надо поправить.
METADATA_GRAPH_URL = os.environ.get(
    "METADATA_GRAPH_URL",
    os.environ.get("METADATA_GRAPH_SSE_URL", "http://mcp-metadata-graph:8001/mcp"),
)

# Apдейт Neo4j-графа (слой 1 .xml + слой 2 .bsl) — включается этим флагом.
# До задачи 4.6.5 (май 2026) tools metadata_upsert_file/metadata_remove_file
# не было, поэтому дефолт по-прежнему false — для совместимости со стариками,
# у кого образ ещё не пересобран. На свежем образе можно безопасно ставить
# true: и .bsl, и .xml будут синхронить call graph и схему данных в Neo4j.
#
# ВАЖНО про пути: если METADATA_WATCH_ENABLED=true и CODE_DIR≠XML_DIR,
# .bsl-событие из CODE_DIR будет проброшено в mcp-metadata-graph с тем же
# абсолютным путём. Сервер ожидает путь внутри своего METADATA_SRC_DIR, и
# если они расходятся — упсёрт молча скипнется (status=skipped,
# reason=path_outside_src_root в логе watcher'а). Для bsl-watch'а в Neo4j
# поднимайте WATCH_CODE_DIR=METADATA_SRC_DIR (одна точка монтирования).
METADATA_WATCH_ENABLED = os.environ.get(
    "METADATA_WATCH_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")

DEBOUNCE_SEC = float(os.environ.get("WATCHER_DEBOUNCE_SEC", "3.0"))
INITIAL_DELAY_SEC = float(os.environ.get("WATCHER_INITIAL_DELAY", "20"))
MCP_CALL_TIMEOUT_SEC = float(os.environ.get("WATCHER_MCP_TIMEOUT", "120"))

# На Windows / Docker Desktop / WSL2 / сетевых FS bind-mount'ов inotify-
# события с хоста не проходят в контейнер. Тогда нужен polling: watchdog
# периодически пересканирует дерево и вычисляет разницу. Медленнее, но
# работает везде. Дефолт — true, потому что большинство пользователей
# на Windows/Mac, и им "работает из коробки" важнее ±2% CPU.
USE_POLLING = os.environ.get("WATCHER_USE_POLLING", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
POLLING_INTERVAL_SEC = float(os.environ.get("WATCHER_POLLING_INTERVAL", "2.0"))

CODE_EXTENSIONS = {".bsl", ".os"}
XML_EXTENSIONS = {".xml"}

# Технические файлы/каталоги, которые никогда не триггерят индексацию.
IGNORED_DIR_PARTS = {
    ".git", ".svn", ".hg", ".idea", ".vscode",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
}
IGNORED_FILE_PREFIXES = ("~", "#", ".#")
IGNORED_FILE_SUFFIXES = (".swp", ".swx", ".tmp", ".bak", "~")


def _log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


def _should_ignore(path: Path) -> bool:
    """Фильтрует служебные пути, которые не должны триггерить индексацию."""
    parts = set(path.parts)
    if parts & IGNORED_DIR_PARTS:
        return True
    name = path.name
    if name.startswith(IGNORED_FILE_PREFIXES):
        return True
    if name.endswith(IGNORED_FILE_SUFFIXES):
        return True
    return False


# ─── Типы событий ────────────────────────────────────────────────────────

@dataclass
class PendingEvent:
    """
    Одно ожидающее обработки событие.
    kind: 'upsert' (модификация/создание) или 'remove' (удаление/уход).
    last_seen: последний момент, когда что-то пришло по этому пути — от него
               считается debounce.
    target:    'metadata' (mcp-metadata-graph). Других адресатов сейчас нет
               (HYG-4), но ключ дедупликации остаётся парой — см. ниже.
    """
    path: str
    kind: str
    target: str
    last_seen: float = field(default_factory=time.monotonic)


# ─── Очередь с дедупликацией ─────────────────────────────────────────────

class DebouncedQueue:
    """
    Мини-планировщик: события по одному пути схлопываются в одно,
    тип последнего события (upsert/remove) побеждает.

    Поток-обработчик в фоне забирает события, у которых last_seen старше
    DEBOUNCE_SEC, и передаёт их в callback. Всё под одним lock'ом —
    нагрузка мизерная (единицы событий/сек), гоняться за lock-free нет
    смысла.

    Ключ дедупликации — `(path, target)`, НЕ `path` в одиночку. Адресат
    сейчас один (HYG-4 убрал code-rag), так что пара избыточна; оставлена
    намеренно — второй адресат означал бы два независимо дебаунсимых
    события на один файл, и ключ по одному лишь пути их бы схлопнул.
    """

    def __init__(self, debounce_sec: float, handler):
        self._debounce = debounce_sec
        self._handler = handler  # sync-функция PendingEvent -> None
        self._pending: dict[tuple[str, str], PendingEvent] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="watcher-dispatch", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, event: PendingEvent) -> None:
        key = (event.path, event.target)
        with self._lock:
            existing = self._pending.get(key)
            if existing is None:
                self._pending[key] = event
            else:
                # Тип последнего события побеждает: если файл был
                # modified, а потом deleted — итог remove.
                existing.kind = event.kind
                existing.last_seen = event.last_seen

    def _loop(self) -> None:
        poll_interval = max(0.2, self._debounce / 4)
        while not self._stop.is_set():
            now = time.monotonic()
            ready: list[PendingEvent] = []
            with self._lock:
                for key, ev in list(self._pending.items()):
                    if now - ev.last_seen >= self._debounce:
                        ready.append(ev)
                        del self._pending[key]
            for ev in ready:
                try:
                    self._handler(ev)
                except Exception as e:
                    _log(f"handler error for {ev.path}: {type(e).__name__}: {e}")
            self._stop.wait(poll_interval)


# ─── MCP вызовы ──────────────────────────────────────────────────────────

async def _call_tool(url: str, tool_name: str, arguments: dict) -> Optional[str]:
    """
    Открывает одноразовое Streamable HTTP-соединение, вызывает tool и
    возвращает текстовое содержимое ответа (конкатенация text-блоков) либо
    None, если tool вернул isError=True / случилась ошибка транспорта.

    Таймаут — чтобы зависший MCP-сервер не подвесил watcher.
    """
    try:
        async with asyncio.timeout(MCP_CALL_TIMEOUT_SEC):
            # Задача 3.2: клиентские headers с общим секретом.
            try:
                from mcp_auth import build_client_headers
                client_headers = build_client_headers()
            except Exception:
                client_headers = {}
            # streamablehttp_client отдаёт третьим элементом колбэк получения
            # session id — в stateless-режиме он не нужен.
            async with streamablehttp_client(url, headers=client_headers) as (
                read_stream, write_stream, _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    texts = []
                    for block in result.content:
                        t = getattr(block, "text", None)
                        if t is not None:
                            texts.append(t)
                    if getattr(result, "isError", False):
                        _log(f"{tool_name} isError: {' | '.join(texts)[:300]}")
                        return None
                    return "\n".join(texts)
    except asyncio.TimeoutError:
        _log(f"{tool_name} timed out after {MCP_CALL_TIMEOUT_SEC}s")
        return None
    except Exception as e:
        _log(f"{tool_name} call failed: {type(e).__name__}: {e}")
        return None


def _dispatch(event: PendingEvent) -> None:
    """
    Синхронная обёртка: отправляет асинхронный вызов в выделенный
    долгоживущий event loop и блокируется на ожидании результата.

    Почему не asyncio.run() на каждое событие: asyncio.run() создаёт
    свежий event loop каждый вызов, а это на практике на Docker Desktop
    под Windows/Mac порождает трудно диагностируемые зависания в связке
    httpx+SSE — прямой вызов той же tool отдаёт ответ за 400 мс, а
    asyncio.run() в фоновом потоке watcher'а — виснет до таймаута.
    Один стабильный loop на отдельном треде устраняет это начисто.
    """
    if event.target == "metadata":
        tool = "metadata_upsert_file" if event.kind == "upsert" else "metadata_remove_file"
        url = METADATA_GRAPH_URL
    else:
        _log(f"unknown target {event.target!r} for {event.path}")
        return

    _log(f"{event.kind} {event.target}: {event.path} → {tool}")

    loop = _ASYNC_LOOP.loop
    future = asyncio.run_coroutine_threadsafe(
        _call_tool(url, tool, {"filepath": event.path}), loop
    )
    try:
        # MCP_CALL_TIMEOUT_SEC уже обеспечивается внутри _call_tool,
        # тут — просто защита на случай, если результат не прилетит
        # в future (например, loop умер). Берём +10с запаса.
        response = future.result(timeout=MCP_CALL_TIMEOUT_SEC + 10)
    except Exception as e:
        _log(f"  ✗ dispatch error: {type(e).__name__}: {e}")
        return

    if response:
        _log(f"  ← {_summarize_response(response)}")


def _summarize_response(response: str) -> str:
    """
    Вытягивает из JSON-ответа MCP-сервера однострочное резюме для лога.
    MCP-сервер отдаёт json.dumps(..., indent=2), поэтому просто взять
    первую строку нельзя — это будет голая скобка. Парсим полноценно
    и собираем ключевые поля. Если это не JSON — возвращаем первые 200
    символов как есть.
    """
    try:
        import json as _json
        data = _json.loads(response)
    except Exception:
        return response.strip().replace("\n", " ")[:200]

    if not isinstance(data, dict):
        return str(data)[:200]

    status = data.get("status", "?")
    parts = [f"status={status}"]
    # Показываем поля, если они есть и несут смысл (ненулевые / непустые).
    for key in ("file", "chunks_indexed", "errors", "reason"):
        if key not in data:
            continue
        value = data[key]
        # chunks_indexed показываем всегда (даже 0 — это информация).
        # Остальные — только если непустые.
        if key == "chunks_indexed" or value not in (None, "", 0):
            parts.append(f"{key}={value}")
    if data.get("delete_error"):
        parts.append(f"delete_error={data['delete_error']}")
    return ", ".join(parts)[:300]


class AsyncLoop:
    """
    Держит asyncio event loop в отдельном демон-потоке на всё время
    жизни процесса. Все MCP-вызовы отправляются сюда через
    run_coroutine_threadsafe.
    """

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="watcher-async-loop", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)
        if self.loop is None:
            raise RuntimeError("async loop не стартовал за 5 секунд")

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass


# Глобальный экземпляр (инициализируется в main()).
_ASYNC_LOOP: "AsyncLoop" = AsyncLoop()


# ─── Watchdog handler ────────────────────────────────────────────────────

class CodeXmlHandler(FileSystemEventHandler):
    """Преобразует raw-события watchdog в PendingEvent'ы нашей очереди."""

    def __init__(self, queue: DebouncedQueue):
        self._q = queue

    # watchdog отдельно вызывает created/modified/deleted/moved. Для всех
    # кроме moved логика одинакова, moved разбираем в два события.

    def on_created(self, event: FileSystemEvent) -> None:  # noqa: D401
        if event.is_directory:
            return
        self._enqueue_path(event.src_path, kind="upsert")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue_path(event.src_path, kind="upsert")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._enqueue_path(event.src_path, kind="remove")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Старый путь → удалить, новый → переиндексировать.
        self._enqueue_path(event.src_path, kind="remove")
        dest = getattr(event, "dest_path", None)
        if dest:
            self._enqueue_path(dest, kind="upsert")

    def _enqueue_path(self, raw_path: str, *, kind: str) -> None:
        path = Path(raw_path)
        if _should_ignore(path):
            return
        ext = path.suffix.lower()
        # Решаем, в какие таргеты слать событие. Адресат один:
        # metadata-graph (слой 1 из .xml, слой 2 из .bsl) при
        # METADATA_WATCH_ENABLED.
        targets: list[str] = []
        if (ext in CODE_EXTENSIONS or ext in XML_EXTENSIONS) \
                and METADATA_WATCH_ENABLED:
            targets.append("metadata")
        if not targets:
            return
        # Нормализуем к POSIX: внутри контейнера это no-op (Path всегда POSIX
        # на Linux), но на Windows-разработчике Path('/ws/X.bsl') в str()
        # отдаёт '\\ws\\X.bsl', и MCP-tool на той стороне получает не-POSIX.
        # Отправляем строго POSIX, контракт с server-side прозрачнее.
        posix_path = path.as_posix()
        now = time.monotonic()
        for target in targets:
            self._q.enqueue(PendingEvent(
                path=posix_path,
                kind=kind,
                target=target,
                last_seen=now,
            ))


# ─── SRC-9: режим git ────────────────────────────────────────────────────

WATCHER_MODE = os.environ.get("WATCHER_MODE", "auto").strip().lower()
POLL_SEC_MIN, POLL_SEC_MAX = 30, 3600
EX_CONFIG = 78


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_poll_sec(raw: Optional[str]) -> float:
    """
    SOURCE_POLL_SEC → секунды. Вне 30…3600 — ошибка, а не молчаливая
    подстановка: урок WATCHER_POLLING_INTERVAL, который из .env не доходил
    до контейнера, и опрос шёл впятеро чаще обещанного без единого слова.
    """
    raw = (raw or "60").strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"SOURCE_POLL_SEC={raw!r}: нужно число секунд") from None
    if not POLL_SEC_MIN <= value <= POLL_SEC_MAX:
        raise ValueError(f"SOURCE_POLL_SEC={raw}: допустимо от {POLL_SEC_MIN} "
                         f"до {POLL_SEC_MAX} секунд (60 — раз в минуту, "
                         f"300 — раз в 5 минут)")
    return value


_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}


def parse_schedule(raw: Optional[str], *, weekly: bool) -> Optional[tuple]:
    """
    «ЧЧ:ММ» (ежедневно) или «день ЧЧ:ММ» (еженедельно; mon…sun или
    пн…вс) → `(день_недели | None, часы, минуты)`. Пусто — None
    (расписание выключено). Ошибка формата — ValueError.
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    parts = raw.split()
    day = None
    if len(parts) == 2:
        if parts[0] not in _WEEKDAYS:
            raise ValueError(f"{raw!r}: день недели — mon…sun или пн…вс")
        day = _WEEKDAYS[parts[0]]
        hm = parts[1]
    elif len(parts) == 1:
        if weekly:
            raise ValueError(f"{raw!r}: нужен день недели, например «sat 02:00»")
        hm = parts[0]
    else:
        raise ValueError(f"{raw!r}: формат «ЧЧ:ММ» или «день ЧЧ:ММ»")
    try:
        h, m = (int(x) for x in hm.split(":"))
    except ValueError:
        raise ValueError(f"{raw!r}: время — ЧЧ:ММ") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"{raw!r}: время вне суток")
    return day, h, m


# Через сколько повторять неудавшуюся полную индексацию.
FULL_RETRY_SEC = 6 * 3600

# Сколько после назначенного времени задачу ещё можно догнать (машина была
# выключена в 03:00 — сверка пройдёт, когда включат, но не через неделю).
SCHEDULE_GRACE_SEC = 12 * 3600


def last_slot(spec: tuple, now: float) -> float:
    """Последний назначенный момент не позже `now` (локальное время)."""
    day, h, m = spec
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if day is None:
        return base if base <= now else base - 86400
    back = (lt.tm_wday - day) % 7
    slot = base - back * 86400
    return slot if slot <= now else slot - 7 * 86400


def is_due(spec: Optional[tuple], now: float, last_run: float) -> bool:
    """Пора ли: назначенный момент прошёл, после него не запускали, и он не
    слишком давно (SCHEDULE_GRACE_SEC)."""
    if spec is None:
        return False
    slot = last_slot(spec, now)
    return last_run < slot and now - slot <= SCHEDULE_GRACE_SEC


class _Quiet:
    """Повтор одной и той же строки не чаще раза в `period` секунд."""

    def __init__(self, period: float = 3600.0):
        self.period = period
        self._last: dict = {}

    def say(self, key: str, msg: str) -> None:
        now = time.monotonic()
        prev = self._last.get(key)
        if prev is None or prev[0] != msg or now - prev[1] >= self.period:
            _log(msg)
            self._last[key] = (msg, now)


def _run_full_index(reason: str) -> int:
    """
    Полная индексация тем же индексером, что и контейнер metadata-indexer
    (образ один). Аренду индексер берёт сам; такты в это время отвечают
    busy и пропускаются.
    """
    import subprocess
    _log(f"полная индексация: {reason}")
    env = dict(os.environ, METADATA_FORCE_REINDEX="true")
    t0 = time.monotonic()
    rc = subprocess.call([sys.executable, "/app/mcp_metadata_indexer.py"], env=env)
    _log(f"полная индексация завершилась с кодом {rc} за "
         f"{(time.monotonic() - t0) / 60:.1f} мин")
    return rc


def _main_git(ctx) -> int:
    from graph_writer import Neo4j
    from source_state import Neo4jStore
    from source_sync import Neo4jGraphOps, max_partial_from_env, sync

    try:
        poll_sec = parse_poll_sec(os.environ.get("SOURCE_POLL_SEC"))
        reconcile_spec = parse_schedule(os.environ.get("SOURCE_RECONCILE_AT"),
                                        weekly=False)
        full_spec = parse_schedule(os.environ.get("SOURCE_FULL_REINDEX_AT"),
                                   weekly=True)
    except ValueError as e:
        _log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        _log("watcher не запущен — исправьте .env и пересоздайте контейнер: "
             "docker compose up -d --no-deps --force-recreate workspace-watcher")
        return EX_CONFIG
    auto_full = _env_bool("SOURCE_AUTO_FULL", False)
    reconcile_on_flag = _env_bool("SOURCE_RECONCILE_ON_FLAG", True)
    max_partial = max_partial_from_env()

    _log(f"mode: GIT (interval={poll_sec:.0f}s) — {ctx.reason}")
    _log(f"  сверка: {os.environ.get('SOURCE_RECONCILE_AT') or 'по флагу needs_reconcile'}"
         f"{'' if reconcile_on_flag else ' (по флагу — выключено)'}; "
         f"плановая полная: {os.environ.get('SOURCE_FULL_REINDEX_AT') or 'выкл.'}; "
         f"сама при needs_full: {'да' if auto_full else 'нет, только напоминает'}")

    neo = Neo4j(os.environ.get("NEO4J_URL", "http://neo4j:7474"),
                os.environ.get("NEO4J_USER", "neo4j"),
                os.environ.get("NEO4J_PASSWORD", ""),
                timeout=float(os.environ.get("NEO4J_TIMEOUT_SEC", "600")))
    neo.wait(timeout=300)
    store = Neo4jStore(neo)
    store.ensure_schema()
    ops = Neo4jGraphOps(neo, ctx.src_root)
    quiet = _Quiet()
    last_alive = 0.0
    # Неудачная полная индексация не пишет last_full_at, и без этой
    # отметки watcher перезапускал бы её на каждом такте всё окно догона —
    # по два часа подряд. Повтор — не раньше, чем через FULL_RETRY_SEC.
    full_attempt = 0.0

    _log("watcher запущен, жду изменений...")
    while True:
        t0 = time.monotonic()
        now = time.time()
        try:
            state = store.read()
            reconcile = False
            if state is not None:
                want_full = is_due(full_spec, now, state.last_full_at) or \
                    (auto_full and state.needs_full)
                if want_full and now - full_attempt >= FULL_RETRY_SEC:
                    why = ("по расписанию" if not state.needs_full
                           else f"needs_full: {state.needs_full_reason}")
                    full_attempt = now
                    if _run_full_index(why) != 0:
                        _log(f"полная индексация не удалась — повтор не раньше "
                             f"чем через {FULL_RETRY_SEC / 3600:.0f} ч")
                    continue
                reconcile = is_due(reconcile_spec, now, state.last_reconcile_at) \
                    or (reconcile_on_flag and state.needs_reconcile)
            if reconcile:
                _log("сверка рабочей копии (git status)…")
            res = sync(ctx, store, ops, owner="watcher", reconcile=reconcile,
                       max_partial=max_partial)
            took = time.monotonic() - t0
            if res.status == "ok":
                quiet._last.clear()
                applied = res.applied.summary() if res.applied else {}
                _log(f"HEAD {res.indexed_before[:10]}→{res.head[:10]}: "
                     f"файлов {len(res.planned)}, "
                     f"{applied.get('by_action', {})}, dirty {res.dirty_count}, "
                     f"за {took:.1f} с")
            elif res.status == "needs_full":
                quiet.say("needs_full", res.message + (
                    "" if auto_full else
                    " — запустите: .\\scripts\\graph-refresh.ps1 -Full "
                    "(или SOURCE_AUTO_FULL=true)"))
            elif res.status in ("busy", "no_state", "error"):
                quiet.say(res.status, f"{res.status}: {res.message}")
            if reconcile and res.ok:
                _log(f"сверка завершена за {took:.1f} с, dirty {res.dirty_count}")
            if time.monotonic() - last_alive >= 3600:
                _log(f"жив: HEAD {res.head[:10] or '—'}, dirty {res.dirty_count}, "
                     f"статус {res.status}")
                last_alive = time.monotonic()
        except Exception as e:  # noqa: BLE001 — такт упал, watcher жив
            quiet.say("exception", f"такт упал: {type(e).__name__}: {e}")
        time.sleep(max(1.0, poll_sec - (time.monotonic() - t0)))


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    if not WATCHER_ENABLED:
        _log("WATCHER_ENABLED=false — watcher отключён, идём в простой sleep-loop")
        _log("  ручная доиндексация работает и так: .\\scripts\\graph-refresh.ps1 "
             "или metadata_refresh() из чата")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    _log(f"initial delay {INITIAL_DELAY_SEC:.0f}s (даём MCP-серверам подняться)")
    time.sleep(INITIAL_DELAY_SEC)

    if WATCHER_MODE not in ("auto", "git", "poll"):
        _log(f"ОШИБКА КОНФИГУРАЦИИ: WATCHER_MODE={WATCHER_MODE!r} — "
             f"допустимо auto | git | poll")
        return EX_CONFIG
    if WATCHER_MODE in ("auto", "git"):
        try:
            sys.path.insert(0, "/app")
            from source_sync import context_from_env
            ctx = context_from_env()
        except Exception as e:  # noqa: BLE001
            if WATCHER_MODE == "git":
                _log(f"ОШИБКА: WATCHER_MODE=git, но источник не определён: {e}")
                return EX_CONFIG
            _log(f"режим git недоступен ({e}) — перехожу в poll")
            ctx = None
        if ctx is not None and ctx.is_git:
            try:
                return _main_git(ctx)
            except KeyboardInterrupt:
                return 0
        if WATCHER_MODE == "git":
            _log(f"ОШИБКА: WATCHER_MODE=git, но выгрузка не в git: "
                 f"{ctx.reason if ctx else '—'}")
            return EX_CONFIG
        _log(f"выгрузка не в git ({ctx.reason if ctx else '—'}) — режим poll: "
             f"каждый цикл = полный обход каталога (HYG-7)")
    return _main_poll()


def _main_poll() -> int:
    """Прежний режим: watchdog опрашивает каталог (задача 2.3 + 4.6.5)."""

    code_path = Path(CODE_DIR)
    xml_path = Path(XML_DIR)

    _log(f"config: debounce={DEBOUNCE_SEC}s, mcp_timeout={MCP_CALL_TIMEOUT_SEC}s")
    _log(f"code dir: {CODE_DIR} (exists={code_path.is_dir()})")
    _log(f"xml dir:  {XML_DIR} (exists={xml_path.is_dir()}, enabled={METADATA_WATCH_ENABLED})")
    if METADATA_WATCH_ENABLED:
        _log(f"metadata-graph: {METADATA_GRAPH_URL}")
    else:
        # Иначе watcher поднимается, крутит опрос и не делает НИЧЕГО — а в
        # логе про это ни строки. Молчаливый холостой ход в этом проекте
        # уже разбирали (FIX-3): состояние надо называть вслух.
        _log("METADATA_WATCH_ENABLED=false — адресатов нет, события никуда "
             "не отправляются. Watcher будет крутить опрос вхолостую.")

    # Долгоживущий event loop для всех MCP-вызовов из фонового потока
    # DebouncedQueue. Поднимаем ДО очереди, чтобы он точно был готов
    # к моменту первого dispatch.
    _ASYNC_LOOP.start()

    queue = DebouncedQueue(DEBOUNCE_SEC, _dispatch)
    queue.start()

    if USE_POLLING:
        _log(f"mode: POLLING (interval={POLLING_INTERVAL_SEC}s) — совместимо с "
             "Docker Desktop on Windows/Mac и сетевыми FS")
        observer = PollingObserver(timeout=POLLING_INTERVAL_SEC)
    else:
        _log("mode: INOTIFY (нативные события ФС) — только для Linux-хостов")
        observer = Observer()

    handler = CodeXmlHandler(queue)

    watched_any = False
    if code_path.is_dir():
        observer.schedule(handler, str(code_path), recursive=True)
        watched_any = True
    else:
        _log(f"⚠ код-директория {CODE_DIR} не существует — BSL watch отключён")

    if METADATA_WATCH_ENABLED:
        if xml_path.is_dir():
            observer.schedule(handler, str(xml_path), recursive=True)
            watched_any = True
        else:
            _log(f"⚠ XML-директория {XML_DIR} не существует — metadata watch отключён")

    if not watched_any:
        _log("нечего наблюдать — выходим")
        return 1

    observer.start()
    _log("watcher запущен, жду изменений...")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _log("SIGINT, завершаемся")
    finally:
        observer.stop()
        observer.join(timeout=5)
        queue.stop()
        _ASYNC_LOOP.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
