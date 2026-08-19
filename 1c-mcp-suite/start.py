#!/usr/bin/env python3
"""
Точка входа для запуска MCP-серверов (Streamable HTTP, TR-1).

ВАЖНО: этот скрипт рассчитан на запуск ВНУТРИ Docker-контейнера,
собранного через Dockerfile.python / Dockerfile.embeddings.
Контейнер копирует файлы вида mcp-metadata-graph/server.py в /app/mcp_metadata_graph.py
(с подчёркиваниями), что и делает их импортируемыми Python-модулями.

Локально, без Docker, импорт сломается — это by design. Используйте docker compose.
"""
import os
import sys
from pathlib import Path

import uvicorn

# Серверы, которые умеет поднимать эта точка входа. Сам start.py едет в ДВА
# образа, и каждый из них поднимает своё:
#   Dockerfile.python     — metadata-graph и query-builder;
#   Dockerfile.embeddings — platform-help (там же torch, qdrant-client и
#                           модель эмбеддингов).
# bsl-checker здесь не значится: у него свой образ (Dockerfile.bsl) и своя
# точка входа. Раньше комментарий утверждал, что все три собираются из
# Dockerfile.python, — это перестало быть правдой, когда справка переехала
# в отдельный образ (B-6).
SERVERS = {
    "metadata-graph":   ("mcp_metadata_graph",  8001),
    "platform-help":    ("mcp_platform_help",   8003),
    "query-builder":    ("mcp_query_builder",   8009),
}

def _check_docker_environment() -> None:
    """Защита от запуска вне Docker — даём понятную ошибку вместо ImportError."""
    here = Path(__file__).resolve().parent
    if str(here) != "/app":
        sys.stderr.write(
            "ОШИБКА: start.py рассчитан на запуск ВНУТРИ Docker-контейнера.\n"
            "Файлы серверов копируются в /app с переименованием через Dockerfile,\n"
            "и без этого импорт по имени модуля невозможен.\n\n"
            "Используйте: docker compose up <service>\n"
        )
        sys.exit(2)


def _wrap_tools_with_metrics(mcp_obj, server_name: str) -> None:
    """Оборачивает все tools декоратором track из mcp_metrics."""
    os.environ.setdefault("MCP_SERVER_NAME", server_name)
    try:
        from mcp_metrics import track
    except Exception as e:
        sys.stderr.write(f"[metrics] mcp_metrics недоступен: {e}\n")
        return

    try:
        tools = getattr(mcp_obj._tool_manager, "_tools", {})
        wrapped = 0
        for tool in tools.values():
            if getattr(tool.fn, "__wrapped_by_track__", False):
                continue
            tool.fn = track(tool.fn)
            try:
                tool.fn.__wrapped_by_track__ = True
            except (AttributeError, TypeError):
                pass
            wrapped += 1
        print(f"[metrics] {server_name}: обёрнуто инструментов: {wrapped}", flush=True)
    except Exception as e:
        sys.stderr.write(f"[metrics] не удалось обернуть tools: {e}\n")


def _wrap_tools_with_usage(mcp_obj, server_name: str) -> None:
    """
    TOOL-1: счётчик вызовов в памяти процесса.

    Здесь же, где и метрики, и по той же причине: схема инструмента уже
    построена, подменять `tool.fn` безопасно. Обёртка на РЕГИСТРАЦИИ
    ломает разрешение аннотаций (`Optional` не виден из чужого модуля) —
    проверено падением регистрации v3-инструментов.

    В отличие от `mcp_metrics`, ничего не пишет на диск: ответ на вопрос
    «звал ли этот инструмент хоть кто-нибудь» нужен на время жизни
    контейнера, и отдаётся он полем `usage` в `*_stats`.
    """
    try:
        from tool_usage import wrap_registered_tools
    except Exception as e:
        sys.stderr.write(f"[usage] tool_usage недоступен: {e}\n")
        return
    try:
        n = wrap_registered_tools(mcp_obj)
        print(f"[usage] {server_name}: под счётчиком инструментов: {n}",
              flush=True)
    except Exception as e:
        sys.stderr.write(f"[usage] не удалось обернуть tools: {e}\n")


def _start_metrics_dashboard_async() -> None:
    """Поднимает HTTP-дашборд метрик в отдельном потоке."""
    if os.environ.get("METRICS_DASHBOARD", "true").lower() not in ("true", "1", "yes"):
        return
    try:
        import threading
        from mcp_metrics import get_dashboard_app
    except Exception as e:
        sys.stderr.write(f"[metrics] dashboard недоступен: {e}\n")
        return

    dash_port = int(os.environ.get("METRICS_PORT", "9000"))

    def _run():
        try:
            app = get_dashboard_app()
            uvicorn.run(app, host="0.0.0.0", port=dash_port, log_level="warning")
        except Exception as e:
            sys.stderr.write(f"[metrics] dashboard упал: {e}\n")

    threading.Thread(target=_run, daemon=True, name="metrics-dashboard").start()
    print(f"[metrics] dashboard: http://0.0.0.0:{dash_port}")


def main():
    _check_docker_environment()

    known = SERVERS

    if len(sys.argv) < 2 or sys.argv[1] not in known:
        print(f"Usage: python start.py <{'|'.join(known.keys())}>")
        for name, (module, port) in known.items():
            print(f"  {name:16s} -> port {port}")
        sys.exit(1)

    name = sys.argv[1]
    module, default_port = known[name]
    port = int(os.environ.get("MCP_PORT", default_port))

    print(f"Starting {name} on port {port}...", flush=True)
    mod = __import__(module)

    for init_func in ("_load_all", "_load_builtin_reference", "_load_templates", "_load_builtin"):
        if hasattr(mod, init_func):
            getattr(mod, init_func)()

    mcp_obj = mod.mcp

    # TOOL-1 / TOOL-2 / SEC-5: приводим набор инструментов к целевому
    # ДО обёртки метриками, иначе обернём то, что сейчас удалим.
    #
    # API-1. Здесь жила вторая половина того же дефекта, что и в самом
    # фильтре: `except Exception` вокруг вызова означал, что даже падение
    # apply_profile не мешало серверу подняться — с metadata_reload и
    # metadata_cypher в наборе. Ловить исключение, чтобы записать строчку в
    # stderr, и продолжать — это и есть fail-open, только вежливый.
    #
    # Теперь исходы разделены. Отказ фильтра (реестр не найден, опасное
    # осталось в наборе) — EX_CONFIG, как пустой MCP_SHARED_SECRET. Всё
    # остальное (сам модуль не доехал в образ, ошибка импорта) — тоже отказ:
    # молча работать без фильтра нельзя ни по какой причине.
    from mcp_tool_filter import apply_profile, ToolRegistryUnavailable
    try:
        apply_profile(mcp_obj, name)
    except ToolRegistryUnavailable as e:
        sys.stderr.write(f"[FATAL] [tool-filter] {e}\n")
        raise SystemExit(78)  # EX_CONFIG, тот же код, что у SEC-3

    _wrap_tools_with_metrics(mcp_obj, name)
    _wrap_tools_with_usage(mcp_obj, name)
    _start_metrics_dashboard_async()

    # TR-1: Streamable HTTP вместо SSE. Аутентификация (SEC-3/TR-3) и
    # DNS rebinding protection (TR-4) настраиваются внутри mcp_http.
    from mcp_http import run as run_http

    run_http(mcp_obj, server_name=name, port=port)


if __name__ == "__main__":
    main()
