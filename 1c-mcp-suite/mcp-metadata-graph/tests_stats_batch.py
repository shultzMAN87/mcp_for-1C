"""
PERF-10. `metadata_stats` — пятнадцать походов в базу вместо одного.
=====================================================================

Что было
────────
`metadata_stats` отвечал 2,1–2,5 с при медиане поиска 95 мс. Для
инструмента «как ты себя чувствуешь» это перевёрнуто: к нему приходят,
когда что-то не так, и он приходит на помощь последним.

Разбор показал, что дешёвое и дорогое там перемешаны и на глаз
неотличимы — все запросы выглядят как «посчитай»:

  `MATCH (n:Label) RETURN count(n)`       счётчик хранилища, O(1). Восемь штук.
  `MATCH (cs:CallSite {resolved: true})`  предикат по свойству — счётчик
                                          хранилища не работает, полный
                                          обход метки. Три штуки, и все три
                                          обходили одну и ту же метку.
  `MATCH ()-[r]->() RETURN type(r), ...`  тип не указан — обход всех рёбер.

Плюс пятнадцатикратная дорога: `_neo4j_query` клал в тело ровно один
statement, хотя транзакционный HTTP-API Neo4j принимает список.

Что проверяется здесь
─────────────────────
Две вещи, и вторая важнее первой.

  1. **Походов стало меньше.** Проверяется счётчиком обращений к
     транспорту, а не секундомером: время в тесте мерило бы скорость
     контейнера сборки, а не правку.

  2. **Ответ не изменился.** Это главное. Правка про скорость не имеет
     права трогать содержание — тот же критерий, по которому принимали
     `PERF-6.1` («качество поиска не изменилось до цифры»). Три обхода
     `CallSite`, сложенные в один `sum(CASE ...)`, обязаны дать те же три
     числа, включая тонкость `FIX-4` про методы объектов платформы.

Двойник Neo4j отвечает по форме HTTP-API и считает обращения. Живой базы
здесь нет и не должно быть: набор обязан идти на голом Python.

Запуск:  python3 tests_stats_batch.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

# `server.py` при импорте требует NEO4J_PASSWORD и отказывается стартовать
# без него (`SEC-2`). Отказ правильный: дефолтного пароля у сервера быть не
# должно. Поэтому соседние наборы `server.py` не трогают вовсе и тестируют
# вынесённые модули — но `metadata_stats` живёт именно здесь.
#
# Заглушка безопасна ровно потому, что соединения не будет: `_neo4j_query_raw`
# подменён двойником во всех тестах набора, а сам пароль никуда не уходит.
# Ставим только если переменной нет — чужое значение не перетираем.
os.environ.setdefault("NEO4J_PASSWORD", "тест-без-соединения")

try:
    import server
except Exception as exc:  # pragma: no cover
    server = None
    _import_error = exc


# ─── двойник Neo4j ───────────────────────────────────────────────────────


class FakeNeo4j:
    """
    Отвечает по форме транзакционного HTTP-API и считает походы.

    Ответ подбирается по тексту запроса: тест не должен зависеть от того,
    в каком порядке `metadata_stats` их складывает, — иначе он сломается
    от перестановки строк, ничего не проверив.
    """

    def __init__(self, counts=None, callsites=None, relations=None,
                 fingerprints=True):
        self.round_trips = 0
        self.statements_seen = []
        self.counts = counts or {}
        self.callsites = callsites or {
            "resolved": 900, "unresolved": 100, "object_method": 40}
        self.relations = relations or {"CALLS": 5000, "HAS_ATTRIBUTE": 3000}
        self.fingerprints = fingerprints

    def __call__(self, body, cypher_for_log="", timeout=None):
        self.round_trips += 1
        results = []
        for st in body["statements"]:
            cypher = st["statement"]
            self.statements_seen.append(cypher)
            results.append(self._answer(cypher, st.get("parameters") or {}))
        return {"results": results, "errors": []}

    def _answer(self, cypher, params):
        if "sum(CASE" in cypher:
            return self._rows(["resolved", "unresolved", "object_method"],
                              [[self.callsites["resolved"],
                                self.callsites["unresolved"],
                                self.callsites["object_method"]]])
        if "kind_ru" in cypher:
            return self._rows(["kind", "count"],
                              [["Справочник", 120], ["Документ", 80]])
        if "type(r)" in cypher:
            return self._rows(["rel", "cnt"],
                              [[k, v] for k, v in self.relations.items()])
        if "Fingerprint" in cypher:
            if not self.fingerprints:
                return self._rows(["value", "mode", "updated_at"], [])
            return self._rows(["value", "mode", "updated_at"],
                              [["abcdef0123456789", "full", 1755400000000]])
        for label, value in self.counts.items():
            if f":{label})" in cypher:
                return self._rows(["c"], [[value]])
        return self._rows(["c"], [[0]])

    @staticmethod
    def _rows(columns, rows):
        return {"columns": columns, "data": [{"row": r} for r in rows]}


DEFAULT_COUNTS = {
    "MetadataObject": 200, "Attribute": 1500, "TabularSection": 60,
    "Form": 90, "EnumValue": 45, "Type": 300,
    "Module": 400, "Callable": 2500,
}


@unittest.skipIf(server is None, "server.py не импортируется вне образа")
class StatsCase(unittest.TestCase):

    def setUp(self):
        self.fake = FakeNeo4j(counts=DEFAULT_COUNTS)
        self._saved_raw = server._neo4j_query_raw
        self._saved_state = server._graph_state
        server._neo4j_query_raw = self.fake
        server._graph_state = lambda: ("ok", "")

    def tearDown(self):
        server._neo4j_query_raw = self._saved_raw
        server._graph_state = self._saved_state

    def stats(self) -> dict:
        return json.loads(server.metadata_stats())


class TestOneRoundTrip(StatsCase):

    def test_single_request_to_neo4j(self):
        self.stats()
        self.assertEqual(
            self.fake.round_trips, 1,
            f"походов в Neo4j: {self.fake.round_trips}. До PERF-10 их было "
            f"пятнадцать, и заметная часть двух с половиной секунд уходила "
            f"не на счёт, а на дорогу",
        )

    def test_all_aggregates_still_asked(self):
        """
        Один поход не должен означать «часть перестали считать».
        Проверяем, что все статьи ответа по-прежнему запрашиваются.
        """
        self.stats()
        joined = " ".join(self.fake.statements_seen)
        for label in DEFAULT_COUNTS:
            self.assertIn(f":{label})", joined, f"перестали считать {label}")
        self.assertIn("Fingerprint", joined)
        self.assertIn("type(r)", joined)

    def test_callsites_scanned_once(self):
        """
        Три обхода метки `CallSite` сложены в один. Раньше каждый обходил
        одни и те же узлы заново — счётчик хранилища на предикате по
        свойству не работает.
        """
        self.stats()
        scans = [c for c in self.fake.statements_seen if "CallSite" in c]
        self.assertEqual(len(scans), 1,
                         f"обходов CallSite: {len(scans)}, ожидался один")

    def test_reports_its_own_timing(self):
        rep = self.stats()
        self.assertIn("timing", rep)
        self.assertEqual(rep["timing"]["round_trips"], 1)
        self.assertIsInstance(rep["timing"]["neo4j_ms"], (int, float))


class TestAnswerUnchanged(StatsCase):
    """
    Правка про скорость не имеет права трогать содержание.

    Тот же критерий, по которому принимали `PERF-6.1`: «качество поиска не
    изменилось до цифры». Здесь — состав и значения полей.
    """

    def test_shape_of_the_answer(self):
        rep = self.stats()
        for key in ("metadata", "code", "relations", "index", "graph_empty"):
            self.assertIn(key, rep)
        for key in ("objects", "attributes", "tabular_sections", "forms",
                    "enum_values", "types", "by_kind"):
            self.assertIn(key, rep["metadata"])
        for key in ("modules", "callables", "callsites", "callsites_resolved",
                    "callsites_unresolved", "callsites_object_method",
                    "resolve_coverage_pct"):
            self.assertIn(key, rep["code"])

    def test_counts_land_in_the_right_fields(self):
        rep = self.stats()
        self.assertEqual(rep["metadata"]["objects"], 200)
        self.assertEqual(rep["metadata"]["attributes"], 1500)
        self.assertEqual(rep["code"]["modules"], 400)
        self.assertEqual(rep["code"]["callables"], 2500)

    def test_fix4_arithmetic_survived_the_merge(self):
        """
        `FIX-4` — самая тонкая часть этого ответа, и она пережила слияние
        трёх запросов в один.

        Смысл: вызовы методов объектов платформы
        (`РезультатЗапроса.Выбрать()`) не пробел в графе — их
        :Callable-адресата не существует. В знаменатель покрытия они не
        идут, иначе метрика занижается (на Котировках было 72.66% при
        фактическом покрытии за 90%).

        900 разрешено, 100 нет, из них 40 — методы платформы.
        Пробелы: 100 − 40 = 60. Покрытие: 900 / (900 + 60) = 93.75%.
        """
        rep = self.stats()["code"]
        self.assertEqual(rep["callsites"], 1000)
        self.assertEqual(rep["callsites_resolved"], 900)
        self.assertEqual(rep["callsites_unresolved"], 60)
        self.assertEqual(rep["callsites_object_method"], 40)
        self.assertEqual(rep["resolve_coverage_pct"], 93.75)

    def test_empty_graph_is_a_valid_answer(self):
        """
        `FIX-3`: инструмент для того и нужен, чтобы отличить «объекта нет в
        конфигурации» от «граф не построен». Ошибку он отдаёт только при
        недоступной Neo4j.
        """
        self.fake.counts = {k: 0 for k in DEFAULT_COUNTS}
        self.fake.callsites = {"resolved": 0, "unresolved": 0,
                               "object_method": 0}
        rep = self.stats()
        self.assertTrue(rep["graph_empty"])
        self.assertEqual(rep["code"]["resolve_coverage_pct"], 0.0)

    def test_missing_fingerprint_is_explained(self):
        """`OBS-2`: отсутствие отпечатка — ответ, а не пустая строка."""
        self.fake.fingerprints = False
        rep = self.stats()
        self.assertIn("note", rep["index"]["xml"])
        self.assertIn("ни разу не индексировался", rep["index"]["xml"]["note"])

    def test_fingerprint_is_shortened_and_dated(self):
        rep = self.stats()["index"]["xml"]
        self.assertEqual(len(rep["fingerprint"]), 12)
        self.assertIn("indexed_at_iso", rep)
        self.assertIn("age_hours", rep)

    def test_relations_keep_their_names(self):
        rep = self.stats()
        self.assertEqual(rep["relations"]["CALLS"], 5000)


class TestBatchTransport(unittest.TestCase):
    """
    `_neo4j_many` — второй путь к базе, и он обязан вести себя как первый.

    Проект уже разбирал, чем кончаются два пути с разным поведением при
    отказе (`FIX-16`, `LOCK-1`). Поэтому обработка ошибок и логирование у
    них общие, и это проверяется.
    """

    @unittest.skipIf(server is None, "server.py не импортируется вне образа")
    def test_order_of_results_matches_order_of_statements(self):
        """
        На соответствие порядка опирается весь разбор ответа. Это гарантия
        API Neo4j, но зависимость от неё должна быть названа вслух — иначе
        перестановка строк тихо перепутает счётчики местами.
        """
        calls = []

        def fake(body, cypher_for_log="", timeout=None):
            calls.append([st["statement"] for st in body["statements"]])
            return {"results": [
                {"columns": ["c"], "data": [{"row": [i]}]}
                for i, _ in enumerate(body["statements"])
            ], "errors": []}

        saved, server._neo4j_query_raw = server._neo4j_query_raw, fake
        try:
            out = server._neo4j_many([("A", None), ("B", None), ("C", None)])
        finally:
            server._neo4j_query_raw = saved
        self.assertEqual(calls[0], ["A", "B", "C"])
        self.assertEqual([rows[0]["c"] for rows in out], [0, 1, 2])

    @unittest.skipIf(server is None, "server.py не импортируется вне образа")
    def test_cypher_error_still_raises(self):
        def fake(body, cypher_for_log="", timeout=None):
            raise RuntimeError("Neo4j: [{'code': 'SyntaxError'}]")

        saved, server._neo4j_query_raw = server._neo4j_query_raw, fake
        try:
            with self.assertRaises(RuntimeError):
                server._neo4j_many([("BROKEN", None)])
        finally:
            server._neo4j_query_raw = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
