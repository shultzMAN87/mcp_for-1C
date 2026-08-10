"""Калибровка bsl_parser на реальной Котировке.

Запуск:
    python calibrate.py /path/to/workspace
"""
import sys
from pathlib import Path

from bsl_parser import (
    walk_workspace_bsl, iter_calls, iter_metadata_access,
    iter_predef, iter_assign_refs, classify_bsl_path,
)


def main(root: Path):
    print(f"Workspace: {root}")

    # Сначала просто счёт .bsl файлов в нашей схеме.
    all_bsl = sorted(root.rglob("*.bsl"))
    classified = [(p, classify_bsl_path(p.relative_to(root).as_posix())) for p in all_bsl]
    matched = [(p, c) for p, c in classified if c]
    unmatched = [p for p, c in classified if not c]

    print(f"BSL файлов всего: {len(all_bsl)}")
    print(f"  классифицированы: {len(matched)}")
    print(f"  пропущены (не в схеме): {len(unmatched)}")
    if unmatched:
        print("  примеры пропущенных:")
        for p in unmatched[:5]:
            print(f"    {p.relative_to(root)}")

    # Полный обход.
    modules = walk_workspace_bsl(root)
    print()
    print(f"Модулей распарсено: {len(modules)}")

    n_proc, n_func = 0, 0
    n_export = 0
    n_default = 0
    n_byval = 0
    n_params = 0
    dirs: dict[str, int] = {}
    cross_total = 0
    cross_pairs: set[tuple[str, str]] = set()
    local_total = 0
    meta_access = 0
    predefs = 0
    assigns = 0

    for m in modules:
        for p in m.procedures:
            if p.kind == "Procedure":
                n_proc += 1
            else:
                n_func += 1
            if p.is_export:
                n_export += 1
            if p.directive:
                dirs[p.directive] = dirs.get(p.directive, 0) + 1
            n_params += len(p.parameters)
            for prm in p.parameters:
                if prm.is_by_value:
                    n_byval += 1
                if prm.default_value:
                    n_default += 1

            for c in iter_calls(p.body_text, line_offset=p.line_start):
                if c.is_local:
                    local_total += 1
                else:
                    cross_total += 1
                    cross_pairs.add((c.module_ref, c.method_name))
            for _ in iter_metadata_access(p.body_text, line_offset=p.line_start):
                meta_access += 1
            for _ in iter_predef(p.body_text_raw, line_offset=p.line_start):
                predefs += 1
            for _ in iter_assign_refs(p.body_text, line_offset=p.line_start):
                assigns += 1

    total_decls = n_proc + n_func
    print()
    print(f"Декларации (Процедура+Функция): {total_decls}")
    print(f"  процедур: {n_proc}")
    print(f"  функций: {n_func}")
    print(f"  экспортных: {n_export}")
    print(f"  с default: {n_default}")
    print(f"  с Знач: {n_byval}")
    print(f"  параметров всего: {n_params}")
    print()
    print("Директивы:")
    for d, c in sorted(dirs.items(), key=lambda x: -x[1]):
        print(f"  &{d:30} {c}")
    print()
    print(f"Cross-module callsite'ов: {cross_total}")
    print(f"  уникальных (module, method): {len(cross_pairs)}")
    print(f"Локальных callsite'ов: {local_total}")
    print(f"Обращений к метаданным (Plural.X): {meta_access}")
    print(f"ПредопределенноеЗначение(...): {predefs}")
    print(f"Присваиваний-источников типа: {assigns}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
