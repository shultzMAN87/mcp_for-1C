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

from model_warmup import LazyOnce, Warmup, build_warmup  # noqa: E402

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
        model = LazyOnce("dense", loader)
        self.assertEqual(model.get(), "модель")
        self.assertEqual(loader.calls, 1)
        self.assertTrue(model.ready)

    def test_repeated_calls_do_not_reload(self):
        loader = SlowLoader(seconds=0.01)
        model = LazyOnce("dense", loader)
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
        model = LazyOnce("dense", loader)
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
        model = LazyOnce("dense", loader)
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
        model = LazyOnce("dense", loader)
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
        model = LazyOnce("dense", loader)
        threading.Thread(target=model.get, daemon=True).start()
        time.sleep(0.05)

        started = time.monotonic()
        self.assertIsNone(model.get(timeout=0.1))
        self.assertLess(time.monotonic() - started, 0.6,
                        "ожидание с таймаутом всё равно заблокировало")

    def test_state_of_untouched_model(self):
        model = LazyOnce("dense", SlowLoader())
        state = model.state()
        self.assertFalse(state["ready"])
        self.assertFalse(state["loading"])
        self.assertIsNone(state["seconds"])
        self.assertEqual(state["loads"], 0)


def _warmup(dense=None, sparse=None, qdrant=None, **kw):
    """
    Реестр целиком из заглушек.

    Отдельная обёртка нужна ровно по одной причине: `build_warmup()` без
    аргументов берёт НАСТОЯЩИЕ загрузчики, а настоящий загрузчик клиента
    импортирует `qdrant_client`, которого на хосте может не быть. Забыть
    третий аргумент — значит получить набор, который зелёный на машине с
    пакетом и красный без него.
    """
    return build_warmup(
        dense_loader=dense or SlowLoader(seconds=0.01, value="dense"),
        sparse_loader=sparse or SlowLoader(seconds=0.01, value="sparse"),
        qdrant_loader=qdrant or SlowLoader(seconds=0.01, value="qdrant"),
        **kw)


class TestWarmup(unittest.TestCase):

    def test_warms_every_part_not_just_dense(self):
        """
        До PERF-6 грелась только dense-модель, хотя гибридный поиск зовёт
        BM25 сразу следом: вся его загрузка лежала на первом запросе. До
        PERF-6.1 не грелся и клиент Qdrant — его секунда с лишним осталась
        последней неоплаченной строкой первого запроса.
        """
        dense = SlowLoader(seconds=0.01, value="dense")
        sparse = SlowLoader(seconds=0.01, value="sparse")
        qdrant = SlowLoader(seconds=0.01, value="qdrant")
        warmup = _warmup(dense, sparse, qdrant)
        warmup.run()
        self.assertEqual(dense.calls, 1)
        self.assertEqual(sparse.calls, 1, "BM25 не прогрет")
        self.assertEqual(qdrant.calls, 1, "клиент Qdrant не прогрет")
        self.assertTrue(warmup.ready)

    def test_qdrant_is_warmed_first(self):
        """
        PERF-6.1. Порядок здесь — не косметика: прогрев идёт одним потоком
        последовательно, и запросу, пришедшему на пятой секунде, доступно
        ровно то, что успело загрузиться. Клиент грузится секунду против
        двенадцати у dense, а нужен раньше — на нём держатся `lookup`,
        `stats` и определение схемы коллекции, которым модель не нужна.
        """
        self.assertEqual(list(_warmup().parts)[0], "qdrant")

    def test_request_during_warmup_waits_instead_of_loading_again(self):
        """
        Расстановка, ради которой всё: контейнер стартовал, прогрев пошёл,
        и тут же пришёл первый запрос.
        """
        dense = SlowLoader(seconds=0.4, value="dense")
        warmup = _warmup(dense=dense)
        warmup.start_background()
        time.sleep(0.05)

        got = warmup.parts["dense"].get()      # «запрос» пришёл во время прогрева
        self.assertEqual(got, "dense")
        self.assertEqual(dense.calls, 1, "запрос начал вторую загрузку")

    def test_broken_warmup_does_not_raise(self):
        """
        Прогрев — оптимизация, а не условие работы. Упавший прогрев не
        имеет права уронить старт сервера.
        """
        dense = SlowLoader(seconds=0.01, boom=RuntimeError("нет весов"))
        warmup = _warmup(dense=dense)
        warmup.run()                       # не бросает
        self.assertFalse(warmup.ready)
        self.assertEqual(warmup.state()["state"], "done_with_errors")

    def test_state_says_what_is_happening(self):
        dense = SlowLoader(seconds=0.3, value="dense")
        warmup = _warmup(dense=dense)
        self.assertEqual(warmup.state()["state"], "idle")
        warmup.start_background()
        time.sleep(0.05)
        self.assertEqual(warmup.state()["state"], "warming")
        for name in ("qdrant", "dense", "sparse"):
            warmup.parts[name].get()
        time.sleep(0.1)
        self.assertEqual(warmup.state()["state"], "ready")
        self.assertIn("разовая цена перезапуска", warmup.state()["note"])

    def test_state_is_json_friendly(self):
        import json
        warmup = _warmup()
        warmup.run()
        text = json.dumps(warmup.state(), ensure_ascii=False)
        self.assertIn("qdrant", text)

    def test_state_calls_the_registry_parts_not_models(self):
        """
        Клиент Qdrant моделью не является. Поле `warmup.models.qdrant` в
        ответе `stats` было бы расхождением слова и дела ровно в том месте,
        ради честности которого затевался весь заход.
        """
        state = _warmup().state()
        self.assertIn("parts", state)
        self.assertNotIn("models", state)


