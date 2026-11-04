"""
SKILL-1. Правила и скиллы агента — живые, а не декоративные.

Скилл или правило, называющие несуществующий инструмент, учат агента
звать то, чего нет: он получает ошибку и начинает угадывать. А инструмент,
которого не называет ни одно правило и ни один скилл, агент почти не
зовёт — так было с половиной инструментов графа до 4 ноября 2026
(анализ влияния, аудит, `metadata_refresh`).

Проверяется:
  • фронтматтер скиллов по формату Agent Skills (его читают и Cursor, и
    OpenCode): `name` = имя папки, `[a-z0-9]+(-[a-z0-9]+)*`, описание есть
    и не длиннее 1024 символов;
  • каждый инструмент, названный в правилах и скиллах, существует в коде
    серверов (у чужого v8std — в списке README);
  • каждый инструмент из README назван хотя бы в одном правиле или скилле
    (кроме служебных, которые зовёт watcher);
  • каждый скилл упомянут в README.

Запуск: python3 scripts/tests_agent_skills.py
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
SKILLS = ROOT / ".cursor" / "skills"
RULES = ROOT / ".cursor" / "rules"
SUITE = ROOT / "1c-mcp-suite"
README = ROOT / "README.md"

TOOL_RE = re.compile(r"`((?:metadata|code|bsl|platform_help|query|v8std)_[a-z_0-9]+)(?:`|\()")
# Похожие на инструмент имена, которые инструментами не являются: параметры.
NOT_TOOLS = {"query_text"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SERVICE_TOOLS = {"metadata_upsert_file", "metadata_remove_file"}


def readme_tools() -> set[str]:
    text = README.read_text(encoding="utf-8")
    return set(re.findall(r"Инструмент \d+ — `([a-z_0-9]+)`", text))


def code_tools() -> set[str]:
    names: set[str] = set()
    for f in SUITE.rglob("*.py"):
        if f.name.startswith("tests_"):
            continue
        names |= set(re.findall(r"def ([a-z_0-9]+)\(", f.read_text(encoding="utf-8", errors="replace")))
    return names


def agent_texts() -> dict[str, str]:
    out = {}
    for f in sorted(RULES.glob("*.mdc")) + sorted(SKILLS.glob("*/*.md")):
        out[str(f.relative_to(ROOT))] = f.read_text(encoding="utf-8")
    return out


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class TestSkillFormat(unittest.TestCase):

    def test_skills_exist(self):
        self.assertTrue(list(SKILLS.glob("*/SKILL.md")), "скиллов нет")

    def test_frontmatter(self):
        for f in sorted(SKILLS.glob("*/SKILL.md")):
            with self.subTest(skill=f.parent.name):
                fm = frontmatter(f.read_text(encoding="utf-8"))
                self.assertEqual(fm.get("name"), f.parent.name, "name ≠ имя папки")
                self.assertRegex(fm["name"], NAME_RE)
                self.assertTrue(fm.get("description"), "нет description")
                self.assertLessEqual(len(fm["description"]), 1024)

    def test_every_skill_is_in_readme(self):
        text = README.read_text(encoding="utf-8")
        for f in sorted(SKILLS.glob("*/SKILL.md")):
            self.assertIn(f".cursor/skills/{f.parent.name}/", text,
                          f"скилл {f.parent.name} не описан в README (раздел 6)")


class TestToolNames(unittest.TestCase):

    def test_named_tools_exist(self):
        known = code_tools() | readme_tools()
        bad = []
        for name, text in agent_texts().items():
            for tool in set(TOOL_RE.findall(text)) - NOT_TOOLS:
                if tool not in known:
                    bad.append(f"{name}: {tool}")
        self.assertEqual(bad, [], "в правилах/скиллах названы несуществующие инструменты")

    def test_every_tool_is_covered(self):
        named = set()
        for text in agent_texts().values():
            named |= set(TOOL_RE.findall(text))
        missing = sorted(readme_tools() - named - SERVICE_TOOLS)
        self.assertEqual(missing, [], "инструменты, о которых агенту не сказано ни в правилах, ни в скиллах")


if __name__ == "__main__":
    unittest.main(verbosity=2)
