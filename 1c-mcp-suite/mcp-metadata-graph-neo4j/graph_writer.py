"""
Запись графа метаданных в Neo4j через HTTP /db/neo4j/tx/commit.

Без зависимости от драйвера neo4j-python — используем urllib, как соседний indexer.py.
Запись батчами через UNWIND $rows (по 500 узлов/рёбер за запрос).

Дизайн:
  ensure_schema()             — CREATE CONSTRAINT / INDEX (идемпотентно)
  fingerprint_workspace()     — sha256 от отсортированного списка (path, sha256(content))
  fingerprint_get(NEO4J)      — читает текущий fingerprint из Neo4j (None если нет)
  fingerprint_write(NEO4J, …) — обновляет fingerprint
  clear_metadata_layer()      — удаляет только слой графа метаданных
                                (узлы :MetadataObject/:Attribute/:Type/...),
                                не трогая будущий слой графа вызовов
  write_graph(NEO4J, graph)   — UNWIND-батчи всех узлов и рёбер

Совместимость:
  Оставляем существующее свойство `attributes_json` на узлах :MetadataObject
  для обратной совместимости с metadata_object_details — старые клиенты
  продолжают работать. Новые tools используют узлы :Attribute напрямую.

Сборка тестируется через testcontainers Neo4j (см. tests_graph_writer.py),
но саму запись на mock'е не имитируем — она проверяется на смоук-стеке.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional

from progress_log import ProgressLogger, human_sec

# A-2: правило «отправлено против записанного» переехало в общий модуль.
# Здесь оно и родилось, но применялось только тут — см. shortfall.py.
try:
    from shortfall import warn_shortfall as _shortfall
except ImportError:  # pragma: no cover — путь только для локального запуска
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from shortfall import warn_shortfall as _shortfall

log = logging.getLogger(__name__)

# ─── Neo4j HTTP-клиент ────────────────────────────────────────────────────


class Neo4j:
    """Минимальный HTTP-клиент к Neo4j /db/neo4j/tx/commit."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def query(self, cypher: str, parameters: Optional[dict] = None,
              include_stats: bool = False) -> dict:
        statement: dict[str, Any] = {
            "statement": cypher,
            "parameters": parameters or {},
        }
        # FIX-31. `includeStats` заставляет Neo4j вернуть, СКОЛЬКО он создал
        # узлов и связей этим запросом. Считает это сама база, по факту
        # записи; ни второго прохода, ни служебных свойств на рёбрах не
        # нужно. Ключ добавляется только когда его просят: без него ответ
        # короче, а запросов у нас тысячи.
        if include_stats:
            statement["includeStats"] = True
        payload = json.dumps({"statements": [statement]}).encode()
        req = urllib.request.Request(
            f"{self.url}/db/neo4j/tx/commit",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read())
        except urllib.error.URLError as e:
            log.warning("Neo4j transport error: %s; cypher head: %s",
                        e, (cypher or "")[:200].replace("\n", " "))
            raise
        errors = result.get("errors", [])
        if errors:
            # Логируем подробно перед raise, иначе writer молча падает на
            # длинных UNWIND'ах, и непонятно, какой именно батч сломался.
            log.warning("Neo4j query error: %s; cypher head: %s",
                        errors, (cypher or "")[:200].replace("\n", " "))
            raise RuntimeError(f"Neo4j: {errors}")
        return result

    @staticmethod
    def _rows_of(result: dict) -> list[dict]:
        cols = result.get("columns", [])
        out = []
        for data in result.get("data", []):
            out.append({c: data["row"][i] for i, c in enumerate(cols)})
        return out

    def rows(self, cypher: str, parameters: Optional[dict] = None) -> list[dict]:
        r = self.query(cypher, parameters)
        return self._rows_of(r["results"][0])

    def rows_with_stats(self, cypher: str,
                        parameters: Optional[dict] = None) -> tuple[list[dict], dict]:
        """
        То же, что `rows`, плюс статистика транзакции (`FIX-31`).

        Возвращает `(строки, stats)`. В `stats` лежат ключи Neo4j
        `relationships_created`, `nodes_created`, `properties_set` и т.п.
        Если версия базы статистику не прислала, второй элемент — пустой
        словарь; вызывающий обязан это пережить, а не считать нулём.
        """
        r = self.query(cypher, parameters, include_stats=True)
        result = r["results"][0]
        return self._rows_of(result), (result.get("stats") or {})

    def wait(self, timeout: float = 120.0) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(self.url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(3)
        raise RuntimeError(f"Neo4j недоступен по {self.url} после {timeout}с")


# ─── Схема ────────────────────────────────────────────────────────────────


CONSTRAINTS = [
    # Уникальность id на каждом типе узла. Используем generic-ключ `id`.
    "CREATE CONSTRAINT meta_id IF NOT EXISTS "
    "FOR (n:MetadataObject) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT attr_id IF NOT EXISTS "
    "FOR (n:Attribute) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ts_id IF NOT EXISTS "
    "FOR (n:TabularSection) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT form_id IF NOT EXISTS "
    "FOR (n:Form) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ev_id IF NOT EXISTS "
    "FOR (n:EnumValue) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT type_id IF NOT EXISTS "
    "FOR (n:Type) REQUIRE n.id IS UNIQUE",
    # Уникальный fingerprint (одна служебная нода)
    "CREATE CONSTRAINT fp_kind IF NOT EXISTS "
    "FOR (n:Fingerprint) REQUIRE n.kind IS UNIQUE",
    # Слой 2: код. См. CODE_LAYER_LABELS ниже.
    "CREATE CONSTRAINT callable_id IF NOT EXISTS "
    "FOR (n:Callable)  REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT param_id IF NOT EXISTS "
    "FOR (n:Parameter) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT callsite_id IF NOT EXISTS "
    "FOR (n:CallSite)  REQUIRE n.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX meta_full_name_eng IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.full_name_eng)",
    "CREATE INDEX meta_full_name_ru  IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.full_name_ru)",
    "CREATE INDEX meta_name          IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.name)",
    "CREATE INDEX meta_kind_eng      IF NOT EXISTS "
    "FOR (n:MetadataObject) ON (n.kind_eng)",
    "CREATE INDEX type_kind_target   IF NOT EXISTS "
    "FOR (n:Type) ON (n.kind, n.target)",
    # Слой 2.
    "CREATE INDEX callable_full_name IF NOT EXISTS "
    "FOR (n:Callable) ON (n.full_name)",
    "CREATE INDEX callable_module_id IF NOT EXISTS "
    "FOR (n:Callable) ON (n.module_id)",
    "CREATE INDEX callable_name      IF NOT EXISTS "
    "FOR (n:Callable) ON (n.name)",
]


# PERF-6. Полнотекстовые индексы для поиска по именам и синонимам.
#
# `metadata_search` ищет через `toLower(n.name) CONTAINS toLower($q)`.
# Подстрочный поиск не использует обычный индекс — это полный перебор, — но
# на 16 тысячах узлов терпимо по скорости. Проблема не в скорости, а в
# качестве: `CONTAINS` не ранжирует. Подстрока либо есть, либо нет, и
# «Контрагенты» окажется в выдаче рядом с «ВводОстатковПоКонтрагентам»
# без всякого признака, что первое релевантнее.
#
# Полнотекстовый индекс даёт score и морфологию токенов. Держим ОБА пути:
# индекс может отсутствовать (граф собран старым индексером, у пользователя
# Community-версия без нужной процедуры), и тогда поиск обязан продолжать
# работать через CONTAINS, а не падать.
#
# Индексы создаются отдельно от обычных: синтаксис другой, и на версиях без
# поддержки они молча пропускаются, а не роняют ensure_schema.
FULLTEXT_INDEXES = [
    ("meta_fulltext",
     "CREATE FULLTEXT INDEX meta_fulltext IF NOT EXISTS "
     "FOR (n:MetadataObject) ON EACH [n.name, n.synonym, n.full_name_ru]"),
    ("callable_fulltext",
     "CREATE FULLTEXT INDEX callable_fulltext IF NOT EXISTS "
     "FOR (n:Callable) ON EACH [n.name, n.full_name]"),
]


def ensure_fulltext_indexes(neo: Neo4j) -> dict[str, bool]:
    """
    Создаёт полнотекстовые индексы. Возвращает {имя: удалось ли}.

    Неудача НЕ является ошибкой: поиск умеет работать и без них (см.
    PERF-6). Поэтому здесь warning, а не исключение — иначе индексация
    боевой конфигурации падала бы на версии Neo4j, где нет полнотекста.
    """
    result: dict[str, bool] = {}
    for name, cypher in FULLTEXT_INDEXES:
        try:
            neo.query(cypher)
            result[name] = True
        except RuntimeError as e:
            if "EquivalentSchemaRule" in str(e) or "already exists" in str(e).lower():
                result[name] = True
                continue
            log.warning(
                "Полнотекстовый индекс %s не создан: %s. Поиск продолжит "
                "работать через CONTAINS — медленнее и без ранжирования.",
                name, e,
            )
            result[name] = False
    return result


def ensure_schema(neo: Neo4j) -> None:
    for c in CONSTRAINTS:
        try:
            neo.query(c)
        except RuntimeError as e:
            # Старые версии Neo4j отдают warning'и как errors — игнорируем
            # "EquivalentSchemaRuleAlreadyExists" и подобные.
            if "EquivalentSchemaRule" not in str(e):
                log.warning("constraint failed: %s", e)
    for i in INDEXES:
        try:
            neo.query(i)
        except RuntimeError as e:
            if "EquivalentSchemaRule" not in str(e):
                log.warning("index failed: %s", e)
    ensure_fulltext_indexes(neo)   # PERF-6


# ─── Fingerprint ──────────────────────────────────────────────────────────


# PERF-3. Два режима подсчёта fingerprint'а.
#
#   FP_MODE_STAT    — по кортежу (путь, размер, mtime_ns). Режим по умолчанию.
#   FP_MODE_CONTENT — по sha256 содержимого. Прежнее поведение, включается
#                     флагом METADATA_FINGERPRINT_STRICT=true.
#
# Почему сменился умолчательный режим. На боевой выгрузке 56 410 файлов и
# 1,2 ГБ, и честная сумма по содержимому считалась 12 минут ПРИ КАЖДОМ
# запуске индексера — включая запуски, где не менялось ничего. Узкое место
# не в самом хешировании, а в файловых операциях: около 26 мс на файл на
# виндовом bind-mount. Открывать и читать гигабайт ради ответа на вопрос
# «изменилось ли хоть что-нибудь» — избыточно: (размер, mtime) отвечает на
# него столь же надёжно в 99% случаев и стоит одного stat на файл.
#
# Оставшийся 1% — копирование выгрузки утилитой, сохраняющей mtime, при
# котором содержимое отличается, а размер совпал. Ровно для него и оставлен
# строгий режим. Способ подсчёта пишется в лог и в свойство `mode` узла
# :Fingerprint, чтобы «почему не переиндексировалось» имело ответ в логе, а
# не в чьей-то памяти.
FP_MODE_STAT = "stat"
FP_MODE_CONTENT = "content"


def _sha256_file(p: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _iter_files(root: Path):
    """
    Рекурсивный обход `root` через os.scandir, отдаёт (relpath_posix, DirEntry).

    Почему не `Path.rglob`: rglob создаёт объект Path на каждый элемент и не
    переиспользует результат stat, который ядро уже отдало при чтении
    каталога. На 56 тысячах файлов разница заметна. `DirEntry.stat()`
    кеширован внутри самого DirEntry — второй вызов бесплатен.

    Симлинки не разыменовываются: выгрузка 1С их не содержит, а переход по
    ним даёт риск зациклиться.
    """
    root_str = str(root)
    stack = [(root_str, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as e:            # нет прав, каталог исчез на ходу
            log.warning("fingerprint: каталог недоступен, пропускаем: %s (%s)",
                        current, e)
            continue
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, rel + "/"))
                elif entry.is_file(follow_symlinks=False):
                    yield rel, entry
            except OSError:             # файл исчез между scandir и stat
                continue


def fingerprint_workspace_multi(
    root: Path,
    suffixes: Iterable[str] = (".xml", ".bsl"),
    strict: bool = False,
) -> tuple[dict[str, str], dict]:
    """
    Считает fingerprint'ы сразу по нескольким расширениям за ОДИН обход дерева.

    Раньше индексер вызывал `fingerprint_workspace_files` дважды — по .xml и
    по .bsl, — то есть дважды обходил те же 56 тысяч файлов. Здесь обход
    один, файлы раскладываются по расширениям на лету.

    Возвращает `({suffix: hexdigest}, meta)`, где meta содержит:
      mode          — FP_MODE_STAT | FP_MODE_CONTENT
      files         — сколько файлов вошло в подсчёт (по всем расширениям)
      bytes         — суммарный размер этих файлов
      elapsed_sec   — сколько занял подсчёт
      by_suffix     — {suffix: количество файлов}
      newest_mtime  — максимальный mtime среди учтённых файлов (для диагностики
                      случая «скопировали выгрузку, mtime у всех одинаковый»)
    """
    t0 = time.monotonic()
    norm = [(s if s.startswith(".") else "." + s).lower() for s in suffixes]
    items: dict[str, list[str]] = {s: [] for s in norm}
    total_bytes = 0
    newest_mtime = 0.0

    for rel, entry in _iter_files(root):
        low = rel.lower()
        for suf in norm:
            if low.endswith(suf):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                total_bytes += st.st_size
                newest_mtime = max(newest_mtime, st.st_mtime)
                if strict:
                    items[suf].append(f"{rel}\t{_sha256_file(Path(entry.path))}")
                else:
                    # st_mtime_ns, а не st_mtime: float-секунды на некоторых
                    # ФС теряют точность до 2 с, и правка, сделанная быстрее,
                    # осталась бы незамеченной.
                    items[suf].append(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")
                break

    mode = FP_MODE_CONTENT if strict else FP_MODE_STAT
    digests = {}
    for suf, lines in items.items():
        lines.sort()
        h = hashlib.sha256("\n".join(lines).encode("utf-8"))
        digests[suf] = h.hexdigest()

    meta = {
        "mode":         mode,
        "files":        sum(len(v) for v in items.values()),
        "bytes":        total_bytes,
        "elapsed_sec":  time.monotonic() - t0,
        "by_suffix":    {s: len(v) for s, v in items.items()},
        "newest_mtime": newest_mtime,
    }
    return digests, meta


def fingerprint_workspace_files(root: Path, suffix: str, strict: bool = False) -> str:
    """
    Fingerprint по одному расширению. Обёртка над `fingerprint_workspace_multi`.

    Сигнатура сохранена ради существующих вызовов; `strict=True` возвращает
    прежнее поведение (sha256 по содержимому).
    """
    digests, _ = fingerprint_workspace_multi(root, [suffix], strict=strict)
    key = suffix if suffix.startswith(".") else "." + suffix
    return digests[key.lower()]


def fingerprint_workspace(root: Path, strict: bool = False) -> str:
    """Backward-compat: fingerprint по всем XML. См. fingerprint_workspace_files."""
    return fingerprint_workspace_files(root, ".xml", strict=strict)


def fingerprint_get(neo: Neo4j, kind: str = "metadata_xml") -> Optional[str]:
    rows = neo.rows(
        "MATCH (n:Fingerprint {kind: $kind}) RETURN n.value AS v",
        {"kind": kind},
    )
    return rows[0]["v"] if rows else None


def fingerprint_get_meta(neo: Neo4j, kind: str = "metadata_xml") -> Optional[dict]:
    """
    Читает fingerprint вместе со способом подсчёта.

    Возвращает `{"value": str, "mode": str}` или None, если узла нет.
    У узлов, записанных до PERF-3, свойства `mode` нет — считаем их
    посчитанными по содержимому, потому что так и было.
    """
    rows = neo.rows(
        "MATCH (n:Fingerprint {kind: $kind}) RETURN n.value AS v, n.mode AS m",
        {"kind": kind},
    )
    if not rows:
        return None
    return {"value": rows[0]["v"], "mode": rows[0]["m"] or FP_MODE_CONTENT}


def fingerprint_matches(
    old: Optional[dict], new_value: str, new_mode: str,
) -> tuple[bool, str]:
    """
    Сравнивает сохранённый fingerprint с новым. Возвращает (совпал, причина).

    Значения, посчитанные разными способами, несравнимы: сумма по содержимому
    и сумма по (размер, mtime) — разные величины, и их несовпадение ничего не
    говорит об изменении файлов. Поэтому смена режима трактуется как «не
    совпал» с отдельной формулировкой: одна переиндексация после смены
    METADATA_FINGERPRINT_STRICT ожидаема и не является дефектом.
    """
    if old is None:
        return False, "fingerprint отсутствует — первая индексация"
    if old.get("mode") != new_mode:
        return False, (
            f"способ подсчёта fingerprint сменился ({old.get('mode')} → {new_mode}) — "
            f"значения несравнимы, переиндексация один раз ожидаема"
        )
    if old.get("value") != new_value:
        return False, (
            f"fingerprint изменился ({(old.get('value') or '')[:8]}… → {new_value[:8]}…)"
        )
    return True, "fingerprint совпал"


def fingerprint_write(neo: Neo4j, value: str, kind: str = "metadata_xml",
                      mode: str = FP_MODE_CONTENT) -> None:
    neo.query(
        """
        MERGE (n:Fingerprint {kind: $kind})
        SET n.value = $value, n.mode = $mode, n.updated_at = timestamp()
        """,
        {"kind": kind, "value": value, "mode": mode},
    )


# ─── PERF-12: снимок счётчиков рёбер ─────────────────────────────────────
#
# Почему снимок, а не подсчёт по запросу. `metadata_stats` отдаёт табличку
# «сколько рёбер каждого типа». Считалась она запросом `MATCH ()-[r]->()
# RETURN type(r), count(*)` — без указания типа, то есть обходом всех
# 2,3 млн рёбер ради тринадцати строк. Замер 18 августа: `neo4j_ms` = 1032
# при том, что остальные восемь счётчиков берутся из счётчиков хранилища и
# стоят копейки.
#
# Отвергнутая версия — перебор `db.relationshipTypes()` с поштучным счётом.
# Дешевле, но меняет ответ: типы, у которых рёбер не осталось, начнут
# приходить нулями. Менять форму ответа ради скорости — ровно то, за что
# `PERF-6.1` поймал сам себя.
#
# Поэтому: обход остаётся, но платит за него индексация — один раз за
# прогон, там, где рядом и так идут минуты. `metadata_stats` читает готовое
# за O(1) и говорит, когда снимок снят: устаревший снимок, о возрасте
# которого известно, честнее свежего числа, за которое платит каждый вызов.
#
# ─── PERF-12, остаток ────────────────────────────────────────────────────
#
# После первой правки `neo4j_ms` упал с 4705 до 642 мс — цель в 200 мс не
# была взята. Остаток локализован: `sum(CASE WHEN cs.resolved …)` обходит
# 722 206 узлов `:CallSite`, потому что предикат ПО СВОЙСТВУ счётчиками
# хранилища не берётся: `count(:CallSite)` бесплатен, а «сколько из них с
# resolved = true» — нет.
#
# Довод тот же, что был для рёбер, и он не про скорость. Числа про резолв
# меняются РОВНО ТОГДА, когда идёт индексация: между прогонами узлы
# `:CallSite` никто не трогает. Значит, каждый вызов `metadata_stats`
# пересчитывал то, что не менялось со вчера.
#
# Кладём их в тот же узел снимка, вторым свойством. Не в `data`: там
# словарь «тип ребра → число», и подмешивать в него ключи чужой природы
# значило бы сломать форму ответа ради экономии на одном свойстве.
# Отдельный узел тоже не заводим — читатель у обоих один и тот же запрос,
# и лишний узел стоил бы лишнего похода.
RELATIONS_SNAPSHOT_KIND = "relation_counts"

RELATION_COUNTS_CYPHER = (
    "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC"
)

RELATIONS_SNAPSHOT_WRITE_CYPHER = """
MERGE (n:Fingerprint {kind: $kind})
SET n.data = $data, n.callsites = $callsites, n.updated_at = timestamp()
"""


def _callsite_counts_cypher() -> str:
    """
    Запрос про резолв. Список причин берётся из `bsl_resolver` — там его
    смысловой оригинал (`FIX-4`), и вторая копия в этом файле была бы
    шестым рукописным списком проекта.

    Импорт ленивый: `graph_writer` умышленно не тянет резолвер на импорте
    — его зовут и из мест, где резолвера в образе может не быть.
    """
    from bsl_resolver import NON_CONFIG_CALL_REASONS
    reasons = "[" + ", ".join(f"'{r}'" for r in sorted(NON_CONFIG_CALL_REASONS)) + "]"
    return (
        "MATCH (cs:CallSite) RETURN "
        "sum(CASE WHEN cs.resolved = true THEN 1 ELSE 0 END) AS resolved, "
        "sum(CASE WHEN cs.resolved = false THEN 1 ELSE 0 END) AS unresolved, "
        "sum(CASE WHEN cs.resolved = false AND cs.reason IN "
        f"{reasons} THEN 1 ELSE 0 END) AS object_method"
    )


def callsite_counts(neo: Neo4j) -> dict[str, int]:
    """Три числа про резолв вызовов. Один обход `:CallSite`, не три."""
    rows = neo.rows(_callsite_counts_cypher())
    row = rows[0] if rows else {}
    return {k: int(row.get(k) or 0)
            for k in ("resolved", "unresolved", "object_method")}


def relations_snapshot_write(neo: Neo4j) -> dict[str, int]:
    """
    Пересчитывает рёбра по типам и кладёт результат в служебный узел.

    Возвращает саму табличку — вызывающему она обычно нужна для лога.

    Узел `:Fingerprint {kind: 'relation_counts'}`: метка уже имеет
    констрейнт на `kind`, то есть индекс, и не удаляется ни одной из
    очисток слоя. Заводить ради этого новую метку значило бы добавить в
    схему сущность, которую потом надо помнить при каждой чистке.

    Вместе с рёбрами снимаются числа про резолв вызовов (PERF-12,
    остаток) — они меняются в тот же момент и по той же причине.
    """
    rows = neo.rows(RELATION_COUNTS_CYPHER)
    counts = {r["rel"]: r["cnt"] for r in rows if r.get("rel")}
    try:
        cs = callsite_counts(neo)
    except Exception as e:  # снимок резолва — удобство, а не условие успеха
        log.warning("Числа резолва в снимок не попали (%s) — metadata_stats "
                    "посчитает их сам, обходом :CallSite", e)
        cs = {}
    neo.query(RELATIONS_SNAPSHOT_WRITE_CYPHER, {
        "kind": RELATIONS_SNAPSHOT_KIND,
        "data": json.dumps(counts, ensure_ascii=False, separators=(",", ":")),
        "callsites": (json.dumps(cs, ensure_ascii=False, separators=(",", ":"))
                      if cs else None),
    })
    return counts


# ─── Очистка слоя ─────────────────────────────────────────────────────────


META_LAYER_LABELS = (
    "MetadataObject", "Attribute", "TabularSection", "Form",
    "EnumValue", "Type",
)

# Слой 2 (call graph): :Callable + конкретная метка :Procedure/:Function,
# :Parameter, :CallSite. :MetadataObject:Module формально остаётся в слое 1
# (это структурная единица), стирается через clear_metadata_layer.
CODE_LAYER_LABELS = (
    "Callable", "Procedure", "Function", "Parameter", "CallSite",
)


# FIX-13. Размер порции при удалении слоя.
#
# История дефекта. Обе очистки делали `MATCH (n) WHERE ... DETACH DELETE n`
# ОДНИМ запросом, то есть одной транзакцией. Neo4j держит в памяти весь
# набор удаляемого до коммита, и на боевой конфигурации транзакция вышла
# на 1,3 млн узлов и 2,29 млн рёбер:
#
#   Neo.TransientError.General.MemoryPoolOutOfMemoryError
#
# Падение случилось в самом конце фазы 2, после 5,3 часа сборки графа
# вызовов, — вся работа была выброшена, слой кода не записан, fingerprint
# не сохранён. Дефект не воспроизводится ни на каком объёме меньше боевого,
# поэтому и дожил до этого прогона.
#
# Почему цикл с LIMIT, а не `CALL { … } IN TRANSACTIONS`. Второе выглядит
# уместнее, но исполняется только в неявной транзакции, а мы ходим в базу
# через HTTP-эндпоинт /tx/commit, который в части версий Neo4j считается
# явной. Получилась бы замена одного отказа на другой, зато более редкий и
# зависящий от версии. Цикл с LIMIT работает везде и одинаково.
DELETE_BATCH_DEFAULT = 10000


def _delete_by_labels(neo: Neo4j, labels: Iterable[str], batch: int,
                      what: str) -> int:
    """
    Порционно удаляет узлы с указанными метками. Возвращает число удалённых.

    Обход идёт по одной метке за раз (`MATCH (n:Метка)`), а не общим
    `WHERE 'Метка' IN labels(n)`: первое использует сканирование по метке,
    второе перебирает все узлы базы на каждую порцию. При 130 порциях
    разница уже не косметическая.

    Узел с несколькими метками (:Callable:Procedure) удалится на первой из
    них; на второй просто не найдётся, поэтому сумма по меткам — это число
    узлов, а не срабатываний.
    """
    total = 0
    prog = ProgressLogger(log, f"удаление {what}", every_sec=15.0,
                          check_every=1, unit="узл")
    for label in labels:
        while True:
            # Метку параметризировать нельзя — она из списка констант модуля.
            rows = neo.rows(
                f"MATCH (n:{label}) WITH n LIMIT $batch "
                f"DETACH DELETE n RETURN count(*) AS deleted",
                {"batch": batch},
            )
            deleted = rows[0]["deleted"] if rows else 0
            if not deleted:
                break
            total += deleted
            prog.step(deleted)
    if total >= PROGRESS_MIN_ITEMS:
        prog.done()
    return total


def clear_metadata_layer(neo: Neo4j, batch: int = DELETE_BATCH_DEFAULT) -> dict:
    """
    Удаляет только узлы слоя метаданных. Узлы графа вызовов (:Procedure,
    :Function, :Parameter), а также :Fingerprint остаются.

    Возвращает {'deleted_nodes': N}.
    """
    n = _delete_by_labels(neo, META_LAYER_LABELS, batch, "слоя 1")
    return {"deleted_nodes": n}


def clear_code_layer(neo: Neo4j, batch: int = DELETE_BATCH_DEFAULT) -> dict:
    """
    Удаляет только узлы слоя вызовов (:Callable + :Parameter + :CallSite).
    Узлы слоя 1 (:MetadataObject и потомки) НЕ затрагиваются.
    :Fingerprint НЕ затрагивается.

    ВАЖНО: :MetadataObject:Module-узлы НЕ удаляются здесь — они формально
    слой 1 (имеют label :MetadataObject), но пишутся в фазе 2. Их сносит
    clear_metadata_layer (при переиндексации XML фаза 2 пересоздаст).

    Возвращает {'deleted_nodes': N}.
    """
    n = _delete_by_labels(neo, CODE_LAYER_LABELS, batch, "слоя 2")
    return {"deleted_nodes": n}


# ─── Запись узлов и рёбер ─────────────────────────────────────────────────


def _chunks(seq, size):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# PERF-5. Порог, ниже которого писатель узлов молчит. На малой конфигурации
# (69 объектов) прогресс-строки только зашумляют лог; на боевой этап записи
# идёт минутами, и без них снаружи не отличить работу от зависания.
PROGRESS_MIN_ITEMS = 5000


def _node_progress(label: str, total: int, enabled: bool = True):
    if not enabled or total < PROGRESS_MIN_ITEMS:
        return None
    return ProgressLogger(log, label, total=total, every_sec=20.0,
                          check_every=1, unit="узл")


# FIX-15. Сколько строк РЕАЛЬНО легло в базу.
#
# Все писатели узлов и рёбер возвращали `len(rows)` — число строк, которые
# они отправили, а не которые записались. Разница не теоретическая: любой
# `MATCH … MERGE` на ненайденном узле молча пропускает строку. Так был
# потерян 69 % рёбер :HAS_METHOD (FIX-14), и лог при этом бодро отчитался
# о полном успехе. Дефект нашёлся только сверкой счётчиков с базой вручную
# — то есть его могло не быть найдено вовсе.
#
# `RETURN count(*)` после MERGE считает строки, дошедшие до записи. Для
# рёбер с дедупликацией (:CALLS, :OPERATES_ON — MERGE по паре узлов) это
# по-прежнему число обработанных строк, а не созданных связей: именно то,
# что нужно, чтобы отличить «схлопнулось по замыслу» от «не нашло узел».
#
# ─── FIX-31. Одного числа мало: их три ───────────────────────────────────
#
# 18 августа запрос перепривязки отчитался `relinked = 83` при 99
# фактически созданных рёбрах. `FIX-29` — тот же дефект зеркально: отчёт
# больше факта на единицу. Оба объясняются одним: `RETURN count(*)` НЕ
# отвечает на вопрос «сколько связей создано». Он отвечает на вопрос
# «сколько строк дошло до записи» — а это другое число, и расходятся они в
# обе стороны:
#
#   меньше факта  — `MATCH (c) MATCH (m) MERGE …` порождает строку на
#                   ПАРУ, и планировщик вправе схлопнуть повторы источника
#                   раньше, чем дойдёт до счётчика;
#   больше факта  — `MERGE` по паре узлов схлопывает одинаковые пары уже
#                   в базе (428 918 вызовов → 351 694 ребра :CALLS).
#
# Отсюда правило: числа три, и каждое отвечает на свой вопрос.
#
#   sent     сколько строк отправлено         — знает вызывающий
#   matched  сколько дошло до записи          — `RETURN count(*)`
#   created  сколько связей реально создано   — статистика транзакции
#
# Сторож `FIX-15` меряет `sent − matched`: это строки, не нашедшие узлов,
# ровно тот класс, которым потерялись 158 961 ребро `HAS_METHOD`. Подменить
# ему число на `created` нельзя — повторная запись тех же рёбер честно даёт
# `created = 0`, и сторож завопил бы на здоровом графе.
#
# Почему статистика транзакции, а не два способа из PLAN-9. `ON CREATE SET
# r._new = true` точен, но требует второго прохода и оставляет служебное
# свойство на миллионах рёбер. `count(r)` до и после дешевле, но верен лишь
# при однопоточной записи — то есть работает, пока никто не забыл, почему
# он работает. `includeStats` не требует ни того, ни другого: число даёт
# сама база, по факту записи, в ответе того же запроса.
#
# Если версия базы (или стаб в тестах) статистику не прислала, `created`
# равен None. Это не ноль: «не создано ничего» и «нечем измерить» —
# разные ответы, и путать их значило бы завести третий способ соврать.


class _Written(NamedTuple):
    sent: int
    matched: int
    created: Optional[int]


def _write_counted(neo: Neo4j, cypher: str, rows: list) -> _Written:
    """Выполняет запись и возвращает три числа: отправлено / дошло / создано."""
    counted = cypher + " RETURN count(*) AS written"
    stats: dict = {}
    with_stats = getattr(neo, "rows_with_stats", None)
    if callable(with_stats):
        res, stats = with_stats(counted, {"rows": rows})
    else:
        # Стаб или экзотический драйвер: работаем как до FIX-31.
        res = neo.rows(counted, {"rows": rows})

    if res and res[0].get("written") is not None:
        matched = int(res[0]["written"])
    else:
        # Счётчика нет — не выдумываем недостачу там, где её нечем измерить.
        matched = len(rows)

    created = stats.get("relationships_created")
    return _Written(len(rows), matched,
                    None if created is None else int(created))


def _query_written(neo: Neo4j, cypher: str, rows: list) -> int:
    """Число строк, дошедших до записи. Узловые писатели меряют только его."""
    return _write_counted(neo, cypher, rows).matched


# Тонкая обёртка над общим правилом: своя подсказка, свой логгер.
# Тело переехало в shortfall.warn_shortfall — второй копии правила в наборе
# быть не должно, это ровно тот жанр, из-за которого разошлись
# gen_lockfiles.sh и .ps1.
def _warn_shortfall(what: str, sent: int, written: int) -> None:
    _shortfall(
        what, sent, written, log=log,
        hint="Строки не нашли узлов: это почти всегда несовпадение меток "
             "или id между слоями; сверьте запрос в EDGE_QUERIES с тем, "
             "какие метки реально висят на узлах (см. FIX-14).",
    )


def _safe_label(s: str) -> str:
    """Очищаем kind_eng для использования как метки Neo4j (буквы/цифры/_)."""
    out = []
    for ch in s or "":
        if ch.isalnum() or ch == "_":
            out.append(ch)
    return "".join(out) or "MetadataObject"


def write_meta_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500,
                     log_progress: bool = True) -> int:
    """
    Узлы метаобъектов. Двойная метка :MetadataObject + :<KindEng> (Catalog,
    Document, ...). Дополнительная метка :KindRu (Справочник, Документ) для
    backwards compatibility с metadata_search старого формата.
    """
    # Группируем по kind_eng — для одного CALL apoc-free дин-метки в Cypher
    # нельзя. Решение: один UNWIND-запрос на kind.
    by_kind: dict[str, list[dict]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind_eng"], []).append(n)

    prog = _node_progress("узлы :MetadataObject", len(nodes), log_progress)
    total = 0
    for kind_eng, group in by_kind.items():
        label_eng = _safe_label(kind_eng)
        # Метку kind_ru тоже навешиваем
        kind_ru = group[0].get("kind_ru") or ""
        label_ru = _safe_label(kind_ru)
        # Cypher: метки задаются на этапе компиляции, поэтому формируем
        # строку запроса под каждую группу.
        labels = "MetadataObject"
        if label_eng and label_eng != "MetadataObject":
            labels += f":{label_eng}"
        if label_ru and label_ru not in (label_eng, "MetadataObject"):
            labels += f":{label_ru}"

        cypher = (
            f"UNWIND $rows AS r "
            f"MERGE (n:{labels} {{id: r.id}}) "
            f"SET n.name           = r.name, "
            f"    n.synonym        = r.synonym, "
            f"    n.comment        = r.comment, "
            f"    n.uuid           = r.uuid, "
            f"    n.kind_eng       = r.kind_eng, "
            f"    n.kind_ru        = r.kind_ru, "
            f"    n.kind_ru_plural = r.kind_ru_plural, "
            f"    n.full_name_eng  = r.full_name_eng, "
            f"    n.full_name_ru   = r.full_name_ru, "
            f"    n.source_xml     = r.source_xml, "
            f"    n.properties_json = r.properties_json, "
            f"    n.attributes_json = r.attributes_json"
        )
        for chunk in _chunks(group, batch):
            rows = []
            for n in chunk:
                # attributes_json — старый формат, нужен для metadata_object_details
                # (поле сохраняем для обратной совместимости).
                attrs_compat = []
                for a in n.get("_attrs_for_compat", []):
                    attrs_compat.append({
                        "name":    a["name"],
                        "synonym": a.get("synonym", ""),
                        "type":    a.get("type_compat", ""),
                        "category": {"attribute": "Реквизиты",
                                     "dimension": "Измерения",
                                     "resource":  "Ресурсы"}.get(a.get("role"), "Реквизиты"),
                    })
                rows.append({
                    "id":             n["id"],
                    "name":           n["name"],
                    "synonym":        n.get("synonym", ""),
                    "comment":        n.get("comment", ""),
                    "uuid":           n.get("uuid", ""),
                    "kind_eng":       n["kind_eng"],
                    "kind_ru":        n.get("kind_ru", ""),
                    "kind_ru_plural": n.get("kind_ru_plural", ""),
                    "full_name_eng":  n["full_name_eng"],
                    "full_name_ru":   n["full_name_ru"],
                    "source_xml":     n.get("source_xml", ""),
                    "properties_json": json.dumps(n.get("properties", {}), ensure_ascii=False),
                    "attributes_json": json.dumps(attrs_compat, ensure_ascii=False),
                })
            total += _query_written(neo, cypher, rows)
            if prog:
                prog.step(len(rows))
    if prog:
        prog.done()
    return total


def write_attribute_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500,
                          log_progress: bool = True) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Attribute {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, "
        "    n.role = r.role, n.is_master = r.is_master, "
        "    n.indexing = r.indexing, n.parent = r.parent"
    )
    prog = _node_progress("узлы :Attribute", len(nodes), log_progress)
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":        n["id"],
            "name":      n["name"],
            "synonym":   n.get("synonym", ""),
            "role":      n.get("role", "attribute"),
            "is_master": n.get("is_master", False),
            "indexing":  n.get("indexing", ""),
            "parent":    n["parent"],
        } for n in chunk]
        total += _query_written(neo, cypher, rows)
        if prog:
            prog.step(len(rows))
    if prog:
        prog.done()
    return total


