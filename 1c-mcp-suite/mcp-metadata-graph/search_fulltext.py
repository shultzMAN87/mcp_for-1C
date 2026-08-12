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


# PERF-6b. Тай-брейк при равном score.
#
# Почему он вообще нужен. Префиксный запрос Lucene (`заказ*`) —
# КОНСТАНТНЫЙ по весу: все совпадения получают одинаковый score. А имена в
# 1С — CamelCase, то есть «ЗаказПокупателя» это ОДИН токен, и точное
# совпадение слова (`заказ^2`) не срабатывает никогда. Значит на типичном
# запросе ранжирования по score просто нет: всё упирается в тай-брейк.
#
# Что было. Тай-брейком стояло `n.full_name_eng`, то есть алфавит по
# английскому имени вида. `CommonPicture` < `DataProcessor` < `Document` —
# и на запрос «заказ» первыми приходили две картинки-иконки, а документы
# уходили на вторую страницу. Ровно та бесполезная выдача, от которой
# PERF-6 должен был избавить.
#
# Три уровня, в порядке убывания надёжности признака:
#
#   1. Имя НАЧИНАЕТСЯ с искомого слова. Самый сильный сигнал:
#      «ЗаказТоваров» релевантнее «ОбработкаИнтернетЗаказовКладовщиком»,
#      хотя Lucene дал им одинаковый вес.
#   2. Вид объекта. Справочники, документы и регистры — то, о чём
#      спрашивают; картинки, роли и элементы стиля — почти никогда.
#      Это не сокрытие: они остаются в выдаче, просто ниже.
#   3. Алфавит — чтобы порядок был воспроизводимым между вызовами.
#      Без него пагинация может задваивать и терять строки на границах
#      страниц: Neo4j не обязан сохранять порядок при равных ключах.

# Веса видов. Меньше — выше в выдаче.
KIND_RANK = {
    1: ("Catalog", "Document", "InformationRegister", "AccumulationRegister",
        "AccountingRegister", "ChartOfAccounts", "ChartOfCharacteristicTypes",
        "ChartOfCalculationTypes", "Enum", "Constant", "ExchangePlan",
        "BusinessProcess", "Task", "DocumentJournal", "Sequence",
        "CalculationRegister", "ExternalDataSource"),
    2: ("CommonModule", "DataProcessor", "Report"),
    3: ("Subsystem", "EventSubscription", "ScheduledJob", "DefinedType",
        "FunctionalOption", "SessionParameter", "FilterCriterion",
        "HTTPService", "WebService", "WSReference"),
    # Всё остальное (Role, CommonPicture, StyleItem, Style, CommonTemplate,
    # XDTOPackage, CommandGroup, CommonCommand, CommonForm, Language,
    # SettingsStorage, DocumentNumerator) получает 4 по умолчанию.
}


def kind_rank_cypher(alias: str = "n") -> str:
    """CASE-выражение веса вида для ORDER BY."""
    parts = []
    for rank, kinds in sorted(KIND_RANK.items()):
        lst = ", ".join(f"'{k}'" for k in kinds)
        parts.append(f"WHEN {alias}.kind_eng IN [{lst}] THEN {rank}")
    return "CASE " + " ".join(parts) + " ELSE 4 END"


def order_by_relevance(alias: str = "n", param: str = "q",
                       with_score: bool = True) -> str:
    """
    Секция ORDER BY, одинаковая для полнотекстового и CONTAINS-путей.

    Одинаковая намеренно: если пути сортируют по-разному, выдача меняется
    при переключении на запасной путь — и объяснить это пользователю будет
    нечем.
    """
    head = "score DESC, " if with_score else ""
    return (
        f"ORDER BY {head}"
        f"CASE WHEN toLower({alias}.name) STARTS WITH toLower(${param}) "
        f"THEN 0 ELSE 1 END, "
        f"{kind_rank_cypher(alias)}, "
        f"{alias}.name, {alias}.full_name_eng"
    )


# Запрос к индексу. `skip`/`limit` — постранично, как в CONTAINS-варианте.
#
# FIX-17. Здесь возвращались `n.full_name` и `n.kind` — свойств с такими
# именами у узлов метаданных НЕТ. Writer пишет `full_name_eng`,
# `full_name_ru`, `kind_eng`, `kind_ru` (см. write_meta_nodes). Cypher на
# несуществующее свойство отдаёт null, а не ошибку, — поэтому поиск исправно
# возвращал строки, у которых вид и полное имя всегда пустые. Тот же класс,
# что FIX-14 и FIX-16: запрос разошёлся с тем, что реально лежит в графе.
FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('meta_fulltext', $ftq) YIELD node AS n, score
{where}
RETURN n.full_name_eng AS full_name, n.kind_ru AS kind, n.name AS name,
       n.synonym AS synonym, score
{order_by}
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
        # FIX-17: kind_ru, а не kind. С прежним условием фильтр по русскому
        # имени вида («Справочник») не срабатывал никогда — сравнение шло с
        # null, — и работал только английский.
        clauses.append("(toLower(n.kind_ru) = toLower($kind) "
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
