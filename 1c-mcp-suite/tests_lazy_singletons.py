"""
AUDIT-3. Ленивое состояние без замка — третий раз подряд.
=========================================================

История дефекта
───────────────
`PERF-6` починил его у моделей справки: `if not _model_loaded: грузим;
_model_loaded = True`. Флаг ставится ПОСЛЕ загрузки, поэтому во время
загрузки он честно отвечает «нет», и второй поток делает из этого «нет»
вывод «значит, надо грузить» — два экземпляра модели на два гигабайта.

`PERF-6.1` нашёл вторую копию того же кода — в том же файле, двадцатью
строками ниже комментария, где дефект разобран.

`AUDIT-3` пошёл искать третью. Она нашлась в `mcp_cache.get_cache()` и
отдельным видом — в счётчике отказов транспорта `platform-help`.

Чем этот набор отличается от «проверить, что оно работает»
─────────────────────────────────────────────────────────
Гонку нельзя поймать тестом надёжно: он проходит девять раз из десяти и в
эти девять раз ничего не доказывает. Поэтому проверок две, и они разные по
жанру.

  1. **По поведению** — два потока, вход в загрузчик задержан. Задержка
     делает окно гонки достоверным, а не случайным: без замка тест падает
     всегда, а не иногда.

  2. **По исходнику** — запрет на форму записи. Дефект узнаётся по виду
     («модульная переменная-кеш, присваивание внутри функции, слова
     `Lock` рядом нет»), и именно вид повторился трижды. Проверка по
     исходнику держится сама и не требует помнить список мест.

Вопрос, который задаётся каждому месту, один: **«этот флаг отвечает на тот
же вопрос, что замок?»** Флаг спрашивает «уже готово?», замок — «этим уже
кто-то занят?».

Что сюда намеренно НЕ попало
────────────────────────────
`_help_collection_kind` в `platform-help`. Гонка за него существует и
стоит одной лишней пробы Qdrant; закрывать её замком нельзя, потому что
замок пришлось бы держать через сетевой поход, а это выстроит все запросы
в очередь за одним таймаутом — ровно та задержка, против которой построен
`FAIL-1`. Решение записано в комментарии у `_transport_lock`, и тест ниже
проверяет, что решение осталось осознанным, а не забытым.

Пакетные скрипты (`bsl_parser`, `bsl_resolver`, `hbk_parser`,
`partial_fingerprint`) тоже не проверяются: они однопоточные по
устройству, второго потока там взяться неоткуда.

Запуск:  python3 tests_lazy_singletons.py
"""

from __future__ import annotations

import re
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ─── 1. По поведению ─────────────────────────────────────────────────────


class TestCacheSingletonIsShared(unittest.TestCase):
    """
    Два потока обязаны получить ОДИН кеш.

    Почему это важнее, чем кажется. Разъехавшийся кеш — не отказ: оба
    объекта работают, оба отдают правдоподобный `stats()`, просто половина
    записей одного не видна другому. Заметить нечем — ровно тот жанр
    «состояние определяется однажды и выдаётся за факт», который проект
    разбирает пятый заход.
    """

    def setUp(self):
        import mcp_cache
        self.mod = mcp_cache
        self._saved = mcp_cache._cache_instance
        mcp_cache._cache_instance = None

    def tearDown(self):
        self.mod._cache_instance = self._saved

    def test_two_threads_get_the_same_instance(self):
        mod = self.mod
        started = threading.Barrier(2)
        slow = threading.Event()
        created = []

        real = mod.MemoryCache

        class SlowMemoryCache(real):
            def __init__(self):
                # Задержка ВНУТРИ создания — это и есть окно гонки. Без неё
                # тест проверял бы скорость планировщика, а не замок.
                created.append(self)
                slow.wait(timeout=5)
                super().__init__()

        mod.MemoryCache = SlowMemoryCache
        results = {}

        def worker(tag):
            started.wait(timeout=5)
            results[tag] = mod.get_cache()

        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
            for t in threads:
                t.start()
            slow.set()
            for t in threads:
                t.join(timeout=10)
        finally:
            mod.MemoryCache = real

        self.assertIs(
            results.get(0), results.get(1),
            "два потока получили РАЗНЫЕ кеши: записи разойдутся между ними, "
            "и попадания молча превратятся в промахи",
        )
        self.assertEqual(
            len(created), 1,
            f"кеш создан {len(created)} раза — замок не удержал",
        )