def write_tabular_section_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:TabularSection {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "synonym": n.get("synonym", ""), "parent": n["parent"]}
                for n in chunk]
        total += _query_written(neo, cypher, rows)
    return total


def write_form_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Form {id: r.id}) "
        "SET n.name = r.name, n.is_main = r.is_main, "
        "    n.main_kind = r.main_kind, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "is_main": n.get("is_main", False),
                 "main_kind": n.get("main_kind", ""),
                 "parent": n["parent"]} for n in chunk]
        total += _query_written(neo, cypher, rows)
    return total


def write_enum_value_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:EnumValue {id: r.id}) "
        "SET n.name = r.name, n.synonym = r.synonym, n.parent = r.parent"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "name": n["name"],
                 "synonym": n.get("synonym", ""), "parent": n["parent"]}
                for n in chunk]
        total += _query_written(neo, cypher, rows)
    return total


def write_type_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Type {id: r.id}) "
        "SET n.kind = r.kind, n.target = r.target"
    )
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{"id": n["id"], "kind": n["kind"], "target": n.get("target")}
                for n in chunk]
        total += _query_written(neo, cypher, rows)
    return total


# Карта rel-имени → Cypher для создания ребра. APOC недоступен, поэтому
# на каждый тип ребра — свой запрос с фиксированным именем.
EDGE_QUERIES: dict[str, Any] = {
    # PERF-4. Значением может быть либо строка (один запрос на тип ребра),
    # либо словарь `метка источника → запрос`. Второе нужно там, где ребро
    # выходит из узлов с разными метками.
    #
    # История дефекта. Здесь стояло `MATCH (a {id: r.src})` — без метки,
    # намеренно, потому что :HAS_ATTRIBUTE идёт и от :MetadataObject, и от
    # :TabularSection. Но все констрейнты в схеме привязаны к меткам, а
    # значит, и все индексы по `id`. Матч без метки индекс использовать не
    # может и вырождается в полный перебор узлов на КАЖДУЮ строку UNWIND.
    #
    # Цена на боевой конфигурации: 42 762 ребра :HAS_ATTRIBUTE (треть слоя 1)
    # против 71 219 узлов в базе. Слой 1 писался 28 минут — 110 элементов в
    # секунду, при том что слой 2 на той же Neo4j пишется со скоростью около
    # 5 100. Разница в сорок раз объясняется целиком этой строкой.
    "HAS_ATTRIBUTE": {
        "MetadataObject": (
            "UNWIND $rows AS r "
            "MATCH (a:MetadataObject {id: r.src}), (b:Attribute {id: r.dst}) "
            "MERGE (a)-[e:HAS_ATTRIBUTE]->(b) "
            "SET e.role = r.role"
        ),
        "TabularSection": (
            "UNWIND $rows AS r "
            "MATCH (a:TabularSection {id: r.src}), (b:Attribute {id: r.dst}) "
            "MERGE (a)-[e:HAS_ATTRIBUTE]->(b) "
            "SET e.role = r.role"
        ),
    },
    "HAS_TABULAR_SECTION": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:TabularSection {id: r.dst}) "
        "MERGE (a)-[:HAS_TABULAR_SECTION]->(b)"
    ),
    "HAS_FORM": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:Form {id: r.dst}) "
        "MERGE (a)-[e:HAS_FORM]->(b) "
        "SET e.is_main = r.is_main, e.main_kind = r.main_kind"
    ),
    "HAS_VALUE": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:EnumValue {id: r.dst}) "
        "MERGE (a)-[:HAS_VALUE]->(b)"
    ),
    "OF_TYPE": (
        "UNWIND $rows AS r "
        "MATCH (a:Attribute {id: r.src}), (b:Type {id: r.dst}) "
        "MERGE (a)-[:OF_TYPE]->(b)"
    ),
    "RESOLVES_TO": (
        "UNWIND $rows AS r "
        "MATCH (a:Type {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:RESOLVES_TO]->(b)"
    ),
    "CONTAINS": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:CONTAINS]->(b)"
    ),
    "PARENT_OF": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:PARENT_OF]->(b)"
    ),
    "OWNED_BY": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:OWNED_BY]->(b)"
    ),
    "BASED_ON": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:BASED_ON]->(b)"
    ),
    "REGISTERS": (
        "UNWIND $rows AS r "
        "MATCH (a:MetadataObject {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[:REGISTERS]->(b)"
    ),
    # ─── Слой 2 (call graph) ─────────────────────────────────────────
    # FIX-14. Источник :HAS_METHOD — модуль, и метки у модулей РАЗНЫЕ.
    #
    # История дефекта. Здесь стояло `MATCH (m:MetadataObject {id: r.src})`
    # одним запросом на все модули. Общие модули, модули объектов и модули
    # менеджеров действительно :MetadataObject — первые приходят из слоя 1,
    # вторые и третьи создаёт write_module_nodes с этой меткой. А вот модуль
    # формы — это узел :Form, которому фаза 2 лишь ДОПИСЫВАЕТ метку :Module
    # (write_form_nodes создаёт форму как `MERGE (n:Form {id: r.id})`, без
    # :MetadataObject). Под `MATCH (m:MetadataObject …)` он не подходит
    # никогда.
    #
    # Цена на боевой конфигурации: из 231 114 рёбер :HAS_METHOD записалось
    # 72 152. Все 158 961 процедуры модулей форм остались без владельца —
    # 69 % слоя кода потеряло связь «объект метаданных → его методы».
    # Ошибки при этом не было ни одной: `MATCH … MERGE` на ненайденном
    # источнике молча пропускает строку, а счётчик в логе считает рёбра на
    # входе. Нашлось только сверкой счётчиков с базой.
    "HAS_METHOD": {
        "MetadataObject": (
            "UNWIND $rows AS r "
            "MATCH (m:MetadataObject {id: r.src}), (c:Callable {id: r.dst}) "
            "MERGE (m)-[e:HAS_METHOD]->(c) "
            "SET e.kind = r.kind"  # 'procedure' | 'function'
        ),
        "Form": (
            "UNWIND $rows AS r "
            "MATCH (m:Form {id: r.src}), (c:Callable {id: r.dst}) "
            "MERGE (m)-[e:HAS_METHOD]->(c) "
            "SET e.kind = r.kind"
        ),
    },
    "HAS_PARAM": (
        "UNWIND $rows AS r "
        "MATCH (c:Callable {id: r.src}), (p:Parameter {id: r.dst}) "
        "MERGE (c)-[e:HAS_PARAM]->(p) "
        "SET e.position = r.position"
    ),
    "CALLS": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:Callable {id: r.dst}) "
        "MERGE (a)-[e:CALLS]->(b) "
        "SET e.line = r.line, e.callsite = r.callsite"
    ),
    "CALL_SITE": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:CallSite {id: r.dst}) "
        "MERGE (a)-[:CALL_SITE]->(b)"
    ),
    "RESOLVES_TO_CALLEE": (
        "UNWIND $rows AS r "
        "MATCH (a:CallSite {id: r.src}), (b:Callable {id: r.dst}) "
        "MERGE (a)-[:RESOLVES_TO_CALLEE]->(b)"
    ),
    "OPERATES_ON": (
        "UNWIND $rows AS r "
        "MATCH (a:Callable {id: r.src}), (b:MetadataObject {id: r.dst}) "
        "MERGE (a)-[e:OPERATES_ON]->(b) "
        "SET e.via = r.via, e.access = r.access"
    ),
    "INFERRED_TYPE": (
        # Пока не используется (задел на 4.6.4 inter-procedural).
        "UNWIND $rows AS r "
        "MATCH (a:Parameter {id: r.src}), (b:Type {id: r.dst}) "
        "MERGE (a)-[e:INFERRED_TYPE]->(b) "
        "SET e.confidence = r.confidence, e.source = r.source"
    ),
}


