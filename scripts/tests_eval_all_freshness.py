"""
FIX-25. Прибор не имеет права показывать вчерашнее как сегодняшнее.
====================================================================

Что было
────────
18 августа владелец запустил `python scripts\\eval_all.py` при выключенном
Docker Desktop. Все пять прогонов упали:

    failed to connect to the docker API at npipe:////./pipe/...

Сводка внизу написала:

    OK: все датасеты дали 100% hard, регрессов нет

Она прочла отчёты, лежавшие с 16–17 августа, и выдала их за результат
этого прогона. Код возврата каждого прогона собирался в словарь
`run_codes` — и не читался ни разу.

Почему это тяжелее остальных находок
────────────────────────────────────
`eval_all.py` — единственный прибор, которым проверяют, не сломалось ли
качество ответов. Прибор, показывающий «всё хорошо» при выключенном
стенде, хуже отсутствующего: отсутствующий заставляет пойти и проверить.

Отдельно стоит заметить, что цифры **сами себе противоречили**: в
`platform_help.jsonl` к тому моменту было 27 примеров, а сводка
показывала `20/20`. Признак был на экране, но сверять его было не с чем.

Три ветки, три разных «нельзя верить»
──────────────────────────────────────
1. прогон вернул ненулевой код — показания не берутся вовсе;
2. прогон вернул ноль, но отчёт не обновился — значит его не создали;
3. отчёт есть и свежий, но примеров в нём не столько, сколько в
   датасете, — он снят на другой его редакции.

Запуск:  python3 scripts/tests_eval_all_freshness.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import eval_all  # noqa: E402


GOOD = dict(rc=0, latest_name="ds_20260818_100000.json",
            prev_name="ds_20260817_100000.json",
            report_total=27, want_examples=27, summary_only=False)


class TestVerdict(unittest.TestCase):

    def test_fresh_report_is_trusted(self):
        problem, _ = eval_all.report_verdict(**GOOD)
        self.assertIsNone(problem)

    def test_failed_run_is_never_trusted(self):
        """
        Сердце FIX-25. Отчёт на диске лежит, он свежий и зелёный — и всё
        равно не годится: этот прогон не состоялся.
        """
        problem, note = eval_all.report_verdict(**{**GOOD, "rc": 1})
        self.assertEqual(problem, "not_run")
        self.assertIn("НЕ СОСТОЯЛСЯ", note)

    def test_report_that_did_not_change_is_stale(self):
        problem, note = eval_all.report_verdict(
            **{**GOOD, "latest_name": GOOD["prev_name"]})
        self.assertEqual(problem, "stale_file")
        self.assertIn("НЕ ОБНОВИЛСЯ", note)

    def test_same_file_is_fine_in_summary_only(self):
        """
        `--summary-only` не запускает ничего и существует ровно ради того,
        чтобы посмотреть последние показания. Требовать от него нового
        отчёта значило бы сделать режим бесполезным.
        """
        problem, _ = eval_all.report_verdict(
            **{**GOOD, "latest_name": GOOD["prev_name"], "summary_only": True})
        self.assertIsNone(problem)

    def test_report_from_another_edition_is_stale(self):
        """
        Та ветка, что поймала бы 18 августа раньше всех: 20 примеров в
        отчёте против 27 в датасете.
        """
        problem, note = eval_all.report_verdict(
            **{**GOOD, "report_total": 20})
        self.assertEqual(problem, "stale_edition")
        self.assertIn("20", note)
        self.assertIn("27", note)

    def test_missing_report_is_named_separately(self):
        problem, _ = eval_all.report_verdict(**{**GOOD, "latest_name": None})
        self.assertEqual(problem, "no_report")

    def test_first_run_ever_has_no_previous(self):
        problem, _ = eval_all.report_verdict(**{**GOOD, "prev_name": None})
        self.assertIsNone(problem)

    def test_unknown_size_does_not_block(self):
        """
        Если пересчитать примеры не удалось (0), сверка размера просто не
        проводится. Сторож не должен краснеть от того, что ему нечем
        мерить, — иначе его отключат.
        """
        problem, _ = eval_all.report_verdict(**{**GOOD, "want_examples": 0})
        self.assertIsNone(problem)


class TestExampleCounting(unittest.TestCase):
    """
    Считать примеры надо так же, как их считает раннер, иначе сверка
    размеров начнёт краснеть на ровном месте: в датасетах проекта живут
    комментарии `//` и пустые строки-разделители.
    """

    def _count(self, text: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            p.write_text(text, encoding="utf-8")
            return eval_all.count_examples(p)

    def test_comments_and_blanks_are_not_examples(self):
        self.assertEqual(self._count(
            '// заголовок\n\n{"id": "a"}\n\n// раздел\n{"id": "b"}\n'), 2)

    def test_missing_file_is_zero_not_an_exception(self):
        self.assertEqual(eval_all.count_examples(Path("/нет/такого.jsonl")), 0)

    def test_real_datasets_are_counted(self):
        """
        Проверка на живых файлах: если формат датасета изменится, сверка
        размеров начнёт врать молча.
        """
        for path in sorted((ROOT / "evals" / "datasets").glob("*.jsonl")):
            with self.subTest(dataset=path.name):
                self.assertGreater(
                    eval_all.count_examples(path), 0,
                    f"в {path.name} насчитано ноль примеров")


class TestReturnCodesAreActuallyRead(unittest.TestCase):
    """
    Сторож на форму записи. `run_codes` уже существовал и уже собирался —
    не хватало одной строки, которая его читает. Пропасть она может так
    же незаметно, как появилась.
    """

    def test_run_codes_is_consumed(self):
        src = (SCRIPTS / "eval_all.py").read_text(encoding="utf-8")
        self.assertIn("run_codes.get(", src,
                      "код возврата прогона снова никто не читает — "
                      "сводка будет показывать прошлые цифры при "
                      "неудавшемся прогоне (FIX-25)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
