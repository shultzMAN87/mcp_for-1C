"""
Тонкая обёртка над `mcp.client.streamable_http.streamablehttp_client`
+ `ClientSession` (TR-1: SSE-транспорта больше нет).

Одна сессия на прогон всего датасета — иначе между примерами
platform-help повторно инициализирует dense/sparse модели, и это
10–20 секунд на пустом месте. `ClientSession` в пределах `async with`
используется для всех call_tool подряд.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@dataclass
class ToolCallResult:
    ok: bool
    parsed: dict | None
    raw_text: str | None
    error: str | None
    duration_ms: float
    is_error_flag: bool


def _build_headers() -> dict[str, str]:
    """
    Аналог mcp_auth.build_client_headers(), без зависимости от него —
    чтобы eval-runner был самодостаточным контейнером.
    """
    secret = os.environ.get("MCP_SHARED_SECRET", "").strip()
    if not secret:
        return {}
    return {"Authorization": f"Bearer {secret}"}


class MCPSession:
    """
    Контекстный менеджер: держит одно соединение и шлёт по нему call_tool.
    """

    def __init__(self, url: str, init_timeout: float = 120.0, call_timeout: float = 60.0):
        self._url = url
        self._init_timeout = init_timeout
        self._call_timeout = call_timeout
        self._session: ClientSession | None = None
        self._transport_cm = None
        self._session_cm = None
        self._streams = None

    async def __aenter__(self) -> "MCPSession":
        headers = _build_headers()
        self._transport_cm = streamablehttp_client(self._url, headers=headers)
        self._streams = await self._transport_cm.__aenter__()
        # Третий элемент — колбэк получения session id; в stateless-режиме
        # серверов он не нужен.
        read_stream, write_stream, _get_session_id = self._streams
        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await asyncio.wait_for(self._session.initialize(), timeout=self._init_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(exc_type, exc, tb)
        except Exception:
            pass
        try:
            if self._transport_cm is not None:
                await self._transport_cm.__aexit__(exc_type, exc, tb)
        except Exception:
            pass
        self._session = None
        self._session_cm = None
        self._transport_cm = None
        self._streams = None
        return False

    async def call_tool(self, name: str, arguments: dict) -> ToolCallResult:
        if self._session is None:
            return ToolCallResult(
                ok=False, parsed=None, raw_text=None,
                error="session not initialized",
                duration_ms=0.0, is_error_flag=False,
            )

        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments or {}),
                timeout=self._call_timeout,
            )
        except asyncio.TimeoutError:
            dt = (time.perf_counter() - t0) * 1000
            return ToolCallResult(
                ok=False, parsed=None, raw_text=None,
                error=f"timeout after {self._call_timeout}s",
                duration_ms=dt, is_error_flag=False,
            )
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000
            return ToolCallResult(
                ok=False, parsed=None, raw_text=None,
                error=f"{type(e).__name__}: {e}",
                duration_ms=dt, is_error_flag=False,
            )

        dt = (time.perf_counter() - t0) * 1000
        is_error = bool(getattr(result, "isError", False))

        raw_text = None
        for block in getattr(result, "content", []) or []:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                raw_text = t
                break

        if raw_text is None:
            return ToolCallResult(
                ok=True, parsed=None, raw_text=None,
                error="no text block in result.content",
                duration_ms=dt, is_error_flag=is_error,
            )

        try:
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                parsed = {"_non_dict_response": parsed}
        except json.JSONDecodeError as e:
            return ToolCallResult(
                ok=True, parsed=None, raw_text=raw_text,
                error=f"json decode: {e}",
                duration_ms=dt, is_error_flag=is_error,
            )

        return ToolCallResult(
            ok=True, parsed=parsed, raw_text=raw_text,
            error=None, duration_ms=dt, is_error_flag=is_error,
        )
