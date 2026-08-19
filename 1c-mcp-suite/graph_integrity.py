"""
FIX-27. Целостность графа: у кода есть владелец или его нет.
=============================================================

Что случилось
─────────────
18 августа `metadata_stats` показал `modules: 0` при 231 129 процедурах.
Проверка на стенде: узлов `:Module` — ноль, рёбер `HAS_METHOD` — ни одного.
Было 14 041 модуль и 231 085 рёбер. Слой владения кодом пропал из графа
несколькими днями раньше, и об этом не сообщил никто.

Вопрос «какие процедуры у этого объекта» перестал отвечать. При этом
`code_callers` и `code_callees` работали — они ходят по `CALLS`, — поэтому
датасет `metadata_graph` всё это время оставался зелёным. Ни одна проверка
не смотрела на владение.

Почему сторож `FIX-15` не помог
───────────────────────────────
`FIX-15` сторожит ЗАПИСЬ: «отправлено N, записано M, разница вслух». Здесь
записи не было вовсе — было удаление чужого. Фаза 1 (XML) сносит
`:MetadataObject`-узлы, среди которых и модули, а фаза 2, которая их
пересоздаёт, не запустилась. Каждый шаг отчитался честно: первый удалил,
что собирался, второй не работал и потому промолчал.

Отсюда правило этого модуля: **проверять надо не шаг, а результат**. Не
«сколько записал писатель», а «сколько процедур в графе осталось без
владельца прямо сейчас».

Как считается — и почему это бесплатно
──────────────────────────────────────
Ровно три числа, каждое берётся из счётчиков хранилища Neo4j за O(1):

    count(:Callable)        — сколько процедур и функций в графе
    count(:Module)          — сколько узлов-модулей
    count(()-[:HAS_METHOD]->())  — сколько связей «модуль → его метод»

Ключевая тонкость про третье: счётчик по КОНКРЕТНОМУ типу ребра берётся из
хранилища, а не обходом. Дорог только запрос без указания типа
(`MATCH ()-[r]->()`) — тот самый, из-за которого затевался `PERF-12`.

У процедуры владелец ровно один (`module_id` у `:Callable` единственный), поэтому
арифметика простая и точная:

    без владельца = процедуры − рёбра HAS_METHOD

Ни одного дополнительного обхода. Проверка стоит столько же, сколько
раньше стоило её отсутствие.

Где применяется
───────────────
  • `metadata_stats` — блок `code.ownership` с полем `answerable`;
  • `indexer.py` — строка в лог в конце прогона и, главное, ПЕРЕД ранним
    выходом «оба fingerprint совпали»: именно там разрушенный граф
    объявлялся актуальным шесть дней подряд;
  • `apply_changes.py` — после точечного обновления.

Зависимостей нет, кроме стандартной библиотеки: модуль кладётся в образ
рядом с `shortfall.py` и проверяется тестами без Neo4j.
"""

from __future__ import annotations

import logging
from typing import Optional

__all__ = [
    "HAS_METHOD_COUNT_CYPHER",
    "STATE_OK", "STATE_PARTIAL", "STATE_BROKEN", "STATE_NO_CODE_LAYER",
    "STATE_EMPTY", "ORPHAN_ALARM_PCT",
    "ownership_report", "ownership_line", "log_ownership",
]

_log = logging.getLogger("graph_integrity")

# Счётчик по КОНКРЕТНОМУ типу ребра — O(1) из счётчиков хранилища.
# Метка у обеих сторон не указана намеренно: тип ребра здесь и есть ключ
# счётчика, а `MATCH (a:Module)-[r:HAS_METHOD]->(b:Callable)` заставил бы
# Neo4j проверять метки и превратил бы O(1) в обход.
HAS_METHOD_COUNT_CYPHER = "MATCH ()-[r:HAS_METHOD]->() RETURN count(r) AS c"

STATE_OK = "ok"                        # владение на месте
STATE_PARTIAL = "partial"              # часть процедур осиротела
STATE_BROKEN = "broken"                # владения нет вовсе — это FIX-27
STATE_NO_CODE_LAYER = "no_code_layer"  # слой кода не построен
STATE_EMPTY = "empty"                  # граф пуст целиком

# Доля процедур без владельца, выше которой ответ считается ухудшенным.
#
# Число не с потолка. Штатный остаток на боевой конфигурации — 28 процедур
# из 231 114, то есть 0,012 %: это шесть модулей форм перечислений, которых
# нет в слое 1 (известное ограничение, разбор в ИТОГИ-ЗАХОДА-4). Порог в
# один процент — восемьдесят таких остатков подряд. Ниже него шум, выше —
# событие.
ORPHAN_ALARM_PCT = 1.0

MEANING_BROKEN = (
    "Это НЕ значит, что у объектов нет процедур. Связь «модуль → его "
    "методы» отсутствует в графе, поэтому на вопросы про состав методов "
    "объекта или модуля опираться нельзя — отвечай, что данных нет, и не "
    "делай вывод об их отсутствии в конфигурации. Вызовы (code_callers / "
    "code_callees) при этом достоверны: они идут по другим рёбрам."
)

