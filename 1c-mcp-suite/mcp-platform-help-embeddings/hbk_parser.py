"""
Парсер HTML-страниц справки 1С (формат V8SH_*).

Формат страницы (стабильный во всех примерах из 8.3.x):

    <h1 class="V8SH_pagetitle">Имя.Метод (Name.Method)</h1>
    <p class="V8SH_title">Имя (Name)</p>
    <p class="V8SH_heading">Метод (Method)</p>
    <div class="__SINCE_SHOW_STYLE__">
        <p class="not_used">Доступен, начиная с версии 8.3.5.</p>
    </div>
    <p class="V8SH_chapter">Синтаксис:</p>
    Метод(&lt;Параметр&gt;)
    <p class="V8SH_chapter">Параметры:</p>
    <div class="V8SH_rubric">&lt;Параметр&gt; (обязательный)</div>
    Тип: <a>Строка</a>. <br>Описание параметра...
    <p class="V8SH_chapter">Возвращаемое значение:</p>...
    <p class="V8SH_chapter">Описание:</p>...
    <p class="V8SH_chapter">Доступность:</p>...
    <p class="V8SH_chapter">Пример:</p>...
    <HR>
    <a>Методическая информация</a>

Что мы извлекаем:
    - kind: method | property | event | object_type | category | unknown
    - name_ru, name_en, parent_ru, parent_en
    - since_version, deprecated
    - syntax (может быть несколько вариантов синтаксиса)
    - params: [{name, type_name, type_link, description, required}]
    - returns (тип + описание)
    - description
    - availability
    - example
    - raw_text — весь текст без HTML (fallback)

Классификация kind идёт по пути файла в .hbk, передаётся в parse_html.
"""

from __future__ import annotations

import html as _html_mod
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


# ── Типы ────────────────────────────────────────────────────────────────

@dataclass
class Param:
    name: str
    type_name: str = ""
    type_link: str = ""
    description: str = ""
    required: bool | None = None  # None = не указано, True = обязательный, False = необязательный


@dataclass
class HelpEntry:
    """Структурированное описание одной страницы справки."""
    # Идентификация
    file_path: str = ""       # путь внутри .hbk (для отладки/ссылки)
    hbk_file: str = ""        # имя .hbk, из которого извлекли
    kind: str = "unknown"     # method | property | event | object_type | category | unknown

    # Имена (RU и EN)
    name_ru: str = ""
    name_en: str = ""
    parent_ru: str = ""
    parent_en: str = ""

    # Версионирование
    since_version: str = ""
    deprecated: bool = False
    deprecated_version: str = ""

    # Содержательные секции
    syntax: str = ""                          # может быть несколько вариантов — склеиваем через \n\n
    params: list[Param] = field(default_factory=list)
    returns: str = ""
    description: str = ""
    availability: str = ""
    example: str = ""
    note: str = ""                            # "Примечание:"

    # Fallback
    raw_text: str = ""                        # весь текст без HTML

    def full_name(self) -> str:
        """Полное имя для идентификации: 'Parent.Name (ParentEn.NameEn)'."""
        # Если parent совпадает с name — это object_type или category,
        # не дублируем в виде "Foo.Foo".
        if self.parent_ru and self.name_ru and self.parent_ru == self.name_ru:
            ru = self.name_ru
        elif self.parent_ru and self.name_ru:
            ru = f"{self.parent_ru}.{self.name_ru}"
        else:
            ru = self.name_ru or self.parent_ru

        if self.parent_en and self.name_en and self.parent_en == self.name_en:
            en = self.name_en
        elif self.parent_en and self.name_en:
            en = f"{self.parent_en}.{self.name_en}"
        else:
            en = self.name_en or self.parent_en

        if ru and en:
            return f"{ru} ({en})"
        return ru or en


# ── Парсер HTML в плоский поток токенов ────────────────────────────────

