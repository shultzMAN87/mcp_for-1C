#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-38. Выгрузка словаря имён из графа в techdocs/names.json.

Смысл: валидатор обязан работать при остановленной Neo4j (R13). Поэтому имена
выгружаются один раз в файл, а docs_validate.py ходит в файл, а не в базу.

Три формы имени берутся из графа, своей нормализации здесь нет
────────────────────────────────────────────────────────────────
Отдельного модуля-нормализатора в наборе не оказалось: FIX-18 решён в Cypher
сравнением с `kind_ru + '.' + name` — так это сделано в пяти местах
(server_v3_tools, server_v3_code_tools, subsystem_scope и далее), и
контрактный тест TestRussianNameForms следит, чтобы шестое место не забыли.

Поэтому здесь тот же приём, а не своя таблица склонений: у узла спрашиваются
`full_name_eng`, `full_name_ru` (множественное число вида) и `kind_ru + '.' +
name` (единственное). Формы приезжают из графа, и разойтись с остальными
инструментами им не на чем.

Ходим в Neo4j так же, как серверы набора: HTTP-API транзакций через urllib,
переменные NEO4J_URL / NEO4J_USER / NEO4J_PASSWORD. Драйвер `neo4j` не нужен —
его нет ни в одном образе, и заводить зависимость ради одного скрипта незачем.

Запуск:
    python scripts\\docs_names_dump.py
    python scripts\\docs_names_dump.py --from-json export.json    # без Neo4j
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Iterable

# B-3 / FAIL-2: защита потоков ровно в той форме, которую требует
# scripts/tests_host_scripts.py. Не encoding="utf-8": подмена кодировки дала
# бы кракозябры в cp1251-консоли, а замена — всего лишь «?» вместо галочки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "techdocs" / "names.json"

# Метка и свойства — из graph_writer.py: у :MetadataObject есть id
# (= full_name_eng), full_name_eng, full_name_ru, kind_eng, kind_ru, name.
QUERY_OBJECTS = """
MATCH (o:MetadataObject)
WHERE o.full_name_eng IS NOT NULL
RETURN o.full_name_eng AS eng,
       o.full_name_ru  AS ru,
       o.kind_ru       AS kind_ru,
       o.kind_eng      AS kind_eng,
       o.name          AS name
"""

# Процедуры в словарь по умолчанию не едут: их 231 114, и проверка по ним
# превратила бы файл в обузу. Флагом можно забрать только экспортные.
QUERY_EXPORTED_PROCEDURES = """
MATCH (p:Procedure)
WHERE coalesce(p.export, false) = true
RETURN coalesce(p.full_name_eng, p.id, p.name) AS eng
LIMIT $limit
"""


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def neo4j_rows(cypher: str, params: dict | None = None, timeout: int = 60) -> list[dict]:
    """Тот же путь к базе, что у серверов набора: /db/neo4j/tx/commit."""
    url = os.environ.get("NEO4J_URL", "http://localhost:7474").rstrip("/")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD") or os.environ.get("NEO4J_PASS")
    if not password:
        raise SystemExit("NEO4J_PASSWORD не задан — как и у серверов набора (SEC-2).")

    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    body = json.dumps({"statements": [{"statement": cypher, "parameters": params or {}}]})
    request = urllib.request.Request(
        f"{url}/db/neo4j/tx/commit",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Neo4j недоступна ({url}): {exc}") from exc

    errors = result.get("errors") or []
    if errors:
        raise SystemExit(f"Neo4j вернула ошибку: {errors}")

    block = result["results"][0]
    columns = block.get("columns", [])
    return [{col: row["row"][i] for i, col in enumerate(columns)}
            for row in block.get("data", [])]


def forms_of(row: dict[str, Any]) -> set[str]:
    """Три формы имени объекта. Ровно те, что принимают инструменты графа."""
    forms: set[str] = set()
    eng = (row.get("eng") or "").strip()
    if eng:
        forms.add(eng)
    ru = (row.get("ru") or "").strip()
    if ru:
        forms.add(ru)                       # множественное: Справочники.Х
    kind_ru = (row.get("kind_ru") or "").strip()
    name = (row.get("name") or "").strip()
    if kind_ru and name:
        forms.add(f"{kind_ru}.{name}")      # единственное: Справочник.Х (FIX-18)
    return forms


def build_dictionary(rows: Iterable[dict[str, Any]]) -> dict:
    aliases: dict[str, str] = {}
    canonical: list[str] = []
    collisions: dict[str, set[str]] = {}

    for row in rows:
        eng = (row.get("eng") or "").strip()
        if not eng:
            continue
        canonical.append(eng)
        for form in forms_of(row):
            key = form.casefold()           # камень 11: регистр не значим
            existing = aliases.get(key)
            if existing and existing != eng:
                collisions.setdefault(key, {existing}).add(eng)
                continue
            aliases[key] = eng

    return {
        "meta": {
            "generated_at": date.today().isoformat(),
            "objects": len(set(canonical)),
            "aliases": len(aliases),
            "collisions": {k: sorted(v) for k, v in sorted(collisions.items())},
            "note": "Генерируется docs_names_dump.py. Руками не править, в git не класть.",
        },
        "canonical": sorted(set(canonical)),
        "aliases": dict(sorted(aliases.items())),
        "procedures": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Словарь имён из графа (DOC-38)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--from-json", default=None,
                        help="файл со списком {eng, ru, kind_ru, name} — режим без Neo4j")
    parser.add_argument("--procedures-limit", type=int, default=0,
                        help="сколько экспортных процедур положить в словарь, 0 — не класть")
    parser.add_argument("--env", default=str(REPO_ROOT / ".env"))
    args = parser.parse_args(argv)

    load_env(Path(args.env))

    procedures: dict[str, str] = {}
    if args.from_json:
        rows = json.loads(Path(args.from_json).read_text(encoding="utf-8-sig"))
    else:
        rows = neo4j_rows(QUERY_OBJECTS)
        if args.procedures_limit > 0:
            for row in neo4j_rows(QUERY_EXPORTED_PROCEDURES,
                                  {"limit": args.procedures_limit}):
                name = (row.get("eng") or "").strip()
                if name:
                    procedures[name.casefold()] = name

    dictionary = build_dictionary(rows)
    dictionary["procedures"] = procedures

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dictionary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    meta = dictionary["meta"]
    print(f"{out_path}: объектов {meta['objects']}, форм имени {meta['aliases']}, "
          f"процедур {len(procedures)}")
    if meta["collisions"]:
        print(f"Коллизии имён: {len(meta['collisions'])}. "
              f"Одна форма ведёт к разным объектам — проверка по ним слепа, разобрать.")
    if meta["objects"] == 0:
        print("В графе ноль объектов. Граф пуст или не тот — словарь бесполезен.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
