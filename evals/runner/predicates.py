"""
Предикаты eval-runner'а.

Каждый тип предиката — отдельная функция `_pred_<type>(pred, result)`,
возвращающая `PredicateOutcome(passed, detail)`. `detail` идёт в JSON-отчёт
и помогает разобраться, ПОЧЕМУ предикат провалился.

Формат результата MCP-tool'а ожидается как dict с ключом `results`
(список хитов с полями `name_ru`, `name_en`, `full_name`, `kind` и т.д.)
— так отдают и `platform_help_search`, и `platform_help_lookup`.

STD-6: у сервера v8std форма ответа другая. `v8std_search` тоже кладёт
список в `results`, но опознавательный признак там — `id` (`std783`), а не
`name_ru`; а `v8std_get_page` и `v8std_explain_diagnostics` ключа `results`
вообще не имеют. Поэтому добавлено ровно две вещи, а не отдельный раннер:

  - `name_in_top_k` смотрит ещё и в `id` с `title`;
  - предикаты `field_equals` и `path_non_empty` работают по точечному пути
    в ответе (`found`, `page.id`, `diagnostics`) и не зависят от формы.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PredicateOutcome:
    """Результат одной проверки."""
    type: str
    passed: bool
    detail: dict[str, Any]
    match_rank: int | None = None


# Ключи, под которыми серверы отдают список найденного.
#
# EVAL-1. Раньше здесь был только `results` — так отвечают platform-help,
# v8std и bsl-checker. Но metadata-graph отдаёт `items`, и на его ответах
# все предикаты видели пустой список: датасет провалился бы целиком, а
# выглядело бы это как «инструменты сломаны», хотя сломан был бы раннер.
#
# Порядок важен: берём первый ключ, под которым лежит список. Складывать
# их нельзя — сервер мог бы вернуть оба и результат задвоился бы.
#
# Список пришлось расширять по факту первого прогона: разные инструменты
# называют «список найденного» по-своему, и предполагать единое имя было
# наивно. Проверять надо было ДО того, как писать датасет, — иначе провал
# раннера неотличим от провала инструментов, что на первом прогоне и
# произошло: пять примеров упали, и выглядело это как сломанный поиск.
_HIT_KEYS = (
    "results",            # platform-help, v8std, bsl-checker
    "items",              # metadata_search, metadata_list_objects, metadata_subsystems
    "hits",
    "callers", "callees",  # code_callers / code_callees
    "direct_attributes",   # metadata_object_attributes
    "members",             # metadata_subsystem_members
)


def _hits(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    for key in _HIT_KEYS:
        r = result.get(key)
        if isinstance(r, list):
            return [h for h in r if isinstance(h, dict)]
    return []


def _norm(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().casefold()


def _pred_non_empty(pred: dict, result: Any) -> PredicateOutcome:
    hits = _hits(result)
    return PredicateOutcome(
        type="non_empty",
        passed=len(hits) > 0,
        detail={"hits_count": len(hits)},
    )


def _pred_results_count_at_least(pred: dict, result: Any) -> PredicateOutcome:
    min_n = int(pred.get("min", 1))
    hits = _hits(result)
    return PredicateOutcome(
        type="results_count_at_least",
        passed=len(hits) >= min_n,
        detail={"min": min_n, "actual": len(hits)},
    )


def _pred_name_in_top_k(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    values = pred.get("values") or []
    if not isinstance(values, list) or not values:
        return PredicateOutcome(
            type="name_in_top_k",
            passed=False,
            detail={"error": "values must be non-empty list"},
        )
    wanted = {_norm(v) for v in values if isinstance(v, str)}

    hits = _hits(result)[:k]
    match_rank: int | None = None
    matched_name: str | None = None

    for idx, h in enumerate(hits, start=1):
        candidates = (
            _norm(h.get("name_ru")),
            _norm(h.get("name_en")),
            _norm(h.get("full_name")),
            # STD-6: у v8std опознавательный признак хита — id (std783),
            # изредка title. Для platform_help этих ключей нет, так что
            # старые датасеты ведут себя ровно как раньше.
            _norm(h.get("id")),
            _norm(h.get("title")),
            # EVAL-1: metadata-graph отдаёт короткое имя в `name`. Без этого
            # ключа поиск, вернувший «Контрагенты» ПЕРВЫМ результатом со
            # score 23.13, считался промахом — предикат просто не смотрел
            # туда, где лежит ответ. Диагностика, молчащая о том, что она
            # не умеет мерить, хуже её отсутствия: два прогона выглядели
            # как дефект ранжирования, которого не было.
            _norm(h.get("name")),
        )
        for c in candidates:
            if c and c in wanted:
                match_rank = idx
                matched_name = c
                break
        if match_rank is not None:
            break

    return PredicateOutcome(
        type="name_in_top_k",
        passed=match_rank is not None,
        detail={
            "k": k,
            "values": values,
            "match_rank": match_rank,
            "matched_name": matched_name,
            "hits_examined": len(hits),
        },
        match_rank=match_rank,
    )


def _pred_any_hit_kind(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    kinds = pred.get("kinds") or []
    if not isinstance(kinds, list) or not kinds:
        return PredicateOutcome(
            type="any_hit_kind",
            passed=False,
            detail={"error": "kinds must be non-empty list"},
        )
    wanted = {_norm(v) for v in kinds if isinstance(v, str)}

    hits = _hits(result)[:k]
    found_kinds = [_norm(h.get("kind")) for h in hits]
    hit_idx = None
    for idx, k_found in enumerate(found_kinds, start=1):
        if k_found in wanted:
            hit_idx = idx
            break

    return PredicateOutcome(
        type="any_hit_kind",
        passed=hit_idx is not None,
        detail={
            "k": k,
            "kinds": kinds,
            "first_match_rank": hit_idx,
            "observed_kinds": found_kinds,
        },
    )


def _pred_full_name_contains(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    substr = pred.get("substr") or ""
    if not isinstance(substr, str) or not substr:
        return PredicateOutcome(
            type="full_name_contains",
            passed=False,
            detail={"error": "substr must be non-empty string"},
        )
    needle = _norm(substr)

    hits = _hits(result)[:k]
    match_rank = None
    matched_full_name = None
    for idx, h in enumerate(hits, start=1):
        # EVAL-1 научил: смотреть в одно поле мало. Тогда `name_in_top_k`
        # знал про name_ru/name_en/full_name, а metadata-graph отдавал
        # короткое имя в `name` — два прогона выглядели как дефект
        # ранжирования, которого не было. Здесь та же ловушка: у статей
        # справки заголовок живёт в name_ru, а full_name собирается из
        # родителя с именем и у части записей пуст.
        haystack = " ".join(
            _norm(h.get(key))
            for key in ("full_name", "name_ru", "name_en", "title", "name")
        )
        if needle in haystack:
            match_rank = idx
            matched_full_name = h.get("full_name") or h.get("name_ru") or h.get("name_en")
            break

    return PredicateOutcome(
        type="full_name_contains",
        passed=match_rank is not None,
        detail={
            "k": k,
            "substr": substr,
            "match_rank": match_rank,
            "matched_full_name": matched_full_name,
        },
    )


def _dig(result: Any, path: str) -> tuple[bool, Any]:
    """
    Достаёт значение по точечному пути: "page.id", "diagnostics", "found".
    Возвращает (нашли ли, значение). Отсутствие ключа и значение None —
    разные вещи, поэтому флагом, а не через sentinel.
    """
    current = result
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _pred_field_equals(pred: dict, result: Any) -> PredicateOutcome:
    """
    Значение по пути равно ожидаемому. Строки сравниваются без учёта
    регистра и пробелов по краям, остальное — как есть.
    """
    path = str(pred.get("path", "")).strip()
    expected = pred.get("value")
    if not path:
        return PredicateOutcome(
            type="field_equals", passed=False,
            detail={"error": "path is required"},
        )

    found, actual = _dig(result, path)
    if isinstance(expected, str) and isinstance(actual, str):
        passed = _norm(actual) == _norm(expected)
    else:
        passed = found and actual == expected

    return PredicateOutcome(
        type="field_equals", passed=bool(found and passed),
        detail={"path": path, "expected": expected, "actual": actual, "found": found},
    )


def _pred_path_non_empty(pred: dict, result: Any) -> PredicateOutcome:
    """
    По пути лежит непустой список/строка/словарь. Нужен там, где ответ не
    имеет ключа `results`: `diagnostics` у v8std_explain_diagnostics,
    `standards` у v8std_explain_snippet, `related` у v8std_get_related.
    """
    path = str(pred.get("path", "")).strip()
    if not path:
        return PredicateOutcome(
            type="path_non_empty", passed=False,
            detail={"error": "path is required"},
        )

    found, value = _dig(result, path)
    size = len(value) if isinstance(value, (list, str, dict)) else None
    passed = bool(found and size)

    return PredicateOutcome(
        type="path_non_empty", passed=passed,
        detail={"path": path, "found": found, "size": size,
                "type": type(value).__name__ if found else None},
    )


def _pred_path_contains(pred: dict, result: Any) -> PredicateOutcome:
    """
    В тексте по пути встречаются все указанные подстроки.

    `EVAL-5`. Понадобился там, где проверять надо СОБРАННЫЙ ТЕКСТ, а не
    поле ответа. `query_build` не возвращает списка соединений вовсе —
    соединения существуют только внутри `query`. Первая редакция примера
    ждала поле `joins`, которого в ответе нет и не было: предикат не мог
    пройти ни при какой работе инструмента.

    Сравнение регистронезависимое: ключевые слова языка запросов пишут
    и капсом, и как придётся, а мерить надо смысл, а не привычку.
    """
    path = str(pred.get("path", "")).strip()
    subs = pred.get("substrings") or ([pred["substr"]] if pred.get("substr") else [])
    if not path or not subs:
        return PredicateOutcome(
            type="path_contains", passed=False,
            detail={"error": "path and substrings (or substr) are required"},
        )

    found, value = _dig(result, path)
    text = value if isinstance(value, str) else ("" if not found else str(value))
    low = text.lower()
    missing = [s for s in subs if str(s).lower() not in low]

    return PredicateOutcome(
        type="path_contains",
        passed=bool(found and not missing),
        detail={"path": path, "found": found, "missing": missing,
                # Кусок текста в отчёт: без него красный пример
                # заставляет лезть в json руками.
                "excerpt": text[:400]},
    )


def _pred_field_at_least(pred: dict, result: Any) -> PredicateOutcome:
    """
    Число по пути не меньше порога.

    Пробел, который стоил четырёх красных примеров. Числовых предикатов в
    наборе не было вообще: `path_non_empty` меряет `len`, и на `int` даёт
    осечку (грабля из `AUDIT-2`, на неё наступили `qb-003`, `mg-404`, а
    потом чуть не наступил я в `bsl-009`), а `field_equals` требует точного
    совпадения — прибивать ожидание к числу из живой базы значит получить
    красный пример при первом же изменении конфигурации.

    Отсюда `field_at_least`: проверяет порядок величины, а не значение.
    «Замечаний не меньше двух», «в графе не меньше тысячи объектов» —
    утверждения, которые переживают рост базы.
    """
    path = str(pred.get("path", "")).strip()
    threshold = pred.get("min")
    if not path or threshold is None:
        return PredicateOutcome(
            type="field_at_least", passed=False,
            detail={"error": "path and min are required"},
        )

    found, actual = _dig(result, path)
    passed = False
    if found and isinstance(actual, bool):
        # True не должен проходить как единица: это почти наверняка
        # ошибка в датасете, а не осознанная проверка.
        actual_note = "bool, а не число"
    elif found and isinstance(actual, (int, float)):
        passed = actual >= threshold
        actual_note = ""
    else:
        actual_note = f"не число: {type(actual).__name__ if found else 'нет поля'}"

    detail = {"path": path, "min": threshold, "actual": actual, "found": found}
    if actual_note:
        detail["problem"] = actual_note
    return PredicateOutcome(type="field_at_least", passed=passed, detail=detail)


def _pred_hit_field_in_top_k(pred: dict, result: Any) -> PredicateOutcome:
    """
    Поле хита в топ-k принимает одно из ожидаемых значений.

    `SEARCH-1`. Все предикаты до этого смотрели в имена: `name_in_top_k`,
    `full_name_contains`. Для диалекта этого мало — вопрос не «какое имя
    в выдаче», а «из какой книги справки страница».

    Признак диалекта в payload есть с самого начала и называется
    `hbk_file`: язык запросов живёт в `shquery_ru.hbk` (128 страниц),
    встроенный — в `shcntx_ru.hbk` (около 25 500) и `shlang_ru.hbk`.
    Проверять его было нечем, поэтому единственный пример про диалект
    (`ph-005`) мерил не диалект, а имена, которые от него зависят
    косвенно.

    Предикат намеренно общий: поле задаётся в датасете. Через него
    проверяется и `chunk_type`, и любое другое поле хита, которое
    когда-нибудь понадобится, — заводить по предикату на поле значило бы
    повторить историю с тремя списками образов (`B-6`).
    """
    k = max(1, int(pred.get("k", 5)))
    field = str(pred.get("field", "")).strip()
    values = pred.get("values") or []
    if not field or not isinstance(values, list) or not values:
        return PredicateOutcome(
            type="hit_field_in_top_k",
            passed=False,
            detail={"error": "field and non-empty values are required"},
        )
    wanted = {_norm(v) for v in values if isinstance(v, str)}

    hits = _hits(result)[:k]
    match_rank = None
    observed = []
    for idx, h in enumerate(hits, start=1):
        actual = _norm(h.get(field))
        observed.append(h.get(field))
        if actual and actual in wanted and match_rank is None:
            match_rank = idx

    return PredicateOutcome(
        type="hit_field_in_top_k",
        passed=match_rank is not None,
        detail={
            "k": k,
            "field": field,
            "values": values,
            "match_rank": match_rank,
            # Наблюдаемые значения печатаются целиком: когда пример
            # красный, ответ на «почему» обычно уже здесь.
            "observed": observed,
        },
        match_rank=match_rank,
    )


_HANDLERS = {
    "non_empty": _pred_non_empty,
    "results_count_at_least": _pred_results_count_at_least,
    "name_in_top_k": _pred_name_in_top_k,
    "any_hit_kind": _pred_any_hit_kind,
    "full_name_contains": _pred_full_name_contains,
    # SEARCH-1: любое поле хита, а не только имя (нужен для hbk_file).
    "hit_field_in_top_k": _pred_hit_field_in_top_k,
    # STD-6: для ответов, у которых нет ключа `results` (сервер v8std).
    "field_equals": _pred_field_equals,
    "path_non_empty": _pred_path_non_empty,
    # Текстовый предикат: см. комментарий у _pred_path_contains (EVAL-5).
    "path_contains": _pred_path_contains,
    # Числовой предикат: см. комментарий у _pred_field_at_least.
    "field_at_least": _pred_field_at_least,
}


# Что этот раннер умеет проверять. Список нужен снаружи: `run_eval`
# сверяет с ним датасет ДО первого вызова, иначе незнакомый предикат
# уезжает в отчёт обычным промахом и выглядит просадкой качества.
KNOWN_PREDICATES = frozenset(_HANDLERS)


def evaluate(pred: dict, result: Any) -> PredicateOutcome:
    t = pred.get("type", "")
    handler = _HANDLERS.get(t)
    if handler is None:
        return PredicateOutcome(
            type=t or "unknown",
            passed=False,
            detail={"error": "unknown_predicate_type", "raw": pred},
        )
    try:
        return handler(pred, result)
    except Exception as e:
        return PredicateOutcome(
            type=t,
            passed=False,
            detail={"error": f"{type(e).__name__}: {e}", "raw": pred},
        )
