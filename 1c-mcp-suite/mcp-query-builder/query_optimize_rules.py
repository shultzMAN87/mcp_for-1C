"""
Правила оптимизации запроса — на разобранной структуре, а не на регулярках.
===========================================================================

QOPT-1 и заодно причина, по которой правила переехали сюда из `server.py`.

Прежние правила читали текст запроса регулярками, и это дало два ложных
срабатывания подряд: FIX-2 (короткая альтернатива «Остатки» матчилась внутри
«ОстаткиИОбороты») и QOPT-1 (правило «отсутствие ограничения выборки»
срабатывало на виртуальной таблице с параметрами, потому что видело только
наличие слов ГДЕ и ПЕРВЫЕ). Оба — один класс ошибки: инструмент врал ровно
на том запросе, ради которого существует.

Теперь источники, их параметры, наличие ГДЕ и ПЕРВЫЕ берутся у парсера, где
это факт структуры, а не совпадение подстроки. Регуляркой осталось одно
правило — функции в условиях: разбор выражений парсер не делает намеренно,
а эвристика здесь дешёвая и ложных срабатываний за собой не тянет.
"""
from __future__ import annotations

import re

from query_parser import SelectPart, Source, parse_batch


def _is_register_main_table(src: Source) -> bool:
    """Обращение к основной таблице регистра вместо виртуальной."""
    return (src.kind == "table" and len(src.parts) == 2
            and src.parts[0].upper().startswith("РЕГИСТР"))


def _is_virtual_table(src: Source) -> bool:
    return src.kind == "table" and len(src.parts) == 3 and \
        src.parts[0].upper().startswith(("РЕГИСТР", "ПЛАНВИДОВРАСЧЕТА"))


def _is_bounded(src: Source) -> bool:
    """Источник, который сам по себе ограничивает выборку.

    Виртуальная таблица с параметрами — ограничена: отбор ушёл внутрь и
    отработает до чтения. Временная таблица ограничена запросом, который её
    наполнил. Подзапрос разбирается своими правилами.
    """
    if src.kind in ("temp", "subquery"):
        return True
    return src.has_params


def _unbounded_sources(part: SelectPart) -> list[Source]:
    if part.first_n is not None or part.has_where:
        return []
    return [s for s in part.sources if not _is_bounded(s)]


def analyze(query_text: str) -> list[dict]:
    """Текст запроса → список рекомендаций. Пустой список = замечаний нет."""
    recs: list[dict] = []
    seen: set[str] = set()

    def add(priority, rule, issue, fix):
        if rule in seen:
            return
        seen.add(rule)
        recs.append({"priority": priority, "rule": rule, "issue": issue, "fix": fix})

    batch = parse_batch(query_text)
    parts: list[SelectPart] = [p for st in batch.statements for p in st.parts]
    sources: list[Source] = [s for p in parts for s in p.sources]

    for src in sources:
        if _is_register_main_table(src):
            add("HIGH", "Виртуальные таблицы",
                f"Запрос к основной таблице регистра ({src.text}) вместо виртуальной",
                "Используйте .Остатки(), .Обороты() или .ОстаткиИОбороты() — "
                "они считаются по итогам и оптимизированы платформой")
            break

    for src in sources:
        if _is_virtual_table(src) and not src.has_params:
            add("HIGH", "Параметры виртуальных таблиц",
                f"Виртуальная таблица {src.text} без параметров — "
                f"условия в ГДЕ применяются уже после её расчёта",
                "Перенесите условия отбора в параметры виртуальной таблицы: "
                ".Остатки(&Период, Склад = &Склад)")
            break

    for src in sources:
        if src.kind == "subquery" and src.join_type:
            add("MEDIUM", "Соединение с подзапросом",
                "Соединение с подзапросом — платформа не может использовать "
                "индексы по его результату",
                "Вынесите подзапрос во временную таблицу (ПОМЕСТИТЬ ВтИмя) "
                "и соединяйтесь с ней, проиндексировав поля соединения")
            break

    for part in parts:
        unbounded = _unbounded_sources(part)
        if unbounded:
            names = ", ".join(s.text or "подзапрос" for s in unbounded)
            add("MEDIUM", "Отсутствие ограничения выборки",
                f"Выборка без ПЕРВЫЕ и без условий ГДЕ из: {names}",
                "Добавьте ПЕРВЫЕ N, условие ГДЕ или параметры виртуальной "
                "таблицы, чтобы ограничить объём читаемых данных")
            break

    for part in parts:
        if part.has_star:
            add("MEDIUM", "Выборка всех полей",
                "ВЫБРАТЬ * читает все поля, включая неиспользуемые",
                "Перечислите нужные поля явно — это уменьшает объём чтения "
                "и позволяет платформе выбрать покрывающий индекс")
            break

    for part in parts:
        if part.distinct:
            add("LOW", "РАЗЛИЧНЫЕ (DISTINCT)",
                "РАЗЛИЧНЫЕ требует сортировки всей выборки",
                "Если дубликаты появились из-за соединения — исправьте "
                "соединение, а не прячьте следствие")
            break

    if re.search(r'ГДЕ.*?(?:ПОДСТРОКА|ВЫРАЗИТЬ|ГОД|МЕСЯЦ|ДЕНЬ|НАЧАЛОПЕРИОДА|'
                 r'КОНЕЦПЕРИОДА|SUBSTRING|CAST)\s*\(',
                 query_text, re.IGNORECASE | re.DOTALL):
        add("HIGH", "Функции в условиях",
            "Функция от поля в условии отбора — индекс по этому полю не "
            "используется",
            "Вычислите значение заранее и передайте параметром, либо "
            "сравнивайте само поле с готовой границей")

    if re.search(r'УПОРЯДОЧИТЬ\s+ПО|ORDER\s+BY', query_text, re.IGNORECASE):
        add("LOW", "Сортировка",
            "УПОРЯДОЧИТЬ ПО сортирует всю выборку целиком",
            "Убедитесь, что поля сортировки входят в индекс, или сортируйте "
            "уже ограниченную выборку")

    return recs
