"""
Тесты SRC-5 — git_source.py на настоящих временных репозиториях.
================================================================

Что проверяется: определение режима (git / legacy), чтение истории
(HEAD, изменения между коммитами, версии файлов), отпечаток файла на диске
против версии в git — включая переводы строк, — и сверка рабочей копии.

Отдельно — то, что модуль НИЧЕГО не пишет в `.git`: снимок каталога до и
после всех операций обязан совпасть. В контейнере выгрузка смонтирована
только на чтение, и пишущая команда там упала бы, а на машине разработчика
— тихо испортила бы чужой репозиторий.

Нужен git на машине прогона. Если его нет, набор пропускается ЦЕЛИКОМ, и
`run_all_tests.py` пометит его `ПУСТО` (CI-5): непроверенный движок не
должен выглядеть проверенным.

Запуск:
    python tests_git_source.py -v
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_source import (ALLOWED_SUBCOMMANDS, GitError, GitRepo, config_subdir,
                        detect, disk_hash, same_content)

HAVE_GIT = shutil.which("git") is not None
HERE = Path(__file__).resolve().parent


def sh(cwd: Path, *args: str, env: dict | None = None) -> str:
    """git для ПОДГОТОВКИ фикстур — пишет, и это нормально: это тест."""
    e = dict(os.environ)
    e.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    if env:
        e.update(env)
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, env=e).stdout.decode()


def write(root: Path, rel: str, text: str, newline: str = "\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return p


def snapshot(d: Path) -> dict:
    out = {}
    for p in sorted(d.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(d))] = (p.is_dir(), st.st_size,
                                      0 if p.is_dir() else st.st_mtime_ns)
    return out


@unittest.skipUnless(HAVE_GIT, "git не установлен — набор не проверяет ничего")
class RepoCase(unittest.TestCase):
    """Репозиторий с конфигурацией в `src/`, как у клона ubd_for_ai."""

    subdir = "src"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "core.autocrlf", "false")
        self.cfg = self.root / self.subdir if self.subdir else self.root
        write(self.root, "AGENTS.md", "служебное\n")
        write(self.cfg, "Configuration.xml", "<c/>\n")
        write(self.cfg, "Catalogs/Товары.xml", "<cat/>\n")
        write(self.cfg, "Catalogs/Товары/Ext/ObjectModule.bsl", "Процедура А()\nКонецПроцедуры\n")
        write(self.cfg, "CommonModules/Общий Модуль/Ext/Module.bsl", "// м\n")
        self.c1 = self.commit("первый")
        self.repo = GitRepo(self.root, self.subdir)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg: str) -> str:
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", msg)
        return sh(self.root, "rev-parse", "HEAD").strip()


class TestDetect(RepoCase):

    def test_git_with_subdir(self):
        d = detect(self.root, self.root / "src")
        self.assertEqual(d.mode, "git", d.reason)
        self.assertEqual(d.subdir, "src")
        self.assertEqual(d.repo.head(), self.c1)

    def test_forced_legacy(self):
        self.assertEqual(detect(self.root, self.cfg, "legacy").mode, "legacy")

    def test_no_repo_is_legacy_in_auto_and_error_in_git(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        d = detect(plain, plain)
        self.assertEqual(d.mode, "legacy")
        self.assertIn(".git", d.reason)
        with self.assertRaises(GitError):
            detect(plain, plain, "git")

    def test_empty_repo_is_legacy(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        sh(empty, "init", "-q")
        d = detect(empty, empty)
        self.assertEqual(d.mode, "legacy")
        self.assertIn("ни одного коммита", d.reason)

    def test_config_outside_repo(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.assertEqual(detect(self.root, other).mode, "legacy")

    def test_mounted_subfolder_is_not_the_repo_root(self):
        """
        Смонтировали `src/` вместо корня: `.git` в нём нет — legacy, а не
        работа против чужого корня.
        """
        self.assertEqual(detect(self.cfg, self.cfg).mode, "legacy")

    def test_bad_mode(self):
        with self.assertRaises(GitError):
            detect(self.root, self.cfg, "auto-git")

    def test_config_subdir(self):
        self.assertEqual(config_subdir(Path("/a/r"), Path("/a/r/src")), "src")
        self.assertEqual(config_subdir(Path("/a/r"), Path("/a/r/")), "")
        self.assertEqual(config_subdir(Path("/a/r"), Path("/a/r/src/")), "src")
        self.assertIsNone(config_subdir(Path("/a/r"), Path("/a/x")))


class TestHistory(RepoCase):

    def test_changed_between(self):
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='2'/>\n")
        (self.cfg / "CommonModules/Общий Модуль/Ext/Module.bsl").unlink()
        write(self.cfg, "Documents/Заказ.xml", "<doc/>\n")
        write(self.root, "AGENTS.md", "вне конфигурации\n")
        write(self.cfg, "ConfigDumpInfo.xml", "<d/>\n")
        c2 = self.commit("второй")
        got = sorted(self.repo.changed_between(self.c1, c2))
        self.assertEqual(got, sorted([
            ("M", "Catalogs/Товары.xml"),
            ("D", "CommonModules/Общий Модуль/Ext/Module.bsl"),
            ("A", "Documents/Заказ.xml"),
            ("A", "ConfigDumpInfo.xml"),
        ]))
        self.assertEqual(self.repo.changed_between(c2, c2), [])

    def test_rename_is_delete_plus_add(self):
        os.rename(self.cfg / "Catalogs/Товары.xml", self.cfg / "Catalogs/Товары2.xml")
        c2 = self.commit("переименование")
        got = sorted(self.repo.changed_between(self.c1, c2))
        self.assertEqual(got, [("A", "Catalogs/Товары2.xml"),
                               ("D", "Catalogs/Товары.xml")])

    def test_commit_exists(self):
        self.assertTrue(self.repo.commit_exists(self.c1))
        self.assertFalse(self.repo.commit_exists("0" * 40))
        self.assertFalse(self.repo.commit_exists("не-хеш"))
        self.assertFalse(self.repo.commit_exists(""))

    def test_blobs_at(self):
        blobs = self.repo.blobs_at(self.c1, ["Catalogs/Товары.xml",
                                             "Нет/Такого.xml",
                                             "CommonModules/Общий Модуль/Ext/Module.bsl"])
        self.assertIsNotNone(blobs["Catalogs/Товары.xml"])
        self.assertIsNone(blobs["Нет/Такого.xml"])
        self.assertIsNotNone(blobs["CommonModules/Общий Модуль/Ext/Module.bsl"])
        self.assertEqual(self.repo.blobs_at(self.c1, []), {})

    def test_disk_hash_equals_git_blob(self):
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        blob = self.repo.blobs_at("HEAD", [rel])[rel]
        disk = self.repo.hash_on_disk([rel])[rel]
        self.assertEqual(disk.raw, blob)
        self.assertTrue(same_content(disk, blob))

    def test_missing_file_on_disk(self):
        self.assertIsNone(self.repo.hash_on_disk(["Нет.xml"])["Нет.xml"])
        self.assertTrue(same_content(None, None))
        self.assertFalse(same_content(None, "abc"))


class TestLsFiles(RepoCase):
    """PERF-14: список файлов из индекса git вместо обхода выгрузки."""

    def test_ls_files(self):
        write(self.cfg, "CommonModules/Новый/Ext/Module.bsl", "// не в git\n")
        write(self.root, "openspec/x.bsl", "// вне конфигурации\n")
        got = self.repo.ls_files(".bsl")
        self.assertEqual(sorted(got), ["Catalogs/Товары/Ext/ObjectModule.bsl",
                                       "CommonModules/Общий Модуль/Ext/Module.bsl"])
        self.assertIn("Configuration.xml", self.repo.ls_files())


class TestLineEndings(RepoCase):
    """
    Репозиторий нормализован (в git LF), на диске CRLF — как бывает после
    коммитов с `core.autocrlf=true` на Windows. Файл обязан опознаться как
    совпадающий с каноном, иначе застрянет в списке изменённых навсегда.
    """

    def test_crlf_on_disk_lf_in_git(self):
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        write(self.cfg, rel, "Процедура А()\nКонецПроцедуры\n", newline="\r\n")
        blob = self.repo.blobs_at("HEAD", [rel])[rel]
        disk = self.repo.hash_on_disk([rel])[rel]
        self.assertNotEqual(disk.raw, blob)
        self.assertTrue(self.repo.note_match(disk, blob))
        self.assertEqual(self.repo.normalized_matches, 1)

    def test_lf_on_disk_crlf_in_git(self):
        rel = "Catalogs/Товары.xml"
        write(self.cfg, rel, "<cat/>\n", newline="\r\n")
        c2 = self.commit("crlf в репозитории")
        write(self.cfg, rel, "<cat/>\n")
        blob = self.repo.blobs_at(c2, [rel])[rel]
        self.assertTrue(same_content(self.repo.hash_on_disk([rel])[rel], blob))

    def test_real_change_is_still_a_change(self):
        rel = "Catalogs/Товары.xml"
        write(self.cfg, rel, "<cat v='другое'/>\n", newline="\r\n")
        blob = self.repo.blobs_at("HEAD", [rel])[rel]
        self.assertFalse(same_content(self.repo.hash_on_disk([rel])[rel], blob))

    def test_no_double_cr(self):
        """LF→CRLF не должен превращать уже CRLF в \\r\\r\\n."""
        p = write(Path(self.tmp.name), "x.txt", "a\nb\n", newline="\r\n")
        h = disk_hash(p)
        self.assertEqual(len(h.variants), 2)   # как есть (= CRLF) и LF


class TestStatus(RepoCase):

    def test_status_sees_modified_untracked_deleted(self):
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='3'/>\n")
        write(self.cfg, "CommonModules/Новый/Ext/Module.bsl", "// новый\n")
        (self.cfg / "Catalogs/Товары/Ext/ObjectModule.bsl").unlink()
        write(self.root, "openspec/changes/x.md", "вне конфигурации\n")
        got = {rel: code for code, rel in self.repo.status()}
        self.assertEqual(set(got), {"Catalogs/Товары.xml",
                                    "CommonModules/Новый/Ext/Module.bsl",
                                    "Catalogs/Товары/Ext/ObjectModule.bsl"})
        self.assertEqual(got["CommonModules/Новый/Ext/Module.bsl"], "??")

    def test_clean(self):
        self.assertEqual(self.repo.status(), [])


class TestReadOnly(RepoCase):
    """`.git` после всех операций модуля — байт в байт тот же."""

    def test_nothing_written_to_git_dir(self):
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='4'/>\n")
        write(self.cfg, "Documents/Новый.xml", "<doc/>\n")
        before = snapshot(self.root / ".git")
        d = detect(self.root, self.cfg)
        r = d.repo
        r.head()
        r.commit_exists(self.c1)
        r.changed_between(self.c1, r.head())
        r.blobs_at("HEAD", ["Catalogs/Товары.xml"])
        r.hash_on_disk(["Catalogs/Товары.xml"])
        r.status()
        r.status()
        r.ls_files(".bsl")
        self.assertEqual(snapshot(self.root / ".git"), before)

    def test_writing_subcommands_are_refused(self):
        for sub in ("add", "commit", "fetch", "pull", "stash", "checkout",
                    "reset", "gc", "hash-object", "update-index"):
            with self.subTest(sub=sub), self.assertRaises(GitError):
                self.repo._run([sub])

    def test_whitelist_is_closed_in_source(self):
        """
        Сторож: ни один модуль набора не вызывает git-подкоманду вне
        белого списка. Проверяется по исходникам — через `_run([...])` и
        через прямой `subprocess` с "git".
        """
        offenders = []
        for py in sorted(HERE.glob("*.py")):
            if py.name.startswith("tests_"):
                continue
            text = py.read_text(encoding="utf-8")
            for m in re.finditer(r'_run\(\s*\[\s*"([a-z-]+)"', text):
                if m.group(1) not in ALLOWED_SUBCOMMANDS:
                    offenders.append(f"{py.name}: {m.group(1)}")
            if py.name != "git_source.py" and re.search(
                    r'subprocess\.\w+\(\s*\[\s*"git"', text):
                offenders.append(f"{py.name}: прямой subprocess git")
        self.assertFalse(offenders, offenders)


class TestRootLayout(RepoCase):
    """Конфигурация прямо в корне репозитория (своя выгрузка + git init)."""

    subdir = ""

    def test_paths_are_config_relative(self):
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='5'/>\n")
        write(self.root, "AGENTS.md", "изменён\n")
        c2 = self.commit("в корне")
        got = sorted(self.repo.changed_between(self.c1, c2))
        self.assertEqual(got, [("M", "AGENTS.md"), ("M", "Catalogs/Товары.xml")])
        d = detect(self.root, self.root)
        self.assertEqual((d.mode, d.subdir), ("git", ""))

    def test_status_in_root_layout(self):
        write(self.cfg, "Catalogs/Товары.xml", "<cat v='6'/>\n")
        self.assertEqual(self.repo.status(), [(" M", "Catalogs/Товары.xml")])


@unittest.skipUnless(HAVE_GIT, "git не установлен")
class TestSha256Repo(unittest.TestCase):

    def test_disk_hash_in_sha256_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            try:
                sh(root, "init", "-q", "--object-format=sha256")
            except subprocess.CalledProcessError:
                self.skipTest("git без поддержки sha256")
            write(root, "Catalogs/К.xml", "<k/>\n")
            sh(root, "add", "-A")
            sh(root, "commit", "-q", "-m", "x")
            repo = GitRepo(root, "")
            self.assertEqual(repo.object_format(), "sha256")
            blob = repo.blobs_at("HEAD", ["Catalogs/К.xml"])["Catalogs/К.xml"]
            self.assertEqual(repo.hash_on_disk(["Catalogs/К.xml"])["Catalogs/К.xml"].raw,
                             blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
