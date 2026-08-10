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
    total = len(items)
    end = params.offset + params.limit
    page = items[params.offset:end]
    has_more = end < total

    response = {
        "total": total,
        "returned": len(page),
        "offset": params.offset,
        "limit": params.limit,
        "has_more": has_more,
    }
    if has_more:
        response["next_offset"] = end
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
        return {"text": "", "offset": 0, "window": window, "total": 0, "has_more": False}

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
    }
    if has_more:
        result["next_offset"] = end

    return result
