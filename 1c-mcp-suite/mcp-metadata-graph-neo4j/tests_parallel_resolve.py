"""
PERF-13 / PERF-15 — замеры и параллельность фазы 2.

Главное здесь — равенство: параллельный разбор и параллельный резолв
должны давать ровно то же, что последовательные (`workers=1`). Граф
строится в одном порядке, счётчики те же, факты для фикс-пойнта те же.
Если тест падает — параллельный путь что-то теряет или переставляет, и
`METADATA_WORKERS=1` на стенде вернёт прежнее поведение, пока не найдено.

Тесты, которым нужен fork (Linux, контейнер), на Windows пропускаются —
там параллельный путь и не включается (`resolve_workers` → 1).

Запуск: python3 tests_parallel_resolve.py
"""

from __future__ import annotations

import gc
import multiprocessing as mp
import os
import pickle
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bsl_parser
import bsl_resolver
from bsl_parser import (
    UNCLOSED_DECLS, ParsedModule, balanced_slices, frozen_for_fork,
    parse_bsl_text, resolve_workers, walk_workspace_bsl,
)
from bsl_resolver import (
    AMBIGUOUS, build_call_graph, build_index_from_modules, merge_pass_results,
)
from progress_log import GC_METER, GcMeter

HAS_FORK = "fork" in mp.get_all_start_methods()


# ─── синтетическая конфигурация ──────────────────────────────────────────
#
# Достаточно пёстрая, чтобы задеть все ветки резолва: вызовы общих модулей,
# локальные вызовы, неизвестные адресаты, менеджеры метаданных, факты для
# фикс-пойнта (возвращаемый тип функции из другого модуля и тип параметра
# по аргументу) — и модули разного веса, чтобы куски были неравными.

def _common_text(i: int, n: int) -> str:
    nxt = (i + 1) % n
    return (
        f"Функция Получить{i}() Экспорт\n"
        f"  Возврат Справочники.Товары{i % 3}.СоздатьЭлемент();\n"
        f"КонецФункции\n"
        f"Функция Обработать{i}(Параметр) Экспорт\n"
        f"  Параметр.Записать();\n"
        f"  Возврат Общий{nxt}.Получить{nxt}();\n"
        f"КонецФункции\n"
        + "".join(
            f"Процедура Шаг{i}_{k}()\n"
            f"  Объект = Общий{nxt}.Получить{nxt}();\n"
            f"  Объект.Записать();\n"
            f"  Общий{nxt}.Обработать{nxt}(Объект);\n"
            f"  Локальная{i}();\n"
            f"  НеИзвестный{k}.Метод();\n"
            f"  Сообщить(\"шаг\");\n"
            f"КонецПроцедуры\n"
            for k in range(1 + i % 7)
        )
        + f"Процедура Локальная{i}()\nКонецПроцедуры\n"
    )


def _synthetic_modules(n: int = 40) -> list[ParsedModule]:
    mods = []
    for i in range(n):
        mods.append(ParsedModule(
            module_id=f"CommonModule.Общий{i}", module_kind="CommonModule",
            parent_metadata_id=None,
            source_path=f"CommonModules/Общий{i}/Ext/Module.bsl",
            is_server=True, is_client=False,
            procedures=parse_bsl_text(_common_text(i, n)),
        ))
    return mods


def _index(mods):
    return build_index_from_modules(
        mods, metadata_objects={f"Товары{k}": f"Catalog.Товары{k}" for k in range(3)})


def _comparable(cg: dict) -> dict:
    """code_graph без времени: оно в stats может быть, и оно разное."""
    stats = {k: v for k, v in cg["stats"].items()
             if "sec" not in k and "time" not in k and "duration" not in k}
    return {**cg, "stats": stats}


# ─── вспомогательные функции параллельности ──────────────────────────────

