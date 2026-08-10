"""
Офлайн-проверка query_validate на реальной выгрузке — без Docker и Neo4j.

Тот же приём, что `calibrate_inference.py` у индексатора: провайдер
метаданных собирается напрямую из XML выгрузки через `metadata_xml.py`, и
на нём прогоняются запросы. Нужен, чтобы после правки резолвера полей
увидеть результат за секунды, а не после пересборки образа и
переиндексации.

    python calibrate_query.py <путь к workspace> [файл-с-запросами.sql]

Без файла запросов печатает сводку по конфигурации и прогоняет встроенный
набор проб, построенный по первому найденному справочнику и регистру.

В образ не копируется — это инструмент разработчика.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-metadata-graph-neo4j"))

import metadata_xml as mx                                  # noqa: E402
from query_check import (AttrInfo, MetadataProvider,       # noqa: E402
                         ObjectInfo, check_query)


class WorkspaceProvider(MetadataProvider):
    """Провайдер поверх XML-выгрузки. В проде его роль играет Neo4j."""

    def __init__(self, root: Path):
        self.objects: dict[str, ObjectInfo] = {}
        self._by_kind: dict[tuple[str, str], ObjectInfo] = {}
        for o in mx.walk_workspace(root):
            info = ObjectInfo(
                id=o.full_name_eng,
                name=o.name,
                kind_ru=o.kind_ru,
                synonym=o.synonym,
                attributes=[
                    AttrInfo(a.name, a.role, [(t.kind, t.target) for t in a.types],
                             a.synonym)
                    for a in o.attributes
                ],
                properties=dict(o.properties or {}),
                has_owner=bool(o.owners),
                tabular_sections={
                    ts.name: [
                        AttrInfo(a.name, "attribute",
                                 [(t.kind, t.target) for t in a.types], a.synonym)
                        for a in ts.attributes
                    ]
                    for ts in o.tabular_sections
                },
            )
            self.objects[info.id] = info
            self._by_kind[(info.kind_ru.lower(), info.name.lower())] = info

    def get(self, kind_ru, name):
        return self._by_kind.get((kind_ru.lower(), name.lower()))

    def get_by_id(self, full_name_eng):
        return self.objects.get(full_name_eng)

    def suggest(self, name, limit=3):
        low = name.lower()
        hits = [o.kind_ru + "." + o.name for o in self.objects.values()
                if low in o.name.lower()]
        return sorted(hits)[:limit]


def _report(title: str, text: str, provider) -> None:
    res = check_query(text, provider)
    mark = "OK  " if res["valid"] else "ОШИБ"
    print(f"\n[{mark}] {title}  (таблиц {res['tables_checked']}, "
          f"полей {res['fields_checked']})")
    for e in res["errors"]:
        print("   ошибка:  ", e)
    for w in res["warnings"]:
        print("   внимание:", w)
    for t in res["temp_tables"]:
        cols = ", ".join(t["columns"]) if t["columns_known"] else "состав неизвестен"
        print(f"   ВТ {t['name']}: {cols}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    provider = WorkspaceProvider(root)
    kinds: dict[str, int] = {}
    for o in provider.objects.values():
        kinds[o.kind_ru] = kinds.get(o.kind_ru, 0) + 1
    print(f"Выгрузка: {root}")
    print("Объектов: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))

    if len(sys.argv) > 2:
        text = Path(sys.argv[2]).read_text(encoding="utf-8")
        _report(Path(sys.argv[2]).name, text, provider)
        return 0

    cat = next((o for o in provider.objects.values()
                if o.kind_ru == "Справочник" and o.attributes), None)
    reg = next((o for o in provider.objects.values()
                if o.kind_ru == "РегистрСведений" and o.attributes), None)
    if cat is None:
        print("В выгрузке нет справочника с реквизитами — пробы пропущены.")
        return 0

    attr = cat.attributes[0].name
    _report("корректный запрос к справочнику",
            f"ВЫБРАТЬ Т.Ссылка, Т.{attr} КАК Знач ИЗ Справочник.{cat.name} КАК Т",
            provider)
    _report("несуществующее поле",
            f"ВЫБРАТЬ Т.Ссылка, Т.ПолеКоторогоНет ИЗ Справочник.{cat.name} КАК Т",
            provider)
    _report("несуществующая таблица",
            "ВЫБРАТЬ Т.Ссылка ИЗ Справочник.НетТакогоСправочника КАК Т",
            provider)
    if cat.tabular_sections:
        ts = next(iter(cat.tabular_sections))
        _report("табличная часть как источник",
                f"ВЫБРАТЬ Т.Ссылка, Т.НомерСтроки ИЗ Справочник.{cat.name}.{ts} КАК Т",
                provider)
    ref_attr = next((a for a in cat.attributes if a.ref_target), None)
    if ref_attr:
        _report("точечный путь в два уровня",
                f"ВЫБРАТЬ Т.{ref_attr.name}.Наименование ИЗ Справочник.{cat.name} КАК Т",
                provider)
        _report("точечный путь с битым вторым сегментом",
                f"ВЫБРАТЬ Т.{ref_attr.name}.НетТакогоПоля ИЗ Справочник.{cat.name} КАК Т",
                provider)
    if reg:
        dim = next((a.name for a in reg.attributes if a.role == "dimension"), None)
        if dim:
            _report("срез последних",
                    f"ВЫБРАТЬ Т.{dim}, Т.Период ИЗ "
                    f"РегистрСведений.{reg.name}.СрезПоследних(&Дата) КАК Т",
                    provider)
            _report("несуществующая виртуальная таблица",
                    f"ВЫБРАТЬ Т.{dim} ИЗ РегистрСведений.{reg.name}.Остатки КАК Т",
                    provider)
    flat = next((o for o in provider.objects.values()
                 if o.kind_ru == "Справочник"
                 and o.properties.get("Hierarchical") == "false"), None)
    if flat is not None:
        _report("FEAT-1.1: Родитель у плоского справочника",
                f"ВЫБРАТЬ Т.Родитель ИЗ Справочник.{flat.name} КАК Т",
                provider)
        if not flat.has_owner:
            _report("FEAT-1.1: Владелец у неподчинённого справочника",
                    f"ВЫБРАТЬ Т.Владелец ИЗ Справочник.{flat.name} КАК Т",
                    provider)
    owned = next((o for o in provider.objects.values()
                  if o.kind_ru == "Справочник" and o.has_owner), None)
    if owned is not None:
        _report("FEAT-1.1: Владелец у подчинённого справочника",
                f"ВЫБРАТЬ Т.Владелец ИЗ Справочник.{owned.name} КАК Т",
                provider)

    _report("пакет с временной таблицей",
            f"""ВЫБРАТЬ Т.Ссылка КАК Объект, Т.{attr} КАК Знач
ПОМЕСТИТЬ ВТДанные
ИЗ Справочник.{cat.name} КАК Т
;
ВЫБРАТЬ В.Объект, В.Знач, В.НетТакойКолонки ИЗ ВТДанные КАК В""",
            provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
