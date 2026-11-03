"""
Тесты SRC-12/14 — PowerShell-скрипты режима git, запуском.
==========================================================

  graph-refresh.ps1      — ручная доиндексация: какая команда уходит в docker
                           при каждом ключе (-PrintOnly, без docker);
  restore-src-canon.ps1  — возврат src/ к HEAD через stash на временном
                           репозитории: правки не теряются, файлы вне src/
                           не трогаются;
  dump-and-commit.ps1    — коммит своей выгрузки (-SkipDump: Конфигуратора
                           в тесте нет): коммитится только src/, повторный
                           запуск без изменений коммита не делает.

Нужен PowerShell (`pwsh` или `powershell`) и git. Без них набор
пропускается целиком (CI-5: `ПУСТО`). Путь к pwsh можно задать
переменной PWSH.

Запуск:
    python scripts/tests_host_ps1.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

SCRIPTS = Path(__file__).resolve().parent
PWSH = os.environ.get("PWSH") or shutil.which("pwsh") or shutil.which("powershell")
GIT = shutil.which("git")

GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def ps(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, **GIT_ENV)
    env.pop("LOCALAPPDATA", None)            # не читать настоящий ubd-git-sync
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPTS / script), *args],
        capture_output=True, cwd=cwd, env=env, timeout=120)


def decode(raw: bytes) -> str:
    """
    Вывод PowerShell в UTF-8 (pwsh 7) или в OEM-кодировке консоли
    (Windows PowerShell 5.1 через конвейер: cp866 на русской Windows).
    Первая редакция читала только UTF-8 — и на Windows не находила в
    выводе русский текст, хотя скрипт печатал ровно то, что нужно.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if os.name == "nt":
        import ctypes
        try:
            return raw.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", "replace")
        except LookupError:
            pass
    return raw.decode("utf-8", "replace")


def out(r: subprocess.CompletedProcess) -> str:
    return decode(r.stdout) + decode(r.stderr)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(repo), *args],
                          check=True,
                          capture_output=True, env=dict(os.environ, **GIT_ENV)
                          ).stdout.decode("utf-8", "replace")


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@unittest.skipUnless(PWSH, "PowerShell не найден — набор не проверяет ничего")
class TestEncoding(unittest.TestCase):

    def test_scripts_with_cyrillic_have_bom(self):
        """
        Windows PowerShell 5.1 читает файл без BOM в кодировке ANSI:
        кириллица в выводе превратится в кракозябры. Конвенция проекта —
        UTF-8 с BOM (как setup-deps.ps1, gen_lockfiles.ps1).
        """
        for name in ("graph-refresh.ps1", "restore-src-canon.ps1",
                     "dump-and-commit.ps1"):
            raw = (SCRIPTS / name).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), name)
            self.assertNotIn(b"\r\n", raw, f"{name}: .gitattributes требует LF")


@unittest.skipUnless(PWSH, "PowerShell не найден — набор не проверяет ничего")
class TestGraphRefresh(unittest.TestCase):

    BASE = ("docker compose run --rm --entrypoint python metadata-indexer "
            "/app/apply_changes.py")

    def cmd(self, *args: str) -> str:
        r = ps("graph-refresh.ps1", "-PrintOnly", *args)
        self.assertEqual(r.returncode, 0, out(r))
        return out(r).strip()

    def test_plain(self):
        self.assertEqual(self.cmd(), self.BASE)

    def test_switches(self):
        self.assertEqual(self.cmd("-DryRun"), self.BASE + " --dry-run")
        self.assertEqual(self.cmd("-Reconcile"), self.BASE + " --reconcile")
        self.assertEqual(self.cmd("-ForceUnlock"), self.BASE + " --force-unlock")
        self.assertEqual(self.cmd("-Full"), self.BASE + " --full --yes")