class TestBalancedSlices(unittest.TestCase):

    def test_covers_everything_in_order(self):
        sizes = [5, 1, 1, 20, 3, 3, 3, 0, 7, 2]
        for n in range(1, 15):
            sl = balanced_slices(sizes, n)
            self.assertEqual(sl[0][0], 0)
            self.assertEqual(sl[-1][1], len(sizes))
            for (a, b, w), (c, _d, _w) in zip(sl, sl[1:]):
                self.assertEqual(b, c, "куски должны идти подряд")
            for a, b, w in sl:
                self.assertLess(a, b, "пустых кусков не бывает")
                self.assertEqual(w, sum(sizes[a:b]))
            self.assertLessEqual(len(sl), min(n, len(sizes)))

    def test_empty_and_zero_weights(self):
        self.assertEqual(balanced_slices([], 4), [])
        sl = balanced_slices([0, 0, 0], 2)
        self.assertEqual((sl[0][0], sl[-1][1]), (0, 3))

    def test_roughly_balanced(self):
        sl = balanced_slices([1] * 1000, 4)
        self.assertEqual([w for *_x, w in sl], [250, 250, 250, 250])


class TestResolveWorkers(unittest.TestCase):

    def _env(self, value):
        env = dict(os.environ)
        env.pop("METADATA_WORKERS", None)
        if value is not None:
            env["METADATA_WORKERS"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    @unittest.skipUnless(HAS_FORK, "нет fork — всегда 1 процесс")
    def test_values(self):
        with self._env("3"):
            self.assertEqual(resolve_workers(), 3)
        with self._env("1"):
            self.assertEqual(resolve_workers(), 1)
        with self._env("0"):
            self.assertEqual(resolve_workers(), 1)
        with self._env("много"), self.assertLogs("bsl_parser", "WARNING"):
            self.assertEqual(resolve_workers(), 1)
        with mock.patch("os.cpu_count", return_value=12):
            for v in (None, "auto", " AUTO ", ""):
                with self._env(v):
                    self.assertEqual(resolve_workers(), 4, v)
        with mock.patch("os.cpu_count", return_value=2), self._env("auto"):
            self.assertEqual(resolve_workers(), 1)
        self.assertEqual(resolve_workers(5), 5, "явное число важнее окружения")

    def test_without_fork_always_one(self):
        with mock.patch("multiprocessing.get_all_start_methods",
                        return_value=["spawn"]):
            self.assertEqual(resolve_workers(8), 1)


class TestFrozenForFork(unittest.TestCase):

    def test_unfreezes_what_it_froze(self):
        gc.unfreeze()
        with frozen_for_fork():
            self.assertGreater(gc.get_freeze_count(), 0)
        self.assertEqual(gc.get_freeze_count(), 0)

    def test_keeps_earlier_freeze(self):
        # METADATA_GC_FREEZE заморозил раньше — не наше, не размораживаем.
        gc.unfreeze()
        gc.freeze()
        try:
            n = gc.get_freeze_count()
            with frozen_for_fork():
                pass
            self.assertGreaterEqual(gc.get_freeze_count(), n)
        finally:
            gc.unfreeze()


class TestMergePassResults(unittest.TestCase):

    def test_merge(self):
        def part(tag, r, u, s, nc, reasons):
            return {"callsite_nodes": [tag], "edges": [tag, tag],
                    "param_facts": [(tag, 1)], "return_facts": [],
                    "stats_resolved": r, "stats_unresolved": u,
                    "stats_skipped": s, "stats_non_config": nc,
                    "reason_counts": reasons}
        out = merge_pass_results([part("a", 1, 2, 3, 1, {"x": 1}),
                                  part("b", 10, 20, 30, 2, {"x": 2, "y": 5})])
        self.assertEqual(out["callsite_nodes"], ["a", "b"])
        self.assertEqual(out["edges"], ["a", "a", "b", "b"])
        self.assertEqual(out["param_facts"], [("a", 1), ("b", 1)])
        self.assertEqual((out["stats_resolved"], out["stats_unresolved"],
                          out["stats_skipped"], out["stats_non_config"]),
                         (11, 22, 33, 3))
        self.assertEqual(out["reason_counts"], {"x": 3, "y": 5})

    def test_ambiguous_survives_pickle(self):
        # Результат дочернего процесса приходит через pickle, а сентинел
        # сравнивается через `is` — он должен остаться тем же объектом.
        self.assertIs(pickle.loads(pickle.dumps(AMBIGUOUS)), AMBIGUOUS)


# ─── главное: параллельно = последовательно ──────────────────────────────

# Функции для пула должны быть на уровне модуля: pickle передаёт их
# дочернему процессу по имени, локальную функцию он не найдёт.
_REAL_CHUNK = bsl_resolver._resolve_chunk
_CHUNK_CALLS = mp.get_context("fork").Value("i", 0) if HAS_FORK else None


def _counting_chunk(bounds):
    with _CHUNK_CALLS.get_lock():
        _CHUNK_CALLS.value += 1
    return _REAL_CHUNK(bounds)


def _failing_chunk(bounds):
    raise RuntimeError("сбой в процессе-работнике")


@unittest.skipUnless(HAS_FORK, "нет fork (Windows) — параллельный путь не включается")
class TestParallelResolveEqualsSequential(unittest.TestCase):

    def setUp(self):
        # Порог «стоит ли распараллеливать» опускаем, чтобы маленькая
        # синтетика пошла параллельным путём.
        self._p = mock.patch.object(bsl_resolver, "PARALLEL_MIN_ITEMS", 1)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_same_code_graph(self):
        mods = _synthetic_modules()
        seq = build_call_graph(mods, _index(mods), workers=1)
        self.assertGreater(seq["stats"]["fixpoint_iterations"], 1,
                           "синтетика должна задеть фикс-пойнт")
        self.assertGreater(len(seq["edges"]), 100, "синтетика слишком бедная")
        for workers in (2, 3):
            par = build_call_graph(mods, _index(mods), workers=workers)
            self.assertEqual(_comparable(par), _comparable(seq), workers)

    def test_parallel_path_really_used(self):
        mods = _synthetic_modules(12)
        _CHUNK_CALLS.value = 0
        with mock.patch.object(bsl_resolver, "_resolve_chunk", _counting_chunk):
            build_call_graph(mods, _index(mods), workers=2)
        self.assertGreater(_CHUNK_CALLS.value, 1)

    def test_small_input_goes_sequential(self):
        mods = _synthetic_modules(4)
        with mock.patch.object(bsl_resolver, "PARALLEL_MIN_ITEMS", 10 ** 9), \
             mock.patch.object(bsl_resolver, "_resolve_chunk",
                               side_effect=AssertionError("не должен звать")):
            cg = build_call_graph(mods, _index(mods), workers=4)
        self.assertTrue(cg["edges"])

    def test_progress_counts_all_procedures(self):
        mods = _synthetic_modules(10)
        steps = []

        class Prog:
            def step(self, n=1, extra=""):
                steps.append(n)
        bsl_resolver.resolve_pass_any(mods, _index(mods), {}, {}, {},
                                      progress=Prog(), workers=3)
        self.assertEqual(sum(steps), sum(len(m.procedures) for m in mods))

    def test_worker_error_fails_the_run(self):
        mods = _synthetic_modules(6)
        with mock.patch.object(bsl_resolver, "_resolve_chunk", _failing_chunk):
            with self.assertRaises(RuntimeError):
                build_call_graph(mods, _index(mods), workers=2)
        self.assertEqual(bsl_resolver._RESOLVE_STATE, {}, "состояние не убрано")


@unittest.skipUnless(HAS_FORK, "нет fork (Windows) — параллельный путь не включается")
class TestParallelParseEqualsSequential(unittest.TestCase):

    def setUp(self):
        self._p = mock.patch.object(bsl_parser, "PARALLEL_MIN_ITEMS", 1)
        self._p.start()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        rels = []
        for i in range(30):
            rel = f"CommonModules/Общий{i}/Ext/Module.bsl"
            rels.append(rel)
            (root / rel).parent.mkdir(parents=True)
            (root / rel).write_text(_common_text(i, 30), encoding="utf-8")
        # Объявление без конца — попадает в UNCLOSED_DECLS.
        rel = "Catalogs/Товары/Ext/ObjectModule.bsl"
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_text("Процедура Сломана()\n  а = 1;\n", encoding="utf-8")
        rels.append(rel)
        rels.append("Странное/Место.bsl")                      # вне схемы путей
        (root / "Странное").mkdir()
        (root / "Странное/Место.bsl").write_text("", encoding="utf-8")
        rels.append("CommonModules/Пропал/Ext/Module.bsl")     # нет на диске
        self.rels = sorted(rels)

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def _walk(self, workers):
        mods = walk_workspace_bsl(self.root, paths=self.rels, workers=workers)
        return mods, list(UNCLOSED_DECLS)

    def test_same_modules_and_unclosed(self):
        seq_mods, seq_unclosed = self._walk(1)
        self.assertEqual(len(seq_mods), 31)
        self.assertTrue(seq_unclosed, "объявление без конца должно попасть в отчёт")
        for workers in (2, 3):
            par_mods, par_unclosed = self._walk(workers)
            self.assertEqual(par_mods, seq_mods, workers)
            self.assertEqual(par_unclosed, seq_unclosed, workers)
        self.assertEqual(bsl_parser._PARSE_STATE, {}, "состояние не убрано")


# ─── PERF-13: счётчик сборщика мусора и профиль ──────────────────────────

class TestGcMeter(unittest.TestCase):

    def test_start_stop_manage_callback(self):
        m = GcMeter()
        m.start()
        m.start()                       # повторный старт не дублирует
        self.assertEqual(sum(cb == m._cb for cb in gc.callbacks), 1)
        gc.collect()
        self.assertGreaterEqual(m.collections[2], 1)
        m.stop()
        m.stop()
        self.assertNotIn(m._cb, gc.callbacks)
        before = m.collections[2]
        gc.collect()
        self.assertEqual(m.collections[2], before, "после stop не считает")

    def test_share_text(self):
        m = GcMeter()
        self.assertEqual(m.share_text(), "", "выключен — пусто")
        m.start()
        try:
            t0 = m._mark_wall
            m.total += 0.5              # полсекунды сборок за 2 с
            self.assertEqual(m.share_text(now=t0 + 2.0), "gc 25%")
            self.assertEqual(m.share_text(now=t0 + 4.0), "gc 0%")
            self.assertEqual(m.share_text(now=t0 + 4.0), "", "нулевой интервал")
        finally:
            m.stop()

    def test_summary_mentions_numbers(self):
        m = GcMeter()
        m.total = 12.5
        m.collections = [100, 10, 3]
        text = m.summary()
        for piece in ("12", "100", "10", "3"):
            self.assertIn(piece, text)


class TestMeasuredBuild(unittest.TestCase):

    def _run(self, profile: bool):
        import indexer
        mods = _synthetic_modules(6)
        env = {"METADATA_PROFILE": "true" if profile else "false"}
        GC_METER.start()
        with mock.patch.dict(os.environ, env):
            if profile:
                with self.assertLogs(indexer.log_bsl, "INFO") as cm:
                    cg = indexer._build_call_graph_measured(mods, _index(mods))
                return cg, "\n".join(cm.output)
            return indexer._build_call_graph_measured(mods, _index(mods)), ""

    def test_profile_gives_same_graph_and_stops_meter(self):
        plain, _ = self._run(False)
        self.assertFalse(GC_METER.active, "счётчик должен остановиться")
        profiled, logs = self._run(True)
        self.assertFalse(GC_METER.active)
        self.assertEqual(_comparable(profiled), _comparable(plain))
        self.assertIn("METADATA_PROFILE", logs)
        self.assertIn("tottime", logs)

    def test_meter_stops_on_error(self):
        import indexer
        GC_METER.start()
        with mock.patch.object(indexer, "build_call_graph",
                               side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                indexer._build_call_graph_measured([], None)
        self.assertFalse(GC_METER.active)


if __name__ == "__main__":
    t0 = time.time()
    unittest.main(verbosity=1)
