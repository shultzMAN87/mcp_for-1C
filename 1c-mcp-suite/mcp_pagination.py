"""
Модуль пагинации для MCP-серверов 1С
======================================
Единый помощник для ограничения и пагинации результатов.

Зачем:
  - Метаданные 1С могут быть огромными (сотни справочников, тысячи реквизитов).
  - Без ограничений один вызов может вернуть мегабайты данных и переполнить
    контекст LLM.
  - Этот модуль даёт единообразный API: limit, offset, опциональные секции.

Использование:
    from mcp_pagination import paginate, PaginationParams

    def my_tool(full_name, limit=20, offset=0, include_modules=False):
        p = PaginationParams(limit=limit, offset=offset)
        all_items = fetch_from_db(...)
        return paginate(all_items, p, extra={"object": full_name})
"""

from dataclasses import dataclass, field
from typing import Any


# ─── Конфиг по умолчанию ─────────────────────────────────────────────────

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_PREVIEW = 5  # Сколько элементов показывать в «сводке»


# ─── B-4: один словарь постраничности на весь набор ──────────────────────
#
# Диагноз AUDIT-2: словарей было три с половиной. У metadata-graph —
# правильный, у bsl-checker — свой с теми же именами, у query-builder —
# `attributes_total` и больше ничего, у platform-help — только `limit`.
#
# Цена не в дубликате кода, а в отсутствии контракта: правила Cursor не
# могли сказать «видишь has_more: true — запроси следующую страницу»,
# потому что у двух серверов из четырёх такого поля нет вовсе.
#
# Отсюда контракт, обязательный для всех четырёх:
#
#   pagination   — "paged" или "none". Есть ВСЕГДА;
#   has_more     — есть всегда, у "none" всегда false;
#   next_offset  — есть всегда, null когда следующей страницы нет;
#   returned     — сколько отдано этим вызовом.
#
# `next_offset` присутствует и в отрицательной ветке нарочно. Урок FIX-19:
# поле, по которому отличают одно состояние от другого, обязано быть в
# обеих ветках, иначе проверка на него получает null там, где всё хорошо, и
# «поля нет, потому что страниц больше нет» неотличимо от «поля нет, потому
# что сервер старой версии».
#
# Отдельная ветка "none" — не отговорка. Она для выдачи, где страниц нет ПО
# СУЩЕСТВУ: семантический поиск ранжирует, и вторая страница по убыванию
# релевантности почти всегда мусор. Ответ говорит это прямо, вместо того
# чтобы предлагать листать и молча ухудшать результат. Правило при этом
# остаётся без исключений: has_more есть у всех, просто у справки он всегда
# false, а что делать вместо листания — сказано в поле рядом.

PAGINATION_PAGED = "paged"
PAGINATION_NONE = "none"


def page_fields(total: int, offset: int, limit: int, returned: int,
                alias: str = "") -> dict:
    """
    Канонический блок постраничности. Единственное место, где эти имена
    появляются на свет.

    `alias` — префикс для тех, кто листает вложенную коллекцию, а не свой
    главный список (`query_fields` листает реквизиты). Тогда рядом с
    каноническими полями кладутся `attributes_total`, `attributes_has_more`
    и `attributes_next_offset` — из того же расчёта, не из второй формулы.
    """
    total = max(0, int(total))
    offset = max(0, int(offset))
    returned = max(0, int(returned))
    end = offset + returned
    has_more = end < total
    out = {
        "pagination": PAGINATION_PAGED,
        "total": total,
        "returned": returned,
        "offset": offset,
        "limit": int(limit),
        "has_more": has_more,
        "next_offset": end if has_more else None,
    }
    if alias:
        out[f"{alias}_total"] = total
        out[f"{alias}_has_more"] = has_more
        out[f"{alias}_next_offset"] = end if has_more else None
        out["paginated_field"] = alias
    return out


