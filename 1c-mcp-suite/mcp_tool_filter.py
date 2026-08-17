"""
Централизованный контроль набора инструментов (задачи TOOL-1, TOOL-2, SEC-5).

Зачем отдельный модуль, а не правка каждого server.py: регистрация инструментов
размазана по четырём файлам (server.py + три register_v3_*), и вырезать оттуда
декораторы — это правка логики ради правки состава. Здесь же состав задан
одним списком, который видно целиком, и его легко ревьюить и менять.

Механика: FastMCP держит инструменты в `mcp._tool_manager._tools` (dict
имя -> Tool). Модуль импортируется ПОСЛЕ того, как сервер зарегистрировал
всё своё, и удаляет лишнее.

Профили (env MCP_TOOL_PROFILE):
  core (по умолчанию) — рабочий набор под три задачи из плана;
  full               — ничего не удаляется, старое поведение.

API-1: почему модуль падает, а не жалуется
──────────────────────────────────────────
`mcp._tool_manager._tools` — приватное поле чужого SDK. До правки при
`AttributeError` модуль писал в stderr «набор оставлен как есть» и
продолжал работу.

Разберём, что это значило. В этой же функции удаляются `metadata_reload`
(`MATCH (n) DETACH DELETE n` без подтверждения) и `metadata_cypher`
(произвольный Cypher). Переименуй следующая версия SDK приватное поле —
фильтр отключился бы целиком, и оба инструмента вернулись бы в набор. Не
«функция перестала работать», а «защита снялась, а сервер поднялся как
обычно»: единственная точка проекта, где цена ошибки не «плохой ответ», а
открытый разрушающий инструмент.

Поэтому исходы разделены:

  реестр не найден            → `ToolRegistryUnavailable`, сервер не стартует;
  опасное осталось в наборе   → `ToolRegistryUnavailable`, сервер не стартует;
  профиль `full`              → легальный путь ничего не удалять, но он
                                задаётся человеком в `MCP_TOOL_PROFILE`, а
                                не отказом самоанализа.

Профиль `full` при этом НЕ отключает удаление опасного: `metadata_reload`
и `metadata_cypher` держатся своими флагами (`ALLOW_DESTRUCTIVE_TOOLS`,
`ALLOW_RAW_CYPHER`), и «показать всё» — это про дубли v2/v3, а не про
разрушающий инструмент.

Это тот же fail-closed, что у пустого `MCP_SHARED_SECRET` (SEC-3), и тот
же код выхода — 78, EX_CONFIG.

Отдельно управляются:
  ALLOW_DESTRUCTIVE_TOOLS=1 — вернуть metadata_reload (SEC-5);
  ALLOW_RAW_CYPHER=1        — вернуть metadata_cypher (SEC-6);
  ENABLE_WATCH_TOOLS=0      — убрать служебные metadata_upsert_file /
                              metadata_remove_file (нужны workspace-watcher'у,
                              поэтому по умолчанию включены).
"""
from __future__ import annotations

import os

# ─── TOOL-1: v2-инструменты, у которых есть канонический v3-эквивалент ───
# Слева — что убираем, справа — чем пользоваться вместо.
V2_SUPERSEDED = {
    "metadata_references_to":  "metadata_referrers",
    "metadata_object_details": "metadata_object_attributes",
    "metadata_dependency_tree": "metadata_find_link_path",
    "metadata_subsystems":      "metadata_subsystem_tree",
    "metadata_subsystem_members": "metadata_subsystem_tree",
    "metadata_references_from": "metadata_find_link_path",
    # Три статистики схлопнуты в одну: metadata_stats теперь отдаёт
    # и слой метаданных, и слой кода (см. mcp-metadata-graph/server.py).
    "metadata_v3_stats": "metadata_stats",
    "code_v3_stats":     "metadata_stats",
    # metadata_list_kinds отдаёт то же, что секция by_kind в metadata_stats.
    "metadata_list_kinds": "metadata_stats",
}

# ─── TOOL-2: platform-help — инструменты без данных ──────────────────────
# Пусто, и это результат, а не недоделка.
#
# Здесь висели `its_search` и `search_all` — вырезанные с Захода 1 в
# ожидании DATA-1 (наполнить коллекцию its_articles). DATA-1 закрыта
# иначе: стандарты приехали отдельным сервером v8std-mcp, коллекция
# `its_articles` не появится, а сами инструменты удалены из
# mcp-platform-help/server.py вместе с кодом, который их обслуживал.
#
# Список оставлен: механика фильтра нужна, и следующий инструмент без
# данных пропишется сюда же.
NO_DATA: dict[str, str] = {}