class _V8Parser(HTMLParser):
    """
    Преобразует HTML в поток токенов:
      ('chapter', text)   — заголовок секции (<p class="V8SH_chapter">)
      ('rubric',  text)   — параметр (<div class="V8SH_rubric">)
      ('title',   text)   — V8SH_title
      ('heading', text)   — V8SH_heading
      ('pagetitle', text) — V8SH_pagetitle
      ('since',   text)   — __SINCE_SHOW_STYLE__
      ('link',    (href, text)) — <a href>...</a>
      ('break',   '')     — <br>, <hr>, <p> без класса
      ('text',    text)   — обычный текст

    На выходе лёгкий поток, из которого проще собирать структуру, чем
    гонять регулярки по HTML.
    """

    # Теги, которые обрывают абзац при закрытии. Всё что нужно для справки.
    _BREAKERS = frozenset({"p", "br", "hr", "div", "tr", "td", "li"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, object]] = []
        self._mode_stack: list[str] = []  # активные спец-режимы ('chapter', 'rubric', 'pagetitle' и т.д.)
        self._buffer: list[str] = []
        self._a_href: str | None = None
        self._a_buf: list[str] = []
        self._in_script_style = False
        self._in_since_block = False

    # ── служебное ──

    def _flush_text(self) -> None:
        if self._buffer:
            text = "".join(self._buffer)
            self._buffer.clear()
            # Если мы внутри спец-блока (chapter/rubric/etc.) — не выплёскиваем
            # текст как 'text', он будет прочитан при закрытии блока.
            if not self._mode_stack:
                cleaned = _normalize_ws(text)
                if cleaned:
                    self.tokens.append(("text", cleaned))
            else:
                self._mode_stack[-1:]  # noop, но можно бы аккумулировать тут
                self._collect_into_mode(text)

    def _collect_into_mode(self, text: str) -> None:
        # Накапливаем текст для активного режима
        if not hasattr(self, "_mode_buf") or self._mode_buf is None:
            self._mode_buf = []
        self._mode_buf.append(text)

    def _open_mode(self, mode: str) -> None:
        self._flush_text()
        self._mode_stack.append(mode)
        self._mode_buf = []

    def _close_mode(self, mode: str) -> None:
        # Закрываем самый внутренний режим с таким именем; если имя не совпало —
        # ничего не делаем (защита от расхождения тегов).
        if not self._mode_stack or self._mode_stack[-1] != mode:
            return
        self._mode_stack.pop()
        text = _normalize_ws("".join(self._mode_buf or []))
        self._mode_buf = []
        if text:
            self.tokens.append((mode, text))

    # ── HTMLParser API ──

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls = attr_map.get("class", "")

        if tag in ("script", "style"):
            self._in_script_style = True
            return

        if tag == "a":
            self._flush_text()
            self._a_href = attr_map.get("href", "")
            self._a_buf = []
            return

        if tag == "p":
            if "V8SH_pagetitle" in cls:
                self._open_mode("pagetitle")
                return
            if "V8SH_title" in cls:
                self._open_mode("title")
                return
            if "V8SH_heading" in cls:
                self._open_mode("heading")
                return
            if "V8SH_chapter" in cls:
                self._open_mode("chapter")
                return
            if "V8SH_versionInfo" in cls:
                self._open_mode("versionInfo")
                return
            # обычный <p> — просто разделитель
            self._flush_text()
            self.tokens.append(("break", ""))
            return

        if tag == "h1":
            if "V8SH_pagetitle" in cls:
                self._open_mode("pagetitle")
                return

        if tag == "div":
            if "V8SH_rubric" in cls:
                self._open_mode("rubric")
                return
            if "__SINCE_SHOW_STYLE__" in cls:
                self._in_since_block = True
                return

        if tag in ("br", "hr"):
            self._flush_text()
            self.tokens.append(("break", ""))

        # остальные теги — просто пропускаем, контент сольётся в текст

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in ("script", "style"):
            self._in_script_style = False
            return

        if tag == "a":
            text = _normalize_ws("".join(self._a_buf))
            href = self._a_href or ""
            self._a_href = None
            self._a_buf = []
            if self._mode_stack:
                # Внутри спец-блока — только текст ссылки идёт в накопитель,
                # href отдельно не запоминается (редко нужен именно там).
                self._collect_into_mode(text)
            else:
                if text:
                    self.tokens.append(("link", (href, text)))
            return

        if tag == "p":
            # Пытаемся закрыть все V8SH_* режимы в порядке стека.
            if self._mode_stack and self._mode_stack[-1] in ("pagetitle", "title", "heading", "chapter", "versionInfo"):
                self._close_mode(self._mode_stack[-1])
                return

        if tag == "h1":
            if self._mode_stack and self._mode_stack[-1] == "pagetitle":
                self._close_mode("pagetitle")
                return

        if tag == "div":
            if self._mode_stack and self._mode_stack[-1] == "rubric":
                self._close_mode("rubric")
                return
            if self._in_since_block:
                # Текст блока since остался в буфере, сливаем
                self._flush_text()
                # Всё что успело попасть между <div class=__SINCE…> и </div>
                # уже ушло токенами 'text'; маркируем конец блока.
                self._in_since_block = False
                return

        if tag in self._BREAKERS:
            self._flush_text()
            self.tokens.append(("break", ""))

    def handle_data(self, data: str) -> None:
        if self._in_script_style:
            return
        if self._a_href is not None:
            self._a_buf.append(data)
            return
        if self._mode_stack:
            self._collect_into_mode(data)
            return
        self._buffer.append(data)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        # Закрываем всё что не закрылось
        while self._mode_stack:
            self._close_mode(self._mode_stack[-1])
        self._flush_text()


