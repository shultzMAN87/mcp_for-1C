"""
ENV-1. Разбор `.env` на хосте — по правилам docker compose.

Боевой случай 4 ноября: в `.env` по образцу README стояло

    WORKSPACE_DIR=D:\\projects\\Cursor\\ubd_for_ai   # корень клона, где .git (НЕ src)

compose комментарий отрезал и стек работал, а `check_prereqs.py` взял путь
вместе с комментарием и сказал «каталог не найден». Здесь — правила разбора
и сторож: ни один скрипт не разбирает `.env` своим способом.

Запуск: python3 scripts/tests_envfile.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from envfile import clean_env_value, parse_env_file, parse_env_text  # noqa: E402


class TestCleanValue(unittest.TestCase):

    def test_inline_comment_is_cut(self):
        self.assertEqual(
            clean_env_value(r" D:\projects\Cursor\ubd_for_ai   # корень клона, где .git (НЕ src)"),
            r"D:\projects\Cursor\ubd_for_ai")
        self.assertEqual(clean_env_value("src\t# или пусто"), "src")

    def test_hash_inside_value_stays(self):
        self.assertEqual(clean_env_value("pa#ss"), "pa#ss")
        self.assertEqual(clean_env_value("abc#def # комментарий"), "abc#def")

    def test_quotes(self):
        self.assertEqual(clean_env_value('"with # hash"  # комментарий'), "with # hash")
        self.assertEqual(clean_env_value("'single'"), "single")
        self.assertEqual(clean_env_value('""'), "")

    def test_empty(self):
        self.assertEqual(clean_env_value(""), "")
        self.assertEqual(clean_env_value("   # только комментарий"), "")


class TestParse(unittest.TestCase):

    def test_text(self):
        env = parse_env_text(
            "# заголовок\n"
            "\n"
            "WORKSPACE_DIR=D:/work/ubd   # корень\n"
            "CONFIG_SUBDIR=src\n"
            "CONFIG_EMPTY=\n"
            "export TZ=Asia/Vladivostok\n"
            "BROKEN LINE\n")
        self.assertEqual(env, {"WORKSPACE_DIR": "D:/work/ubd", "CONFIG_SUBDIR": "src",
                               "CONFIG_EMPTY": "", "TZ": "Asia/Vladivostok"})

    def test_file_with_bom(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_bytes(b"\xef\xbb\xbfKEY=value # c\n")
            self.assertEqual(parse_env_file(p), {"KEY": "value"})
            self.assertEqual(parse_env_file(Path(d) / "нет"), {})

    def test_check_prereqs_uses_it(self):
        import check_prereqs
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("WORKSPACE_DIR=D:/x   # комментарий\n", encoding="utf-8")
            self.assertEqual(check_prereqs.parse_env_file(p)["WORKSPACE_DIR"], "D:/x")


class TestPrereqsSeeWorkspace(unittest.TestCase):
    """Тот же подъём 4 ноября: с комментарием в WORKSPACE_DIR проверка
    каталога падала, а .cursor/mcp.json искался только в репозитории стека."""

    def test_workspace_and_cursor_config(self):
        import os
        import check_prereqs
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ubd_for_ai"
            (ws / "src").mkdir(parents=True)
            (ws / ".cursor").mkdir()
            (ws / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
            envf = Path(d) / ".env"
            envf.write_text(f"WORKSPACE_DIR={ws}   # корень клона, где .git (НЕ src)\n", encoding="utf-8")
            env = check_prereqs.parse_env_file(envf)
            saved = os.environ.pop("WORKSPACE_DIR", None)
            try:
                self.assertEqual(env["WORKSPACE_DIR"], str(ws))
                r = check_prereqs.check_cursor_config(env)
                self.assertIn(str(ws), r.message)
                # Раскладка PLAN-11: конфигурация в src, а не в корне клона.
                (ws / ".git").mkdir()
                (ws / "src" / "Configuration.xml").write_text("<c/>", encoding="utf-8")
                saved_sub = os.environ.pop("CONFIG_SUBDIR", None)
                try:
                    env2 = dict(env, CONFIG_SUBDIR="src")
                    r = check_prereqs.check_workspace(env2)
                    self.assertEqual(r.status, "ok", r.message)
                    self.assertIn("git", r.message)
                    r = check_prereqs.check_workspace(env)       # CONFIG_SUBDIR не задан
                    self.assertEqual(r.status, "fail")
                    self.assertIn("CONFIG_SUBDIR=src", r.hint)
                    r = check_prereqs.check_workspace(dict(env, CONFIG_SUBDIR="нет"))
                    self.assertEqual(r.status, "fail")
                finally:
                    if saved_sub is not None:
                        os.environ["CONFIG_SUBDIR"] = saved_sub
            finally:
                if saved is not None:
                    os.environ["WORKSPACE_DIR"] = saved


class TestNoPrivateEnvParsers(unittest.TestCase):
    """Скрипт, который читает `.env` и сам режет строки по `=`, обязан
    брать значение через envfile — иначе комментарий в конце строки снова
    уедет в путь или в секрет."""

    SPLIT = re.compile(r"""(split|partition)\(\s*["']=["']""")

    def test_every_reader_goes_through_envfile(self):
        offenders = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name in ("envfile.py", Path(__file__).name):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if ".env" in text and self.SPLIT.search(text) and "envfile" not in text:
                offenders.append(f.name)
        self.assertEqual(offenders, [], "разбирают .env сами — перевести на envfile.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
