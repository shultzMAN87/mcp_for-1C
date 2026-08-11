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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from progress_log import ProgressLogger, human_sec

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

    def query(self, cypher: str, parameters: Optional[dict] = None) -> dict:
        payload = json.dumps({
            "statements": [{
                "statement": cypher,
                "parameters": parameters or {},
            }]
        }).encode()
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

    def rows(self, cypher: str, parameters: Optional[dict] = None) -> list[dict]:
        r = self.query(cypher, parameters)
        cols = r["results"][0].get("columns", [])
        out = []
        for data in r["results"][0].get("data", []):
            out.append({c: data["row"][i] for i, c in enumerate(cols)})
        return out

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
def _query_written(neo: Neo4j, cypher: str, rows: list) -> int:
    """Выполняет запись и возвращает число обработанных строк."""
    try:
        res = neo.rows(cypher + " RETURN count(*) AS written", {"rows": rows})
    except Exception:
        raise
    if res and res[0].get("written") is not None:
        return int(res[0]["written"])
    # Стаб или экзотический драйвер, не вернувший счётчик: не выдумываем
    # недостачу там, где её нечем измерить.
    return len(rows)


def _warn_shortfall(what: str, sent: int, written: int) -> None:
    if written >= sent:
        return
    log.warning(
        "%s: записано %d из %d — %d строк не нашли узлов и пропущены молча. "
        "Это почти всегда несовпадение меток или id между слоями; "
        "сверьте запрос в EDGE_QUERIES с тем, какие метки реально висят "
        "на узлах (см. FIX-14).",
        what, written, sent, sent - written,
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
                log_progress: bool = True) -> dict[str, int]:
    """
    Запись всех рёбер. Возвращает счётчик по типам рёбер.

    Группировка идёт по паре (тип ребра, метка источника): у типов с
    несколькими вариантами запроса — по одному UNWIND-запросу на метку,
    см. EDGE_QUERIES и PERF-4. Счётчик в ответе по-прежнему сводится к типу
    ребра, чтобы вызывающий код и тесты не заметили разницы.
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
        for chunk in _chunks(group, batch):
            rows = []
            for e in chunk:
                row = {"src": e["src"], "dst": e["dst"]}
                row.update(e.get("props") or {})
                rows.append(row)
            written += _query_written(neo, cypher, rows)
            if prog:
                prog.step(len(rows))

        if prog:
            # Финальную строку печатаем только на заметных группах, иначе
            # лог малой конфигурации утонет в отчётах о десяти рёбрах.
            if len(group) >= 5000 or prog.elapsed >= 5.0:
                prog.done()
        _warn_shortfall(f"рёбра {name}", len(group), written)
        counters[rel] = counters.get(rel, 0) + written
    return counters


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
    edge_counters = write_edges(neo, code_graph.get("edges", []))
    log.info("  рёбра: %d за %s", len(code_graph.get("edges", [])),
             human_sec(time.monotonic() - t_edges))

    return {
        "nodes_written": {
            "Module":    n_module,
            "Callable":  n_callable,
            "Parameter": n_parameter,
            "CallSite":  n_callsite,
            "Type":      n_type,
        },
        "edges_written": edge_counters,
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
    edge_counters = write_edges(neo, graph["edges"])
    log.info("  рёбра: %d за %s", len(graph["edges"]),
             human_sec(time.monotonic() - t_edges))

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
        "stats":           stats,
    }