# PERF-4. Когда у ребра несколько вариантов запроса, а `src_label` на ребре
# не проставлен (старый вызывающий код), метку приходится выводить из id.
# Единственный такой случай сейчас — :HAS_ATTRIBUTE, где id реквизита ТЧ
# строится как "<объект>.TS.<имя ТЧ>.<имя реквизита>" (см. metadata_xml).
def _infer_src_label(rel: str, src_id: str, variants: dict) -> str:
    if rel == "HAS_ATTRIBUTE":
        return "TabularSection" if ".TS." in (src_id or "") else "MetadataObject"
    if rel == "HAS_METHOD":
        # id модуля формы: "<Вид>.<Объект>.Form.<ИмяФормы>" (см. bsl_parser).
        return "Form" if ".Form." in (src_id or "") else "MetadataObject"
    return next(iter(variants))


def write_edges(neo: Neo4j, edges: list[dict], batch: int = 500,
                log_progress: bool = True,
                report: Optional[dict] = None) -> dict[str, int]:
    """
    Запись всех рёбер. Возвращает счётчик по типам рёбер.

    Группировка идёт по паре (тип ребра, метка источника): у типов с
    несколькими вариантами запроса — по одному UNWIND-запросу на метку,
    см. EDGE_QUERIES и PERF-4. Счётчик в ответе по-прежнему сводится к типу
    ребра, чтобы вызывающий код и тесты не заметили разницы.

    `report` (FIX-31) — необязательный словарь, в который складывается вся
    правда: `{тип ребра: {"sent": N, "matched": N, "created": N|None}}`.
    Отдельным аргументом, а не возвращаемым значением, ровно по одной
    причине: возвращаемое значение читают тринадцать мест, и менять его
    форму ради числа, которое нужно двум, — это менять ответ ради
    удобства писателя.
    """
    by_key: dict[tuple[str, str], list[dict]] = {}
    unknown: dict[str, int] = {}
    inferred_labels = 0

    for e in edges:
        rel = e["rel"]
        q = EDGE_QUERIES.get(rel)
        if q is None:
            unknown[rel] = unknown.get(rel, 0) + 1
            continue
        if isinstance(q, dict):
            label = e.get("src_label") or ""
            if label not in q:
                label = _infer_src_label(rel, e.get("src", ""), q)
                inferred_labels += 1
        else:
            label = ""
        by_key.setdefault((rel, label), []).append(e)

    for rel, n in unknown.items():
        log.warning("Неизвестный тип ребра, пропускаем: %s (%d шт)", rel, n)
    if inferred_labels:
        log.debug("write_edges: метка источника выведена из id для %d рёбер",
                  inferred_labels)

    counters: dict[str, int] = {}
    for (rel, label), group in by_key.items():
        q = EDGE_QUERIES[rel]
        cypher = q[label] if isinstance(q, dict) else q
        name = f"{rel}:{label}" if label else rel

        prog = ProgressLogger(
            log, f"рёбра {name}", total=len(group), every_sec=20.0,
            check_every=1, unit="реб",
        ) if log_progress else None

        written = 0
        created: Optional[int] = 0
        for chunk in _chunks(group, batch):
            rows = []
            for e in chunk:
                row = {"src": e["src"], "dst": e["dst"]}
                row.update(e.get("props") or {})
                rows.append(row)
            w = _write_counted(neo, cypher, rows)
            written += w.matched
            # Одна неизмеренная порция делает неизмеренной всю группу:
            # сумма из «созданных» и «неизвестно скольких» — это число,
            # которое выглядит точным и им не является.
            created = None if (created is None or w.created is None) \
                else created + w.created
            if prog:
                prog.step(len(rows))

        if prog:
            # Финальную строку печатаем только на заметных группах, иначе
            # лог малой конфигурации утонет в отчётах о десяти рёбрах.
            if len(group) >= 5000 or prog.elapsed >= 5.0:
                prog.done()
        _warn_shortfall(f"рёбра {name}", len(group), written)
        _report_merge_dedup(name, group, written, created)
        counters[rel] = counters.get(rel, 0) + written
        if report is not None:
            slot = report.setdefault(
                rel, {"sent": 0, "matched": 0, "created": 0})
            slot["sent"] += len(group)
            slot["matched"] += written
            slot["created"] = None if (slot["created"] is None or created is None) \
                else slot["created"] + created
    return counters


