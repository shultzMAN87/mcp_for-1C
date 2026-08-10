"""Smoke на реальной Котировке для bsl_resolver.

Прогоняет парсер + резолвер (без Neo4j — индекс собирается из модулей +
синтетический metadata_objects), показывает покрытие резолва.
"""
import sys
from pathlib import Path

from bsl_parser import walk_workspace_bsl
from bsl_resolver import build_call_graph, build_index_from_modules


def main(root: Path):
    print(f"Workspace: {root}")
    modules = walk_workspace_bsl(root)
    print(f"Модулей: {len(modules)}")

    # Соберём synthetic metadata_objects из:
    # 1) parent_metadata_id всех не-CommonModule
    # 2) известных имён в выгрузке мы тут не знаем — попробуем найти
    # обращения к ним через scan iter_metadata_access и зарегистрируем.
    metadata_objects: dict[str, str] = {}
    for m in modules:
        if m.parent_metadata_id:
            kind, name = m.parent_metadata_id.split(".", 1)
            metadata_objects.setdefault(name, m.parent_metadata_id)
    # На реальной выгрузке этого хватит — Form/ObjectModule/ManagerModule
    # покрывают все Catalog/Document/InformationRegister, у которых есть BSL.
    # Для остальных (Enum, RegisterX без модулей) — добавим из metadata_xml.py.
    try:
        from metadata_xml import walk_workspace, build_graph
        objects = walk_workspace(root)
        graph = build_graph(objects)
        for n in graph["meta_nodes"]:
            metadata_objects.setdefault(n["name"], n["id"])
    except Exception as e:
        print(f"WARN: cannot load metadata_xml: {e}")

    print(f"Metadata objects in index: {len(metadata_objects)}")

    index = build_index_from_modules(modules, metadata_objects=metadata_objects)
    print(f"Callable IDs in index: {len(index.callable_ids)}")
    print(f"Common modules: {len(index.common_modules)}")

    cg = build_call_graph(modules, index)
    s = cg["stats"]

    print()
    print(f"Nodes: Module={s['module_nodes']}, Callable={s['callable_nodes']}, "
          f"Parameter={s['parameter_nodes']}, CallSite={s['callsite_nodes']}")
    print(f"Edges: total {s['edges_total']}")
    from collections import Counter
    edge_counts = Counter(e["rel"] for e in cg["edges"])
    for rel, n in sorted(edge_counts.items(), key=lambda x: -x[1]):
        print(f"  {rel}: {n}")

    print()
    written_cs = s["resolved"] + s["unresolved"]  # = len(callsite_nodes)
    if written_cs:
        pct = 100.0 * s["resolved"] / written_cs
        print(f"РЕЗОЛВ: resolved={s['resolved']} / written={written_cs} ({pct:.1f}%)")
    print(f"Skipped (built-in/metadata_access): {s['skipped']}")
    print()
    print("Top reasons:")
    for reason, n in sorted(s["reason_counts"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {reason}: {n}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
