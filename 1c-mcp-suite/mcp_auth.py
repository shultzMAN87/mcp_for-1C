"""
Shared-secret аутентификация для MCP SSE-серверов (задача 3.2).

Предыстория: FastMCP по умолчанию поднимает SSE-приложение без какой-либо
аутентификации — любой процесс в docker-network может вызвать любой tool
(`sonar_scan_code`, `http_service_call`, `platform_help_*` и т.д.).
Для dev-кластера этого хватало, но после индексации всей справки платформы
(4.7) и появления tools, потенциально ходящих в живую 1С, это стало
реальной дырой.

Решение: простой pre-shared secret через HTTP-заголовок. Ни OAuth, ни JWT —
overkill для внутренней docker-network. Одного секрета, загружаемого из
env `MCP_SHARED_SECRET`, достаточно, чтобы отсечь всё, кроме явно
сконфигурированных клиентов.

Режимы (изменено в SEC-3, fail-closed):
- env не задан          → сервер НЕ стартует. Раньше middleware просто не
                          навешивалась, а порт при этом публиковался на
                          0.0.0.0 — то есть открытый доступ ко всему стеку
                          с warning'ом в логе, который никто не читает.
- env задан (непустой)  → middleware активна, /mcp требует совпадающий
                          заголовок, иначе 401.

Протокол:
- Заголовок `Authorization: Bearer <secret>` (основной; совпадает с тем,
  как opencode ожидает видеть его в `mcp-config.json → headers`).
- Заголовок `X-MCP-Secret: <secret>` (альтернативный; удобен для curl и
  простых скриптов, где Bearer вводит в заблуждение про OAuth).
- Любой из двух подходит; приоритет у `Authorization`, если заданы оба.

Что middleware пропускает без проверки:
- `GET /` — FastMCP иногда туда кладёт health-пинг, и keepalive от
  docker-compose должен работать без секрета.
- `GET /health`, `GET /healthz` — для docker healthcheck.
- `OPTIONS *` — CORS preflight, в заголовках секрета быть не может
  по определению.

Что проверяется: всё остальное, в первую очередь `/mcp`.
TR-3: middleware никогда не была привязана к конкретному пути — она
проверяет ВСЁ, кроме PUBLIC_PATHS. Поэтому переход с /sse + /messages/*
на /mcp её не ломает; ловушка была не здесь, а в fail-open выше.

Клиентская часть: `build_client_headers()` отдаёт dict пригодный для
прямой передачи в `streamablehttp_client(url, headers=...)`.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from typing import Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "MCP_SHARED_SECRET"
HEADER_AUTHORIZATION = "authorization"  # Starlette lowercases headers
HEADER_X_SECRET = "x-mcp-secret"
BEARER_PREFIX = "bearer "

# Пути, которые middleware НЕ проверяет. Всё остальное — под защитой,
# включая /mcp (единственный эндпоинт после TR-1).
PUBLIC_PATHS = frozenset({"/", "/health", "/healthz"})


# ─── Серверная сторона ───────────────────────────────────────────────────


class SharedSecretMiddleware:
    """
    ASGI-middleware: проверяет заголовок с общим секретом на каждом
    HTTP-запросе к MCP-серверу. Работает поверх SSE-приложения FastMCP
    (которое внутри Starlette).

    Параметры:
        app:       обёртываемое ASGI-приложение (из `mcp_http.build_app`).
        secret:    pre-shared secret. Пустая строка/None = ValueError:
                   решение о fail-closed принимается в `wrap_app`.
        server_name: имя сервера для логов — чтобы в объединённом stdout
                   было видно, кто вернул 401.
    """

    def __init__(self, app: ASGIApp, secret: str, server_name: str = "mcp") -> None:
        if not secret:
            # Защита от случайного прямого инстанцирования без секрета —
            # публичный API ходит через wrap_app(), и тот принимает
            # такое решение централизованно.
            raise ValueError(
                "SharedSecretMiddleware: secret обязателен. "
                "Если секрет не задан, просто не оборачивайте приложение."
            )
        self.app = app
        self.secret = secret
        self.server_name = server_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # WebSocket и lifespan — пропускаем. FastMCP SSE-транспорт
            # чистый HTTP + Server-Sent Events, WS не использует.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")

        if method == "OPTIONS" or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        provided = _extract_secret(scope)
        if provided is None:
            await self._deny(send, reason="missing_credentials", path=path)
            return

        # hmac.compare_digest — защита от timing-side-channel. На длинах
        # 32-64 байта разница смешная, но привычка полезная и бесплатная.
        if not hmac.compare_digest(provided, self.secret):
            await self._deny(send, reason="invalid_credentials", path=path)
            return

        await self.app(scope, receive, send)

    async def _deny(self, send: Send, *, reason: str, path: str) -> None:
        # Короткий лог в stderr — не раскрываем ни секрет, ни IP
        # (Docker-network, всё равно только внутренние адреса).
        sys.stderr.write(
            f"[mcp-auth] {self.server_name}: 401 {reason} on {path}\n"
        )
        response = JSONResponse(
            {"error": "unauthorized", "reason": reason},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
        )
        await response(
            {"type": "http", "method": "GET", "path": path, "headers": []},
            _empty_receive,
            send,
        )


async def _empty_receive() -> dict:
    # Stub для JSONResponse: он не читает body, но ASGI требует receive.
    return {"type": "http.disconnect"}


def _extract_secret(scope: Scope) -> str | None:
    """
    Достаёт секрет из заголовков ASGI scope. Возвращает None, если
    заголовка нет или формат невалидный.

    ASGI headers — список кортежей (name_bytes, value_bytes), имя всегда
    в нижнем регистре (по спеке).
    """
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
    auth_value: bytes | None = None
    x_secret_value: bytes | None = None

    for name, value in headers:
        if name == HEADER_AUTHORIZATION.encode():
            auth_value = value
        elif name == HEADER_X_SECRET.encode():
            x_secret_value = value

    if auth_value is not None:
        try:
            decoded = auth_value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        # Bearer-префикс case-insensitive по RFC 6750.
        if decoded.lower().startswith(BEARER_PREFIX):
            return decoded[len(BEARER_PREFIX):].strip() or None
        # Не Bearer — возможно, пользователь положил голый секрет.
        # Не поддерживаем это молча: атакующему проще угадать схему.
        return None

    if x_secret_value is not None:
        try:
            return x_secret_value.decode("latin-1").strip() or None
        except UnicodeDecodeError:
            return None

    return None


class MissingSecretError(RuntimeError):
    """MCP_SHARED_SECRET не задан. Стартовать в таком виде нельзя (SEC-3)."""


def require_secret() -> str:
    """Читает секрет из окружения. Пусто -> MissingSecretError."""
    secret = os.environ.get(ENV_VAR, "").strip()
    if not secret:
        raise MissingSecretError(
            f"{ENV_VAR} не задан или пуст — сервер не стартует (fail-closed, SEC-3).\n"
            f"Сгенерируйте секрет и положите его в .env:\n"
            f"  Linux/macOS: openssl rand -hex 32\n"
            f"  Windows:     python -c \"import secrets;print(secrets.token_hex(32))\"\n"
            f"Один и тот же секрет нужен всем серверам и клиенту (.cursor/mcp.json)."
        )
    return secret


def wrap_app(app: ASGIApp, server_name: str = "mcp") -> ASGIApp:
    """
    Основная публичная функция для серверной стороны.
    Вызывается из mcp_http.build_app().

    В отличие от прежнего поведения, пустой секрет — не warning, а отказ
    старта: приложение без аутентификации на 0.0.0.0 хуже, чем упавший
    контейнер, потому что выглядит рабочим.
    """
    secret = require_secret()

    # Маскируем секрет в логе — достаточно показать длину, чтобы человек
    # мог сверить, что подгрузилось «что-то» правильной длины.
    sys.stderr.write(
        f"[mcp-auth] {server_name}: auth ENABLED (secret length={len(secret)})\n"
    )
    return SharedSecretMiddleware(app, secret=secret, server_name=server_name)


def wrap_sse_app(app: ASGIApp, server_name: str = "mcp") -> ASGIApp:
    """DEPRECATED. Оставлено на случай внешних вызовов; SSE больше нет."""
    return wrap_app(app, server_name=server_name)


# ─── Клиентская сторона ──────────────────────────────────────────────────


def build_client_headers() -> dict[str, str]:
    """
    Хелпер для кода, который открывает соединения к нашим же
    MCP-серверам (оркестратор, watcher, code_reindex_trigger).

    Возвращает:
        {"Authorization": "Bearer <secret>"} если env задан,
        пустой dict иначе. В обоих случаях можно без условий
        передавать в `streamablehttp_client(url, headers=...)`.
    """
    secret = os.environ.get(ENV_VAR, "").strip()
    if not secret:
        return {}
    return {"Authorization": f"Bearer {secret}"}


# ─── CLI-самотест ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Мини-тест: запустите `MCP_SHARED_SECRET=test python mcp_auth.py`
    # чтобы убедиться, что модуль импортируется и логика экстракции
    # работает. Не заменяет smoke_auth.py, но полезно в отладке.
    tests = [
        ([(b"authorization", b"Bearer abc")], "abc"),
        ([(b"authorization", b"bearer abc")], "abc"),  # lowercase ok
        ([(b"authorization", b"Basic abc")], None),
        ([(b"x-mcp-secret", b"xyz")], "xyz"),
        ([(b"authorization", b"Bearer xxx"), (b"x-mcp-secret", b"yyy")], "xxx"),
        ([], None),
        ([(b"authorization", b"Bearer   padded  ")], "padded"),
    ]
    failed = 0
    for headers, expected in tests:
        scope = {"type": "http", "headers": headers}
        got = _extract_secret(scope)
        status = "✓" if got == expected else "✗"
        if got != expected:
            failed += 1
        print(f"{status} headers={headers} expected={expected!r} got={got!r}")
    print(
        json.dumps(
            {"passed": len(tests) - failed, "failed": failed, "total": len(tests)},
            indent=2,
        )
    )
    sys.exit(1 if failed else 0)
