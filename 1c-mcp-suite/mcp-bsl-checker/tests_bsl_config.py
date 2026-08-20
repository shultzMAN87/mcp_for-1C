"""
Тесты CFG-4: файл настроек диагностик доезжает до анализатора.

Три группы, и они про разное:

  1. `describe` — читает ли модуль файл правильно и не падает ли на битом.
  2. `brief` / `degradation_reason` — говорит ли ответ вслух, каким
     набором правил он получен.
  3. **FIX-30** — доезжает ли конфигурация до ОБОИХ путей анализа.

Третья группа — главная. Первые две проверяют модуль, который написан
сегодня; третья проверяет дефект, который прожил в проекте с PERF-7 и был
невидим ровно потому, что механизм существовал наполовину: переменная
читалась, ключ собирался, `bsl_stats` показывал файл на месте — а
`--analyze` запускался без него.

Тест на такой дефект нельзя написать «по коду»: если проверять, что
`_run_analysis` умеет принимать `config_path`, тест был бы зелёным и до
правки. Проверять надо КОМАНДУ, которая ушла в JVM, — то есть подменять
subprocess и смотреть argv. Ровно так же устроен `tests_graph_contract.py`
(TEST-1): он сверяет не намерение, а текст того, что уходит наружу.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bsl_config


def _write(tmpdir: str, name: str, content: str) -> str:
    p = Path(tmpdir) / name
    p.write_text(content, encoding="utf-8")
    return str(p)


VALID = json.dumps({
    "language": "ru",
    "configurationRoot": "УБД",
    "diagnostics": {
        "mode": "ONLY",
        "parameters": {
            "UsingModalWindows": True,
            "EmptyCodeBlock": True,
            "UnusedLocalMethod": False,
        },
    },
}, ensure_ascii=False)


class TestDescribe(unittest.TestCase):

    def test_no_path_is_not_an_error(self):
        """Отсутствие своего конфига — штатный режим, а не поломка."""
        info = bsl_config.describe("")
        self.assertFalse(info["requested"])
        self.assertFalse(info["applied"])
        self.assertIn("note", info)
        self.assertEqual(bsl_config.config_arg(info), "")
        self.assertEqual(bsl_config.degradation_reason(info), "")

    def test_valid_file_is_applied_and_described(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
        self.assertTrue(info["applied"])
        self.assertEqual(info["mode"], "ONLY")
        self.assertEqual(info["language"], "ru")
        self.assertEqual(info["configuration_root"], "УБД")
        self.assertEqual(info["diagnostics_declared"], 3)
        # Выключена только та, у которой явное false.
        self.assertEqual(info["diagnostics_enabled"], 2)
        self.assertTrue(info["fingerprint"].startswith("sha256:"))

    def test_missing_file_degrades_but_does_not_raise(self):
        info = bsl_config.describe("/нет/такого/файла.json")
        self.assertTrue(info["requested"])
        self.assertFalse(info["present"])
        self.assertFalse(info["applied"])
        self.assertIn("не найден", info["error"])
        self.assertIn("ПО УМОЛЧАНИЮ", bsl_config.degradation_reason(info))

    def test_broken_json_is_not_passed_to_analyzer(self):
        """
        Главное решение модуля. Битый конфиг НЕ уходит в `--configuration`:
        BSL LS с ним не поднимается ни быстрым путём, ни запасным, и
        вместо деградации (проверили не тем набором) получился бы полный
        отказ (не проверили вовсе).
        """
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", "{ это не json"))
        self.assertTrue(info["present"])
        self.assertFalse(info["valid"])
        self.assertFalse(info["applied"])
        self.assertEqual(bsl_config.config_arg(info), "")
        self.assertIn("JSON", info["error"])

    def test_bom_does_not_break_parsing(self):
        """
        Файл настроек правят в блокноте Windows, и BOM в начале — обычное
        дело. json.loads на нём падает с «Expecting value: line 1», то
        есть рабочий файл выглядел бы битым.
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_bytes(b"\xef\xbb\xbf" + VALID.encode("utf-8"))
            info = bsl_config.describe(str(p))
        self.assertTrue(info["applied"])

    def test_fingerprint_changes_with_content(self):
        """Отпечаток отвечает на вопрос «контейнер читает новый файл?»."""
        with tempfile.TemporaryDirectory() as td:
            a = bsl_config.describe(_write(td, "a.json", VALID))
            b = bsl_config.describe(
                _write(td, "b.json", VALID.replace("ONLY", "EXCEPT")))
        self.assertNotEqual(a["fingerprint"], b["fingerprint"])


