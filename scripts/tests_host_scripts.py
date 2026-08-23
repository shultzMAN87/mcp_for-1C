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
#
# Список не ведётся руками — это был четвёртый такой список в проекте
# (после образов в трёх наборах, которые убрал B-6). Каждый новый скрипт
# в scripts/ попадает под проверку по факту появления файла; забыть
# дописать его сюда больше нельзя, потому что дописывать некуда.
HOST_SCRIPTS = tuple(sorted(
    p.name for p in Path(__file__).resolve().parent.glob("*.py")
    if not p.name.startswith("tests_")
))

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
        ok, n, _, _, _, _, how, _proved = self._run_fake(
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
        ok, n, _, _, _, _, how, _proved = self._run_fake(
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
        ok, n, _, _, _, _, how, _proved = self._run_fake('print("готово")\n')
        self.assertTrue(ok)
        self.assertEqual(n, 0)
        self.assertEqual(how, "")


class TestFullySkippedSuiteIsNotSuccess(unittest.TestCase):
    """
    CI-5. Четвёртое состояние набора: запустился и не проверил ничего.

    `A-5` различил «прошло», «упало» и «не запускалось». Приёмка `PERF-12`
    показала четвёртое: `tests_stats_batch.py` печатал

        OK    tests_stats_batch.py     28 тестов    0.1 с (28 пропущено)

    и читался как норма, пока внутри него три теста лежали сломанными.
    Пропуск был поштучным (нет пакета `mcp`), а сумма получилась полной —
    то есть набор превратился в тишину, а тишина выглядит согласием.

    Проверяем ИСХОД ПРОГОНА, а не арифметику: правило живёт в `main`, и
    тест на `is_fully_skipped` был бы зелёным при любом поведении ключа
    `--strict`. Для этого у `main` есть шов — список наборов можно
    передать аргументом.
    """

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import run_all_tests
        self.mod = run_all_tests

    def _run_main(self, body: str, argv: list[str]) -> tuple[int, str]:
        import io
        import contextlib
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tests_twin.py"
            path.write_text(body, encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.mod.main(argv, suites=[path])
            return rc, buf.getvalue()

    ALL_SKIPPED = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    @unittest.skip('нет пакета/стенда')\n"
        "    def test_a(self): pass\n"
        "    @unittest.skip('нет пакета/стенда')\n"
        "    def test_b(self): pass\n"
        "unittest.main(verbosity=2)\n"
    )

    HALF_SKIPPED = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    @unittest.skip('нужен Neo4j')\n"
        "    def test_a(self): pass\n"
        "    def test_b(self): pass\n"
        "unittest.main(verbosity=2)\n"
    )

    def test_strict_fails_on_a_suite_that_skipped_everything(self):
        rc, out = self._run_main(self.ALL_SKIPPED, ["--strict"])
        self.assertEqual(rc, 1, out)
        self.assertIn("не проверили ничего", out)

    def test_normal_run_still_passes(self):
        """
        Обычный прогон не роняем намеренно: пропуск по отсутствию стенда —
        повседневность, и красный прогон на машине без Neo4j отучил бы
        запускать тесты вовсе. Строгость — там, где её просят.
        """
        rc, out = self._run_main(self.ALL_SKIPPED, [])
        self.assertEqual(rc, 0, out)
        # Но молчать об этом нельзя даже в обычном прогоне.
        self.assertIn("ПУСТО", out)
        self.assertIn("НИЧЕГО НЕ ПРОВЕРИЛИ: 1", out)

    def test_partially_skipped_suite_is_not_touched(self):
        """
        Граница проходит по 100 %. `tests_graph_writer` пропускает четыре
        теста из семи без Neo4j — и три оставшихся проверяют настоящее.
        """
        rc, out = self._run_main(self.HALF_SKIPPED, ["--strict"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ПУСТО", out)

    def test_rule_itself(self):
        # tests_stats_batch: 28 тестов, все пропущены декоратором.
        self.assertTrue(self.mod.is_fully_skipped(28, 28, proved=0))
        # tests_graph_writer без Neo4j: три теста дошли до вердикта, ещё
        # четыре пропущены на уровне класса и в «Ran N» не вошли. Первая
        # редакция правила краснела именно здесь.
        self.assertFalse(self.mod.is_fully_skipped(3, 4, proved=3))
        # Ноль тестов — другой диагноз («счётчик не распознан»), у него
        # свой блок и своя ветка --strict.
        self.assertFalse(self.mod.is_fully_skipped(0, 0, proved=0))

    def test_verdicts_are_counted_from_verbose_output(self):
        """
        Третье число берётся из строк вердиктов, а не из арифметики: ровно
        поэтому пропуск на уровне класса перестал выглядеть провалом.
        """
        text = ("test_a (T.test_a) ... ok\n"
                "test_b (T.test_b) ... skipped 'нет Neo4j'\n"
                "test_c (T.test_c) ... FAIL\n")
        self.assertEqual(self.mod.verdicts_in(text), 2)
        self.assertEqual(self.mod.verdicts_in("... skipped 'нет'\n"), 0)

    def test_class_level_skips_do_not_look_like_an_empty_suite(self):
        """
        Живой двойник `tests_graph_writer`: класс целиком пропущен через
        setUpClass, а рядом есть работающий тест. Такой набор проверяет
        настоящее, и ронять на нём `--strict` нельзя.
        """
        rc, out = self._run_main(
            "import unittest\n"
            "class Skipped(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls): raise unittest.SkipTest('нет Neo4j')\n"
            "    def test_x(self): pass\n"
            "    def test_y(self): pass\n"
            "class Real(unittest.TestCase):\n"
            "    def test_z(self): pass\n"
            "unittest.main(verbosity=2)\n",
            ["--strict"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ПУСТО", out)


class TestSubprocessOutputIsDecodedSafely(unittest.TestCase):
    """
    `B-3`, третья встреча — и с обратной стороны трубы.

    Первые две правки чинили СВОЙ вывод: `_say()` в сервере справки и
    `PYTHONIOENCODING` дочерним процессам в `run_all_tests.py`. Этот набор
    проверяет ровно их — что каждый хостовый скрипт настраивает свои
    потоки.

    А `archive_docs.py` сломался на ЧТЕНИИ чужого:

        subprocess.run([...], capture_output=True, text=True)

    Без явной кодировки `text=True` декодирует вывод тем, что вернёт
    `locale.getpreferredencoding()` — на русской Windows это cp1251. Git на
    отказе печатает имя файла, а имена документов здесь кириллические.
    Поток-читатель падает с `UnicodeDecodeError` **в отдельном потоке**,
    `run()` этого не замечает и возвращает `stderr = None`, дальше
    `.strip()` на `None` — и скрипт умирает посреди переноса, оставив
    половину файлов в корне.

    Чинить это по одному месту бессмысленно: болезнь не в скрипте, а в
    привычке писать `text=True`. Поэтому проверяется форма записи во всех
    хостовых скриптах разом.
    """

    CALL_RE = re.compile(r"subprocess\.(run|Popen|check_output)\s*\(",
                         re.MULTILINE)

    @staticmethod
    def _strip_docstrings(text: str) -> str:
        """
        Разборы дефектов в этом проекте цитируют плохой код целиком —
        иначе объяснение не читается. Проверка формы записи обязана
        отличать цитату от вызова, иначе она ловит собственную
        документацию и учит писать про дефекты обтекаемо.
        """
        out = []
        parts = re.split(r'("""|\'\'\')', text)
        inside = False
        for part in parts:
            if part in ('"""', "\'\'\'"):
                inside = not inside
                out.append(part)
            else:
                out.append(" " * len(part) if inside else part)
        return "".join(out)

    def _calls(self, text: str):
        """(смещение, текст вызова) для каждого обращения к subprocess."""
        text = self._strip_docstrings(text)
        for m in self.CALL_RE.finditer(text):
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            yield m.start(), text[m.start():i + 1]

    def test_no_text_true_without_encoding(self):
        offenders = []
        for name in HOST_SCRIPTS:
            path = SCRIPTS / name
            text = path.read_text(encoding="utf-8")
            for pos, call in self._calls(text):
                asks_str = "text=True" in call or "universal_newlines=True" in call
                if not asks_str:
                    continue
                if "encoding=" in call:
                    continue
                line = text[:pos].count("\n") + 1
                offenders.append(f"{name}:{line}")
        self.assertFalse(
            offenders,
            "вывод дочернего процесса декодируется кодировкой консоли:\n  "
            + "\n  ".join(offenders) +
            "\n\ntext=True без encoding= берёт cp1251 на русской Windows и "
            "падает на кириллице в чужом сообщении об ошибке — причём в "
            "отдельном потоке, так что run() вернёт stderr=None. "
            "Пишите: encoding=\"utf-8\", errors=\"replace\".",
        )

    def test_decoding_never_kills_the_operation(self):
        """
        `errors="replace"` обязателен, а не желателен.

        Диагностика печатается ради операции, а не наоборот. Строгий режим
        превращает нечитаемое сообщение в отказ всей команды — здесь это
        стоило половины переноса.
        """
        offenders = []
        for name in HOST_SCRIPTS:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for pos, call in self._calls(text):
                if "encoding=" not in call:
                    continue
                if "errors=" in call:
                    continue
                offenders.append(f"{name}:{text[:pos].count(chr(10)) + 1}")
        self.assertFalse(
            offenders,
            "encoding= задан, errors= нет:\n  " + "\n  ".join(offenders) +
            "\nНечитаемый байт в чужом выводе уронит операцию целиком.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