# ── Утилиты ────────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")
_NBSP_RE = re.compile(r"[\u00a0\u2002\u2003\u2009]+")


def _normalize_ws(s: str) -> str:
    s = _NBSP_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


# Разбор pagetitle вида "Рус (Eng)" или "Рус.Метод (Eng.Method)".
# Ключевая сложность: и RU, и EN части могут содержать угловые скобки
# (например "<Имя справочника>"), поэтому простая regexp не справляется.
# Решение: находим ПОСЛЕДНЮЮ непарную пару круглых скобок на верхнем уровне
# (вложенные угловые скобки не считаются), всё что до — RU, внутри — EN.
def _split_ru_en(text: str) -> tuple[str, str]:
    """
    'Имя (Name)' → ('Имя', 'Name').
    'СпрМенеджер.<Имя справочника>.Найти (CatalogManager.<Catalog name>.Find)'
        → ('СпрМенеджер.<Имя справочника>.Найти', 'CatalogManager.<Catalog name>.Find')
    Если подходящей пары круглых скобок нет — ('Имя', '').
    """
    if not text:
        return "", ""
    s = text.strip()
    if not s.endswith(")"):
        return s, ""

    # Идём от конца, находим парную открывающую скобку
    depth = 0
    open_idx = -1
    for i in range(len(s) - 1, -1, -1):
        c = s[i]
        if c == ")":
            depth += 1
        elif c == "(":
            depth -= 1
            if depth == 0:
                open_idx = i
                break
    if open_idx < 0:
        return s, ""

    ru_part = s[:open_idx].rstrip()
    en_part = s[open_idx + 1:-1].strip()

    # Если EN-часть выглядит не как имя (содержит недопустимое), возможно, мы
    # отрезали не то (например фразу в скобках). Эвристика: в EN-части должна
    # быть хоть одна латинская буква, и она не должна начинаться с кириллицы.
    if not re.search(r"[A-Za-z]", en_part):
        return s, ""

    return ru_part, en_part


