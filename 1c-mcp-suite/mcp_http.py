"""
Общая точка входа Streamable HTTP для всех MCP-серверов набора (задача TR-1).

Заменяет `mcp.sse_app()`. SSE-эндпоинтов (`/sse` + `/messages/`) больше нет:
транспорт один — Streamable HTTP, один эндпоинт `/mcp` на сервер,
режим stateless (контейнер перезапускается — клиент просто переподключается).

Закрывает:
  TR-1  — миграция транспорта;
  TR-3  — auth-middleware навешивается на актуальный путь, а не на исчезнувший;
  TR-4  — DNS rebinding protection включена осознанно, со списком хостов;
  SEC-3 — без MCP_SHARED_SECRET сервер не стартует (fail-closed в mcp_auth).

Используется из `start.py` (серверы на Dockerfile.python) и напрямую из
`mcp-bsl-checker/server.py` (у него свой образ Dockerfile.bsl).

ВАЖНО про путь. `mcp.streamable_http_app()` уже отдаёт приложение, у которого
эндпоинт лежит на `settings.streamable_http_path` (по умолчанию `/mcp`).
Монтировать его ещё раз в свой Starlette по пути «/mcp» нельзя — получится
`/mcp/mcp`. Поэтому приложение берётся как есть, а `/healthz` добавляется
маршрутом внутрь него.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable, Sequence

from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"


# ─── TR-4: защита от DNS rebinding ───────────────────────────────────────

def _configure_transport_security(
    mcp_obj,
    *,
    server_name: str,
    port: int,
    extra_hosts: Sequence[str] = (),
) -> None:
    """
    Раньше во всех серверах стояло `enable_dns_rebinding_protection = False`
    без объяснения. Теперь защита включена по умолчанию и настроена явно.

    Список разрешённых Host собирается из реальных способов обращения:
      - `127.0.0.1:<port>` / `localhost:<port>` — Cursor и MCP Inspector
        ходят через проброшенный на loopback порт (см. SEC-4);
      - `<имя_сервиса>:<port>` — обращения внутри docker-сети
        (workspace-watcher, code_reindex_trigger, eval-runner);
      - всё, что добавлено через MCP_ALLOWED_HOSTS (через запятую).

    Выключается одной переменной: MCP_DNS_REBINDING_PROTECTION=0.
    Это на случай непредвиденного клиента, который шлёт другой Host;
    симптом — 400/421 при подключении.
    """
    enabled = os.environ.get("MCP_DNS_REBINDING_PROTECTION", "1").strip().lower() \
        not in ("0", "false", "no", "off")

    settings = mcp_obj.settings
    if getattr(settings, "transport_security", None) is None:
        try:
            from mcp.server.transport_security import TransportSecuritySettings
        except ImportError:
            sys.stderr.write(
                "[mcp-http] TransportSecuritySettings недоступен в этой версии SDK; "
                "проверьте пин версий (TR-2)\n"
            )
            return
        settings.transport_security = TransportSecuritySettings()

    ts = settings.transport_security
    ts.enable_dns_rebinding_protection = enabled

    if not enabled:
        sys.stderr.write(
            f"[mcp-http] {server_name}: DNS rebinding protection ВЫКЛЮЧЕНА "
            f"(MCP_DNS_REBINDING_PROTECTION=0)\n"
        )
        return

    hosts = [
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"{server_name}:{port}",
        f"mcp-{server_name}:{port}",
    ]
    hosts.extend(h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
    hosts.extend(extra_hosts)
    ts.allowed_hosts = sorted(set(hosts))
    ts.allowed_origins = sorted({
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    })


# ─── Сборка приложения ───────────────────────────────────────────────────

def build_app(
    mcp_obj,
    *,
    server_name: str,
    port: int,
    extra_hosts: Sequence[str] = (),
) -> ASGIApp:
    """
    Возвращает готовое ASGI-приложение: `/mcp` под аутентификацией,
    `/healthz` без неё (для docker healthcheck и быстрой проверки руками).
    """
    mcp_obj.settings.streamable_http_path = MCP_PATH
    mcp_obj.settings.stateless_http = True
    # json_response=True отдаёт обычный JSON вместо SSE-потока внутри
    # Streamable HTTP. По спецификации допустимы оба варианта; по умолчанию
    # оставляем потоковый (спецификационный) режим.
    mcp_obj.settings.json_response = os.environ.get(
        "MCP_JSON_RESPONSE", "0"
    ).strip().lower() in ("1", "true", "yes", "on")

    _configure_transport_security(
        mcp_obj, server_name=server_name, port=port, extra_hosts=extra_hosts
    )

    app = mcp_obj.streamable_http_app()

    async def healthz(_request):
        return PlainTextResponse("ok")

    app.router.routes.insert(0, Route(HEALTH_PATH, healthz, methods=["GET"]))

    # SEC-3 + TR-3: без секрета сервер не стартует; middleware висит на всём,
    # кроме PUBLIC_PATHS (туда входит /healthz).
    from mcp_auth import wrap_app
    app = wrap_app(app, server_name=server_name)

    return app


def run(
    mcp_obj,
    *,
    server_name: str,
    port: int,
    host: str = "0.0.0.0",
    extra_hosts: Sequence[str] = (),
    wrap_outer: Iterable = (),
) -> None:
    """
    Запуск сервера. `wrap_outer` — дополнительные ASGI-обёртки, применяются
    снаружи (используется для audit-middleware у rest-proxy).
    """
    import uvicorn
    from mcp_auth import MissingSecretError

    try:
        app = build_app(mcp_obj, server_name=server_name, port=port, extra_hosts=extra_hosts)
    except MissingSecretError as exc:
        sys.stderr.write(f"[FATAL] {exc}\n")
        raise SystemExit(78)  # EX_CONFIG

    for wrapper in wrap_outer:
        app = wrapper(app)

    print(
        f"[mcp-http] {server_name}: Streamable HTTP на "
        f"http://{host}:{port}{MCP_PATH} (stateless, auth on)",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port)
