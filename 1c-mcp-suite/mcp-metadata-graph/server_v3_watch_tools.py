"""
MCP-tools для инкрементального обновления графа (watcher-интеграция, 4.6.5).
=============================================================================

Подгружается из server.py в конце файла, рядом с server_v3_tools и
server_v3_code_tools:

    from server_v3_watch_tools import register_v3_watch_tools
    register_v3_watch_tools(mcp, SRC_DIR, NEO4J_URL, NEO4J_USER, NEO4J_PASS,
                            _neo4j_available)

Регистрирует:

  metadata_refresh(paths=None)     — SRC-8: доиндексация. Без аргументов —
                                     «проверь сейчас» (такт: HEAD + dirty);
                                     с путями — «я поменял эти файлы».
  metadata_upsert_file(filepath)   — файл создан/изменён → точечная переиндексация
  metadata_remove_file(filepath)   — файл удалён/перемещён → точечное удаление среза

Последние два дёргает `workspace-watcher` в прежнем режиме poll. В режиме
git они проходят через тот же `source_sync.sync()`, что и metadata_refresh:
иначе граф обновился бы, а состояние источника (indexed_sha + dirty) — нет,
и следующий возврат канона остался бы незамеченным.

Здесь же — `source_stats_block()` для блока `source` в metadata_stats.

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


# ─── SRC-8/12: состояние источника для инструментов и metadata_stats ─────

# Определение режима (git/legacy) — это `git rev-parse` и проверка
# каталогов: дёшево, но не бесплатно, а меняется только при пересоздании
# контейнера. Кешируется на пять минут.
_SOURCE_CACHE: dict = {"ctx": None, "at": 0.0, "error": ""}
_SOURCE_TTL_SEC = 300.0
_NEO_PARAMS: dict = {}

# Точки подмены для тестов (tests_refresh_tool.py): хранилище состояния и
# операции над графом. В бою — None, и берутся Neo4jStore / Neo4jGraphOps.
_STORE_FACTORY = None
_OPS_FACTORY = None


def _source_context(force: bool = False):
    import time as _time
    now = _time.monotonic()
    if not force and _SOURCE_CACHE["ctx"] is not None \
            and now - _SOURCE_CACHE["at"] < _SOURCE_TTL_SEC:
        return _SOURCE_CACHE["ctx"]
    try:
        from source_sync import context_from_env
        ctx = context_from_env()
        _SOURCE_CACHE.update(ctx=ctx, at=now, error="")
    except Exception as e:  # noqa: BLE001 — режим неизвестен, но сервер жив
        _SOURCE_CACHE.update(ctx=None, at=now, error=f"{type(e).__name__}: {e}")
        return None
    return ctx


def _source_store():
    if _STORE_FACTORY is not None:
        return _STORE_FACTORY()
    from graph_writer import Neo4j as _Neo4j
    from source_state import Neo4jStore
    return Neo4jStore(_Neo4j(_NEO_PARAMS["url"], _NEO_PARAMS["user"],
                             _NEO_PARAMS["pass"]))


def _watcher_info() -> dict:
    enabled = os.environ.get("WATCHER_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on")
    return {"enabled": enabled,
            "poll_sec": os.environ.get("SOURCE_POLL_SEC", "60"),
            "note": ("watcher живёт в контейнере workspace-watcher; здесь — "
                     "его настройки из .env")}


def source_stats_block() -> dict:
    """
    Блок `source` для metadata_stats: свежий ли граф относительно выгрузки.

    Никогда не бросает исключений: metadata_stats — диагностический
    инструмент, и сломанный источник не должен ронять ответ про граф.
    """
    try:
        ctx = _source_context()
        if ctx is None:
            return {"mode": "unknown", "error": _SOURCE_CACHE["error"]}
        from source_sync import describe
        state = None
        holder = None
        if ctx.is_git and (_NEO_PARAMS or _STORE_FACTORY is not None):
            store = _source_store()
            state = store.read()
            holder = store.holder()
        block = describe(state, ctx)
        block["watcher"] = _watcher_info()
        if holder is not None and holder.ok:
            block["lease_holder"] = holder.describe()
        block["hint"] = _source_hint(block)
        return block
    except Exception as e:  # noqa: BLE001
        return {"mode": "unknown", "error": f"{type(e).__name__}: {e}"}


def _source_hint(block: dict) -> str:
    if block.get("mode") != "git":
        return ("режим legacy: граф не знает, свежий ли он. После обновления "
                "выгрузки — apply_changes.py; см. README «Обновление выгрузки»")
    if block.get("state"):
        return ("состояния источника нет: граф ещё не индексировался в режиме "
                "git. Запустите metadata-indexer (docker compose up "
                "metadata-indexer) — полная индексация не понадобится, если "
                "выгрузка не менялась")
    if block.get("needs_full"):
        return ("нужна полная индексация: " + (block.get("needs_full_reason") or "")
                + ". Запуск: .\\scripts\\graph-refresh.ps1 -Full")
    if block.get("in_sync") is False:
        n = block.get("behind_files")
        lag = f"на {n} файлов" if n is not None else ""
        if block.get("watcher", {}).get("enabled"):
            return (f"граф отстаёт {lag}; watcher включён и догонит за такт, "
                    f"или metadata_refresh() — сейчас").strip()
        return (f"граф отстаёт {lag} — запустите доиндексацию "
                f"(metadata_refresh() или .\\scripts\\graph-refresh.ps1) "
                f"или включите watcher (WATCHER_ENABLED=true)").replace("  ", " ")
    if block.get("needs_reconcile"):
        return ("граф актуален по коммиту; незакоммиченные правки на диске "
                "ещё не сверялись — metadata_refresh(reconcile=True) или "
                "graph-refresh.ps1 -Reconcile")
    return "граф актуален"


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
    _NEO_PARAMS.update({"url": neo4j_url, "user": neo4j_user, "pass": neo4j_pass})

    def _err(reason: str, **extra) -> str:
        d = {"status": "error", "reason": reason}
        d.update(extra)
        return json.dumps(d, ensure_ascii=False)

    def _neo() -> "_Neo4j":
        return _Neo4j(neo4j_url, neo4j_user, neo4j_pass)

    def _run_sync(paths: list, owner: str, *, reconcile: bool = False,
                  dry_run: bool = False) -> dict:
        from source_sync import (Neo4jGraphOps, max_partial_from_env, sync)
        ctx = _source_context()
        store = _source_store()
        if dry_run:
            ops = None
        elif _OPS_FACTORY is not None:
            ops = _OPS_FACTORY(ctx)
        else:
            ops = Neo4jGraphOps(_neo(), ctx.src_root)
        res = sync(ctx, store, ops,
                   owner=owner, extra_paths=paths, reconcile=reconcile,
                   dry_run=dry_run, max_partial=max_partial_from_env())
        out = res.as_dict()
        out["source"] = {k: v for k, v in source_stats_block().items()
                         if k in ("mode", "head", "indexed_sha", "in_sync",
                                  "dirty_count", "needs_full",
                                  "needs_full_reason", "needs_reconcile", "hint")}
        return out

    def _via_sync(paths: list, owner: str) -> Optional[str]:
        """
        В режиме git старые инструменты идут через sync — см. докстринг
        модуля. None — режим legacy, работать по-старому.
        """
        ctx = _source_context()
        if ctx is None or not ctx.is_git:
            return None
        try:
            return json.dumps(_run_sync(paths, owner), ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.exception("%s через sync упал", owner)
            return _err("exception", detail=f"{type(e).__name__}: {e}")

    # ─── metadata_refresh (SRC-8) ────────────────────────────────────────

    @mcp.tool()
    def metadata_refresh(paths: Optional[list[str]] = None,
                         reconcile: bool = False,
                         dry_run: bool = False) -> str:
        """
        Доиндексировать граф по изменениям в выгрузке конфигурации.

        КОГДА ВЫЗЫВАТЬ:
          • после того как ты записал, создал или удалил файлы в каталоге
            конфигурации (src/…) — передай их пути в `paths`;
          • после возврата канона (git stash / checkout) или обновления
            выгрузки — вызови без аргументов;
          • перед ответом, который опирается на граф, если metadata_stats →
            source.in_sync = false.

        Параметры:
          paths     — пути изменённых файлов в любом виде: от корня
                      конфигурации (`Catalogs/X/Ext/ObjectModule.bsl`), от
                      корня репозитория (`src/Catalogs/…`), абсолютные
                      Windows-пути (`D:\\…\\src\\Catalogs\\…`). Пути вне
                      конфигурации (AGENTS.md, openspec/…) возвращаются со
                      статусом outside_config — это не ошибка.
          reconcile — сверить всю рабочую копию через git status: найти
                      правки, о которых никто не сообщил. Может занять
                      минуты на большой выгрузке.
          dry_run   — показать, что было бы обновлено, ничего не меняя.

        Без аргументов — «проверь сейчас»: то же, что делает watcher по
        таймеру (сдвинулся ли HEAD, изменились ли ранее правленные файлы).

        Возвращает JSON: status (ok | unchanged | busy | needs_full |
        no_state | error | legacy), paths — статус по каждому переданному
        пути (reindexed | removed | unchanged | outside_config |
        not_in_graph | busy), applied — сводка, source — свежесть графа.

        busy — граф сейчас обновляет кто-то другой (watcher, индексер):
        повтори через минуту. needs_full — изменений слишком много для
        точечного обновления, нужна полная индексация; сам инструмент её не
        запускает (это часы), а говорит об этом.
        """
        if not _watch_available:
            return _err("incremental_module_unavailable",
                        hint="Пересоберите образ mcp-metadata-graph")
        if not _neo4j_reachable():
            return _err("neo4j_unavailable")
        paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
        ctx = _source_context()
        if ctx is None:
            return _err("source_unavailable", detail=_SOURCE_CACHE["error"])
        if not ctx.is_git:
            return json.dumps(_legacy_refresh(paths, ctx), ensure_ascii=False,
                              indent=2)
        try:
            out = _run_sync(paths, "refresh", reconcile=reconcile, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            logger.exception("metadata_refresh упал")
            return _err("exception", detail=f"{type(e).__name__}: {e}")
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _legacy_refresh(paths: list, ctx) -> dict:
        """
        Каталог без git: состояние не отслеживается. С путями — то, что
        делал metadata_upsert_file; без путей — подсказка, что делать.
        """
        out: dict = {"status": "legacy", "mode": "legacy", "reason": ctx.reason,
                     "message": ("выгрузка не в git — граф не знает, что "
                                 "изменилось. Файлы, переданные в paths, "
                                 "обновлены по одному; после обновления всей "
                                 "выгрузки запустите apply_changes.py")}
        results = {}
        for p in paths:
            try:
                r = _upsert_file(_neo(), src_root, p)
                if r.get("reason") == "file_not_found":
                    r = _remove_file(_neo(), src_root, p)
                results[p] = r.get("status") or "error"
            except Exception as e:  # noqa: BLE001
                results[p] = f"error: {type(e).__name__}: {e}"
        if results:
            out["paths"] = results
        return out

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
        routed = _via_sync([filepath], "upsert_file")
        if routed is not None:
            return routed
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
        routed = _via_sync([filepath], "remove_file")
        if routed is not None:
            return routed
        try:
            result = _remove_file(_neo(), src_root, filepath)
        except Exception as e:  # noqa: BLE001
            logger.exception("metadata_remove_file(%s) упал", filepath)
            return _err("exception", detail=f"{type(e).__name__}: {e}",
                        filepath=filepath)
        return json.dumps(result, ensure_ascii=False, indent=2)

    logger.info("v3 watch tools (metadata_refresh / metadata_upsert_file / "
                "metadata_remove_file) зарегистрированы (incremental=%s)",
                _watch_available)
    ctx = _source_context()
    if ctx is not None:
        logger.info("Источник: режим %s — %s", ctx.mode, ctx.reason)
    else:
        logger.warning("Источник: режим не определён — %s", _SOURCE_CACHE["error"])