class TestTransportCounterIsAtomic(unittest.TestCase):
    """
    Счётчик отказов транспорта не имеет права терять инкременты.

    `x += 1` — это чтение, сложение и запись. Потерянный инкремент означает,
    что порог `FAIL-1` не набирается, быстрый отказ не включается, и каждый
    вызов снова платит восемь секунд таймаута. То есть механизм не
    срабатывает ровно в том сценарии, ради которого написан: Qdrant лёг, и
    отказы приходят пачкой.
    """

    def setUp(self):
        try:
            sys.path.insert(0, str(HERE / "mcp-platform-help"))
            import server  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"platform-help не импортируется вне образа: {exc}")
        self.server = sys.modules["server"]
        self.server._transport_fails = 0
        self.server._help_collection_kind = None

    def test_parallel_failures_are_all_counted(self):
        srv = self.server
        # Порог поднимаем выше числа потоков: иначе счётчик остановится на
        # пороге по делу, и тест перестанет мерить то, что называет.
        saved = srv.TRANSPORT_FAILS_BEFORE_GIVING_UP
        srv.TRANSPORT_FAILS_BEFORE_GIVING_UP = 10_000
        try:
            def worker():
                for _ in range(500):
                    srv._note_transport_failure("тест")

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(
                srv._transport_fails, 2000,
                "инкременты потерялись — порог быстрого отказа наберётся "
                "не тогда, когда должен",
            )
        finally:
            srv.TRANSPORT_FAILS_BEFORE_GIVING_UP = saved
            srv._transport_fails = 0


# ─── 2. По исходнику ─────────────────────────────────────────────────────


# Файлы, которые крутятся под uvicorn и обслуживают запросы параллельно.
# Пакетные скрипты сюда не входят по устройству, а не по недосмотру.
CONCURRENT_FILES = (
    "mcp_cache.py",
    "mcp-platform-help/server.py",
    "mcp-platform-help/model_warmup.py",
    "mcp-metadata-graph/server.py",
    "mcp-query-builder/server.py",
    "mcp-bsl-checker/server.py",
    "mcp-bsl-checker/bsl_lsp.py",
)

# Форма дефекта: «спросили, пусто ли; раз пусто — заполнили».
#
# Ловится не по одной строке, а по совпадению трёх признаков в пределах
# одной функции: (1) проверка переменной на пустоту, (2) присваивание ТОЙ ЖЕ
# переменной, (3) это общее состояние, а не локальная переменная — то есть
# либо `global`, либо атрибут объекта.
#
# Третий признак обязателен. Без него шаблон ловит любую функцию с
# `if not self._available: ...` — таких в проекте одиннадцать, и все они
# ничего не создают. Проверка, кричащая одинаково на дефект и на обычный
# код, — это лампочка без надписи; тот же урок, что у `check_publish.py`.
def _functions_with_lazy_guard(text: str):
    """(имя функции, номер строки) для каждой ленивой инициализации без замка."""
    out = []
    fn_name, fn_line, fn_body = "", 0, []

    def flush():
        if not fn_name:
            return
        body = "\n".join(fn_body)
        if re.search(r"with\s+\S*_?lock\b|_lock\.acquire", body, re.I):
            return

        guarded = set()
        for line in fn_body:
            m = re.match(
                r"\s*if\s+(?:not\s+)?(self\._[A-Za-z_0-9]+|_[A-Za-z_0-9]+)"
                r"(?:\s+is\s+None)?\s*:", line)
            if m:
                guarded.add(m.group(1))
        if not guarded:
            return

        shared = {n for n in guarded
                  if n.startswith("self.")
                  or re.search(rf"^\s*global\b[^\n]*\b{re.escape(n)}\b", body, re.M)}
        if not shared:
            return

        filled = {n for n in shared
                  if re.search(rf"^\s*{re.escape(n)}\s*=[^=]", body, re.M)}
        if filled:
            out.append((fn_name, fn_line))

    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(\s*)def\s+([A-Za-z_0-9]+)", line)
        if m:
            flush()
            fn_name, fn_line, fn_body = m.group(2), i, []
            continue
        if fn_name:
            fn_body.append(line)
    flush()
    return out


