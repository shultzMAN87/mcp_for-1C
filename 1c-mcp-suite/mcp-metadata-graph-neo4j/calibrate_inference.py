"""Калибровка резолва вызовов на реальной Котировке (4.6.4 + FIX-4).

Воспроизводимый замер «до/после» без Neo4j и без Docker: парсер + резолвер
гоняются прямо по XML-выгрузке. В отличие от calibrate.py (тот меряет
парсер), этот меряет РЕЗОЛВ: coverage %, разбивку reason_counts и разбивку
неразрешённых по природе module_ref.

FIX-4 разделил `unresolved` на два класса:
  • пробелы     — вызов похож на обращение к коду конфигурации, адресат
                  не найден. Это и есть то, что стоит чинить.
  • методы объектов — слева от точки объявленная переменная
                  (`РезультатЗапроса.Выбрать()`). Резолвить некуда, в
                  знаменатель покрытия не идёт.
Скрипт печатает обе метрики: честную (FIX-4) и прежнюю, чтобы разница была
видна прямо в отчёте.

Запуск:
    python calibrate_inference.py /path/to/workspace
    python calibrate_inference.py /path/to/workspace --check
    python calibrate_inference.py /path/to/workspace --save baseline.json
    python calibrate_inference.py /path/to/workspace --baseline baseline.json

`--check` — приёмка FIX-4: exit 0, только если покрытие ≥ порога
(`--min-coverage`, по умолчанию 90) и среди оставшихся пробелов нет имён,
объявленных в области видимости своей же процедуры.

Внимание: без живой Neo4j индекс собирается по именам папок
(metadata_objects неполон, common_module_props пуст), поэтому абсолютные
числа НИЖЕ стендовых. Значимы пропорции и дельта «до/после».
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from bsl_parser import (
    walk_workspace_bsl, iter_calls, collect_local_names,
    context_property_names,
)
from bsl_resolver import (
    build_index_from_modules, build_call_graph, infer_local_types,
    NON_CONFIG_CALL_REASONS,
)


# Имя переменной/идентификатора BSL — для поиска присваиваний и параметров.
_RE_ASSIGN_LHS = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*='
    r'(?![=<>])'  # не ==, не <=, не >=
)

# Причины, по которым резолвер вообще не создаёт :CallSite (ResolveResult.skip).
# В reason_counts они лежат вперемешку с причинами unresolved, и без этого
# списка отчёт помечал бы `collection_method` как пробел в графе.
_SKIP_REASONS = frozenset({
    "builtin",
    "metadata_access_not_call",
    "collection_method",
})


def _assigned_vars(body_text: str) -> set[str]:
    """Множество имён переменных, которым в теле процедуры что-то присваивается
    (любым выражением, не только Plural.X.Method)."""
    return {m.group("var") for m in _RE_ASSIGN_LHS.finditer(body_text)}


def classify_unknown_module(proc, module_ref: str, typed_vars: set[str]) -> str:
    """Классифицирует природу module_ref у unresolved-callsite'а с reason=unknown_module.

    Группы (по PLAN_4_6_4.md раздел 2):
      • 'param'                — module_ref это имя параметра caller-процедуры
      • 'local_untyped_assign' — module_ref это локал, которому что-то присваивается,
                                 но тип не выведен
      • 'no_assignment'        — module_ref без присваивания в теле (глобал / неявный
                                 ЭтотОбъект / длинная цепочка) — статически нерешаемо
    """
    param_names = {p.name for p in proc.parameters}
    if module_ref in param_names:
        return "param"
    assigned = _assigned_vars(proc.body_text)
    if module_ref in assigned and module_ref not in typed_vars:
        return "local_untyped_assign"
    if module_ref in assigned:
        # присвоен И типизирован, но всё равно unknown_module — редкий случай
        # (например тип выведен, но KIND_TO_MODULE_ROLE его не знает). Считаем
        # как local_untyped_assign — он «достижим» при усилении dataflow.
        return "local_untyped_assign"
    return "no_assignment"


def _object_attributes_from_xml(root: Path) -> dict:
    """full_name_eng → имена реквизитов и табличных частей (нижний регистр).

    Дублирует то, что `build_index_from_neo4j` читает из графа. Если
    метаданные не разобрались, возвращает пусто и замер просто теряет
    точность на реквизитах — падать он от этого не должен.
    """
    try:
        from metadata_xml import walk_workspace
    except ImportError:                      # pragma: no cover
        return {}
    out: dict[str, frozenset[str]] = {}
    for o in walk_workspace(root):
        names = {a.name.lower() for a in o.attributes}
        names |= {ts.name.lower() for ts in o.tabular_sections}
        if names:
            out[o.full_name_eng] = frozenset(names)
    return out


def measure(root: Path) -> dict:
    """Один полный замер. Возвращает dict с метриками."""
    modules = walk_workspace_bsl(root)
    index = build_index_from_modules(modules)
    # FIX-5 часть 2: реквизиты объектов резолвер в проде читает из Neo4j.
    # Здесь берём их напрямую из XML — иначе офлайн-замер показывал бы
    # пробелы, которых в графе уже нет, и расходился бы с metadata_stats.
    index.object_attributes = _object_attributes_from_xml(root)
    cg = build_call_graph(modules, index)
    stats = cg["stats"]

    resolved = stats["resolved"]
    unresolved = stats["unresolved"]
    gaps = stats["unresolved_gaps"]
    object_methods = stats["unresolved_object_method"]

    # Честное покрытие (FIX-4) считает сам резолвер. Прежнюю метрику
    # оставляем рядом — по ней видно, насколько сильно шум её занижал.
    coverage = stats["resolve_coverage_pct"]
    legacy_total = resolved + unresolved
    coverage_legacy = (100.0 * resolved / legacy_total) if legacy_total else 0.0

    proc_by_caller: dict[str, object] = {}
    for m in modules:
        for proc in m.procedures:
            proc_by_caller[f"{m.module_id}.{proc.name}"] = proc

    # Область видимости по caller_id — та же, что видит резолвер.
    scope_by_caller: dict[str, set[str]] = {}
    for m in modules:
        for proc in m.procedures:
            scope_by_caller[f"{m.module_id}.{proc.name}"] = (
                collect_local_names(proc) | m.module_vars | m.context_names
            )

    # FIX-4.1: сбор реквизитов форм — самодиагностика. Разбор Ext/Form.xml
    # устойчив к схеме и при промахе молча вернёт пусто; здесь видно, сколько
    # модулей форм есть и у скольких из них реквизиты действительно собрались.
    form_modules = [m for m in modules if m.module_kind == "Form"]
    forms_with_attrs = sum(
        1 for m in form_modules
        if m.context_names - context_property_names("Form")
    )
    form_attrs_total = sum(
        len(m.context_names - context_property_names("Form"))
        for m in form_modules
    )

    # Кэш typed_vars по caller_id (infer_local_types может быть дорогим).
    typed_cache: dict[str, set[str]] = {}

    group_counts = {"param": 0, "local_untyped_assign": 0, "no_assignment": 0}
    # Приёмка FIX-4: среди оставшихся пробелов не должно быть имён,
    # объявленных в своей же процедуре. Если такие есть — значит область
    # видимости где-то не доехала до резолвера.
    leaked: list[dict] = []
    gap_module_refs: dict[str, int] = {}

    for cs in cg["callsite_nodes"]:
        if cs["resolved"]:
            continue
        reason = cs.get("reason") or ""
        if reason in NON_CONFIG_CALL_REASONS:
            continue

        caller_id = cs["caller_id"]
        ref = cs["module_ref"]
        if ref:
            gap_module_refs[ref] = gap_module_refs.get(ref, 0) + 1
            # FIX-4.1: сравнение регистронезависимое, как в резолвере.
            if ref.lower() in scope_by_caller.get(caller_id, ()):
                leaked.append({
                    "caller_id": caller_id,
                    "module_ref": ref,
                    "method_name": cs["method_name"],
                    "line": cs["line"],
                    "reason": reason,
                })

        if reason != "unknown_module":
            continue
        proc = proc_by_caller.get(caller_id)
        if proc is None:
            continue
        if caller_id not in typed_cache:
            typed_cache[caller_id] = set(infer_local_types(proc).keys())
        grp = classify_unknown_module(proc, ref, typed_cache[caller_id])
        group_counts[grp] += 1

    # :INFERRED_TYPE / :Type — появятся после этапа D.
    n_inferred_type = sum(1 for e in cg["edges"] if e["rel"] == "INFERRED_TYPE")
    n_type_nodes = len(cg.get("type_nodes", []))
    fixpoint_iterations = stats.get("fixpoint_iterations")

    return {
        "modules": len(modules),
        "callables": stats["callable_nodes"],
        "resolved": resolved,
        "unresolved": unresolved,
        "unresolved_gaps": gaps,
        "unresolved_object_method": object_methods,
        "skipped": stats["skipped"],
        "coverage": coverage,
        "coverage_legacy": coverage_legacy,
        "reason_counts": dict(stats["reason_counts"]),
        "unknown_module_groups": group_counts,
        "top_gap_module_refs": sorted(
            gap_module_refs.items(), key=lambda x: -x[1])[:15],
        "scope_leaks": leaked[:20],
        "scope_leaks_total": len(leaked),
        "form_modules": len(form_modules),
        "forms_with_attrs": forms_with_attrs,
        "form_attrs_total": form_attrs_total,
        "inferred_type_edges": n_inferred_type,
        "type_nodes": n_type_nodes,
        "fixpoint_iterations": fixpoint_iterations,
    }


def print_report(r: dict, baseline: dict | None = None) -> None:
    print(f"Модулей:    {r['modules']}")
    print(f"Callable:   {r['callables']}")
    print()
    print(f"resolved:   {r['resolved']}")
    print(f"unresolved: {r['unresolved']}")
    print(f"  пробелы в графе:        {r['unresolved_gaps']}")
    print(f"  методы объектов (FIX-4): {r['unresolved_object_method']}")
    print(f"skipped:    {r['skipped']}")
    print()
    cov = r["coverage"]
    if baseline:
        delta = cov - baseline.get("coverage", 0.0)
        print(f"coverage:   {cov:.2f}%  (было {baseline.get('coverage', 0.0):.2f}%, "
              f"дельта {delta:+.2f} п.п.)")
    else:
        print(f"coverage:   {cov:.2f}%")
    print(f"  прежняя метрика (с методами объектов в знаменателе): "
          f"{r['coverage_legacy']:.2f}%")
    print()
    print("reason_counts:")
    for reason, cnt in sorted(r["reason_counts"].items(), key=lambda x: -x[1]):
        if reason in _SKIP_REASONS:
            mark = "  ○"
        elif reason in NON_CONFIG_CALL_REASONS:
            mark = "  ·"
        else:
            mark = "  ✗"
        print(f"{mark} {reason:34} {cnt}")
    print("  ○ — skip, :CallSite не создаётся (built-in, метод коллекции)")
    print("  · — :CallSite есть, но это метод объекта: не пробел, в покрытие не идёт")
    print("  ✗ — настоящий пробел в графе")
    print()
    print("разбивка unknown_module по природе module_ref:")
    g = r["unknown_module_groups"]
    base_g = baseline["unknown_module_groups"] if baseline else {}
    for key, label in [
        ("param",                "параметр caller'а (inter-procedural)"),
        ("local_untyped_assign", "локал с присваиванием, тип не выведен"),
        ("no_assignment",        "без присваивания (статически нерешаемо)"),
    ]:
        cur = g.get(key, 0)
        if baseline:
            d = cur - base_g.get(key, 0)
            print(f"  {label:46} {cur:5}  ({d:+d})")
        else:
            print(f"  {label:46} {cur:5}")
    print()
    if r["top_gap_module_refs"]:
        print("топ module_ref среди настоящих пробелов:")
        for ref, cnt in r["top_gap_module_refs"]:
            print(f"  {ref:40} {cnt}")
        print()
    if r["scope_leaks_total"]:
        print(f"⚠ имён из области видимости среди пробелов: {r['scope_leaks_total']}")
        for leak in r["scope_leaks"]:
            print(f"    {leak['caller_id']}:{leak['line']} "
                  f"{leak['module_ref']}.{leak['method_name']} → {leak['reason']}")
        print()
    # FIX-4.1: разбор Ext/Form.xml устойчив к схеме и при промахе молчит.
    # Здесь это видно: если модули форм есть, а реквизитов ноль — схема
    # выгрузки отличается от ожидаемой, надо прислать один Form.xml.
    fm = r.get("form_modules", 0)
    if fm:
        print(f"реквизиты форм из Ext/Form.xml: {r['form_attrs_total']} шт. "
              f"у {r['forms_with_attrs']} из {fm} модулей форм")
        if not r["forms_with_attrs"]:
            print("  ⚠ ни у одной формы реквизиты не собрались — вероятно, "
                  "схема Form.xml отличается от ожидаемой")
        print()
    print(f":INFERRED_TYPE рёбер: {r['inferred_type_edges']}")
    print(f":Type-узлов слоя 2:   {r['type_nodes']}")
    if r["fixpoint_iterations"] is not None:
        print(f"итераций фикс-пойнта: {r['fixpoint_iterations']}")


def check(r: dict, min_coverage: float) -> int:
    """Приёмка FIX-4. Возвращает exit-code."""
    print()
    print("=" * 60)
    failures: list[str] = []

    if r["coverage"] < min_coverage:
        failures.append(
            f"покрытие {r['coverage']:.2f}% ниже порога {min_coverage:.2f}%")
    else:
        print(f"✓ покрытие {r['coverage']:.2f}% ≥ {min_coverage:.2f}%")

    if r["scope_leaks_total"]:
        failures.append(
            f"{r['scope_leaks_total']} пробелов ссылаются на имена, объявленные "
            f"в своей же процедуре — область видимости не доехала до резолвера")
    else:
        print("✓ среди пробелов нет имён локальных переменных")

    if failures:
        print()
        for f in failures:
            print(f"✗ FAIL: {f}")
        return 1
    print()
    print("✓ PASS")
    return 0


def main(root: Path, baseline_path: Path | None = None,
         save_path: Path | None = None, do_check: bool = False,
         min_coverage: float = 90.0) -> int:
    print(f"Workspace: {root}")
    print("=" * 60)
    r = measure(root)

    baseline = None
    if baseline_path and baseline_path.exists():
        import json
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        print(f"(сравнение с baseline: {baseline_path.name})")
        print("=" * 60)

    print_report(r, baseline)

    if save_path:
        import json
        save_path.write_text(json.dumps(r, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print()
        print(f"Замер сохранён в {save_path}")

    return check(r, min_coverage) if do_check else 0


if __name__ == "__main__":
    # Использование:
    #   python calibrate_inference.py <workspace>
    #   python calibrate_inference.py <workspace> --check [--min-coverage 90]
    #   python calibrate_inference.py <workspace> --save baseline.json
    #   python calibrate_inference.py <workspace> --baseline baseline.json
    args = sys.argv[1:]
    if not args:
        print("usage: python calibrate_inference.py <workspace> "
              "[--check] [--min-coverage N] [--save FILE | --baseline FILE]")
        sys.exit(1)
    ws = Path(args[0]).resolve()
    baseline_p = None
    save_p = None
    if "--baseline" in args:
        baseline_p = Path(args[args.index("--baseline") + 1])
    if "--save" in args:
        save_p = Path(args[args.index("--save") + 1])
    min_cov = 90.0
    if "--min-coverage" in args:
        min_cov = float(args[args.index("--min-coverage") + 1])
    sys.exit(main(ws, baseline_p, save_p, "--check" in args, min_cov))
