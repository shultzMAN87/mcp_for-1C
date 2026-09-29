#!/usr/bin/env python3
"""
Preflight-проверка окружения для 1C MCP Suite.

Запускает набор проверок и выдаёт таблицу "что готово / чего не хватает".
Используется перед первым `docker compose up`, чтобы новый пользователь сразу
увидел конкретный список шагов настройки, а не ловил загадочные ошибки при
старте контейнеров.

Зависимости: только stdlib Python 3.8+. Не требует pip install чего бы то ни
было — пользователь может ещё ничего не установить.

Использование:
    py scripts/check_prereqs.py            # Windows
    python3 scripts/check_prereqs.py       # Linux/macOS
    make check-prereqs                     # Linux/macOS (через Makefile)

Выход:
    0 — все критичные проверки прошли
    1 — есть хотя бы один FAIL (стек не поднимется)
    Предупреждения (WARN) не приводят к ненулевому exit code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# B-3. Печать не должна ронять скрипт.
#
# `FAIL-2`: на приёмке 15 августа набор упал с UnicodeEncodeError на знаке
# ⚠ — консоль была cp1251, а в строке стоял символ, которого в ней нет.
# Тогда починили сервер справки и дочерние процессы run_all_tests, но сами
# хостовые скрипты остались: у них вывод уходит в консоль напрямую, и
# `$OutputEncoding` в PowerShell тут не помогает — он про то, чем консоль
# ЧИТАЕТ вывод, а не чем Python его кодирует.
#
# Воспроизводится одной строкой:
#     PYTHONIOENCODING=cp1251 python3 scripts/eval_all.py --summary-only
#
# errors=replace, а не encoding=utf-8: подмена кодировки дала бы кракозябры
# в cp1251-консоли, а замена — всего лишь «?» вместо галочки. Испортить
# украшение можно, уронить diagnostics-скрипт нельзя. Особенно
# check_prereqs: к нему идут именно тогда, когда что-то не работает.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# ─── Цвета в консоли ─────────────────────────────────────────────────────────
# ANSI escape-коды. Автоматически отключаются, если:
#   - выход не в TTY (пайп, редирект в файл)
#   - переменная NO_COLOR установлена (см. https://no-color.org/)
#   - Windows и отсутствует Windows Terminal (старый cmd не умеет ANSI)

_ENABLE_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and (os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("TERM"))
)

def _c(code: str, text: str) -> str:
    """Красим текст, если цвета разрешены."""
    if not _ENABLE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(s: str) -> str:  return _c("32", s)
def red(s: str) -> str:    return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def bold(s: str) -> str:   return _c("1",  s)
def dim(s: str) -> str:    return _c("2",  s)


# ─── Модель результата ──────────────────────────────────────────────────────

@dataclass
class CheckResult:
    status: str   # "ok" | "warn" | "fail"
    message: str
    hint: str = ""


def ok(msg: str) -> CheckResult:
    return CheckResult("ok", msg)

def warn(msg: str, hint: str = "") -> CheckResult:
    return CheckResult("warn", msg, hint)

def fail(msg: str, hint: str = "") -> CheckResult:
    return CheckResult("fail", msg, hint)


# ─── Корень проекта ─────────────────────────────────────────────────────────
# Скрипт лежит в scripts/, корень — на уровень выше.

ROOT = Path(__file__).resolve().parent.parent


# ─── Чтение .env ─────────────────────────────────────────────────────────────

def parse_env_file(path: Path) -> dict[str, str]:
    """
    Парсит .env как простой словарь. Не поддерживает экспорт, подстановку
    переменных и многострочные значения — нам такие и не нужны.
    Пустые строки и комментарии (#) пропускаются.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Срезаем внешние кавычки, если есть
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def is_placeholder(value: str) -> bool:
    """
    Плейсхолдер — это значение, которое явно нужно заменить.
    Распознаём по типичным маркерам, которые встречаются в .env.example.
    """
    if not value:
        return True
    v = value.lower()
    markers = ("<токен>", "<token>", "change_me", "changeme", "<your", "xxx", "example.com")
    return any(m in v for m in markers)


# ─── Сами проверки ──────────────────────────────────────────────────────────

# B-3, третья встреча. Вывод `docker` читается ЯВНО в utf-8 с заменой
# нечитаемого. Без этого `text=True` берёт кодировку консоли (cp1251 на
# русской Windows), и любое сообщение docker'а с не-ASCII символом роняет
# декодирование в потоке-читателе — причём `run()` этого не замечает и
# отдаёт stderr=None.
#
# Особенно неудачно это здесь: к `check_prereqs.py` приходят именно тогда,
# когда что-то не работает, то есть ровно тогда, когда docker и печатает
# необычные сообщения.
def check_docker() -> CheckResult:
    """Docker установлен и запущен."""
    if not shutil.which("docker"):
        return fail(
            "Docker не найден в PATH",
            "Установите Docker Desktop (https://www.docker.com/products/docker-desktop) "
            "или Docker Engine для Linux.",
        )
    try:
        out = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return fail(
                "Docker установлен, но не отвечает",
                "Запустите Docker Desktop или сервис dockerd.",
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return fail(f"Docker недоступен: {e}", "Проверьте, что Docker Desktop запущен.")
    return ok("Docker установлен и запущен")


def check_docker_compose() -> CheckResult:
    """docker compose (v2) доступен."""
    if not shutil.which("docker"):
        return fail("Docker не найден — compose проверить невозможно")
    try:
        out = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return fail(
                "docker compose (v2) недоступен",
                "Проверьте, что установлен Docker Desktop или docker-compose-plugin.",
            )
        # Пример вывода: "Docker Compose version v2.24.6"
        match = re.search(r"v?(\d+\.\d+\.\d+)", out.stdout)
        version = match.group(1) if match else "?"
        return ok(f"docker compose v{version}")
    except Exception as e:
        return fail(f"Ошибка вызова docker compose: {e}")


def check_env_file() -> CheckResult:
    """`.env` создан (не `.env.example`)."""
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env.exists():
        hint = "Скопируйте .env.example в .env и заполните ключи:\n" \
               "  Linux/macOS: cp .env.example .env\n" \
               "  Windows:     Copy-Item .env.example .env"
        if not example.exists():
            return fail(".env и .env.example отсутствуют", hint)
        return fail(".env не создан", hint)
    return ok(".env существует")


# DOC-1. LLM-ключи нужны ТОЛЬКО оркестратору OpenCode — сервису из
# профиля `opencode`, который в связке с Cursor не поднимается вообще.
# Проверка при этом ставила FAIL и красила весь отчёт: она кричала о том,
# что не влияет.
#
# Это зеркало той же болезни, ради которой затеян весь заход. Там отказ
# выглядел как успех, здесь успех выглядит как отказ, и цена одинаковая —
# в отчёт перестают смотреть. Красный, который горит всегда, ничего не
# сообщает.
#
# Теперь: если профиль `opencode` не запрошен, отсутствие ключей — WARN с
# пояснением, за что они отвечают. Профиль считаем запрошенным, если он
# перечислен в COMPOSE_PROFILES (переменная окружения или .env).


def _opencode_requested(env_vars: dict[str, str]) -> bool:
    raw = os.environ.get("COMPOSE_PROFILES") or env_vars.get("COMPOSE_PROFILES", "")
    return "opencode" in [p.strip() for p in raw.split(",") if p.strip()]


def check_llm_key(env_vars: dict[str, str]) -> CheckResult:
    """Хотя бы один LLM-ключ задан и не плейсхолдер."""
    openrouter = env_vars.get("OPENROUTER_API_KEY", "")
    anthropic  = env_vars.get("ANTHROPIC_API_KEY", "")
    openrouter_ok = openrouter and not is_placeholder(openrouter)
    anthropic_ok  = anthropic  and not is_placeholder(anthropic)
    if openrouter_ok and anthropic_ok:
        return ok("LLM-ключи заданы: OPENROUTER_API_KEY и ANTHROPIC_API_KEY")
    if openrouter_ok:
        return ok("LLM-ключ задан: OPENROUTER_API_KEY")
    if anthropic_ok:
        return ok("LLM-ключ задан: ANTHROPIC_API_KEY")

    hint = ("Заполните в .env хотя бы одну переменную:\n"
            "  OPENROUTER_API_KEY=sk-or-v1-...\n"
            "  ANTHROPIC_API_KEY=sk-ant-...")
    if _opencode_requested(env_vars):
        return fail("Ни один LLM-ключ не задан (или все — плейсхолдеры)", hint)
    return warn(
        "LLM-ключи не заданы — для связки с Cursor это норма",
        "Ключи нужны только оркестратору OpenCode (профиль `opencode`), "
        "который здесь не поднимается. Пять MCP-серверов работают без них.\n"
        "Если всё же нужен OpenCode:\n" + hint,
    )


def check_llm_models(env_vars: dict[str, str]) -> CheckResult:
    """LLM_MODEL_STRONG и LLM_MODEL_FAST заданы."""
    missing = [k for k in ("LLM_MODEL_STRONG", "LLM_MODEL_FAST") if not env_vars.get(k)]
    if missing:
        if not _opencode_requested(env_vars):
            # DOC-1, вторая половина: те же модели и тот же адресат.
            return ok(f"Модели оркестратора не заданы ({', '.join(missing)}) — "
                      f"профиль `opencode` не поднят, они не нужны")
        return warn(
            f"Не заданы: {', '.join(missing)}",
            "В .env пропишите модели оркестратора, например:\n"
            "  LLM_MODEL_STRONG=anthropic/claude-sonnet-4.5\n"
            "  LLM_MODEL_FAST=anthropic/claude-haiku-4.5\n"
            "Без них оркестратор упадёт на первом вызове субагента.",
        )
    return ok(f"Модели: STRONG={env_vars['LLM_MODEL_STRONG']}, "
              f"FAST={env_vars['LLM_MODEL_FAST']}")


def check_neo4j_password(env_vars: dict[str, str]) -> CheckResult:
    """
    Пароль Neo4j задан и не является дефолтом или плейсхолдером.

    В docker-compose.yml используется ${NEO4J_PASSWORD:?...} — compose упадёт
    с явной ошибкой, если переменная не задана. Но старый дефолт 'password1c'
    compose не поймает (переменная "задана"), поэтому ловим его здесь.
    """
    pw = env_vars.get("NEO4J_PASSWORD", "")

    # Единая подсказка для всех проблемных случаев
    hint = (
        "Сгенерируйте случайный пароль и пропишите в .env:\n"
        "  Linux/macOS: NEO4J_PASSWORD=$(openssl rand -base64 24)\n"
        "  Windows:     NEO4J_PASSWORD=<результат [Convert]::ToBase64String((1..18|%{[byte](Get-Random -Max 256)}))>"
    )

    if not pw:
        return fail("NEO4J_PASSWORD не задан — docker compose up не запустится", hint)
    if pw == "password1c":
        return fail("NEO4J_PASSWORD использует старый дефолт 'password1c'", hint)
    if is_placeholder(pw):
        return fail("NEO4J_PASSWORD — плейсхолдер, замените на реальный пароль", hint)
    if len(pw) < 12:
        return warn(
            f"NEO4J_PASSWORD короткий ({len(pw)} символов) — рекомендуется от 16",
            hint,
        )
    return ok("NEO4J_PASSWORD задан")


def check_mcp_shared_secret(env_vars: dict[str, str]) -> CheckResult:
    """
    SEC-3. Общий секрет MCP задан и не плейсхолдер.

    Раньше пустое значение просто отключало аутентификацию, и стек поднимался
    открытым. Теперь серверы с пустым секретом не стартуют (выход с кодом 78),
    но лучше поймать это здесь, до docker compose up.
    """
    secret = env_vars.get("MCP_SHARED_SECRET", "")
    hint = (
        "Сгенерируйте секрет и пропишите в .env:\n"
        "  Linux/macOS: openssl rand -hex 32\n"
        "  Windows:     python -c \"import secrets;print(secrets.token_hex(32))\"\n"
        "Тот же секрет нужен в .cursor/mcp.json."
    )
    if not secret:
        return fail("MCP_SHARED_SECRET не задан — серверы не стартуют (SEC-3)", hint)
    if is_placeholder(secret):
        return fail("MCP_SHARED_SECRET — плейсхолдер, замените на реальный", hint)
    if len(secret) < 32:
        return warn(
            f"MCP_SHARED_SECRET короткий ({len(secret)} символов) — рекомендуется от 48",
            hint,
        )
    return ok(f"MCP_SHARED_SECRET задан ({len(secret)} символов)")


def check_cursor_config() -> CheckResult:
    """
    CFG-1. Конфиг подключения Cursor существует и в нём нет плейсхолдера.

    Файл кладётся в корень рабочего проекта 1С, поэтому здесь проверяется
    только копия в репозитории — если её нет, подключать Cursor нечем.
    """
    path = ROOT / ".cursor" / "mcp.json"
    if not path.is_file():
        return warn(
            ".cursor/mcp.json не найден — Cursor не к чему подключать",
            "Файл идёт в архиве Захода 1; положите его в корень рабочего проекта 1С.",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if "ЗАМЕНИТЬ_НА_MCP_SHARED_SECRET" in text:
        return fail(
            ".cursor/mcp.json содержит плейсхолдер вместо секрета",
            "Впишите значение MCP_SHARED_SECRET из .env в поле Authorization.",
        )
    if "/sse" in text:
        return fail(
            ".cursor/mcp.json указывает на /sse — этого эндпоинта больше нет (TR-1)",
            "Замените /sse на /mcp во всех четырёх адресах.",
        )
    return ok(".cursor/mcp.json заполнен")


def check_workspace(env_vars: dict[str, str]) -> CheckResult:
    """
    FIX-1. Каталог с XML-выгрузкой конфигурации.

    На него смотрят metadata-indexer, bsl-checker и workspace-watcher.
    Пустой каталог — это молчаливо пустой граф и bsl_check_directory,
    который «не нашёл замечаний», потому что нечего проверять.

    Проверяется тот же каталог, что монтирует docker-compose.yml:
    ${WORKSPACE_DIR:-./workspace}. Раньше путь был зашит как ROOT/workspace,
    и при выгрузке вне репозитория (WORKSPACE_DIR=D:/...) проверка давала
    ложный FAIL, хотя контейнеры видели выгрузку. Приоритет как у compose:
    переменная окружения оболочки, затем .env, затем ./workspace.
    Относительный путь считается от корня проекта (каталог docker-compose.yml).
    """
    raw = (os.environ.get("WORKSPACE_DIR") or env_vars.get("WORKSPACE_DIR") or "").strip()
    if raw:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = ROOT / path
        label = f"WORKSPACE_DIR={raw}"
    else:
        path = ROOT / "workspace"
        label = "workspace/"

    if not path.is_dir():
        return fail(
            f"{label}: каталог не найден — индексировать нечего",
            f"Проверьте путь ({path}). Либо выгрузите конфигурацию в XML "
            "(Конфигуратор → Конфигурация → Выгрузить конфигурацию в файлы) "
            "в этот каталог, либо поправьте WORKSPACE_DIR в .env.",
        )
    if not _dir_has_files(path, (".xml", ".bsl")):
        return fail(
            f"{label}: в корне каталога нет ни .xml, ни .bsl",
            "Граф метаданных будет пустым, bsl-checker не найдёт файлов. "
            "WORKSPACE_DIR должен указывать на корень выгрузки — туда, где "
            "лежит Configuration.xml, а не на каталог уровнем выше.",
        )
    return ok(f"{label}: выгрузка найдена ({path})")


def check_compose_file_separator(env_vars: dict[str, str]) -> CheckResult:
    """
    COMPOSE_FILE использует правильный разделитель для текущей ОС.

    Это та самая Windows-ловушка: разделитель путей в переменной COMPOSE_FILE
    зависит от ОС — на Linux/macOS это ':', на Windows ';'. Если скопировать
    .env.example как есть на Windows, docker compose вывалится с ошибкой
    'CreateFile ...:... The filename syntax is incorrect'.
    """
    value = env_vars.get("COMPOSE_FILE", "")
    if not value:
        # Необязательная переменная (нужна только при мультифайловом compose)
        return ok("COMPOSE_FILE не задан (OK, используется docker-compose.yml по умолчанию)")

    is_windows = os.name == "nt"
    expected_sep = ";" if is_windows else ":"
    wrong_sep    = ":" if is_windows else ";"

    # Смотрим, какой разделитель в значении. Но двоеточие в пути на Windows
    # может быть и частью буквы диска ("D:\..."). Поэтому исключаем такие.
    # Практика: на Linux в COMPOSE_FILE не должно быть ';', на Windows — ':'
    # за пределами "C:\..." паттерна.

    if is_windows:
        # Нас интересует ':', который разделяет пути в COMPOSE_FILE
        # (а не ':' после буквы диска в абсолютном пути вроде "C:\...").
        # Убираем все абсолютные пути с буквой диска и смотрим, остался ли ':'.
        stripped = re.sub(r"\b[A-Za-z]:[\\/]", "", value)
        if ":" in stripped:
            fixed_value = value.replace(":", ";", 1)
            return fail(
                "COMPOSE_FILE использует ':' как разделитель — это для Linux/macOS.\n"
                f"    Текущее значение: {value}",
                "На Windows разделитель — ';'. Исправьте в .env:\n"
                f"  COMPOSE_FILE={fixed_value}",
            )
    else:
        if ";" in value:
            return fail(
                "COMPOSE_FILE использует ';' как разделитель — это для Windows.\n"
                f"    Текущее значение: {value}",
                "На Linux/macOS разделитель — ':'. Исправьте в .env:\n"
                f"  COMPOSE_FILE={value.replace(';', ':', 1)}",
            )
    return ok(f"COMPOSE_FILE разделитель корректен для {'Windows' if is_windows else 'Linux/macOS'}")


def _dir_has_files(path: Path, extensions: tuple[str, ...] | None = None) -> bool:
    """Есть ли в директории хотя бы один файл (опционально — с нужным расширением), не считая README/.gitignore."""
    if not path.is_dir():
        return False
    for entry in path.iterdir():
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if name.startswith(".") or name.startswith("readme"):
            continue
        if extensions is None or name.endswith(extensions):
            return True
    return False


# HELP-COPY. Справка копируется скриптом, а не руками: руками брали весь
# bin (с английскими *_root.hbk) или не ту версию платформы.
_HELP_COPY_HINT = (
    "      python scripts\\fetch_platform_help.py\n"
    "    Каталог bin платформы берётся из ONEC_BIN_DIR в .env, например\n"
    "      ONEC_BIN_DIR=C:\\Program Files\\1cv8\\8.3.27.1606\\bin\n"
    "    без неё — самая новая версия в C:\\Program Files\\1cv8.\n"
    "    Найденные версии: python scripts\\fetch_platform_help.py --list"
)


def _help_source_note(path: Path) -> str:
    """Версия платформы из SOURCE.json, если справку клал fetch_platform_help."""
    try:
        info = json.loads((path / "SOURCE.json").read_text(encoding="utf-8"))
        return f", платформа {info.get('platform_version', '?')}"
    except Exception:
        return ""


def check_platform_help() -> CheckResult:
    """
    `.hbk`-файлы справки платформы (опционально).

    FIX-11: проверяем не наличие файлов, а наличие содержимого.

    Раньше здесь было `есть ли хоть один .hbk`. Этого мало: в
    platform-help-data легко оказаться сорока файлами, из которых 38 весят
    по 0.0 МБ, — и проверка их пропустит. На практике так и вышло: справка
    индексировалась из одного `shcntx_ru.hbk`, а `shlang_ru.hbk` (встроенный
    язык) и `shquery_ru.hbk` (язык запросов) были заглушками и в индекс не
    попали. Обнаружилось это только через лог индексатора, спустя недели.

    Порог в 1 МБ выбран по фактическим размерам: настоящие файлы справки
    измеряются мегабайтами (shcntx — около 39 МБ), заглушки — десятыми
    долями.
    """
    path = ROOT / "platform-help-data"
    if not _dir_has_files(path, (".hbk",)):
        return warn(
            "platform-help-data/ не содержит .hbk — справка платформы будет пустой",
            "Скопируйте русскую справку (*_ru.hbk) из установленной платформы:\n"
            f"{_HELP_COPY_HINT}",
        )

    STUB_LIMIT = 1024 * 1024

    # Ключевые разделы и их минимальный правдоподобный размер.
    #
    # Порог у каждого свой, и это не придирка: shcntx_ru.hbk весит около
    # 39 МБ, а shquery_ru.hbk — 0,2 МБ, и это его нормальный размер, а не
    # заглушка. Проверено на двух установках платформы (8.3.27 и 8.5.1) —
    # там везде так. Единый порог в 1 МБ давал ложную тревогу «нет
    # ключевых разделов» на совершенно здоровой установке, то есть ровно
    # ту болезнь, которую проверки и должны лечить.
    KEY_FILES = {
        "shcntx_ru.hbk": (1024 * 1024, "контекстная справка (методы и объекты)"),
        "shlang_ru.hbk": (20 * 1024, "встроенный язык"),
        "shquery_ru.hbk": (20 * 1024, "язык запросов"),
        "shclang_ru.hbk": (20 * 1024, "синтаксис языка"),
    }

    files = sorted(path.glob("*.hbk"))
    real = [f for f in files if f.stat().st_size >= STUB_LIMIT]
    stubs = [f for f in files if f.stat().st_size < STUB_LIMIT]
    total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)

    if not real:
        return warn(
            f"platform-help-data/: {len(files)} .hbk, все меньше 1 МБ "
            f"(всего {total_mb:.1f} МБ) — индекс справки будет пустым",
            "Похоже, скопированы заглушки, а не сами файлы справки. "
            "Перезалейте их из установленной платформы:\n"
            f"{_HELP_COPY_HINT}",
        )

    # Перечисляем не «какие пустые» (их бывает три десятка и это шум), а
    # какие ключевые отсутствуют и какие файлы реально с содержимым.
    sizes = {f.name: f.stat().st_size for f in files}
    missing_key = [
        (name, descr) for name, (min_size, descr) in KEY_FILES.items()
        if sizes.get(name, 0) < min_size
    ]

    if missing_key:
        have = ", ".join(f"{f.name} ({f.stat().st_size / (1024*1024):.0f} МБ)"
                         for f in real)
        lost = "\n".join(f"        {n} — {d}" for n, d in missing_key)
        return warn(
            f"platform-help-data/: с содержимым {len(real)} из {len(files)} "
            f"файлов ({total_mb:.1f} МБ), нет {len(missing_key)} ключевых",
            f"Есть: {have}\n"
            f"    Пустые или отсутствуют ключевые разделы:\n{lost}\n"
            "    Без них поиск по справке будет отвечать только по одному разделу.\n"
            "    Перезалейте справку из установленной платформы:\n"
            f"{_HELP_COPY_HINT}",
        )

    if stubs:
        return warn(
            f"platform-help-data/: с содержимым {len(real)} из {len(files)} "
            f"файлов ({total_mb:.1f} МБ), ключевые разделы на месте"
            f"{_help_source_note(path)}",
            f"{len(stubs)} файлов меньше 1 МБ — это дополнительные разделы "
            "(интерфейсы, отчёты). На основные сценарии не влияет.",
        )

    return ok(f"platform-help-data/: {len(real)} .hbk, {total_mb:.1f} МБ"
              f"{_help_source_note(path)}")


def check_v8std_data() -> CheckResult:
    """
    Корпус стандартов для сервера v8std-mcp (STD-1, опционально).

    Смотрим на FETCH.json, а не просто на наличие каталога: корпус может
    быть скачан наполовину, и тогда сервер поднимется, но искать будет не по
    чему. Возраст важен отдельно — стандарты обновляются, и молча работать
    на полугодовалом срезе хуже, чем знать об этом.
    """
    path = ROOT / "v8std-data"
    manifest = path / "FETCH.json"

    if not manifest.is_file():
        return warn(
            "v8std-data/ пуст — сервер стандартов возьмёт индекс с v8std.ru "
            "при старте (нужна сеть)",
            "Забрать корпус локально: python3 scripts/fetch_v8std.py",
        )

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        return warn(
            f"v8std-data/FETCH.json не читается: {type(exc).__name__}",
            "Перезабрать корпус: python3 scripts/fetch_v8std.py --force",
        )

    # V8STD-2: сервер переехал в пакет runtime/. Старая раскладка
    # (scripts/v8std_mcp_server.py) ещё поднимается, но просит обновиться.
    new_server = path / "runtime" / "v8std_mcp_server.py"
    old_server = path / "scripts" / "v8std_mcp_server.py"
    missing = []
    if not new_server.is_file() and not old_server.is_file():
        missing.append("runtime/v8std_mcp_server.py")
    if not (path / "docs" / "ai" / "pages.jsonl").is_file():
        missing.append("docs/ai/pages.jsonl")
    if missing:
        return warn(
            f"v8std-data/: не хватает файлов ({', '.join(missing)})",
            "Перезабрать корпус: python3 scripts/fetch_v8std.py",
        )

    fetched = data.get("fetched_at", "")
    age_days = None
    try:
        from datetime import datetime, timezone
        age_days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(fetched)).days
    except Exception:
        pass

    commit = data.get("commit") or "—"
    if age_days is not None and age_days > 90:
        return warn(
            f"v8std-data/: корпусу {age_days} дн. (commit {commit})",
            "Стандарты обновляются. Обновить: python3 scripts/fetch_v8std.py",
        )

    if not new_server.is_file():
        return warn(
            f"v8std-data/: сервер в старой раскладке scripts/ (commit {commit})",
            "Автор v8std перенёс сервер в пакет runtime/ (V8STD-2). Обновить:\n"
            "      python scripts/fetch_v8std.py\n"
            "      docker compose build v8std-mcp\n"
            "      docker compose up -d --force-recreate v8std-mcp",
        )

    age = f", возраст {age_days} дн." if age_days is not None else ""
    return ok(f"v8std-data/: commit {commit}{age}")


def run_all_checks() -> list[tuple[str, CheckResult]]:
    """
    Выполняет все проверки, возвращает список (label, result).
    Проверки, зависящие от .env, пропускаются если .env не существует.
    """
    checks: list[tuple[str, CheckResult]] = []

    # Независимые от .env проверки
    checks.append(("Docker",           check_docker()))
    checks.append(("docker compose",   check_docker_compose()))

    env_result = check_env_file()
    checks.append((".env",             env_result))

    # Если .env нет — дальнейшие проверки бесполезны
    if env_result.status == "fail":
        return checks

    env_vars = parse_env_file(ROOT / ".env")

    checks.append(("LLM ключ",         check_llm_key(env_vars)))
    checks.append(("LLM модели",       check_llm_models(env_vars)))
    checks.append(("COMPOSE_FILE",     check_compose_file_separator(env_vars)))
    checks.append(("Neo4j пароль",     check_neo4j_password(env_vars)))
    checks.append(("MCP секрет",       check_mcp_shared_secret(env_vars)))
    checks.append((".cursor/mcp.json", check_cursor_config()))
    checks.append(("workspace/",       check_workspace(env_vars)))
    checks.append(("platform-help-data/", check_platform_help()))
    checks.append(("v8std-data/",       check_v8std_data()))

    return checks


def render(results: list[tuple[str, CheckResult]]) -> tuple[int, int, int]:
    """Печатает таблицу и возвращает (n_ok, n_warn, n_fail)."""
    n_ok = n_warn = n_fail = 0

    label_width = max(len(label) for label, _ in results)

    print()
    print(bold("Preflight 1C MCP Suite"))
    print(dim("─" * 70))
    print()

    for label, res in results:
        if res.status == "ok":
            icon = green("✓ OK  ")
            n_ok += 1
        elif res.status == "warn":
            icon = yellow("⚠ WARN")
            n_warn += 1
        else:
            icon = red("✗ FAIL")
            n_fail += 1
        print(f"  {icon}  {label.ljust(label_width)}  {res.message}")

    print()
    print(dim("─" * 70))

    # Подсказки по проблемам
    problems = [(label, res) for label, res in results if res.status in ("warn", "fail")]
    if problems:
        print()
        print(bold("Что исправить:"))
        print()
        for label, res in problems:
            colored = red(label) if res.status == "fail" else yellow(label)
            print(f"  {colored}: {res.message}")
            if res.hint:
                for line in res.hint.splitlines():
                    print(f"      {dim(line)}")
            print()

    # Итог
    summary = f"{green(f'{n_ok} OK')}  {yellow(f'{n_warn} WARN')}  {red(f'{n_fail} FAIL')}"
    print(f"Итого: {summary}")
    print()

    return n_ok, n_warn, n_fail


def main() -> int:
    results = run_all_checks()
    _, _, n_fail = render(results)
    return 1 if n_fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
