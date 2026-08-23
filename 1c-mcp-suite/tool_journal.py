"""
EVAL-7. Журнал вызовов: что агент позвал и сколько раз.
========================================================

Зачем
─────
Половина критериев в `evals/manual-prompts.md` — «обязано быть вызвано».
Прогон 22 августа прошёл без журнала, и треть отметок оказалась
косвенной: судили по тексту ответа. Так неразличимы ровно те два случая,
ради которых проверка заводилась, — «один вызов `explain_diagnostics` со
всем списком кодов» и «пять вызовов подряд». Оба дают одинаковый ответ на
экране.

Почему не хватило того, что уже было
─────────────────────────────────────
В проекте есть два счётчика, и ни один не отвечает на этот вопрос.

`tool_usage.py` (TOOL-1) считает вызовы за время жизни процесса, без
времени каждого. Пять вызовов внутри сценария от пяти вызовов за день не
отличить, а нарезать по сценариям нечем.

`mcp_metrics.py` пишет SQLite с временем каждого вызова — и этого хватило
бы, если бы серверов было четыре. Но пятый, `v8std`, чужой: он не
импортирует наших модулей и в общую базу не пишет. А главная непроверяемая
отметка — как раз про него.

Что оказалось на месте
──────────────────────
Чужой сервер умеет вести журнал сам: `McpToolUsageLogger` и ключ
`--usage-log` в `v8std_mcp_server.py` пишут JSONL со временем и именем
инструмента. Наш `v8std_entrypoint.py` этот ключ просто не передавал.

Отсюда решение: **один формат, один каталог, две стороны пишут**. Наши
серверы пишут этим модулем, чужой — своим ключом, читает обе половины
`scripts/journal_report.py`. Формат строки повторяет чужой (`ts`, `tool`),
чтобы читателю не пришлось знать, кто её написал.

Чего здесь нет
──────────────
Аргументов вызова. Соблазн большой — «а с каким запросом звали?» — но в
аргументы едет код пользователя, а журнал лежит файлом на диске и попадает
в архив прогона. Нужен текст запроса — он есть в ответе агента, рядом, в
том же чате.

Выключен по умолчанию: `MCP_TOOL_JOURNAL` не задан — модуль не делает
ничего. Журнал нужен на время ручного прогона, а не всегда.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = ["journal_path", "record", "install", "line_for", "note_start"]

_lock = threading.Lock()


def journal_path() -> str:
    """Куда писать. Пустая строка — журнал выключен."""
    return (os.environ.get("MCP_TOOL_JOURNAL") or "").strip()


def _iso(ts: float) -> str:
    """UTC с точностью до секунды — как пишет чужой сервер."""
    return (datetime.fromtimestamp(ts, timezone.utc)
            .replace(microsecond=0).isoformat())


def line_for(server: str, tool: str, ts: float,
             ms: int | None = None, ok: bool = True) -> str:
    """
    Одна строка журнала. Вынесена отдельно, чтобы формат проверялся
    тестом без файловой системы и без запуска сервера.
    """
    payload: dict[str, Any] = {"ts": _iso(ts), "tool": tool, "server": server}
    if ms is not None:
        payload["ms"] = ms
    if not ok:
        payload["ok"] = False
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def note_start(server: str, tools: int) -> bool:
    """
    Отметить в журнале, что сервер поднялся и журнал ведёт.

    Появилось после первого же применения (прогон 23 августа). До первого
    вызова инструмента журнала не существовало как файла, и отчёт печатал
    «Вызовов в журнале нет» с тремя шагами починки — хотя чинить было
    нечего, агента просто ещё не спрашивали.

    Норма, притворившаяся отказом, — то же самое, с чем борются `FIX-3` и
    `OBS-1`, только вывернутое наизнанку: там отказ выглядел успехом.
    Строка о старте делает состояние «журнал включён, вызовов пока нет»
    наблюдаемым, а не выводимым из отсутствия файла.
    """
    path = journal_path()
    if not path:
        return False
    line = json.dumps({"ts": _iso(time.time()), "event": "start",
                       "server": server, "tools": tools},
                      ensure_ascii=False, sort_keys=True)
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except OSError:
        return False


def record(server: str, tool: str, ts: float | None = None,
           ms: int | None = None, ok: bool = True) -> bool:
    """
    Дописать строку. Возвращает True, если записали.

    Никогда не бросает: журнал — это диагностика, и он не имеет права
    отменить ответ инструмента. Урок `FAIL-2` в чистом виде: там печать
    диагностики роняла вызов, к которому не имела отношения.
    """
    path = journal_path()
    if not path:
        return False
    line = line_for(server, tool, time.time() if ts is None else ts, ms, ok)
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except OSError:
        return False


def _journaling(fn: Callable, server: str, name: str) -> Callable:
    def inner(*args, **kwargs):
        t0 = time.monotonic()
        ok = True
        try:
            return fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            # Пишем ПОСЛЕ вызова, в отличие от счётчика TOOL-1: там важно
            # было, чтобы инструмент не видел себя в `never_called`, здесь
            # важна длительность. Упавший вызов записывается тоже — иначе
            # «инструмент не звали» и «звали, и он упал» снова сольются.
            record(server, name, ms=int((time.monotonic() - t0) * 1000), ok=ok)

    inner.__name__ = getattr(fn, "__name__", name)
    inner.__doc__ = fn.__doc__
    inner.__wrapped__ = fn
    inner.__journaled__ = True
    return inner


def install(mcp: Any, server_name: str) -> int:
    """
    Обернуть УЖЕ ЗАРЕГИСТРИРОВАННЫЕ инструменты. Возвращает их число.

    Ноль означает «журнал выключен», и это не ошибка.

    Про «уже зарегистрированные» — тот же урок, что у `tool_usage`:
    обёртка на `mcp.tool` ломает разрешение аннотаций, потому что FastMCP
    строит схему по самой функции и разрешает имена в её модуле. Здесь
    схема уже построена, подмена `tool.fn` безопасна.
    """
    if not journal_path():
        return 0
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) or {}
    wrapped = 0
    for name, tool in tools.items():
        fn = getattr(tool, "fn", None)
        if fn is None or getattr(fn, "__journaled__", False):
            continue
        tool.fn = _journaling(fn, server_name, name)
        wrapped += 1
    note_start(server_name, wrapped)
    return wrapped
