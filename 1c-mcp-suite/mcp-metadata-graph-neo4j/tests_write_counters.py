"""
FIX-31 / FIX-32. Счётчик, который считает не то, о чём его спрашивают.
======================================================================

`FIX-31`. 18 августа запрос перепривязки владения отчитался
`relinked = 83` при 99 фактически созданных рёбрах. `FIX-29` — тот же
дефект зеркально: писатель сказал 231 102, в базе оказалось 231 101.

Общее у обоих — не арифметика, а подмена вопроса. `RETURN count(*)` после
`MERGE` отвечает «сколько строк дошло до записи», а читали его как
«сколько связей создано». Пока разница выглядела погрешностью, её
объясняли схлопыванием пар; на перепривязке она вышла в 16 % и в другую
сторону.

Почему это блокировало заход. Сторож `FIX-15` («отправлено N, записано M,
разница вслух») — единственное, что стоит между проектом и повторением
истории со 158 961 процедурой без владельца. Сторож, чьё число
ошибается на 16 %, не заметит недостачу в 16 %.

Что проверяется здесь:

  • три числа считаются раздельно и означают разное;
  • `created` берётся из статистики транзакции, а не выводится;
  • отсутствие статистики даёт None, а не ноль;
  • сторож `FIX-15` по-прежнему меряет `sent − matched`, иначе повторная
    запись тех же рёбер выглядела бы стопроцентной потерей;
  • `FIX-32`: производный список папок равен источнику.

Neo4j подменён стабом: правка про то, КАКОЕ число берётся из ответа базы,
а не про то, как база считает.

Запуск:  python3 tests_write_counters.py -v
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from graph_writer import (  # noqa: E402
    Neo4j, _report_merge_dedup, _write_counted, edges_created,
    log_edge_report, write_edges,
)
from incremental import _TAIL_TOP_DIRS, relink_code_ownership  # noqa: E402
from metadata_xml import KINDS  # noqa: E402


# ─── стабы ───────────────────────────────────────────────────────────────


class StatsNeo:
    """
    Стаб, отвечающий и строками, и статистикой транзакции.

    `created` задаётся списком — по одному значению на вызов, чтобы можно
    было воспроизвести именно ту ситуацию, где счётчик строк и факт
    расходятся.
    """

    def __init__(self, created: list, rows_value=None):
        self.created = list(created)
        self.rows_value = rows_value
        self.calls: list[tuple[str, dict]] = []

    def _next_created(self):
        return self.created.pop(0) if self.created else 0

    def _rows(self, cypher, params):
        if self.rows_value is not None:
            return [dict(self.rows_value)]
        if "count(*) AS written" in cypher:
            return [{"written": len((params or {}).get("rows", []))}]
        return [{"n": 0}]

    def rows(self, cypher, parameters=None):
        self.calls.append((cypher, parameters or {}))
        return self._rows(cypher, parameters)

    def rows_with_stats(self, cypher, parameters=None):
        self.calls.append((cypher, parameters or {}))
        made = self._next_created()
        stats = {} if made is None else {"relationships_created": made}
        return self._rows(cypher, parameters), stats


class PlainNeo:
    """Стаб БЕЗ статистики транзакции — прежняя база, прежний ответ."""

    def __init__(self, rows_value=None):
        self.rows_value = rows_value
        self.calls: list[tuple[str, dict]] = []

    def rows(self, cypher, parameters=None):
        self.calls.append((cypher, parameters or {}))
        if self.rows_value is not None:
            return [dict(self.rows_value)]
        if "count(*) AS written" in cypher:
            return [{"written": len((parameters or {}).get("rows", []))}]
        return [{"n": 0}]


def _edge(src: str, dst: str, rel: str = "CONTAINS") -> dict:
    return {"rel": rel, "src": src, "dst": dst, "props": {}}


# ─── клиент: includeStats ────────────────────────────────────────────────


class TestClientAsksForStats(unittest.TestCase):
    """
    Ключ `includeStats` обязан уходить в базу — иначе `created` не появится
    ниоткуда, и весь FIX-31 сведётся к переименованию прежнего числа.
    """

    def setUp(self):
        self.neo = Neo4j("http://neo4j:7474", "neo4j", "пароль")
        self.sent: list[dict] = []

        def fake_query(cypher, parameters=None, include_stats=False):
            self.sent.append({"cypher": cypher, "include_stats": include_stats})
            result = {"columns": ["written"], "data": [{"row": [7]}]}
            if include_stats:
                result["stats"] = {"relationships_created": 5}
            return {"results": [result], "errors": []}

        self.neo.query = fake_query  # type: ignore[method-assign]

    def test_rows_does_not_ask_for_stats(self):
        """Обычное чтение не должно платить за статистику, которую не спросили."""
        self.neo.rows("MATCH (n) RETURN n")
        self.assertFalse(self.sent[0]["include_stats"])

    def test_rows_with_stats_asks(self):
        rows, stats = self.neo.rows_with_stats("MERGE (a)-[:R]->(b)")
        self.assertTrue(self.sent[0]["include_stats"])
        self.assertEqual(rows, [{"written": 7}])
        self.assertEqual(stats["relationships_created"], 5)

    def test_missing_stats_block_is_empty_dict_not_crash(self):
        self.neo.query = lambda c, p=None, include_stats=False: {  # type: ignore
            "results": [{"columns": [], "data": []}], "errors": []}
        rows, stats = self.neo.rows_with_stats("MERGE (a)-[:R]->(b)")
        self.assertEqual(rows, [])
        self.assertEqual(stats, {})


class TestIncludeStatsReachesThePayload(unittest.TestCase):
    """
    Тот же вопрос на уровень ниже: ключ должен лежать В STATEMENT, а не
    рядом с ним. Ошибиться тут легко, а последствие тихое — Neo4j
    незнакомый ключ верхнего уровня игнорирует, статистика не приходит, и
    `created` навсегда равен None.
    """

    def test_key_is_inside_the_statement(self):
        neo = Neo4j("http://neo4j:7474", "neo4j", "пароль")
        captured = {}

        class FakeResponse:
            status = 200

            def read(self):
                import json
                return json.dumps(
                    {"results": [{"columns": [], "data": [], "stats": {}}],
                     "errors": []}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            import json
            captured.update(json.loads(req.data))
            return FakeResponse()

        import urllib.request
        saved = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            neo.query("MERGE (a)-[:R]->(b)", include_stats=True)
        finally:
            urllib.request.urlopen = saved

        self.assertNotIn("includeStats", captured,
                         "ключ верхнего уровня Neo4j молча проигнорирует")
        self.assertTrue(captured["statements"][0]["includeStats"])


# ─── три числа ───────────────────────────────────────────────────────────


class TestThreeNumbersAreDifferent(unittest.TestCase):

    def test_created_comes_from_the_database(self):
        neo = StatsNeo(created=[99], rows_value={"written": 83})
        w = _write_counted(neo, "MERGE …", [{"src": "a", "dst": "b"}])
        self.assertEqual(w.sent, 1)
        self.assertEqual(w.matched, 83, "счётчик строк — как был")
        self.assertEqual(w.created, 99, "факт — из статистики транзакции")

    def test_without_stats_created_is_none_not_zero(self):
        """
        «Не создано ничего» и «нечем измерить» — разные ответы. Ноль на
        месте второго дал бы третий способ соврать, причём молча.
        """
        neo = PlainNeo(rows_value={"written": 5})
        w = _write_counted(neo, "MERGE …", [{"src": "a", "dst": "b"}])
        self.assertEqual(w.matched, 5)
        self.assertIsNone(w.created)

    def test_one_unmeasured_chunk_poisons_the_group(self):
        """
        Сумма из «созданных» и «неизвестно скольких» выглядит точной и ею
        не является. Одна неизмеренная порция делает неизмеренной группу.
        """
        neo = StatsNeo(created=[3, None, 4])
        report: dict = {}
        write_edges(neo, [_edge(f"s{i}", f"d{i}") for i in range(6)],
                    batch=2, log_progress=False, report=report)
        self.assertIsNone(report["CONTAINS"]["created"])
        self.assertEqual(report["CONTAINS"]["sent"], 6)


class TestMergeCollapsesPairs(unittest.TestCase):
    """
    Мера приёмки PLAN-9: две записи одной пары дают created 1, а не 2.
    """

    def test_duplicate_pair_creates_one_edge(self):
        neo = StatsNeo(created=[1])
        report: dict = {}
        counters = write_edges(
            neo,
            [_edge("CommonModule.X", "CommonModule.X.Проц", "HAS_METHOD"),
             _edge("CommonModule.X", "CommonModule.X.Проц", "HAS_METHOD")],
            log_progress=False, report=report)
        self.assertEqual(report["HAS_METHOD"]["sent"], 2)
        self.assertEqual(report["HAS_METHOD"]["matched"], 2,
                         "до записи дошли обе строки")
        self.assertEqual(report["HAS_METHOD"]["created"], 1,
                         "а связь создалась одна — это и есть FIX-29")
        self.assertEqual(counters["HAS_METHOD"], 2,
                         "прежний ответ не меняется: его читают 13 мест")

    def test_repeat_write_creates_nothing_and_is_not_a_shortfall(self):
        """
        Повторная запись тех же рёбер: created 0 — и это здоровье, а не
        потеря. Если бы сторож FIX-15 мерил created, он завопил бы здесь.
        """
        neo = StatsNeo(created=[0])
        log_ = logging.getLogger("graph_writer")
        records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                records.append(record.getMessage())

        handler = Catcher()
        log_.addHandler(handler)
        log_.setLevel(logging.INFO)
        try:
            report: dict = {}
            write_edges(neo, [_edge("a", "b")], log_progress=False,
                        report=report)
        finally:
            log_.removeHandler(handler)

        self.assertEqual(report["CONTAINS"]["created"], 0)
        self.assertFalse([r for r in records if "недоста" in r.lower()],
                         "повторная запись не есть недостача")


class TestShortfallStillMeasuresMatched(unittest.TestCase):
    """
    `FIX-15` меряет строки, не нашедшие узлов (класс FIX-14). Это
    `sent − matched`, и подменять его на `created` нельзя.
    """

    def setUp(self):
        self.log = logging.getLogger("graph_writer")
        self.records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record.getMessage())

        self.handler = Catcher()
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def test_rows_that_found_no_nodes_are_announced(self):
        neo = StatsNeo(created=[2], rows_value={"written": 2})
        write_edges(neo, [_edge(f"s{i}", f"d{i}") for i in range(10)],
                    log_progress=False)
        text = " ".join(self.records)
        self.assertTrue(any(w in text for w in ("отправлено", "записано")),
                        f"сторож FIX-15 промолчал на недостаче: {self.records}")


class TestCreatedAbovePredictionIsAnnounced(unittest.TestCase):
    """
    Ровно `relinked = 83 при 99`: создано больше, чем строк после
    схлопывания. Раньше это было невидимо — предсказание печаталось, а
    сравнить его было не с чем.
    """

    def setUp(self):
        self.log = logging.getLogger("graph_writer")
        self.records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record.getMessage())

        self.handler = Catcher()
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def test_warns_when_fact_exceeds_prediction(self):
        group = [{"src": "A", "dst": "B"}, {"src": "A", "dst": "B"},
                 {"src": "A", "dst": "C"}]
        _report_merge_dedup("HAS_METHOD", group, written=3, created=5)
        text = " ".join(self.records)
        self.assertIn("создано 5", text)
        self.assertIn("предсказанных 2", text)

    def test_silent_when_fact_is_below_prediction(self):
        """Часть рёбер могла существовать до записи — это законно."""
        group = [{"src": "A", "dst": "B"}, {"src": "A", "dst": "B"}]
        _report_merge_dedup("HAS_METHOD", group, written=2, created=0)
        self.assertFalse([r for r in self.records if "⚠" in r])


class TestEdgeReportHelpers(unittest.TestCase):

    def test_edges_created_reads_the_report(self):
        report = {"CALLS": {"sent": 10, "matched": 10, "created": 7},
                  "HAS_METHOD": {"sent": 3, "matched": 3, "created": None}}
        self.assertEqual(edges_created(report),
                         {"CALLS": 7, "HAS_METHOD": None})

    def test_edges_created_on_empty_report(self):
        self.assertEqual(edges_created({}), {})
        self.assertEqual(edges_created(None), {})

    def test_log_is_silent_when_all_three_agree(self):
        log_ = logging.getLogger("тест-отчёта-рёбер")
        records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                records.append(record.getMessage())

        log_.addHandler(Catcher())
        log_.setLevel(logging.INFO)
        log_edge_report({"CALLS": {"sent": 5, "matched": 5, "created": 5}}, log_)
        self.assertEqual(records, [],
                         "отчёт, который печатается всегда, перестают читать")

    def test_log_speaks_when_not_measured(self):
        log_ = logging.getLogger("тест-отчёта-рёбер-2")
        records: list[str] = []

        class Catcher(logging.Handler):
            def emit(inner, record):  # noqa: N805
                records.append(record.getMessage())

        log_.addHandler(Catcher())
        log_.setLevel(logging.INFO)
        log_edge_report({"CALLS": {"sent": 5, "matched": 5, "created": None}},
                        log_)
        self.assertIn("не измерено", " ".join(records),
                      "молчание должно означать «сошлось», а не «не считали»")


# ─── перепривязка владения ───────────────────────────────────────────────


class TestRelinkReportsTheFact(unittest.TestCase):
    """
    Тот самый сценарий: счётчик строк 83, создано 99.
    """

    def test_has_method_is_the_created_count(self):
        neo = StatsNeo(created=[70, 29], rows_value={"n": 43})
        result = relink_code_ownership(neo, "Catalog.Контрагенты")
        self.assertEqual(result["matched"], 86, "43 + 43 — счётчик строк")
        self.assertEqual(result["created"], 99, "70 + 29 — факт")
        self.assertEqual(result["has_method"], 99,
                         "наружу уходит факт, а не оценка")

    def test_healthy_graph_relinks_nothing(self):
        """
        Ноль здесь — хороший ответ: чинить было нечего. Раньше на том же
        графе приезжало число «подтверждённых» связей, неотличимое от
        числа восстановленных.
        """
        neo = StatsNeo(created=[0, 0], rows_value={"n": 231})
        result = relink_code_ownership(neo, "Catalog.Контрагенты")
        self.assertEqual(result["has_method"], 0)
        self.assertEqual(result["matched"], 462)

    def test_falls_back_to_matched_without_stats(self):
        """Прежняя база — прежний ответ, а не None в поле числа."""
        neo = PlainNeo(rows_value={"n": 12})
        result = relink_code_ownership(neo, "Catalog.Контрагенты")
        self.assertEqual(result["has_method"], 24)
        self.assertIsNone(result["created"])


# ─── FIX-32: производный список ──────────────────────────────────────────


class TestTailTopDirsMatchKinds(unittest.TestCase):
    """
    Пятое расхождение производного списка с источником в проекте.
    Предыдущие четыре — три `COPY` в Dockerfile'ах и пара в генераторах
    лок-файлов — чинились так же: список выводится, тест сверяет множества.
    """

    def test_every_kind_directory_is_known_to_norm_rel(self):
        missing = sorted({k[0] for k in KINDS} - _TAIL_TOP_DIRS)
        self.assertFalse(
            missing,
            f"виды есть в KINDS, но неизвестны _norm_rel: {missing}\n"
            f"Путь /workspace/<Вид>/X.xml уйдёт в path_outside_src_root, и "
            f"частичное обновление молча пропустит файл.",
        )

    def test_no_ghosts_beyond_kinds_and_extension(self):
        extra = sorted(_TAIL_TOP_DIRS - {k[0] for k in KINDS}
                       - {"tests-extension"})
        self.assertFalse(extra, f"в списке папки, которых нет в KINDS: {extra}")

    def test_fix8_kinds_are_in(self):
        """Восемь видов FIX-8 — ровно те, которых не хватало."""
        for d in ("Sequences", "XDTOPackages", "StyleItems", "Styles",
                  "WSReferences", "CommandGroups", "DocumentNumerators",
                  "ExternalDataSources"):
            self.assertIn(d, _TAIL_TOP_DIRS)

    def test_extension_dir_survives(self):
        self.assertIn("tests-extension", _TAIL_TOP_DIRS)


class TestSequencePathPassesPartialUpdate(unittest.TestCase):
    """Сквозная проверка: путь из FIX-8-вида доезжает до относительного."""

    def test_absolute_watcher_path_resolves(self):
        from incremental import _norm_rel
        self.assertEqual(
            _norm_rel(Path("/data/1c-src"), "/workspace/Sequences/Партии.xml"),
            "Sequences/Партии.xml",
        )

    def test_xdto_package_too(self):
        from incremental import _norm_rel
        self.assertEqual(
            _norm_rel(Path("/data/1c-src"), "/workspace/XDTOPackages/Обмен.xml"),
            "XDTOPackages/Обмен.xml",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