# FIX-29. Расхождение на единицу: писатель отчитался `HAS_METHOD: 231 102`,
# в базе оказалось 231 101.
#
# Потерянной строки за этим нет. `_query_written` считает строки, ДОШЕДШИЕ
# до записи, а `MERGE` по паре узлов схлопывает одинаковые пары: две строки
# с одним и тем же (src, dst) дают одно ребро. Для `:CALLS` и `:OPERATES_ON`
# это давно известно и объявлено (428 918 вызовов → 351 694 ребра), а вот
# для `HAS_METHOD` дубль — событие: он означает, что в модуле дважды
# объявлена процедура с одним именем, либо два файла претендуют на один
# `module_id`.
#
# Правило поэтому такое: не «подогнать счётчик», а НАЗВАТЬ дубли. Счётчик,
# который не сходится, — это почти всегда непонятое правило, и лечится оно
# формулировкой правила, а не вычитанием.
def _report_merge_dedup(name: str, group: list, written: int,
                        created: Optional[int] = None) -> None:
    """Сколько строк схлопнется в базе из-за MERGE по паре — вслух.

    `created` (FIX-31) — сколько связей база создала на самом деле. Пока
    его не было, строка «в базе будет N» оставалась предсказанием, и
    проверить её было нечем: ровно поэтому расхождение на 16 % прожило до
    ручной сверки. Теперь предсказание печатается рядом с фактом, и если
    они разошлись — это говорится, а не подгоняется.
    """
    pairs = set()
    dups: list[str] = []
    for e in group:
        key = (e.get("src"), e.get("dst"))
        if key in pairs:
            if len(dups) < 3:
                dups.append(f"{key[0]} → {key[1]}")
        else:
            pairs.add(key)
    duplicates = len(group) - len(pairs)
    if not duplicates:
        return
    tail = f" (напр. {', '.join(dups)})" if dups else ""
    predicted = written - duplicates
    log.info(
        "  рёбра %s: строк %d, из них дублей по паре %d — MERGE схлопнет их, "
        "в базе будет %d%s",
        name, len(group), duplicates, predicted, tail,
    )
    # Часть рёбер могла существовать до записи — тогда `created` меньше
    # предсказания законно. Тревожно обратное: создано БОЛЬШЕ, чем строк
    # после схлопывания. Это и есть `relinked = 83 при 99`.
    if created is not None and created > predicted:
        log.warning(
            "  ⚠ рёбра %s: создано %d при предсказанных %d — счётчик строк "
            "меньше факта. Опираться надо на created (FIX-31)",
            name, created, predicted,
        )