def _split_dot(full: str) -> tuple[str, str]:
    """
    'Родитель.Имя' → ('Родитель', 'Имя'). Без точки → ('', full).
    Учитывает угловые скобки: 'Родитель.<Имя типа>.Метод' → ('Родитель.<Имя типа>', 'Метод'),
    а не ('Родитель.<Имя типа', 'типа>.Метод'). Для этого используем
    баланс угловых скобок при поиске последней точки-разделителя.
    """
    if "." not in full:
        return "", full.strip()
    depth = 0
    # Ищем последнюю точку на верхнем уровне (depth == 0).
    split_at = -1
    for i in range(len(full) - 1, -1, -1):
        c = full[i]
        if c == ">":
            depth += 1
        elif c == "<":
            depth -= 1
        elif c == "." and depth == 0:
            split_at = i
            break
    if split_at < 0:
        return "", full.strip()
    return full[:split_at].strip(), full[split_at + 1:].strip()


_REQUIRED_RE = re.compile(r"\(\s*обязательн\w*\s*\)", re.IGNORECASE)
_OPTIONAL_RE = re.compile(r"\(\s*необязательн\w*\s*\)", re.IGNORECASE)
_SINCE_VERSION_RE = re.compile(r"начиная с версии\s+([\d.]+?)\.?(?:\s|$|[,;])", re.IGNORECASE)
_DEPRECATED_RE = re.compile(r"устарел\w*(?:\s+(?:с версии\s+)?([\d.]+?)\.?(?:\s|$|[,;]))?", re.IGNORECASE)


# ── Классификация kind по пути файла ───────────────────────────────────

def classify_kind_by_path(file_path: str) -> str:
    """
    Определяет тип страницы по пути внутри .hbk.
    Пути видели такие:
      objects/catalog125.html                              → category (крупный раздел)
      objects/.../TypeName.html                            → object_type
      objects/.../TypeName/methods/MethodName42.html       → method
      objects/.../TypeName/properties/PropName10182.html   → property
      objects/.../TypeName/events/EventName599.html        → event
    """
    lower = file_path.lower()
    if "/methods/" in lower:
        return "method"
    if "/properties/" in lower:
        return "property"
    if "/events/" in lower:
        return "event"
    if lower.startswith("objects/"):
        # Различаем category (objects/catalogN.html — 2 части) и object_type (3+ части).
        depth = file_path.count("/")
        if depth == 1:
            return "category"
        return "object_type"
    if lower.startswith("tables/"):
        return "table"
    return "unknown"


# ── Извлечение структуры из токенов ────────────────────────────────────

