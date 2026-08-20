"""
PERF-7. BSL Language Server в режиме LSP вместо запуска JVM на каждый вызов.
============================================================================

Цена, которую платим сейчас
───────────────────────────
Замер на боевом стенде 16 августа: медиана `bsl_check_code` — 9,4 секунды,
максимум 16,6. При этом `bsl_stats`, который щупает ту же java, отвечает за
100 мс. Разница не в анализе, а в старте: каждый вызов поднимает JVM,
грузит jar на 43 МБ, инициализирует парсер и после ответа всё это
выбрасывает.

Для инструмента, к которому агент идёт после каждой правки кода, десять
секунд — это не «медленно», это «перестану им пользоваться».

Что меняется
────────────
BSL LS без аргументов работает языковым сервером: живёт процессом, говорит
по LSP через stdin/stdout, диагностики присылает уведомлением
`textDocument/publishDiagnostics` в ответ на открытие документа. JVM
стартует один раз, дальше — миллисекунды.

Чем за это платим — и почему это всё-таки допустимо
──────────────────────────────────────────────────
Безсостоятельность `bsl-checker` была его главным свойством: единственный
сервер набора, не зависящий ни от чего внешнего. Долгоживущий процесс это
свойство ломает — появляется то, что может умереть, зависнуть, утечь
памятью и вообще жить между вызовами.

Три вещи делают размен приемлемым:

  1. **Транспорт остаётся stateless.** Между вызовами MCP не сохраняется
     ничего, что относится к разговору. Живой процесс — это кеш дорогого
     старта, а не сессия: любой вызов самодостаточен, и результат не
     зависит от того, что спрашивали раньше.

  2. **Отказ этого кеша не отказ инструмента.** Если LSP не поднялся,
     умер или молчит — уходим на старый путь `--analyze`. Ответ будет
     прежний, только медленный. Это единственный способ ввести
     долгоживущий процесс, не сделав его новой точкой отказа.

  3. **Состояние видно снаружи.** `bsl_stats` показывает, жив ли процесс,
     сколько раз перезапускался и чем закончилась последняя попытка.
     Скрытое состояние — то, что делает систему неотлаживаемой; названное
     вслух — обычная деталь устройства.

Чего этот модуль НЕ делает
──────────────────────────
Не трогает `bsl_check_directory`. Анализ каталога и так идёт минуты, старт
JVM в нём теряется, а открывать по LSP сотни файлов — другая задача со
своими рисками (память процесса, порядок публикации диагностик). Каталог
остаётся на `--analyze`, и это осознанная граница, а не недоделка.

Честно о проверенности
──────────────────────
Протокольная часть покрыта тестами с настоящим обменом по каналам
(`tests_bsl_lsp.py`): рамки сообщений, корреляция ответов, ожидание
диагностик, таймауты, смерть процесса, перезапуск. Чего в песочнице нет —
самой BSL LS: java и jar живут только у вас.

Поэтому в модуле есть `--compare`: он прогоняет один и тот же файл обоими
путями и печатает разницу диагностик и времена. Это и есть приёмка
`PERF-7` — тот же приём «сверка входа с выходом», что в `shortfall.py`.

    docker exec mcp-bsl-checker python3 /app/bsl_lsp.py --compare /app/пример.bsl

Пока сверка не сделана, режим можно выключить одной переменной:
`BSL_LSP_MODE=off`.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# CFG-4: сборка командной строки для JVM — общая с путём `--analyze`.
# Модуль лежит рядом, в образе оба файла попадают в /app.
import bsl_config

__all__ = [
    "LspUnavailable",
    "BslLspClient",
    "to_report",
    "severity_name",
    "LSP_MODE",
]


# ─── настройки ───────────────────────────────────────────────────────────

# auto — пробуем LSP, при неудаче тихо уходим на --analyze (умолчание);
# on   — только LSP, отказ вместо отката (для проверки и для CI);
# off  — только старый путь.
LSP_MODE = os.environ.get("BSL_LSP_MODE", "auto").strip().lower()

# Старт включает подъём JVM и загрузку jar — это те самые ~10 с, ради
# которых всё затевается. Один раз их заплатить надо.
STARTUP_TIMEOUT_SEC = float(os.environ.get("BSL_LSP_STARTUP_TIMEOUT_SEC", "90"))

# Ожидание диагностик по одному файлу на живом сервере. Тридцати секунд
# хватает с запасом: обычный ответ — доли секунды.
REQUEST_TIMEOUT_SEC = float(os.environ.get("BSL_LSP_TIMEOUT_SEC", "30"))

# FAIL-1 в миниатюре. Если LSP не поднимается, платить полный таймаут
# старта на КАЖДЫЙ вызов нельзя: инструмент станет медленнее, чем был до
# правки. После неудачи не пробуем снова, пока не пройдёт это время.
RETRY_COOLDOWN_SEC = float(os.environ.get("BSL_LSP_RETRY_COOLDOWN_SEC", "60"))


class LspUnavailable(RuntimeError):
    """LSP-путь не сработал. Вызывающий обязан уйти на `--analyze`."""


# ─── рамки сообщений ─────────────────────────────────────────────────────
#
# LSP поверх потока: заголовок `Content-Length: N`, пустая строка, ровно N
# байт тела. Ошибиться тут легко и незаметно — рассинхронизация потока
# выглядит как «сервер молчит», а не как ошибка разбора, поэтому чтение
# намеренно строгое.


def frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def read_message(stream) -> dict | None:
    """
    Читает одно сообщение. None — поток закончился (процесс умер).

    Заголовки читаем построчно до пустой строки, тело — ровно по длине.
    """
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("utf-8", "replace").strip()
        if not line:
            break
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

    try:
        length = int(headers.get("content-length", ""))
    except ValueError:
        # Заголовка нет или он битый — поток рассинхронизирован, и дальше
        # читать бессмысленно: любое следующее чтение прочитает мусор.
        raise LspUnavailable(f"нет Content-Length в заголовках: {headers}")

    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LspUnavailable(f"тело сообщения не разбирается: {exc}") from exc


# ─── перевод диагностик в форму отчёта `--analyze` ───────────────────────

SEVERITY_NAMES = {1: "Error", 2: "Warning", 3: "Information", 4: "Hint"}


def severity_name(value) -> str:
    """
    По LSP важность — число 1..4, в json-отчёте `--analyze` — имя.

    Приводим к имени, чтобы форма ответа инструмента не зависела от того,
    каким путём он получен: агент не должен видеть разницу между режимами.
    Число оставляем рядом отдельным полем — оно точнее и не теряется.
    """
    if isinstance(value, str):
        return value
    return SEVERITY_NAMES.get(value, str(value or ""))


def to_report(path: str, diagnostics: list) -> dict:
    """
    Собирает ответ в ТОЧНО ТОЙ форме, что отдаёт json-репортер `--analyze`.

    Это главное решение всей правки. Можно было отдать диагностики как
    есть и научить три инструмента разбирать два формата — и получить
    третий способ прочитать одно и то же, рядом с двумя имеющимися
    (`fileinfos` и голый список). Вместо этого перевод делается в одном
    месте, на границе, а `bsl_check_code`, `bsl_check_file` и разбор
    отчёта остаются нетронутыми.

    Побочная выгода важнее косметики: старые примеры датасета
    (`bsl-001`…`bsl-003`) проверяют новый путь, ничего не зная о нём. Если
    LSP отдаст другие диагностики, они покраснеют.
    """
    items = []
    for d in diagnostics or []:
        item = dict(d)
        item["severity"] = severity_name(d.get("severity"))
        if isinstance(d.get("severity"), int):
            item["severity_lsp"] = d["severity"]
        items.append(item)
    return {"fileinfos": [{"path": path, "diagnostics": items}]}


# ─── клиент ──────────────────────────────────────────────────────────────


def _default_launcher(java_cmd: str, java_opts: str, jar: str, config: str):
    # CFG-4: argv собирает bsl_config — общий модуль с путём `--analyze`.
    # Раньше команда собиралась здесь, а вторая, почти такая же, — в
    # server.py; ключ `--configuration` был в обеих, но доезжал только
    # отсюда (FIX-30).
    cmd = bsl_config.lsp_argv(java_cmd, java_opts, jar, config)
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0,
    )


class BslLspClient:
    """
    Долгоживущий BSL LS. Один процесс, один замок, честные таймауты.

    Замок глобальный на весь обмен «открыли файл → дождались диагностик →
    закрыли». FastMCP исполняет синхронные инструменты в рабочем потоке на
    запрос, то есть два вызова могут прийти одновременно, а stdin у
    процесса один. Могла бы быть очередь запросов с маршрутизацией по id —
    но диагностики приходят УВЕДОМЛЕНИЕМ, без id запроса, и связать их с
    вызывающим можно только по URI. Замок проще и не врёт; выигрыш
    измеряется в задержке одного вызова, а не в пропускной способности.
    """

    def __init__(self, java_cmd="java", java_opts="", jar="", config="",
                 launcher=None, clock=time.monotonic):
        self._launcher = launcher or (
            lambda: _default_launcher(java_cmd, java_opts, jar, config))
        self._clock = clock
        self._lock = threading.RLock()
        self._proc = None
        self._reader = None
        self._responses: dict[int, dict] = {}
        self._diagnostics: dict[str, list] = {}
        self._events: dict[str, threading.Event] = {}
        self._response_events: dict[int, threading.Event] = {}
        self._next_id = 1
        self._stderr_tail: list[str] = []

        # Состояние для bsl_stats — оно же и есть «названное вслух».
        self.started_at = None
        self.requests = 0
        self.restarts = 0
        self.last_error = ""
        self.last_failure_at = None
        self.consecutive_failures = 0
        self._blocked_until = None

    # ─ жизненный цикл ─

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # Порог, после которого падения В ЗАПРОСАХ приравниваются к «не
    # поднимается»: три подряд означают, что дело не в конкретном файле.
    FAILURES_BEFORE_COOLDOWN = 3

    def _note_failure(self, message: str, blocks_start: bool = False) -> None:
        """
        Записать неудачу и решить, запрещает ли она следующую попытку.

        Разделение появилось потому, что тест перезапуска отказался
        проходить, и прав был он. Пауза задумывалась против одного случая —
        «LSP вообще не поднимается» (нет java, битый jar): там повторные
        попытки только добавляют полный таймаут старта к каждому вызову.

        Но под неё попало и падение ПОСРЕДИ запроса, а это другое событие.
        Умерший процесс — обычное дело для долгоживущей java, и одна
        смерть не повод выключать быстрый путь на минуту для всех
        последующих проверок.

        Разница по существу: не поднялся — виноват сервер, пробовать
        бессмысленно; умер в работе — возможно, виноват конкретный файл, и
        следующий вызов имеет право на новый процесс. Если же падает три
        раза подряд, дело всё-таки в сервере — тогда пауза.
        """
        self.last_error = message[:300]
        self.last_failure_at = self._clock()
        self.consecutive_failures += 1
        if blocks_start or self.consecutive_failures >= self.FAILURES_BEFORE_COOLDOWN:
            self._blocked_until = self._clock() + RETRY_COOLDOWN_SEC

    def _cooling_down(self) -> bool:
        """
        Недавно не получилось — не пробуем снова.

        Без этого сломанный LSP делает инструмент медленнее, чем он был до
        правки: сначала полный таймаут старта, потом всё равно `--analyze`.
        Тот же урок, что `FAIL-1` у справки.
        """
        if self._blocked_until is None:
            return False
        return self._clock() < self._blocked_until

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            if self._cooling_down():
                raise LspUnavailable(
                    f"LSP не поднялся {RETRY_COOLDOWN_SEC:g} с назад "
                    f"({self.last_error}); пока не пробуем снова")
            try:
                self._proc = self._launcher()
            except Exception as exc:
                self._note_failure(f"{type(exc).__name__}: {exc}",
                                   blocks_start=True)
                raise LspUnavailable(self.last_error) from exc

            self._responses.clear()
            self._diagnostics.clear()
            self._events.clear()
            self._response_events.clear()
            self._stderr_tail = []

            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="bsl-lsp-reader")
            self._reader.start()
            if self._proc.stderr is not None:
                threading.Thread(target=self._drain_stderr, daemon=True,
                                 name="bsl-lsp-stderr").start()

            try:
                self._initialize()
            except Exception as exc:
                self._note_failure(f"initialize: {exc}", blocks_start=True)
                self.stop()
                raise LspUnavailable(self.last_error) from exc

            self.started_at = self._clock()

    def stop(self) -> None:
        """
        Остановка в правильном порядке — и порядок здесь не педантизм.

        Первая редакция закрывала потоки сразу после `kill()`, и набор
        тестов начал изредка падать: примерно один прогон из десяти, всегда
        в разных местах. Причина — поток чтения, застрявший в `read()` на
        уже закрытом файловом дескрипторе. Номера дескрипторов
        переиспользуются: следующий процесс получает тот же номер, и
        зомби-поток вычитывает ЧУЖИЕ сообщения. Снаружи это выглядит как
        «диагностики не пришли», то есть как отказ анализатора.

        В тестах это плавающий провал. В работе — редкий, но настоящий:
        перезапуск после падения java даёт ровно ту же расстановку.

        Правильный порядок: сначала завершить процесс, дождаться, пока
        читатель увидит конец потока и выйдет сам, и только потом закрывать
        дескрипторы. Тогда закрывать нечего некому.
        """
        with self._lock:
            proc, self._proc = self._proc, None
            reader, self._reader = self._reader, None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    # По протоколу: сначала shutdown, потом exit. Если
                    # сервер уже не слушает, молча идём убивать.
                    try:
                        self._send({"jsonrpc": "2.0", "id": self._take_id(),
                                    "method": "shutdown", "params": None},
                                   proc=proc)
                        self._send({"jsonrpc": "2.0", "method": "exit"},
                                   proc=proc)
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=2)
                        except Exception:
                            pass
            finally:
                # Читатель выходит сам, увидев конец потока. Ждём его до
                # закрытия дескрипторов — иначе он останется висеть на них.
                if reader is not None and reader.is_alive():
                    reader.join(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        stream and stream.close()
                    except Exception:
                        pass

    def restart(self) -> None:
        with self._lock:
            self.stop()
            self.restarts += 1
            self.start()

    # ─ обмен ─

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, payload: dict, proc=None) -> None:
        proc = proc or self._proc
        if proc is None or proc.stdin is None:
            raise LspUnavailable("процесс LSP не запущен")
        proc.stdin.write(frame(payload))
        proc.stdin.flush()

    def _drain_stderr(self) -> None:
        """
        stderr читаем и запоминаем хвост.

        Не читать нельзя: заполнится буфер канала, и процесс встанет —
        зависший анализатор выглядел бы как «медленно», а не как ошибка.
        Хвост попадает в текст отказа: без него причина смерти java
        остаётся только в логах контейнера.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-20]

    def _read_loop(self) -> None:
        proc = self._proc
        stream = proc.stdout if proc else None
        if stream is None:
            return
        while True:
            try:
                message = read_message(stream)
            except LspUnavailable as exc:
                self._note_failure(str(exc))
                break
            except Exception as exc:  # поток закрылся под нами
                self._note_failure(f"{type(exc).__name__}: {exc}")
                break
            if message is None:
                break
            if proc is not self._proc:
                # Процесс сменился, а этот поток ещё жив: всё, что он
                # прочитает, относится к прошлой жизни (или, хуже, к чужому
                # дескриптору). Молча уходим.
                break
            self._dispatch(message)

        # Поток кончился — будим всех ожидающих, иначе они досидят до
        # таймаута, хотя ответа уже не будет никогда.
        for event in list(self._events.values()) + list(self._response_events.values()):
            event.set()

    def _dispatch(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            mid = message["id"]
            self._responses[mid] = message
            event = self._response_events.get(mid)
            if event:
                event.set()
            return
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = params.get("uri", "")
            self._diagnostics[uri] = params.get("diagnostics") or []
            event = self._events.get(uri)
            if event:
                event.set()

    def _request(self, method: str, params, timeout: float):
        mid = self._take_id()
        event = threading.Event()
        self._response_events[mid] = event
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params})
        if not event.wait(timeout):
            self._response_events.pop(mid, None)
            raise LspUnavailable(f"{method}: нет ответа за {timeout:g} с")
        self._response_events.pop(mid, None)
        message = self._responses.pop(mid, None)
        if message is None:
            raise LspUnavailable(f"{method}: процесс закончился без ответа"
                                 + self._stderr_note())
        if "error" in message:
            raise LspUnavailable(f"{method}: {message['error']}")
        return message.get("result")

    def _stderr_note(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | stderr: " + " ".join(self._stderr_tail[-5:])

    def _initialize(self) -> None:
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": None,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": False},
                },
            },
        }, STARTUP_TIMEOUT_SEC)
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # ─ то, ради чего всё ─

    def diagnostics(self, path: str, text: str | None = None,
                    timeout: float | None = None) -> list:
        """
        Диагностики одного файла. Бросает `LspUnavailable` — не молчит.

        Молчание здесь было бы худшим исходом: пустой список неотличим от
        «замечаний нет», а это ровно то различение, ради которого писался
        `OBS-1`.
        """
        if LSP_MODE == "off":
            raise LspUnavailable("BSL_LSP_MODE=off")
        timeout = REQUEST_TIMEOUT_SEC if timeout is None else timeout
        file_path = Path(path)
        if text is None:
            text = file_path.read_text(encoding="utf-8-sig")
        uri = file_path.as_uri()

        with self._lock:
            if not self.running():
                if self._proc is not None:
                    # Процесс был и умер — это перезапуск, а не первый старт.
                    self.stop()
                    self.restarts += 1
                self.start()

            event = threading.Event()
            self._events[uri] = event
            self._diagnostics.pop(uri, None)
            try:
                self._send({
                    "jsonrpc": "2.0", "method": "textDocument/didOpen",
                    "params": {"textDocument": {
                        "uri": uri, "languageId": "bsl",
                        "version": int(self._clock() * 1000) % 1_000_000,
                        "text": text,
                    }},
                })
                if not event.wait(timeout):
                    self._note_failure(
                        f"диагностики не пришли за {timeout:g} с")
                    raise LspUnavailable(self.last_error + self._stderr_note())
                if not self.running():
                    self._note_failure("процесс LSP умер во время анализа")
                    raise LspUnavailable(self.last_error + self._stderr_note())
                self.requests += 1
                self.consecutive_failures = 0
                self._blocked_until = None
                return self._diagnostics.get(uri) or []
            finally:
                self._events.pop(uri, None)
                self._diagnostics.pop(uri, None)
                # Закрываем документ: иначе сервер держит его разобранным
                # в памяти, а мы открываем по файлу на каждый вызов.
                try:
                    if self.running():
                        self._send({
                            "jsonrpc": "2.0",
                            "method": "textDocument/didClose",
                            "params": {"textDocument": {"uri": uri}},
                        })
                except Exception:
                    pass

    # ─ что показать в bsl_stats ─

    def state(self) -> dict:
        alive = self.running()
        return {
            "mode": LSP_MODE,
            "running": alive,
            "pid": self._proc.pid if (self._proc and alive) else None,
            "uptime_sec": (round(self._clock() - self.started_at, 1)
                           if (alive and self.started_at) else None),
            "requests": self.requests,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "cooling_down": self._cooling_down(),
            "note": (
                "Долгоживущий процесс BSL LS: JVM стартует один раз, а не на "
                "каждый вызов (PERF-7). Если он не поднялся, проверки идут "
                "прежним путём --analyze — медленнее, но с тем же "
                "результатом."
            ),
        }


