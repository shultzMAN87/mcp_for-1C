"""
OBS-1. Единый словарь отказа для серверов набора.
=================================================

Откуда это взялось
──────────────────
Формат придумывать не пришлось — он в проекте уже был. `FIX-3` завёл в
`graph_state.py` ответ с полем `answerable: false` и явной формулировкой
«это НЕ значит, что объекта нет в конфигурации». Причина была та же, что и
у всего этого захода: агент, получив «Neo4j недоступна» на
`metadata_search`, уверенно сообщал пользователю, что такого объекта нет, —
хотя про конфигурацию не было известно ничего.

Формат хороший, и пользовались им два сервера из пяти. Остальные сообщали
об отказе как придётся:

  • `bsl-checker` — полем `error`, причём одинаково для «файла нет» (ответ
    дан) и «BSL Language Server не найден» (ответа нет). Различить их
    вызывающий не может, а жёсткое правило 5 в `.cursor/rules/mcp-tools.mdc`
    требует от модели именно этого различения;
  • `platform-help` — полем `degraded` (заход `A-3`);
  • `metadata` — полем `found` для «не нашлось» и исключением для «не смог».

Это те же «два списка, которые обязаны совпадать» из `HYG-2`, только
списков четыре. Разойдутся — и разошлись.

Два поля, два разных вопроса
────────────────────────────
Их легко перепутать, поэтому здесь они разведены жёстко.

**`answerable`** — можно ли опереться на ответ. Присутствует ВСЕГДА, в
любом ответе любого инструмента. `false` означает: инструмент не смог
ответить, про предмет вопроса ничего не известно, домысливать и отвечать
по памяти нельзя.

**`degraded`** — ответ пригоден, но получен хуже штатного. Гибрид
свалился в плотный поиск; часть счётчиков не досчиталась. Это не отказ:
результатом пользоваться можно, просто он беднее обычного.

Их сочетания осмысленны все четыре:

  answerable=true,  degraded=false — норма;
  answerable=true,  degraded=true  — ответ хуже обычного, но годен;
  answerable=false, degraded=true  — не смог, и до этого работал плохо;
  answerable=false, degraded=false — не смог сразу (файла нет, граф пуст).

Чего здесь нарочно НЕТ
──────────────────────
Поля `found` и `error` не трогаются. `found: false` — это ответ («такого
объекта нет»), а не отказ, и путать их нельзя: на `found` завязаны девять
предикатов в датасетах и жёсткое правило 3 в правилах Cursor. `answerable`
отвечает на другой вопрос и живёт рядом, а не вместо.

Симметрия обязательна
─────────────────────
`answerable` ставится и на успехе тоже. Урок `FIX-19`: поле, по которому
отличают норму от отказа, обязано быть в обеих ветках — иначе проверка на
него получает `null` там, где всё хорошо, и отличить «поле отсутствует,
потому что всё хорошо» от «поле отсутствует, потому что сервер старой
версии» невозможно.

Зависимостей нет, кроме стандартной библиотеки: модуль кладётся в четыре
образа и проверяется тестами без единого пакета.
"""

from __future__ import annotations

import functools
import json
import threading
from typing import Any, Callable, Optional

__all__ = [
    "ANSWERABLE", "DEGRADED",
    "refusal", "mark", "install_answerable_field",
    "begin_call", "note_degraded", "is_degraded", "degradation_reasons",
    "MEANING_TOOL_DOWN", "MEANING_NOT_ASKED",
]

ANSWERABLE = "answerable"
DEGRADED = "degraded"


# ─── Типовые формулировки ────────────────────────────────────────────────
#
# `meaning` — не украшение. Именно этот текст читает модель, и именно он
# останавливает её от вывода «значит, такого объекта нет». Формулировка
# взята из graph_state.py, где она уже доказала пользу.

MEANING_TOOL_DOWN = (
    "Это НЕ значит, что искомого нет. Инструмент не смог ответить, "
    "поэтому про предмет вопроса сейчас не известно ничего — не делай "
    "вывод о его наличии или отсутствии и не отвечай по памяти. "
    "Сообщи пользователю, что инструмент недоступен."
)

MEANING_NOT_ASKED = (
    "Вопрос до данных не дошёл: запрос отклонён до обращения к источнику. "
    "Про предмет вопроса ничего не известно."
)


