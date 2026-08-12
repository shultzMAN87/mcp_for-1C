#!/usr/bin/env python3
"""
BASE-1 — сверка графа с записанной базой.
==========================================

Зачем. Сверка с базой Захода 2 дала ложную тревогу: числа лежали в
markdown-документе, а выгрузка успела измениться, и понять это было
неоткуда — рядом с числами не хранилось ни fingerprint'а, ни даты, ни
команды, которой они получены.

Здесь база лежит в `evals/baseline.json` вместе с fingerprint'ом выгрузки.
Если fingerprint разошёлся, скрипт **не молчит и не падает**, а прямо
говорит: расхождение чисел ожидаемо, потому что изменилась сама выгрузка.
Это и есть разница между «база сломалась» и «база устарела» — в Заходе 2
её было нечем провести.

Запуск (нужен живой Neo4j):
    python3 scripts/check_baseline.py
    python3 scripts/check_baseline.py --update   # перезаписать базу
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "evals" / "baseline.json"

# Допуск в процентах. Ноль был бы бесполезен: конфигурация живёт, и
# сдвиг на десяток объектов — норма, а не поломка. Смысл проверки в том,
# чтобы поймать обвал (слой не записался, инструмент потерял половину
# рёбер), а не зафиксировать конфигурацию в янтаре.
DEFAULT_TOLERANCE_PCT = 5.0

COUNTS = {
    "MetadataObject": "MATCH (n:MetadataObject) WHERE NOT n:Module RETURN count(n) AS c",
    "Attribute":      "MATCH (n:Attribute) RETURN count(n) AS c",
    "Form":           "MATCH (n:Form) RETURN count(n) AS c",
    "Module":         "MATCH (n:Module) RETURN count(n) AS c",
    "Callable":       "MATCH (n:Callable) RETURN count(n) AS c",
    "CallSite":       "MATCH (n:CallSite) RETURN count(n) AS c",
    "Parameter":      "MATCH (n:Parameter) RETURN count(n) AS c",
    "Subsystem":      "MATCH (n:MetadataObject {kind_eng:'Subsystem'}) RETURN count(n) AS c",
    "rel_HAS_METHOD": "MATCH ()-[r:HAS_METHOD]->() RETURN count(r) AS c",
    "rel_HAS_ATTRIBUTE": "MATCH ()-[r:HAS_ATTRIBUTE]->() RETURN count(r) AS c",
    "rel_CONTAINS":   "MATCH ()-[r:CONTAINS]->() RETURN count(r) AS c",
    "rel_CALLS":      "MATCH ()-[r:CALLS]->() RETURN count(r) AS c",
    "rel_CALL_SITE":  "MATCH ()-[r:CALL_SITE]->() RETURN count(r) AS c",
    # Инварианты. Ноль здесь — не «мало данных», а «связь на месте».
    "orphan_Callable":
        "MATCH (c:Callable) WHERE NOT ()-[:HAS_METHOD]->(c) RETURN count(c) AS c",
}


def env_chain() -> dict:
    out = {}
    for name in (".env", ".env.local"):
        p = ROOT / name
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    out.update(os.environ)
    return out


def neo4j_rows(url: str, user: str, password: str, cypher: str) -> list[dict]:
    body = json.dumps({"statements": [{"statement": cypher}]}).encode()
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        url.rstrip("/") + "/db/neo4j/tx/commit", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Basic {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    res = data["results"][0]
    return [dict(zip(res["columns"], row["row"])) for row in res.get("data", [])]


def collect(url: str, user: str, password: str) -> dict:
    out = {}
    for name, cypher in COUNTS.items():
        out[name] = neo4j_rows(url, user, password, cypher)[0]["c"]
    fp = neo4j_rows(
        url, user, password,
        "MATCH (n:Fingerprint) RETURN n.kind AS kind, n.value AS value, n.mode AS mode",
    )
    out["_fingerprints"] = {r["kind"]: {"value": r["value"], "mode": r["mode"]}
                            for r in fp}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="BASE-1: сверка графа с базой.")
    ap.add_argument("--update", action="store_true",
                    help="перезаписать базу текущими числами")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="допуск в процентах")
    args = ap.parse_args()

    env = env_chain()
    url = env.get("NEO4J_URL", "http://localhost:7474")
    user = env.get("NEO4J_USER", "neo4j")
    password = env.get("NEO4J_PASSWORD", "")
    if not password:
        print("NEO4J_PASSWORD не задан — базу проверить нечем.", file=sys.stderr)
        return 2

    try:
        actual = collect(url, user, password)
    except (urllib.error.URLError, OSError) as e:
        print(f"Neo4j недоступен ({url}): {e}", file=sys.stderr)
        print("Сверка пропущена — это не провал тестов.", file=sys.stderr)
        return 0

    if args.update or not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_комментарий": (
                "BASE-1. Эталонные числа графа. Обновлять командой "
                "`python3 scripts/check_baseline.py --update` ПОСЛЕ того, как "
                "убедились, что новые числа верны, а не просто новые."
            ),
            "_как_получено": (
                "docker compose up -d --force-recreate metadata-indexer "
                "(METADATA_FORCE_XML=true), затем этот скрипт с --update"
            ),
            "tolerance_pct": args.tolerance or DEFAULT_TOLERANCE_PCT,
            "counts": {k: v for k, v in actual.items() if not k.startswith("_")},
            "fingerprints": actual["_fingerprints"],
        }
        BASELINE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"База записана: {BASELINE.relative_to(ROOT)}")
        return 0

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    tolerance = args.tolerance or base.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)

    # Сначала fingerprint: он решает, как читать расхождения ниже.
    fp_changed = []
    for kind, saved in (base.get("fingerprints") or {}).items():
        now = actual["_fingerprints"].get(kind)
        if not now:
            fp_changed.append(f"{kind}: исчез")
        elif now.get("mode") != saved.get("mode"):
            fp_changed.append(f"{kind}: режим {saved.get('mode')} → {now.get('mode')}")
        elif now.get("value") != saved.get("value"):
            fp_changed.append(f"{kind}: выгрузка изменилась")

    if fp_changed:
        print("ВНИМАНИЕ: fingerprint разошёлся с базой —")
        for line in fp_changed:
            print(f"    {line}")
        print("    Расхождение чисел ниже ОЖИДАЕМО: изменилась сама выгрузка,")
        print("    а не граф. Убедитесь, что числа верны, и обновите базу:")
        print("        python3 scripts/check_baseline.py --update")
        print()

    problems = []
    for name, expected in base["counts"].items():
        got = actual.get(name)
        if got is None:
            problems.append(f"{name}: нет в текущем графе")
            continue
        if name.startswith("orphan_"):
            # Инварианты — без допуска: рост с нуля означает потерю связи.
            if got > expected:
                problems.append(f"{name}: {expected} → {got} (связь потеряна)")
            continue
        if expected == 0:
            continue
        delta_pct = 100.0 * (got - expected) / expected
        mark = "" if abs(delta_pct) <= tolerance else "  ← за допуском"
        line = f"  {name:<20} {expected:>9} → {got:>9}  ({delta_pct:+.1f}%){mark}"
        print(line)
        if abs(delta_pct) > tolerance and not fp_changed:
            problems.append(f"{name}: {expected} → {got} ({delta_pct:+.1f}%)")

    print()
    if problems:
        print(f"БАЗА НЕ СОШЛАСЬ ({len(problems)}):")
        for p in problems:
            print(f"    {p}")
        return 1
    print(f"База сошлась (допуск {tolerance:.0f}%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