# ─── проверка на живой машине ────────────────────────────────────────────


# Куда в контейнере смонтирована выгрузка. Первый существующий и берём.
# Список нужен потому, что путь монтирования — вещь, которую держат в
# голове ровно до первого раза, когда она понадобилась.
SOURCE_ROOTS = ("/data/1c-src", "/workspace", "/data/1c-config")


def find_sample(root: str = "") -> str:
    """
    Любой .bsl из выгрузки — чтобы сверку можно было запустить, не зная,
    где что лежит внутри контейнера.

    Берём не первый попавшийся, а средний по размеру из первых полусотни:
    на пустом модуле сверка ничего не покажет (диагностик нет у обоих
    путей), а на самом большом — будет долго идти прежним путём.
    """
    roots = [root] if root else list(SOURCE_ROOTS)
    found = []
    for candidate in roots:
        base = Path(candidate)
        if not base.is_dir():
            continue
        for path in base.rglob("*.bsl"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 200:
                found.append((size, str(path)))
            if len(found) >= 50:
                break
        if found:
            break
    if not found:
        return ""
    found.sort()
    # Смещение к меньшему: при двух кандидатах берём тот, что быстрее
    # пройдёт прежним путём — сверке важна одинаковость, а не объём.
    return found[(len(found) - 1) // 2][1]


def _compare(file_path: str, java_cmd: str, java_opts: str, jar: str,
             config: str) -> int:
    """
    Сверка двух путей на одном файле: то же ли самое и насколько быстрее.

    Приёмка `PERF-7`. Протокольную часть можно проверить тестами, а вот
    что BSL LS в режиме LSP выдаёт ТЕ ЖЕ диагностики, что и `--analyze`, —
    проверяется только на живой java с настоящим jar. Здесь это делается
    одной командой.
    """
    import tempfile

    path = Path(file_path) if file_path else Path("")
    if file_path and not path.exists():
        print(f"Нет файла: {path}\n")
        print("Внутри контейнера выгрузка лежит не там, где на диске. "
              "Смонтировано:")
        for candidate in SOURCE_ROOTS:
            base = Path(candidate)
            print(f"  {candidate:<18} {'есть' if base.is_dir() else 'нет'}")
        print("\nЗапустите без аргумента — файл найдётся сам:")
        print("  docker exec mcp-bsl-checker python3 /app/bsl_lsp.py --compare")
        return 2

    if not file_path:
        sample = find_sample()
        if not sample:
            print("Не нашёл ни одного .bsl в " + ", ".join(SOURCE_ROOTS))
            print("Укажите файл явно: --compare <путь внутри контейнера>")
            return 2
        path = Path(sample)
        print(f"Файл выбран сам: {path}")

    print(f"Файл: {path}")
    print(f"jar:  {jar}")
    print()

    # ── путь 1: как было ──
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as outdir:
        # CFG-4: та же сборка argv, что в бою. Своя копия здесь означала
        # бы, что сверка сравнивает не то, что работает: расхождение
        # набором правил выглядело бы как расхождение между путями.
        cmd = bsl_config.analyze_argv(
            java_cmd, java_opts, jar, str(path.parent), outdir, config)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        report_file = Path(outdir) / "bsl-json.json"
        analyze = []
        if report_file.exists():
            data = json.loads(report_file.read_text(encoding="utf-8"))
            entries = data.get("fileinfos", data if isinstance(data, list) else [])
            for fi in entries:
                if path.name.lower() in str(fi.get("path", "")).lower():
                    analyze = fi.get("diagnostics") or []
        else:
            print(f"  --analyze не создал отчёт, код {proc.returncode}")
            print(f"  stderr: {(proc.stderr or '')[-500:]}")
    t_analyze = time.monotonic() - t0
    print(f"--analyze : {t_analyze:6.2f} с, диагностик {len(analyze)}")

    # ── путь 2: LSP, два вызова — холодный и тёплый ──
    client = BslLspClient(java_cmd=java_cmd, java_opts=java_opts, jar=jar,
                          config=config)
    try:
        t0 = time.monotonic()
        lsp = client.diagnostics(str(path))
        t_cold = time.monotonic() - t0

        t0 = time.monotonic()
        lsp_again = client.diagnostics(str(path))
        t_warm = time.monotonic() - t0
    except LspUnavailable as exc:
        print(f"LSP       : НЕ РАБОТАЕТ — {exc}")
        print("\nВывод: оставьте BSL_LSP_MODE=off, работает прежний путь.")
        client.stop()
        return 1
    print(f"LSP холодн: {t_cold:6.2f} с, диагностик {len(lsp)}   (включая старт JVM)")
    print(f"LSP тёплый: {t_warm:6.2f} с, диагностик {len(lsp_again)}")
    client.stop()

    # ── сверка ──
    def key(d):
        start = (d.get("range") or {}).get("start") or {}
        return (start.get("line"), d.get("code"), d.get("message"))

    left, right = {key(d) for d in analyze}, {key(d) for d in lsp}
    print()
    if left == right:
        print(f"СОВПАДАЕТ: {len(left)} диагностик, оба пути дали одно и то же.")
        if t_warm > 0:
            print(f"Выигрыш на тёплом вызове: в {t_analyze / t_warm:.0f} раз "
                  f"({t_analyze:.1f} с → {t_warm:.2f} с).")
        return 0

    print("РАСХОЖДЕНИЕ — переключать нельзя, пока не разобрались:")
    for item in sorted(left - right, key=lambda x: str(x)):
        print(f"  только --analyze: строка {item[0]}, {item[1]}")
    for item in sorted(right - left, key=lambda x: str(x)):
        print(f"  только LSP:       строка {item[0]}, {item[1]}")
    return 1


def main() -> int:  # pragma: no cover — ручной инструмент
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--compare", metavar="FILE", nargs="?", const="",
                    default=None,
                    help="сверить --analyze и LSP на одном файле; без "
                         "аргумента файл берётся из выгрузки сам")
    args = ap.parse_args()

    jar = os.environ.get("BSL_LS_JAR", "/opt/bsl-language-server/bsl-ls.jar")
    java_cmd = os.environ.get("BSL_JAVA_CMD", "java")
    java_opts = os.environ.get("JAVA_OPTS", "-Xmx512m")
    # CFG-4: сверка обязана идти ТЕМ ЖЕ набором правил, что и сервер, —
    # включая решение «битый конфиг анализатору не передаём». Иначе она
    # сравнивала бы два пути на настройках, которых в бою нет.
    _cfg = bsl_config.describe(os.environ.get("BSL_LS_CONFIG", ""))
    config = bsl_config.config_arg(_cfg)
    if _cfg.get("requested"):
        print("Набор диагностик: "
              + (f"{_cfg['path']} ({_cfg.get('fingerprint', '?')}, "
                 f"mode={_cfg.get('mode', '?')})" if _cfg.get("applied")
                 else f"НЕ ПРИМЕНЁН — {_cfg.get('error', '')}"))

    if args.compare is not None:
        return _compare(args.compare, java_cmd, java_opts, jar, config)
    ap.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
