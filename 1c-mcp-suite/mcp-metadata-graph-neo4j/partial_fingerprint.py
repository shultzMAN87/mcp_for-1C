"""
PERF-8 — частичный fingerprint: что именно изменилось в выгрузке.
==================================================================

Задача. Сейчас fingerprint — одна сумма на всю выгрузку: изменился хоть
один байт, переиндексируется весь слой. Правка одного общего модуля стоит
столько же, сколько полная замена конфигурации, — около двух часов. На
вопрос «мне опять ждать два часа?» ответ был «да», и это неправильный
ответ.

Идея. Считать сумму не на выгрузку целиком, а **на каждый объект
метаданных и каждый модуль отдельно**. Тогда сравнение двух состояний даёт
не «изменилось / не изменилось», а конкретный список: что добавлено, что
изменено, что удалено. Дальше по этому списку работает точечное обновление
из `incremental.py`.

Чего это НЕ ускоряет. Обход дерева остаётся: чтобы узнать, что изменилось,
надо посмотреть на все файлы. Это те же 3–14 минут `stat` на виндовом
bind-mount, что и сейчас, — не лучше и не хуже. Экономия начинается после
обхода: вместо полной пересборки идёт работа только по изменившимся
единицам.

Граница честности. Слой 2 (граф вызовов) собирается ГЛОБАЛЬНЫМ фикс-пойнтом:
тип параметра в модуле A зависит от вызова в модуле B. Точечное обновление
такой сходимости не даёт — см. шапку `incremental.py`. Поэтому частичный
режим оставляет граф локально верным и глобально слегка отстающим, и это
надо не прятать, а измерять: сколько накоплено «долга» и когда пора
прогнать полную сборку. Для этого здесь считается `stale_debt`.

Где хранится состояние. В самом графе, на узлах, которые уже есть:
`:MetadataObject.fp` для объекта, `:Module.fp` для модуля. Отдельная
таблица файлов была бы третьим источником правды, который рано или поздно
разойдётся с графом; узел, которого нет в графе, — это ровно объект,
которого нет в индексе, и наоборот.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from bsl_parser import classify_bsl_path
from metadata_xml import KIND_BY_DIR
from progress_log import ProgressLogger

log = logging.getLogger(__name__)

# Ключ для файлов, которые не принадлежат ни объекту, ни модулю:
# Configuration.xml, DefinedTypes на верхнем уровне и прочее корневое.
# Их изменение затрагивает конфигурацию целиком, поэтому такой ключ —
# сигнал «частичным обновлением не обойтись».
ROOT_KEY = "_root"


def owner_of(rel_path: str) -> Optional[str]:
    """
    Кому принадлежит файл: id объекта метаданных, id модуля или ROOT_KEY.

    Возвращает None для файлов, которые индексатор всё равно не читает
    (картинки, макеты в бинарном виде и т. п.) — их изменение не должно
    вызывать переиндексацию.

    Разбор путей не дублируется: используются те же `classify_bsl_path` и
    таблица `KINDS`, что и в самом индексаторе. Своя копия правил рано или
    поздно разошлась бы с настоящей — ровно тот класс дефектов, который
    Заход 4 разбирал пять раз подряд.
    """
    rel = rel_path.replace("\\", "/")
    parts = rel.split("/")
    if parts and parts[0] == "tests-extension":
        parts = parts[1:]
    if not parts:
        return None

    low = rel.lower()

    # ─ BSL: модуль ─
    if low.endswith(".bsl"):
        info = classify_bsl_path(rel)
        return info[0] if info else None

    if not low.endswith(".xml"):
        return None

    # ─ Верхнеуровневый XML объекта: Catalogs/Контрагенты.xml ─
    if len(parts) == 2 and parts[0] in KIND_BY_DIR:
        return f"{KIND_BY_DIR[parts[0]][1]}.{parts[1][:-4]}"

    # ─ Вложенный XML внутри объекта: Catalogs/Контрагенты/Forms/…/Form.xml ─
    #
    # Принадлежит РОДИТЕЛЬСКОМУ объекту, а не сам по себе: формы и макеты
    # описаны в верхнем XML, отдельными объектами не являются. Их правка
    # обязана перестраивать родителя — иначе изменение реквизитов формы
    # (а их читает резолвер, см. FIX-4.1) осталось бы незамеченным.
    if len(parts) > 2 and parts[0] in KIND_BY_DIR:
        return f"{KIND_BY_DIR[parts[0]][1]}.{parts[1]}"

    # ─ Корневые файлы конфигурации ─
    if len(parts) == 1:
        return ROOT_KEY

    return None


def scan_workspace(root: Path, keep_files: bool = False) -> tuple[dict[str, str], dict]:
    """
    Один обход дерева → отпечаток на каждого владельца.

    Возвращает `({owner_id: sha256}, meta)`. Стоимость — ровно один `stat`
    на файл, как у обычного fingerprint: обход общий, а не дополнительный.

    `keep_files=True` дополнительно кладёт в meta раскладку
    `{owner_id: [relpath, …]}`. Нужна применению плана: чтобы обновить
    изменившуюся единицу, надо знать её файлы, а второй обход ради этого
    стоил бы ещё три минуты. 53 785 путей в памяти — несколько мегабайт,
    несопоставимо дешевле.
    """
    t0 = time.monotonic()
    per_owner: dict[str, list[str]] = {}
    files = skipped = 0
    total_bytes = 0

    stack = [(str(root), "")]
    while stack:
        current, prefix = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as e:
            log.warning("частичный fingerprint: каталог недоступен: %s (%s)",
                        current, e)
            continue
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, rel + "/"))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                low = rel.lower()
                if not (low.endswith(".xml") or low.endswith(".bsl")):
                    continue
                owner = owner_of(rel)
                if owner is None:
                    skipped += 1
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            files += 1
            total_bytes += st.st_size
            per_owner.setdefault(owner, []).append(
                f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")

    digests = {}
    for owner, lines in per_owner.items():
        lines.sort()
        digests[owner] = hashlib.sha256(
            "\n".join(lines).encode("utf-8")).hexdigest()

    meta = {
        "owners":      len(digests),
        "files":       files,
        "skipped":     skipped,
        "bytes":       total_bytes,
        "elapsed_sec": time.monotonic() - t0,
    }
    if keep_files:
        meta["files_by_owner"] = {
            owner: sorted(line.split("\t", 1)[0] for line in lines)
            for owner, lines in per_owner.items()
        }
    return digests, meta


def dependent_owners(owner: str, all_owners: Iterable[str]) -> list[str]:
    """
    Кого ещё придётся перестроить вместе с этой единицей.

    Обновление XML объекта сносит его срез целиком — вместе с узлами его
    модулей (`_clear_meta_object_slice` удаляет всё по префиксу id). Значит
    после перестройки объекта надо заново залить и его модули, даже если
    сами модули не менялись. Без этого правка одного реквизита справочника
    молча уносила бы весь его код из графа.

    Для модуля зависимых нет: модуль обновляется сам по себе.
    """
    if "." not in owner:
        return []
    # Модуль? Тогда зависимых нет.
    tail = owner.rsplit(".", 1)[-1]
    if tail in ("ObjectModule", "ManagerModule") or ".Form." in owner:
        return []
    if owner.startswith("CommonModule."):
        return []
    prefix = owner + "."
    return sorted(o for o in all_owners if o.startswith(prefix))


class ChangePlan:
    """
    Что делать с выгрузкой: список добавленного, изменённого и удалённого.

    `full_reindex_reason` заполнен, когда частичным обновлением обойтись
    нельзя. Отдельное поле, а не исключение: вызывающий должен видеть и
    причину, и сам список — по нему понятно, почему решение такое.
    """

    def __init__(self, added: set[str], changed: set[str], removed: set[str],
                 reason: Optional[str] = None):
        self.added = added
        self.changed = changed
        self.removed = removed
        self.full_reindex_reason = reason

    @property
    def total(self) -> int:
        return len(self.added) + len(self.changed) + len(self.removed)

    @property
    def touched(self) -> set[str]:
        return self.added | self.changed | self.removed

    def as_dict(self) -> dict:
        return {
            "added":   sorted(self.added),
            "changed": sorted(self.changed),
            "removed": sorted(self.removed),
            "total":   self.total,
            "full_reindex_reason": self.full_reindex_reason,
        }

    def summary(self) -> str:
        if self.full_reindex_reason:
            return f"полная переиндексация: {self.full_reindex_reason}"
        if not self.total:
            return "изменений нет"
        return (f"изменений {self.total}: добавлено {len(self.added)}, "
                f"изменено {len(self.changed)}, удалено {len(self.removed)}")


def build_plan(current: dict[str, str], stored: dict[str, str],
               max_partial: int = 0) -> ChangePlan:
    """
    Сравнивает текущее состояние с сохранённым.

    `max_partial` — порог, выше которого частичное обновление теряет смысл:
    если поменялась половина конфигурации, полная пересборка и быстрее, и
    даёт корректный глобальный резолв. 0 — без порога.

    Пустое сохранённое состояние — не «всё добавлено», а «мы ничего не
    знаем»: первый прогон обязан быть полным, иначе слой 2 соберётся из
    несвязанных кусков.
    """
    if not stored:
        return ChangePlan(set(), set(), set(),
                          reason="сохранённого состояния нет — первая индексация")

    cur_keys, old_keys = set(current), set(stored)
    added = cur_keys - old_keys
    removed = old_keys - cur_keys
    changed = {k for k in (cur_keys & old_keys) if current[k] != stored[k]}

    if ROOT_KEY in (added | changed):
        return ChangePlan(added, changed, removed,
                          reason="изменились корневые файлы конфигурации")

    plan = ChangePlan(added, changed, removed)
    if max_partial and plan.total > max_partial:
        plan.full_reindex_reason = (
            f"изменилось {plan.total} единиц при пороге {max_partial} — "
            f"полная пересборка быстрее и даёт корректный глобальный резолв")
    return plan


# ─── Хранение состояния в графе ───────────────────────────────────────────


# ─── Хранение: один блок JSON, а не свойство на каждом узле ──────────────
#
# Как было и почему не сработало. Отпечаток каждого владельца писался
# свойством `fp` на его собственный узел. Звучало красиво — «граф сам себе
# реестр», — но означало 24 795 поисков по индексу на каждое сохранение.
# Первая версия делала это ещё и запросом без метки (PERF-4 в третий раз),
# вторая — с метками, но всё равно упиралась в таймаут: 500 строк не
# уложились в 600 секунд при том, что чтение отвечает за 0,1 с.
#
# Ошибка была не в запросе, а в решении. Я выбрал схему по признаку
# «элегантно», не посчитав, во что она обходится. 24 795 обращений к базе
# ради данных, которые нужны целиком и всегда сразу, — это не реестр, это
# перебор.
#
# Как стало. Весь набор отпечатков — один JSON, разложенный по нескольким
# служебным узлам :Fingerprint. Чтение — один запрос, запись — несколько
# (по числу кусков). Метка :Fingerprint уже имеет констрейнт на `kind`,
# то есть индекс; ничего нового в схему не добавляется.
#
# Что теряется. Отпечаток больше не исчезает сам вместе с удалённым узлом.
# Это и не нужно: удаление определяется сравнением ключей набора с текущим
# обходом — ровно так работает build_plan, — а не наличием узла.

FP_CHUNK_PREFIX = "partial_owners_"

# Владельцев на один узел. 5 000 записей — это около 600 КБ JSON:
# достаточно крупно, чтобы кусков было мало, и достаточно скромно, чтобы
# не упереться в лимиты payload и в память при разборе.
FP_CHUNK_SIZE = 5000

READ_CHUNKS_CYPHER = """
MATCH (n:Fingerprint) WHERE n.kind STARTS WITH $prefix
RETURN n.kind AS kind, n.data AS data
ORDER BY n.kind
"""

WRITE_CHUNK_CYPHER = """
MERGE (n:Fingerprint {kind: $kind})
SET n.data = $data, n.updated_at = timestamp()
RETURN count(*) AS written
"""

DROP_EXTRA_CHUNKS_CYPHER = """
MATCH (n:Fingerprint) WHERE n.kind STARTS WITH $prefix AND NOT n.kind IN $keep
DELETE n
RETURN count(*) AS dropped
"""


def read_stored(neo) -> dict[str, str]:
    """Отпечатки, сохранённые при прошлом прогоне. Один запрос."""
    rows = neo.rows(READ_CHUNKS_CYPHER, {"prefix": FP_CHUNK_PREFIX})
    out: dict[str, str] = {}
    for r in rows:
        raw = r.get("data")
        if not raw:
            continue
        try:
            out.update(json.loads(raw))
        except (ValueError, TypeError) as e:
            # Битый кусок не должен выглядеть как «этих объектов не было»:
            # тогда план показал бы их как новые и спровоцировал лишнюю
            # работу. Честнее сказать и потребовать полного прогона.
            log.warning("Кусок отпечатков %s не читается (%s) — состояние "
                        "неполное, потребуется полная переиндексация",
                        r.get("kind"), e)
    return out


def write_stored(neo, digests: dict[str, str],
                 chunk_size: int = FP_CHUNK_SIZE) -> dict:
    """
    Сохраняет весь набор отпечатков. Возвращает статистику записи.

    Пишется набор ЦЕЛИКОМ, а не только изменившееся: он и читается целиком,
    а частичная запись означала бы, что при сбое посреди прогона состояние
    останется несогласованным — часть ключей от старого обхода, часть от
    нового. Различить такое потом нечем.
    """
    keys = sorted(digests)
    chunks = [keys[i:i + chunk_size] for i in range(0, len(keys), chunk_size)]

    written_chunks = 0
    kept: list[str] = []
    for idx, keys_chunk in enumerate(chunks):
        kind = f"{FP_CHUNK_PREFIX}{idx:03d}"
        payload = json.dumps({k: digests[k] for k in keys_chunk},
                             ensure_ascii=False, separators=(",", ":"))
        neo.rows(WRITE_CHUNK_CYPHER, {"kind": kind, "data": payload})
        kept.append(kind)
        written_chunks += 1

    # Набор мог сократиться — лишние куски от прошлого раза убираем, иначе
    # в состоянии остались бы владельцы, которых давно нет.
    dropped = 0
    rows = neo.rows(DROP_EXTRA_CHUNKS_CYPHER,
                    {"prefix": FP_CHUNK_PREFIX, "keep": kept})
    if rows:
        dropped = rows[0].get("dropped") or 0

    return {"sent": len(keys), "written": len(keys),
            "chunks": written_chunks, "dropped_chunks": dropped,
            "missing_count": 0}


STALE_DEBT_CYPHER = """
MATCH (cs:CallSite) WHERE cs.reason = 'stale_after_incremental'
RETURN count(cs) AS debt
"""


def stale_debt(neo) -> int:
    """
    Сколько callsite'ов помечено устаревшими после точечных обновлений.

    Это и есть «долг» частичного режима, выраженный числом. Пока его не
    измеряли, понять, насколько граф отстал от истины, было нечем — и
    единственным честным ответом оставалось «прогоняйте полную сборку на
    всякий случай».
    """
    rows = neo.rows(STALE_DEBT_CYPHER)
    return rows[0]["debt"] if rows else 0
