"""
DOC-2. Что мешает выложить репозиторий наружу.
===============================================

Зачем скрипт, а не однократный просмотр
───────────────────────────────────────
«Проверить историю на секреты» — задача, которую делают один раз и потом
считают сделанной. Но история растёт: чистая сегодня, она будет другой
через десять коммитов, а вывод «проверено» останется тем же. Это ровно
тот жанр, который проект чинит пятый заход подряд — состояние определяется
однажды и выдаётся за факт.

Поэтому проверка оформлена так, чтобы её можно было прогнать перед каждой
публикацией и в любой момент после. Запуск:

    python scripts/check_publish.py

Код возврата ненулевой, если есть хоть один FAIL, — тогда скрипт годится и
для хука, и для CI.

Что проверяется
───────────────
1. Секреты и приватные данные в отслеживаемых файлах.
2. То же самое во ВСЕЙ истории: удалённый из рабочего каталога файл
   остаётся в объектах гита навсегда, и `git rm` его не убирает.
3. Проприетарные и чужие данные: `.hbk` (справка 1С), выгрузка
   конфигурации, корпус `v8std-data`, файлы лицензий платформы.
4. Обязательные для публикации файлы и разделы.

Чего проверка НЕ делает
───────────────────────
Не решает за вас, можно ли публиковать. Она отвечает на вопрос «не уедет
ли наружу то, чему там не место», и ничего не знает про то, хотите ли вы
публиковать вообще.

Не является юридической проверкой. Раздел про лицензии зависимостей — в
`ARCHITECTURE.md`, и он тоже не заменяет юриста.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# FAIL-2. Защита потоков до первой печати: консоль cp1251 не умеет ✓ и ⚠,
# а к этому скрипту приходят как раз тогда, когда что-то не так.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

# ─── что ищем в тексте ───────────────────────────────────────────────────
#
# Значение должно быть похоже на настоящее: восемь и более символов из
# алфавита ключей. Плейсхолдеры вида `<openssl rand -hex 32>` и
# `${NEO4J_PASSWORD}` под шаблон не подходят, и это намеренно — иначе
# проверка кричала бы на собственный README, а на крик, который всегда
# ложный, перестают смотреть.
VALUE = r"['\"]?[A-Za-z0-9+/=_-]{8,}"

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("присвоен MCP_SHARED_SECRET", rf"MCP_SHARED_SECRET\s*[=:]\s*{VALUE}"),
    ("присвоен NEO4J_PASSWORD", rf"NEO4J_PASSWORD\s*[=:]\s*{VALUE}"),
    ("присвоен NEO4J_AUTH", rf"NEO4J_AUTH\s*[=:]\s*neo4j/{VALUE}"),
    ("ключ OpenAI", r"sk-[A-Za-z0-9_-]{20,}"),
    ("токен GitHub", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("ключ AWS", r"AKIA[0-9A-Z]{16}"),
    ("токен Slack", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("приватный ключ", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("присвоен LLM-ключ", rf"(ANTHROPIC|OPENAI|GOOGLE)_API_KEY\s*[=:]\s*{VALUE}"),
)

# Пути, которых не должно быть ни в рабочем каталоге, ни в истории.
#
# Третья колонка — важность, и она здесь не для красоты. Первая редакция
# считала любой лишний путь блокером и выдала 286 строк FAIL, из которых
# 284 были про отчёты прогонов: сообщение «публиковать нельзя» утонуло в
# мусоре, а два настоящих пункта (нет LICENSE, нет ARCHITECTURE.md) ушли
# наверх и потерялись. Проверка, которая кричит одинаково про утёкший
# пароль и про лишний .json, — это лампочка без надписи, ровно то, что
# заход разбирал в soft-промахах.
#
#   FAIL — уедет наружу то, чему там не место: секрет, проприетарный файл
#          вендора, чужие данные;
#   WARN — сор в репозитории: публикации не мешает, но чистить стоит.
FORBIDDEN_PATHS: tuple[tuple[str, str, str], ...] = (
    (r"(^|/)\.env$", "файл .env с секретами", "FAIL"),
    (r"(^|/)\.env\.(?!example).+$", "локальный .env.*", "FAIL"),
    (r"\.hbk$", "справка платформы 1С — проприетарные файлы вендора", "FAIL"),
    (r"\.(cf|dt|epf|erf)$", "бинарники 1С", "FAIL"),
    (r"\.lic$", "файл лицензии", "FAIL"),
    (r"\.(pem|key|p12|pfx)$", "ключ или сертификат", "FAIL"),
    (r"^\.cursor/mcp\.json$", "конфигурация Cursor с bearer-токеном", "FAIL"),
    (r"^v8std-data/", "чужой корпус стандартов (забирается fetch_v8std.py)", "FAIL"),
    (r"^workspace/(?!\.gitkeep|AGENTS\.md)", "выгрузка конфигурации 1С", "FAIL"),
    (r"^platform-help-data/(?!\.gitkeep|README)", "справка платформы", "FAIL"),
    (r"^evals/reports/(?!\.gitkeep)",
     "отчёты прогонов: локальный сор, растут с каждым прогоном "
     "(git rm -r --cached evals/reports)", "WARN"),
)

REQUIRED_FILES: tuple[tuple[str, str], ...] = (
    ("README.md", "с чего начинает пришедший со стороны"),
    ("ARCHITECTURE.md", "устройство набора в двух страницах"),
    ("LICENSE", "без него код формально «все права защищены»"),
    (".env.example", "шаблон конфигурации"),
    (".gitignore", "иначе секреты уедут при первом же add -A"),
)


def scan_text(text: str) -> list[str]:
    """
    Что подозрительного нашлось в этом тексте.

    Отдельная функция, потому что её можно проверить тестом, не заводя
    репозиторий с подложенным секретом.
    """
    found = []
    for name, pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            found.append(name)
    return found


def forbidden(path: str) -> tuple[str, str]:
    """(чем плох, важность) или две пустые строки."""
    for pattern, why, level in FORBIDDEN_PATHS:
        if re.search(pattern, path):
            return why, level
    return "", ""


def summarize(paths: list[str], prefix: str) -> list[tuple[str, str]]:
    """
    Свернуть находки в строки «N файлов такого-то вида», а не по строке на
    файл: сто сорок три одинаковых сообщения — это одно сообщение и число.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        why, level = forbidden(path)
        if why:
            groups.setdefault((why, level), []).append(path)
    out = []
    for (why, level), found in sorted(groups.items()):
        example = ", ".join(sorted(found)[:2])
        tail = f" и ещё {len(found) - 2}" if len(found) > 2 else ""
        out.append((level, f"{prefix}: {why} — {len(found)} шт. "
                           f"({example}{tail})"))
    return out


