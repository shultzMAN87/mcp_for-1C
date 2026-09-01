#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-33. Классификатор утверждений и карточка страницы. Плюс R15.

Что делает
──────────
Берёт разобранную страницу, режет на утверждения и раскладывает каждое по
четырём категориям из PLAN-DOCS 6.3:

  подтверждено   объекты, упомянутые в утверждении, есть в конфигурации;
  противоречит   объект есть, но заявленная связь графом не подтверждается;
  не существует  имя похоже на объект метаданных, но такого в графе нет;
  не проверяемо  замысел, бизнес-правило, история. САМОЕ ЦЕННОЕ.

Чего он НЕ делает, и это принципиально
───────────────────────────────────────
Не решает, кто прав. Расхождение «в статье А, в конфигурации Б» имеет три
причины: статья устарела; статья описывает целевое состояние; в коде дефект.
Различить их машина не может, а «приведение документации в соответствие с
кодом» в третьем случае документирует баг как норму (6.4, R17). Поэтому в
карточке расхождение сформулировано нейтрально, без вывода.

Не удаляет ничего. Может пометить, переместить, понизить в статусе. Удаляет
только человек (правило блока H).

Про категорию «не проверяемо»
──────────────────────────────
Это ровно то, ради чего документация существует: замысел, которого нет ни в
коде, ни в графе. Агент, оставляющий подтверждённое и убирающий
неподтверждённое, делает статью аккуратнее и беднее, и заметить это почти
невозможно (R15). Поэтому здесь есть машинная проверка `--check-draft`: все
фрагменты «не проверяемо» обязаны присутствовать в черновике ДОСЛОВНО, иначе
черновик отклоняется. Проверка не зависит от послушности модели.

Запуск:
    python scripts\\docs_classify.py --card techdocs\\_imported\\3948112.json
    python scripts\\docs_classify.py --check-draft черновик.md --card 3948112.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NAMES = REPO_ROOT / "techdocs" / "names.json"

CONFIRMED = "подтверждено"
CONTRADICTS = "противоречит"
MISSING = "не существует"
UNVERIFIABLE = "не проверяемо"

# Кандидат в имя объекта метаданных: «Вид.Имя», обе раскладки, точка внутри.
# Это разбор текста статьи, а не storage format — там AST (см. R16).
NAME_CANDIDATE = re.compile(
    r"\b([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_]{2,})\.([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_]{2,})\b")

# Слова, после которых в статье обычно идёт заявление о связи. Список
# намеренно короткий: он лишь помечает утверждение как проверяемое графом,
# а вывод всё равно делает человек.
LINK_MARKERS = ("вызыва", "пишем", "пишет", "запис", "чита", "обновля",
                "формиру", "заполня", "использу", "движени")

# Виды, которые встречаются в тексте как обычные слова, а не как имена
# объектов. Без этого «Решение 2024. Обмен» превращается в кандидата.
STOP_PREFIXES = {"тел", "рис", "см", "т.е", "т.к", "стр", "п", "гл"}


@dataclass
class Statement:
    text: str
    category: str
    names: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Card:
    page_id: str
    title: str
    updated: str = ""
    author: str = ""
    targets: list[str] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    unsupported: dict[str, int] = field(default_factory=dict)
    parse_ok: bool = True
    graph_checked: bool = False

    def counts(self) -> dict[str, int]:
        result = {CONFIRMED: 0, CONTRADICTS: 0, MISSING: 0, UNVERIFIABLE: 0}
        for statement in self.statements:
            result[statement.category] = result.get(statement.category, 0) + 1
        return result


class Names:
    """Словарь имён из DOC-38. Только чтение файла, графа здесь нет (R13)."""

    def __init__(self, aliases: dict[str, str]):
        self._aliases = aliases

    @classmethod
    def load(cls, path: Path) -> "Names":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        aliases = {k.casefold(): v for k, v in (data.get("aliases") or {}).items()}
        for canon in data.get("canonical") or []:
            aliases.setdefault(canon.casefold(), canon)
        return cls(aliases)

    def canon(self, name: str) -> str | None:
        return self._aliases.get(name.strip().casefold())

    def known_prefix(self, prefix: str) -> bool:
        """Встречался ли такой вид объекта вообще: Справочник, Catalog и т.п."""
        needle = prefix.casefold() + "."
        return any(key.startswith(needle) for key in self._aliases)


