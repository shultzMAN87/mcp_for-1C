"""
SRC-6. Состояние источника и аренда записи.
===========================================

Один узел `:SourceState {key: 'main'}` отвечает на вопрос «чему сейчас
соответствует граф»:

    граф = коммит `indexed_sha` + файлы из `dirty` в том виде, в каком они
           лежали на диске, когда их проиндексировали.

Этот инвариант — главное. Любая операция, обновившая граф, обязана в той
же записи состояния оставить его верным; операция, которая упала на
полпути, не сдвигает `indexed_sha` и не трогает `dirty` — следующий такт
повторит работу.

Аренда
──────
Писателей несколько: такт watcher'а, `metadata_refresh` из чата,
`apply_changes.py` из консоли и индексер. Без порядка между ними два
одновременных обновления одного объекта дают ровно тот класс дефектов, что
`FIX-27`: срез снесён одним, привязка кода потеряна другим. Поэтому граф
обновляет только держатель аренды (`lock_owner` до `lock_until`).
Аренда с истечением, а не замок: упавший процесс не держит граф вечно.

Хранилищ два, с одним интерфейсом:
  • `Neo4jStore`  — боевое;
  • `MemoryStore` — для тестов логики (source_sync), где Neo4j не нужен.
Сама Cypher-часть проверяется `tests_source_state.py` против живой базы
(`NEO4J_TEST_URL`), как и остальная запись в граф (tests_graph_writer.py).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger("source-state")

STATE_KEY = "main"
DELETED = "deleted"          # значение в dirty: файл удалён на диске

# Время аренды. Такт и metadata_refresh продлевают её по ходу работы.
# Индексер (FIX-39) — тоже: пульсом из строк прогресса, раз в минуту.
# Раньше он брал аренду сразу на восемь часов, и упавший индексер запирал
# граф на всё это время. Полчаса — с запасом на самый долгий шаг без
# строки прогресса (запрос к Neo4j, разбор XML).
LEASE_SEC = 600.0
INDEXER_LEASE_SEC = 30 * 60.0


class LeaseLost(RuntimeError):
    """Аренду перехватили (истекла и её взял другой) — писать нельзя."""


@dataclass
class SourceState:
    mode: str = "git"
    config_subdir: str = ""
    indexed_sha: str = ""
    # путь от корня конфигурации → git-хеш того, что проиндексировано
    # (содержимое на диске в момент индексации) или DELETED
    dirty: dict = field(default_factory=dict)
    needs_reconcile: bool = False
    needs_full: bool = False
    needs_full_reason: str = ""
    last_tick_at: float = 0.0
    last_change_at: float = 0.0
    last_change: dict = field(default_factory=dict)
    last_reconcile_at: float = 0.0
    last_full_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    updated_at: float = 0.0

    # ── сериализация в свойства узла ──

    _JSON_FIELDS = ("dirty", "last_change")

    def to_props(self) -> dict:
        d = asdict(self)
        for k in self._JSON_FIELDS:
            d[k + "_json"] = json.dumps(d.pop(k), ensure_ascii=False,
                                        separators=(",", ":"), sort_keys=True)
        return d

    @classmethod
    def from_props(cls, props: dict) -> "SourceState":
        st = cls()
        broken = False
        for k in list(asdict(st)):
            if k in cls._JSON_FIELDS:
                raw = props.get(k + "_json")
                try:
                    val = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    log.warning("SourceState.%s не читается — нужна сверка", k)
                    val = {}
                    broken = True
                setattr(st, k, val if isinstance(val, dict) else {})
            elif k in props and props[k] is not None:
                setattr(st, k, type(getattr(st, k))(props[k]))
        # Битое состояние не должно выглядеть как «чистое»: пустой dirty
        # означал бы «граф = коммит», а это ложь. Флаг ставится ПОСЛЕ цикла —
        # иначе его затёр бы needs_reconcile=False из тех же свойств
        # (так и было в первой редакции; поймано тестом).
        if broken:
            st.needs_reconcile = True
        return st


@dataclass
class Lease:
    ok: bool
    owner: str
    until: float

    def describe(self) -> str:
        left = max(0.0, self.until - time.time())
        return f"{self.owner} (ещё {left:.0f} с)"


# ─── Хранилище в памяти (тесты) ───────────────────────────────────────────


class MemoryStore:
    def __init__(self):
        self.state: Optional[SourceState] = None
        self.lock_owner = ""
        self.lock_until = 0.0
        self.writes = 0

    def ensure_schema(self) -> None:
        pass

    def read(self) -> Optional[SourceState]:
        if self.state is None:
            return None
        return SourceState.from_props(self.state.to_props())   # копия

    def acquire(self, owner: str, ttl: float = LEASE_SEC,
                now: Optional[float] = None) -> Lease:
        now = time.time() if now is None else now
        if not self.lock_owner or self.lock_until < now or self.lock_owner == owner:
            self.lock_owner, self.lock_until = owner, now + ttl
            return Lease(True, owner, self.lock_until)
        return Lease(False, self.lock_owner, self.lock_until)

    def release(self, owner: str) -> None:
        if self.lock_owner == owner:
            self.lock_owner, self.lock_until = "", 0.0

    def force_unlock(self) -> None:
        self.lock_owner, self.lock_until = "", 0.0

    def holder(self) -> Lease:
        return Lease(bool(self.lock_owner) and self.lock_until >= time.time(),
                     self.lock_owner, self.lock_until)

    def write(self, state: SourceState, owner: str,
              now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if self.lock_owner != owner or self.lock_until < now:
            raise LeaseLost(f"аренда у {self.lock_owner or 'никого'}, а пишет {owner}")
        state.updated_at = now
        self.state = SourceState.from_props(state.to_props())
        self.writes += 1


# ─── Neo4j ────────────────────────────────────────────────────────────────

SCHEMA_CYPHER = ("CREATE CONSTRAINT source_state_key IF NOT EXISTS "
                 "FOR (s:SourceState) REQUIRE s.key IS UNIQUE")

READ_CYPHER = "MATCH (s:SourceState {key: $key}) RETURN properties(s) AS s"

# Блокировка берётся ДО чтения: `SET s._lock_probe` захватывает узел на
# запись, и значения lock_* ниже читаются уже под замком. Без этого две
# транзакции могли бы прочитать «свободно» одновременно и обе взять аренду.
ACQUIRE_CYPHER = """
MERGE (s:SourceState {key: $key})
SET s._lock_probe = $now
WITH s, (s.lock_owner IS NULL OR s.lock_owner = ''
         OR s.lock_until IS NULL OR s.lock_until < $now
         OR s.lock_owner = $owner) AS free
