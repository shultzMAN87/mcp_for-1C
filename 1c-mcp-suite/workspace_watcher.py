"""
Workspace watcher — задача 2.3.

Следит за изменениями BSL-кода и XML-метаданных и инкрементально обновляет
RAG-коллекции. Работает поверх уже существующих MCP-серверов через SSE:

  • .bsl / .os   → mcp-code-rag:   tools `code_reindex_file` / `code_remove_file`
  • .xml         → mcp-metadata-graph: tools `metadata_upsert_file` /
                   `metadata_remove_file` (задача 4.6.5). Включается через
                   METADATA_WATCH_ENABLED=true; до 4.6.5 эти tools отсутствовали,
                   поэтому флаг по умолчанию выключен — оставлен на случай,
                   если пользователь захочет включить XML-watch без BSL.
                   Сам BSL-апдейт идёт через mcp-code-rag (для Qdrant) И
                   при необходимости — через эти же metadata-tools (для Neo4j-
                   графа); чтобы получить полную картину в графе, поднимите
                   METADATA_WATCH_ENABLED=true и расширьте CODE_EXTENSIONS
                   привязку (см. ниже).

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
CODE_RAG_URL = os.environ.get(
    "CODE_RAG_URL", os.environ.get("CODE_RAG_SSE_URL", "http://mcp-code-rag:8011/mcp")
)
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
# CODE_WATCH_ENABLED: фанаут .bsl-событий в mcp-code-rag (Qdrant).
# Сервер mcp-code-rag удалён из проекта, поэтому по умолчанию выключено —
# иначе watcher на каждое сохранение файла долбится в несуществующий
# контейнер и пишет в лог таймауты. Вернуть в true вместе с сервером.
CODE_WATCH_ENABLED = os.environ.get(
    "CODE_WATCH_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")

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
    target:    'code' (mcp-code-rag) | 'metadata' (mcp-metadata-graph).
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

    Ключ дедупликации — `(path, target)`, НЕ `path` в одиночку. Это нужно
    для fan-out'а одного .bsl-события в два таргета (code-rag для Qdrant
    + metadata-graph для Neo4j после 4.6.5): они должны жить как два
    независимо дебаунсимых события на один файл.
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
    if event.target == "code":
        tool = "code_reindex_file" if event.kind == "upsert" else "code_remove_file"
        url = CODE_RAG_URL
    elif event.target == "metadata":
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
        # Решаем, в какие таргеты слать событие.
        # .bsl/.os: всегда → code-rag (Qdrant); опционально → metadata-graph
        #           (Neo4j call graph, 4.6.5) при METADATA_WATCH_ENABLED.
        # .xml:     только → metadata-graph при METADATA_WATCH_ENABLED.
        targets: list[str] = []
        if ext in CODE_EXTENSIONS:
            if CODE_WATCH_ENABLED:
                targets.append("code")
            if METADATA_WATCH_ENABLED:
                targets.append("metadata")
        elif ext in XML_EXTENSIONS and METADATA_WATCH_ENABLED:
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


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    if not WATCHER_ENABLED:
        _log("WATCHER_ENABLED=false — watcher отключён, идём в простой sleep-loop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    _log(f"initial delay {INITIAL_DELAY_SEC:.0f}s (даём MCP-серверам подняться)")
    time.sleep(INITIAL_DELAY_SEC)

    code_path = Path(CODE_DIR)
    xml_path = Path(XML_DIR)

    _log(f"config: debounce={DEBOUNCE_SEC}s, mcp_timeout={MCP_CALL_TIMEOUT_SEC}s")
    _log(f"code dir: {CODE_DIR} (exists={code_path.is_dir()})")
    _log(f"xml dir:  {XML_DIR} (exists={xml_path.is_dir()}, enabled={METADATA_WATCH_ENABLED})")
    _log(f"code-rag:       {CODE_RAG_URL} (enabled={CODE_WATCH_ENABLED})")
    if METADATA_WATCH_ENABLED:
        _log(f"metadata-graph: {METADATA_GRAPH_URL}")

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