# ─── Опасные инструменты ─────────────────────────────────────────────────
DESTRUCTIVE = {
    "metadata_reload": "MATCH (n) DETACH DELETE n без подтверждения (SEC-5)",
}
RAW_CYPHER = {
    "metadata_cypher": "произвольный Cypher; чёрный список подстрок обходится (SEC-6)",
}
WATCH_TOOLS = {
    "metadata_upsert_file": "служебный, вызывается workspace-watcher",
    "metadata_remove_file": "служебный, вызывается workspace-watcher",
}


class ToolRegistryUnavailable(RuntimeError):
    """
    Не удалось привести набор инструментов к целевому (API-1).

    Отдельный класс, а не голый RuntimeError: `start.py` обязан отличать
    этот отказ от любой другой ошибки импорта и выйти с EX_CONFIG, а не
    записать строку в лог и подняться.
    """


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _drop(tools: dict, name: str, reason: str, removed: list) -> None:
    if name in tools:
        del tools[name]
        removed.append((name, reason))


def apply_profile(mcp_obj, server_name: str) -> None:
    """Приводит набор инструментов сервера к целевому. Идемпотентна."""
    profile = os.environ.get("MCP_TOOL_PROFILE", "core").strip().lower()

    try:
        tools = mcp_obj._tool_manager._tools
    except AttributeError as exc:
        raise ToolRegistryUnavailable(
            f"{server_name}: не удалось добраться до реестра инструментов "
            f"FastMCP ({exc}).\n"
            f"Это приватное поле чужого SDK (mcp._tool_manager._tools), и оно "
            f"могло переехать при обновлении.\n"
            f"Сервер не стартует намеренно: без фильтра в наборе остались бы "
            f"metadata_reload (DETACH DELETE без подтверждения) и "
            f"metadata_cypher (произвольный Cypher).\n"
            f"Что делать: закрепить прежнюю версию mcp в "
            f"requirements*.lock.txt либо поправить путь к реестру здесь, в "
            f"mcp_tool_filter.py."
        ) from exc

    if not isinstance(tools, dict):
        raise ToolRegistryUnavailable(
            f"{server_name}: реестр инструментов найден, но это "
            f"{type(tools).__name__}, а не dict — удалять из него нечем. "
            f"См. комментарий выше про версию SDK."
        )

    before = len(tools)
    removed: list[tuple[str, str]] = []

    if profile != "full":
        for name, replacement in V2_SUPERSEDED.items():
            _drop(tools, name, f"дубль, используйте {replacement}", removed)
        for name, reason in NO_DATA.items():
            _drop(tools, name, reason, removed)

    if not _flag("ALLOW_DESTRUCTIVE_TOOLS", False):
        for name, reason in DESTRUCTIVE.items():
            _drop(tools, name, reason, removed)

    if not _flag("ALLOW_RAW_CYPHER", False):
        for name, reason in RAW_CYPHER.items():
            _drop(tools, name, reason, removed)

    if not _flag("ENABLE_WATCH_TOOLS", True):
        for name, reason in WATCH_TOOLS.items():
            _drop(tools, name, reason, removed)

    _assert_dangerous_gone(tools, server_name)

    after = len(tools)
    print(
        f"[tool-filter] {server_name}: профиль={profile}, "
        f"инструментов {before} -> {after}",
        flush=True,
    )
    for name, reason in removed:
        print(f"[tool-filter]   - {name}: {reason}", flush=True)
    if after:
        print(f"[tool-filter]   набор: {', '.join(sorted(tools))}", flush=True)


def _assert_dangerous_gone(tools: dict, server_name: str) -> None:
    """
    Сверка входа с выходом для набора инструментов.

    Тот же приём, что `shortfall.py` применяет к данным: недостаточно
    выполнить удаление — надо проверить, что после него удалённого нет.
    Между «мы вызвали del» и «инструмента в наборе нет» помещается всё, что
    делает защиту бумажной: другое имя ключа, второй реестр, повторная
    регистрация после фильтра.

    Проверяется только опасное. Дубли v2/v3 — вопрос удобства, и если они
    останутся, это плохой набор, а не открытая дверь.
    """
    must_be_gone: list[tuple[str, str]] = []
    if not _flag("ALLOW_DESTRUCTIVE_TOOLS", False):
        must_be_gone += [(n, "ALLOW_DESTRUCTIVE_TOOLS") for n in DESTRUCTIVE]
    if not _flag("ALLOW_RAW_CYPHER", False):
        must_be_gone += [(n, "ALLOW_RAW_CYPHER") for n in RAW_CYPHER]

    left = [(name, env) for name, env in must_be_gone if name in tools]
    if not left:
        return

    lines = ", ".join(f"{name} (вернуть осознанно: {env}=1)"
                      for name, env in left)
    raise ToolRegistryUnavailable(
        f"{server_name}: опасные инструменты остались в наборе после "
        f"фильтра — {lines}.\n"
        f"Удаление выполнено, но проверка показала их на месте: значит, "
        f"реестр не тот, инструмент зарегистрирован повторно или имя "
        f"разъехалось. Сервер не стартует."
    )
