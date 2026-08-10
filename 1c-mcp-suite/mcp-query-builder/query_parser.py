"""
Парсер языка запросов 1С (FEAT-1 / FEAT-2).
============================================

Чистый stdlib. Ни FastMCP, ни Neo4j — по тем же причинам, по которым в
Заходе 2 родился `graph_state.py`: `server.py` при импорте поднимает FastMCP
и требует `NEO4J_PASSWORD`, поэтому юнит-тестами не покрывается ни в CI, ни
на хосте. Всё, что нужно тестировать, живёт здесь.

Что делает и чего не делает
---------------------------
Делает: раскладывает пакет запросов на структуру, достаточную для проверки
имён — источники с псевдонимами, соединения, временные таблицы, состав
колонок каждой секции ВЫБРАТЬ, все обращения вида `Псевдоним.Поле.Поле`.

Не делает: полный разбор выражений. Внутри ГДЕ, ПО, ИТОГИ и элементов
выборки выражение остаётся плоским потоком токенов, из которого выдёргиваются
цепочки идентификаторов. Для сверки имён этого достаточно, а полноценный
парсер выражений — это ещё столько же кода ради вычислений, которые нам не
нужны.

Основные точки входа:
  tokenize(text)        → list[Token]
  parse_batch(text)     → Batch (пакет запросов: список Statement)
  Statement.temp_table  → имя ВТ из ПОМЕСТИТЬ, если есть
  Statement.columns()   → состав колонок результата (FEAT-2)

Границы разбора выбраны по факту: запрос, который парсер не понял, не
объявляется ошибочным. `Batch.parse_errors` заполняется только там, где
структура сломана однозначно (нет ВЫБРАТЬ, незакрытая скобка). Всё
остальное — молча, чтобы инструмент не врал на конструкциях, которых мы не
предусмотрели. Тот же принцип, что у разбора `Ext/Form.xml` в FIX-4.1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─── Лексер ──────────────────────────────────────────────────────────────

TOK_IDENT = "ident"
TOK_NUMBER = "number"
TOK_STRING = "string"
TOK_PARAM = "param"       # &Параметр
TOK_PUNCT = "punct"

_IDENT_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*", re.UNICODE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_PUNCT_2 = (">=", "<=", "<>")
_PUNCT_1 = set("().,;=<>+-*/")


@dataclass
class Token:
    kind: str
    text: str
    pos: int
    line: int

    @property
    def up(self) -> str:
        return self.text.upper()

    def is_kw(self, *words: str) -> bool:
        return self.kind == TOK_IDENT and self.up in words


def tokenize(text: str) -> list[Token]:
    """Текст запроса → список токенов.

    Съедает комментарии `//` до конца строки и строковые литералы в двойных
    кавычках (удвоенная кавычка внутри — экранированная). Незакрытая кавычка
    не роняет разбор: литерал тянется до конца текста.
    """
    tokens: list[Token] = []
    i, n = 0, len(text)
    line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        # комментарий
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # строковый литерал
        if ch == '"':
            start, start_line = i, line
            i += 1
            while i < n:
                if text[i] == "\n":
                    line += 1
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(Token(TOK_STRING, text[start:i], start, start_line))
            continue
        # параметр
        if ch == "&":
            m = _IDENT_RE.match(text, i + 1)
            end = m.end() if m else i + 1
            tokens.append(Token(TOK_PARAM, text[i:end], i, line))
            i = end
            continue
        m = _IDENT_RE.match(text, i)
        if m:
            tokens.append(Token(TOK_IDENT, m.group(0), i, line))
            i = m.end()
            continue
        m = _NUMBER_RE.match(text, i)
        if m:
            tokens.append(Token(TOK_NUMBER, m.group(0), i, line))
            i = m.end()
            continue
        if text[i:i + 2] in _PUNCT_2:
            tokens.append(Token(TOK_PUNCT, text[i:i + 2], i, line))
            i += 2
            continue
        if ch in _PUNCT_1:
            tokens.append(Token(TOK_PUNCT, ch, i, line))
            i += 1
            continue
        # неизвестный символ — пропускаем, разбор не роняем
        i += 1
    return tokens


# ─── Словарь языка ───────────────────────────────────────────────────────

# Префиксы таблиц в языке запросов. Ключ — как пишут в запросе, значение —
# kind_ru из metadata_xml (совпадает, но список нужен отдельно: в запросе
# встречаются префиксы, которых нет в графе, например Последовательность).
TABLE_PREFIXES = {
    "СПРАВОЧНИК": "Справочник",
    "ДОКУМЕНТ": "Документ",
    "ПЕРЕЧИСЛЕНИЕ": "Перечисление",
    "РЕГИСТРСВЕДЕНИЙ": "РегистрСведений",
    "РЕГИСТРНАКОПЛЕНИЯ": "РегистрНакопления",
    "РЕГИСТРБУХГАЛТЕРИИ": "РегистрБухгалтерии",
    "РЕГИСТРРАСЧЕТА": "РегистрРасчета",
    "ПЛАНСЧЕТОВ": "ПланСчетов",
    "ПЛАНВИДОВХАРАКТЕРИСТИК": "ПланВидовХарактеристик",
    "ПЛАНВИДОВРАСЧЕТА": "ПланВидовРасчета",
    "ПЛАНОБМЕНА": "ПланОбмена",
    "БИЗНЕСПРОЦЕСС": "БизнесПроцесс",
    "ЗАДАЧА": "Задача",
    "КОНСТАНТА": "Константа",
    "ЖУРНАЛДОКУМЕНТОВ": "ЖурналДокументов",
    "ПОСЛЕДОВАТЕЛЬНОСТЬ": "Последовательность",
    "КРИТЕРИЙОТБОРА": "КритерийОтбора",
    "ВНЕШНИЙИСТОЧНИКДАННЫХ": "ВнешнийИсточникДанных",
}

# Ключевые слова, которые не могут быть началом цепочки поля.
KEYWORDS = {
    "ВЫБРАТЬ", "SELECT", "РАЗЛИЧНЫЕ", "DISTINCT", "ПЕРВЫЕ", "TOP",
    "ПОМЕСТИТЬ", "INTO", "ИЗ", "FROM", "КАК", "AS", "ГДЕ", "WHERE",
    "СГРУППИРОВАТЬ", "GROUP", "ПО", "BY", "ИМЕЮЩИЕ", "HAVING",
    "ОБЪЕДИНИТЬ", "UNION", "ВСЕ", "ALL", "УПОРЯДОЧИТЬ", "ORDER",
    "ИТОГИ", "TOTALS", "ОБЩИЕ", "OVERALL", "УНИЧТОЖИТЬ", "DROP",
    "СОЕДИНЕНИЕ", "JOIN", "ЛЕВОЕ", "LEFT", "ПРАВОЕ", "RIGHT",
    "ПОЛНОЕ", "FULL", "ВНУТРЕННЕЕ", "INNER", "ВНЕШНЕЕ", "OUTER",
    "И", "AND", "ИЛИ", "OR", "НЕ", "NOT", "ЕСТЬ", "IS", "NULL",
    "В", "IN", "ИЕРАРХИИ", "HIERARCHY", "МЕЖДУ", "BETWEEN",
    "ПОДОБНО", "LIKE", "СПЕЦСИМВОЛ", "ESCAPE", "ВОЗР", "ASC",
    "УБЫВ", "DESC", "АВТОУПОРЯДОЧИВАНИЕ", "AUTOORDER",
    "ИНДЕКСИРОВАТЬ", "INDEX", "ВЫБОР", "CASE", "КОГДА", "WHEN",
    "ТОГДА", "THEN", "ИНАЧЕ", "ELSE", "КОНЕЦ", "END",
    "ИСТИНА", "TRUE", "ЛОЖЬ", "FALSE", "НЕОПРЕДЕЛЕНО", "UNDEFINED",
    "РАЗРЕШЕННЫЕ", "ALLOWED", "ИЗМЕНЕНИЯ", "ПУСТАЯТАБЛИЦА",
    "ПЕРИОДАМИ", "PERIODS", "ТОЛЬКО", "ONLY", "ССЫЛКА", "REFS",
}

# Функции, содержимое которых не является обращением к полям:
# ЗНАЧЕНИЕ(Перечисление.X.Y), ТИП(Справочник.X) — там имена типов.
_OPAQUE_FUNCS = {"ЗНАЧЕНИЕ", "VALUE", "ТИП", "TYPE"}

# Ключевые слова-секции. Пары — потому что ПО встречается ещё и в условии
# соединения, на том же уровне вложенности, что и секции.
_SECTION_SINGLE = {
    "ИЗ": "from", "FROM": "from",
    "ГДЕ": "where", "WHERE": "where",
    "ИМЕЮЩИЕ": "having", "HAVING": "having",
    "ИТОГИ": "totals", "TOTALS": "totals",
    "АВТОУПОРЯДОЧИВАНИЕ": "autoorder", "AUTOORDER": "autoorder",
}
_SECTION_PAIR = {
    ("СГРУППИРОВАТЬ", "ПО"): "group",
    ("GROUP", "BY"): "group",
    ("УПОРЯДОЧИТЬ", "ПО"): "order",
    ("ORDER", "BY"): "order",
    ("ИНДЕКСИРОВАТЬ", "ПО"): "index",
    ("INDEX", "BY"): "index",
}


# ─── Структуры ───────────────────────────────────────────────────────────

@dataclass
class FieldRef:
    """Обращение к полю: `Т.Номенклатура.Наименование` → path из трёх частей."""
    path: list[str]
    section: str            # select | from | where | group | order | having | totals | join
    line: int

    @property
    def text(self) -> str:
        return ".".join(self.path)


@dataclass
class Source:
    """Источник данных в секции ИЗ."""
    kind: str                       # table | temp | subquery
    parts: list[str] = field(default_factory=list)   # ['Справочник','Номенклатура']
    alias: str = ""
    has_params: bool = False        # за именем шли скобки — параметры ВТ
    param_tokens: list[Token] = field(default_factory=list)
    subquery: "Statement | None" = None
    join_type: str = ""             # "" для первого источника
    line: int = 0

    @property
    def text(self) -> str:
        return ".".join(self.parts)

    @property
    def ref_name(self) -> str:
        """Имя, по которому на источник ссылаются, если псевдонима нет."""
        return self.alias or (self.parts[-1] if self.parts else "")


@dataclass
class SelectItem:
    alias: str
    path: list[str]                 # непустой, если элемент — голая ссылка на поле
    is_star: bool = False
    line: int = 0

    @property
    def column_name(self) -> str:
        if self.alias:
            return self.alias
        if self.path:
            return self.path[-1]
        return ""


@dataclass
class SelectPart:
    """Одна часть ОБЪЕДИНИТЬ."""
    items: list[SelectItem] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    refs: list[FieldRef] = field(default_factory=list)
    has_star: bool = False
    distinct: bool = False
    first_n: int | None = None
    has_where: bool = False
    line: int = 0


@dataclass
class Statement:
    """Запрос пакета: SELECT (возможно с ОБЪЕДИНИТЬ) либо УНИЧТОЖИТЬ."""
    kind: str = "select"            # select | drop | unknown
    parts: list[SelectPart] = field(default_factory=list)
    temp_table: str = ""            # ПОМЕСТИТЬ
    drop_table: str = ""            # УНИЧТОЖИТЬ
    line: int = 0

    @property
    def main(self) -> SelectPart | None:
        return self.parts[0] if self.parts else None

    def columns(self) -> tuple[list[str], bool]:
        """(имена колонок результата, известны ли они полностью) — FEAT-2.

        Состав берётся из первой части ОБЪЕДИНИТЬ: платформа требует, чтобы
        части были согласованы по числу колонок, а имена результата задаёт
        именно первая.
        """
        p = self.main
        if p is None:
            return [], False
        if p.has_star:
            return [], False
        names, known = [], True
        for it in p.items:
            name = it.column_name
            if name:
                names.append(name)
            else:
                known = False       # выражение без КАК — имя колонки не выводится
        return names, known


@dataclass
class Batch:
    statements: list[Statement] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


# ─── Вспомогательное ─────────────────────────────────────────────────────

def _split_top(tokens: list[Token], sep: str) -> list[list[Token]]:
    """Разбить по знаку `sep` на нулевом уровне вложенности скобок."""
    out: list[list[Token]] = []
    cur: list[Token] = []
    depth = 0
    for t in tokens:
        if t.kind == TOK_PUNCT:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
            elif t.text == sep and depth == 0:
                out.append(cur)
                cur = []
                continue
        cur.append(t)
    out.append(cur)
    return out


def _find_top(tokens: list[Token], predicate) -> int:
    """Индекс первого токена нулевого уровня, для которого predicate(i) истинно."""
    depth = 0
    for i, t in enumerate(tokens):
        if t.kind == TOK_PUNCT:
            if t.text == "(":
                depth += 1
                continue
            if t.text == ")":
                depth -= 1
                continue
        if depth == 0 and predicate(i):
            return i
    return -1


def _read_chain(tokens: list[Token], i: int) -> tuple[list[str], int]:
    """Прочитать цепочку `Имя.Имя.Имя` начиная с позиции i."""
    parts = [tokens[i].text]
    j = i + 1
    while (j + 1 < len(tokens)
           and tokens[j].kind == TOK_PUNCT and tokens[j].text == "."
           and tokens[j + 1].kind == TOK_IDENT):
        parts.append(tokens[j + 1].text)
        j += 2
    return parts, j


def extract_refs(tokens: list[Token], section: str) -> list[FieldRef]:
    """Выдернуть из потока токенов обращения к полям.

    Пропускаем: ключевые слова; имя функции (идентификатор перед `(`);
    содержимое ЗНАЧЕНИЕ()/ТИП() целиком — там имена типов, а не полей;
    цепочку сразу после КАК — это либо псевдоним, либо тип в ВЫРАЗИТЬ().
    """
    refs: list[FieldRef] = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind != TOK_IDENT:
            i += 1
            continue
        up = t.up
        # после КАК идёт псевдоним или имя типа — не поле
        if up in ("КАК", "AS"):
            i += 1
            if i < n and tokens[i].kind == TOK_IDENT:
                _, i = _read_chain(tokens, i)
            continue
        # ЗНАЧЕНИЕ(...) / ТИП(...) — пропустить скобки целиком
        if up in _OPAQUE_FUNCS and i + 1 < n and tokens[i + 1].text == "(":
            depth = 0
            j = i + 1
            while j < n:
                if tokens[j].text == "(":
                    depth += 1
                elif tokens[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        if up in KEYWORDS:
            i += 1
            continue
        # имя функции: идентификатор, за которым сразу скобка
        if i + 1 < n and tokens[i + 1].kind == TOK_PUNCT and tokens[i + 1].text == "(":
            i += 1
            continue
        parts, j = _read_chain(tokens, i)
        refs.append(FieldRef(parts, section, t.line))
        i = j
    return refs


# ─── Разбор источников (секция ИЗ) ───────────────────────────────────────

_JOIN_WORDS = {"ЛЕВОЕ", "LEFT", "ПРАВОЕ", "RIGHT", "ПОЛНОЕ", "FULL",
               "ВНУТРЕННЕЕ", "INNER", "СОЕДИНЕНИЕ", "JOIN"}


def _parse_from(tokens: list[Token]) -> tuple[list[Source], list[FieldRef]]:
    """Секция ИЗ → список источников и обращения к полям из условий ПО."""
    sources: list[Source] = []
    refs: list[FieldRef] = []

    # Соединения и запятые режем на нулевом уровне.
    for chunk in _split_top(tokens, ","):
        pos = 0
        pending_join = ""
        while pos < len(chunk):
            t = chunk[pos]
            if t.kind == TOK_IDENT and t.up in _JOIN_WORDS:
                words = []
                while (pos < len(chunk) and chunk[pos].kind == TOK_IDENT
                       and (chunk[pos].up in _JOIN_WORDS
                            or chunk[pos].up in ("ВНЕШНЕЕ", "OUTER"))):
                    words.append(chunk[pos].up)
                    pos += 1
                pending_join = " ".join(words)
                continue
            if t.kind == TOK_IDENT and t.up in ("ПО", "BY"):
                # условие соединения — до следующего слова соединения
                depth = 0
                j = pos + 1
                while j < len(chunk):
                    tk = chunk[j]
                    if tk.kind == TOK_PUNCT and tk.text == "(":
                        depth += 1
                    elif tk.kind == TOK_PUNCT and tk.text == ")":
                        depth -= 1
                    elif (depth == 0 and tk.kind == TOK_IDENT
                          and tk.up in _JOIN_WORDS):
                        break
                    j += 1
                refs.extend(extract_refs(chunk[pos + 1:j], "join"))
                pos = j
                continue
            src, pos = _parse_source(chunk, pos)
            if src is None:
                pos += 1
                continue
            src.join_type = pending_join
            pending_join = ""
            sources.append(src)
    return sources, refs


def _parse_source(chunk: list[Token], pos: int) -> tuple[Source | None, int]:
    t = chunk[pos]
    if t.kind == TOK_PUNCT and t.text == "(":
        depth, j = 0, pos
        while j < len(chunk):
            if chunk[j].text == "(":
                depth += 1
            elif chunk[j].text == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = chunk[pos + 1:j]
        src = Source(kind="subquery", line=t.line)
        sub = _parse_statement(inner)
        src.subquery = sub
        pos = j + 1
    elif t.kind == TOK_IDENT:
        parts, j = _read_chain(chunk, pos)
        src = Source(kind="table", parts=parts, line=t.line)
        pos = j
        if pos < len(chunk) and chunk[pos].kind == TOK_PUNCT and chunk[pos].text == "(":
            depth, k = 0, pos
            while k < len(chunk):
                if chunk[k].text == "(":
                    depth += 1
                elif chunk[k].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            src.has_params = True
            src.param_tokens = chunk[pos + 1:k]
            pos = k + 1
        if len(parts) == 1:
            src.kind = "temp"
    else:
        return None, pos
    # псевдоним
    if (pos < len(chunk) and chunk[pos].kind == TOK_IDENT
            and chunk[pos].up in ("КАК", "AS")
            and pos + 1 < len(chunk) and chunk[pos + 1].kind == TOK_IDENT):
        src.alias = chunk[pos + 1].text
        pos += 2
    elif (pos < len(chunk) and chunk[pos].kind == TOK_IDENT
          and chunk[pos].up not in KEYWORDS):
        # псевдоним без слова КАК платформа не разрешает, но встречается
        src.alias = chunk[pos].text
        pos += 1
    return src, pos


# ─── Разбор запроса ──────────────────────────────────────────────────────

def _sections(tokens: list[Token]) -> list[tuple[str, list[Token]]]:
    """Разрезать запрос на секции по ключевым словам нулевого уровня."""
    marks: list[tuple[int, str, int]] = []   # (позиция, имя, длина заголовка)
    depth = 0
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == TOK_PUNCT:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
            i += 1
            continue
        if depth == 0 and t.kind == TOK_IDENT:
            nxt = tokens[i + 1].up if i + 1 < len(tokens) else ""
            pair = _SECTION_PAIR.get((t.up, nxt))
            if pair:
                marks.append((i, pair, 2))
                i += 2
                continue
            single = _SECTION_SINGLE.get(t.up)
            if single:
                marks.append((i, single, 1))
                i += 1
                continue
            if t.up in ("ВЫБРАТЬ", "SELECT"):
                marks.append((i, "select", 1))
                i += 1
                continue
        i += 1
    out: list[tuple[str, list[Token]]] = []
    for idx, (start, name, hdr) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(tokens)
        out.append((name, tokens[start + hdr:end]))
    return out


def _split_unions(tokens: list[Token]) -> list[list[Token]]:
    """Разрезать по ОБЪЕДИНИТЬ [ВСЕ] нулевого уровня."""
    parts: list[list[Token]] = []
    cur: list[Token] = []
    depth = 0
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == TOK_PUNCT:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
        if depth == 0 and t.kind == TOK_IDENT and t.up in ("ОБЪЕДИНИТЬ", "UNION"):
            parts.append(cur)
            cur = []
            i += 1
            if i < len(tokens) and tokens[i].kind == TOK_IDENT and tokens[i].up in ("ВСЕ", "ALL"):
                i += 1
            continue
        cur.append(t)
        i += 1
    parts.append(cur)
    return parts


def _parse_select_body(tokens: list[Token]):
    """Тело секции ВЫБРАТЬ.

    → (элементы, есть *, РАЗЛИЧНЫЕ, ПЕРВЫЕ N, ПОМЕСТИТЬ, токены списка полей)

    Последним возвращается тело без служебных слов и без `ПОМЕСТИТЬ <Имя>`:
    имя временной таблицы — не обращение к полю, и если отдать его в
    extract_refs, проверка полей начнёт ругаться на несуществующий псевдоним.
    """
    distinct = False
    first_n: int | None = None
    temp = ""
    i = 0
    while i < len(tokens) and tokens[i].kind == TOK_IDENT:
        up = tokens[i].up
        if up in ("РАЗЛИЧНЫЕ", "DISTINCT"):
            distinct = True
            i += 1
        elif up in ("ПЕРВЫЕ", "TOP"):
            i += 1
            if i < len(tokens) and tokens[i].kind == TOK_NUMBER:
                first_n = int(float(tokens[i].text))
                i += 1
        elif up in ("РАЗРЕШЕННЫЕ", "ALLOWED"):
            i += 1
        else:
            break
    body = tokens[i:]
    # ПОМЕСТИТЬ <Имя> — в конце списка полей
    p = _find_top(body, lambda k: body[k].kind == TOK_IDENT
                  and body[k].up in ("ПОМЕСТИТЬ", "INTO"))
    if p >= 0:
        if p + 1 < len(body) and body[p + 1].kind == TOK_IDENT:
            temp = body[p + 1].text
        body = body[:p]

    items: list[SelectItem] = []
    has_star = False
    for chunk in _split_top(body, ","):
        chunk = [t for t in chunk]
        if not chunk:
            continue
        if len(chunk) == 1 and chunk[0].kind == TOK_PUNCT and chunk[0].text == "*":
            has_star = True
            items.append(SelectItem(alias="", path=[], is_star=True, line=chunk[0].line))
            continue
        if any(t.kind == TOK_PUNCT and t.text == "*" for t in chunk) and \
           not any(t.kind == TOK_IDENT and t.up not in KEYWORDS for t in chunk):
            has_star = True
            continue
        alias = ""
        a = _find_top(chunk, lambda k: chunk[k].kind == TOK_IDENT
                      and chunk[k].up in ("КАК", "AS"))
        if a >= 0 and a + 1 < len(chunk) and chunk[a + 1].kind == TOK_IDENT:
            alias = chunk[a + 1].text
            expr = chunk[:a]
        else:
            expr = chunk
        path: list[str] = []
        if expr and expr[0].kind == TOK_IDENT and expr[0].up not in KEYWORDS:
            candidate, j = _read_chain(expr, 0)
            if j == len(expr):
                path = candidate
        items.append(SelectItem(alias=alias, path=path, line=chunk[0].line))
    return items, has_star, distinct, first_n, temp, body


def _parse_part(tokens: list[Token]) -> tuple[SelectPart, str]:
    part = SelectPart(line=tokens[0].line if tokens else 0)
    temp = ""
    for name, body in _sections(tokens):
        if name == "select":
            items, star, distinct, first_n, t, field_tokens = _parse_select_body(body)
            part.items = items
            part.has_star = star
            part.distinct = distinct
            part.first_n = first_n
            if t:
                temp = t
            part.refs.extend(extract_refs(field_tokens, "select"))
        elif name == "from":
            sources, join_refs = _parse_from(body)
            part.sources = sources
            part.refs.extend(join_refs)
        elif name == "where":
            part.has_where = True
            part.refs.extend(extract_refs(body, "where"))
        elif name in ("group", "order", "having", "totals", "index"):
            part.refs.extend(extract_refs(body, name))
    return part, temp


def _parse_statement(tokens: list[Token]) -> Statement:
    if not tokens:
        return Statement(kind="unknown")
    first = tokens[0]
    if first.kind == TOK_IDENT and first.up in ("УНИЧТОЖИТЬ", "DROP"):
        name = tokens[1].text if len(tokens) > 1 and tokens[1].kind == TOK_IDENT else ""
        return Statement(kind="drop", drop_table=name, line=first.line)
    st = Statement(kind="select", line=first.line)
    if not (first.kind == TOK_IDENT and first.up in ("ВЫБРАТЬ", "SELECT")):
        st.kind = "unknown"
        return st
    for chunk in _split_unions(tokens):
        if not chunk:
            continue
        part, temp = _parse_part(chunk)
        if temp:
            st.temp_table = temp
        st.parts.append(part)
    return st


def parse_batch(text: str) -> Batch:
    """Текст (возможно пакет через `;`) → Batch."""
    batch = Batch()
    tokens = tokenize(text)
    if not tokens:
        return batch
    depth = 0
    for t in tokens:
        if t.kind == TOK_PUNCT:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
    if depth != 0:
        batch.parse_errors.append(
            "Скобки не сбалансированы — структура запроса разобрана частично."
        )
    for chunk in _split_top(tokens, ";"):
        chunk = [t for t in chunk]
        if not chunk:
            continue
        st = _parse_statement(chunk)
        if st.kind == "unknown":
            batch.parse_errors.append(
                f"Строка {chunk[0].line}: запрос не начинается с ВЫБРАТЬ или "
                f"УНИЧТОЖИТЬ — разобрать не удалось."
            )
            continue
        batch.statements.append(st)
    return batch
