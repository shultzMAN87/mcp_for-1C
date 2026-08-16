"""
Тесты B-7: состояние bsl-checker.

Проверяется не «поля на месте», а два свойства, ради которых инструмент
писался:

  • он отвечает и тогда, когда анализатор мёртв (иначе бесполезен ровно в
    тот момент, для которого сделан);
  • он не выдаёт неготовность за готовность — `linter_available` false во
    всех трёх нерабочих сочетаниях, а не только в очевидном.

Плюс контрактная часть: модуль обязан доехать в образ и быть вызванным из
server.py. Забытая строка COPY — отдельный жанр в этом проекте, и стоит она
подъёма всего стека.

Запуск:  python3 tests_bsl_health.py
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bsl_health import (  # noqa: E402
    AnalysisLog,
    health_report,
    jar_manifest,
    jar_version,
    parse_java_version,
    probe_jar,
    probe_java,
)

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


TEMURIN = (
    'openjdk version "17.0.10" 2024-01-16\n'
    'OpenJDK Runtime Environment Temurin-17.0.10+7 (build 17.0.10+7)\n'
    'OpenJDK 64-Bit Server VM Temurin-17.0.10+7 (build 17.0.10+7, mixed mode)\n'
)


class TestJavaVersionParsing(unittest.TestCase):

    def test_temurin(self):
        parsed = parse_java_version(TEMURIN)
        self.assertEqual(parsed["version"], "17.0.10")
        self.assertIn("Temurin", parsed["runtime"])

    def test_oracle_style(self):
        parsed = parse_java_version('java version "1.8.0_392"\nJava(TM) SE...')
        self.assertEqual(parsed["version"], "1.8.0_392")

    def test_unknown_format_does_not_explode(self):
        """
        Диагностика, падающая на незнакомой сборке JVM, хуже диагностики,
        которая честно говорит «вижу вот это, разобрать не смог».
        """
        parsed = parse_java_version("какая-то незнакомая строка")
        self.assertEqual(parsed["version"], "")
        self.assertEqual(parsed["raw"], "какая-то незнакомая строка")

    def test_empty(self):
        self.assertEqual(parse_java_version("")["version"], "")


class TestProbeJava(unittest.TestCase):

    def test_available(self):
        out = probe_java(runner=lambda cmd, t: _FakeResult(0, "", TEMURIN))
        self.assertTrue(out["available"])
        self.assertEqual(out["version"], "17.0.10")
        self.assertEqual(out["error"], "")

    def test_version_goes_to_stderr_and_that_is_normal(self):
        """`java -version` пишет в stderr — это её штатное поведение."""
        out = probe_java(runner=lambda cmd, t: _FakeResult(0, TEMURIN, ""))
        self.assertTrue(out["available"], "stdout тоже надо читать")

    def test_missing_binary(self):
        def boom(cmd, t):
            raise FileNotFoundError(cmd[0])

        out = probe_java(runner=boom)
        self.assertFalse(out["available"])
        self.assertIn("не найден", out["error"])

    def test_timeout(self):
        def slow(cmd, t):
            raise subprocess.TimeoutExpired(cmd, t)

        out = probe_java(timeout=2, runner=slow)
        self.assertFalse(out["available"])
        self.assertIn("2", out["error"])

    def test_nonzero_exit(self):
        out = probe_java(runner=lambda cmd, t: _FakeResult(1, "", "сломалось"))
        self.assertFalse(out["available"])
        self.assertIn("код 1", out["error"])

    def test_any_exception_is_swallowed(self):
        """Проба не имеет права уронить инструмент, к которому идут за диагнозом."""
        def boom(cmd, t):
            raise RuntimeError("что-то совсем неожиданное")

        self.assertFalse(probe_java(runner=boom)["available"])


class TestJarProbe(unittest.TestCase):

    def _make_jar(self, tmp: Path, manifest: str, padding: int = 4096) -> Path:
        jar = tmp / "bsl-ls.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("META-INF/MANIFEST.MF", manifest)
            zf.writestr("padding.bin", b"\0" * padding)
        return jar

    def test_version_from_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            jar = self._make_jar(Path(td),
                                 "Manifest-Version: 1.0\r\n"
                                 "Implementation-Title: bsl-language-server\r\n"
                                 "Implementation-Version: 0.28.5\r\n")
            self.assertEqual(jar_version(jar), "0.28.5")
            out = probe_jar(jar)
            self.assertTrue(out["present"])
            self.assertEqual(out["version"], "0.28.5")
            self.assertEqual(out["error"], "")

    def test_manifest_continuation_lines_are_glued(self):
        """
        Манифест режет длинные строки на 72 байта с переносом через пробел.
        Не склеим — получим обрубок версии и «диагностику», которая врёт.
        """
        with tempfile.TemporaryDirectory() as td:
            jar = self._make_jar(Path(td),
                                 "Implementation-Version: 0.28\n .5-SNAPSHOT\n")
            self.assertEqual(jar_version(jar), "0.28.5-SNAPSHOT")

    def test_version_from_filename_when_manifest_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            jar = self._make_jar(tmp, "Manifest-Version: 1.0\n")
            renamed = tmp / "bsl-language-server-0.27.1-exec.jar"
            jar.rename(renamed)
            self.assertEqual(jar_version(renamed), "0.27.1")

    def test_missing_file(self):
        out = probe_jar("/нет/такого/bsl-ls.jar")
        self.assertFalse(out["present"])
        self.assertEqual(out["error"], "файла нет")

    def test_stub_is_not_a_jar(self):
        """
        Урок FIX-11: сорок заглушек по 0,0 МБ прошли проверку с галочкой.
        «Файл существует» и «файл рабочий» — разные утверждения.
        """
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "bsl-ls.jar"
            stub.write_bytes(b"")
            out = probe_jar(stub)
            self.assertFalse(out["present"])
            self.assertIn("заглушка", out["error"])

    def test_broken_zip_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            junk = Path(td) / "bsl-ls.jar"
            junk.write_bytes(b"x" * 5000)
            self.assertEqual(jar_manifest(junk), {})
            out = probe_jar(junk)
            self.assertTrue(out["present"], "файл есть и он не пустой")
            self.assertEqual(out["version"], "")
            self.assertIn("версия не определена", out["error"])


class TestAnalysisLog(unittest.TestCase):

    def test_counters(self):
        log = AnalysisLog()
        log.record_ok(1.5)
        log.record_fail("analysis_timeout", "не уложился")
        log.record_fail("analysis_timeout", "снова")
        snap = log.snapshot()
        self.assertEqual((snap["runs_total"], snap["runs_ok"], snap["runs_failed"]),
                         (3, 1, 2))
        self.assertEqual(snap["failures_by_error"]["analysis_timeout"], 2)
        self.assertEqual(snap["last_fail_error"], "analysis_timeout")
        self.assertEqual(snap["last_ok_duration_sec"], 1.5)

    def test_empty_log_says_so(self):
        """
        Нули обязаны читаться как «не запускали», а не как «сломано».
        Иначе свежеподнятый контейнер выглядит аварийным.
        """
        snap = AnalysisLog().snapshot()
        self.assertEqual(snap["runs_total"], 0)
        self.assertEqual(snap["last_ok_iso"], "")
        self.assertIsNone(snap["last_ok_age_sec"])
        self.assertIn("не запускали", snap["note"])

    def test_age_is_counted(self):
        clock = {"t": 1000.0}
        log = AnalysisLog(clock=lambda: clock["t"])
        log.record_ok(0.5)
        clock["t"] = 1090.0
        self.assertEqual(log.snapshot()["last_ok_age_sec"], 90.0)

    def test_parallel_calls_do_not_lose_records(self):
        """
        FastMCP исполняет синхронные инструменты в рабочем потоке на запрос:
        два анализа идут параллельно, и счётчик без блокировки теряет часть.
        """
        log = AnalysisLog()

        def worker():
            for _ in range(200):
                log.record_ok(0.01)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(log.snapshot()["runs_total"], 800)


def _java(ok=True):
    return {"available": ok, "command": "java",
            "version": "17.0.10" if ok else "",
            "runtime": "", "probe_ms": 12,
            "error": "" if ok else "исполняемый файл не найден: java"}


def _jar(ok=True):
    return {"path": "/opt/bsl-language-server/bsl-ls.jar", "present": ok,
            "version": "0.28.5" if ok else "", "size_mb": 43.2 if ok else 0.0,
            "modified_iso": "", "error": "" if ok else "файла нет"}


def _report(java_ok=True, jar_ok=True, **kw):
    return health_report(
        jar_path="/opt/bsl-language-server/bsl-ls.jar",
        java_probe=lambda: _java(java_ok),
        jar_probe=lambda: _jar(jar_ok),
        **kw,
    )


class TestHealthReport(unittest.TestCase):

    def test_healthy(self):
        r = _report()
        self.assertTrue(r["linter_available"])
        self.assertTrue(r["answerable"])
        self.assertFalse(r["degraded"])
        self.assertNotIn("degradation_reasons", r)

    def test_all_three_broken_combinations(self):
        """
        Неготовность видна во всех сочетаниях, а не только в очевидном.
        Проверять по одному «нет java» — как раз тот случай, когда тест
        зелёный, а инструмент врёт.
        """
        for java_ok, jar_ok in ((False, True), (True, False), (False, False)):
            with self.subTest(java=java_ok, jar=jar_ok):
                r = _report(java_ok, jar_ok)
                self.assertFalse(r["linter_available"])
                self.assertTrue(r["degraded"])
                self.assertTrue(r["degradation_reasons"])
                self.assertIn("hint", r)

    def test_answers_even_when_broken(self):
        """
        Главное свойство инструмента состояния. `answerable: false` здесь
        означал бы «не могу сказать, как я себя чувствую» — то есть отказ
        ровно в тот момент, ради которого инструмент и написан. Так же
        устроен metadata_stats после FIX-3: он обязан отвечать на пустом
        графе.
        """
        r = _report(java_ok=False, jar_ok=False)
        self.assertTrue(r["answerable"])
        self.assertIn("НЕ значит", r["meaning"])

    def test_meaning_warns_against_silence_being_read_as_cleanliness(self):
        r = _report(java_ok=False)
        self.assertIn("bsl_check_", r["meaning"])

    def test_missing_config_is_noticed_but_not_fatal(self):
        """
        Без своего конфига BSL LS работает на наборе по умолчанию — это не
        отказ. Но состав замечаний будет не тот, которого ждут, и молчать
        об этом нельзя.
        """
        r = _report(config_path="/нет/такого/config.json")
        self.assertTrue(r["linter_available"])
        self.assertTrue(r["degraded"])
        self.assertFalse(r["config"]["present"])

    def test_no_config_at_all_is_normal(self):
        r = _report(config_path="")
        self.assertFalse(r["degraded"])
        self.assertIn("note", r["config"])

    def test_analysis_block_carries_settings_and_counters(self):
        log = AnalysisLog()
        log.record_fail("analysis_timeout", "не уложился")
        r = _report(log=log, analysis_timeout_sec=90, java_opts="-Xmx1g")
        self.assertEqual(r["analysis"]["timeout_sec"], 90)
        self.assertEqual(r["analysis"]["java_opts"], "-Xmx1g")
        self.assertEqual(r["analysis"]["runs_failed"], 1)

    def test_report_is_json_serializable(self):
        """Инструмент отдаёт строку JSON — несериализуемое поле уронит вызов."""
        json.dumps(_report(), ensure_ascii=False)

    def test_says_it_is_not_cached(self):
        """
        Урок 15 августа: metadata_stats отдавал из кеша картину здоровья
        работающего графа во время аварии. Кешированный ответ на вопрос
        «жив ли ты прямо сейчас» не устарел, а перевёрнут. Здесь кеша нет,
        и ответ это заявляет.
        """
        self.assertIn("кеша нет", _report()["probe_note"])


class TestDelivery(unittest.TestCase):
    """
    Контрактная часть по образцу `tests_graph_contract`: сверяем не поведение,
    а исходники — доехал ли модуль в образ и вызван ли он сервером.
    """

    def test_copy_line_in_dockerfile(self):
        """
        B-6: имя Dockerfile здесь больше не прибито. Раньше стояло
        `Dockerfile.bsl` строкой — то есть третья копия одной и той же
        проверки со своим списком образов. Карту строит tests_delivery.py.
        """
        sys.path.insert(0, str(SUITE))
        from tests_delivery import assert_delivered
        assert_delivered(self, "bsl_health.py")

    def test_server_registers_the_tool(self):
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("def bsl_stats(", src)
        self.assertIn("from bsl_health import", src)

    def test_every_analysis_outcome_is_recorded(self):
        """
        Учёт должен быть в обёртке, а не расставлен по точкам возврата:
        иначе следующая ветка отказа появится без записи, и счётчик тихо
        разойдётся с действительностью.
        """
        src = (HERE / "server.py").read_text(encoding="utf-8")
        # PERF-7 переименовал внутреннюю функцию: `_run_analysis_inner`
        # стал `_analyze_dir` — теперь это не «внутренность», а один из
        # двух путей анализа, и имя должно говорить, какой именно.
        self.assertIn("_analyze_dir", src)
        self.assertIn("record_fail", src)
        self.assertIn("record_ok", src)
        # Учёт по-прежнему в одном месте: обёртка `_run_analysis` знает про
        # оба пути, а не каждый путь про журнал.
        self.assertEqual(src.count("_analysis_log.record_"), 3,
                         "запись исхода расползлась по точкам возврата")

    def test_tool_is_measured(self):
        """
        Инструмент без единого примера в датасете — ровно то состояние, в
        котором `bsl_check_*` прожил с молчаливым «ошибок не найдено».
        """
        dataset = SUITE.parent / "evals" / "datasets" / "bsl_checker.jsonl"
        self.assertTrue(dataset.exists(), "нет evals/datasets/bsl_checker.jsonl")
        self.assertIn('"bsl_stats"', dataset.read_text(encoding="utf-8"),
                      "bsl_stats не покрыт ни одним примером датасета")


if __name__ == "__main__":
    unittest.main(verbosity=2)