# ─── FIX-31: как читать отчёт write_edges ────────────────────────────────


def edges_created(report: dict) -> dict[str, Optional[int]]:
    """`{тип ребра: сколько связей создано}` — или None там, где не измерено."""
    return {rel: slot.get("created") for rel, slot in (report or {}).items()}


def log_edge_report(report: dict, out: Optional[logging.Logger] = None) -> None:
    """
    Строка в лог там, где отправленное, дошедшее и созданное разошлись.

    Молчит, когда все три числа совпали, — по тому же правилу, что и
    `_report_merge_dedup`: отчёт, который печатается всегда, перестают
    читать. Но молчание здесь означает именно «сошлось», а не «не
    считали»: неизмеренная группа (`created is None`) о себе говорит.
    """
    log_ = out or log
    for rel in sorted(report or {}):
        slot = report[rel]
        sent, matched, created = slot["sent"], slot["matched"], slot["created"]
        if created is None:
            log_.info("  рёбра %s: отправлено %d, дошло %d, создано — не "
                      "измерено (база не прислала статистику транзакции)",
                      rel, sent, matched)
        elif not (sent == matched == created):
            log_.info("  рёбра %s: отправлено %d, дошло %d, создано %d",
                      rel, sent, matched, created)


# ─── Пишет конфигурационный узел ──────────────────────────────────────────