def _tokens_to_entry(tokens: list[tuple[str, object]], entry: HelpEntry) -> HelpEntry:
    """
    Проходит по токенам и заполняет entry. Работает конечным автоматом:
    последняя 'chapter' задаёт, куда копятся последующие 'text'/'link'.
    """
    current_chapter: str = ""      # оригинальное название главы ('Синтаксис:', 'Параметры:', ...)
    current_chapter_key: str = ""  # нормализованный ключ (syntax|params|returns|description|availability|example|note|...)
    chapters_buf: dict[str, list[str]] = {}
    params_buf: list[Param] = []
    current_param: Param | None = None
    all_text_buf: list[str] = []

    def _flush_param() -> None:
        nonlocal current_param
        if current_param is not None:
            # Чистим description от ведущего "Тип: " — это будет отдельным полем
            m = re.match(r"^\s*Тип:\s*([^.\n]+?)\.?\s*(.*)$", current_param.description, re.DOTALL)
            if m:
                current_param.type_name = current_param.type_name or m.group(1).strip()
                current_param.description = _normalize_ws(m.group(2))
            params_buf.append(current_param)
        current_param = None

    def _append_to_chapter(text: str) -> None:
        if not text:
            return
        if current_chapter_key == "params":
            if current_param is not None:
                # Накапливаем описание параметра
                if current_param.description:
                    current_param.description += " " + text
                else:
                    current_param.description = text
            # вне rubric внутри chapter=params текст обычно относится к следующему
            # параметру — но rubric нам его даст, так что молча игнорируем
        elif current_chapter_key == "syntax":
            # syntax хранится как список "вариантов". Текущий вариант — последний.
            lst = chapters_buf.setdefault("syntax", [""])
            if not lst:
                lst.append("")
            lst[-1] = (lst[-1] + " " + text) if lst[-1] else text
        elif current_chapter_key:
            chapters_buf.setdefault(current_chapter_key, []).append(text)

    for kind, payload in tokens:
        if kind == "pagetitle":
            # 'Родитель.Имя (Parent.Name)' → name_ru, name_en, parent_ru, parent_en
            ru, en = _split_ru_en(str(payload))
            all_text_buf.append(str(payload))
            p_ru, n_ru = _split_dot(ru)
            p_en, n_en = _split_dot(en)
            entry.parent_ru = entry.parent_ru or p_ru
            entry.name_ru = entry.name_ru or n_ru
            entry.parent_en = entry.parent_en or p_en
            entry.name_en = entry.name_en or n_en

        elif kind == "title":
            # 'Имя (Name)' — имя родительского объекта (без .Метод)
            ru, en = _split_ru_en(str(payload))
            all_text_buf.append(str(payload))
            if not entry.parent_ru and not entry.name_ru:
                # object_type / category: это само имя объекта
                entry.name_ru = entry.name_ru or ru
                entry.name_en = entry.name_en or en
            elif ru == entry.name_ru or en == entry.name_en:
                # title повторяет name — это страница типа (не метод/свойство),
                # где pagetitle и title совпадают. Не дублируем в parent.
                pass
            else:
                entry.parent_ru = entry.parent_ru or ru
                entry.parent_en = entry.parent_en or en

        elif kind == "heading":
            # 'Метод (Method)' — имя метода/свойства/события
            ru, en = _split_ru_en(str(payload))
            all_text_buf.append(str(payload))
            entry.name_ru = entry.name_ru or ru
            entry.name_en = entry.name_en or en

        elif kind == "chapter":
            _flush_param()
            current_chapter = str(payload)
            current_chapter_key = _chapter_key(current_chapter)
            all_text_buf.append(current_chapter)
            # Каждый новый "Синтаксис:" — отдельный вариант. Маркируем это
            # началом нового элемента в списке syntax-фрагментов.
            if current_chapter_key == "syntax":
                chapters_buf.setdefault("syntax", []).append("")
            # "Вариант синтаксиса: Из строки" — подпись варианта, пишем как
            # отдельный элемент перед самим syntax'ом.
            if current_chapter.lower().startswith("вариант синтаксиса"):
                label = current_chapter.rstrip(" :").strip()
                chapters_buf.setdefault("syntax_labels", []).append(label)

        elif kind == "rubric":
            # Параметр: "<Имя> (обязательный)"
            if current_chapter_key != "params":
                # rubric за пределами "Параметры:" игнорируем как шум
                continue
            _flush_param()
            name_text = str(payload)
            all_text_buf.append(name_text)
            required: bool | None = None
            if _REQUIRED_RE.search(name_text):
                required = True
            elif _OPTIONAL_RE.search(name_text):
                required = False
            # Сам идентификатор параметра — до первой скобки
            pname = name_text.split("(")[0].strip()
            # Снимаем угловые скобки если есть
            pname = pname.strip("<>").strip()
            current_param = Param(name=pname, required=required)

        elif kind == "text":
            text = str(payload)
            all_text_buf.append(text)
            _append_to_chapter(text)

        elif kind == "link":
            href, text = payload  # type: ignore[misc]
            all_text_buf.append(text)
            if current_chapter_key == "params" and current_param is not None:
                # Ссылка в описании параметра — обычно это тип (первая ссылка после "Тип:")
                if not current_param.type_name:
                    current_param.type_name = text
                    current_param.type_link = href
                else:
                    if current_param.description:
                        current_param.description += " " + text
                    else:
                        current_param.description = text
            else:
                _append_to_chapter(text)

        elif kind == "versionInfo":
            # 'Доступен, начиная с версии 8.3.5.' / 'Устарел с 8.3.20.'
            text = str(payload)
            all_text_buf.append(text)
            if m := _SINCE_VERSION_RE.search(text):
                entry.since_version = entry.since_version or m.group(1)
            if m := _DEPRECATED_RE.search(text):
                entry.deprecated = True
                entry.deprecated_version = entry.deprecated_version or (m.group(1) or "")

        elif kind == "break":
            # просто разделитель; кладём пробел, чтобы слова не слипались
            if current_chapter_key:
                _append_to_chapter(" ")
            all_text_buf.append(" ")

        else:
            # неизвестный токен — пропускаем
            pass

    _flush_param()

    # Сборка полей
    entry.params = params_buf

    # Собираем syntax с нумерацией вариантов если их больше одного.
    # Пустые элементы списка (бывают после "Вариант синтаксиса:" без
    # следующей секции) пропускаем.
    syntax_parts = [_normalize_ws(s) for s in chapters_buf.get("syntax", []) if _normalize_ws(s)]
    syntax_labels = chapters_buf.get("syntax_labels", [])
    if len(syntax_parts) > 1:
        labeled = []
        for i, s in enumerate(syntax_parts, start=1):
            label = syntax_labels[i - 1] if i - 1 < len(syntax_labels) else ""
            if label:
                labeled.append(f"{i}. {label}:\n   {s}")
            else:
                labeled.append(f"{i}. {s}")
        entry.syntax = "\n".join(labeled)
    else:
        entry.syntax = syntax_parts[0] if syntax_parts else ""

    entry.returns = _normalize_ws(" ".join(chapters_buf.get("returns", [])))
    entry.description = _normalize_ws(" ".join(chapters_buf.get("description", [])))
    entry.availability = _normalize_ws(" ".join(chapters_buf.get("availability", [])))
    entry.example = _normalize_ws(" ".join(chapters_buf.get("example", [])))
    entry.note = _normalize_ws(" ".join(chapters_buf.get("note", [])))

    # "Тип: X ." → "X" (убираем маркер и хвостовую точку)
    if entry.returns:
        m = re.match(r"^\s*Тип:\s*(.*?)\s*\.?\s*$", entry.returns, re.DOTALL)
        if m:
            entry.returns = _normalize_ws(m.group(1))

    entry.raw_text = _normalize_ws(" ".join(all_text_buf))

    # Ищем "Доступен, начиная с версии 8.3.X" в сыром тексте, если
    # versionInfo-токена не было (блок __SINCE_SHOW_STYLE__ внутри обычного div).
    if not entry.since_version:
        if m := _SINCE_VERSION_RE.search(entry.raw_text):
            entry.since_version = m.group(1)
    if not entry.deprecated:
        if _DEPRECATED_RE.search(entry.raw_text):
            entry.deprecated = True

    return entry


