#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-30. Инвентаризация пространства Confluence. Только чтение.

Это первый пункт блока H и, возможно, единственный, который вообще
понадобится. Отчёт отвечает на вопрос «нужна ли кампания по legacy и в каком
объёме»: сколько страниц, какого возраста, сколько похоже на техническую
часть, насколько богато оформлены. Возможен исход, при котором технических
страниц полсотни, живых из них полтора десятка, и вся миграция — день ручной
работы. Узнать это надо ДО написания конвертера, а не после.

Ни одного запроса на запись. Клиент создаётся с allow_write=False, так что
запись невозможна не по договорённости, а механически (R18).

Про частоту запросов (R19). Метаданные читаются пакетами через CQL, а не
постранично — это на порядок меньше вызовов. Заголовки лимитов пишутся в
журнал с первого прогона: без них реальные пороги неизвестны, а узнавать их
блокировкой сервисной учётки — плохой способ.

Оформление оценивается по выборке (--sample): для этого нужны тела страниц,
а тянуть тела всего пространства ради оценки незачем.

Запуск:
    python scripts\\docs_inventory.py --space TECHDOC
    python scripts\\docs_inventory.py --space TECHDOC --sample 30 --out отчёт.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from confluence_client import ConfluenceError, client_from_env  # noqa: E402
from confluence_storage import parse_storage  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Слова, по которым страница похожа на техническую. Грубо и намеренно:
# точность здесь не нужна, нужен порядок величины.
TECH_MARKERS = ("http-сервис", "http сервис", "rest", "api", "обмен", "интеграц",
                "регламентн", "выгрузк", "загрузк", "справочник", "регистр",
                "документ.", "общиймодуль", "процедур", "функци", "json", "xml",
                "алгоритм", "запрос", "подсистем")


def age_bucket(when: str) -> str:
    try:
        moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "дата неизвестна"
    days = (datetime.now(timezone.utc) - moment).days
    if days <= 90:
        return "до 3 месяцев"
    if days <= 365:
        return "3–12 месяцев"
    if days <= 730:
        return "1–2 года"
    return "старше 2 лет"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Инвентаризация пространства (DOC-30)")
    parser.add_argument("--space", required=True)
    parser.add_argument("--sample", type=int, default=20,
                        help="сколько страниц прочитать с телом для оценки оформления")
    parser.add_argument("--out", default="inventory.json")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="ограничить обход (для первой пробы)")
    args = parser.parse_args(argv)

    client = client_from_env(allow_write=False, verbose=True)

    print(f"Обход пространства {args.space}. Запись невозможна: клиент открыт "
          f"только на чтение.")
    started = time.monotonic()
    try:
        pages = list(client.search_cql(
            f"space = {args.space} and type = page",
            limit=100, expand="version,space,metadata.labels,ancestors",
            max_pages=args.max_pages))
    except ConfluenceError as exc:
        print(f"Обход не удался: {exc}")
        return 1
    elapsed = time.monotonic() - started

    ages = Counter()
    authors = Counter()
    labels = Counter()
    tech_like = 0
    records = []
    for page in pages:
        version = page.get("version") or {}
        when = version.get("when", "")
        author = ((version.get("by") or {}).get("displayName")
                  or (version.get("by") or {}).get("username") or "неизвестен")
        title = page.get("title", "")
        page_labels = [l.get("name", "") for l in
                       ((page.get("metadata") or {}).get("labels") or {}).get("results", [])]
        is_tech = any(marker in title.casefold() for marker in TECH_MARKERS)
        if is_tech:
            tech_like += 1
        ages[age_bucket(when)] += 1
        authors[author] += 1
        for label in page_labels:
            labels[label] += 1
        records.append({"id": page.get("id"), "title": title, "updated": when,
                        "author": author, "labels": page_labels,
                        "tech_like_title": is_tech,
                        "depth": len(page.get("ancestors") or [])})

    # Оформление — по выборке: для этого нужны тела.
    sample_report = {"checked": 0, "unsupported": Counter(), "unparsed": 0,
                     "avg_blocks": 0}
    sample_ids = [r["id"] for r in records[:args.sample] if r["id"]]
    if sample_ids:
        blocks_total = 0
        for page in client.get_pages_batch(sample_ids, expand="body.storage"):
            body = (((page.get("body") or {}).get("storage") or {}).get("value") or "")
            parsed = parse_storage(body)
            sample_report["checked"] += 1
            if not parsed.ok:
                sample_report["unparsed"] += 1
                continue
            blocks_total += len(parsed.blocks)
            for name, count in parsed.unsupported.items():
                sample_report["unsupported"][name] += count
        if sample_report["checked"]:
            sample_report["avg_blocks"] = round(blocks_total / sample_report["checked"], 1)

    report = {
        "space": args.space,
        "pages_total": len(pages),
        "seconds": round(elapsed, 1),
        "tech_like_titles": tech_like,
        "by_age": dict(ages),
        "top_authors": dict(authors.most_common(10)),
        "labels": dict(labels.most_common(20)),
        "sample": {"checked": sample_report["checked"],
                   "unparsed": sample_report["unparsed"],
                   "avg_blocks": sample_report["avg_blocks"],
                   "unsupported": dict(sample_report["unsupported"])},
        "pages": records,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8", newline="\n")

    print(f"\nСтраниц: {len(pages)} за {elapsed:.1f} с")
    print(f"Похоже на техническую часть по заголовку: {tech_like}")
    print("Возраст: " + ", ".join(f"{k} — {v}" for k, v in ages.items()))
    if sample_report["checked"]:
        print(f"Выборка {sample_report['checked']} страниц: "
              f"в среднем {sample_report['avg_blocks']} блоков, "
              f"не разобралось {sample_report['unparsed']}")
        if sample_report["unsupported"]:
            print("  неподдерживаемые конструкции: " + ", ".join(
                f"{k} ({v})" for k, v in sample_report["unsupported"].most_common()))
    print(f"\nОтчёт: {args.out}")
    print("\nЭтот отчёт — основание решить, нужен ли блок H целиком. "
          "Полсотни страниц с полутора десятками живых — это день ручной работы, "
          "а не конвертер с классификатором.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
