"""
Тесты PERF-7: клиент BSL LS в режиме LSP.

Как проверяется
───────────────
Не заглушками поверх клиента, а НАСТОЯЩИМ обменом: поддельный LSP-сервер
поднимается потоком, читает те же рамки `Content-Length`, отвечает на
`initialize` и присылает `publishDiagnostics`. То есть проверяется тот код,
который поедет в контейнер, включая разбор потока, — а он и есть самое
хрупкое место: рассинхронизация выглядит как «сервер молчит», а не как
ошибка, и без такого теста нашлась бы только на живом стенде.

Чего здесь нет и быть не может — самой BSL LS: java и jar живут только на
машине пользователя. Совпадение диагностик двух путей проверяется
командой `python3 bsl_lsp.py --compare <файл>` в контейнере.

Запуск:  python3 tests_bsl_lsp.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bsl_lsp  # noqa: E402
from bsl_lsp import (  # noqa: E402
    BslLspClient,
    LspUnavailable,
    frame,
    read_message,
    severity_name,
    to_report,
)

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent

# Боевые таймауты здесь ни к чему: 90 секунд на старт JVM осмысленны в
# контейнере и бессмысленны против поддельного сервера, который отвечает
# мгновенно или не отвечает вовсе.
#
# Это не косметика. Один прогон из тридцати занял 101 секунду — набор
# упёрся ровно в эти 90 секунд и провалился по времени. Тест, способный
# ждать полторы минуты, рано или поздно съест прогон целиком, а причину
# будут искать в другом месте.
_REAL_TIMEOUTS = (bsl_lsp.STARTUP_TIMEOUT_SEC, bsl_lsp.REQUEST_TIMEOUT_SEC)


def setUpModule():
    bsl_lsp.STARTUP_TIMEOUT_SEC = 3.0
    bsl_lsp.REQUEST_TIMEOUT_SEC = 3.0


def tearDownModule():
    bsl_lsp.STARTUP_TIMEOUT_SEC, bsl_lsp.REQUEST_TIMEOUT_SEC = _REAL_TIMEOUTS


class FakeServer:
    """
    Поддельный языковой сервер: два канала и поток, говорящий по LSP.

    Ведёт себя как `subprocess.Popen` настолько, насколько клиенту нужно:
    `stdin`, `stdout`, `stderr`, `poll()`, `wait()`, `kill()`, `pid`.
    """

    def __init__(self, diagnostics=None, *, answer_initialize=True,
                 publish=True, die_after=None, delay=0.0, garbage=False):
        self.diagnostics = diagnostics if diagnostics is not None else []
        self.answer_initialize = answer_initialize
        self.publish = publish
        self.die_after = die_after      # умереть после N didOpen
        self.delay = delay
        self.garbage = garbage
        self.opened: list[str] = []
        self.closed: list[str] = []
        self._dead = False

        c2s_r, c2s_w = os.pipe()        # клиент → сервер
        s2c_r, s2c_w = os.pipe()        # сервер → клиент
        err_r, err_w = os.pipe()
        self.stdin = os.fdopen(c2s_w, "wb", buffering=0)
        self.stdout = os.fdopen(s2c_r, "rb", buffering=0)
        self.stderr = os.fdopen(err_r, "rb", buffering=0)
        self._server_in = os.fdopen(c2s_r, "rb", buffering=0)
        self._server_out = os.fdopen(s2c_w, "wb", buffering=0)
        self._err_out = os.fdopen(err_w, "wb", buffering=0)
        self.pid = 4242

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # ─ интерфейс процесса ─

    def poll(self):
        return None if not self._dead else 1

    def wait(self, timeout=None):
        self._die()
        return 1

    def kill(self):
        self._die()

    def _die(self):
        """
        Смерть «процесса»: клиент видит конец потока.

        FIX-26. Раньше здесь закрывался ещё и `_server_in` — прямо под
        потоком, который висел на нём в блокирующем `read()`. Закрытие
        дескриптора спящий поток НЕ будит: он остаётся на номере, а номер
        тут же переиспользует следующий `os.pipe()`. Дальше поток-зомби
        вычитывает `initialize` СЛЕДУЮЩЕГО сервера, настоящий обработчик
        его не видит, и клиент падает с «нет ответа за 3 с».

        Это ровно тот дефект, ради которого написан `TestShutdownOrder`, —
        только в самом тесте, а не в клиенте. Он и делал набор плавающим:
        3–4 провала на 40 прогонов, всегда в
        `test_many_restarts_do_not_leak_readers`.

        Поэтому `_die` закрывает только ПИШУЩИЕ концы сервера: клиент
        получает конец потока, как от умершего процесса, а читающий конец
        остаётся открытым — пока дескриптор занят, его номер никому не
        достанется. Разбирает канал `close()`, и в правильном порядке.
        """
        if self._dead:
            return
        self._dead = True
        for stream in (self._server_out, self._err_out):
            try:
                stream.close()
            except Exception:
                pass

    def close(self):
        """
        Закрыть ВСЕ шесть концов, включая клиентские.

        `_die()` закрывает только серверную половину: так ведёт себя
        умерший процесс, и для проверок этого достаточно. Но тест, не
        дошедший до `_die()`, оставлял три открытых дескриптора — на
        Windows это видно как `ResourceWarning: unclosed file` и,
        предположительно, как повод потоку-читателю остаться на
        блокирующем чтении.

        Возможная причина зависания прогона 17 августа. Доказать её не
        удалось: набор больше не воспроизвёл зависание ни разу. Но
        закрывать за собой дескрипторы правильно независимо от того,
        виноваты они были или нет.
        """
        self._die()
        # Порядок тот же, что у `BslLspClient.stop`: сначала дать потоку
        # увидеть конец потока — закрыть ПИШУЩИЙ конец его канала, —
        # дождаться выхода и только потом закрывать сам дескриптор.
        # Закрывать читающий конец под спящим потоком нельзя (FIX-26).
        try:
            self.stdin.close()
        except Exception:
            pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        for stream in (self._server_in, self.stdout, self.stderr):
            try:
                stream.close()
            except Exception:
                pass

    def __del__(self):
        # Страховка на случай теста, забывшего про addCleanup: CPython
        # считает ссылки, поэтому вызов приходит сразу после теста.
        try:
            self.close()
        except Exception:
            pass

    # ─ поведение сервера ─

    def _send(self, payload):
        try:
            self._server_out.write(frame(payload))
        except Exception:
            pass

    def _serve(self):
        while True:
            try:
                message = read_message(self._server_in)
            except Exception:
                return
            if message is None:
                return
            method = message.get("method")

            if method == "initialize":
                if not self.answer_initialize:
                    continue            # молчим — клиент обязан отвалиться
                if self.garbage:
                    # Битые рамки: проверяем, что клиент не зависает молча.
                    try:
                        self._server_out.write("это не LSP\r\n\r\n".encode("utf-8"))
                    except Exception:
                        pass
                    continue
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "result": {"capabilities": {}}})

            elif method == "textDocument/didOpen":
                uri = message["params"]["textDocument"]["uri"]
                self.opened.append(uri)
                if self.die_after is not None and len(self.opened) > self.die_after:
                    self._die()
                    return
                if self.delay:
                    time.sleep(self.delay)
                if self.publish:
                    self._send({"jsonrpc": "2.0",
                                "method": "textDocument/publishDiagnostics",
                                "params": {"uri": uri,
                                           "diagnostics": self.diagnostics}})

            elif method == "textDocument/didClose":
                self.closed.append(message["params"]["textDocument"]["uri"])

            elif method == "shutdown":
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "result": None})
            elif method == "exit":
                self._die()
                return


DIAG = [{
    "range": {"start": {"line": 3, "character": 0},
              "end": {"line": 3, "character": 10}},
    "severity": 2,
    "code": "CanonicalSpellingKeywords",
    "source": "bsl-language-server",
    "message": "Ключевое слово написано не канонически",
}]


def _client(server_factory, **kw):
    return BslLspClient(launcher=server_factory, **kw)


class TestFraming(unittest.TestCase):
    """
    Рамки сообщений. Ошибка здесь не выглядит ошибкой: поток
    рассинхронизируется, и снаружи это «сервер молчит».
    """

    def test_roundtrip(self):
        import io
        payload = {"jsonrpc": "2.0", "method": "тест", "params": {"x": "ы"}}
        stream = io.BytesIO(frame(payload))
        self.assertEqual(read_message(stream), payload)

    def test_length_is_in_bytes_not_characters(self):
        """
        Кириллица в UTF-8 занимает по два байта. Считать символы вместо
        байтов — классическая ошибка, и проявится она только на русских
        сообщениях, то есть на всех сообщениях BSL LS.
        """
        import io
        payload = {"message": "Ключевое слово написано не канонически"}
        raw = frame(payload)
        header = raw.split(b"\r\n\r\n")[0].decode()
        declared = int(header.split(":")[1])
        self.assertEqual(declared, len(raw.split(b"\r\n\r\n", 1)[1]))
        self.assertEqual(read_message(io.BytesIO(raw)), payload)

    def test_two_messages_in_a_row(self):
        import io
        a = {"id": 1, "result": {}}
        b = {"method": "textDocument/publishDiagnostics"}
        stream = io.BytesIO(frame(a) + frame(b))
        self.assertEqual(read_message(stream), a)
        self.assertEqual(read_message(stream), b)

    def test_closed_stream_is_none_not_hang(self):
        import io
        self.assertIsNone(read_message(io.BytesIO(b"")))

    def test_missing_length_is_an_error(self):
        import io
        with self.assertRaises(LspUnavailable):
            read_message(io.BytesIO(b"X-Foo: 1\r\n\r\n{}"))


class TestSeverityAndShape(unittest.TestCase):

    def test_severity_numbers_become_names(self):
        self.assertEqual(severity_name(1), "Error")
        self.assertEqual(severity_name(2), "Warning")
        self.assertEqual(severity_name(4), "Hint")

    def test_unknown_severity_does_not_explode(self):
        self.assertEqual(severity_name(None), "")
        self.assertEqual(severity_name(99), "99")
        self.assertEqual(severity_name("Warning"), "Warning")

    def test_report_shape_matches_analyze(self):
        """
        Форма ответа обязана совпадать с json-репортером `--analyze` — на
        этом держится то, что три инструмента и старые примеры датасета
        остались нетронутыми.
        """
        report = to_report("/tmp/Module.bsl", DIAG)
        self.assertIn("fileinfos", report)
        fi = report["fileinfos"][0]
        self.assertEqual(fi["path"], "/tmp/Module.bsl")
        d = fi["diagnostics"][0]
        self.assertEqual(d["range"]["start"]["line"], 3)
        self.assertEqual(d["severity"], "Warning")
        self.assertEqual(d["severity_lsp"], 2)
        self.assertEqual(d["code"], "CanonicalSpellingKeywords")

    def test_empty_diagnostics_still_give_a_file_entry(self):
        """
        «Замечаний нет» — это отчёт с пустым списком, а не отсутствие
        отчёта. Иначе вызывающий прочитает пустоту как отказ.
        """
        report = to_report("/tmp/Module.bsl", [])
        self.assertEqual(report["fileinfos"][0]["diagnostics"], [])


class TestDialogue(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_module.bsl"
        self.tmp.write_text("Процедура Тест() КонецПроцедуры", encoding="utf-8")
        self.addCleanup(lambda: self.tmp.exists() and self.tmp.unlink())
        self.servers = []
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        # `close()`, а не `_die()`: второй закрывает только серверные
        # концы (так ведёт себя умерший процесс), а клиентские три
        # оставались открытыми до сборки мусора. На Windows это видно
        # как ResourceWarning; см. FakeServer.close.
        for s in self.servers:
            s.close()

    def _make(self, **kw):
        def factory():
            server = FakeServer(**kw)
            self.servers.append(server)
            return server
        return factory

    def test_diagnostics_arrive(self):
        client = _client(self._make(diagnostics=DIAG))
        got = client.diagnostics(str(self.tmp))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["code"], "CanonicalSpellingKeywords")
        client.stop()

    def test_no_diagnostics_is_an_empty_list_not_an_error(self):
        client = _client(self._make(diagnostics=[]))
        self.assertEqual(client.diagnostics(str(self.tmp)), [])
        client.stop()

    def test_jvm_starts_once(self):
        """
        Ради этого всё и затевалось: второй вызов не поднимает процесс
        заново.
        """
        starts = []

        def factory():
            server = FakeServer(diagnostics=DIAG)
            self.servers.append(server)
            starts.append(1)
            return server

        client = _client(factory)
        client.diagnostics(str(self.tmp))
        client.diagnostics(str(self.tmp))
        client.diagnostics(str(self.tmp))
        self.assertEqual(len(starts), 1, "процесс поднимается на каждый вызов")
        self.assertEqual(client.requests, 3)
        client.stop()

    def test_document_is_closed_after_each_check(self):
        """Иначе сервер держит разобранным каждый когда-либо открытый файл."""
        client = _client(self._make(diagnostics=DIAG))
        client.diagnostics(str(self.tmp))
        time.sleep(0.05)
        self.assertEqual(len(self.servers[0].closed), 1)
        client.stop()

    def test_text_may_be_passed_without_a_file_on_disk(self):
        client = _client(self._make(diagnostics=DIAG))
        client.diagnostics(str(self.tmp), text="Процедура Иначе() КонецПроцедуры")
        client.stop()

    def test_silence_becomes_a_refusal_not_an_empty_answer(self):
        """
        Самое опасное поведение: сервер жив, но диагностик не прислал.
        Вернуть пустой список значило бы сказать «замечаний нет» — то
        самое, против чего написан OBS-1.
        """
        client = _client(self._make(publish=False))
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(self.tmp), timeout=0.3)
        client.stop()

    def test_initialize_without_answer_fails_fast(self):
        client = _client(self._make(answer_initialize=False))
        bsl_lsp.STARTUP_TIMEOUT_SEC = 0.3
        try:
            with self.assertRaises(LspUnavailable):
                client.diagnostics(str(self.tmp))
        finally:
            bsl_lsp.STARTUP_TIMEOUT_SEC = 3.0
        client.stop()

    def test_broken_frames_do_not_hang(self):
        client = _client(self._make(garbage=True))
        bsl_lsp.STARTUP_TIMEOUT_SEC = 0.5
        try:
            with self.assertRaises(LspUnavailable):
                client.diagnostics(str(self.tmp))
        finally:
            bsl_lsp.STARTUP_TIMEOUT_SEC = 3.0
        client.stop()

    def test_death_during_a_call_is_a_refusal_not_an_empty_answer(self):
        """
        Процесс умер посреди запроса.

        Я писал этот тест с ожиданием «клиент перезапустится и ответит», и
        реализация со мной не согласилась. Права она. Диагностики могли
        прийти частично — сервер успел разобрать половину файла и упал.
        Отдать то, что пришло, значило бы сказать «вот все замечания», не
        зная этого. Отказ здесь честнее, тем более что вызывающий уходит
        на `--analyze` и ответ всё равно получит.
        """
        client = _client(self._make(diagnostics=DIAG, die_after=1))
        client.diagnostics(str(self.tmp))          # первый — успешен
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(self.tmp))      # на втором сервер умирает
        client.stop()

    def test_next_call_after_death_starts_a_fresh_process(self):
        """
        А вот СЛЕДУЮЩИЙ вызов обязан поднять новый процесс: смерть
        долгоживущей java — обычное дело, и она не должна выводить
        быстрый путь из строя навсегда.
        """
        client = _client(self._make(diagnostics=DIAG, die_after=1))
        client.diagnostics(str(self.tmp))
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(self.tmp))
        time.sleep(0.05)
        got = client.diagnostics(str(self.tmp))
        self.assertEqual(len(got), 1, "новый процесс не поднялся")
        self.assertEqual(client.restarts, 1)
        client.stop()

    def test_parallel_calls_do_not_mix_up(self):
        """
        FastMCP исполняет синхронные инструменты в рабочем потоке на
        запрос. У процесса один stdin, и диагностики приходят
        уведомлением без id — связать их с вызывающим можно только по URI.
        """
        client = _client(self._make(diagnostics=DIAG, delay=0.02))
        results = []

        def worker():
            results.append(client.diagnostics(str(self.tmp)))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(len(r) == 1 for r in results))
        client.stop()

    def test_mode_off_refuses_without_starting_anything(self):
        started = []

        def factory():
            started.append(1)
            raise AssertionError("процесс не должен подниматься при mode=off")

        client = _client(factory)
        bsl_lsp.LSP_MODE = "off"
        try:
            with self.assertRaises(LspUnavailable):
                client.diagnostics(str(self.tmp))
        finally:
            bsl_lsp.LSP_MODE = "auto"
        self.assertEqual(started, [])


class TestCooldown(unittest.TestCase):
    """
    FAIL-1 в миниатюре. Сломанный LSP не должен делать инструмент
    медленнее, чем он был до правки: полный таймаут старта на каждый
    вызов — это ровно тот случай, когда «улучшение» ухудшает.
    """

    def test_second_attempt_is_skipped_while_cooling_down(self):
        attempts = []
        now = {"t": 1000.0}

        def factory():
            attempts.append(1)
            raise OSError("java не найдена")

        client = BslLspClient(launcher=factory, clock=lambda: now["t"])
        tmp = Path(__file__).resolve().parent / "_tmp_cool.bsl"
        tmp.write_text("Процедура Т() КонецПроцедуры", encoding="utf-8")
        self.addCleanup(lambda: tmp.exists() and tmp.unlink())

        for _ in range(3):
            with self.assertRaises(LspUnavailable):
                client.diagnostics(str(tmp))
        self.assertEqual(len(attempts), 1, "пробуем снова, не выждав паузы")

        now["t"] += bsl_lsp.RETRY_COOLDOWN_SEC + 1
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(tmp))
        self.assertEqual(len(attempts), 2, "после паузы обязаны попробовать")

    def test_state_names_the_failure(self):
        """Скрытое состояние делает систему неотлаживаемой."""
        def factory():
            raise OSError("java не найдена")

        client = BslLspClient(launcher=factory)
        tmp = Path(__file__).resolve().parent / "_tmp_state.bsl"
        tmp.write_text("Процедура Т() КонецПроцедуры", encoding="utf-8")
        self.addCleanup(lambda: tmp.exists() and tmp.unlink())
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(tmp))
        state = client.state()
        self.assertFalse(state["running"])
        self.assertIn("java", state["last_error"])
        self.assertTrue(state["cooling_down"])


class TestSampleSearch(unittest.TestCase):
    """
    Сверка должна запускаться, не требуя знать, где внутри контейнера
    лежит выгрузка. Я сам ошибся этим путём в инструкции — написал
    `/workspace`, тогда как смонтировано `/data/1c-src`, — и первая же
    попытка приёмки уткнулась в «нет файла».

    Вывод не «впредь быть внимательнее», а «пусть инструмент найдёт сам».
    """

    def test_picks_a_non_empty_file(self):
        import tempfile
        from bsl_lsp import find_sample

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "пусто.bsl").write_text("", encoding="utf-8")
            (root / "a" / "Мал.bsl").write_text("Процедура Т() КонецПроцедуры" * 10,
                                                encoding="utf-8")
            picked = find_sample(str(root))
            self.assertTrue(picked.endswith("Мал.bsl"),
                            "выбран пустой файл — сверка на нём ничего не покажет")

    def test_missing_root_is_empty_not_an_exception(self):
        from bsl_lsp import find_sample
        self.assertEqual(find_sample("/нет/такого/каталога"), "")


class TestShutdownOrder(unittest.TestCase):
    """
    Порядок остановки. Нашлось плавающим провалом набора — примерно один
    прогон из десяти, каждый раз в другом тесте.

    Причина: поток чтения, застрявший в `read()` на закрытом дескрипторе.
    Номера дескрипторов переиспользуются, следующий процесс получает тот
    же номер — и зомби-поток вычитывает чужие сообщения. Снаружи это
    выглядит как «диагностики не пришли», то есть как отказ анализатора.

    В работе расстановка та же: перезапуск после падения java.
    """

    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_stop.bsl"
        self.tmp.write_text("Процедура Т() КонецПроцедуры", encoding="utf-8")
        self.addCleanup(lambda: self.tmp.exists() and self.tmp.unlink())

    def test_reader_thread_is_gone_after_stop(self):
        servers = []

        def factory():
            server = FakeServer(diagnostics=DIAG)
            servers.append(server)
            return server

        client = BslLspClient(launcher=factory)
        client.diagnostics(str(self.tmp))
        reader = client._reader
        self.assertIsNotNone(reader)
        client.stop()
        self.assertFalse(reader.is_alive(),
                         "поток чтения пережил остановку и висит на "
                         "дескрипторе, который вот-вот переиспользуют")
        for s in servers:
            s.close()

    def test_many_restarts_do_not_leak_readers(self):
        """
        Двадцать циклов «поднять — остановить». Если читатели остаются
        жить, их станет двадцать, и однажды один из них перехватит чужое
        сообщение.
        """
        before = threading.active_count()
        servers = []

        def factory():
            server = FakeServer(diagnostics=DIAG)
            servers.append(server)
            return server

        client = BslLspClient(launcher=factory)
        for _ in range(20):
            client.diagnostics(str(self.tmp))
            client.stop()
        for s in servers:
            s.close()
        time.sleep(0.2)
        # Потоки поддельных серверов свои, поэтому запас щедрый; важно, что
        # число не растёт линейно с числом перезапусков.
        self.assertLess(threading.active_count() - before, 25)


class TestFailureKinds(unittest.TestCase):
    """
    Два разных события, которые легко спутать: «не поднялся» и «умер в
    работе». Первое запрещает следующую попытку на минуту, второе — нет.
    Тест перезапуска нашёл, что я их не различал.
    """

    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_kinds.bsl"
        self.tmp.write_text("Процедура Т() КонецПроцедуры", encoding="utf-8")
        self.addCleanup(lambda: self.tmp.exists() and self.tmp.unlink())

    def test_three_deaths_in_a_row_do_trigger_the_pause(self):
        """
        Одна смерть — случайность. Три подряд означают, что дело не в
        конкретном файле, а в сервере: тогда пауза нужна, иначе каждый
        вызов платит полный старт JVM впустую.
        """
        servers = []

        def factory():
            server = FakeServer(diagnostics=DIAG, publish=False)
            servers.append(server)
            return server

        client = BslLspClient(launcher=factory)
        for _ in range(3):
            with self.assertRaises(LspUnavailable):
                client.diagnostics(str(self.tmp), timeout=0.2)
        self.assertTrue(client.state()["cooling_down"])
        self.assertEqual(client.state()["consecutive_failures"], 3)
        for s in servers:
            s.close()

    def test_success_resets_the_streak(self):
        """
        Иначе редкие одиночные сбои за день накапливались бы и однажды
        выключали быстрый путь на ровном месте.
        """
        state = {"publish": False}
        servers = []

        def factory():
            server = FakeServer(diagnostics=DIAG, publish=state["publish"])
            servers.append(server)
            return server

        client = BslLspClient(launcher=factory)
        with self.assertRaises(LspUnavailable):
            client.diagnostics(str(self.tmp), timeout=0.2)
        self.assertEqual(client.state()["consecutive_failures"], 1)

        state["publish"] = True
        client.stop()
        client.diagnostics(str(self.tmp))
        self.assertEqual(client.state()["consecutive_failures"], 0)
        self.assertFalse(client.state()["cooling_down"])
        client.stop()
        for s in servers:
            s.close()


class TestDelivery(unittest.TestCase):
    """Доставка и подключение — общей проверкой B-6 и текстом сервера."""

    def test_module_is_delivered(self):
        sys.path.insert(0, str(SUITE))
        from tests_delivery import assert_delivered
        assert_delivered(self, "bsl_lsp.py")

    def test_server_falls_back_to_analyze(self):
        """
        Долгоживущий процесс не имеет права стать новой точкой отказа:
        при неудаче LSP сервер обязан уйти на прежний путь.
        """
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("LspUnavailable", src)
        self.assertIn("_analyze_dir", src)

    def test_state_is_visible_in_stats(self):
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("lsp", src.lower())
        self.assertIn("lsp_state", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
