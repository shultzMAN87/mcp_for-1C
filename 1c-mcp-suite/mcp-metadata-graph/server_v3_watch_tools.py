"""
MCP-tools для инкрементального обновления графа (watcher-интеграция, 4.6.5).
=============================================================================

Подгружается из server.py в конце файла, рядом с server_v3_tools и
server_v3_code_tools:

    from server_v3_watch_tools import register_v3_watch_tools
    register_v3_watch_tools(mcp, SRC_DIR, NEO4J_URL, NEO4J_USER, NEO4J_PASS,
                            _neo4j_available)

Регистрирует два tool'а, которые дёргает `workspace-watcher`
(см. workspace_watcher.py, target='metadata'):

  metadata_upsert_file(filepath)   — файл создан/изменён → точечная переиндексация
  metadata_remove_file(filepath)   — файл удалён/перемещён → точечное удаление среза

Оба делегируют в `incremental.py` (слой indexer'а, лежит в /app рядом —
copy'ится Dockerfile.python'ом для нужд indexer'а, переиспользуем).

ВАЖНО про границу актуальности инкремента — см. докстринг incremental.py.
Кратко: исходящие связи изменённого файла корректны полностью; входящие
:CALLS из чужих модулей подчищаются (осиротевшие callsite'ы помечаются
stale), но глобальная пере-сходимость type inference — только при полном
`metadata-indexer`. Для рабочего цикла агента инкремента достаточно.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def register_v3_watch_tools(
    mcp,
    src_dir: str,
    neo4j_url: str,
    neo4j_user: str,
    neo4j_pass: str,
    neo4j_available: Callable,
    graph_state: Optional[Callable] = None,
) -> None:
    """
    Регистрирует metadata_upsert_file / metadata_remove_file.

    `src_dir` — корень XML+BSL выгрузки (METADATA_SRC_DIR). Должен быть
    смонтирован в контейнер mcp-metadata-graph (тот же volume, что у
    metadata-indexer). Если не смонтирован — upsert вернёт file_not_found.
    """

    def _neo4j_reachable() -> bool:
        """FIX-3: только доступность, без требования наполненного графа.

        Пустой граф для этих инструментов — не повод отказывать: incremental
        сам вернёт `metadata_layer_empty` с внятной причиной. Раньше оба
        случая давали `neo4j_unavailable`, и по ответу нельзя было понять,
        Neo4j лежит или индексация не отработала.
        """
        if graph_state is not None:
            state, _detail = graph_state()
            return state != "unavailable"
        return bool(neo4j_available())

    from pathlib import Path

    # incremental.py + его зависимости (graph_writer / bsl_parser /
    # bsl_resolver / metadata_xml) лежат в /app — Dockerfile.python их
    # копирует для metadata-indexer, а server.py делает sys.path.insert(0,
    # "/app"). Импортируем лениво внутри register, чтобы отсутствие модуля
    # (старый образ) не уронило весь сервер — только эти два tool'а.
    try:
        from incremental import upsert_file as _upsert_file
        from incremental import remove_file as _remove_file
        from graph_writer import Neo4j as _Neo4j
        _watch_available = True
    except ImportError as e:
        logger.warning(
            "incremental.py недоступен — metadata_upsert_file/"
            "metadata_remove_file работать не будут: %s", e)
        _watch_available = False

    src_root = Path(src_dir)

    def _err(reason: str, **extra) -> str:
        d = {"status": "error", "reason": reason}
        d.update(extra)
        return json.dumps(d, ensure_ascii=False)

    def _neo() -> "_Neo4j":
        return _Neo4j(neo4j_url, neo4j_user, neo4j_pass)

    # ─── metadata_upsert_file ────────────────────────────────────────────

    @mcp.tool()
    def metadata_upsert_file(filepath: str) -> str:
        """
        Точечно переиндексировать один файл выгрузки 1С в граф Neo4j.

        Поддерживаются:
          • .bsl  — модуль кода → слой 2 (call graph): :Callable / :Parameter /
                    :CallSite + исходящие :CALLS / :OPERATES_ON, локальный
                    type inference (:INFERRED_TYPE).
          • .xml  — верхнеуровневый метаобъект → слой 1 (схема данных):
                    :MetadataObject + :Attribute / :TabularSection / :Form /
                    :EnumValue + :OF_TYPE / :RESOLVES_TO / :CONTAINS / …

        Вызывается file-watcher'ом при created/modified. Идемпотентно:
        старый срез файла сносится, пишется свежий.

        Параметры:
          filepath — абсолютный путь внутри контейнера (например,
                     '/data/1c-src/Catalogs/АукАукционы/Ext/ObjectModule.bsl')
                     или относительный от корня выгрузки.

        Возвращает JSON со статусом:
          status: 'reindexed' | 'removed' | 'skipped' | 'error'
          + счётчики записанных узлов/рёбер и метрики резолва (для .bsl).

        ГРАНИЦА АКТУАЛЬНОСТИ: инкремент корректен для исходящих связей
        изменённого файла. Глобальная пере-сходимость inter-procedural
        type inference и пере-резолюция чужих callsite'ов — только при
        полном прогоне metadata-indexer. Для рабочего цикла достаточно.
        """
        if not _watch_available:
            return _err("incremental_module_unavailable",
                        hint="Пересоберите образ mcp-metadata-graph "
                             "(нужен incremental.py в /app)")
        if not _neo4j_reachable():
            return _err("neo4j_unavailable")
        if not src_root.is_dir():
            return _err("src_dir_not_mounted",
                        src_dir=str(src_root),
                        hint="Смонтируйте METADATA_SRC_DIR в контейнер "
                             "mcp-metadata-graph (тот же volume, что у "
                             "metadata-indexer)")
        try:
            result = _upsert_file(_neo(), src_root, filepath)
        except Exception as e:  # noqa: BLE001
            logger.exception("metadata_upsert_file(%s) упал", filepath)
            return _err("exception", detail=f"{type(e).__name__}: {e}",
                        filepath=filepath)
        return json.dumps(result, ensure_ascii=False, indent=2)

    # ─── metadata_remove_file ────────────────────────────────────────────

    @mcp.tool()
    def metadata_remove_file(filepath: str) -> str:
        """
        Удалить из графа срез, принадлежащий одному файлу выгрузки 1С.

        Вызывается file-watcher'ом при deleted/moved (для moved — на старый
        путь; новый путь придёт отдельным metadata_upsert_file).

        Поддерживаются .bsl (срез слоя 2 модуля) и .xml (срез слоя 1
        метаобъекта). Файл на диске уже может отсутствовать — классификация
        идёт по пути, не по содержимому.

        Параметры:
          filepath — абсолютный путь внутри контейнера или относительный
                     от корня выгрузки.

        Возвращает JSON:
          status: 'removed' | 'skipped' | 'error'
          + счётчики удалённых узлов.

        :Type-узлы (общие между объектами) и сами :Module/:MetadataObject
        родительских контейнеров при удалении .bsl НЕ трогаются — сносится
        только то, что однозначно принадлежит файлу.
        """
        if not _watch_available:
            return _err("incremental_module_unavailable")
        if not _neo4j_reachable():
            return _err("neo4j_unavailable")
        try:
            result = _remove_file(_neo(), src_root, filepath)
        except Exception as e:  # noqa: BLE001
            logger.exception("metadata_remove_file(%s) упал", filepath)
            return _err("exception", detail=f"{type(e).__name__}: {e}",
                        filepath=filepath)
        return json.dumps(result, ensure_ascii=False, indent=2)

    logger.info("v3 watch tools (metadata_upsert_file / metadata_remove_file) "
                "зарегистрированы (incremental=%s)", _watch_available)