MEANING_NO_CODE = (
    "Это НЕ значит, что в конфигурации нет кода. Слой кода в графе не "
    "построен: в нём нет ни одной процедуры. Вопросы про код сейчас "
    "неотвечаемы целиком."
)

MEANING_PARTIAL = (
    "Ответ пригоден, но неполон: часть процедур в графе не привязана к "
    "своему модулю. Если состав методов конкретного объекта выглядит "
    "пустым, это может быть следствием, а не фактом."
)

HINT_REINDEX = (
    "Починка: METADATA_FORCE_BSL=true и обычный старт metadata-indexer — "
    "фаза 2 пересоберёт слой кода и связи владения."
)


def ownership_report(
    callables: int,
    modules: int,
    has_method: int,
    objects: Optional[int] = None,
) -> dict:
    """
    Приговор о владении кодом по трём счётчикам.

    `objects` — число `:MetadataObject`; нужно, только чтобы отличить
    «граф пуст» от «слой кода не построен». Без него оба случая сливаются
    в один, и пустой граф выглядел бы поломкой.

    Возвращает словарь, годный для укладки в ответ инструмента как есть.
    Поле `answerable` отвечает на вопрос «можно ли опереться на ответ про
    состав методов», а НЕ на вопрос «жив ли сервер»: остальные разделы
    статистики при разрушенном владении по-прежнему верны.
    """
    callables = max(0, int(callables or 0))
    modules = max(0, int(modules or 0))
    has_method = max(0, int(has_method or 0))
    objects = None if objects is None else max(0, int(objects))

    without_owner = max(0, callables - has_method)
    with_owner = callables - without_owner
    orphan_pct = round(100.0 * without_owner / callables, 3) if callables else 0.0

    if callables == 0:
        if not objects:
            state = STATE_EMPTY
            message = "граф пуст: ни объектов метаданных, ни кода"
            meaning = MEANING_NO_CODE
        else:
            state = STATE_NO_CODE_LAYER
            message = (f"слой кода не построен: объектов {objects}, "
                       f"процедур 0")
            meaning = MEANING_NO_CODE
        answerable, degraded, hint = False, True, HINT_REINDEX
    elif modules == 0 or has_method == 0:
        state = STATE_BROKEN
        message = (f"связь «модуль → метод» отсутствует: процедур "
                   f"{callables}, узлов-модулей {modules}, рёбер HAS_METHOD "
                   f"{has_method}")
        meaning, answerable, degraded, hint = MEANING_BROKEN, False, True, HINT_REINDEX
    elif orphan_pct >= ORPHAN_ALARM_PCT:
        state = STATE_PARTIAL
        message = (f"без владельца {without_owner} процедур из {callables} "
                   f"({orphan_pct} %)")
        meaning, answerable, degraded, hint = MEANING_PARTIAL, True, True, HINT_REINDEX
    else:
        state = STATE_OK
        message = (f"владение на месте: {with_owner} процедур из {callables} "
                   f"привязаны к модулю")
        meaning, answerable, degraded, hint = "", True, False, ""

    report = {
        "state": state,
        # `objects` кладётся в отчёт, а не остаётся аргументом: по нему
        # вызывающий решает, чем чинить — одной фазой 2 или полным
        # прогоном. Пока это число знал только вызывающий, решение
        # опиралось на ключ, которого в отчёте нет, и на пустом графе
        # молча выбирало не ту починку (поймано tests_indexer_phases).
        "objects": objects if objects is not None else 0,
        "callables": callables,
        "modules": modules,
        "has_method": has_method,
        "with_owner": with_owner,
        "without_owner": without_owner,
        "orphan_pct": orphan_pct,
        "answerable": answerable,
        "degraded": degraded,
        "message": message,
    }
    # `meaning` и `hint` кладём только когда есть что сказать: пустая строка
    # в норме — это шум, который читает модель, и на который она тратит
    # внимание каждый вызов.
    if meaning:
        report["meaning"] = meaning
    if hint:
        report["hint"] = hint
    return report


def ownership_line(report: dict) -> str:
    """Одна строка для лога. Числа те же, что в ответе инструмента."""
    mark = "✓" if report["state"] == STATE_OK else "⚠"
    return (f"{mark} владение кодом: процедур {report['callables']}, "
            f"без владельца {report['without_owner']} "
            f"({report['orphan_pct']} %), узлов-модулей {report['modules']}")


def log_ownership(report: dict, log: Optional[logging.Logger] = None) -> bool:
    """
    Печатает приговор. Возвращает True, если владение в порядке.

    Главная строка идёт в INFO ВСЕГДА — по той же причине, по которой
    `TallyBook` печатает сводку и на успехе: «всё хорошо» должно быть
    утверждением, которое можно сравнить со следующим прогоном. Отсутствие
    строки утверждением не является — именно из-за него разрушенный граф
    прожил незамеченным несколько дней.
    """
    out = log or _log
    out.info("  %s", ownership_line(report))
    if report["state"] == STATE_OK:
        return True
    out.warning("  ⚠ %s", report["message"])
    if report.get("hint"):
        out.warning("    %s", report["hint"])
    return False