class TestFinalStep(unittest.TestCase):
    """
    Шаг `then` — то, что имеет смысл только после прогрева и загрузкой не
    является: у справки это предварительное определение схемы коллекции,
    которому нужен уже созданный клиент.
    """

    def test_then_runs_after_every_part(self):
        seen = {}
        warmup = _warmup()

        def then(say):
            seen["ready"] = warmup.ready

        warmup.run(then=then)
        self.assertTrue(seen.get("ready"),
                        "заключительный шаг пошёл раньше, чем всё загрузилось")

    def test_then_failure_does_not_break_the_warmup(self):
        """
        По той же причине, по которой прогрев не роняет упавшая загрузка:
        это ускорение, а не условие работы.
        """
        warmup = _warmup()

        def then(say):
            raise RuntimeError("Qdrant не ответил")

        warmup.run(then=then)               # не бросает
        self.assertTrue(warmup.ready)
        self.assertEqual(warmup.state()["state"], "ready")

    def test_then_is_optional(self):
        warmup = _warmup()
        warmup.run()
        self.assertTrue(warmup.ready)

    def test_background_warmup_carries_the_step(self):
        """
        Сервер зовёт именно фоновый вариант — если `then` теряется по
        дороге в поток, на стенде это не видно ничем, кроме лишней
        задержки первого запроса.
        """
        done = threading.Event()
        warmup = _warmup()
        warmup.start_background(then=lambda say: done.set())
        self.assertTrue(done.wait(timeout=10),
                        "заключительный шаг не выполнился в фоне")


class TestDelivery(unittest.TestCase):

    def test_module_is_delivered(self):
        sys.path.insert(0, str(SUITE))
        from tests_delivery import assert_delivered
        assert_delivered(self, "model_warmup.py")

    def test_server_uses_the_registry_not_its_own_flags(self):
        """
        Старые `_model_loaded` / `_sparse_loaded` / `_qclient_loaded` не
        должны вернуться: именно флаг-после-загрузки и был дефектом. Третий
        появился в списке после PERF-6.1 — клиент Qdrant болел тем же, что
        и модели, просто дешевле.
        """
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("model_warmup", src)
        # Смотрим КОД, а не текст: в комментарии старый флаг упоминается
        # нарочно — там объяснено, чем он был плох. Первая редакция этого
        # теста краснела именно на объяснении, то есть требовала стереть
        # разбор дефекта вместе с дефектом.
        code = "\n".join(line.split("#", 1)[0]
                          for line in src.splitlines())
        for flag in ("_model_loaded", "_sparse_loaded", "_qclient_loaded"):
            self.assertNotIn(f"{flag} =", code,
                             f"флаг {flag} вернулся в код")

    def test_environment_is_read_in_one_place(self):
        """
        PERF-6.1. Клиент создаётся в model_warmup.py, а `stats` показывает
        таймаут из server.py. Пока оба читают одно окружение с одинаковыми
        умолчаниями, разницы нет; она появляется в день, когда поправят
        одно из двух, — и выглядит как «в stats написан один таймаут, а
        клиент живёт с другим». Такое расхождение не даёт ошибки и не
        видно в логах: ровно тот жанр, против которого весь заход.
        """
        code = "\n".join(
            line.split("#", 1)[0]
            for line in (HERE / "server.py").read_text(encoding="utf-8").splitlines())
        for var in ("EMBEDDING_MODEL", "BM25_MODEL",
                    "QDRANT_URL", "HELP_QDRANT_TIMEOUT_SEC"):
            self.assertNotIn(f'"{var}"', code,
                             f"{var} читается вторым экземпляром в server.py — "
                             f"его читает model_warmup.py, оттуда и импорт")

    def test_warmup_state_is_visible_in_stats(self):
        src = (HERE / "server.py").read_text(encoding="utf-8")
        self.assertIn("warmup", src.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
