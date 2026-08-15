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


if __name__ == "__main__":
    unittest.main(verbosity=2)
