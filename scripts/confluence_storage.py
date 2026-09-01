#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-31. Разбор Confluence storage format в структуру. Только AST.

Регулярки по storage format запрещены (R16): это XML, и вытаскивать из него
текст подстроками — способ молча потерять половину содержимого. Здесь
настоящий разбор через ElementTree, а всё, что разобрать не вышло,
перечисляется в отчёте поимённо, а не выбрасывается.

Почему парсер «снисходительный»
────────────────────────────────
Storage format — это XML, который строгим парсером берётся не всегда:

  • именованные HTML-сущности (&nbsp;, &mdash;) в XML не определены, и
    штатный ElementTree падает на первой же;
  • префиксы ac: и ri: используются без объявления пространств имён — они
    подразумеваются платформой.

Оба случая лечатся подготовкой документа перед разбором, а не правкой
текста после. Сущности превращаются в числовые ссылки по таблице из
стандартной библиотеки, документ оборачивается корнем с объявленными
пространствами. Никакого угадывания содержимого при этом не происходит.

Что на выходе
─────────────
`parse_storage(xml)` отдаёт `Parsed`:

  blocks      — список блоков (heading, paragraph, list_item, table_row,
                code, panel, unsupported) с текстом и уровнем;
  unsupported — какие конструкции встретились и не поддержаны, с числом
                вхождений: draw.io, jira, вложения, диаграммы;
  ok          — разобралось ли вообще. False означает «страница не
                разобрана», а не «страница пустая». Разница существенная.

Текст макросов, которые мы не понимаем, НЕ выбрасывается: он попадает в
блок `unsupported` вместе с именем макроса, чтобы человек в карточке увидел,
что именно осталось за бортом.
"""

from __future__ import annotations

import html.entities
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable

# B-3 / FAIL-2: защита потоков ровно в той форме, которую требует
# scripts/tests_host_scripts.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

AC = "http://atlassian.com/content"
RI = "http://atlassian.com/resource-identifier"

# Макросы, из которых мы умеем достать содержимое.
SUPPORTED_MACROS = {"code", "panel", "info", "note", "warning", "tip",
                    "expand", "section", "column", "quote", "status"}

# Блочные теги, по которым режется текст. Инлайновые (b, i, a, span, code)
# намеренно не перечислены: их содержимое склеивается в текст родителя.
BLOCK_TAGS = {"p", "div", "blockquote", "pre"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


@dataclass
class Block:
    kind: str            # heading | paragraph | list_item | table_row | code | panel | unsupported
    text: str
    level: int = 0       # для heading
    detail: str = ""     # для unsupported — имя макроса


@dataclass
class Parsed:
    ok: bool
    blocks: list[Block] = field(default_factory=list)
    unsupported: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.text.strip())


def prepare(xml: str) -> str:
    """Именованные сущности → числовые, объявление пространств имён.

    Это подготовка документа к разбору, а не разбор подстроками: содержимое
    не интерпретируется, меняется только форма записи тех же символов.
    """
    def to_numeric(match: re.Match) -> str:
        name = match.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return match.group(0)          # эти XML знает сам
        code = html.entities.name2codepoint.get(name)
        return f"&#{code};" if code else match.group(0)

    normalized = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", to_numeric, xml)
    return (f'<root xmlns:ac="{AC}" xmlns:ri="{RI}">{normalized}</root>')


def _inline_text(element: ET.Element) -> str:
    """Весь текст поддерева одной строкой, без вложенных блоков."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(_inline_text(child))
        if child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _local(tag: str) -> tuple[str, str]:
    """Тег → (пространство, имя). ElementTree отдаёт '{ns}name'."""
    if tag.startswith("{"):
        namespace, _, name = tag[1:].partition("}")
        return namespace, name
    return "", tag


def _macro_name(element: ET.Element) -> str:
    return element.get(f"{{{AC}}}name", "") or element.get("name", "")


