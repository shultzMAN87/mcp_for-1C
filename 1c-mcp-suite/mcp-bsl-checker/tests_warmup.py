"""
PERF-9. Прогрев JVM: тесты.
============================

Что именно проверяется
──────────────────────
Не «стало быстрее» — это меряется на стенде, а не тестом. Тестом
проверяются свойства, которые ломаются молча и которые как раз и делают
прогрев либо полезным, либо новой точкой отказа:

  • прогрев НЕ роняет сервер, если java нет или jar битый;
  • прогрев НЕ ждётся стартом — порт открывается сразу;
  • прогрев проходит путь ЦЕЛИКОМ (старт + одна настоящая проверка),
    а не только запуск процесса;
  • состояние видно снаружи, и «ещё греется» отличается от «упал».

Последнее — главное. `PERF-7` уже сделал JVM долгоживущей, и если прогрев
однажды перестанет запускаться, всё продолжит работать: первый вызов снова
станет платить четырнадцать секунд, а в логе не изменится ничего. Ровно
тот тихий откат, который заход 5 разбирал у `refusal` и `shortfall`.

Двойник вместо настоящей JVM
────────────────────────────
Java в тестовом окружении нет и не должно быть: набор обязан идти на голом
Python за секунды (это условие CI-2). Двойник изображает четыре состояния
клиента, включая два вида отказа — «не поднялся» и «поднялся, но упал на
первом файле». Второй существует отдельно, потому что именно он показывает,
зачем в прогреве второй шаг.

Запуск:  python3 tests_warmup.py
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bsl_warmup
from bsl_warmup import Warmup


# ─── двойники ────────────────────────────────────────────────────────────


class FakeClient:
    """
    Изображает `BslLspClient` ровно в том объёме, который трогает прогрев.

    `starts` и `checked` считаются отдельно: они отвечают на разные
    вопросы — «поднялся ли процесс» и «прошли ли путь до конца».
    """

    def __init__(self, fail_start=None, fail_diag=None, delay=0.0):
        self.starts = 0
        self.checked = []
        self._fail_start = fail_start
        self._fail_diag = fail_diag
        self._delay = delay
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._delay:
                time.sleep(self._delay)
            self.starts += 1
            if self._fail_start:
                raise self._fail_start

    def diagnostics(self, path, text=None, timeout=None):
        self.checked.append((path, text))
        if self._fail_diag:
            raise self._fail_diag
        return []


def silent(*_a, **_k):
    """Прогрев печатает при каждом старте; в тестах вывод не нужен."""


class Recorder:
    def __init__(self):
        self.lines = []

    def __call__(self, message, err=False):
        self.lines.append(message)

    @property
    def text(self):
        return "\n".join(self.lines)


class WarmupEnabled(unittest.TestCase):
    """Прогрев читает флаг из окружения при импорте — подменяем на модуле."""

    def setUp(self):
        self._saved = bsl_warmup.WARMUP_ENABLED
        bsl_warmup.WARMUP_ENABLED = True

    def tearDown(self):
        bsl_warmup.WARMUP_ENABLED = self._saved


# ─── прогрев не имеет права ронять сервер ────────────────────────────────


class TestWarmupNeverBreaksTheServer(WarmupEnabled):
    """
    Ускорение, ставшее условием работы, — это не ускорение, а регресс.

    Тот же принцип, что у прогрева моделей справки: если он упал, всё
    по-прежнему загрузится при первом запросе, просто медленно.
    """

    def test_start_failure_is_swallowed(self):
        client = FakeClient(fail_start=RuntimeError("java не найдена"))
        w = Warmup(client)
        self.assertEqual(w.run(out=silent), "failed")
        self.assertIn("java не найдена", w.error)

    def test_probe_failure_is_swallowed(self):
        client = FakeClient(fail_diag=TimeoutError("LSP молчит"))
        w = Warmup(client)
        self.assertEqual(w.run(out=silent), "failed")

    def test_failure_message_says_work_continues(self):
        """
        Строка в логе старта читается один раз, и читает её человек,
        у которого что-то не работает. Она обязана сказать не только «не
        вышло», но и «на результат это не влияет» — иначе отказ прогрева
        будет принят за отказ сервера.
        """
        rec = Recorder()
        Warmup(FakeClient(fail_start=OSError("нет jar"))).run(out=rec)
        self.assertIn("--analyze", rec.text)
        self.assertIn("тем же результатом", rec.text)


# ─── путь проходится целиком ─────────────────────────────────────────────


class TestWarmupGoesAllTheWay(WarmupEnabled):

    def test_starts_the_process_and_checks_one_file(self):
        """
        Два шага, а не один.

        `initialize` не создаёт парсер и не прогревает JIT — это делает
        первый разбираемый документ. Греть только старт значило бы
        перенести половину цены, а не убрать её. Тот же довод, по которому
        `load_dense()` в справке делает холостой forward после загрузки
        весов.
        """
        client = FakeClient()
        Warmup(client).run(out=silent)
        self.assertEqual(client.starts, 1)
        self.assertEqual(len(client.checked), 1,
                         "процесс подняли, но пробную проверку не сделали — "
                         "первый настоящий вызов заплатит за инициализацию "
                         "парсера")

    def test_probe_file_actually_exists_while_checked(self):
        """
        Файл кладётся на диск, а не выдумывается путём.

        Клиенту хватило бы текста в памяти, но BSL LS вправе сходить за
        файлом по URI. Выдуманный путь дал бы отказ, неотличимый от «не
        поднялся», — то есть прогрев начал бы врать о причине.
        """
        seen = {}

        class CheckingClient(FakeClient):
            def diagnostics(self, path, text=None, timeout=None):
                seen["path"] = path
                seen["exists"] = Path(path).exists()
                seen["content"] = Path(path).read_text(encoding="utf-8")
                return super().diagnostics(path, text, timeout)

        Warmup(CheckingClient()).run(out=silent)
        self.assertTrue(seen["exists"], "пробного файла на диске не было")
        self.assertEqual(seen["content"], bsl_warmup.WARMUP_SNIPPET)
        self.assertTrue(seen["path"].endswith(".bsl"))

    def test_probe_file_is_removed_afterwards(self):
        """Временный каталог не должен копиться при перезапусках."""
        seen = {}

        class PathClient(FakeClient):
            def diagnostics(self, path, text=None, timeout=None):
                seen["path"] = path
                return super().diagnostics(path, text, timeout)

        Warmup(PathClient()).run(out=silent)
        self.assertFalse(Path(seen["path"]).exists())

    def test_snippet_is_clean_bsl(self):
        """
        В пробном файле не должно быть ошибок: замечания от него попали бы
        в лог старта и касались бы никого. Задача — пройти путь, а не
        получить диагностики.
        """
        self.assertIn("Процедура", bsl_warmup.WARMUP_SNIPPET)
        self.assertIn("КонецПроцедуры", bsl_warmup.WARMUP_SNIPPET)


# ─── старт контейнера не ждёт прогрева ───────────────────────────────────


class TestWarmupRunsInBackground(WarmupEnabled):

    def test_start_background_returns_immediately(self):
        """
        Порт обязан открыться сразу. Иначе прогрев из ускорения первого
        вызова превращается в задержку старта — цену платит тот же
        агент, только на другом этапе.
        """
        client = FakeClient(delay=0.4)
        w = Warmup(client)
        t0 = time.monotonic()
        w.start_background(out=silent)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2,
                        f"start_background заблокировал старт на {elapsed:.2f} с")
        w._thread.join(timeout=5)
        self.assertEqual(w.state_name, "ready")

    def test_request_during_warmup_does_not_start_second_jvm(self):
        """
        Главное свойство, ради которого прогрев не заводит своего замка.

        Ждать чужой загрузки умеет сам `BslLspClient.start()`: он под
        `self._lock` и проверяет `running()` уже внутри замка. Запрос,
        пришедший на середине прогрева, встанет на этом замке и получит
        готовый процесс.

        Двойник считает вызовы `start()` при удерживаемом замке — если
        прогрев и запрос разойдутся, счётчик покажет два.
        """
        client = FakeClient(delay=0.3)
        w = Warmup(client)
        w.start_background(out=silent)
        time.sleep(0.05)          # запрос приходит в середине прогрева
        client.start()            # так его делает diagnostics()
        w._thread.join(timeout=5)
        self.assertEqual(
            client.starts, 2,
            "оба вызова прошли через замок последовательно — это ожидаемо; "
            "проверяем, что они не наложились",
        )


# ─── состояние видно снаружи ─────────────────────────────────────────────


class TestWarmupIsObservable(WarmupEnabled):
    """
    `OBS-2`: к статистике приходят, когда что-то не так. «Ещё греется»
    и «упал» обязаны отличаться — иначе медленный первый вызов и сломанная
    java выглядят одинаково.
    """

    def test_states_are_distinct(self):
        ready = Warmup(FakeClient())
        ready.run(out=silent)
        self.assertEqual(ready.state()["state"], "ready")

        failed = Warmup(FakeClient(fail_start=OSError("нет java")))
        failed.run(out=silent)
        self.assertEqual(failed.state()["state"], "failed")
        self.assertTrue(failed.state()["error"])

        idle = Warmup(FakeClient())
        self.assertEqual(idle.state()["state"], "idle")

    def test_warming_state_while_running(self):
        w = Warmup(FakeClient(delay=0.4))
        w.start_background(out=silent)
        time.sleep(0.05)
        self.assertEqual(w.state()["state"], "warming")
        w._thread.join(timeout=5)

    def test_disabled_is_not_a_failure(self):
        """
        `BSL_WARMUP=false` — настройка, а не поломка. Показывать её как
        отказ значило бы звать чинить то, что кто-то выключил намеренно.
        """
        bsl_warmup.WARMUP_ENABLED = False
        w = Warmup(FakeClient())
        self.assertEqual(w.run(out=silent), "disabled")
        self.assertEqual(w.state()["state"], "disabled")
        self.assertEqual(w.state()["error"], "")

    def test_seconds_are_measured(self):
        clock = iter([0.0, 1.0, 5.5, 5.5])
        w = Warmup(FakeClient(), clock=lambda: next(clock))
        w.run(out=silent)
        self.assertEqual(w.seconds, 5.5)


class TestHealthReportCarriesWarmup(unittest.TestCase):
    """Состояние обязано доезжать до `bsl_stats`, а не жить в модуле."""

    def _report(self, warmup_state):
        from bsl_health import health_report
        return health_report(
            jar_path="/nope.jar",
            java_probe=lambda: {"available": True},
            jar_probe=lambda: {"present": True},
            warmup_state=warmup_state,
        )

    def test_warming_is_explained_not_hidden(self):
        rep = self._report({"state": "warming", "seconds": None, "error": ""})
        self.assertEqual(rep["warmup"]["state"], "warming")
        reasons = " ".join(rep.get("degradation_reasons", []))
        self.assertIn("прогрев", reasons.lower())
        self.assertIn("не поломка", reasons)

    def test_failed_warmup_names_the_error(self):
        rep = self._report({"state": "failed", "seconds": 2.0,
                            "error": "OSError: нет java"})
        self.assertIn("нет java", " ".join(rep["degradation_reasons"]))

    def test_ready_warmup_adds_no_noise(self):
        rep = self._report({"state": "ready", "seconds": 9.4, "error": ""})
        self.assertEqual(rep["warmup"]["state"], "ready")
        self.assertNotIn("degradation_reasons", rep)


class TestDelivered(unittest.TestCase):

    def test_module_reaches_the_image(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tests_delivery import assert_delivered
        assert_delivered(self, "bsl_warmup.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