@unittest.skipUnless(PWSH, "PowerShell не найден — набор не проверяет ничего")
class TestGraphRefreshLogFix40(unittest.TestCase):
    """
    FIX-40. Боевой случай 3 ноября: `graph-refresh.ps1 -Full *>&1 | Tee-Object`
    — первая строка docker в stderr при $ErrorActionPreference = "Stop"
    роняла скрипт, полная индексация шла без свидетелей, лог пропал.
    Подменяем docker скриптом, который пишет и в stdout, и в stderr (как
    compose и индексер), с кириллицей, и выходит с кодом 3.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        fake_py = d / "fake_docker.py"
        fake_py.write_text(
            "import sys\n"
            "sys.stderr.write('WARN переменная не задана\\n'); sys.stderr.flush()\n"
            "print('аргументы: ' + ' '.join(sys.argv[1:]), flush=True)\n"
            "for i in range(3):\n"
            "    sys.stderr.write(f'[INFO] резолв, итерация {i + 1}\\n'); sys.stderr.flush()\n"
            "print('ИТОГ: готово', flush=True)\n"
            "sys.exit(3)\n", encoding="utf-8")
        if os.name == "nt":
            fake = d / "fake_docker.cmd"
            fake.write_text(f'@"{sys.executable}" -X utf8 "{fake_py}" %*\r\n', encoding="ascii")
        else:
            fake = d / "fake_docker"
            fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" -X utf8 "{fake_py}" "$@"\n',
                            encoding="utf-8")
            fake.chmod(0o755)
        self.logs = d / "logs"
        self.env = {"GRAPH_REFRESH_DOCKER": str(fake),
                    "GRAPH_REFRESH_LOG_DIR": str(self.logs)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_stderr_does_not_stop_script_and_log_is_saved(self):
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            r = ps("graph-refresh.ps1", "-Full", "-Yes")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(r.returncode, 3, out(r))
        logs = list(self.logs.glob("graph-refresh-*.log"))
        self.assertEqual(len(logs), 1, out(r))
        text = logs[0].read_text(encoding="utf-8")
        for line in ("WARN переменная не задана", "резолв, итерация 3",
                     "ИТОГ: готово", "--full --yes"):
            self.assertIn(line, text)
        self.assertIn("Нужна полная индексация", out(r), "итог по коду выхода")
        self.assertIn("Лог:", out(r))


@unittest.skipUnless(PWSH and GIT, "нужны PowerShell и git")
class TestRestoreCanon(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "ubd for ai"       # пробел — нарочно
        write(self.repo, "AGENTS.md", "служебное\n")
        write(self.repo, "src/Configuration.xml", "<c/>\n")
        write(self.repo, "src/CommonModules/Общий/Ext/Module.bsl", "// канон\n")
        git(self.repo.parent, "init", "-q", str(self.repo))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "канон")

    def tearDown(self):
        self.tmp.cleanup()

    def test_restores_src_and_keeps_changes_in_stash(self):
        write(self.repo, "src/CommonModules/Общий/Ext/Module.bsl", "// правка агента\n")
        write(self.repo, "src/CommonModules/Новый/Ext/Module.bsl", "// новый от агента\n")
        write(self.repo, "AGENTS.md", "правка вне src\n")
        r = ps("restore-src-canon.ps1", "-Repo", str(self.repo), "-Yes", "-NoSync")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertEqual(git(self.repo, "status", "--porcelain", "--", "src").strip(), "")
        self.assertFalse((self.repo / "src/CommonModules/Новый").exists())
        self.assertIn("правка вне src",
                      (self.repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("restore-src-canon", git(self.repo, "stash", "list"))
        # правки достаются обратно
        git(self.repo, "stash", "pop", "-q")
        self.assertIn("правка агента", (self.repo / "src/CommonModules/Общий/Ext/Module.bsl")
                      .read_text(encoding="utf-8"))
        self.assertTrue((self.repo / "src/CommonModules/Новый/Ext/Module.bsl").exists())

    def test_clean_src(self):
        r = ps("restore-src-canon.ps1", "-Repo", str(self.repo), "-Yes", "-NoSync")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("возвращать нечего", out(r))
        self.assertEqual(git(self.repo, "stash", "list").strip(), "")

    def test_not_a_repo(self):
        r = ps("restore-src-canon.ps1", "-Repo", self.tmp.name, "-Yes", "-NoSync")
        self.assertEqual(r.returncode, 1)
        self.assertIn(".git", out(r))


@unittest.skipUnless(PWSH and GIT, "нужны PowerShell и git")
class TestDumpAndCommit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "my config"

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_and_commit_only_src(self):
        r = ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-Init", "-SkipDump")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertEqual(git(self.repo, "config", "core.autocrlf").strip(), "false")
        self.assertIn("изменений нет", out(r))

        write(self.repo, "src/Configuration.xml", "<c/>\n")
        write(self.repo, "src/Catalogs/Товары.xml", "<cat/>\n")
        write(self.repo, "AGENTS.md", "не часть выгрузки\n")
        r = ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-SkipDump",
               "-Message", "первая выгрузка")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("Закоммичено: 2", out(r))
        files = git(self.repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), ["src/Catalogs/Товары.xml", "src/Configuration.xml"])
        self.assertIn("AGENTS.md", git(self.repo, "status", "--porcelain"))

        r = ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-SkipDump")
        self.assertEqual(r.returncode, 0, out(r))
        self.assertIn("изменений нет", out(r))

    def test_without_git(self):
        self.repo.mkdir()
        r = ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-SkipDump")
        self.assertEqual(r.returncode, 1)
        self.assertIn("-Init", out(r))

    def test_dump_needs_platform(self):
        ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-Init", "-SkipDump")
        env_saved = os.environ.pop("ONEC_PLATFORM_EXE", None)
        try:
            r = ps("dump-and-commit.ps1", "-Repo", str(self.repo), "-File", "x")
        finally:
            if env_saved is not None:
                os.environ["ONEC_PLATFORM_EXE"] = env_saved
        self.assertEqual(r.returncode, 1)
        self.assertIn("1cv8.exe", out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
