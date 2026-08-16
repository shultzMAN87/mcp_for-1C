"""
PERF-6. Двадцать три секунды на первый запрос к справке.
=========================================================

Что было видно
──────────────
Замер 16 августа: сразу после `--force-recreate` первый `ph-001` отработал
за 23 405 мс, следующие — за 108–286 мс. Одна цена за перезапуск
контейнера, дальше быстро.

Диагноз, который оказался неверным
──────────────────────────────────
Очевидное объяснение — «модель грузится лениво, первый запрос за это
платит». Оно неверно: **прогрев в фоне уже был написан** (`_warmup` в
server.py, фоновый поток, холостой прогон). То есть модель начинала
грузиться при старте контейнера, задолго до первого запроса.

Настоящих причин три, и ни одна не про лень.

**1. Модель грузилась ДВАЖДЫ одновременно.** `_get_model()` выглядел так:

    if not _model_loaded:
        _model = SentenceTransformer(...)   # 10+ секунд
        _model_loaded = True

Флаг ставился ПОСЛЕ загрузки. Пока поток прогрева грузил модель, флаг
оставался ложным — и пришедший запрос заходил в ту же ветку и начинал
грузить вторую копию. Два процесса тянут одни и те же 2 ГБ весов,
конкурируя за память и диск; каждый идёт медленнее, чем шёл бы один.
Отсюда 23 секунды вместо примерно двенадцати.

Это не гонка в смысле «редко и случайно»: при старте контейнера прогрев и
первый запрос сходятся почти всегда. FastMCP исполняет синхронные
инструменты в рабочем потоке на запрос, так что и два запроса подряд дают
две загрузки.

**2. BM25 не грелся вовсе.** Поиск гибридный: `_embed_query` и следом
`_embed_query_sparse`. Прогрев трогал только dense-модель, поэтому загрузка
sparse целиком лежала на первом запросе — и её никто не замечал за спиной
первой проблемы.

**3. Стоимость была не видна.** «Двадцать три секунды» — единственное, что
знал наблюдатель. Из чего они складываются — импорт torch, чтение весов,
первый forward, загрузка BM25 — не знал никто, а без этого нельзя выбирать,
что чинить. Здесь это меряется по частям.

Что делает модуль
─────────────────
Держит модели как «ленивые синглтоны, которые грузятся ровно один раз, кто
бы ни попросил», меряет каждый этап и рассказывает своё состояние.

Границы: сам не решает, когда греть, и ничего не знает про MCP. Когда
греть — дело сервера.

Отдельно про ONNX
─────────────────
Первым делом при слове «медленно грузится torch» хочется заменить его на
ONNX. Прежде чем менять, стоит посмотреть на замер:

    docker exec mcp-platform-help python3 /app/model_warmup.py --breakdown

И помнить главное: индекс из 41 062 чанков построен ЭТОЙ реализацией
модели. Другая реализация того же e5 (fastembed, onnxruntime) даёт близкие,
но не обязательно те же векторы — пулинг, нормализация и токенизатор могут
отличаться в мелочах. Векторы запроса перестанут точно соответствовать
векторам индекса, и выдача поедет молча: ошибки не будет, просто ответы
станут чуть хуже. Это ровно тот класс дефектов, против которого написан
весь проект.

Поэтому замена реализации — не правка производительности, а смена
математики поиска, и требует сверки косинусов на выборке чанков (порог
0.999) либо переиндексации. Здесь этого нет намеренно.
"""
from __future__ import annotations

import os
import threading
import time

__all__ = ["LazyModel", "Warmup", "load_dense", "load_sparse", "build_warmup"]