def refusal(
    error: str,
    message: str,
    meaning: str = MEANING_TOOL_DOWN,
    hint: str = "",
    degraded: bool = True,
    **extra: Any,
) -> dict:
    """
    Готовый ответ-отказ: инструмент не смог ответить.

    `error`   — короткий код для машины: neo4j_unavailable, linter_missing…
    `message` — что произошло, одной фразой для человека.
    `meaning` — что из этого НЕ следует. Главное поле; см. модульную шапку.
    `hint`    — что сделать, чтобы починить.

    `degraded` по умолчанию True: инструмент, который не смог ответить,
    работает хуже штатного по определению. Явный False уместен там, где
    отказ — штатный исход, а не поломка (запрос отклонён по форме).
    """
    payload = {
        "error": error,
        ANSWERABLE: False,
        DEGRADED: bool(degraded),
        "message": message,
        "meaning": meaning,
    }
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


# ─── Состояние одного вызова ─────────────────────────────────────────────
#
# FastMCP исполняет синхронные инструменты в рабочем потоке на запрос,
# поэтому состояние привязано к потоку. Глобальная переменная здесь дала бы
# протечку между параллельными вызовами: пометка деградации от одного
# запроса всплыла бы в ответе другого.

_local = threading.local()


def begin_call() -> None:
    """Начало вызова инструмента: сбрасываем пометки прошлого."""
    _local.degraded = False
    _local.reasons = []


def note_degraded(reason: str) -> None:
    """
    Пометить текущий ответ как полученный хуже штатного.

    Зовётся оттуда, где отказ проглатывается ради частичного результата:
    счётчик не досчитался, ветка поиска не отработала. Раньше такие места
    молчали — ответ выглядел полноценным.
    """
    _local.degraded = True
    reasons = getattr(_local, "reasons", None)
    if reasons is None:
        reasons = _local.reasons = []
    if reason and reason not in reasons:
        reasons.append(reason)


def is_degraded() -> bool:
    return bool(getattr(_local, "degraded", False))


def degradation_reasons() -> list[str]:
    return list(getattr(_local, "reasons", []) or [])


# ─── Проставление полей ──────────────────────────────────────────────────


def mark(payload: dict) -> dict:
    """
    Дописывает `answerable` и `degraded`, не трогая уже проставленные.

    Явное значение всегда сильнее: инструмент лучше знает про свой ответ,
    чем обёртка. Обёртка только гарантирует, что поле есть.
    """
    if not isinstance(payload, dict):
        return payload
    payload.setdefault(ANSWERABLE, True)
    if DEGRADED not in payload:
        payload[DEGRADED] = is_degraded()
    if is_degraded() and not payload.get("degradation_reasons"):
        reasons = degradation_reasons()
        if reasons:
            payload["degradation_reasons"] = reasons
    return payload


def _mark_json_text(text: str) -> str:
    """
    Проставляет поля в уже сериализованном ответе.

    Инструменты возвращают строку JSON, а не словарь, поэтому приходится
    разбирать обратно. Разбор нарочно осторожный: не JSON, не объект,
    список верхнего уровня — возвращаем как есть. Обёртка, которая может
    испортить нормальный ответ, хуже отсутствующей.
    """
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return text
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if not isinstance(payload, dict):
        return text
    before = (payload.get(ANSWERABLE), payload.get(DEGRADED))
    mark(payload)
    if (payload.get(ANSWERABLE), payload.get(DEGRADED)) == before:
        # Ничего не изменилось — отдаём исходную строку, чтобы не терять
        # авторское форматирование (отступы, порядок ключей).
        return text
    indent = 2 if "\n" in text else None
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def wrap_tool(fn: Callable) -> Callable:
    """Оборачивает функцию инструмента: сброс состояния + проставление полей."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        begin_call()
        out = fn(*args, **kwargs)
        if isinstance(out, str):
            return _mark_json_text(out)
        if isinstance(out, dict):
            return mark(out)
        return out

    return inner


def install_answerable_field(mcp: Any) -> Any:
    """
    Ставит обёртку на регистрацию инструментов сервера.

    Одна строка на сервер вместо правки каждого `return json.dumps(...)`.
    Так поле появляется и у инструментов, которых ещё нет: список, который
    надо пополнять руками, рано или поздно разойдётся с действительностью —
    в этом проекте так было трижды с `COPY` в Dockerfile и один раз с
    генераторами лок-файлов.

    Возвращает переданный объект, чтобы вызов читался одной строкой.
    """
    original_tool = mcp.tool

    def tool(*args, **kwargs):
        decorator = original_tool(*args, **kwargs)

        def register(fn):
            return decorator(wrap_tool(fn))

        return register

    mcp.tool = tool
    return mcp