def write_configuration_node(neo: Neo4j, name: str, stats: dict) -> None:
    neo.query(
        "MERGE (c:Configuration {name: $name}) "
        "SET c.objects = $objects, c.edges = $edges, c.updated_at = timestamp()",
        {"name": name, "objects": stats["meta_objects"],
         "edges": stats["edges_total"]},
    )


# ─── Слой 2: write-функции (Module/Callable/Parameter/CallSite) ──────────


def write_module_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500) -> int:
    """
    Узлы-модули для крепления :Callable. Две существенно разных ситуации:

    1) Form (module_role="Form"). Узел УЖЕ существует в графе 1 как
       :MetadataObject:Form с тем же id (например, Catalog.X.Form.Y). Здесь
       просто навешиваем метку :Module и SET BSL-специфичные свойства.
       `MERGE` с новой меткой :Module сломал бы constraint form_id IS UNIQUE.

    2) ObjectModule / ManagerModule (и другие будущие роли). Узлов в графе 1
       нет — создаём новые с MERGE по id и набором меток
       :MetadataObject:Module:<Role>.

    CommonModule пропускается на стороне индексера (узел из графа 1 уже
    содержит :CommonModule, нам ничего добавлять не надо).
    """
    by_role: dict[str, list[dict]] = {}
    for n in nodes:
        role = n.get("module_role", "Module")
        by_role.setdefault(role, []).append(n)

    total = 0
    for role, group in by_role.items():
        label_role = _safe_label(role)
        if role == "Form":
            # Узел уже существует с метками :MetadataObject:Form (id уникален
            # по constraint form_id). MATCH по :Form гарантирует, что мы
            # обновляем именно тот узел, а не создаём дубль.
            cypher = (
                "UNWIND $rows AS r "
                "MATCH (n:Form {id: r.id}) "
                "SET n:Module, "
                "    n.module_role        = r.module_role, "
                "    n.parent_metadata_id = r.parent_metadata_id, "
                "    n.source_path        = r.source_path, "
                "    n.is_server          = r.is_server, "
                "    n.is_client          = r.is_client"
                # name / kind_eng / full_name_eng не трогаем — фаза 1 их уже
                # задала. SET name → пустой бы стёр осмысленное «ФормаЭлемента».
            )
        else:
            # Новый узел — :MetadataObject:Module:<Role>.
            labels = "MetadataObject:Module"
            if label_role and label_role != "Module":
                labels += f":{label_role}"
            cypher = (
                f"UNWIND $rows AS r "
                f"MERGE (n:{labels} {{id: r.id}}) "
                f"SET n.name             = r.name, "
                f"    n.kind_eng         = r.kind_eng, "
                f"    n.module_role      = r.module_role, "
                f"    n.parent_metadata_id = r.parent_metadata_id, "
                f"    n.source_path      = r.source_path, "
                f"    n.is_server        = r.is_server, "
                f"    n.is_client        = r.is_client, "
                f"    n.full_name_eng    = r.full_name_eng"
            )

        for chunk in _chunks(group, batch):
            rows = []
            for n in chunk:
                rows.append({
                    "id":                  n["id"],
                    "name":                n["name"],
                    "kind_eng":            n.get("kind_eng", "Module"),
                    "module_role":         n.get("module_role", "Module"),
                    "parent_metadata_id":  n.get("parent_metadata_id"),
                    "source_path":         n.get("source_path", ""),
                    "is_server":           bool(n.get("is_server", False)),
                    "is_client":           bool(n.get("is_client", False)),
                    "full_name_eng":       n.get("full_name_eng", n["id"]),
                })
            total += _query_written(neo, cypher, rows)
    return total