class TestConfigurationRoot(unittest.TestCase):
    """
    `configurationRoot` ищется относительно АНАЛИЗИРУЕМОГО каталога.
    Не нашёлся — BSL LS молчит, а часть диагностик не срабатывает: отчёт
    выглядит чище, чем код. Молчание здесь и есть дефект.
    """

    def test_warns_when_root_missing_in_src(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
            warning = bsl_config.check_root(info, td)
        self.assertIn("УБД", warning)
        self.assertIn("ОТНОСИТЕЛЬНО", warning)

    def test_silent_when_root_present(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "УБД").mkdir()
            info = bsl_config.describe(_write(td, "c.json", VALID))
            self.assertEqual(bsl_config.check_root(info, td), "")

    def test_silent_when_root_not_declared(self):
        info = bsl_config.describe("")
        self.assertEqual(bsl_config.check_root(info, "/tmp"), "")


class TestBrief(unittest.TestCase):

    def test_applied_config_names_its_ruleset(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
        section = bsl_config.brief(info)
        self.assertTrue(section["applied"])
        self.assertEqual(section["mode"], "ONLY")
        self.assertIn("fingerprint", section)

    def test_unapplied_config_says_so_in_the_answer(self):
        """
        «Замечаний не найдено» при mode: ONLY значит «не найдено ИЗ ЭТОГО
        СПИСКА». Если список не тот, ответ обязан это сказать сам.
        """
        section = bsl_config.brief(bsl_config.describe("/нет/файла.json"))
        self.assertFalse(section["applied"])
        self.assertIn("по умолчанию", section["ruleset"])
        self.assertIn("error", section)


class TestConfigReachesBothPaths(unittest.TestCase):
    """
    FIX-30. Дефект, ради которого писался весь заход.

    До правки: параметр `config_path` у `_run_analysis` существовал, но его
    не передавал ни один из трёх инструментов (`server.py`, строки 373, 439,
    491). Конфигурация доезжала только до долгоживущего BSL LS; `--analyze`
    работал набором по умолчанию. Один файл давал разный состав замечаний в
    зависимости от того, жив ли LSP, — и признака в ответе не было.

    Проверяем не намерение, а argv: что именно уйдёт в JVM. Первая версия
    этих тестов импортировала `server.py` и была бы честнее — но `server.py`
    не импортируется без пакета `mcp`, то есть набор молча не запускался бы
    в прогоне на голом Python. Ровно тот случай, ради которого в `A-5`
    появился `--strict`: не запущенная проверка выглядит как пройденная.
    Поэтому сборка argv переехала в `bsl_config`, и проверяется она там.
    """

    JAVA, OPTS, JAR = "java", "-Xmx1g", "/opt/bsl-ls.jar"

    def test_analyze_path_carries_configuration(self):
        argv = bsl_config.analyze_argv(
            self.JAVA, self.OPTS, self.JAR, "/data/1c-src", "/tmp/out",
            "/app/bsl-config/bsl-language-server.json")
        self.assertIn("--configuration", argv)
        self.assertEqual(argv[argv.index("--configuration") + 1],
                         "/app/bsl-config/bsl-language-server.json")
        # Прежние ключи на месте: правка не должна была тронуть FIX-9.
        self.assertEqual(argv[argv.index("--srcDir") + 1], "/data/1c-src")
        self.assertEqual(argv[argv.index("--outputDir") + 1], "/tmp/out")
        self.assertEqual(argv[argv.index("--reporter") + 1], "json")

    def test_lsp_path_carries_the_same_configuration(self):
        """
        Оба пути обязаны идти одним набором правил — иначе обещание PERF-7
        «ответ будет тот же, только медленный» не выполняется.
        """
        cfg = "/app/bsl-config/bsl-language-server.json"
        a = bsl_config.analyze_argv(
            self.JAVA, self.OPTS, self.JAR, "/src", "/out", cfg)
        l = bsl_config.lsp_argv(self.JAVA, self.OPTS, self.JAR, cfg)
        self.assertEqual(a[a.index("--configuration") + 1],
                         l[l.index("--configuration") + 1])

    def test_no_config_means_no_key_at_all(self):
        """
        Пустая строка осталась осмысленной: «намеренно без конфигурации».
        Ею пользуется сверка `bsl_lsp.py --compare`; подменить её общим
        значением значило бы сломать сравнение, которое она делает.
        """
        for argv in (
            bsl_config.analyze_argv(self.JAVA, "", self.JAR, "/s", "/o", ""),
            bsl_config.lsp_argv(self.JAVA, "", self.JAR, ""),
        ):
            self.assertNotIn("--configuration", argv)

    def test_both_callers_use_the_shared_builder(self):
        """
        Проверка текстом — как в `tests_graph_contract.py` (TEST-1). Смысл
        не в стиле: пока команду собирают в двух местах, следующий ключ
        заведёт расхождение заново, и FIX-30 повторится под другим номером.
        """
        here = Path(__file__).resolve().parent
        server_src = (here / "server.py").read_text(encoding="utf-8")
        lsp_src = (here / "bsl_lsp.py").read_text(encoding="utf-8")

        self.assertIn("bsl_config.analyze_argv", server_src)
        self.assertIn("bsl_config.lsp_argv", lsp_src)
        # Своей сборки argv не осталось ни там, ни там.
        for name, src in (("server.py", server_src), ("bsl_lsp.py", lsp_src)):
            self.assertNotIn(
                '"--analyze",\n', src,
                f"{name}: команда для JVM снова собирается на месте")

    def test_broken_config_reaches_neither_path(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", "{ битый"))
        arg = bsl_config.config_arg(info)
        self.assertEqual(arg, "")
        self.assertNotIn("--configuration",
                         bsl_config.lsp_argv(self.JAVA, "", self.JAR, arg))


class TestStrictMode(unittest.TestCase):
    """
    Строгий режим: отказ вместо проверки не тем набором правил.
    Продолжение A-5 — «не запускалось» и «проверено» не должны выглядеть
    одинаково.
    """

    def test_refuses_when_config_requested_but_missing(self):
        info = bsl_config.describe("/нет/файла.json")
        self.assertTrue(bsl_config.should_refuse(info, strict=True))

    def test_silent_when_config_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
        self.assertFalse(bsl_config.should_refuse(info, strict=True))

    def test_silent_when_config_was_never_requested(self):
        """
        Строгий режим не должен ломать стенд, где конфигурации нет вовсе:
        это осознанный режим работы, а не поломка.
        """
        self.assertFalse(
            bsl_config.should_refuse(bsl_config.describe(""), strict=True))

    def test_off_by_default_degrades_instead(self):
        info = bsl_config.describe("/нет/файла.json")
        self.assertFalse(bsl_config.should_refuse(info, strict=False))
        self.assertTrue(bsl_config.degradation_reason(info))


class TestAnswersCarryTheRuleset(unittest.TestCase):
    """
    Секция `config` в ответах инструментов проверки. Без неё «замечаний не
    найдено» невозможно ни воспроизвести, ни сравнить с прошлым отчётом:
    при `mode: ONLY` это значит «не найдено ИЗ ЭТОГО СПИСКА».
    """

    def test_every_check_tool_reports_its_config(self):
        source = (Path(__file__).resolve().parent / "server.py").read_text(
            encoding="utf-8")
        # Пять точек возврата у трёх инструментов: два исхода у
        # bsl_check_code, два у bsl_check_file, один у bsl_check_directory.
        self.assertGreaterEqual(
            source.count('"config": config'), 5,
            "не во всех ответах инструментов проверки есть секция config")

    def test_strict_flag_is_wired_to_the_server(self):
        source = (Path(__file__).resolve().parent / "server.py").read_text(
            encoding="utf-8")
        self.assertIn("BSL_LS_CONFIG_STRICT", source)
        self.assertEqual(source.count("_strict_refusal()"), 4,
                         "строгий режим подключён не ко всем трём проверкам")


class TestMountedWorkspaceCheck(unittest.TestCase):
    """
    CFG-4.1. `check_root` срабатывает только при вызове проверки каталога —
    то есть о неработающем `configurationRoot` узнаёшь, уже читая отчёт и
    веря ему. `bsl_stats` обязан отвечать на этот вопрос заранее.
    """

    def test_resolves_when_root_is_in_the_mounted_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "УБД").mkdir()
            info = bsl_config.describe(_write(td, "c.json", VALID))
            out = bsl_config.check_mounted_workspace(info, roots=(td,))
        self.assertTrue(out["resolves"])
        self.assertEqual(out["found_in"], td)

    def test_warns_when_root_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
            out = bsl_config.check_mounted_workspace(info, roots=(td,))
        self.assertFalse(out["resolves"])
        self.assertIn("не найден", out["warning"])
        self.assertEqual(out["searched"], [td])

    def test_says_nothing_when_root_not_declared(self):
        self.assertEqual(bsl_config.check_mounted_workspace(
            bsl_config.describe("")), {})

    def test_no_mounted_workspace_is_not_a_warning(self):
        """
        Выгрузка не смонтирована — проверять нечем, и это не повод пугать:
        для bsl_check_directory это и так означает, что анализировать
        нечего.
        """
        with tempfile.TemporaryDirectory() as td:
            info = bsl_config.describe(_write(td, "c.json", VALID))
        out = bsl_config.check_mounted_workspace(info, roots=("/нет/такого",))
        self.assertIsNone(out["resolves"])
        self.assertNotIn("warning", out)

    def test_source_roots_match_bsl_lsp(self):
        """
        Два списка путей монтирования в одном образе — ровно тот жанр,
        который в этом проекте расходился пять раз. Копия неизбежна
        (bsl_lsp импортирует bsl_config, не наоборот), поэтому её сторожит
        тест.
        """
        import bsl_lsp
        self.assertEqual(tuple(bsl_config.SOURCE_ROOTS),
                         tuple(bsl_lsp.SOURCE_ROOTS))


class TestLspModeIsWired(unittest.TestCase):
    """
    CFG-4.1. README с самого начала обещал `BSL_LSP_MODE=off`, а переменная
    в контейнер не пробрасывалась: рычаг описан, рычага нет. Тот же жанр,
    что FIX-30, и ловится так же — текстом того, что уходит наружу.
    """

    def test_compose_passes_lsp_mode_to_the_container(self):
        root = Path(__file__).resolve().parents[2]
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("BSL_LSP_MODE=${BSL_LSP_MODE:-auto}", compose)

    def test_documented_bsl_variables_are_all_passed(self):
        root = Path(__file__).resolve().parents[2]
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        for name in ("BSL_LS_JAR", "BSL_WARMUP", "BSL_LSP_MODE",
                     "BSL_LS_CONFIG", "BSL_LS_CONFIG_STRICT"):
            self.assertIn(f"- {name}=", compose,
                          f"{name} документирован, но в контейнер не идёт")


if __name__ == "__main__":
    unittest.main(verbosity=2)
