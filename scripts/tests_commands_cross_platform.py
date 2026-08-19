"""
DOC-6 / HYG-5. Совет, ведущий в несуществующую команду.
========================================================

Что было
────────
`check_publish.py`, `Dockerfile.embeddings` и `requirements-embeddings.txt`
советовали `make lock`. На основной рабочей машине проекта — Windows,
PowerShell — команды `make` нет вовсе; работает `.\\scripts\\gen_lockfiles.ps1`.

Предупреждение, ведущее в несуществующую команду, — половина
предупреждения: человек видит, что что-то не так, и не может это
исправить там, где находится.

Это `LOCK-1` в третьей одежде. Там пара пакетов была в `gen_lockfiles.sh`
и отсутствовала в `.ps1` — два пути, а знали про один. Здесь два пути
запуска, а документация знает один.

Что проверяется
───────────────
Каждое **сообщение, адресованное человеку**, где сказано `make <цель>`,
обязано рядом называть и вариант без make.

Правило не выдумано отдельно от Makefile: оно из него и выводится. Цель
`lock` запускает `scripts/gen_lockfiles.sh`, значит windows-вариант —
одноимённый `.ps1`; цель `test` запускает `scripts/run_all_tests.py`,
значит достаточно назвать сам скрипт. Список соответствий, который ведут
руками, разошёлся бы с Makefile ровно так же, как разошлись пять
предыдущих списков в проекте.

Чего проверка НЕ трогает
────────────────────────
`PLAN-*.md` и `docs/archive/`. Планы говорят о командах, а не советуют их
(«`README.md` советует `make lock`» — это описание дефекта, и требовать
рядом PowerShell-строку значило бы требовать переписать формулировку
задачи). Архив — застывшая история: он описывает, как было, и правка в
нём стирает свидетельство.

Запуск:  python3 scripts/tests_commands_cross_platform.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "1c-mcp-suite"

MAKE_CALL = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)\b")

# Сколько строк вокруг считается «тем же сообщением». Три — потому что
# столько занимает связка «проблема / команда Linux / команда Windows»;
# больше означало бы, что PowerShell-строку можно спрятать в соседнем
# абзаце и проверка её зачтёт.
WINDOW = 3


def makefile_targets() -> dict[str, list[str]]:
    """
    Цель → строки её рецепта. Разбор простой намеренно: если Makefile
    станет сложнее разбора, это будет видно по красному тесту, а не по
    молча пропущенным целям.
    """
    targets: dict[str, list[str]] = {}
    current: str | None = None
    for raw in (ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            if current:
                targets[current].append(raw.strip())
            continue
        m = re.match(r"^([a-zA-Z][\w-]*)\s*:(?!=)", raw)
        current = m.group(1) if m else None
        if current:
            targets.setdefault(current, [])
    return targets


def alternatives_for(target: str, recipes: list[str]) -> list[str]:
    """
    Чем эту цель можно запустить без make. Пусто — значит проверять
    нечего (цель вроде `help`, состоящая из одних `@echo`).
    """
    out: list[str] = []
    for line in recipes:
        line = line.lstrip("@-")
        if line.startswith("echo "):
            continue
        m = re.search(r"scripts/([\w.-]+)\.sh", line)
        if m and (ROOT / "scripts" / f"{m.group(1)}.ps1").exists():
            out.append(f"{m.group(1)}.ps1")
            continue
        m = re.search(r"scripts/([\w.-]+\.py)", line)
        if m:
            out.append(m.group(1))
            continue
        if line.startswith("docker compose"):
            out.append("docker compose")
    return out


def files_under_check() -> list[Path]:
    files: list[Path] = []
    files += [p for p in ROOT.glob("*.md") if not p.name.startswith("PLAN")]
    files += sorted(ROOT.glob("scripts/*.py"))
    files += sorted(SUITE.glob("Dockerfile*"))
    files += sorted(SUITE.glob("requirements*.txt"))
    files.append(ROOT / "evals" / "datasets" / "README.md")
    return [p for p in files if p.exists() and p.name != Path(__file__).name]


class TestMakeAdviceNamesTheOtherWay(unittest.TestCase):

    def setUp(self):
        self.targets = makefile_targets()
        self.assertIn("lock", self.targets, "Makefile разобрался неправильно")

    def test_every_make_advice_has_a_non_make_twin(self):
        offenders: list[str] = []
        for path in files_under_check():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for idx, line in enumerate(lines):
                for target in MAKE_CALL.findall(line):
                    alts = alternatives_for(target, self.targets.get(target, []))
                    if not alts:
                        continue
                    lo = max(0, idx - WINDOW)
                    window = "\n".join(lines[lo:idx + WINDOW + 1])
                    if not any(a in window for a in alts):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{idx + 1}: советует "
                            f"«make {target}», рядом нет варианта без make "
                            f"({' | '.join(alts)})"
                        )
        self.assertFalse(
            offenders,
            "DOC-6: на Windows команды make нет. Сообщение, называющее "
            "цель make, обязано называть и способ запустить то же самое "
            "напрямую:\n  " + "\n  ".join(offenders),
        )

    def test_the_check_can_actually_fail(self):
        """
        Сторож на сторожа. Правило, которое не краснеет ни на чём, —
        украшение; проверяем на заведомо плохом тексте.
        """
        lines = ["Если лок пуст, запустите: make lock"]
        alts = alternatives_for("lock", self.targets["lock"])
        self.assertTrue(alts, "у цели lock не нашлось варианта без make")
        self.assertFalse(any(a in lines[0] for a in alts))


class TestWindowsInstructionsUseTheRightPython(unittest.TestCase):
    """
    `FIX-24`, вторая находка того же прогона.

    В `PowerShell` команды `python3` нет: её перехватывает заглушка
    Microsoft Store. Заглушка печатает слово «Python», ничего не
    запускает и **возвращает нулевой код возврата** — то есть команда
    выглядит выполнившейся, а не сломанной.

    Ровно так и вышло 17 августа: инструкция, написанная заходом ранее,
    советовала `python3 scripts\\run_all_tests.py`, и прогон «прошёл»
    мгновенно и без единого теста.

    Это `DOC-6` наизнанку. Там команду называли только для одной системы;
    здесь назвали для обеих, но команду второй системы взяли из первой.

    Windows-строка узнаётся по обратным слэшам в пути или по блоку
    ```powershell — то есть по тому же признаку, по которому её читает
    человек.
    """

    WINDOWS_HINT = re.compile(r"(scripts\\|\.ps1\b|\.\\)")

    def test_no_python3_in_windows_lines(self):
        offenders = []
        for path in files_under_check():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            in_ps_block = False
            for idx, line in enumerate(lines, start=1):
                if line.strip().startswith("```"):
                    in_ps_block = line.strip().lower().startswith("```powershell")
                    continue
                windows = in_ps_block or self.WINDOWS_HINT.search(line)
                if windows and re.search(r"\bpython3\b", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{idx}: {line.strip()}")
        self.assertFalse(
            offenders,
            "в PowerShell `python3` перехватывает заглушка Microsoft Store: "
            "печатает «Python», ничего не запускает и возвращает 0 — "
            "команда выглядит выполнившейся. Нужен `python` или `py -3`:\n  "
            + "\n  ".join(offenders),
        )


class TestLineEndingsArePinned(unittest.TestCase):
    """
    HYG-5. `git add` сыпал полусотней предупреждений
    «LF will be replaced by CRLF». Сегодня безобидно: в репозитории лежит
    LF. Но на машине с другой настройкой `core.autocrlf` diff станет
    шумным, а `.sh`-скрипты в рабочей копии получат CRLF и перестанут
    запускаться в контейнере — `/bin/sh^M: bad interpreter`.
    """

    def setUp(self):
        self.path = ROOT / ".gitattributes"
        self.assertTrue(self.path.exists(), "нет .gitattributes (HYG-5)")
        self.text = self.path.read_text(encoding="utf-8")

    def test_text_files_are_lf(self):
        self.assertIn("* text=auto eol=lf", self.text)

    def test_binaries_are_declared(self):
        """
        Справка и jar-ник анализатора не должны подвергаться нормализации
        концов строк ни при каких настройках клиента.
        """
        for ext in (".hbk", ".jar"):
            self.assertRegex(
                self.text, rf"\*\{ext}\s+binary",
                f"{ext} не объявлен бинарным — git попробует чинить в нём "
                f"переводы строк",
            )

    def test_shell_scripts_stay_lf(self):
        """
        `.sh` уезжают в контейнер; CRLF в них ломает запуск молча и
        неочевидно, поэтому им своя явная строка, а не общее правило.
        """
        self.assertRegex(self.text, r"\*\.sh\s+text\s+eol=lf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
