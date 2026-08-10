"""
Чанкинг HelpEntry для индексации в Qdrant.

Идея: одна страница справки порождает несколько специализированных чанков,
каждый — самостоятельный по смыслу и оптимальный для своего типа запросов.

Типы чанков:
    card         — плотная карточка (имя + kind + краткое описание +
                   since + returns + availability). Главный чанк, есть
                   у каждой entry. Работает на все типы запросов: и
                   семантических ("метод для поиска по наименованию"),
                   и точечных ("ПоляСхемыЗапроса.Найти").

    params       — перечень параметров с типами и описаниями. Генерится
                   только если у entry есть params. Для запросов
                   "какие параметры у Метод.Добавить".

    syntax       — сигнатуры синтаксиса (часто несколько вариантов).
                   Для BM25 важны уникальные токены-идентификаторы.

    example      — если есть пример кода. Отдельным чанком, чтобы
                   находился при запросах "как использовать X".

    description  — если description большой (>1500 символов). Режется
                   по предложениям на куски ≤1500. Иначе целиком идёт
                   в card.

У каждого чанка payload содержит все ключевые поля entry (name_ru/en,
parent_ru/en, kind, since, deprecated, hbk_file, file_path, chunk_type),
чтобы фильтровать и точечно доставать без подъёма полного документа.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Iterator

from hbk_parser import HelpEntry, Param


# Порог: description длиннее этого — режем. Иначе идёт целиком в card.
LONG_DESC_THRESHOLD = 1500
# Целевой размер длинного куска (разделитель — предложение).
LONG_CHUNK_TARGET = 1200
# Если текст длиннее этого — рубим даже по словам, лишь бы влезло.
HARD_CHUNK_LIMIT = 2000


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", re.UNICODE)


def _chunk_by_sentences(text: str, target: int = LONG_CHUNK_TARGET) -> list[str]:
    """
    Режет текст по предложениям, склеивая их в куски примерно по target
    символов. Последнее предложение куска не обрезается.
    """
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if not s:
            continue
        # Защита от аномальных «предложений» длиной >HARD_CHUNK_LIMIT —
        # режем их по словам.
        while len(s) > HARD_CHUNK_LIMIT:
            head = s[:HARD_CHUNK_LIMIT]
            last_space = head.rfind(" ")
            if last_space < 100:
                last_space = HARD_CHUNK_LIMIT
            chunks.append(head[:last_space])
            s = s[last_space:].lstrip()

        if not current:
            current = s
        elif len(current) + 1 + len(s) <= target:
            current += " " + s
        else:
            chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


def _format_param(p: Param) -> str:
    """Строка для перечня параметров."""
    req_marker = {True: "обязательный", False: "необязательный", None: ""}[p.required]
    pieces = [p.name]
    if req_marker:
        pieces.append(f"({req_marker})")
    if p.type_name:
        pieces.append(f"— {p.type_name}")
    line = " ".join(pieces)
    if p.description:
        line = f"{line}: {p.description}"
    return line


def _kind_label(kind: str) -> str:
    """Человекочитаемое название kind для карточки."""
    return {
        "method": "метод",
        "property": "свойство",
        "event": "событие",
        "object_type": "тип",
        "category": "раздел",
        "table": "таблица",
        "unknown": "",
    }.get(kind, kind)


def _camel_split(name: str) -> list[str]:
    """
    Разбивает CamelCase-идентификатор по границам регистра, сохраняя
    многословные капсы-последовательности как один токен:
      'НайтиПоНаименованию' → ['Найти', 'По', 'Наименованию']
      'XMLReader'           → ['XML', 'Reader']
      'getHTTPResponse'     → ['get', 'HTTP', 'Response']

    Идея в том, чтобы BM25 видел не только цельное имя как один токен,
    но и его морфемы как самостоятельные слова. Тогда запрос
    'НайтиПоНаименованию' (одно слово) ложится и на целое имя,
    и на сумму 'Найти + По + Наименованию' — второй сигнал нам не нужен
    в этом случае, но для фраз типа 'найти по имени' он решающий.
    """
    if not name:
        return []
    # Регулярка: либо блок из подряд идущих заглавных с кириллицей/латиницей,
    # либо заглавная + строчные, либо просто строчные.
    tokens = re.findall(
        r"[A-ZА-ЯЁ]+(?=[A-ZА-ЯЁ][a-zа-яё])"  # HTTP в HTTPResponse
        r"|[A-ZА-ЯЁ]?[a-zа-яё]+"             # Найти, Response
        r"|[A-ZА-ЯЁ]+",                       # XML, ID (концевые капсы)
        name,
    )
    return [t for t in tokens if t]


def _build_name_signal(entry: HelpEntry) -> list[str]:
    """
    Собирает строки с именами, которые добавляются в card-чанк для
    усиления сигнала BM25 и dense-эмбеддинга по точному имени.

    Возвращает две строки:
      Имена: <все цельные идентификаторы через пробел>
      Слова: <разложение по CamelCase>
    Плюс пустой пропуск, если разлагать нечего (имя — одно слово).
    """
    full_ids: list[str] = []
    camel_tokens: list[str] = []

    # Цельные идентификаторы: имя, имя.родитель, EN-пара
    candidates = [
        entry.name_ru,
        entry.name_en,
        f"{entry.parent_ru}.{entry.name_ru}" if entry.parent_ru and entry.name_ru else "",
        f"{entry.parent_en}.{entry.name_en}" if entry.parent_en and entry.name_en else "",
    ]
    seen = set()
    for c in candidates:
        c = c.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        full_ids.append(c)

    # CamelCase-разложение коротких имён (без точки — parent разберётся отдельно).
    for nm in (entry.name_ru, entry.name_en):
        if not nm:
            continue
        parts = _camel_split(nm)
        # Добавляем разложение только если оно осмысленно (≥2 частей и имя
        # длиннее чем просто слово).
        if len(parts) >= 2:
            camel_tokens.extend(parts)

    lines: list[str] = []
    if full_ids:
        lines.append("Имена: " + " ".join(full_ids))
    if camel_tokens:
        # Дедуп с сохранением порядка
        seen = set()
        uniq = []
        for t in camel_tokens:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        lines.append("Слова: " + " ".join(uniq))
    return lines


def build_card_text(entry: HelpEntry) -> str:
    """
    Собирает текст «карточки» entry. Карточка включает:
      - kind + full_name
      - усиление сигнала по именам (цельные + CamelCase-разложение)
      - description (если <= LONG_DESC_THRESHOLD, целиком; иначе первые ~1500 симв)
      - returns (если есть)
      - availability (если есть)
      - since/deprecated
    """
    lines: list[str] = []

    header = entry.full_name() or "(без имени)"
    label = _kind_label(entry.kind)
    if label:
        lines.append(f"[{label}] {header}")
    else:
        lines.append(header)

    # Усиление имён: повтор как отдельные токены + морфологическое разложение.
    # Делаем ДО описания, чтобы BM25 и e5 "обратили внимание" на имена в
    # первую очередь.
    lines.extend(_build_name_signal(entry))

    desc = entry.description or ""
    if len(desc) > LONG_DESC_THRESHOLD:
        # Первое предложение + "…" — в карточку помещаем только аннотацию.
        first_sentence = _SENTENCE_SPLIT_RE.split(desc, maxsplit=1)[0]
        if len(first_sentence) > LONG_DESC_THRESHOLD:
            first_sentence = first_sentence[:LONG_DESC_THRESHOLD].rstrip() + "…"
        lines.append(first_sentence)
    elif desc:
        lines.append(desc)

    if entry.returns:
        lines.append(f"Возвращает: {entry.returns}")

    if entry.availability:
        lines.append(f"Доступность: {entry.availability}")

    meta = []
    if entry.since_version:
        meta.append(f"доступен с {entry.since_version}")
    if entry.deprecated:
        if entry.deprecated_version:
            meta.append(f"УСТАРЕЛ с {entry.deprecated_version}")
        else:
            meta.append("УСТАРЕЛ")
    if meta:
        lines.append(" · ".join(meta))

    return "\n".join(lines)


def build_params_text(entry: HelpEntry) -> str:
    """Текст чанка с параметрами. Пустой если params нет."""
    if not entry.params:
        return ""
    lines = [f"[параметры] {entry.full_name()}"]
    for p in entry.params:
        lines.append("— " + _format_param(p))
    return "\n".join(lines)


def build_syntax_text(entry: HelpEntry) -> str:
    """Текст чанка с синтаксисом. Пустой если syntax нет."""
    if not entry.syntax:
        return ""
    # Имена тут особенно важны для BM25 — подкидываем их в начало.
    header_parts = []
    if entry.parent_ru or entry.name_ru:
        header_parts.append(f"{entry.parent_ru}.{entry.name_ru}".strip("."))
    if entry.parent_en or entry.name_en:
        header_parts.append(f"{entry.parent_en}.{entry.name_en}".strip("."))
    header = " / ".join(h for h in header_parts if h)
    lines = [f"[синтаксис] {header}" if header else "[синтаксис]"]
    lines.append(entry.syntax)
    return "\n".join(lines)


def build_example_text(entry: HelpEntry) -> str:
    if not entry.example:
        return ""
    return f"[пример] {entry.full_name()}\n{entry.example}"


def _base_payload(entry: HelpEntry) -> dict:
    """Общие поля payload, одинаковые у всех чанков одной entry."""
    return {
        "kind": entry.kind,
        "name_ru": entry.name_ru,
        "name_en": entry.name_en,
        "parent_ru": entry.parent_ru,
        "parent_en": entry.parent_en,
        "full_name": entry.full_name(),
        "since_version": entry.since_version,
        "deprecated": entry.deprecated,
        "deprecated_version": entry.deprecated_version,
        "hbk_file": entry.hbk_file,
        "file_path": entry.file_path,
        # Поля, которые точечно пригодятся read-path'у для быстрой выдачи
        # карточки без лишних поисков
        "availability": entry.availability,
        "returns": entry.returns,
    }


def build_chunks(entry: HelpEntry) -> Iterator[dict]:
    """
    Главная функция: превращает одну entry в поток чанков.
    Каждый чанк — dict с ключами:
        text          — текст для эмбеддинга
        chunk_type    — 'card' | 'params' | 'syntax' | 'example' | 'description'
        chunk_index   — порядковый номер (у description может быть несколько частей)
        + все поля _base_payload
    """
    base = _base_payload(entry)

    # 1. card — всегда
    card_text = build_card_text(entry)
    yield {
        **base,
        "chunk_type": "card",
        "chunk_index": 0,
        "text": card_text,
    }

    # 2. params — если есть
    params_text = build_params_text(entry)
    if params_text:
        yield {
            **base,
            "chunk_type": "params",
            "chunk_index": 0,
            "text": params_text,
        }

    # 3. syntax — если есть
    syntax_text = build_syntax_text(entry)
    if syntax_text:
        yield {
            **base,
            "chunk_type": "syntax",
            "chunk_index": 0,
            "text": syntax_text,
        }

    # 4. example — если есть
    example_text = build_example_text(entry)
    if example_text:
        yield {
            **base,
            "chunk_type": "example",
            "chunk_index": 0,
            "text": example_text,
        }

    # 5. description — только если большое. Иначе оно уже в card.
    desc = entry.description or ""
    if len(desc) > LONG_DESC_THRESHOLD:
        parts = _chunk_by_sentences(desc)
        header = entry.full_name()
        for idx, part in enumerate(parts):
            yield {
                **base,
                "chunk_type": "description",
                "chunk_index": idx,
                "text": f"[описание ч.{idx + 1}] {header}\n{part}",
            }
