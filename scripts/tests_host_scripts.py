"""
B-3. Хостовые скрипты не должны падать на кодировке консоли.

Что было. `FAIL-2`: набор упал с `UnicodeEncodeError` на знаке `⚠` —
консоль была cp1251, а такого символа в ней нет. Тогда починили сервер
справки и дочерние процессы `run_all_tests`, но сами скрипты, которые
запускаются на хосте, остались. Их вывод уходит в консоль напрямую, а
`$OutputEncoding` в PowerShell тут не помогает: он про то, чем консоль
ЧИТАЕТ вывод, а не чем Python его кодирует.

Воспроизводилось одной строкой:

    PYTHONIOENCODING=cp1251 python3 scripts/eval_all.py --summary-only
    UnicodeEncodeError: 'charmap' codec can't encode character '\\u26a0'

Сегодня от этого спасали две строки `$OutputEncoding`, которые набираются
руками в начале сессии. Перенаправление вывода в файл, запуск из CI или
забытые две строки — и скрипт падает. Особенно неудачно это у
`check_prereqs.py`: к нему приходят именно тогда, когда что-то не работает.

Запуск:  python3 tests_host_scripts.py
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Скрипты, которые запускает человек на хосте. Модули, живущие только в
# контейнерах, сюда не входят: там UTF-8 и проблемы нет.
HOST_SCRIPTS = ("check_prereqs.py", "eval_all.py", "run_all_tests.py",
                "probe_hbk.py")

# Символы вне cp1251, которыми проект украшает вывод.
DECORATIONS = "✓⚠✗─═↑↓"


class TestGuardIsPresent(unittest.TestCase):
    """
    Статическая проверка: у каждого хостового скрипта стоит защита потоков.

    Она первична по отношению к запуску: запустить в тесте можно не всякий
    скрипт (`run_all_tests.py` вызвал бы сам себя рекурсивно), а проверить
    исходник — любой.
    """

    def test_every_host_script_reconfigures_streams(self):
        for name in HOST_SCRIPTS:
            path = SCRIPTS / name
            self.assertTrue(path.exists(), f"нет {name}")
            src = path.read_text(encoding="utf-8")
            self.assertRegex(
                src, r"reconfigure\(errors=[\"']replace[\"']\)",
                f"{name}: нет защиты потоков — упадёт при cp1251-консоли",
            )

    def test_guard_runs_before_any_printing(self):
        """
        Защита обязана стоять раньше первой печати, иначе она бесполезна:
        падение случится до неё.
        """
        for name in HOST_SCRIPTS:
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            guard_at = src.find("reconfigure(errors=")
            self.assertGreater(guard_at, 0, name)

            # Ищем первую печать вне определений функций — то есть ту, что
            # исполняется при импорте модуля.
            first_print = None
            for m in re.finditer(r"^print\(", src, re.M):
                first_print = m.start()
                break
            if first_print is not None:
                self.assertLess(
                    guard_at, first_print,
                    f"{name}: печать раньше защиты потоков",
                )

    def test_decorations_are_actually_used(self):
        """
        Проверка на осмысленность самой проверки: если бы никто не печатал
        символов вне cp1251, весь этот набор был бы пустой формальностью.
        """
        used = [n for n in HOST_SCRIPTS
                if any(ch in (SCRIPTS / n).read_text(encoding="utf-8")
                       for ch in DECORATIONS)]
        self.assertTrue(
            used, "ни один скрипт не печатает символов вне cp1251 — "
                  "защита потоков перестала быть нужной, проверьте почему",
        )


class TestActuallySurvivesNarrowConsole(unittest.TestCase):
    """
    Живая проверка: запускаем скрипт с cp1251 и смотрим, что он не упал.

    Запускается только `eval_all.py --summary-only` — он читает каталог
    отчётов, ничего не меняет и отрабатывает мгновенно. `run_all_tests.py`
    здесь запускать нельзя: этот набор лежит в `scripts/` и попал бы в
    собственный прогон рекурсивно.
    """

    def test_eval_all_summary_under_cp1251(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1251"
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "eval_all.py"), "--summary-only"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=60,
        )
        output = proc.stdout + proc.stderr
        self.assertNotIn(
            "UnicodeEncodeError", output,
            "скрипт снова падает на кодировке консоли",
        )
        # Код возврата не проверяем: без отчётов в evals/reports скрипт
        # законно возвращает единицу. Нас интересует только то, что он
        # дошёл до конца, а не оборвался на печати.


class TestSuiteCounting(unittest.TestCase):
    """
    A-5, третье состояние: набор запустился и не сказал, сколько проверок
    отработало.

    Приёмка 16 августа: `evals/runner/tests.py` написан не на unittest, в
    его выводе нет строки «Ran N tests», и итог выглядел как

        OK   tests.py    0 тестов   2.1 с

    То есть пять настоящих проверок не попали ни в общее число, ни в чьё-то
    внимание. А набор, который сломался бы так, что выходит с нулём
    проверок и кодом 0, выглядел бы ровно так же — это тот самый жанр
    «отказ, притворившийся успехом», ради которого затевался весь заход.
    """

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import run_all_tests
        self.mod = run_all_tests

    def _run_fake(self, body: str):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tests_fake.py"
            path.write_text(body, encoding="utf-8")
            return self.mod.run_one(path)

    def test_unittest_output_is_counted(self):
        ok, n, _, _, _, _, how = self._run_fake(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self): pass\n"
            "    def test_b(self): pass\n"
            "unittest.main(verbosity=2)\n"
        )
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertEqual(how, "unittest")

    def test_progress_format_is_counted(self):
        """Формат `evals/runner/tests.py`: пять функций и печать прогресса."""
        ok, n, _, _, _, _, how = self._run_fake(
            'print("[1/5] predicates: OK")\n'
            'print("[5/5] run_one + report: OK")\n'
        )
        self.assertTrue(ok)
        self.assertEqual(n, 5, "прогресс-строки не посчитаны")
        self.assertEqual(how, "прогресс-строки")

    def test_silent_success_is_not_counted_as_tests(self):
        """
        Набор, который ничего не сказал, не должен добавлять к общему числу
        придуманных проверок. Ноль здесь — честный ответ «неизвестно», и
        именно он выносится отдельным числом в итоговую строку.
        """
        ok, n, _, _, _, _, how = self._run_fake('print("готово")\n')
        self.assertTrue(ok)
        self.assertEqual(n, 0)
        self.assertEqual(how, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