def write_callable_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500,
                         log_progress: bool = True) -> int:
    """
    :Callable-узлы. Двойная метка :Callable:Procedure / :Callable:Function
    задаётся через поле `kind` ('Procedure' | 'Function').
    """
    by_kind: dict[str, list[dict]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind"], []).append(n)

    prog = _node_progress("узлы :Callable", len(nodes), log_progress)

    total = 0
    for kind, group in by_kind.items():
        label = _safe_label(kind)  # "Procedure" | "Function"
        labels = "Callable"
        if label and label != "Callable":
            labels += f":{label}"
        cypher = (
            f"UNWIND $rows AS r "
            f"MERGE (n:{labels} {{id: r.id}}) "
            f"SET n.name        = r.name, "
            f"    n.full_name   = r.full_name, "
            f"    n.module_id   = r.module_id, "
            f"    n.kind        = r.kind, "
            f"    n.is_export   = r.is_export, "
            f"    n.directive   = r.directive, "
            f"    n.line_start  = r.line_start, "
            f"    n.line_end    = r.line_end, "
            f"    n.source_path = r.source_path"
        )
        for chunk in _chunks(group, batch):
            rows = [{
                "id":          n["id"],
                "name":        n["name"],
                "full_name":   n.get("full_name", n["id"]),
                "module_id":   n["module_id"],
                "kind":        n["kind"],
                "is_export":   bool(n.get("is_export", False)),
                "directive":   n.get("directive", ""),
                "line_start":  int(n.get("line_start", 0)),
                "line_end":    int(n.get("line_end", 0)),
                "source_path": n.get("source_path", ""),
            } for n in chunk]
            total += _query_written(neo, cypher, rows)
            if prog:
                prog.step(len(rows))
    if prog:
        prog.done()
    return total


def write_parameter_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500,
                          log_progress: bool = True) -> int:
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:Parameter {id: r.id}) "
        "SET n.name           = r.name, "
        "    n.position       = r.position, "
        "    n.is_by_value    = r.is_by_value, "
        "    n.has_default    = r.has_default, "
        "    n.default_value  = r.default_value, "
        "    n.callable_id    = r.callable_id"
    )
    prog = _node_progress("узлы :Parameter", len(nodes), log_progress)
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":            n["id"],
            "name":          n["name"],
            "position":      int(n.get("position", 0)),
            "is_by_value":   bool(n.get("is_by_value", False)),
            "has_default":   bool(n.get("has_default", False)),
            "default_value": n.get("default_value", ""),
            "callable_id":   n["callable_id"],
        } for n in chunk]
        total += _query_written(neo, cypher, rows)
        if prog:
            prog.step(len(rows))
    if prog:
        prog.done()
    return total


def write_callsite_nodes(neo: Neo4j, nodes: list[dict], batch: int = 500,
                         log_progress: bool = True) -> int:
    """
    :CallSite-узлы.

    `resolved` (bool) и `reason` (текст) проставляются резолвером. Если
    callsite разрешён в callee, рёбра :CALLS и :RESOLVES_TO_CALLEE пишутся
    отдельно через write_edges (см. EDGE_QUERIES).
    """
    cypher = (
        "UNWIND $rows AS r "
        "MERGE (n:CallSite {id: r.id}) "
        "SET n.caller_id   = r.caller_id, "
        "    n.module_ref  = r.module_ref, "
        "    n.method_name = r.method_name, "
        "    n.line        = r.line, "
        "    n.col         = r.col, "
        "    n.resolved    = r.resolved, "
        "    n.reason      = r.reason"
    )
    prog = _node_progress("узлы :CallSite", len(nodes), log_progress)
    total = 0
    for chunk in _chunks(nodes, batch):
        rows = [{
            "id":          n["id"],
            "caller_id":   n["caller_id"],
            "module_ref":  n.get("module_ref", ""),
            "method_name": n["method_name"],
            "line":        int(n.get("line", 0)),
            "col":         int(n.get("col", 0)),
            "resolved":    bool(n.get("resolved", False)),
            "reason":      n.get("reason", ""),
        } for n in chunk]
        total += _query_written(neo, cypher, rows)
        if prog:
            prog.step(len(rows))
    if prog:
        prog.done()
    return total


