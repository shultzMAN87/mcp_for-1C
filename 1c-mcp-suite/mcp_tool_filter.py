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

Отдельно управляются:
  ALLOW_DESTRUCTIVE_TOOLS=1 — вернуть metadata_reload (SEC-5);
  ALLOW_RAW_CYPHER=1        — вернуть metadata_cypher (SEC-6);
  ENABLE_WATCH_TOOLS=0      — убрать служебные metadata_upsert_file /
                              metadata_remove_file (нужны workspace-watcher'у,
                              поэтому по умолчанию включены).
"""
from __future__ import annotations

import os
import sys

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
# its-articles/ содержит только README.txt, коллекция its_articles пуста.
# search_all при этом обещает в докстринге общее ранжирование, которого в
# коде нет. Обе возвращаются после выполнения DATA-1.
NO_DATA = {
    "its_search": "нет данных: its-articles/ пуст (см. DATA-1)",
    "search_all": "зависит от its_search + докстринг не соответствует коду",
}

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
        sys.stderr.write(
            f"[tool-filter] {server_name}: не удалось добраться до реестра "
            f"инструментов ({exc}); набор оставлен как есть\n"
        )
        return

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
