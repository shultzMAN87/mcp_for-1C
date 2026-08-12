"""
PERF-6 — полнотекстовый поиск по метаданным.
=============================================

Что было. `metadata_search` искал через
`toLower(n.name) CONTAINS toLower($q)`. Подстрочный поиск не использует
обычный индекс, но на 16 тысячах узлов это терпимо по скорости. Настоящая
проблема — качество: `CONTAINS` не ранжирует. Подстрока либо есть, либо
нет, и на слово вроде «заказ» приходят сотни совпадений в произвольном
порядке (сортировка по имени — не релевантность).

Что стало. Полнотекстовый индекс Neo4j даёт score и токенизацию. `CONTAINS`
остаётся запасным путём и включается сам, когда индекса нет: граф мог быть
собран старым индексером, а у пользователя может не оказаться нужной
процедуры. Поиск обязан работать в обоих случаях — молчаливая деградация
здесь уместнее отказа.

Модуль отдельный, потому что подготовка запроса — чистая функция без Neo4j
и без fastmcp, и её можно проверить офлайн. Сам `server.py` офлайн не
импортируется.
"""
from __future__ import annotations

import re

# Символы, значимые для парсера запросов Lucene. Экранируем все: строка
# приходит от пользователя как обычный текст, а не как выражение Lucene.
# Без экранирования запрос вида «Контрагенты (ЕГРЮЛ)» или «счёт-фактура»
# уронил бы поиск синтаксической ошибкой — на пустом месте.
_LUCENE_SPECIAL = r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)'


def escape_lucene(text: str) -> str:
    """Экранирует спецсимволы Lucene в пользовательской строке."""
    return re.sub(_LUCENE_SPECIAL, r"\\\1", text or "")


def build_fulltext_query(query: str, fuzzy: bool = True) -> str:
    """
    Собирает выражение Lucene из пользовательской строки.

    Логика:
      • каждое слово ищется как префикс (`контраг*`) — иначе поиск по
        началу слова, самый частый способ искать объект, не работал бы:
        полнотекстовый индекс совпадает по целым токенам;
      • при `fuzzy` добавляется вариант с одной опечаткой (`контрагент~1`),
        но с меньшим весом — иначе опечаточные совпадения перебивали бы
        точные;
      • слова соединяются через OR, а не AND: «заказ клиента» должно
        находить и «ЗаказКлиента», и «ЗаказПоставщику», отдав первому
        больший score. AND отсёк бы половину полезного.

    Возвращает пустую строку, если искать нечего, — вызывающий код по ней
    понимает, что в индекс идти незачем.
    """
    words = [w for w in re.split(r"[\s,;]+", (query or "").strip()) if w]
    if not words:
        return ""

    parts = []
    for w in words:
        esc = escape_lucene(w)
        if not esc:
            continue
        parts.append(f"{esc}*")
        # Точное совпадение слова весит больше префиксного.
        parts.append(f"{esc}^2")
        if fuzzy and len(esc) >= 4:
            parts.append(f"{esc}~1^0.5")
    return " OR ".join(parts)


# Запрос к индексу. `skip`/`limit` — постранично, как в CONTAINS-варианте.
FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('meta_fulltext', $ftq) YIELD node AS n, score
{where}
RETURN n.full_name AS full_name, n.kind AS kind, n.name AS name,
       n.synonym AS synonym, score
ORDER BY score DESC, n.full_name
SKIP $offset LIMIT $limit
"""

FULLTEXT_COUNT_CYPHER = """
CALL db.index.fulltext.queryNodes('meta_fulltext', $ftq) YIELD node AS n
{where}
RETURN count(n) AS total
"""


def fulltext_where(kind: str = "", exclude_modules: bool = True,
                   scope: str = "") -> str:
    """
    Секция WHERE для полнотекстового запроса.

    `exclude_modules` — узлы модулей объекта и менеджера тоже несут метку
    :MetadataObject (их 5 843), но объектами метаданных в смысле поиска не
    являются: их `name` это «ObjectModule» / «ManagerModule». В выдаче на
    запрос «module» они дали бы шум, а пользы не дают — для кода есть
    отдельные code-инструменты.
    """
    clauses = []
    if exclude_modules:
        clauses.append("NOT n:Module")
    if kind:
        clauses.append("(toLower(n.kind) = toLower($kind) "
                       "OR toLower(n.kind_eng) = toLower($kind))")
    if scope:
        # SCALE-1: границы подсистемы. Импорт локальный — модуль поиска не
        # должен зависеть от модуля областей на уровне загрузки, они
        # используются и по отдельности.
        from subsystem_scope import subsystem_scope_cypher
        clauses.append(subsystem_scope_cypher("n"))
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def is_missing_index_error(err: object) -> bool:
    """
    Отличает «нет полнотекстового индекса» от любой другой ошибки.

    Важно не глотать всё подряд: если Neo4j лёг или запрос синтаксически
    неверен, поиск должен сказать об этом, а не тихо уйти на запасной путь
    и молча вернуть результат похуже.
    """
    text = str(err).lower()
    markers = (
        "no such fulltext index",
        "there is no such fulltext schema index",
        "unknown function",
        "unknown procedure",
        "db.index.fulltext.querynodes",
        "procedurenotfound",
        "indexnotfound",
    )
    return any(m in text for m in markers)