def find_candidates(text: str, names: Names) -> tuple[list[str], list[str]]:
    """Возвращает (найденные в словаре канонические, ненайденные как есть)."""
    found: list[str] = []
    unknown: list[str] = []
    for match in NAME_CANDIDATE.finditer(text):
        prefix, whole = match.group(1), match.group(0)
        if prefix.casefold() in STOP_PREFIXES:
            continue
        canon = names.canon(whole)
        if canon:
            if canon not in found:
                found.append(canon)
        elif names.known_prefix(prefix):
            # Вид объекта настоящий, а такого объекта нет — это уже сигнал,
            # а не случайная точка в предложении.
            if whole not in unknown:
                unknown.append(whole)
    return found, unknown


def classify(text: str, names: Names, edges: set[tuple[str, str]] | None,
             targets: Iterable[str]) -> Statement:
    found, unknown = find_candidates(text, names)

    if unknown:
        return Statement(text, MISSING, names=found, unknown=unknown,
                         note="в конфигурации таких объектов не найдено")

    if not found:
        # Ни одного имени объекта — проверить нечем. Это и есть замысел.
        return Statement(text, UNVERIFIABLE)

    claims_link = any(marker in text.casefold() for marker in LINK_MARKERS)
    if claims_link and edges is not None:
        target_list = list(targets)
        for name in found:
            if not target_list:
                break
            if not any((t, name) in edges or (name, t) in edges for t in target_list):
                return Statement(
                    text, CONTRADICTS, names=found,
                    note=f"в статье связь с {name} заявлена, в графе рёбер между ним и "
                         f"объектами страницы нет")

    return Statement(text, CONFIRMED, names=found,
                     note="объекты существуют; связь проверена по графу"
                          if (claims_link and edges is not None) else
                          "объекты существуют")


def build_card(page: dict, parsed, names: Names,
               edges: set[tuple[str, str]] | None = None) -> Card:
    """page: {id, title, updated, author, storage}. parsed: Parsed из confluence_storage."""
    from confluence_storage import statements as split_statements

    card = Card(
        page_id=str(page.get("id", "")),
        title=page.get("title", ""),
        updated=page.get("updated", ""),
        author=page.get("author", ""),
        unsupported=dict(parsed.unsupported),
        parse_ok=parsed.ok,
        graph_checked=edges is not None,
    )
    if not parsed.ok:
        return card

    # DOC-32: targets страницы — объекты, встреченные в тексте.
    all_found: list[str] = []
    all_unknown: list[str] = []
    for text in split_statements(parsed):
        found, unknown = find_candidates(text, names)
        for name in found:
            if name not in all_found:
                all_found.append(name)
        for name in unknown:
            if name not in all_unknown:
                all_unknown.append(name)
    card.targets = all_found
    card.unknown_names = all_unknown

    for text in split_statements(parsed):
        card.statements.append(classify(text, names, edges, card.targets))
    return card


def suggest_verdict(card: Card) -> str:
    """Подсказка, а не решение. Решает человек — см. 6.5."""
    if not card.parse_ok:
        return "отложить — страница не разобрана, содержимое трогать нельзя"
    if card.unsupported:
        what = ", ".join(f"{k} ({v})" for k, v in card.unsupported.items())
        return f"отложить до ручного переноса: {what}"
    counts = card.counts()
    if counts[UNVERIFIABLE] == 0 and card.statements:
        return ("пометить неактуальной — в статье нет ничего, кроме производного; "
                "это отдаёт metadata-graph")
    if counts[CONTRADICTS] or counts[MISSING]:
        return "решает человек: сначала разобрать расхождения ниже"
    return "мигрировать"