def _walk(element: ET.Element, out: list[Block], unsupported: dict[str, int]) -> None:
    namespace, name = _local(element.tag)

    if namespace == AC and name == "structured-macro":
        macro = _macro_name(element) or "без имени"
        if macro in SUPPORTED_MACROS:
            body = []
            for child in element.iter():
                _, child_name = _local(child.tag)
                if child_name in ("plain-text-body", "rich-text-body"):
                    body.append(_inline_text(child))
            text = "\n".join(t for t in body if t)
            out.append(Block("code" if macro == "code" else "panel", text, detail=macro))
        else:
            unsupported[macro] = unsupported.get(macro, 0) + 1
            out.append(Block("unsupported", _inline_text(element), detail=macro))
        return

    if namespace == AC and name in ("image", "link"):
        kind = "изображение" if name == "image" else "ссылка ac:link"
        target = ""
        for child in element:
            _, child_name = _local(child.tag)
            if child_name in ("attachment", "page", "url"):
                target = (child.get(f"{{{RI}}}filename")
                          or child.get(f"{{{RI}}}content-title")
                          or child.get(f"{{{RI}}}value") or "")
        if name == "image":
            unsupported[kind] = unsupported.get(kind, 0) + 1
            out.append(Block("unsupported", target, detail=kind))
            return
        # ac:link ведёт себя как инлайн, текст заберёт родитель.

    if name in HEADINGS:
        out.append(Block("heading", _inline_text(element), level=HEADINGS[name]))
        return

    if name == "li":
        out.append(Block("list_item", _inline_text(element)))
        return

    if name == "tr":
        cells = []
        for cell in element:
            _, cell_name = _local(cell.tag)
            if cell_name in ("td", "th"):
                cells.append(_inline_text(cell))
        out.append(Block("table_row", " | ".join(c for c in cells if c)))
        return

    if name in BLOCK_TAGS:
        # Блок с вложенными блоками разбираем вглубь, простой — берём целиком.
        has_nested = any(_local(child.tag)[1] in BLOCK_TAGS | set(HEADINGS)
                         | {"li", "tr", "structured-macro"} for child in element.iter()
                         if child is not element)
        if not has_nested:
            text = _inline_text(element)
            if text:
                out.append(Block("paragraph", text))
            return

    for child in element:
        _walk(child, out, unsupported)


def parse_storage(xml: str) -> Parsed:
    if not (xml or "").strip():
        return Parsed(ok=True)
    try:
        root = ET.fromstring(prepare(xml))
    except ET.ParseError as exc:
        # Страница НЕ разобрана. Это не «страница пустая» — вызывающий
        # обязан различать, иначе потеряет содержимое молча (R16).
        return Parsed(ok=False, error=f"XML не разобран: {exc}")

    blocks: list[Block] = []
    unsupported: dict[str, int] = {}
    for child in root:
        _walk(child, blocks, unsupported)
    if root.text and root.text.strip():
        blocks.insert(0, Block("paragraph", re.sub(r"\s+", " ", root.text).strip()))

    return Parsed(ok=True, blocks=[b for b in blocks if b.text.strip() or b.kind == "unsupported"],
                  unsupported=unsupported)


def statements(parsed: Parsed) -> list[str]:
    """Разбиение на утверждения для классификатора (DOC-33).

    Заголовки не утверждения — это оглавление. Абзац режется на предложения,
    пункт списка и строка таблицы считаются одним утверждением целиком:
    резать их по точке значит терять смысл, который держится на строке.
    """
    result: list[str] = []
    for block in parsed.blocks:
        if block.kind in ("heading", "unsupported"):
            continue
        if block.kind in ("list_item", "table_row", "code"):
            text = block.text.strip()
            if text:
                result.append(text)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[А-ЯA-ZЁ])", block.text):
            sentence = sentence.strip()
            if len(sentence) >= 12:
                result.append(sentence)
    return result