# Ключи глав — приводим к одному словарю с устойчивыми ключами.
_CHAPTER_MAP = {
    "синтаксис": "syntax",
    "syntax": "syntax",
    "вариант синтаксиса": "syntax",  # вариант синтаксиса — добавляем как ещё один кусок syntax
    "параметры": "params",
    "parameters": "params",
    "возвращаемое значение": "returns",
    "return value": "returns",
    "описание": "description",
    "описание варианта метода": "description",
    "description": "description",
    "доступность": "availability",
    "availability": "availability",
    "использование": "usage",
    "usage": "usage",
    "использование в версии": "version_usage",
    "пример": "example",
    "example": "example",
    "примечание": "note",
    "note": "note",
    "see also": "see_also",
    "см. также": "see_also",
    "связанные свойства": "related_props",
    "связанные методы": "related_methods",
    "свойства": "members_props",  # для object_type
    "методы": "members_methods",
    "события": "members_events",
}


def _chapter_key(title: str) -> str:
    """Нормализует 'Синтаксис:' → 'syntax'."""
    base = title.rstrip(" :").strip().lower()
    return _CHAPTER_MAP.get(base, "")


# ── Публичная функция ──────────────────────────────────────────────────

# ─── FIX-13: повествовательные страницы без классов V8SH_* ───────────────
#
# Парсер выше держится целиком на классах `V8SH_pagetitle`, `V8SH_chapter`
# и прочих — это разметка карточек синтакс-помощника (shcntx_ru.hbk).
# Но в справке есть и другой формат: обычные статьи документации, где
# заголовок это `<H1 class="">`, а тело — просто `<P>`. Так устроены
# shquery_ru.hbk (язык запросов) и shclang_ru.hbk (синтаксис языка).
#
# На таких страницах ни одно правило не срабатывало: имя и описание
# оставались пустыми, и `iter_chunks_from_dir` отбрасывал запись как
# пустую. В логе это выглядело как «HTML: 25, чанков: 0» — файл прочитан,
# страницы извлечены, а в индекс не попало ничего. Ни ошибки, ни
# предупреждения: молчаливая потеря целого раздела справки.
#
# Заметить это можно было только сопоставив две колонки лога, что и
# произошло — через несколько недель после того, как индекс собрали.
#
# Фолбэк не трогает основной путь: он включается, только если карточный
# разбор не дал ни имени, ни описания. Для shcntx_ru.hbk (25 509 страниц)
# поведение не меняется вообще.