# ─── работа с гитом ──────────────────────────────────────────────────────


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def is_repo() -> bool:
    return (ROOT / ".git").exists()


def tracked_files() -> list[str]:
    return [p for p in git("ls-files").splitlines() if p]


def historical_paths() -> set[str]:
    """Все пути, когда-либо бывшие в истории, включая удалённые."""
    return {p for p in git("log", "--all", "--pretty=format:", "--name-only")
            .splitlines() if p.strip()}


def history_blobs() -> list[tuple[str, str]]:
    """
    (путь, содержимое) для всех текстовых объектов истории.

    Читаем именно объекты, а не рабочий каталог: файл, удалённый коммитом,
    из истории никуда не девается — и утечёт вместе с репозиторием.
    """
    listing = git("rev-list", "--objects", "--all").splitlines()
    out = []
    for line in listing:
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, path = parts
        if not path.strip():
            continue
        if Path(path).suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".hbk",
                ".jar", ".ico", ".woff", ".woff2"):
            continue
        proc = subprocess.run(
            ["git", "cat-file", "-p", sha], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            continue
        out.append((path, proc.stdout))
    return out


# ─── проверки ────────────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []
        self.oks: list[str] = []

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def ok(self, msg: str) -> None:
        self.oks.append(msg)


def check_required(rep: Report) -> None:
    missing = [(n, why) for n, why in REQUIRED_FILES if not (ROOT / n).exists()]
    for name, why in missing:
        rep.fail(f"нет {name} — {why}")
    if not missing:
        rep.ok(f"обязательные файлы на месте ({len(REQUIRED_FILES)})")


def check_worktree(rep: Report) -> None:
    """Отслеживаемые файлы: пути и содержимое."""
    if not is_repo():
        rep.warn("это не git-репозиторий — проверены только файлы на диске")
        return
    found = summarize(tracked_files(), "под версионным контролем")
    for level, msg in found:
        (rep.fail if level == "FAIL" else rep.warn)(msg)
    if not found:
        rep.ok("в отслеживаемых файлах нет секретов и приватных данных")


def check_history(rep: Report) -> None:
    if not is_repo():
        return
    ever = summarize(sorted(historical_paths()), "в ИСТОРИИ")
    for level, msg in ever:
        if level == "FAIL":
            rep.fail(msg + ". Удаление файла сегодня историю не чистит: "
                           "нужен git filter-repo или новый репозиторий")
        else:
            rep.warn(msg + ". В истории останется даже после git rm — "
                           "но это сор, а не утечка")
    if not ever:
        rep.ok("в истории не было ни .env, ни .hbk, ни выгрузки 1С")

    hits: list[str] = []
    blobs = history_blobs()
    for path, text in blobs:
        for name in scan_text(text):
            hits.append(f"{path}: {name}")
    for hit in sorted(set(hits)):
        rep.fail(f"похоже на секрет в истории — {hit}")
    if not hits:
        rep.ok(f"по содержимому истории чисто ({len(blobs)} объектов)")


def check_gitignore(rep: Report) -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8") \
        if (ROOT / ".gitignore").exists() else ""
    for needle, why in ((".env", "секреты"),
                        ("/workspace/", "выгрузка конфигурации"),
                        ("/platform-help-data/", "справка платформы"),
                        ("/v8std-data/", "чужой корпус"),
                        ("/evals/reports/", "отчёты прогонов")):
        if needle not in text:
            rep.warn(f".gitignore не закрывает {needle} ({why})")


def check_dead_weight(rep: Report) -> None:
    """
    PERF-11. Мёртвый груз в образах — предупреждение, не блокер.

    Образ справки тянет ~2,5 ГБ колёс `nvidia-*` и `triton`: `torch` в
    сборке по умолчанию объявляет CUDA-рантайм обязательной зависимостью,
    независимо от того, есть ли на машине GPU. На стенде GPU не проброшен —
    два с половиной гигабайта не выполняются ни разу.

    Почему WARN, а не FAIL. Лечится это пересборкой лока (`make lock`), для
    которой нужны сеть и docker. Отказ на этом означал бы, что проверка
    краснеет по причине, которую сегодняшний коммит исправить не может, —
    ровно то, из-за чего у истории гита появился `--worktree-only`.
    Публиковаться с толстым образом можно; не знать о нём — нельзя.
    """
    lock = ROOT / "1c-mcp-suite" / "requirements-embeddings.lock.txt"
    src = ROOT / "1c-mcp-suite" / "requirements-embeddings.txt"
    if not lock.exists() or not src.exists():
        return

    lock_text = lock.read_text(encoding="utf-8", errors="replace")
    heavy = sorted({
        line.split("==")[0]
        for line in lock_text.splitlines()
        if line[:1].isalpha() and (line.startswith(("nvidia-", "triton")))
    })
    if not heavy:
        return

    declares_cpu = "download.pytorch.org/whl/cpu" in src.read_text(
        encoding="utf-8", errors="replace")
    if declares_cpu:
        rep.warn(
            f"лок образа справки всё ещё тянет CUDA ({len(heavy)} пакетов, "
            f"~2,5 ГБ мёртвого груза). Индекс CPU-сборок в "
            f"requirements-embeddings.txt уже объявлен — осталось пересобрать: "
            f"make lock && docker compose build mcp-platform-help"
        )
    else:
        rep.warn(
            f"лок образа справки тянет CUDA ({len(heavy)} пакетов, ~2,5 ГБ). "
            f"На машине без проброшенного GPU они мертвы — см. PERF-11"
        )


def check_env_example(rep: Report) -> None:
    """
    Шаблон обязан оставаться шаблоном.

    Самый вероятный способ утечки здесь — не злой умысел, а привычка:
    заполнил .env.example своими значениями «чтобы работало» и закоммитил.
    """
    for name in (".env.example", "1c-mcp-suite/.env.example"):
        path = ROOT / name
        if not path.exists():
            continue
        found = scan_text(path.read_text(encoding="utf-8"))
        for what in found:
            rep.fail(f"{name}: {what} — шаблон должен оставаться пустым")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    worktree_only = "--worktree-only" in argv
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("Ключи:")
        print("  --worktree-only   не проверять историю гита (см. CI-2)")
        return 0

    rep = Report()
    check_required(rep)
    check_gitignore(rep)
    check_env_example(rep)
    check_dead_weight(rep)
    check_worktree(rep)
    if worktree_only:
        # CI-2. Зачем этот ключ вообще нужен.
        #
        # Проверка истории отвечает на вопрос «не уедет ли наружу то, чему
        # там не место, ВМЕСТЕ С РЕПОЗИТОРИЕМ». Ответ на него не меняется от
        # пуша: файл, попавший в историю год назад, лечится только
        # git filter-repo или новым репозиторием. Поставить такую проверку
        # на каждый пуш значит завести CI, который красный всегда и по
        # причине, которую сегодняшний коммит исправить не может.
        #
        # Красный CI, который нельзя позеленить, перестают читать за неделю
        # — и вместе с ним перестают читать 937 тестов, стоящих рядом. Это
        # ровно та лампочка без надписи, которую заход 5 разбирал в
        # soft-промахах, только ценой ей будет весь сторож.
        #
        # Поэтому разделено по вопросам, а не по строгости:
        #   на каждый пуш   — рабочий каталог: «не добавил ли Я секрет»;
        #   перед публикацией — плюс история: «чист ли репозиторий целиком».
        #
        # Второй запускается человеком или вручную из Actions, и провал в
        # нём — повод принимать решение, а не чинить коммит.
        rep.warn("история не проверялась (--worktree-only): вопрос «чист ли "
                 "репозиторий целиком» решается перед публикацией, а не на "
                 "каждом пуше")
    else:
        try:
            check_history(rep)
        except RuntimeError as exc:
            rep.warn(f"историю проверить не удалось: {exc}")

    print("Проверка перед публикацией")
    print("=" * 60)
    for msg in rep.oks:
        print(f"  OK    {msg}")
    for msg in rep.warns:
        print(f"  WARN  {msg}")
    for msg in rep.fails:
        print(f"  FAIL  {msg}")
    print("=" * 60)
    if rep.fails:
        print(f"Публиковать нельзя: {len(rep.fails)} FAIL.")
        return 1
    if rep.warns:
        print(f"Можно публиковать. Предупреждений: {len(rep.warns)} — "
              f"это не блокеры, но посмотрите.")
        return 0
    print("Чисто.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
