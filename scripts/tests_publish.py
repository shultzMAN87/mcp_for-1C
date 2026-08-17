"""
Тесты DOC-2: проверка перед публикацией сама должна быть проверена.

Зачем набор на диагностический скрипт. Проверка, которая ничего не находит,
и проверка, которая сломана, выглядят снаружи одинаково — это та самая тема,
ради которой затевался весь заход. У `check_publish.py` цена ошибки
несимметрична: пропущенный секрет уезжает в публичный репозиторий
навсегда, а лишняя строка стоит минуты чтения.

Отдельно проверяется калибровка. Первая редакция скрипта считала блокером
любой лишний путь и выдала 286 строк FAIL, из которых 284 были про отчёты
прогонов: два настоящих пункта («нет LICENSE», «нет ARCHITECTURE.md»)
утонули в мусоре. Число без разбора важности — лампочка без надписи.

Запуск:  python3 tests_publish.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_publish as cp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestFindsRealSecrets(unittest.TestCase):

    def test_assigned_shared_secret(self):
        self.assertTrue(cp.scan_text("MCP_SHARED_SECRET=9f2c4b1ae77d0e5a"))

    def test_assigned_neo4j_password(self):
        self.assertTrue(cp.scan_text('NEO4J_PASSWORD: "SuperSecret123"'))

    def test_llm_and_cloud_keys(self):
        for line in (
            "OPENAI_API_KEY=sk-proj-AbCdEf0123456789AbCdEf",
            "token = ghp_0123456789abcdefghijABCDEFGH",
            "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            self.assertTrue(cp.scan_text(line), f"пропущено: {line}")


class TestDoesNotCryWolf(unittest.TestCase):
    """
    Ложные срабатывания дороже, чем кажется: на проверку, которая всегда
    красная, перестают смотреть — и она перестаёт работать в тот день,
    когда покраснела по делу.
    """

    def test_placeholders_from_readme_are_not_secrets(self):
        for line in (
            "MCP_SHARED_SECRET=<openssl rand -hex 32>",
            "NEO4J_PASSWORD=<случайная строка>",
            "MCP_SHARED_SECRET=",
            "NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}",
            "и заполнить MCP_SHARED_SECRET в .env",
        ):
            self.assertEqual(cp.scan_text(line), [], f"ложная тревога: {line}")

    def test_real_env_example_is_clean(self):
        """Самый вероятный путь утечки — заполненный и закоммиченный шаблон."""
        for name in (".env.example", "1c-mcp-suite/.env.example"):
            path = ROOT / name
            if path.exists():
                self.assertEqual(
                    cp.scan_text(path.read_text(encoding="utf-8")), [], name)


class TestSeverity(unittest.TestCase):

    def test_leak_is_a_blocker(self):
        for path in ("1c-mcp-suite/.env", "platform-help-data/shcntx_ru.hbk",
                     "workspace/Catalogs/Контрагенты.xml", ".cursor/mcp.json"):
            why, level = cp.forbidden(path)
            self.assertTrue(why, path)
            self.assertEqual(level, "FAIL", f"{path} должен блокировать")

    def test_clutter_is_only_a_warning(self):
        why, level = cp.forbidden("evals/reports/v8std_20260816_044916.json")
        self.assertTrue(why)
        self.assertEqual(level, "WARN",
                         "лишний .json нельзя объявлять утечкой — на крик, "
                         "который всегда ложный, перестают смотреть")

    def test_normal_files_pass(self):
        for path in ("README.md", ".env.example", "workspace/.gitkeep",
                     "platform-help-data/README.md", "evals/reports/.gitkeep",
                     "1c-mcp-suite/mcp-platform-help/server.py"):
            self.assertEqual(cp.forbidden(path), ("", ""), path)


class TestSummary(unittest.TestCase):

    def test_many_files_collapse_into_one_line(self):
        paths = [f"evals/reports/run_{i}.json" for i in range(143)]
        rows = cp.summarize(paths, "под версионным контролем")
        self.assertEqual(len(rows), 1, "сто сорок три сообщения вместо одного")
        level, text = rows[0]
        self.assertEqual(level, "WARN")
        self.assertIn("143 шт.", text)

    def test_different_kinds_are_not_mixed(self):
        rows = cp.summarize(["1c-mcp-suite/.env",
                             "evals/reports/a.json"], "в ИСТОРИИ")
        self.assertEqual(len(rows), 2)
        self.assertEqual({level for level, _ in rows}, {"FAIL", "WARN"})

    def test_nothing_found_means_no_rows(self):
        self.assertEqual(cp.summarize(["README.md"], "где-то"), [])


class TestRequiredFilesExist(unittest.TestCase):
    """
    Не про скрипт, а про репозиторий: без этих файлов публиковать нечего.
    Держится здесь, чтобы забытый LICENSE краснел в общем прогоне, а не
    только когда кто-то вспомнит запустить проверку руками.
    """

    def test_publication_files_are_in_place(self):
        for name, why in cp.REQUIRED_FILES:
            self.assertTrue((ROOT / name).exists(), f"нет {name} — {why}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