class _PlainParser(HTMLParser):
    """
    Минимальный разбор статьи: заголовок и текст.

    Отдельный класс, а не ветка в _V8Parser: тот держит стек режимов и
    сложную логику секций, и вмешательство туда рискует сломать разбор
    25 тысяч рабочих страниц ради 46 нерабочих.
    """

    SKIP_TAGS = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_h1 = False
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag in ("p", "br", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_h1:
            self.h1 += data
        self._parts.append(data)

    def text(self) -> str:
        return _normalize_ws("".join(self._parts))


def _apply_plain_fallback(entry: HelpEntry, html: str) -> None:
    """Заполняет пустую entry из обычной HTML-статьи."""
    plain = _PlainParser()
    try:
        plain.feed(html)
        plain.close()
    except Exception:
        return

    name = _normalize_ws(plain.h1) or _normalize_ws(plain.title)
    body = plain.text()

    # Заголовок обычно повторяется первой строкой текста — не дублируем.
    if name and body.startswith(name):
        body = body[len(name):].lstrip(" .—-")

    if not name and not body:
        return

    entry.name_ru = name
    entry.description = body
    if entry.kind == "unknown":
        # Отдельный kind, чтобы такие страницы можно было отфильтровать в
        # platform_help_search и отличить от карточек метода.
        entry.kind = "article"
    if not entry.raw_text:
        entry.raw_text = body


def parse_html(file_path: str, raw: bytes, *, hbk_file: str = "") -> HelpEntry:
    """
    Парсит одну HTML-страницу справки.
    file_path — имя внутри .hbk (например 'objects/catalog213/.../Add4692.html').
    raw — сырые байты HTML (utf-8).
    """
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("cp1251", errors="replace")

    # Справка 1С местами генерит битый HTML (например <boody> вместо <body>),
    # стандартный HTMLParser терпимо к этому относится — ему всё равно.
    parser = _V8Parser()
    parser.feed(html)
    parser.close()

    entry = HelpEntry(file_path=file_path, hbk_file=hbk_file, kind=classify_kind_by_path(file_path))
    _tokens_to_entry(parser.tokens, entry)

    # Раскрываем оставшиеся HTML-энтити в текстовых полях на случай если
    # convert_charrefs не всё дожевал.
    entry.syntax = _html_mod.unescape(entry.syntax)
    entry.description = _html_mod.unescape(entry.description)
    entry.example = _html_mod.unescape(entry.example)
    entry.note = _html_mod.unescape(entry.note)
    entry.availability = _html_mod.unescape(entry.availability)
    entry.returns = _html_mod.unescape(entry.returns)
    for p in entry.params:
        p.description = _html_mod.unescape(p.description)
        p.type_name = _html_mod.unescape(p.type_name)

    # FIX-13: карточный разбор ничего не дал — пробуем как обычную статью.
    # Условие то же, по которому индексатор отбрасывает запись, поэтому
    # страница либо будет разобрана здесь, либо всё равно была бы потеряна.
    if not entry.name_ru and not entry.name_en and not entry.description:
        _apply_plain_fallback(entry, html)

    return entry
