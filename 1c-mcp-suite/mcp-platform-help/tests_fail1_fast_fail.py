"""
Тесты FAIL-1: отказ должен быть не только громким, но и быстрым.

Что случилось на приёмке 15 августа. Остановили Qdrant, и каждый вызов
`platform_help_search` стал занимать 7,9 с вместо 190 мс. Формат коллекции
кешируется положительно навсегда, поэтому после смерти Qdrant сервер
продолжал считать коллекцию гибридной и честно ждал двух таймаутов подряд
на каждом запросе.

Проверяется именно поведение кеша, а не сеть: `server.py` подменяется
заглушками, чтобы набор шёл без Qdrant, без модели эмбеддингов и без
пакета `mcp`.

Запуск:  python3 tests_fail1_fast_fail.py
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _install_stubs() -> None:
    """
    Заглушки тяжёлых зависимостей, чтобы набор шёл на хосте.

    Первая редакция подставляла только `mcp` и рассчитывала на то, что
    остальные импорты в `server.py` обёрнуты в try/except (они обёрнуты).
    На приёмке 15 августа выяснилось, что этого мало: на Windows набор
    не запустился с `ModuleNotFoundError: sentence_transformers`, а на
    Linux прошёл. Полагаться на то, что все пути к необязательным пакетам
    защищены, — то же самое рассуждение «ошибок не найдено», которое этот
    проект чинит четвёртый заход подряд: пути защищены сегодня и в той
    ветке, которую я посмотрел.

    Поэтому подставляем всё, чего может не быть на хосте. Тесты FAIL-1
    проверяют логику кеша и счётчик отказов — ни модель, ни клиент Qdrant
    им не нужны ни в каком виде.
    """
    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    if "mcp" not in sys.modules:
        mcp_pkg = _module("mcp")
        server_mod = _module("mcp.server")
        fastmcp_mod = _module("mcp.server.fastmcp")

        class FastMCP:
            def __init__(self, *a, **kw):
                pass

            def tool(self, *a, **kw):
                def deco(fn):
                    return fn
                return deco

        fastmcp_mod.FastMCP = FastMCP
        server_mod.fastmcp = fastmcp_mod
        mcp_pkg.server = server_mod

    if "sentence_transformers" not in sys.modules:
        st = _module("sentence_transformers")

        class SentenceTransformer:
            def __init__(self, *a, **kw):
                pass

            def get_sentence_embedding_dimension(self):
                return 768

            def encode(self, texts, **kw):
                # Ноль-вектор: ни один тест здесь не смотрит на числа.
                return [[0.0] * 768 for _ in texts]

        st.SentenceTransformer = SentenceTransformer

    if "fastembed" not in sys.modules:
        fe = _module("fastembed")

        class SparseTextEmbedding:
            def __init__(self, *a, **kw):
                pass

            def query_embed(self, texts, **kw):
                return iter(())

        fe.SparseTextEmbedding = SparseTextEmbedding

    if "qdrant_client" not in sys.modules:
        qc = _module("qdrant_client")
        models = _module("qdrant_client.models")

        class QdrantClient:
            def __init__(self, *a, **kw):
                pass

            def get_collection(self, *a, **kw):
                raise RuntimeError("заглушка: Qdrant в тестах не поднимается")

            def query_points(self, *a, **kw):
                raise RuntimeError("заглушка: Qdrant в тестах не поднимается")

        qc.QdrantClient = QdrantClient
        qc.models = models


_install_stubs()
sys.path.insert(0, str(ROOT))

import os

# Прогрев модели в тестах не нужен — он тянет два гигабайта весов.
os.environ.setdefault("HELP_WARMUP", "0")

try:
    import server  # noqa: E402
except Exception as exc:  # pragma: no cover — диагностика, а не поведение
    # Набор, который «не запускался», в итоговой строке выглядит одинаково
    # для любой причины. Печатаем свою — иначе разбираться придётся
    # запуском вручную, как 15 августа.
    print(f"tests_fail1_fast_fail: не удалось импортировать server.py — "
          f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise


class _Base(unittest.TestCase):
    def setUp(self):
        # Каждый тест начинает с чистого состояния модуля.
        server._help_collection_kind = None
        server._help_collection_kind_at = 0.0
        server._transport_fails = 0
        server._degrade_counts = {"hybrid_to_dense": 0, "empty": 0}
        server._degrade_announced = set()


class TestTransportFailureCounter(_Base):

    def test_single_failure_keeps_the_cached_format(self):
        """
        Один отказ — не повод менять режим. Сеть моргает, Qdrant
        перезапускается; сбрасывать кеш на первом же промахе значило бы
        ронять режим на ровном месте.
        """
        server._help_collection_kind = "hybrid"
        server._note_transport_failure("hybrid")
        self.assertEqual(server._help_collection_kind, "hybrid")
        self.assertEqual(server._transport_fails, 1)

    def test_threshold_drops_the_format_to_missing(self):
        """Два подряд — считаем коллекцию недоступной и перестаём ждать."""
        server._help_collection_kind = "hybrid"
        server._note_transport_failure("hybrid")
        server._note_transport_failure("legacy_dense")
        self.assertEqual(server._help_collection_kind, "missing")

    def test_success_resets_the_counter(self):
        """Один удавшийся поход обнуляет счёт — иначе редкие промахи копятся."""
        server._help_collection_kind = "hybrid"
        server._note_transport_failure("hybrid")
        server._note_transport_success()
        server._note_transport_failure("hybrid")
        self.assertEqual(server._help_collection_kind, "hybrid",
                         "два промаха с успехом между ними — не подряд")

    def test_missing_is_not_downgraded_twice(self):
        """Повторный сброс уже сброшенного не должен обновлять отсчёт recheck."""
        server._help_collection_kind = "missing"
        server._help_collection_kind_at = 12345.0
        server._note_transport_failure("hybrid")
        server._note_transport_failure("hybrid")
        self.assertEqual(server._help_collection_kind_at, 12345.0)


class TestFastFailPath(_Base):
    """
    Главное: после сброса вызов не должен ходить в сеть вообще. Меряем не
    время (оно зависит от машины), а факт обращения.
    """

    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_hybrid(query, limit=10, kind_filter=""):
            self.calls.append("hybrid")
            server._note_transport_failure("hybrid")
            return None

        def fake_legacy(query, limit=10, kind_filter=""):
            self.calls.append("legacy")
            server._note_transport_failure("legacy_dense")
            return None

        self._orig = (server._help_search_hybrid, server._help_search_legacy_dense)
        server._help_search_hybrid = fake_hybrid
        server._help_search_legacy_dense = fake_legacy

    def tearDown(self):
        server._help_search_hybrid, server._help_search_legacy_dense = self._orig

    def test_first_call_pays_two_timeouts_then_the_rest_are_free(self):
        server._help_collection_kind = "hybrid"
        server._help_collection_kind_at = server.time.monotonic()

        hits, mode = server._help_search("что угодно", 5)
        self.assertEqual(hits, [])
        self.assertEqual(mode, "unavailable")
        # Первый вызов честно пробует оба маршрута — это и есть цена
        # диагноза, платится один раз.
        self.assertEqual(self.calls, ["hybrid", "legacy"])

        # Кеш уже сброшен в "missing" — дальше сеть не трогаем.
        self.calls.clear()
        for _ in range(5):
            hits, mode = server._help_search("что угодно", 5)
            self.assertEqual(mode, "unavailable")
        self.assertEqual(self.calls, [],
                         "после сброса режима запросы всё ещё уходят в сеть — "
                         "восемь секунд на вызов вернулись")

    def test_recheck_happens_after_the_window(self):
        """
        Сброс не навсегда: через MISSING_RECHECK_SEC сервер обязан
        попробовать снова, иначе поднявшийся Qdrant остался бы незамеченным
        до перезапуска контейнера — ровно дефект FIX-12, только наоборот.
        """
        server._help_collection_kind = "missing"
        server._help_collection_kind_at = (
            server.time.monotonic() - server.MISSING_RECHECK_SEC - 1
        )
        probed = []
        orig = server._probe_collection_http
        server._probe_collection_http = lambda: probed.append(1) or None
        try:
            server._detect_help_collection_kind()
        finally:
            server._probe_collection_http = orig
        self.assertTrue(probed, "окно перепроверки истекло, а проверки не было")

    def test_within_the_window_nothing_touches_the_network(self):
        """
        Обратная сторона: пока окно не истекло, проба не делается вовсе.
        Именно это превращает 3,9 с в 30 мс.
        """
        server._help_collection_kind = "missing"
        server._help_collection_kind_at = server.time.monotonic()
        probed = []
        orig = server._probe_collection_http
        server._probe_collection_http = lambda: probed.append(1) or None
        try:
            self.assertEqual(server._detect_help_collection_kind(), "missing")
        finally:
            server._probe_collection_http = orig
        self.assertEqual(probed, [], "в сеть сходили, хотя ответ уже известен")


class TestOtherToolsFailFastToo(_Base):
    """
    FAIL-1, вторая половина. Замер 15 августа: поиск стал отвечать за 30 мс,
    а lookup продолжал платить 3,9 с и stats 7,8 с — они ходят в Qdrant
    своими маршрутами и про кеш формата не знали.
    """

    def setUp(self):
        super().setUp()
        server._help_collection_kind = "missing"
        server._help_collection_kind_at = server.time.monotonic()
        self.probed = []
        self._orig_probe = server._probe_collection_http
        self._orig_client = server._get_qclient
        server._probe_collection_http = lambda: self.probed.append("probe") or None
        server._get_qclient = lambda: self.probed.append("client") or None

    def tearDown(self):
        server._probe_collection_http = self._orig_probe
        server._get_qclient = self._orig_client

    def test_lookup_does_not_wait_for_a_timeout(self):
        import json
        data = json.loads(server.platform_help_lookup("СтрДлина"))
        self.assertTrue(data["degraded"])
        self.assertFalse(data["found"])
        self.assertEqual(self.probed, [],
                         "lookup всё ещё ходит в сеть при известном отказе")

    def test_stats_does_not_wait_for_a_timeout(self):
        import json
        data = json.loads(server.platform_help_stats())
        self.assertTrue(data["degraded"])
        self.assertFalse(data["qdrant_available"])
        self.assertEqual(self.probed, [],
                         "stats всё ещё ходит в сеть при известном отказе")

    def test_stats_says_the_answer_is_cached(self):
        """
        Кешированный ответ обязан называть себя кешированным — иначе это
        `FIX-12` наоборот: состояние, определённое однажды, выдаётся за
        свежий факт.
        """
        import json
        data = json.loads(server.platform_help_stats())
        self.assertIn("кешированный", data["note"])


class TestPrewarmCollectionKind(_Base):
    """
    PERF-6.1, вторая половина. Живёт в этом наборе, а не в
    tests_model_warmup.py, по двум причинам: проверяется тот же кеш формата
    коллекции, что и во всём файле, и здесь уже стоят заглушки, без которых
    server.py на хосте не импортируется.

    Правило одно: положительный ответ запоминаем, «missing» на старте
    выбрасываем. Иначе прогрев своими руками заводит FIX-12 — стек
    поднимается целиком, help-indexer ещё строит индекс, и запомненное
    «коллекции нет» отвечает «поиск недоступен» следующие полминуты, хотя
    проверка живьём сказала бы обратное.
    """

    def setUp(self):
        super().setUp()
        # Подменяем определение схемы, а не сеть: проверяется правило «что
        # делать с ответом», а не то, как ответ получен. Оригинал
        # возвращаем на место — `del` тут стёр бы настоящую функцию из
        # модуля и увёз бы за собой весь остаток набора.
        self._orig_detect = server._detect_help_collection_kind

    def tearDown(self):
        server._detect_help_collection_kind = self._orig_detect

    def _detect_returns(self, verdict):
        def detect():
            server._help_collection_kind = verdict
            server._help_collection_kind_at = server.time.monotonic()
            return verdict
        server._detect_help_collection_kind = detect

    def test_successful_detection_is_remembered(self):
        self._detect_returns("hybrid")
        self.assertEqual(server._prewarm_collection_kind(lambda *_: None),
                         "hybrid")
        self.assertEqual(server._help_collection_kind, "hybrid",
                         "схему определили и тут же забыли")

    def test_missing_at_startup_is_discarded(self):
        self._detect_returns("missing")
        self.assertEqual(server._prewarm_collection_kind(lambda *_: None),
                         "discarded")
        self.assertIsNone(
            server._help_collection_kind,
            "«коллекции нет» на старте запомнено как факт — следующие "
            "MISSING_RECHECK_SEC поиск будет отвечать «недоступен» из кеша")

    def test_discarded_verdict_leaves_the_failure_counter_alone(self):
        """
        Выбрасываем вывод о коллекции, а не отказ транспорта: если Qdrant
        правда лежит, отказ был настоящий, и FAIL-1 обязан его засчитать.
        Обнулить счётчик здесь значило бы оттянуть быстрый отказ на один
        лишний таймаут.
        """
        def detect():
            server._note_transport_failure("проба коллекции")
            server._help_collection_kind = "missing"
            return "missing"

        server._detect_help_collection_kind = detect
        server._prewarm_collection_kind(lambda *_: None)
        self.assertEqual(server._transport_fails, 1)

    def test_prewarm_is_wired_into_the_warmup(self):
        """
        Шаг, который никто не зовёт, выглядит точно так же, как шаг,
        который отработал: первый запрос просто чуть медленнее, и всё.
        """
        code = "\n".join(
            line.split("#", 1)[0]
            for line in (ROOT / "server.py").read_text(encoding="utf-8").splitlines())
        self.assertIn("then=_prewarm_collection_kind", code)


class TestTimeoutsAreShort(unittest.TestCase):

    def test_default_timeout_is_seconds_not_tens_of_seconds(self):
        """
        Локальный контейнер в docker-сети либо отвечает за доли секунды,
        либо не отвечает вовсе. Пятнадцать секунд ожидания не улучшают ни
        один исход.
        """
        self.assertLessEqual(server.QDRANT_TIMEOUT_SEC, 5)
        self.assertGreaterEqual(server.QDRANT_TIMEOUT_SEC, 1)

    def test_no_long_hardcoded_timeouts_left(self):
        """
        Проверка по исходнику: длинные таймауты складываются друг с другом,
        и именно сумма двух дала 7,9 с на вызов.
        """
        import re
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        long_ones = [
            m.group(0) for m in re.finditer(r"timeout=(\d+)", text)
            if int(m.group(1)) > 5
        ]
        self.assertEqual(long_ones, [],
                         f"остались длинные таймауты: {long_ones}")


class TestDiagnosticsCannotKillTheCall(_Base):
    """
    FAIL-2. Печать диагностики не должна ронять то, что диагностирует.

    Приёмка 15 августа на Windows: `_get_model()` поймал отсутствие
    `sentence_transformers`, начал печатать «⚠ Не удалось загрузить
    модель», и упал на печати — в консоли была cp1251, знака ⚠ там нет.
    Наружу вместо мягкой деградации полетел UnicodeEncodeError.

    Здесь консоль cp1251 воспроизводится намеренно: без этого дефект
    виден только на Windows, то есть после выкатки.
    """

    def _cp1251_stream(self):
        import io
        raw = io.BytesIO()
        return io.TextIOWrapper(raw, encoding="cp1251", newline="")

    def test_model_diagnostics_survive_a_narrow_console(self):
        """
        После PERF-6 сообщение о неудачной загрузке печатает не
        `_get_model()`, а прогрев. Первая редакция теста сбрасывала флаг
        `_model_loaded`, которого в коде больше нет: тест продолжал
        проходить, но проверял пустоту — заглушка модели грузится успешно,
        и печатать было нечего.

        Теперь путь воспроизводится честно: загрузка падает, прогрев
        сообщает об этом в консоль cp1251, где знака ⚠ не существует.
        """
        import contextlib
        from model_warmup import build_warmup

        def boom():
            raise RuntimeError("нет весов")

        warmup = build_warmup(dense_loader=boom, sparse_loader=lambda: "s",
                              qdrant_loader=lambda: "q")
        stream = self._cp1251_stream()
        with contextlib.redirect_stdout(stream):
            warmup.run(server._say)        # не должно бросить
        self.assertFalse(warmup.ready)

    def test_transport_warning_survives_a_narrow_console(self):
        import contextlib
        server._help_collection_kind = "hybrid"
        stream = self._cp1251_stream()
        with contextlib.redirect_stderr(stream):
            server._note_transport_failure("hybrid")
            server._note_transport_failure("legacy_dense")
        self.assertEqual(server._help_collection_kind, "missing",
                         "сообщение не напечаталось — но режим обязан "
                         "смениться в любом случае")

    def test_degrade_announcement_survives_a_narrow_console(self):
        import contextlib
        stream = self._cp1251_stream()
        with contextlib.redirect_stderr(stream):
            server._announce_degrade("empty", "проверка узкой консоли")


class TestStatsReportTransportState(_Base):

    def test_stats_expose_the_counter(self):
        """
        Счётчик должен быть виден снаружи: ненулевое значение при
        работающем поиске означает, что Qdrant отвечает через раз, — а это
        не видно ни по одному другому полю.
        """
        import json
        server._transport_fails = 3
        data = json.loads(server.platform_help_stats())
        # Сравниваем с текущим значением, а не с тройкой: сам вызов stats
        # может сходить в сеть и досчитать. Проверяем, что поле проброшено,
        # а не что число застыло.
        self.assertEqual(data["transport_fails_in_a_row"], server._transport_fails)
        self.assertGreaterEqual(data["transport_fails_in_a_row"], 3)
        self.assertIn("qdrant_timeout_sec", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