def write_code_graph(neo: Neo4j, code_graph: dict) -> dict:
    """
    Пишет полный слой 2 (call graph) из bsl_resolver.build_call_graph().

    Ожидаемый формат `code_graph`:
      {
        "module_nodes":   [...],   # :MetadataObject:Module узлы
        "callable_nodes": [...],   # :Callable узлы
        "parameter_nodes": [...],  # :Parameter узлы
        "callsite_nodes": [...],   # :CallSite узлы
        "type_nodes":     [...],   # :Type узлы слоя 2 (4.6.4) — MERGE по id,
                                   #   переиспользуют :Type слоя 1 где совпадает id
        "edges":          [...],   # рёбра с rel ∈ HAS_METHOD, HAS_PARAM, CALLS,
                                   #   ..., INFERRED_TYPE (4.6.4)
        "stats":          {...},
      }

    Строгий порядок: сначала Module-узлы (т.к. :HAS_METHOD ссылается на них),
    затем Callable, Parameter, CallSite, Type, потом всё остальное через
    write_edges (включая :INFERRED_TYPE — ему нужны и :Parameter, и :Type).
    """
    ensure_schema(neo)

    def _timed(name: str, fn, nodes: list) -> int:
        t = time.monotonic()
        n = fn(neo, nodes)
        log.info("  %s: %d за %s", name, n, human_sec(time.monotonic() - t))
        _warn_shortfall(f"узлы {name}", len(nodes), n or 0)
        return n

    n_module    = _timed(":Module",    write_module_nodes,
                         code_graph.get("module_nodes", []))
    n_callable  = _timed(":Callable",  write_callable_nodes,
                         code_graph.get("callable_nodes", []))
    n_parameter = _timed(":Parameter", write_parameter_nodes,
                         code_graph.get("parameter_nodes", []))
    n_callsite  = _timed(":CallSite",  write_callsite_nodes,
                         code_graph.get("callsite_nodes", []))
    # 4.6.4: :Type-узлы слоя 2. MERGE по id — если узел уже есть из слоя 1
    # (XML-фаза пишет ссылочные типы реквизитов), он переиспользуется, а не
    # дублируется. Недостающие типы (например CatalogObject, которого слой 1
    # мог не писать) — досоздаются. Должны быть записаны ДО write_edges, т.к.
    # :INFERRED_TYPE матчит (:Parameter)-(:Type).
    n_type      = _timed(":Type",      write_type_nodes,
                         code_graph.get("type_nodes", []))

    t_edges = time.monotonic()
    edge_report: dict = {}
    edge_counters = write_edges(neo, code_graph.get("edges", []),
                                report=edge_report)
    log.info("  рёбра: %d за %s", len(code_graph.get("edges", [])),
             human_sec(time.monotonic() - t_edges))
    log_edge_report(edge_report)

    return {
        "nodes_written": {
            "Module":    n_module,
            "Callable":  n_callable,
            "Parameter": n_parameter,
            "CallSite":  n_callsite,
            "Type":      n_type,
        },
        "edges_written": edge_counters,
        # FIX-31: сколько связей база создала на самом деле. Отдельным
        # ключом, а не подменой прежнего: `edges_written` отвечает на
        # вопрос «сколько строк дошло», и он тоже нужен — по нему видно
        # строки, не нашедшие узлов.
        "edges_created": edges_created(edge_report),
        "stats": code_graph.get("stats", {}),
    }


# ─── Главная функция записи ───────────────────────────────────────────────


def write_graph(neo: Neo4j, graph: dict, config_name: str = "Конфигурация",
                build_compat_attrs: bool = True) -> dict:
    """
    Пишет полный граф из build_graph() в Neo4j.

    Если build_compat_attrs=True, attribute_nodes используются для построения
    attributes_json на :MetadataObject (обратная совместимость с metadata_object_details).
    """
    stats = graph["stats"]
    log.info("write_graph: %d meta + %d attrs + %d ts + %d forms + %d ev + %d types; %d edges",
             stats["meta_objects"], stats["attributes"], stats["tabular_sections"],
             stats["forms"], stats["enum_values"], stats["type_nodes"], stats["edges_total"])

    # Готовим compat-атрибуты на узлах метаобъектов
    if build_compat_attrs:
        # Соберём строку типа для compat-формата (как в старом indexer.py).
        # При composite — соединяем через '; '.
        type_kind_to_compat = {  # обратный маппинг
            "CatalogRef":                  "СправочникСсылка",
            "DocumentRef":                 "ДокументСсылка",
            "EnumRef":                     "ПеречислениеСсылка",
            "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
            "ChartOfAccountsRef":          "ПланСчетовСсылка",
            "ChartOfCalculationTypesRef":  "ПланВидовРасчетаСсылка",
            "BusinessProcessRef":          "БизнесПроцессСсылка",
            "TaskRef":                     "ЗадачаСсылка",
            "ExchangePlanRef":             "ПланОбменаСсылка",
            "DocumentJournalRef":          "ЖурналДокументовСсылка",
            "CatalogObject":               "СправочникОбъект",
            "DocumentObject":              "ДокументОбъект",
            "String": "Строка", "Number": "Число", "Date": "Дата",
            "Boolean": "Булево", "UUID": "УникальныйИдентификатор",
            "ValueStorage": "ХранилищеЗначения",
            "Reference": "Ссылка", "Unknown": "",
        }
        type_node_by_id = {t["id"]: t for t in graph["type_nodes"]}
        # of_type для каждого attr
        of_type_by_attr: dict[str, list[str]] = {}
        for e in graph["edges"]:
            if e["rel"] == "OF_TYPE":
                of_type_by_attr.setdefault(e["src"], []).append(e["dst"])
        # компиляция compat-строки типа
        def compat_type(attr_id: str) -> str:
            type_ids = of_type_by_attr.get(attr_id, [])
            parts = []
            for tid in type_ids:
                t = type_node_by_id.get(tid)
                if not t: continue
                ru = type_kind_to_compat.get(t["kind"], t["kind"])
                if t.get("target"):
                    # "Catalog.X" → "Х" (имя без префикса)
                    short = t["target"].split(".", 1)[-1]
                    parts.append(f"{ru}.{short}" if ru else short)
                else:
                    parts.append(ru)
            return "; ".join([p for p in parts if p])
        # Прицепим compat-атрибуты к meta_nodes
        attr_by_parent: dict[str, list[dict]] = {}
        for a in graph["attr_nodes"]:
            ac = dict(a)
            ac["type_compat"] = compat_type(a["id"])
            attr_by_parent.setdefault(a["parent"], []).append(ac)
        # Сольём ТЧ-реквизиты в attributes_json родительского объекта
        # (старый indexer хранил их в tabular_sections_json, но это для нас сейчас
        # лишняя сложность — оставим _attrs_for_compat пустым для родителей ТЧ,
        # т.е. для самих TS-узлов attributes_json не сохраним. metadata_object_details
        # их и не показывает в attributes-блоке).
        for n in graph["meta_nodes"]:
            attrs = attr_by_parent.get(n["id"], [])
            n["_attrs_for_compat"] = [a for a in attrs if a.get("role") != "_internal"]

    ensure_schema(neo)

    # PERF-5: этап записи слоя 1 на боевой конфигурации шёл 28 минут одной
    # немой строкой. Тайминг по каждому виду узлов — это то, чем PERF-4 был
    # найден: видно не «медленно вообще», а какой именно кусок медленный.
    def _timed(name: str, fn, nodes: list) -> None:
        t = time.monotonic()
        written = fn(neo, nodes)
        log.info("  %s: %d за %s", name, written, human_sec(time.monotonic() - t))
        _warn_shortfall(f"узлы {name}", len(nodes), written or 0)

    _timed(":Type",           write_type_nodes,            graph["type_nodes"])
    _timed(":MetadataObject", write_meta_nodes,            graph["meta_nodes"])
    _timed(":Attribute",      write_attribute_nodes,       graph["attr_nodes"])
    _timed(":TabularSection", write_tabular_section_nodes, graph["ts_nodes"])
    _timed(":Form",           write_form_nodes,            graph["form_nodes"])
    _timed(":EnumValue",      write_enum_value_nodes,      graph["enum_value_nodes"])

    t_edges = time.monotonic()
    edge_report: dict = {}
    edge_counters = write_edges(neo, graph["edges"], report=edge_report)
    log.info("  рёбра: %d за %s", len(graph["edges"]),
             human_sec(time.monotonic() - t_edges))
    log_edge_report(edge_report)

    write_configuration_node(neo, config_name, stats)

    return {
        "nodes_written": {
            "MetadataObject": stats["meta_objects"],
            "Attribute":      stats["attributes"],
            "TabularSection": stats["tabular_sections"],
            "Form":           stats["forms"],
            "EnumValue":      stats["enum_values"],
            "Type":           stats["type_nodes"],
        },
        "edges_written":   edge_counters,
        "edges_created":   edges_created(edge_report),
        "stats":           stats,
    }