def no_pagination(returned: int, limit: int = 0, reason: str = "",
                  instead: str = "") -> dict:
    """
    Блок для выдачи, у которой страниц нет по существу.

    `reason` — почему их нет; `instead` — что делать вместо листания. Оба
    попадают в ответ: сказать «страниц нет» и не сказать, что делать, —
    значит оставить агента ровно там, откуда он пришёл.
    """
    out = {
        "pagination": PAGINATION_NONE,
        "returned": max(0, int(returned)),
        "has_more": False,
        "next_offset": None,
    }
    if limit:
        out["limit"] = int(limit)
    if reason:
        out["pagination_reason"] = reason
    if instead:
        out["pagination_instead"] = instead
    return out


@dataclass
class PaginationParams:
    """Параметры пагинации с валидацией."""
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    max_limit: int = MAX_LIMIT

    def __post_init__(self):
        # Жёсткая валидация
        if self.limit <= 0:
            self.limit = DEFAULT_LIMIT
        if self.limit > self.max_limit:
            self.limit = self.max_limit
        if self.offset < 0:
            self.offset = 0


def paginate(items: list, params: PaginationParams, extra: dict | None = None) -> dict:
    """
    Разбивает список на страницы и формирует стандартный ответ.

    Параметры:
      items  — полный список (например, из Neo4j)
      params — параметры пагинации
      extra  — дополнительные поля в ответе (например, {"object": "Справочник.Х"})

    Возвращает:
      {
        "total": 150,
        "returned": 20,
        "offset": 0,
        "limit": 20,
        "has_more": True,
        "next_offset": 20,
        "items": [...],
        ...extra...
      }
    """
    page = items[params.offset:params.offset + params.limit]
    # B-4: блок собирается там же, где и у остальных серверов. Раньше здесь
    # была своя формула, и `next_offset` она клала только при наличии
    # следующей страницы — та самая асимметрия, которую чинил FIX-19.
    response = page_fields(len(items), params.offset, params.limit, len(page))
    response["items"] = page

    if extra:
        response.update(extra)

    return response


def summarize(items: list, preview: int = DEFAULT_PREVIEW) -> dict:
    """
    Возвращает краткую сводку вместо полных данных.
    Используется когда агенту достаточно «сколько и какие примерно».

    Возвращает:
      {
        "total": 150,
        "preview_count": 5,
        "preview": [...первые 5...],
        "hint": "Используйте limit/offset для получения полного списка"
      }
    """
    return {
        "total": len(items),
        "preview_count": min(preview, len(items)),
        "preview": items[:preview],
        "hint": (
            "Это сводка. Для полного списка вызовите тот же инструмент с параметрами "
            f"limit и offset (например, limit={DEFAULT_LIMIT}, offset=0)."
        ),
    }


def truncate_text(text: str, max_chars: int = 2000) -> dict:
    """
    Обрезает длинный текст (например, модуль BSL) с пометкой.

    Используется когда нужно показать «кусочек» большого текстового поля.
    """
    if not text:
        return {"text": "", "truncated": False, "original_length": 0}

    if len(text) <= max_chars:
        return {"text": text, "truncated": False, "original_length": len(text)}

    return {
        "text": text[:max_chars],
        "truncated": True,
        "original_length": len(text),
        "shown_chars": max_chars,
        "hint": f"Показано {max_chars} из {len(text)} символов. "
                f"Используйте offset для получения следующей части.",
    }


def truncate_text_window(text: str, offset: int = 0, window: int = 2000) -> dict:
    """
    Возвращает «окно» текста с заданного смещения.
    Позволяет листать большие модули по частям.
    """
    if not text:
        return {"text": "", "offset": 0, "window": window, "total": 0,
                "has_more": False, "next_offset": None}

    total = len(text)
    if offset < 0:
        offset = 0
    if offset >= total:
        return {
            "text": "",
            "offset": offset,
            "window": window,
            "total": total,
            "has_more": False,
            "next_offset": None,
            "hint": "offset превышает размер текста",
        }

    end = min(offset + window, total)
    has_more = end < total

    result = {
        "text": text[offset:end],
        "offset": offset,
        "window": window,
        "total": total,
        "shown_chars": end - offset,
        "has_more": has_more,
        "next_offset": end if has_more else None,
    }

    return result