def render_card(card: Card) -> str:
    counts = card.counts()
    lines = [
        f"Страница: {card.title} (page_id {card.page_id})",
        f"Обновлена: {card.updated or 'неизвестно'}, автор {card.author or 'неизвестен'}",
        f"targets: {', '.join(card.targets) if card.targets else '—'}",
    ]
    if card.unknown_names:
        lines.append(f"не найдено в графе: {', '.join(card.unknown_names)}")
    if not card.parse_ok:
        lines.append("")
        lines.append("СТРАНИЦА НЕ РАЗОБРАНА. Это не значит, что она пустая.")
        lines.append(f"Вердикт: {suggest_verdict(card)}")
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"Утверждений: {len(card.statements)}   "
        f"подтверждено {counts[CONFIRMED]} / противоречит {counts[CONTRADICTS]} / "
        f"нет объекта {counts[MISSING]} / не проверяемо {counts[UNVERIFIABLE]}")
    if not card.graph_checked:
        lines.append("Граф не опрашивался: проверено только существование объектов, "
                     "связи — нет.")

    problems = [s for s in card.statements if s.category in (CONTRADICTS, MISSING)]
    if problems:
        lines.append("")
        lines.append("Расхождения (в статье А, в конфигурации Б — кто прав, решает человек):")
        for statement in problems:
            lines.append(f"  - «{statement.text[:120]}»")
            lines.append(f"    {statement.note}")

    unverifiable = [s for s in card.statements if s.category == UNVERIFIABLE]
    if unverifiable:
        lines.append("")
        lines.append(f"Не проверяемо ({len(unverifiable)}) — сохранить дословно:")
        for statement in unverifiable[:10]:
            lines.append(f"  - «{statement.text[:120]}»")
        if len(unverifiable) > 10:
            lines.append(f"  … и ещё {len(unverifiable) - 10}")

    if card.unsupported:
        lines.append("")
        lines.append("Неподдерживаемые конструкции: "
                     + ", ".join(f"{k} ({v})" for k, v in card.unsupported.items()))

    lines.append("")
    lines.append(f"Вердикт: {suggest_verdict(card)}")
    return "\n".join(lines)


# ─── R15: машинная проверка сохранности непроверяемого ───────────────────

def normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def check_preserved(card: Card, draft_text: str) -> list[str]:
    """Возвращает список утерянных фрагментов. Пустой список — черновик годен.

    Сравнение дословное с точностью до пробелов. Мягче нельзя: перефразировка
    и есть тот способ, которым ценное исчезает незаметно.
    """
    haystack = normalize_for_compare(draft_text)
    lost = []
    for statement in card.statements:
        if statement.category != UNVERIFIABLE:
            continue
        if normalize_for_compare(statement.text) not in haystack:
            lost.append(statement.text)
    return lost


def card_to_json(card: Card) -> dict[str, Any]:
    return {
        "page_id": card.page_id, "title": card.title, "updated": card.updated,
        "author": card.author, "targets": card.targets,
        "unknown_names": card.unknown_names, "unsupported": card.unsupported,
        "parse_ok": card.parse_ok, "graph_checked": card.graph_checked,
        "counts": card.counts(), "verdict_hint": suggest_verdict(card),
        "statements": [
            {"text": s.text, "category": s.category, "names": s.names,
             "unknown": s.unknown, "note": s.note} for s in card.statements
        ],
    }


def card_from_json(data: dict[str, Any]) -> Card:
    card = Card(
        page_id=str(data.get("page_id", "")), title=data.get("title", ""),
        updated=data.get("updated", ""), author=data.get("author", ""),
        targets=list(data.get("targets") or []),
        unknown_names=list(data.get("unknown_names") or []),
        unsupported=dict(data.get("unsupported") or {}),
        parse_ok=bool(data.get("parse_ok", True)),
        graph_checked=bool(data.get("graph_checked", False)),
    )
    for item in data.get("statements") or []:
        card.statements.append(Statement(
            text=item["text"], category=item["category"],
            names=list(item.get("names") or []), unknown=list(item.get("unknown") or []),
            note=item.get("note", "")))
    return card


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Карточка страницы (DOC-33)")
    parser.add_argument("--card", required=True, help="файл карточки .json")
    parser.add_argument("--check-draft", default=None,
                        help="проверить черновик на сохранность непроверяемого (R15)")
    args = parser.parse_args(argv)

    card = card_from_json(json.loads(Path(args.card).read_text(encoding="utf-8-sig")))

    if args.check_draft:
        draft = Path(args.check_draft).read_text(encoding="utf-8-sig")
        lost = check_preserved(card, draft)
        if lost:
            print(f"ЧЕРНОВИК ОТКЛОНЁН: потеряно фрагментов «{UNVERIFIABLE}»: {len(lost)}")
            for text in lost:
                print(f"  - «{text[:140]}»")
            print("\nЭти фрагменты обязаны присутствовать дословно. "
                  "Перефразировка не считается сохранением.")
            return 1
        print(f"Черновик годен: все фрагменты «{UNVERIFIABLE}» на месте дословно.")
        return 0

    print(render_card(card))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