EMBEDDING_MODEL_NAME = os.environ.get(
    "EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
BM25_MODEL_NAME = os.environ.get("BM25_MODEL", "Qdrant/bm25")


class LazyModel:
    """
    Модель, которая грузится ровно один раз.

    Ключевое отличие от прежнего `_get_model()` — замок вокруг загрузки, а
    не только флаг после неё. Флаг отвечает на вопрос «уже загружено?», и
    во время загрузки честно отвечает «нет»; из этого «нет» второй поток
    делал вывод «значит, надо грузить». Замок отвечает на другой вопрос —
    «этим уже кто-то занят?» — и именно его надо было задавать.
    """

    def __init__(self, name: str, loader, clock=time.monotonic):
        self.name = name
        self._loader = loader
        self._clock = clock
        self._lock = threading.Lock()
        self._value = None
        self._done = False
        self.error = ""
        self.seconds = None
        self.waiters = 0          # сколько раз кто-то ждал чужой загрузки
        self.loads = 0            # сколько раз реально грузили

    def get(self, timeout: float | None = None):
        """
        Загруженная модель или None.

        Без таймаута ждём столько, сколько нужно: ответ дороже скорости, а
        поведение то же, что было до правки. С таймаутом — вернём None, не
        дождавшись; тогда вызывающий решает, что делать.
        """
        if self._done:
            return self._value

        acquired = self._lock.acquire(
            timeout=timeout if timeout is not None else -1)
        if not acquired:
            self.waiters += 1
            return None
        try:
            if self._done:
                # Пока ждали замок, загрузку закончил другой поток. Это не
                # исключение, а нормальный путь: именно ради него замок и
                # нужен.
                self.waiters += 1
                return self._value
            started = self._clock()
            try:
                self._value = self._loader()
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self._value = None
            self.seconds = round(self._clock() - started, 2)
            self.loads += 1
            self._done = True
            return self._value
        finally:
            self._lock.release()

    @property
    def ready(self) -> bool:
        return self._done and self._value is not None

    @property
    def loading(self) -> bool:
        return not self._done and self._lock.locked()

    def state(self) -> dict:
        return {
            "ready": self.ready,
            "loading": self.loading,
            "seconds": self.seconds,
            "loads": self.loads,
            "waited_for_it": self.waiters,
            "error": self.error,
        }


class Warmup:
    """
    Набор моделей и фоновый прогрев.

    Прогрев — оптимизация, а не условие работы: если он упал, всё
    по-прежнему загрузится при первом запросе, просто медленно. Поэтому
    здесь ничего не бросается наружу.
    """

    def __init__(self, models: dict, clock=time.monotonic):
        self.models = models
        self._clock = clock
        self.started_at = None
        self.finished_at = None
        self._thread = None

    def run(self, say=None) -> None:
        say = say or (lambda *_a, **_k: None)
        self.started_at = self._clock()
        for name, model in self.models.items():
            say(f"[warmup] {name}: загрузка...")
            model.get()
            state = model.state()
            if state["ready"]:
                say(f"[warmup] {name}: готово за {state['seconds']} с")
            else:
                say(f"[warmup] {name}: не загрузилось ({state['error']}), "
                    f"попробуем при первом запросе")
        self.finished_at = self._clock()

    def start_background(self, say=None) -> None:
        self._thread = threading.Thread(
            target=self.run, args=(say,), name="model-warmup", daemon=True)
        self._thread.start()

    @property
    def ready(self) -> bool:
        return all(m.ready for m in self.models.values())

    def state(self) -> dict:
        total = None
        if self.started_at is not None and self.finished_at is not None:
            total = round(self.finished_at - self.started_at, 2)
        running = self._thread is not None and self._thread.is_alive()
        return {
            "state": ("ready" if self.ready else
                      "warming" if running else
                      "idle" if self.started_at is None else "done_with_errors"),
            "total_sec": total,
            "models": {name: m.state() for name, m in self.models.items()},
            "note": (
                "Модели грузятся один раз на жизнь контейнера. Пока идёт "
                "прогрев, запрос ждёт его, а не запускает вторую загрузку "
                "(PERF-6). Если state=warming, первый поиск будет медленным "
                "— это разовая цена перезапуска, а не поломка."
            ),
        }


# ─── как именно грузятся модели ──────────────────────────────────────────


def load_dense():
    """
    Dense-модель плюс один холостой прогон.

    Прогон обязателен: внутри torch есть ленивая инициализация, и первый
    настоящий forward стоит заметно дороже последующих. Греть загрузку и
    оставить первый forward пользователю — значит переставить проблему, а
    не убрать.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    model.encode(["query: прогрев"], show_progress_bar=False,
                 normalize_embeddings=True)
    return model


def load_sparse():
    """
    BM25 плюс холостой прогон.

    До PERF-6 sparse не грелся вовсе, хотя гибридный поиск зовёт его сразу
    следом за dense: вся его загрузка лежала на первом запросе и не была
    видна за спиной более крупной проблемы.
    """
    from fastembed import SparseTextEmbedding

    model = SparseTextEmbedding(model_name=BM25_MODEL_NAME)
    next(iter(model.query_embed(["прогрев"])))
    return model


def build_warmup(dense_loader=None, sparse_loader=None) -> Warmup:
    return Warmup({
        "dense": LazyModel("dense", dense_loader or load_dense),
        "sparse": LazyModel("sparse", sparse_loader or load_sparse),
    })


# ─── замер по частям ─────────────────────────────────────────────────────


def _breakdown() -> int:  # pragma: no cover — ручной инструмент
    """
    Из чего складывается цена первого запроса.

    Нужен для решения про ONNX: если время уходит в импорт torch, замена
    реализации что-то даст; если в чтение весов с диска — почти ничего, и
    рисковать соответствием векторов индексу незачем.
    """
    print(f"Модель dense : {EMBEDDING_MODEL_NAME}")
    print(f"Модель sparse: {BM25_MODEL_NAME}")
    print()

    rows = []

    t0 = time.monotonic()
    from sentence_transformers import SentenceTransformer  # noqa: F401
    rows.append(("импорт sentence_transformers (тянет torch)",
                 time.monotonic() - t0))

    t0 = time.monotonic()
    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    rows.append(("чтение весов e5 с диска", time.monotonic() - t0))

    t0 = time.monotonic()
    model.encode(["query: прогрев"], show_progress_bar=False,
                 normalize_embeddings=True)
    rows.append(("первый forward (ленивая инициализация torch)",
                 time.monotonic() - t0))

    t0 = time.monotonic()
    model.encode(["query: второй"], show_progress_bar=False,
                 normalize_embeddings=True)
    rows.append(("второй forward — столько стоит запрос потом",
                 time.monotonic() - t0))

    t0 = time.monotonic()
    from fastembed import SparseTextEmbedding
    rows.append(("импорт fastembed", time.monotonic() - t0))

    t0 = time.monotonic()
    sparse = SparseTextEmbedding(model_name=BM25_MODEL_NAME)
    rows.append(("загрузка BM25", time.monotonic() - t0))

    t0 = time.monotonic()
    next(iter(sparse.query_embed(["прогрев"])))
    rows.append(("первый BM25 embed", time.monotonic() - t0))

    width = max(len(name) for name, _ in rows)
    total = 0.0
    for name, seconds in rows:
        # Второй forward — справочная строка, в сумму цены старта не входит.
        if not name.startswith("второй forward"):
            total += seconds
        print(f"  {name:<{width}}  {seconds:7.2f} с")
    print(f"  {'ИТОГО цена первого запроса':<{width}}  {total:7.2f} с")
    print()
    print("Что с этим делать:")
    print("  • Прогрев при старте убирает эту цену с пути пользователя")
    print("    целиком — она платится, пока контейнер поднимается.")
    print("  • Замена torch на ONNX имеет смысл, только если основная доля")
    print("    в первых двух строках. И она НЕ бесплатна: индекс построен")
    print("    этой реализацией, у другой векторы запроса могут отличаться,")
    print("    и выдача поедет молча. Нужна сверка косинусов или")
    print("    переиндексация 41 062 чанков.")
    return 0


def main() -> int:  # pragma: no cover — ручной инструмент
    import argparse

    ap = argparse.ArgumentParser(description="Замер цены загрузки моделей")
    ap.add_argument("--breakdown", action="store_true",
                    help="из чего складывается цена первого запроса")
    args = ap.parse_args()
    if args.breakdown:
        return _breakdown()
    ap.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
