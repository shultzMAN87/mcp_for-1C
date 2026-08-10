#!/usr/bin/env python3
"""
Точка входа контейнера `v8std-mcp` (STD-4).

Сервер здесь чужой — это `scripts/v8std_mcp_server.py` из репозитория
zeegin/v8std, положенный в образ монтированием `./v8std-data:/opt/v8std:ro`
(забирает `scripts/fetch_v8std.py`). Мы его не форкаем и не правим: своё —
только запуск с нужными флагами и внятная диагностика, когда корпуса нет.

Почему обёртка, а не `command:` в compose:

  1. Флагов девять, часть — списковые (`--allowed-host` повторяется), в YAML
     это нечитаемо.
  2. Без проверки файлов отсутствие корпуса выглядит как traceback внутри
     чужого кода. Здесь оно выглядит как «запустите fetch_v8std.py».
  3. `RetrievalRules.load()` при отсутствии `retrieval-rules.yml` молча
     возвращает пустой список правил — сервер поднимется, а
     `v8std_explain_snippet` будет тихо работать хуже. Такую деградацию надо
     видеть в логе, а не обнаруживать через месяц.

Режим без локального индекса. Если `docs/ai/pages.jsonl` нет, а
V8STD_ALLOW_REMOTE_INDEX=1 (по умолчанию), сервер стартует со штатными
URL-ами v8std.ru и сам скачает индекс в кеш. Это удобно для первого запуска,
но требует сети (и прокси, если он у вас системный). Запросы агента при этом
всё равно остаются локальными — наружу уходит только загрузка корпуса.
V8STD_ALLOW_REMOTE_INDEX=0 запрещает и это: тогда без корпуса контейнер
падает с кодом 78 (EX_CONFIG), как остальные серверы набора при плохой
конфигурации.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("V8STD_REPO_ROOT", "/opt/v8std"))
SERVER = REPO_ROOT / "scripts" / "v8std_mcp_server.py"
PAGES = REPO_ROOT / "docs" / "ai" / "pages.jsonl"
VECTORS = REPO_ROOT / "docs" / "ai" / "search-vectors.jsonl"
RULES = REPO_ROOT / "retrieval-rules.yml"

HOST = os.environ.get("V8STD_MCP_HOST", "0.0.0.0")
PORT = os.environ.get("V8STD_MCP_PORT", "8765")
MCP_PATH = os.environ.get("V8STD_MCP_PATH", "/mcp")
CACHE_DIR = os.environ.get("V8STD_MCP_CACHE_DIR", "/var/lib/v8std-mcp")
LOG_LEVEL = os.environ.get("V8STD_MCP_LOG_LEVEL", "WARNING")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _die(message: str, hint: str = "") -> None:
    sys.stderr.write(f"[FATAL] v8std-mcp: {message}\n")
    if hint:
        sys.stderr.write(hint.rstrip() + "\n")
    raise SystemExit(78)  # EX_CONFIG — как в mcp_http.run


FETCH_HINT = (
    "Корпус забирается с хоста одной командой:\n"
    "    python3 scripts/fetch_v8std.py\n"
    "Каталог ./v8std-data монтируется в контейнер как /opt/v8std:ro\n"
    "(см. сервис v8std-mcp в docker-compose.yml)."
)


def main() -> int:
    if not SERVER.is_file():
        _die(
            f"не найден сервер {SERVER}",
            FETCH_HINT,
        )

    argv = [
        sys.executable,
        str(SERVER),
        "--cache-dir", CACHE_DIR,
        "--host", HOST,
        "--port", str(PORT),
        "--mcp-path", MCP_PATH,
        "--log-level", LOG_LEVEL,
        # Аналог TR-4 у остальных серверов: Host/Origin ограничены явно.
        # Порт в шаблонах со звёздочкой — их парсер это допускает.
        "--allowed-host", "127.0.0.1:*",
        "--allowed-host", "localhost:*",
        "--allowed-host", "v8std-mcp:*",
        "--allowed-origin", "http://127.0.0.1:*",
        "--allowed-origin", "http://localhost:*",
    ]

    if PAGES.is_file() and PAGES.stat().st_size > 0:
        argv += ["--pages", str(PAGES)]
        if VECTORS.is_file() and VECTORS.stat().st_size > 0:
            argv += ["--vectors", str(VECTORS)]
        else:
            sys.stderr.write(
                "[v8std-mcp] нет search-vectors.jsonl — семантическая ветка поиска "
                "выключена, останутся точное совпадение и BM25. "
                "Лечится повторным запуском scripts/fetch_v8std.py\n"
            )
        print(f"[v8std-mcp] локальный индекс: {PAGES}", flush=True)
    elif _flag("V8STD_ALLOW_REMOTE_INDEX", True):
        sys.stderr.write(
            "[v8std-mcp] локального индекса нет — беру опубликованный с v8std.ru "
            "(нужна сеть; при системном прокси задайте HTTPS_PROXY у сервиса).\n"
            + FETCH_HINT + "\n"
        )
    else:
        _die(
            f"нет индекса {PAGES}, а V8STD_ALLOW_REMOTE_INDEX=0",
            FETCH_HINT,
        )

    if not RULES.is_file():
        sys.stderr.write(
            "[v8std-mcp] нет retrieval-rules.yml — v8std_explain_snippet будет "
            "работать без алиасов и сигнатур вызовов, то есть заметно хуже. "
            "Это не ошибка запуска, но и не норма.\n"
        )

    print(
        f"[v8std-mcp] Streamable HTTP на http://{HOST}:{PORT}{MCP_PATH} "
        f"(stateless, чужой сервер v8std, авторизации нет — порт только на loopback)",
        flush=True,
    )
    # exec, а не subprocess: сигналы от docker должны доходить до сервера.
    os.chdir(REPO_ROOT)
    os.execv(sys.executable, argv)


if __name__ == "__main__":
    raise SystemExit(main())
