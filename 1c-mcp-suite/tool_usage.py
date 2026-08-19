"""
TOOL-1. Кого из инструментов не зовёт никто.
=============================================

Зачем
─────
Инструментов в наборе тридцать шесть. Каждый — строка в списке, который
модель читает перед каждым решением, и каждая неиспользуемая строка мешает
остальным: выбор становится длиннее, а описания начинают конкурировать
между собой. Заход 4 завёл эту задачу и не сделал; с тех пор список рос.

Чтобы убрать лишнее, надо сначала узнать, что лишнее. Пока счётчика нет,
разговор про «этот инструмент не нужен» держится на ощущении — ровно то,
против чего затевался Заход 7.

Что здесь есть и чего нет
─────────────────────────
Есть: счётчик вызовов **в памяти процесса**, по имени инструмента, плюс
время последнего вызова и число отказов. Отдаётся в `*_stats`.

Нет: хранения между перезапусками. Транспорт stateless, стейт держать
негде, а заводить ради счётчика базу — это второй источник правды рядом с
`mcp_metrics.py`, который уже пишет SQLite. Здесь другое назначение:
`mcp_metrics` копит историю для дашборда, а этот счётчик отвечает на
вопрос «за время жизни контейнера кто-нибудь звал этот инструмент?».
Недели работы хватает, чтобы увидеть нули.

Почему не список имён руками
────────────────────────────
Обёртка ставится на всё, что зарегистрировано, — одним проходом в
`start.py`, рядом с обёрткой метриками. Список инструментов, который надо
пополнять руками, разошёлся бы с действительностью: в этом проекте так
было четырежды с `COPY` в Dockerfile и один раз с генераторами лок-файлов.

Зависимостей нет, кроме стандартной библиотеки.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["count_call", "note_error", "reset_usage", "tool_names",
           "usage_snapshot", "wrap_registered_tools"]

_lock = threading.Lock()
_calls: dict[str, int] = {}
_errors: dict[str, int] = {}
_last: dict[str, float] = {}
_started = time.time()


def count_call(name: str) -> None:
    """Отметить вызов инструмента. Дёшево и потокобезопасно."""
    with _lock:
        _calls[name] = _calls.get(name, 0) + 1
        _last[name] = time.time()


def note_error(name: str) -> None:
    """Отметить, что вызов кончился исключением."""
    with _lock:
        _errors[name] = _errors.get(name, 0) + 1


def reset_usage() -> None:
    """Обнулить счётчики. Нужно тестам, в работе не вызывается."""
    with _lock:
        _calls.clear()
        _errors.clear()
        _last.clear()


def usage_snapshot(registered: Any = None) -> dict:
    """
    Что звали и сколько раз — за время жизни процесса.

    `registered` — список имён всех инструментов сервера. Если он передан,
    в ответ попадает и `never_called`: главный вопрос этой задачи не «кого
    звали», а **кого не звали ни разу**, и без списка зарегистрированных
    ответить на него нельзя — незваный инструмент не оставляет следов.
    """
    with _lock:
        calls = dict(_calls)
        errors = dict(_errors)
        last = dict(_last)

    snapshot: dict[str, Any] = {
        "uptime_hours": round((time.time() - _started) / 3600.0, 2),
        "total_calls": sum(calls.values()),
        "by_tool": dict(sorted(calls.items(), key=lambda kv: -kv[1])),
        "note": ("счётчик живёт в памяти процесса и обнуляется при "
                 "перезапуске контейнера — он отвечает на вопрос «звали ли "
                 "инструмент за это время», а не ведёт историю"),
    }
    if errors:
        snapshot["errors_by_tool"] = dict(sorted(errors.items(),
                                                 key=lambda kv: -kv[1]))
    if last:
        newest = max(last.values())
        snapshot["last_call_iso"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(newest))
    if registered:
        never = sorted(n for n in registered if n not in calls)
        snapshot["never_called"] = never
        snapshot["registered"] = len(list(registered))
    return snapshot


def wrap_registered_tools(mcp: Any) -> int:
    """
    Оборачивает УЖЕ ЗАРЕГИСТРИРОВАННЫЕ инструменты. Возвращает их число.

    Почему после регистрации, а не на ней
    ─────────────────────────────────────
    Первая версия ставила обёртку на `mcp.tool`, как это делает
    `install_answerable_field`. Выглядело единообразно и не работало:
    FastMCP строит схему инструмента по самой функции, через
    `get_type_hints`, а тот разрешает аннотации в глобальном пространстве
    ТОЙ функции, которую ему дали. Обёртка живёт в этом модуле, здесь нет
    ни `Optional`, ни прочих имён из сервера, — и регистрация
    `code_procedures_operating_on` упала с «Optional is not defined».

    Поймано не тестом, а живым запуском сервера: в тестовом двойнике
    аннотации были простые. Урок ровно тот же, что у `FIX-14`, — обёртка
    молча роняет не вызов, а описание, и наружу это выглядит как «агент
    перестал звать инструмент».

    Здесь схема уже построена, и подмена `tool.fn` ничего не ломает. Тем же
    приёмом и в том же месте работает обёртка метриками
    (`start.py._wrap_tools_with_metrics`) — второй способ делать одно и то
    же не нужен.
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) or {}
    wrapped = 0
    for name, tool in tools.items():
        fn = getattr(tool, "fn", None)
        if fn is None or getattr(fn, "__usage_counted__", False):
            continue
        tool.fn = _counting(fn, name)
        wrapped += 1
    return wrapped


def _counting(fn: Callable, name: str) -> Callable:
    def inner(*args, **kwargs):
        # Считаем ДО вызова: инструмент, который сейчас работает, не должен
        # видеть себя в `never_called` — именно так и вышло с
        # `metadata_stats`, который сам и печатает этот список.
        count_call(name)
        try:
            return fn(*args, **kwargs)
        except Exception:
            # Упавший вызов — это тоже вызов: инструмент, который зовут и
            # который всегда падает, надо видеть, а не принимать за
            # незваный.
            note_error(name)
            raise

    inner.__name__ = getattr(fn, "__name__", name)
    inner.__doc__ = fn.__doc__
    inner.__wrapped__ = fn
    inner.__usage_counted__ = True
    return inner


def tool_names(mcp: Any) -> list[str]:
    """
    Имена зарегистрированных инструментов — как их видит сам FastMCP.

    Читается из менеджера инструментов, а не из своего списка: своё
    перечисление разошлось бы при первом же добавлении, и `never_called`
    начал бы врать в самую опасную сторону — показывать инструмент
    незваным потому, что о нём не знает счётчик.
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) or {}
    try:
        return sorted(tools.keys())
    except Exception:  # pragma: no cover
        return []
