"""
HYG-2. Тест на расхождение `gen_lockfiles.sh` и `gen_lockfiles.ps1`.

Как это выглядело. Пара `v8std` была в bash-версии и отсутствовала в
PowerShell-версии: на Windows генерировались три лок-файла вместо четырёх,
скрипт при этом отрабатывал успешно и ничего не говорил. Задача `LOCK-1`
висела в плане неделю с диагнозом «не хватило времени», а на деле не
хватало одной строки в файле, на который никто не смотрел.

Это `A-2` в другой одежде: два списка, которые обязаны совпадать, и
никто их не сверяет. Разошлись молча — разойдутся снова, потому что
править приходится оба, а помнить об этом должен человек.

Проверяем ровно одно: множества пар (источник → лок) в обоих скриптах
одинаковы. Версия базового образа намеренно не сверяется — она задаётся
по-разному и совпадать не обязана.

Запуск:  python3 scripts/tests_lockfile_pairs.py
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "gen_lockfiles.sh"
PS1 = ROOT / "scripts" / "gen_lockfiles.ps1"
SUITE = ROOT / "1c-mcp-suite"


def pairs_from_sh(text: str) -> set[tuple[str, str]]:
    """
    Строки вида "requirements.txt:requirements.lock.txt:python:3.12-slim"
    внутри массива. Берём только первые два поля.
    """
    out = set()
    for m in re.finditer(r'"(requirements[^":]*\.txt):(requirements[^":]*\.lock\.txt):', text):
        out.add((m.group(1), m.group(2)))
    return out


def pairs_from_ps1(text: str) -> set[tuple[str, str]]:
    """
    Блоки вида:
        src   = "requirements.txt"
        dst   = "requirements.lock.txt"
    Порядок полей внутри блока фиксирован — сопоставляем последовательно.
    """
    out = set()
    src = None
    for m in re.finditer(r'(src|dst)\s*=\s*"([^"]+)"', text):
        key, value = m.group(1), m.group(2)
        if key == "src":
            src = value
        elif src is not None:
            out.add((src, value))
            src = None
    return out


class TestLockfileGeneratorsAgree(unittest.TestCase):

    def setUp(self):
        for path in (SH, PS1):
            if not path.exists():
                self.skipTest(f"нет {path.name}")
        self.sh = pairs_from_sh(SH.read_text(encoding="utf-8"))
        self.ps1 = pairs_from_ps1(PS1.read_text(encoding="utf-8"))

    def test_parsers_found_something(self):
        """Пустой разбор означал бы, что тест зелёный по недоразумению."""
        self.assertTrue(self.sh, "в gen_lockfiles.sh не разобрана ни одна пара")
        self.assertTrue(self.ps1, "в gen_lockfiles.ps1 не разобрана ни одна пара")

    def test_same_pairs(self):
        only_sh = self.sh - self.ps1
        only_ps1 = self.ps1 - self.sh
        self.assertFalse(
            only_sh,
            f"есть в gen_lockfiles.sh и нет в .ps1: {sorted(only_sh)} — "
            f"на Windows эти лок-файлы не соберутся, и скрипт об этом не скажет",
        )
        self.assertFalse(
            only_ps1,
            f"есть в gen_lockfiles.ps1 и нет в .sh: {sorted(only_ps1)}",
        )

    def test_sources_exist(self):
        """
        Каждая пара ссылается на существующий requirements-файл. Пара,
        указывающая в пустоту, тоже отрабатывает молча.
        """
        for src, _dst in sorted(self.sh):
            self.assertTrue((SUITE / src).exists(),
                            f"{src} упомянут в генераторе, но файла нет")

    def test_all_requirements_covered(self):
        """
        Обратная сторона: requirements-файл, который есть в проекте, но не
        упомянут ни в одном генераторе. Ровно так живут незалоченные
        зависимости — пересборка образа тихо берёт другие версии.
        """
        declared = {src for src, _ in self.sh}
        on_disk = {
            p.name for p in SUITE.glob("requirements*.txt")
            if ".lock." not in p.name
        }
        missing = on_disk - declared
        self.assertFalse(
            missing,
            f"не попали в генераторы лок-файлов: {sorted(missing)} — "
            f"образ, который их ставит, собирается по границам версий",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
