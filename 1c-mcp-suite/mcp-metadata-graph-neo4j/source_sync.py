"""
SRC-7..12. Синхронизация графа с выгрузкой через git — один алгоритм.
=====================================================================

Такт watcher'а, `metadata_refresh` из чата, `apply_changes.py` из консоли,
сверка и старт индексера — это ОДНА функция `sync()`. Разница только в
том, какие файлы она рассматривает:

  • такт                — файлы из `dirty` + `git diff indexed_sha..HEAD`;
  • metadata_refresh    — то же + пути, которые назвал агент;
  • сверка (reconcile)  — то же + всё, что показывает `git status`.

Для каждого рассматриваемого файла сравниваются три версии:

    в графе   — хеш из `dirty`, если файл там есть, иначе версия в
                `indexed_sha` (граф = коммит + dirty, см. source_state);
    на диске  — то, что лежит сейчас;
    в HEAD    — канон.

  граф ≠ диск   → файл переиндексируется;
  диск ≠ HEAD   → файл остаётся (или попадает) в `dirty`;
  диск = HEAD   → из `dirty` уходит.

Так одним правилом покрываются pull (HEAD сдвинулся), правки агента
(диск ≠ HEAD), возврат канона через stash (диск снова = HEAD, хотя HEAD не
сдвигался) и правки, о которых никто не сообщил (их находит сверка).

Применение изменений (`apply_paths`) — тоже одно на всех. Neo4j оно не
знает: работает через `GraphOps`, в бою это `Neo4jGraphOps` поверх
incremental.py, в тестах — запись вызовов.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol

from git_source import Detection, GitError, GitRepo, detect
from metadata_xml import KIND_BY_DIR
from partial_fingerprint import owner_of
from source_state import (DELETED, LEASE_SEC, LeaseLost, SourceState)

log = logging.getLogger("source-sync")

DEFAULT_MAX_PARTIAL = 300
# Сколько путей из dirty показывать в ответах и metadata_stats.
DIRTY_SAMPLE = 20

# Статусы incremental, означающие успех, и ожидаемые пропуски — те же, что
# в apply_changes.py (там они живут в модуле с побочными эффектами при
# импорте, поэтому продублированы; расхождение ловит tests_source_sync).
OK_STATUSES = {"reindexed", "removed", "created", "updated", "ok"}
EXPECTED_SKIPS = {"not_a_toplevel_object_xml", "not_an_xml_file",
                  "not_a_bsl_file", "unsupported_extension",
                  "form_not_in_metadata"}     # FIX-41: хвост формы в выгрузке


# ─── Контекст ─────────────────────────────────────────────────────────────


@dataclass
class SourceContext:
    mode: str                       # 'git' | 'legacy'
    reason: str
    repo_root: Path
    src_root: Path
    subdir: str = ""
    repo: Optional[GitRepo] = None

    @property
    def is_git(self) -> bool:
        return self.mode == "git" and self.repo is not None


def context_from_env() -> SourceContext:
    """
    METADATA_SRC_DIR    — корень конфигурации (в контейнере /data/repo/src);
    METADATA_REPO_DIR   — корень репозитория (/data/repo); не задан —
                          совпадает с корнем конфигурации;
    SOURCE_MODE         — auto | git | legacy.
    """
    src_root = Path(os.environ.get("METADATA_SRC_DIR", "/data/1c-src"))
    repo_root = Path(os.environ.get("METADATA_REPO_DIR") or src_root)
    mode = os.environ.get("SOURCE_MODE", "auto")
    det: Detection = detect(repo_root, src_root, mode)
    return SourceContext(det.mode, det.reason, repo_root, src_root,
                         det.subdir, det.repo)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def max_partial_from_env() -> int:
    return env_int("METADATA_MAX_PARTIAL", DEFAULT_MAX_PARTIAL)


# ─── Применение изменений ─────────────────────────────────────────────────


class GraphOps(Protocol):
    def upsert(self, rel: str) -> dict: ...
    def remove_object(self, top_xml_rel: str) -> dict: ...
    def remove_module(self, bsl_rel: str) -> dict: ...
    def finish(self) -> dict: ...


@dataclass
class ApplyResult:
    files: list = field(default_factory=list)     # [{rel, action, status, reason}]
    failed: int = 0
    owners: int = 0
    ignored: list = field(default_factory=list)   # пути без владельца в графе
    needs_full_reason: str = ""
    finish: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.needs_full_reason

    def summary(self) -> dict:
        by = {}
        for f in self.files:
            by[f["action"]] = by.get(f["action"], 0) + 1
        return {"owners": self.owners, "files": len(self.files),
                "by_action": by, "failed": self.failed}


def _object_key(rel: str) -> Optional[tuple[str, str, str]]:
    """
    `(префикс, каталог_вида, имя_объекта)` для пути внутри объекта или
    None для пути вне каталогов видов (корневые модули и т. п.).
    `tests-extension/Catalogs/X/…` → ('tests-extension/', 'Catalogs', 'X').
    """
    parts = rel.split("/")
    off = 1 if parts and parts[0] == "tests-extension" else 0
    if len(parts) < off + 2 or parts[off] not in KIND_BY_DIR:
        return None
    name = parts[off + 1]
    if len(parts) == off + 2:
        if not name.lower().endswith(".xml"):
            return None
        name = name[:-4]
    prefix = "/".join(parts[:off]) + ("/" if off else "")
    return prefix, parts[off], name


def _object_modules(src_root: Path, key: tuple[str, str, str]) -> list[str]:
    """
    Все модули объекта на диске, которые граф умеет индексировать.

    Обновление XML объекта сносит его срез вместе с кодом (по префиксу id),
    поэтому после него модули заливаются заново — даже не изменившиеся.
    Это тот же `dependent_owners` из PERF-8, только список берётся с диска
    одного объекта, а не из полного обхода выгрузки. Модули без владельца в
    графе (команды, наборы записей — FIX-34) не берутся: incremental их
    всё равно пропустил бы.
    """
    prefix, d, name = key
    base_rel = f"{prefix}{d}/{name}"
    base = src_root / base_rel
    out: list[str] = []
    if not base.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.lower().endswith(".bsl"):
                continue
            rel = Path(dirpath, fn).relative_to(src_root).as_posix()
            if owner_of(rel) is not None:
                out.append(rel)
    return sorted(out)


def plan_owners(rel_paths: Iterable[str]) -> tuple[dict, dict, list]:
    """
    Пути → работа по владельцам.

    Возвращает `(objects, standalone, ignored)`:
      objects    — {ключ объекта: {"xml": bool, "modules": set(rel)}};
      standalone — {rel: owner} для модулей вне каталогов видов;
      ignored    — пути, которых нет в графе (корневые XML, FIX-34 и т. п.).
    """
    objects: dict = {}
    standalone: dict = {}
    ignored: list = []
    for rel in sorted(set(rel_paths)):
        owner = owner_of(rel)
        if owner is None:
            ignored.append(rel)
            continue
        key = _object_key(rel)
        if key is None:
            standalone[rel] = owner
            continue
        work = objects.setdefault(key, {"xml": False, "modules": set()})
        if rel.lower().endswith(".bsl"):
            work["modules"].add(rel)
        else:
            work["xml"] = True
    return objects, standalone, ignored


def apply_paths(ops: GraphOps, src_root: Path, rel_paths: Iterable[str],
                max_partial: int = DEFAULT_MAX_PARTIAL,
                renew: Optional[Callable[[], None]] = None) -> ApplyResult:
    """
    Привести граф в соответствие с диском для перечисленных файлов.

    Порядок — как в apply_changes (PERF-8): сначала удаления, потом
    обновления; внутри объекта XML раньше BSL (`upsert_xml_file` пересобирает
    срез объекта целиком, и залитый раньше BSL был бы снесён следом).

    Ошибка по одному файлу не останавливает остальные — их счёт в `failed`,
    и вызывающий НЕ сдвигает состояние: следующий такт повторит работу.
    """
    res = ApplyResult()
    objects, standalone, res.ignored = plan_owners(rel_paths)
    res.owners = len(objects) + len(standalone)
    if max_partial and res.owners > max_partial:
        res.needs_full_reason = (
            f"изменилось {res.owners} единиц при пороге {max_partial} — "
            f"полная индексация быстрее и даёт корректный глобальный резолв")
        return res

    def run(action: str, rel: str, fn: Callable[[str], dict]) -> None:
        try:
            r = fn(rel) or {}
        except LeaseLost:
            raise
        except Exception as e:  # noqa: BLE001 — по одному файлу, дальше идём
            r = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        status, reason = r.get("status"), r.get("reason")
        if status in OK_STATUSES or (status == "skipped" and reason in EXPECTED_SKIPS):
            pass
        else:
            res.failed += 1
            log.warning("    ✗ %s %s → %s (%s)", action, rel, status, reason)
        res.files.append({"rel": rel, "action": action, "status": status,
                          "reason": reason})

    # ── удаления ──
    upserts: list[tuple[str, list[str]]] = []
    for key, work in sorted(objects.items()):
        prefix, d, name = key
        top = f"{prefix}{d}/{name}.xml"
        if work["xml"] and not (src_root / top).is_file():
            run("remove_object", top, ops.remove_object)
            for m in sorted(work["modules"]):
                if not (src_root / m).is_file():
                    run("remove_module", m, ops.remove_module)
            continue
        gone = sorted(m for m in work["modules"] if not (src_root / m).is_file())
        for m in gone:
            run("remove_module", m, ops.remove_module)
        if work["xml"]:
            upserts.append((top, _object_modules(src_root, key)))
        else:
            live = sorted(set(work["modules"]) - set(gone))
            if live:
                upserts.append(("", live))
    for rel in sorted(standalone):
        if not (src_root / rel).is_file():
            run("remove_module", rel, ops.remove_module)

    # ── обновления ──
    for top, modules in upserts:
        if renew:
            renew()
        if top:
            run("upsert", top, ops.upsert)
        for m in modules:
            run("upsert", m, ops.upsert)
    for rel in sorted(standalone):
        if (src_root / rel).is_file():
            if renew:
                renew()
            run("upsert", rel, ops.upsert)

    if res.files:
        try:
            res.finish = ops.finish() or {}
        except Exception as e:  # noqa: BLE001 — итог — удобство, не условие успеха
            res.finish = {"error": f"{type(e).__name__}: {e}"}
    return res


# ─── Боевые операции над графом ───────────────────────────────────────────


class Neo4jGraphOps:
    """`GraphOps` поверх incremental.py — то же, чем пользуется apply_changes."""

    def __init__(self, neo, src_root: Path):
        self.neo = neo
        self.src_root = src_root

    def upsert(self, rel: str) -> dict:
        from incremental import upsert_file
        return upsert_file(self.neo, self.src_root, rel)

    def remove_object(self, top_xml_rel: str) -> dict:
        from incremental import remove_xml_file
        return remove_xml_file(self.neo, self.src_root, top_xml_rel)

    def remove_module(self, bsl_rel: str) -> dict:
        from incremental import remove_bsl_file
        return remove_bsl_file(self.neo, self.src_root, bsl_rel)

    def finish(self) -> dict:
        """Снимок рёбер (PERF-12), целостность владения (FIX-27), долг."""
        out: dict = {}
        try:
            from graph_writer import relations_snapshot_write
            relations_snapshot_write(self.neo)
        except Exception as e:  # noqa: BLE001
            out["snapshot_error"] = f"{type(e).__name__}: {e}"
        out["ownership"] = ownership(self.neo)
        out["stale_debt"] = stale_debt(self.neo)
        return out


def _graph_integrity():
    try:
        import graph_integrity
    except ImportError:  # pragma: no cover — локальный запуск из каталога набора
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import graph_integrity
    return graph_integrity


def ownership(neo) -> dict:
    """Три O(1)-счётчика владения кодом (FIX-27) — как в apply_changes."""
    gi = _graph_integrity()

    def one(cypher: str) -> int:
        rows = neo.rows(cypher)
        return (rows[0].get("c") or 0) if rows else 0

    return gi.ownership_report(
        one("MATCH (n:Callable) RETURN count(n) AS c"),
        one("MATCH (n:Module) RETURN count(n) AS c"),
        one(gi.HAS_METHOD_COUNT_CYPHER),
        objects=one("MATCH (n:MetadataObject) RETURN count(n) AS c"),
    )


def stale_debt(neo) -> int:
    from partial_fingerprint import stale_debt as _debt
    return _debt(neo)


# ─── Синхронизация ────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    status: str             # ok | unchanged | busy | no_state | needs_full | error | legacy | dry_run
    message: str = ""
    head: str = ""
    indexed_before: str = ""
    indexed_after: str = ""
    planned: list = field(default_factory=list)      # что требовало обновления
    applied: Optional[ApplyResult] = None
    paths: dict = field(default_factory=dict)        # для путей агента: rel → статус
    dirty_count: int = 0
    dirty_sample: list = field(default_factory=list)
    lease_holder: str = ""
    normalized_matches: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "unchanged", "dry_run")

    def as_dict(self) -> dict:
        d = {
            "status": self.status,
            "message": self.message,
            "head": self.head,
            "indexed_sha_before": self.indexed_before,
            "indexed_sha_after": self.indexed_after,
            "dirty_count": self.dirty_count,
            "dirty_sample": self.dirty_sample,
        }
        if self.planned:
            d["planned"] = self.planned[:200]
            d["planned_total"] = len(self.planned)
        if self.applied is not None:
            d["applied"] = self.applied.summary()
            d["failed_files"] = [f for f in self.applied.files
                                 if f["status"] not in OK_STATUSES][:50]
            if self.applied.finish:
                d["after"] = self.applied.finish
        if self.paths:
            d["paths"] = self.paths
        if self.lease_holder:
            d["lease_holder"] = self.lease_holder
        if self.normalized_matches:
            d["normalized_line_endings"] = self.normalized_matches
        return d


def state_usable(state: Optional[SourceState], ctx: SourceContext) -> tuple[bool, str]:
    if state is None:
        return False, "состояния источника в графе нет — нужна индексация"
    if state.mode != "git":
        return False, f"состояние записано в режиме {state.mode}"
    if not state.indexed_sha:
        return False, "в состоянии нет проиндексированного коммита"
    if state.config_subdir != ctx.subdir:
        return False, (f"граф собран для каталога конфигурации "
                       f"'{state.config_subdir}', а сейчас '{ctx.subdir}'")
    return True, ""


def _normalize_extra(ctx: SourceContext, paths: Iterable[str]) -> tuple[list, dict]:
    """
    Пути агента → пути от корня конфигурации. Возвращает (годные, статусы
    отказов). Нормализация — та же, что у metadata_upsert_file (SRC-4).
    """
    from incremental import _norm_rel
    good: list = []
    refused: dict = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        rel = _norm_rel(ctx.src_root, raw)
        if rel is None:
            refused[raw] = "outside_config"
            continue
        if ctx.subdir and (rel == ctx.subdir or rel.startswith(ctx.subdir + "/")):
            rel = rel[len(ctx.subdir) + 1:]
        if not (ctx.src_root / rel).exists() and not _looks_like_config(rel):
            refused[raw] = "outside_config"
            continue
        if owner_of(rel) is None:
            refused[raw] = "not_in_graph"
            continue
        good.append(rel)
    return good, refused


def _looks_like_config(rel: str) -> bool:
    first = rel.split("/", 1)[0]
    return first in KIND_BY_DIR or first == "tests-extension"


def sync(ctx: SourceContext, store, ops: Optional[GraphOps], *, owner: str,
         extra_paths: Iterable[str] = (), reconcile: bool = False,
         dry_run: bool = False, max_partial: int = DEFAULT_MAX_PARTIAL,
         lease_ttl: float = LEASE_SEC, keep_lease: bool = False,
         now: Optional[float] = None) -> SyncResult:
    """
    Привести граф к «indexed_sha = HEAD, dirty = то, что отличается от HEAD».

    `owner`     — кто работает (для аренды и логов): watcher, refresh, …
    `keep_lease`— не отпускать аренду в конце (индексер держит её сам).
    `dry_run`   — посчитать план, ничего не меняя и аренду не беря.
    """
    if not ctx.is_git:
        return SyncResult("legacy", f"режим legacy: {ctx.reason}")
    repo: GitRepo = ctx.repo
    now = time.time() if now is None else now

    extra_paths = [p for p in extra_paths]
    good_extra, refused = _normalize_extra(ctx, extra_paths)

    if not dry_run:
        lease = store.acquire(owner, lease_ttl, now=now)
        if not lease.ok:
            return SyncResult("busy", f"граф сейчас обновляет {lease.describe()}",
                              lease_holder=lease.owner,
                              paths={p: "busy" for p in extra_paths})
    try:
        return _sync_locked(ctx, repo, store, ops, owner=owner,
                            good_extra=good_extra, refused=refused,
                            reconcile=reconcile, dry_run=dry_run,
                            max_partial=max_partial, lease_ttl=lease_ttl, now=now)
    finally:
        if not dry_run and not keep_lease:
            store.release(owner)


def _sync_locked(ctx, repo: GitRepo, store, ops, *, owner, good_extra, refused,
                 reconcile, dry_run, max_partial, lease_ttl, now) -> SyncResult:
    state = store.read()
    usable, why = state_usable(state, ctx)
    if not usable:
        return SyncResult("no_state", why, paths={**refused,
                          **{p: "no_state" for p in good_extra}})

    res = SyncResult("unchanged", indexed_before=state.indexed_sha)
    try:
        head = repo.head()
        if head is None:
            raise GitError("в репозитории нет коммитов")
        res.head = head
        if not repo.commit_exists(state.indexed_sha):
            return _needs_full(store, state, owner, res, now, dry_run,
                               f"проиндексированного коммита {state.indexed_sha[:10]} "
                               f"нет в репозитории (история переписана?)")

        dirty = dict(state.dirty)
        cands: set = set(dirty) | set(good_extra)
        diff = repo.changed_between(state.indexed_sha, head)
        cands |= {rel for _st, rel in diff}
        if reconcile:
            cands |= {rel for _code, rel in repo.status()}

        # В граф идут не все файлы выгрузки. Остальные (корневые XML,
        # FIX-34) не сравниваются вовсе, а из dirty — выбрасываются.
        cands = {p for p in cands if owner_of(p) is not None}
        for p in list(dirty):
            if p not in cands:
                dirty.pop(p)

        # Ранний отказ ДО чтения файлов: неделя без pull может дать тысячи
        # путей, и хешировать их каждую минуту, чтобы снова сказать «много», —
        # это и есть та цена, от которой SRC уходит.
        objects, standalone, _ign = plan_owners(cands - set(dirty))
        rough = len(objects) + len(standalone)
        if max_partial and rough > max_partial:
            return _needs_full(store, state, owner, res, now, dry_run,
                               f"изменилось {rough} единиц при пороге {max_partial}")

        cands_l = sorted(cands)
        disk = repo.hash_on_disk(cands_l)
        head_b = repo.blobs_at(head, cands_l)
        not_dirty = [p for p in cands_l if p not in dirty]
        idx_b = (head_b if state.indexed_sha == head
                 else repo.blobs_at(state.indexed_sha, not_dirty))

        to_apply: list = []
        new_dirty = dict(dirty)
        for p in cands_l:
            d = disk.get(p)
            if p in dirty:
                rec = dirty[p]
                graph_matches_disk = (d is None) if rec == DELETED else \
                    (d is not None and rec in d.variants)
            else:
                graph_matches_disk = repo.note_match(d, idx_b.get(p))
            if not graph_matches_disk:
                to_apply.append(p)
            if repo.note_match(d, head_b.get(p)):
                new_dirty.pop(p, None)
            else:
                new_dirty[p] = d.raw if d is not None else DELETED
        res.planned = to_apply
        res.normalized_matches = repo.normalized_matches
        for p in good_extra:
            res.paths[p] = "planned" if p in to_apply else "unchanged"
        res.paths.update(refused)

        if dry_run:
            res.status = "dry_run"
            res.indexed_after = state.indexed_sha
            res.message = (f"будет обновлено файлов: {len(to_apply)}, "
                           f"HEAD {_short(state.indexed_sha)}→{_short(head)}")
            _fill_dirty(res, new_dirty)
            return res

        def renew() -> None:
            lease = store.acquire(owner, lease_ttl)
            if not lease.ok:
                raise LeaseLost(f"аренду перехватил {lease.owner}")

        if to_apply:
            applied = apply_paths(ops, ctx.src_root, to_apply,
                                  max_partial=max_partial, renew=renew)
            res.applied = applied
            for f in applied.files:
                if f["rel"] in res.paths:
                    res.paths[f["rel"]] = f["status"] or "error"
            if applied.needs_full_reason:
                return _needs_full(store, state, owner, res, now, dry_run,
                                   applied.needs_full_reason)
            if applied.failed:
                state.last_error = (f"не обновлено файлов: {applied.failed} "
                                    f"из {len(applied.files)}; состояние не сдвинуто")
                state.last_error_at = now
                state.last_tick_at = now
                store.write(state, owner, now=now)
                res.status = "error"
                res.message = state.last_error
                res.indexed_after = state.indexed_sha
                _fill_dirty(res, state.dirty)
                return res

        moved = state.indexed_sha != head
        dirty_changed = new_dirty != state.dirty
        state.indexed_sha = head
        state.dirty = new_dirty
        state.needs_full = False
        state.needs_full_reason = ""
        state.last_tick_at = now
        state.last_error = ""
        if reconcile:
            state.needs_reconcile = False
            state.last_reconcile_at = now
        if to_apply:
            state.last_change_at = now
            state.last_change = {"by": owner, "files": len(to_apply),
                                 "head": head, **(res.applied.summary()
                                                  if res.applied else {})}
        store.write(state, owner, now=now)

        res.indexed_after = head
        res.status = "ok" if (to_apply or moved or dirty_changed) else "unchanged"
        res.message = (f"обновлено файлов: {len(to_apply)}; "
                       f"HEAD {_short(res.indexed_before)}→{_short(head)}"
                       if (to_apply or moved) else "граф актуален")
        _fill_dirty(res, new_dirty)
        return res

    except LeaseLost as e:
        res.status = "busy"
        res.message = str(e)
        return res
    except GitError as e:
        res.status = "error"
        res.message = f"git: {e}"
        _record_error(store, state, owner, now, res.message, dry_run)
        return res


def _needs_full(store, state: SourceState, owner: str, res: SyncResult,
                now: float, dry_run: bool, reason: str) -> SyncResult:
    res.status = "needs_full"
    res.message = f"нужна полная индексация: {reason}"
    res.indexed_after = state.indexed_sha
    if not dry_run:
        state.needs_full = True
        state.needs_full_reason = reason
        state.last_tick_at = now
        try:
            store.write(state, owner, now=now)
        except LeaseLost as e:
            res.message += f" (состояние не записано: {e})"
    _fill_dirty(res, state.dirty)
    return res


def _record_error(store, state: SourceState, owner: str, now: float,
                  message: str, dry_run: bool) -> None:
    if dry_run:
        return
    state.last_error = message
    state.last_error_at = now
    state.last_tick_at = now
    try:
        store.write(state, owner, now=now)
    except LeaseLost:
        pass


def _fill_dirty(res: SyncResult, dirty: dict) -> None:
    res.dirty_count = len(dirty)
    res.dirty_sample = sorted(dirty)[:DIRTY_SAMPLE]


def _short(sha: str) -> str:
    return (sha or "—")[:10]


# ─── Состояние после полной индексации / принятие существующего графа ─────


def mark_indexed(ctx: SourceContext, store, owner: str, head: str, *,
                 full: bool, now: Optional[float] = None) -> SourceState:
    """
    Граф только что собран полностью (или принят как есть при переходе со
    старой схемы) по выгрузке на диске при `HEAD = head`.

    `dirty` пуст, но это НЕ утверждение «незакоммиченных правок нет»:
    индексер читал диск, и если на нём были правки поверх коммита, граф
    их содержит, а состояние — нет. Поэтому `needs_reconcile=True`: сверка
    (SRC-11) найдёт такие файлы и занесёт в dirty.
    """
    now = time.time() if now is None else now
    prev = store.read()
    st = SourceState(mode="git", config_subdir=ctx.subdir, indexed_sha=head,
                     dirty={}, needs_reconcile=True)
    if prev is not None:
        st.last_reconcile_at = prev.last_reconcile_at
        st.last_full_at = prev.last_full_at
        st.last_change_at = prev.last_change_at
        st.last_change = prev.last_change
    if full:
        st.last_full_at = now
    st.last_tick_at = now
    store.write(st, owner, now=now)
    return st


def describe(state: Optional[SourceState], ctx: SourceContext,
             head: Optional[str] = None) -> dict:
    """Блок `source` для metadata_stats и ответов инструментов."""
    out: dict = {"mode": ctx.mode, "reason": ctx.reason,
                 "config_subdir": ctx.subdir}
    if not ctx.is_git:
        return out
    if head is None:
        try:
            head = ctx.repo.head()
        except GitError as e:
            out["git_error"] = str(e)
    out["head"] = head
    if state is None:
        out["state"] = "нет — граф ещё не собирался в режиме git"
        return out
    out.update({
        "indexed_sha": state.indexed_sha,
        "in_sync": bool(head) and head == state.indexed_sha,
        "dirty_count": len(state.dirty),
        "dirty_sample": sorted(state.dirty)[:DIRTY_SAMPLE],
        "needs_full": state.needs_full,
        "needs_full_reason": state.needs_full_reason,
        "needs_reconcile": state.needs_reconcile,
        "last_tick_at": _iso(state.last_tick_at),
        "last_change_at": _iso(state.last_change_at),
        "last_change": state.last_change,
        "last_reconcile_at": _iso(state.last_reconcile_at),
        "last_full_at": _iso(state.last_full_at),
        "last_error": state.last_error,
    })
    if head and state.indexed_sha and head != state.indexed_sha:
        try:
            n = len(ctx.repo.changed_between(state.indexed_sha, head))
            out["behind_files"] = n
        except GitError:
            pass
    return out


def _iso(ts: float) -> Optional[str]:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
