"""
SRC-5. Всё, что граф спрашивает у git.
======================================

Граф узнаёт об изменениях выгрузки у git, а не обходом 56 тысяч файлов
через границу Docker Desktop (~5 мс на `stat`, 3–12 минут на обход).
Здесь — тонкая обёртка над `git` и чистые функции поверх неё. Neo4j этот
модуль не знает: тестируется на временных репозиториях (tests_git_source.py).

Только чтение
─────────────
Выгрузка смонтирована в контейнер `:ro`, а `.git` принадлежит человеку и
его `ubd-git-sync`. Поэтому:

  • разрешены только подкоманды из `ALLOWED_SUBCOMMANDS` — список закрытый,
    сторож в тестах проверяет, что другие в коде не появились;
  • `GIT_OPTIONAL_LOCKS=0` — `git status` не пытается обновить индекс и
    не создаёт `index.lock`;
  • `safe.directory=*` — владелец каталога, смонтированного с Windows, не
    совпадает с пользователем контейнера, и без этого git откажется
    работать вовсе («dubious ownership»).

Хеш файла на диске считается здесь же, в Python, по формуле git
(`sha1("blob <длина>\\0" + содержимое)`), а не `git hash-object`: одна
подкоманда меньше, процесс на каждый файл не нужен.

Переводы строк
──────────────
Если репозиторий когда-то коммитили с `core.autocrlf=true` (глобальная
настройка Windows, контейнер её не видит), в git лежат LF, а на диске CRLF.
Сравнение «файл совпадает с каноном» тогда не сработало бы никогда, и путь
навсегда застрял бы в списке изменённых. Поэтому у файла на диске три
отпечатка — как есть, с CRLF→LF и с LF→CRLF, — и совпадение засчитывается
по любому. Совпадение не по сырому варианту отмечается в
`GitRepo.normalized_matches`: это признак нормализованного репозитория.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

# Закрытый список. Пишущих подкоманд (add, commit, fetch, pull, stash,
# checkout, reset, gc, hash-object -w, update-index …) здесь нет и быть не
# должно: контейнер читает, пишет человек на хосте.
ALLOWED_SUBCOMMANDS = frozenset({
    "rev-parse", "cat-file", "diff", "status", "version", "ls-files",
})

# Таймаут одного вызова git. `status` на боевой выгрузке может идти минуты
# (SRC-11), остальное — доли секунды.
DEFAULT_TIMEOUT_SEC = 120.0
STATUS_TIMEOUT_SEC = float(os.environ.get("SOURCE_STATUS_TIMEOUT_SEC", "1800"))


class GitError(RuntimeError):
    """git не смог ответить: нет бинарника, не репозиторий, таймаут, код ≠ 0."""


# ─── Отпечаток файла на диске ─────────────────────────────────────────────


@dataclass(frozen=True)
class DiskHash:
    """
    Отпечатки файла на диске. `raw` — git-хеш содержимого как есть;
    `variants` — он же плюс варианты с другой нормализацией переводов строк.
    """
    raw: str
    variants: frozenset

    def matches(self, blob: Optional[str]) -> bool:
        return blob is not None and blob in self.variants


def _blob_hash(data: bytes, algo: str) -> str:
    h = hashlib.new(algo)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def disk_hash(path: Path, algo: str = "sha1") -> Optional[DiskHash]:
    """Отпечаток файла или None, если файла нет (или это не файл)."""
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    raw = _blob_hash(data, algo)
    variants = {raw}
    if b"\r\n" in data:
        variants.add(_blob_hash(data.replace(b"\r\n", b"\n"), algo))
    if b"\n" in data:
        # LF→CRLF: сначала свести к LF, чтобы не получить \r\r\n.
        lf = data.replace(b"\r\n", b"\n")
        variants.add(_blob_hash(lf.replace(b"\n", b"\r\n"), algo))
    return DiskHash(raw=raw, variants=frozenset(variants))


def same_content(disk: Optional[DiskHash], blob: Optional[str]) -> bool:
    """
    Совпадает ли файл на диске с версией в git. Оба отсутствуют — тоже
    «совпадает»: файла нет ни там, ни там.
    """
    if disk is None:
        return blob is None
    return disk.matches(blob)


# ─── Обёртка над git ──────────────────────────────────────────────────────


@dataclass
class GitRepo:
    """
    Репозиторий выгрузки. `root` — корень репозитория (где `.git`),
    `subdir` — каталог конфигурации внутри него (`src` или пусто).

    Все пути наружу — ОТ КОРНЯ КОНФИГУРАЦИИ (`Catalogs/X.xml`), как их
    понимают `owner_of` и `incremental`. Пути от корня репозитория живут
    только внутри этого класса.
    """
    root: Path
    subdir: str = ""
    timeout: float = DEFAULT_TIMEOUT_SEC
    normalized_matches: int = field(default=0, init=False)
    _algo: Optional[str] = field(default=None, init=False, repr=False)

    # ── низкий уровень ──

    def _run(self, args: list[str], *, input_bytes: Optional[bytes] = None,
             timeout: Optional[float] = None,
             extra_config: Iterable[str] = (),
             ok_codes: Iterable[int] = (0,)) -> subprocess.CompletedProcess:
        if not args or args[0] not in ALLOWED_SUBCOMMANDS:
            raise GitError(f"подкоманда git не разрешена: {args[:1]}")
        if shutil.which("git") is None:
            raise GitError("git не установлен в образе (SRC-3)")
        cmd = ["git", "-c", "safe.directory=*", "-c", "core.quotepath=off"]
        for kv in extra_config:
            cmd += ["-c", kv]
        cmd += ["-C", str(self.root)] + list(args)
        env = dict(os.environ)
        env.update({
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            # Глобальные и системные настройки хоста в контейнер не попадают
            # и так; здесь это делается явным, чтобы тесты на машине
            # разработчика не зависели от его ~/.gitconfig.
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        try:
            proc = subprocess.run(
                cmd, input=input_bytes, capture_output=True, env=env,
                timeout=timeout or self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {args[0]}: таймаут {e.timeout:.0f} с") from e
        except OSError as e:
            raise GitError(f"git {args[0]}: {e}") from e
        if proc.returncode not in tuple(ok_codes):
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} → код {proc.returncode}: {err[:300]}")
        return proc

    def _repo_path(self, rel: str) -> str:
        return f"{self.subdir}/{rel}" if self.subdir else rel

    def _config_rel(self, repo_rel: str) -> Optional[str]:
        if not self.subdir:
            return repo_rel
        prefix = self.subdir + "/"
        return repo_rel[len(prefix):] if repo_rel.startswith(prefix) else None

    def _pathspec(self) -> list[str]:
        # `:(top)` — путь от корня репозитория, независимо от того, откуда
        # запущен git; без хвоста — весь репозиторий. Форма `:(top).` НЕ
        # годится: она не совпадает ни с чем (поймано тестом
        # TestRootLayout — конфигурация в корне репозитория).
        return ["--", f":(top){self.subdir}" if self.subdir else ":(top)"]

    # ── сведения о репозитории ──

    def toplevel(self) -> Path:
        out = self._run(["rev-parse", "--show-toplevel"]).stdout
        return Path(out.decode("utf-8", "surrogateescape").strip())

    def object_format(self) -> str:
        """sha1 у обычных репозиториев, sha256 — у созданных с этим форматом."""
        if self._algo is None:
            try:
                out = self._run(["rev-parse", "--show-object-format"]).stdout
                fmt = out.decode().strip() or "sha1"
            except GitError:
                fmt = "sha1"   # git старше 2.29 этого ключа не знает
            self._algo = fmt if fmt in ("sha1", "sha256") else "sha1"
        return self._algo

    def head(self) -> Optional[str]:
        """Хеш коммита HEAD или None, если коммитов ещё нет."""
        proc = self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                         ok_codes=(0, 1))
        sha = proc.stdout.decode().strip()
        return sha or None

    def commit_exists(self, sha: str) -> bool:
        if not sha:
            return False
        proc = self._run(["cat-file", "-e", f"{sha}^{{commit}}"], ok_codes=(0, 1, 128))
        return proc.returncode == 0

    # ── что изменилось ──

    def changed_between(self, a: str, b: str) -> list[tuple[str, str]]:
        """
        `[(статус, путь_от_корня_конфигурации)]` между двумя коммитами.

        `--no-renames`: переименование приходит парой «удалён + добавлен»,
        и отдельной логики для него не нужно — граф всё равно удаляет
        старый объект и строит новый.
        """
        if a == b:
            return []
        proc = self._run(["diff", "--no-renames", "--name-status", "-z", a, b]
                         + self._pathspec())
        parts = proc.stdout.split(b"\0")
        out: list[tuple[str, str]] = []
        i = 0
        while i + 1 < len(parts):
            status = parts[i].decode("ascii", "replace")
            path = parts[i + 1].decode("utf-8", "surrogateescape")
            i += 2
            if not status:
                continue
            rel = self._config_rel(path)
            if rel is not None:
                out.append((status[0], rel))
        return out

    def blobs_at(self, rev: str, rels: Iterable[str]) -> dict[str, Optional[str]]:
        """
        Хеш версии каждого файла в коммите `rev`; None — файла там нет.
        Один процесс на весь список (`cat-file --batch-check`).
        """
        rels = list(dict.fromkeys(rels))
        if not rels:
            return {}
        lines = "".join(f"{rev}:{self._repo_path(r)}\n" for r in rels)
        proc = self._run(["cat-file", "--batch-check"],
                         input_bytes=lines.encode("utf-8", "surrogateescape"))
        answers = proc.stdout.decode("utf-8", "surrogateescape").splitlines()
        out: dict[str, Optional[str]] = {}
        for rel, line in zip(rels, answers):
            fields = line.split()
            out[rel] = fields[0] if len(fields) >= 2 and fields[1] == "blob" else None
        for rel in rels[len(answers):]:   # pragma: no cover — git ответил не на всё
            out[rel] = None
        return out

    def hash_on_disk(self, rels: Iterable[str]) -> dict[str, Optional[DiskHash]]:
        algo = self.object_format()
        base = self.root / self.subdir if self.subdir else self.root
        return {r: disk_hash(base / r, algo) for r in dict.fromkeys(rels)}

    def ls_files(self, suffix: str = "") -> list[str]:
        """
        PERF-14. Файлы конфигурации, известные git (индекс), — пути от корня
        конфигурации. Читается один файл `.git/index`: секунды вместо обхода
        выгрузки через границу Docker. Файлы, которых git не знает
        (неотслеживаемые), сюда не попадают — их добавляет вызывающий.
        """
        proc = self._run(["ls-files", "-z"] + self._pathspec())
        low = suffix.lower()
        out: list[str] = []
        for raw in proc.stdout.split(b"\0"):
            if not raw:
                continue
            path = raw.decode("utf-8", "surrogateescape")
            if low and not path.lower().endswith(low):
                continue
            rel = self._config_rel(path)
            if rel is not None:
                out.append(rel)
        return out

    def status(self) -> list[tuple[str, str]]:
        """
        Изменённые и неотслеживаемые файлы рабочей копии в каталоге
        конфигурации: `[(код XY, путь_от_корня_конфигурации)]`.

        Единственная команда, которая обходит рабочую копию, — поэтому
        только для сверки (SRC-11). Настройки — против перехеширования всей
        выгрузки: индекс писал git на Windows, inode и владельца контейнер
        видит другими, и без `checkStat=minimal` каждая запись индекса
        показалась бы «возможно изменённой».
        """
        proc = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--no-renames"] + self._pathspec(),
            timeout=STATUS_TIMEOUT_SEC,
            extra_config=("core.checkStat=minimal", "core.trustctime=false",
                          "core.fsmonitor=false", "core.untrackedCache=false"),
        )
        out: list[tuple[str, str]] = []
        for entry in proc.stdout.split(b"\0"):
            if len(entry) < 4:
                continue
            code = entry[:2].decode("ascii", "replace")
            path = entry[3:].decode("utf-8", "surrogateescape")
            if path.endswith("/"):
                continue
            rel = self._config_rel(path)
            if rel is not None:
                out.append((code, rel))
        return out

    def note_match(self, disk: Optional[DiskHash], blob: Optional[str]) -> bool:
        """`same_content` + учёт совпадений по нормализованному варианту."""
        ok = same_content(disk, blob)
        if ok and disk is not None and blob is not None and disk.raw != blob:
            self.normalized_matches += 1
        return ok


# ─── Определение режима ───────────────────────────────────────────────────


@dataclass
class Detection:
    mode: str                     # 'git' | 'legacy'
    reason: str
    repo: Optional[GitRepo] = None
    subdir: str = ""


def config_subdir(repo_root: Path, src_root: Path) -> Optional[str]:
    """
    Подкаталог конфигурации внутри репозитория: `src`, `` (корень) или
    None, если конфигурация вне репозитория.
    """
    try:
        rel = PurePosixPath(
            os.path.relpath(os.path.normpath(str(src_root)),
                            os.path.normpath(str(repo_root))).replace("\\", "/"))
    except ValueError:            # разные диски на Windows
        return None
    s = str(rel)
    if s == ".":
        return ""
    if s.startswith(".."):
        return None
    return s


def detect(repo_root: Path, src_root: Path, mode: str = "auto") -> Detection:
    """
    git или legacy. `mode`: auto (по наличию репозитория) | git | legacy.

    В режиме `git` (принудительно) отсутствие репозитория — не тихий откат
    к legacy, а ошибка: человек явно попросил git, и молча работать иначе
    значило бы обмануть его ожидания.
    """
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "git", "legacy"):
        raise GitError(f"SOURCE_MODE={mode!r}: допустимо auto | git | legacy")
    if mode == "legacy":
        return Detection("legacy", "SOURCE_MODE=legacy")

    def fail(reason: str) -> Detection:
        if mode == "git":
            raise GitError(reason + " (а SOURCE_MODE=git требует репозиторий)")
        return Detection("legacy", reason)

    subdir = config_subdir(repo_root, src_root)
    if subdir is None:
        return fail(f"каталог конфигурации {src_root} лежит вне {repo_root}")
    if not (repo_root / ".git").exists():
        return fail(f"в {repo_root} нет .git")
    repo = GitRepo(repo_root, subdir)
    try:
        top = repo.toplevel()
    except GitError as e:
        return fail(f"git не отвечает: {e}")
    if os.path.normcase(os.path.realpath(top)) != \
            os.path.normcase(os.path.realpath(repo_root)):
        return fail(f"корень репозитория {top}, а смонтирован {repo_root}")
    try:
        head = repo.head()
    except GitError as e:
        return fail(f"git не отвечает: {e}")
    if head is None:
        return fail("в репозитории нет ни одного коммита")
    where = f"подкаталог {subdir}" if subdir else "корень репозитория"
    return Detection("git", f"репозиторий {repo_root}, конфигурация — {where}",
                     repo=repo, subdir=subdir)
