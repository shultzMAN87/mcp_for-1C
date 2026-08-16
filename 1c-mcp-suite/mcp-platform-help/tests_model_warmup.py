"""
Тесты PERF-6: модели грузятся ровно один раз.

Главная проверка — та, которой не было и из-за отсутствия которой дефект
дожил до боевого замера: **при одновременном обращении загрузка происходит
один раз**. Прежний код ставил флаг ПОСЛЕ загрузки, поэтому поток прогрева
и первый запрос грузили две копии модели по 2 ГБ одновременно, конкурируя
за память и диск. Отсюда 23 секунды вместо примерно двенадцати.

Настоящие модели здесь не грузятся: torch и fastembed живут только в
образе. Проверяется поведение обёртки, а цена самих моделей меряется
командой `python3 model_warmup.py --breakdown` внутри контейнера.

Запуск:  python3 tests_model_warmup.py
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_warmup import LazyModel, Warmup, build_warmup  # noqa: E402

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent


class SlowLoader:
    """Медленная загрузка, считающая, сколько раз её позвали по-настоящему."""

    def __init__(self, seconds=0.2, value="модель", boom=None):
        self.seconds = seconds
        self.value = value
        self.boom = boom
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        time.sleep(self.seconds)
        if self.boom:
            raise self.boom
        return self.value


class TestLoadedOnce(unittest.TestCase):

    def test_single_call_loads(self):
        loader = SlowLoader(seconds=0.01)
        model = LazyModel("dense", loader)
        self.assertEqual(model.get(), "модель")
        self.assertEqual(loader.calls, 1)
        self.assertTrue(model.ready)

    def test_repeated_calls_do_not_reload(self):
        loader = SlowLoader(seconds=0.01)
        model = LazyModel("dense", loader)
        for _ in range(5):
            model.get()
        self.assertEqual(loader.calls, 1)

    def test_concurrent_callers_load_once(self):
        """
        Сердце PERF-6.

        Прежний `_get_model()` в этой ситуации грузил модель дважды: флаг
        `_model_loaded` ставился после загрузки, и пришедший во время неё
        поток видел «не загружено» и начинал грузить свою копию.

        Пять потоков, одна загрузка — иначе правки нет.
        """
        loader = SlowLoader(seconds=0.3)
        model = LazyModel("dense", loader)
        results = []

        def worker():
            results.append(model.get())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(loader.calls, 1, "модель загрузилась несколько раз")
        self.assertEqual(results, ["модель"] * 5)
        self.assertEqual(model.state()["loads"], 1)
        self.assertGreaterEqual(model.state()["waited_for_it"], 1,
                                "никто не ждал — значит, ждать было нечего")

    def test_waiters_get_the_same_object(self):
        """Не копию и не None: ждали ради этого."""
        loader = SlowLoader(seconds=0.2, value=object())
        model = LazyModel("dense", loader)
        got = []

        threads = [threading.Thread(target=lambda: got.append(model.get()))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(set(id(x) for x in got)), 1)


class TestFailures(unittest.TestCase):

    def test_failed_load_is_not_retried(self):
        """
        Повторять безнадёжную загрузку по 12 секунд на каждый запрос —
        худшее, что можно сделать. Прежний код это понимал (ставил флаг и в
        ветке ошибки), и поведение сохранено.
        """
        loader = SlowLoader(seconds=0.01, boom=RuntimeError("нет весов"))
        model = LazyModel("dense", loader)
        self.assertIsNone(model.get())
        self.assertIsNone(model.get())
        self.assertEqual(loader.calls, 1)
        self.assertIn("нет весов", model.state()["error"])
        self.assertFalse(model.state()["ready"])

    def test_timeout_returns_none_without_blocking(self):
        """
        Вызывающий может решить не ждать. По умолчанию ждём — ответ дороже
        скорости, — но возможность нужна: без неё нельзя быстро сказать
        «модель ещё грузится».
        """
        loader = SlowLoader(seconds=1.0)
        model = LazyModel("dense", loader)
        threading.Thread(target=model.get, daemon=True).start()
        time.sleep(0.05)

        started = time.monotonic()
        self.assertIsNone(model.get(timeout=0.1))
        self.assertLess(time.monotonic() - started, 0.6,
                        "ожидание с таймаутом всё равно заблокировало")

    def test_state_of_untouched_model(self):
        model = LazyModel("dense", SlowLoader())
        state = model.state()
        self.assertFalse(state["ready"])
        self.assertFalse(state["loading"])
        self.assertIsNone(state["seconds"])
        self.assertEqual(state["loads"], 0)


class TestWarmup(unittest.TestCase):

    def test_warms_every_model_not_just_dense(self):
        """
        До PERF-6 грелась только dense-модель, хотя гибридный поиск зовёт
        BM25 сразу следом: вся его загрузка лежала на первом запросе.
        """
        dense = SlowLoader(seconds=0.01, value="dense")
        sparse = SlowLoader(seconds=0.01, value="sparse")
        warmup = build_warmup(dense_loader=dense, sparse_loader=sparse)
        warmup.run()
        self.assertEqual(dense.calls, 1)
        self.assertEqual(sparse.calls, 1, "BM25 не прогрет")
        self.assertTrue(warmup.ready)

    def test_request_during_warmup_waits_instead_of_loading_again(self):
        """
        Расстановка, ради которой всё: контейнер стартовал, прогрев пошёл,
        и тут же пришёл первый запрос.
        """
        dense = SlowLoader(seconds=0.4, value="dense")
        sparse = SlowLoader(seconds=0.01, value="sparse")
        warmup = build_warmup(dense_loader=dense, sparse_loader=sparse)
        warmup.start_background()
        time.sleep(0.05)

        got = warmup.models["dense"].get()      # «запрос» пришёл во время прогрева
        self.assertEqual(got, "dense")
        self.assertEqual(dense.calls, 1, "запрос начал вторую загрузку")

    def test_broken_warmup_does_not_raise(self):
        """
        Прогрев — оптимизация, а не условие работы. Упавший прогрев не
        имеет права уронить старт сервера.
        """
        dense = SlowLoader(seconds=0.01, boom=RuntimeError("нет весов"))
        warmup = build_warmup(dense_loader=dense,
                              sparse_loader=SlowLoader(seconds=0.01))
        warmup.run()                       # не бросает
        self.assertFalse(warmup.ready)
        self.assertEqual(warmup.state()["state"], "done_with_errors")

    def test_state_says_what_is_happening(self):
        dense = SlowLoader(seconds=0.3, value="dense")
        warmup = build_warmup(dense_loader=dense,
                              sparse_loader=SlowLoader(seconds=0.01))
        self.assertEqual(warmup.state()["state"], "idle")
        warmup.start_background()
        time.sleep(0.05)
        self.assertEqual(warmup.state()["state"], "warming")
        warmup.models["dense"].get()
        warmup.models["sparse"].get()
        time.sleep(0.1)
        self.assertEqual(warmup.state()["state"], "ready")
        self.assertIn("разовая цена перезапуска", warmup.state()["note"])

    def test_state_is_json_friendly(self):
        import json
        warmup = build_warmup(dense_loader=SlowLoader(seconds=0.01),
                              sparse_loader=SlowLoader(seconds=0.01))
        warmup.run()
        json.dumps(warmup.state(), ensure_ascii=False)


class TestDelivery(unittest.TestCase):

    def test_module_is_delivered(self):
        sys.path.insert(0, str(SUITE))
        from tests_delivery import assert_delivered
        assert_delivered(self, "model_warmup.py")

    def test_server_uses_the_registry_not_its_own_flags(self):
        """
        Старые `_model_loaded` / `_sparse_loaded` не должны вернуться:
        именно флаг-после-загрузки и был дефектом.
        """
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("model_warmup", src)
        # Смотрим КОД, а не текст: в комментарии старый флаг упоминается
        # нарочно — там объяснено, чем он был плох. Первая редакция этого
        # теста краснела именно на объяснении, то есть требовала стереть
        # разбор дефекта вместе с дефектом.
        code = "\n".join(line.split("#", 1)[0]
                          for line in src.splitlines())
        for flag in ("_model_loaded", "_sparse_loaded"):
            self.assertNotIn(f"{flag} =", code,
                             f"флаг {flag} вернулся в код")

    def test_warmup_state_is_visible_in_stats(self):
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("warmup", src.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