# Разобранные и осознанно оставленные места: имя функции -> почему.
#
# Список не «исключения из правила», а протокол разбора. Пустой список
# означал бы, что проверка не встречала ничего сложнее шаблона, — а она
# встречала, и решение стоит держать рядом с проверкой, а не в чате.
REVIEWED = {
    "_detect_help_collection_kind":
        "AUDIT-3: замок пришлось бы держать через сетевой поход, а это "
        "выстроит все запросы в очередь за одним таймаутом — то самое, "
        "против чего FAIL-1. Цена гонки — одна лишняя проба Qdrant.",
    "_prewarm_collection_kind":
        "AUDIT-3: вызывается один раз при старте, до приёма запросов.",
}


class TestNoNewFlagInsteadOfLock(unittest.TestCase):

    def test_concurrent_files_have_no_unreviewed_lazy_guards(self):
        found = []
        for rel in CONCURRENT_FILES:
            path = HERE / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, line in _functions_with_lazy_guard(text):
                if name in REVIEWED:
                    continue
                found.append(f"{rel}:{line} {name}()")
        self.assertFalse(
            found,
            "ленивая инициализация без замка в коде, который обслуживает "
            "запросы параллельно:\n  " + "\n  ".join(found) +
            "\n\nВопрос к каждому месту: этот флаг отвечает на тот же "
            "вопрос, что замок? Флаг спрашивает «уже готово?», замок — "
            "«этим уже кто-то занят?». Если место разобрано и оставлено "
            "осознанно, впишите его в REVIEWED вместе с причиной.",
        )

    def test_reviewed_entries_still_exist(self):
        """
        Разбор без места, к которому он относится, — это заметка, которая
        переживёт свой предмет и будет введена в заблуждение следующим.
        Ровно то, что `DOC-5` делает с `PLAN.md`.
        """
        all_text = "\n".join(
            (HERE / rel).read_text(encoding="utf-8", errors="replace")
            for rel in CONCURRENT_FILES if (HERE / rel).exists())
        for name in REVIEWED:
            self.assertIn(
                f"def {name}", all_text,
                f"в REVIEWED записан {name}(), которого больше нет в коде",
            )


class TestLockedPlacesStayLocked(unittest.TestCase):
    """
    Обратная проверка: там, где замок уже стоит, он должен остаться.

    Без неё набор ловит только появление нового дефекта и молчит на
    удалении лечения из старого.
    """

    def test_lsp_client_start_holds_the_lock(self):
        text = (HERE / "mcp-bsl-checker" / "bsl_lsp.py").read_text(encoding="utf-8")
        start = text.index("def start(self)")
        head = text[start:start + 200]
        self.assertIn("with self._lock", head,
                      "BslLspClient.start() потерял замок: два вызова "
                      "поднимут две JVM")

    def test_cache_singleton_holds_the_lock(self):
        text = (HERE / "mcp_cache.py").read_text(encoding="utf-8")
        start = text.index("def get_cache()")
        self.assertIn("with _cache_lock", text[start:start + 600])

    def test_transport_counter_holds_the_lock(self):
        text = (HERE / "mcp-platform-help" / "server.py").read_text(encoding="utf-8")
        for fn in ("_note_transport_failure", "_note_transport_success"):
            start = text.index(f"def {fn}(")
            self.assertIn("with _transport_lock", text[start:start + 900],
                          f"{fn}() потерял замок вокруг счётчика")


if __name__ == "__main__":
    unittest.main(verbosity=2)
