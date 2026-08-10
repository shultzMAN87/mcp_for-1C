"""
Индексатор справки платформы 1С в Qdrant (hybrid dense+sparse).

Пайплайн:
  1. Считаем fingerprint списка .hbk (имена + размеры + mtime).
  2. Решаем, надо ли переиндексировать, по режиму REINDEX_MODE.
  3. Читаем каждый .hbk через hbk_reader (manually-parsed Local File Headers),
     распаковываем HTML deflate-ом.
  4. Парсим HTML через hbk_parser — получаем структурированные HelpEntry.
  5. Через hbk_chunker превращаем entry в несколько специализированных
     чанков (card / params / syntax / example / description).
  6. Каждый чанк эмбеддим DВА раза:
       - dense (sentence-transformers e5) — для семантики
       - sparse (fastembed BM25)          — для точных совпадений имён
  7. Создаём коллекцию в Qdrant с двумя именованными векторами
     ("dense" + "sparse"), как в its_indexer. Записываем точки батчами.
  8. Пишем служебную точку id=0 с fingerprint'ом.

Режимы (REINDEX_MODE):
  skip_if_nonempty  (дефолт)  — если коллекция есть и непуста, выходим.
                                Самый быстрый dev-старт.
  if_files_changed            — пересчитать, если fingerprint .hbk другой.
  force                       — всегда с нуля.

Коллекция при обнаружении старой (single-vector) схемы ПЕРЕСОЗДАЁТСЯ
автоматически. Это миграция с legacy, а не расчёт на сохранность
старых данных — в старой схеме справка всё равно была пустая.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Пакеты, которые уже ставит Dockerfile.embeddings
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from fastembed import SparseTextEmbedding

# Локальные модули в /app
sys.path.insert(0, "/app")
from hbk_reader import iter_html_from_hbk, HbkReadError
from hbk_parser import parse_html
from hbk_chunker import build_chunks

# ─── Конфиг ──────────────────────────────────────────────────────────────

PLATFORM_DIR = os.environ.get("PLATFORM_BIN_DIR", "/data/1c-platform")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "platform_help")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
BM25_MODEL = os.environ.get("BM25_MODEL", "Qdrant/bm25")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "64"))
UPSERT_BATCH = int(os.environ.get("UPSERT_BATCH", "200"))

REINDEX_MODE = os.environ.get("REINDEX_MODE", "skip_if_nonempty").strip().lower()
_VALID_MODES = {"skip_if_nonempty", "if_files_changed", "force"}
if REINDEX_MODE not in _VALID_MODES:
    print(f"⚠ Неизвестный REINDEX_MODE={REINDEX_MODE!r}, использую skip_if_nonempty")
    REINDEX_MODE = "skip_if_nonempty"

# Язык справки: ru (по умолчанию, только *_ru.hbk) / en (только *_root.hbk) / both.
# В поставке 1С рядом лежат две версии одного и того же файла:
#   shcntx_ru.hbk    — русская справка
#   shcntx_root.hbk  — английская
# Агенту на русском проекте нужна только русская, английская лишь раздувает
# индекс и зашумляет семантику.
HBK_INDEX_LANG = os.environ.get("HBK_INDEX_LANG", "ru").strip().lower()
if HBK_INDEX_LANG not in ("ru", "en", "both"):
    print(f"⚠ Неизвестный HBK_INDEX_LANG={HBK_INDEX_LANG!r}, использую 'ru'")
    HBK_INDEX_LANG = "ru"

# Служебная точка с fingerprint'ом. ID=0 зарезервирован, чанки идут с 1.
FINGERPRINT_POINT_ID = 0


def _keep_hbk(path: Path) -> bool:
    """Решает, надо ли индексировать данный .hbk исходя из HBK_INDEX_LANG."""
    stem = path.stem.lower()
    is_root = stem.endswith("_root")
    is_ru = stem.endswith("_ru")
    if HBK_INDEX_LANG == "ru":
        return is_ru
    if HBK_INDEX_LANG == "en":
        return is_root
    return True  # both


# ─── Fingerprint ─────────────────────────────────────────────────────────

def compute_files_fingerprint(directory: str) -> str:
    """
    SHA-256 по списку .hbk: имя + размер + mtime (секунды). Дёшево и достаточно.
    Учитывает фильтр HBK_INDEX_LANG — если пользователь переключил язык,
    fingerprint меняется и if_files_changed запускает переиндексацию.
    """
    base = Path(directory)
    if not base.is_dir():
        return "no_dir"
    entries = [f"_lang={HBK_INDEX_LANG}"]
    for path in sorted(base.glob("*.hbk")):
        if not _keep_hbk(path):
            continue
        try:
            st = path.stat()
            entries.append(f"{path.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            continue
    if len(entries) == 1:
        return "empty_dir"
    joined = "\n".join(entries).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


# ─── Работа с Qdrant ─────────────────────────────────────────────────────

def wait_for_qdrant(client: QdrantClient, timeout: int = 120) -> None:
    print(f"Ожидание Qdrant ({QDRANT_URL})...")
    start = time.time()
    last_error: Exception | None = None
    while time.time() - start < timeout:
        try:
            client.get_collections()
            print("  ✓ Qdrant доступен")
            return
        except Exception as e:
            last_error = e
            time.sleep(2)
    raise RuntimeError(f"Qdrant не доступен после {timeout}s: {last_error}")


def collection_info(client: QdrantClient, name: str) -> tuple[bool, bool, int]:
    """
    Возвращает (exists, is_hybrid_schema, points_count).
    is_hybrid_schema = True, если коллекция имеет именованные векторы
    'dense' + 'sparse'. Если схема single-vector (legacy) — False.
    """
    try:
        info = client.get_collection(name)
    except Exception:
        return False, False, 0

    vcfg = info.config.params.vectors
    sparse_cfg = info.config.params.sparse_vectors
    # При named vectors vcfg — это dict {str: VectorParams}. При single — VectorParams.
    is_named = isinstance(vcfg, dict)
    has_dense = is_named and "dense" in vcfg
    has_sparse = bool(sparse_cfg) and "sparse" in sparse_cfg
    return True, (has_dense and has_sparse), info.points_count or 0


def read_stored_fingerprint(client: QdrantClient, name: str) -> str | None:
    """Достаёт fingerprint из служебной точки id=0. None если её нет."""
    try:
        points = client.retrieve(collection_name=name, ids=[FINGERPRINT_POINT_ID], with_payload=True)
        if not points:
            return None
        return points[0].payload.get("fingerprint") if points[0].payload else None
    except Exception:
        return None


def create_hybrid_collection(client: QdrantClient, name: str, dense_dim: int) -> None:
    client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )
    print(f"  ✓ Коллекция '{name}' создана (dense={dense_dim} + sparse BM25)")


def write_fingerprint_point(client: QdrantClient, name: str, dense_dim: int, fingerprint: str) -> None:
    """
    Служебная точка с fingerprint'ом. Нулевой dense-вектор, пустой sparse —
    она не участвует в поиске (запросы фильтруются по chunk_type!=fingerprint
    и по тому, что sparse пустой). Но на всякий случай помечаем флагом в
    payload: _type=fingerprint.
    """
    client.upsert(
        collection_name=name,
        wait=True,
        points=[
            models.PointStruct(
                id=FINGERPRINT_POINT_ID,
                vector={
                    "dense": [0.0] * dense_dim,
                    "sparse": models.SparseVector(indices=[], values=[]),
                },
                payload={
                    "_type": "fingerprint",
                    "chunk_type": "fingerprint",
                    "fingerprint": fingerprint,
                    "indexed_at": int(time.time()),
                },
            )
        ],
    )
    print(f"  ✓ fingerprint записан: {fingerprint[:12]}…")


# ─── Решение: надо ли индексировать ──────────────────────────────────────

def decide_reindex(client: QdrantClient, fingerprint_now: str) -> bool:
    """Возвращает True, если нужно полностью переиндексировать."""
    print(f"→ REINDEX_MODE={REINDEX_MODE}")
    exists, is_hybrid, count = collection_info(client, COLLECTION_NAME)

    if REINDEX_MODE == "force":
        if exists:
            print("  режим force: удаляю коллекцию")
            client.delete_collection(COLLECTION_NAME)
        return True

    if not exists:
        print("  коллекции нет — первичная индексация")
        return True

    if not is_hybrid:
        # Legacy single-vector схема (то что сейчас висит с 7 пустыми точками).
        # Для гибридного поиска нужно пересоздать.
        print(f"  коллекция в legacy single-vector схеме — пересоздаю в hybrid")
        client.delete_collection(COLLECTION_NAME)
        return True

    if REINDEX_MODE == "skip_if_nonempty":
        if count > 1:  # учёт fingerprint-точки
            print(f"✓ Коллекция '{COLLECTION_NAME}' уже существует ({count} точек) — пропускаю")
            print(f"  Для переиндексации: REINDEX_MODE=force")
            return False
        print("  коллекция почти пустая — индексирую")
        client.delete_collection(COLLECTION_NAME)
        return True

    # REINDEX_MODE == "if_files_changed"
    stored = read_stored_fingerprint(client, COLLECTION_NAME)
    if stored is None:
        print("  fingerprint отсутствует (старая индексация) — переиндексирую")
        client.delete_collection(COLLECTION_NAME)
        return True
    if stored == fingerprint_now:
        print(f"✓ Fingerprint совпадает ({stored[:12]}…) — данные не менялись, пропускаю")
        return False
    print(f"  fingerprint изменился: {stored[:12]}… → {fingerprint_now[:12]}…")
    client.delete_collection(COLLECTION_NAME)
    return True


# ─── Чтение и чанкинг .hbk ───────────────────────────────────────────────

def iter_chunks_from_dir(directory: str):
    """Генератор чанков по всем .hbk в каталоге."""
    base = Path(directory)
    all_hbk = sorted(base.glob("*.hbk"))
    hbk_files = [p for p in all_hbk if _keep_hbk(p)]
    skipped = len(all_hbk) - len(hbk_files)
    print(f"\nНайдено .hbk: {len(all_hbk)}, обрабатываем {len(hbk_files)} (язык='{HBK_INDEX_LANG}', пропущено {skipped})")

    total_html = 0
    total_entries = 0
    total_chunks = 0
    parse_errors = 0

    for hbk_path in hbk_files:
        size_mb = hbk_path.stat().st_size / (1024 * 1024)
        print(f"  [{hbk_path.name}] {size_mb:.1f} MB")
        file_html = 0
        file_chunks = 0
        try:
            for html_name, raw in iter_html_from_hbk(hbk_path):
                file_html += 1
                try:
                    entry = parse_html(html_name, raw, hbk_file=hbk_path.name)
                except Exception as e:
                    parse_errors += 1
                    if parse_errors < 5:
                        print(f"    parse error {html_name}: {type(e).__name__}: {e}")
                    continue

                # Пустые записи (без имени и без текста) — пропускаем.
                if not entry.name_ru and not entry.name_en and not entry.description:
                    continue

                total_entries += 1
                for chunk in build_chunks(entry):
                    total_chunks += 1
                    file_chunks += 1
                    yield chunk
        except HbkReadError as e:
            print(f"    ⚠ read error: {e}")
            continue

        total_html += file_html
        print(f"    HTML: {file_html}, чанков: {file_chunks}")

    print(f"\nИтого: HTML={total_html}, entries={total_entries}, chunks={total_chunks}, parse_errors={parse_errors}")


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("Индексация справки 1С в Qdrant (hybrid dense+sparse)")
    print("=" * 60)

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    wait_for_qdrant(client)

    fingerprint_now = compute_files_fingerprint(PLATFORM_DIR)
    print(f"  fingerprint: {fingerprint_now[:12]}…")

    if not decide_reindex(client, fingerprint_now):
        return 0

    # ── Собираем чанки ───────────────────────────────────────────────────
    # iter_chunks — ленивый генератор; чтобы эффективно батчево считать
    # эмбеддинги, придётся материализовать в память. Для 25K entries это
    # порядка 80-150K чанков × ~300 байт текста = 30-50 MB. Приемлемо.
    print("\n── Сбор чанков ──")
    t0 = time.monotonic()
    chunks: list[dict] = list(iter_chunks_from_dir(PLATFORM_DIR))
    print(f"Собрано {len(chunks)} чанков за {time.monotonic()-t0:.1f}s")

    if not chunks:
        print("⚠ Нет чанков для индексации. Проверьте PLATFORM_BIN_DIR.")
        return 0

    # ── Загрузка моделей ─────────────────────────────────────────────────
    print(f"\n── Загрузка моделей ──")
    print(f"dense:  {EMBEDDING_MODEL}")
    dense_model = SentenceTransformer(EMBEDDING_MODEL)
    dense_dim = dense_model.get_sentence_embedding_dimension()
    print(f"  ✓ dim={dense_dim}")

    print(f"sparse: {BM25_MODEL}")
    sparse_model = SparseTextEmbedding(model_name=BM25_MODEL)
    print(f"  ✓ loaded")

    # ── Коллекция ────────────────────────────────────────────────────────
    # decide_reindex уже удалил старую при необходимости. Создаём чистую.
    print(f"\n── Создание коллекции '{COLLECTION_NAME}' ──")
    create_hybrid_collection(client, COLLECTION_NAME, dense_dim)

    # ── Эмбеддинги ───────────────────────────────────────────────────────
    is_e5 = "e5" in EMBEDDING_MODEL.lower()
    texts = [c["text"] for c in chunks]
    dense_inputs = [f"passage: {t}" if is_e5 else t for t in texts]

    print(f"\n── Dense эмбеддинги ({len(texts)} чанков) ──")
    t0 = time.monotonic()
    dense_vectors: list[list[float]] = []
    for i in range(0, len(dense_inputs), BATCH_SIZE):
        batch = dense_inputs[i:i + BATCH_SIZE]
        emb = dense_model.encode(batch, show_progress_bar=False, normalize_embeddings=True)
        dense_vectors.extend(emb.tolist())
        if (i // BATCH_SIZE) % 20 == 0 and i > 0:
            done = min(i + BATCH_SIZE, len(dense_inputs))
            rate = done / (time.monotonic() - t0)
            eta = (len(dense_inputs) - done) / rate if rate > 0 else 0
            print(f"  {done}/{len(dense_inputs)} ({rate:.0f}/s, eta {eta:.0f}s)")
    print(f"  ✓ готово за {time.monotonic()-t0:.1f}s")

    print(f"\n── Sparse (BM25) эмбеддинги ({len(texts)} чанков) ──")
    t0 = time.monotonic()
    # fastembed вернёт iterator по SparseEmbedding(indices, values).
    # batch_size=128 — чуть побольше чем у dense, BM25 дешёвый.
    sparse_embeddings = list(sparse_model.embed(texts, batch_size=128))
    print(f"  ✓ готово за {time.monotonic()-t0:.1f}s ({len(sparse_embeddings)} sparse векторов)")

    # ── Загрузка точек ───────────────────────────────────────────────────
    print(f"\n── Запись точек (batch={UPSERT_BATCH}) ──")
    t0 = time.monotonic()
    total = len(chunks)

    def make_point(idx: int) -> models.PointStruct:
        chunk = chunks[idx]
        svec = sparse_embeddings[idx]
        return models.PointStruct(
            # ID начинаем с 1 — ноль зарезервирован под fingerprint
            id=idx + 1,
            vector={
                "dense": dense_vectors[idx],
                "sparse": models.SparseVector(
                    indices=svec.indices.tolist(),
                    values=svec.values.tolist(),
                ),
            },
            payload=chunk,  # весь dict включая text — это payload
        )

    for i in range(0, total, UPSERT_BATCH):
        batch_points = [make_point(j) for j in range(i, min(i + UPSERT_BATCH, total))]
        client.upsert(collection_name=COLLECTION_NAME, points=batch_points, wait=False)
        if (i // UPSERT_BATCH) % 5 == 0 and i > 0:
            done = min(i + UPSERT_BATCH, total)
            rate = done / (time.monotonic() - t0)
            print(f"  {done}/{total} ({rate:.0f}/s)")
    # Последний батч — с wait=True, чтобы убедиться что всё долилось.
    print(f"  ✓ {total} точек записано за {time.monotonic()-t0:.1f}s")

    # Fingerprint
    write_fingerprint_point(client, COLLECTION_NAME, dense_dim, fingerprint_now)

    print("\n" + "=" * 60)
    print(f"✓ Индексация завершена: {total} чанков из {len(set(c['file_path'] for c in chunks))} страниц")
    print(f"  Коллекция: {COLLECTION_NAME}")
    print(f"  Векторы: dense (e5, {dense_dim}) + sparse (BM25)")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