SET s.lock_owner = CASE WHEN free THEN $owner ELSE s.lock_owner END,
    s.lock_until = CASE WHEN free THEN $until ELSE s.lock_until END
RETURN free, s.lock_owner AS owner, s.lock_until AS until
"""

RELEASE_CYPHER = """
MATCH (s:SourceState {key: $key}) WHERE s.lock_owner = $owner
SET s.lock_owner = '', s.lock_until = 0.0
RETURN count(s) AS n
"""

FORCE_UNLOCK_CYPHER = """
MATCH (s:SourceState {key: $key})
SET s.lock_owner = '', s.lock_until = 0.0
RETURN count(s) AS n
"""

# Запись — только держателем живой аренды. Ноль строк = аренду потеряли.
WRITE_CYPHER = """
MATCH (s:SourceState {key: $key})
SET s._lock_probe = $now
WITH s
WHERE s.lock_owner = $owner AND s.lock_until >= $now
SET s += $props
RETURN count(s) AS n
"""


class Neo4jStore:
    def __init__(self, neo, key: str = STATE_KEY):
        self.neo = neo
        self.key = key

    def ensure_schema(self) -> None:
        try:
            self.neo.query(SCHEMA_CYPHER)
        except RuntimeError as e:
            if "EquivalentSchemaRule" not in str(e):
                log.warning("ограничение :SourceState не создано: %s", e)

    def read(self) -> Optional[SourceState]:
        rows = self.neo.rows(READ_CYPHER, {"key": self.key})
        if not rows or not rows[0].get("s"):
            return None
        props = rows[0]["s"]
        # Узел, созданный одной арендой (MERGE), без записанного состояния —
        # это «состояния нет», а не «граф = пустой коммит».
        if "mode" not in props:
            return None
        return SourceState.from_props(props)

    def acquire(self, owner: str, ttl: float = LEASE_SEC,
                now: Optional[float] = None) -> Lease:
        now = time.time() if now is None else now
        rows = self.neo.rows(ACQUIRE_CYPHER, {"key": self.key, "owner": owner,
                                              "now": now, "until": now + ttl})
        r = rows[0] if rows else {}
        return Lease(bool(r.get("free")), r.get("owner") or "",
                     float(r.get("until") or 0.0))

    def release(self, owner: str) -> None:
        try:
            self.neo.rows(RELEASE_CYPHER, {"key": self.key, "owner": owner})
        except Exception as e:   # noqa: BLE001 — аренда истечёт сама
            log.warning("аренда не отпущена (%s) — истечёт сама", e)

    def force_unlock(self) -> None:
        self.neo.rows(FORCE_UNLOCK_CYPHER, {"key": self.key})

    def holder(self) -> Lease:
        rows = self.neo.rows(
            "MATCH (s:SourceState {key: $key}) "
            "RETURN s.lock_owner AS owner, s.lock_until AS until",
            {"key": self.key})
        if not rows:
            return Lease(False, "", 0.0)
        owner = rows[0].get("owner") or ""
        until = float(rows[0].get("until") or 0.0)
        return Lease(bool(owner) and until >= time.time(), owner, until)

    def write(self, state: SourceState, owner: str,
              now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        state.updated_at = now
        rows = self.neo.rows(WRITE_CYPHER, {"key": self.key, "owner": owner,
                                            "now": now, "props": state.to_props()})
        if not rows or not rows[0].get("n"):
            raise LeaseLost(f"аренда :SourceState потеряна ({owner}) — "
                            f"состояние не записано, следующий такт повторит работу")
