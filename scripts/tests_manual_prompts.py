"""
EVAL-4. Чек-лист обязан остаться чек-листом.
=============================================

Зачем сторож на документ
────────────────────────
`evals/manual-prompts.md` уже существовал — и был прозой: «ожидается»,
«провал», рассуждения о том, каким должен быть ответ. Прозу нельзя
прогнать: два человека (и один человек в разные дни) прочтут её
по-разному, а спор о качестве текста ответа не имеет конца.

Переписан он в чек-лист ровно по одному признаку: **у каждого сценария
записано, какие инструменты агент обязан вызвать**. Список вызовов виден
в интерфейсе Cursor и спору не подлежит — либо вызвал, либо нет.

Зарастание здесь такое же, как у корня репозитория (`DOC-3`): каждый
следующий сценарий проще дописать словами, чем по форме, и через три
захода файл снова станет прозой. Поэтому форма проверяется.

Что НЕ проверяется: текст сценариев, их количество сверх минимума и
формулировки промптов. Это содержание, и оно должно меняться.

Запуск:  python3 scripts/tests_manual_prompts.py
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
DOC = ROOT / "evals" / "manual-prompts.md"

# Инструменты, которые сценарии вправе называть. Список берётся не из
# головы: серверов пять, и имя, которого нет ни у одного из них, в
# чек-листе означает опечатку либо инструмент, удалённый заходом назад, —
# и сценарий с таким именем не пройдёт никогда, а выглядеть будет живым.
TOOL_RE = re.compile(r"`([a-z][a-z0-9_]*_[a-z0-9_]+)`")

SERVER_PREFIXES = ("metadata_", "code_", "query_", "bsl_",
                   "platform_help_", "v8std_")


def scenarios(text: str) -> list[tuple[str, str]]:
    """Разделы вида `## 4. Кто вызывает процедуру` и их тело."""
    parts = re.split(r"^## (\d+)\.\s*(.+)$", text, flags=re.M)
    out = []
    for i in range(1, len(parts), 3):
        out.append((f"{parts[i]}. {parts[i + 1].strip()}", parts[i + 2]))
    return out


class TestChecklistShape(unittest.TestCase):

    def setUp(self):
        self.assertTrue(DOC.exists(), f"нет {DOC}")
        self.text = DOC.read_text(encoding="utf-8")
        self.items = scenarios(self.text)

    def test_enough_scenarios(self):
        """
        Десять — нижняя граница из PLAN-7. Меньше означает, что часть
        серверов не проверяется вовсе: именно так и было — `query-builder`
        не встречался в прошлой редакции ни разу, при том что
        «`query_fields` до написания запроса» и есть главный вопрос.
        """
        self.assertGreaterEqual(
            len(self.items), 10,
            f"сценариев {len(self.items)}, а нужно 10–15",
        )

    def test_every_scenario_names_required_calls(self):
        bad = [name for name, body in self.items
               if "Обязано быть" not in body]
        self.assertFalse(
            bad,
            "у сценариев нет строки «Обязано быть вызвано» — значит "
            "критерий снова про текст ответа, а не про список вызовов "
            f"(EVAL-4): {bad}",
        )

    def test_every_scenario_names_a_failure(self):
        bad = [name for name, body in self.items
               if "Не должно быть" not in body]
        self.assertFalse(
            bad, f"у сценариев не сказано, что считается провалом: {bad}")

    def test_every_scenario_has_a_prompt(self):
        bad = [name for name, body in self.items if "```" not in body]
        self.assertFalse(
            bad, f"у сценариев нет промпта в блоке кода: {bad}")

    def test_prompt_does_not_name_the_tool(self):
        """
        Промпт, называющий инструмент, проверяет послушность, а не
        догадается ли агент сам. Блоки кода в сценарии — это то, что
        человек вставляет в чат; имён инструментов там быть не должно.
        """
        offenders = []
        for name, body in self.items:
            for block in re.findall(r"```(?:\w+)?\n(.*?)```", body, flags=re.S):
                if block.strip().startswith("docker "):
                    continue          # шаг стенда, не промпт
                for tool in TOOL_RE.findall("`" + block.replace("\n", "` `") + "`"):
                    if tool.startswith(SERVER_PREFIXES):
                        offenders.append(f"{name}: в промпте назван {tool}")
        self.assertFalse(offenders, "; ".join(offenders))


class TestAllServersAreCovered(unittest.TestCase):
    """
    Пять серверов — пять источников ошибок. Перекос проверялся руками и
    оказался сильным: из двенадцати сценариев прошлой редакции семь были
    про `v8std`, один про метаданные и ни одного про запросы.
    """

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_each_server_appears(self):
        for prefix, human in (
            ("metadata_", "metadata"),
            ("code_", "metadata, слой кода"),
            ("query_", "query-builder"),
            ("bsl_", "bsl-checker"),
            ("platform_help_", "platform-help"),
            ("v8std_", "v8std"),
        ):
            self.assertRegex(
                self.text, rf"`{prefix}\w+`",
                f"ни один сценарий не проверяет {human} — сервер есть, "
                f"мерила нет",
            )


class TestResultGoesSomewhere(unittest.TestCase):
    """
    Прогон без места для результата — разовое развлечение. Смысл в
    сравнении со следующим заходом, а сравнивать можно только записанное.
    """

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_status_is_the_destination(self):
        self.assertIn("СТАТУС.md", self.text,
                      "не сказано, куда девать результат прогона")

    def test_result_is_not_a_percentage(self):
        self.assertIn("из 15", self.text,
                      "итог должен называть числом сценариев и списком "
                      "непрошедших, а не процентом")

    def test_blank_form_exists(self):
        self.assertIn("| № |", self.text, "нет бланка для отметок")


if __name__ == "__main__":
    unittest.main(verbosity=2)
