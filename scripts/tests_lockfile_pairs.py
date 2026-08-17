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


class TestCiPinsSameMcpVersion(unittest.TestCase):
    """
    CI-2. Шестое место, где записана версия одной и той же библиотеки.

    Workflow ставит `mcp[cli]` руками — образов у него нет, а
    `evals/runner/tests.py` без пакета не запускается. Значит, появилась
    ещё одна строка с версией, и появилась она вне лок-файлов.

    Чем это грозит конкретно: SDK переименовал `streamablehttp_client`
    между версиями. Разъедься эта строка с лок-файлом — CI начнёт краснеть
    на импорте раннера, то есть на чужом релизе, а не на нашей правке. Это
    ровно тот жанр, из-за которого CI перестают читать.

    Проверяем то же, что `LOCK-1`: два места, описывающих одно, обязаны
    совпадать, и сверять их должен не человек.
    """

    WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
    LOCK = SUITE / "requirements.lock.txt"

    def setUp(self):
        if not self.WORKFLOW.exists():
            self.skipTest("workflow не заведён — CI-2 ещё не сделан")

    def test_workflow_pin_matches_lockfile(self):
        wf = self.WORKFLOW.read_text(encoding="utf-8")
        pinned = re.findall(r"mcp\[cli\]==([\d.]+)", wf)
        self.assertTrue(
            pinned, "в workflow нет закреплённой версии mcp — прогон "
                    "возьмёт свежую и однажды сломается не по делу")

        lock = self.LOCK.read_text(encoding="utf-8")
        in_lock = re.findall(r"^mcp==([\d.]+)", lock, re.M)
        self.assertTrue(in_lock, f"{self.LOCK.name}: строки mcp== нет")

        self.assertEqual(
            set(pinned), set(in_lock),
            f"workflow ставит mcp {pinned}, а образы собираются с "
            f"{in_lock}. Прогон в CI обязан идти на той же версии SDK, "
            f"что и стенд — иначе он проверяет не то, что поедет.",
        )


class TestGeneratorsUseTheSameFlags(unittest.TestCase):
    """
    PERF-11. Одинаковые пары — половина дела; флаги тоже обязаны совпадать.

    `--emit-index-url` переносит в лок-файл строку `--extra-index-url`,
    которой `requirements-embeddings.txt` указывает индекс CPU-сборок
    torch. Расходись этот флаг между `.sh` и `.ps1` — лок, собранный на
    Windows, потерял бы адрес индекса, и образ снова притащил бы ~2,5 ГБ
    колёс `nvidia-*` из PyPI.

    Отличить такой лок от правильного на глаз нельзя: версии в нём те же,
    нет только одной строки в заголовке. Это `LOCK-1` во второй одежде.
    """

    # Только настоящие вызовы: в обоих файлах слово pip-compile встречается
    # и в комментариях, и в сообщении «pip-compile не найден».
    FLAG_RE = re.compile(r"pip-compile --quiet[^\n\"']*")

    def flags(self, path: Path) -> list[set[str]]:
        text = path.read_text(encoding="utf-8")
        out = []
        for m in self.FLAG_RE.finditer(text):
            out.append({w for w in m.group(0).split() if w.startswith("--")}
                       - {"--output-file"})
        return out

    def test_both_generators_pass_the_same_flags(self):
        sh_sets, ps_sets = self.flags(SH), self.flags(PS1)
        self.assertTrue(sh_sets and ps_sets, "вызовов pip-compile не нашлось")
        # В bash-версии два вызова (--local и docker), в PowerShell один.
        # Набор флагов у всех обязан быть одинаковым.
        all_sets = sh_sets + ps_sets
        first = all_sets[0]
        for other in all_sets[1:]:
            self.assertEqual(
                first, other,
                f"флаги pip-compile разошлись: {sorted(first)} vs "
                f"{sorted(other)}. Лок, собранный разными скриптами, "
                f"описывал бы разные образы",
            )

    def test_emit_index_url_is_on(self):
        """
        Без него `--extra-index-url` из исходного requirements не попадёт в
        лок, и правка `PERF-11` окажется написанной, но не работающей —
        худший из исходов, потому что выглядит сделанной.
        """
        for path in (SH, PS1):
            self.assertIn("--emit-index-url", path.read_text(encoding="utf-8"),
                          f"{path.name}: нет --emit-index-url (PERF-11)")


class TestCpuTorchIsDeclared(unittest.TestCase):
    """
    PERF-11, исходная сторона. Сам лок здесь не проверяется: пересобрать его
    может только машина с сетью и docker (`make lock`), а до пересборки
    проверка была бы красной по причине, которую сегодняшний коммит не
    исправляет. Про непересобранный лок предупреждает `check_publish.py` —
    предупреждением, а не отказом.
    """

    SRC = SUITE / "requirements-embeddings.txt"

    def test_cpu_index_is_declared(self):
        text = self.SRC.read_text(encoding="utf-8")
        self.assertIn("download.pytorch.org/whl/cpu", text,
                      "не объявлен индекс CPU-сборок torch — образ справки "
                      "тянет ~2,5 ГБ мёртвых колёс nvidia-*")

    def test_declaration_explains_itself(self):
        """
        Строка `--extra-index-url` без объяснения выглядит как случайность,
        и первый же, кто будет чистить файл, её уберёт.
        """
        text = self.SRC.read_text(encoding="utf-8")
        self.assertIn("PERF-11", text)
        self.assertIn("make lock", text,
                      "не сказано главного: правка этого файла без "
                      "пересборки лока не меняет ничего")


if __name__ == "__main__":
    unittest.main(verbosity=2)
