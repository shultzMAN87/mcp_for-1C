"""
Тесты A-2: общий модуль сверки «вход против выхода».

Отдельный набор, без зависимостей — модуль должен собираться и проверяться
в обоих образах (Dockerfile.python и Dockerfile.embeddings).

Запуск:  python3 tests_shortfall.py
"""

import logging
import re
import unittest
from pathlib import Path

from shortfall import Tally, TallyBook, format_shortfall, warn_shortfall

ROOT = Path(__file__).resolve().parent


class _Capture(logging.Handler):
    """Собирает записи лога, чтобы проверять не факт вызова, а текст."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def texts(self, level=None) -> list[str]:
        return [
            r.getMessage()
            for r in self.records
            if level is None or r.levelno == level
        ]

    @property
    def warnings(self) -> list[str]:
        return self.texts(logging.WARNING)

    @property
    def infos(self) -> list[str]:
        return self.texts(logging.INFO)


def _logger(name: str) -> tuple[logging.Logger, _Capture]:
    log = logging.getLogger(name)
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    cap = _Capture()
    log.addHandler(cap)
    return log, cap


# ─── warn_shortfall: наследник graph_writer._warn_shortfall ──────────────


class TestWarnShortfall(unittest.TestCase):

    def test_no_warning_when_all_written(self):
        log, cap = _logger("t.ws.1")
        self.assertEqual(warn_shortfall("рёбра CALLS", 100, 100, log=log), 0)
        self.assertEqual(cap.warnings, [])

    def test_no_warning_when_written_more(self):
        # MERGE может вернуть больше обработанных строк, чем отправлено.
        log, cap = _logger("t.ws.2")
        self.assertEqual(warn_shortfall("узлы", 10, 12, log=log), 0)
        self.assertEqual(cap.warnings, [])

    def test_warns_with_numbers(self):
        log, cap = _logger("t.ws.3")
        self.assertEqual(warn_shortfall("узлы :Module", 14047, 14041, log=log), 6)
        self.assertEqual(len(cap.warnings), 1)
        text = cap.warnings[0]
        self.assertIn("14041", text)
        self.assertIn("14047", text)
        self.assertIn("6", text)

    def test_hint_appended(self):
        log, cap = _logger("t.ws.4")
        warn_shortfall("рёбра HAS_METHOD", 5, 1, log=log, hint="см. FIX-14")
        self.assertIn("см. FIX-14", cap.warnings[0])

    def test_format_is_separate_from_logging(self):
        text = format_shortfall("страницы", 128, 25, unit="страниц")
        self.assertIn("25", text)
        self.assertIn("128", text)
        self.assertIn("103", text)


# ─── Tally: арифметика ───────────────────────────────────────────────────


class TestTallyArithmetic(unittest.TestCase):

    def test_clean_run(self):
        t = Tally("шаг", unit="файл")
        for _ in range(10):
            t.see()
            t.keep()
        self.assertEqual(t.lost, 0)
        self.assertEqual(t.unexplained, 0)
        self.assertTrue(t.ok)
        self.assertEqual(t.problems(), [])

    def test_explained_drop_is_not_a_problem(self):
        t = Tally("чтение контейнера", unit="запись")
        t.see(128)
        t.keep(25)
        t.drop("картинка", n=100)
        t.drop("служебная запись", n=3)
        self.assertEqual(t.lost, 103)
        self.assertEqual(t.unexplained, 0)
        self.assertTrue(t.ok)

    def test_unexplained_gap_is_the_main_finding(self):
        # Ровно случай HBK-1: 128 записей на входе, 25 страниц на выходе,
        # и никто не сказал, куда делись остальные.
        t = Tally("чтение контейнера", unit="запись")
        t.see(128)
        t.keep(25)
        self.assertEqual(t.unexplained, 103)
        self.assertFalse(t.ok)
        self.assertTrue(any("без объяснения" in p for p in t.problems()))

    def test_negative_gap_reports_miscounting(self):
        t = Tally("шаг")
        t.see(10)
        t.keep(8)
        t.drop("причина", n=5)
        self.assertEqual(t.unexplained, -3)
        self.assertFalse(t.ok)
        self.assertTrue(any("не сходятся" in p for p in t.problems()))

    def test_zero_output_is_a_failure_not_a_result(self):
        t = Tally("разбор", unit="файл")
        t.see(40)
        t.drop("не разобрано", n=40)
        self.assertEqual(t.unexplained, 0)  # арифметика сошлась
        self.assertFalse(t.ok)              # но выход нулевой
        self.assertTrue(any("это отказ" in p for p in t.problems()))

    def test_empty_input_is_not_a_failure(self):
        # Пустой вход — законная ситуация (нет файлов такого вида).
        t = Tally("шаг")
        self.assertTrue(t.ok)
        self.assertEqual(t.problems(), [])

    def test_alarm_reason_surfaces_even_when_arithmetic_fits(self):
        t = Tally("разбор XML", unit="файл")
        t.see(100)
        t.keep(98)
        t.drop("битый XML", example="Catalogs/Плохой.xml", n=2, alarm=True)
        self.assertEqual(t.unexplained, 0)
        self.assertFalse(t.ok)
        self.assertTrue(any("законной не бывает" in p for p in t.problems()))

    def test_min_keep_ratio(self):
        t = Tally("шаг", min_keep_ratio=0.5)
        t.see(100)
        t.keep(30)
        t.drop("причина", n=70)
        self.assertFalse(t.ok)
        self.assertTrue(any("дошло" in p for p in t.problems()))

        good = Tally("шаг", min_keep_ratio=0.5)
        good.see(100)
        good.keep(70)
        good.drop("причина", n=30)
        self.assertTrue(good.ok)


# ─── Tally: вывод ────────────────────────────────────────────────────────


class TestTallyOutput(unittest.TestCase):

    def test_input_is_always_printed(self):
        # Смысл A-1: вход должен быть виден и тогда, когда всё хорошо.
        log, cap = _logger("t.out.1")
        t = Tally("разбор BSL", unit="файл", log=log)
        t.see(17296)
        t.keep(17296)
        self.assertTrue(t.report())
        joined = " ".join(cap.infos)
        self.assertIn("17296", joined)
        self.assertIn("вход", joined)
        self.assertEqual(cap.warnings, [])

    def test_examples_limited_and_shown(self):
        t = Tally("разбор", unit="файл")
        for i in range(10):
            t.drop("битый XML", example=f"file{i}.xml")
        line = t.reasons_line()
        self.assertIn("битый XML 10", line)
        self.assertIn("file0.xml", line)
        self.assertNotIn("file5.xml", line)

    def test_report_returns_false_and_warns(self):
        log, cap = _logger("t.out.2")
        t = Tally("шаг", log=log)
        t.see(100)
        t.keep(60)
        self.assertFalse(t.report())
        self.assertTrue(cap.warnings)

    def test_hint_only_on_problems(self):
        log, cap = _logger("t.out.3")
        ok = Tally("шаг", log=log)
        ok.see(5)
        ok.keep(5)
        ok.report(hint="подсказка")
        self.assertNotIn("подсказка", " ".join(cap.infos + cap.warnings))

        bad = Tally("шаг2", log=log)
        bad.see(5)
        bad.keep(1)
        bad.report(hint="подсказка")
        self.assertIn("подсказка", " ".join(cap.warnings))

    def test_to_dict(self):
        t = Tally("шаг")
        t.see(3)
        t.keep(2)
        t.drop("причина")
        d = t.to_dict()
        self.assertEqual(d["seen"], 3)
        self.assertEqual(d["kept"], 2)
        self.assertEqual(d["dropped"], {"причина": 1})
        self.assertTrue(d["ok"])


# ─── TallyBook ───────────────────────────────────────────────────────────


class TestTallyBook(unittest.TestCase):

    def test_empty_book_says_so(self):
        log, cap = _logger("t.book.1")
        book = TallyBook(log)
        self.assertEqual(book.report(), 0)
        self.assertIn("сверок не было", " ".join(cap.infos))

    def test_counts_bad_stages(self):
        log, cap = _logger("t.book.2")
        book = TallyBook(log)
        good = book.stage("хороший")
        good.see(10)
        good.keep(10)
        bad = book.stage("плохой")
        bad.see(10)
        bad.keep(4)
        self.assertEqual(book.report(), 1)
        self.assertIn("плохой", " ".join(cap.warnings))

    def test_all_stages_listed(self):
        log, cap = _logger("t.book.3")
        book = TallyBook(log)
        for name in ("шаг1", "шаг2", "шаг3"):
            t = book.stage(name)
            t.see(1)
            t.keep(1)
        book.report()
        joined = " ".join(cap.infos)
        for name in ("шаг1", "шаг2", "шаг3"):
            self.assertIn(name, joined)


# ─── Доставка модуля в образы ────────────────────────────────────────────


class TestModuleIsDelivered(unittest.TestCase):
    """
    B-6: список образов здесь больше не живёт.

    Было две проверки со своим перечнем Dockerfile — и точно такие же лежали
    в tests_refusal и tests_bsl_health, каждая со своим списком. Проверка
    «модуль X импортируется сервером Y, значит COPY обязан быть» сама стала
    тем списком, который надо помнить.

    Теперь карту «импортёр → образ» строит tests_delivery.py по исходникам,
    а здесь остаётся вопрос про конкретный модуль: меня-то довезли?
    """

    def test_delivered_everywhere_it_is_imported(self):
        from tests_delivery import assert_delivered
        assert_delivered(self, "shortfall.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
