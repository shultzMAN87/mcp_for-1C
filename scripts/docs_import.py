#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-31 / DOC-32 / DOC-37. Импорт страниц Confluence и карточки для человека.

Это миграция, а не синхронизация. Страницу читают один раз, кладут в
репозиторий, дальше единственный источник правды — git. Возврата к чтению из
Confluence по этой странице больше нет.

Что делает прогон
─────────────────
  1. Читает тела страниц пакетами (R19).
  2. Кладёт ИСХОДНИК дословно в techdocs/_imported/<page_id>.txt. Это R15:
     что бы дальше ни случилось с черновиком, оригинал лежит рядом.
  3. Разбирает storage format через AST (DOC-31). Всё, что не разобралось,
     перечисляется поимённо — молча не теряется ничего.
  4. Вытаскивает targets по словарю имён (DOC-32), регистронезависимо, во
     всех трёх формах.
  5. Складывает карточку: что подтверждено, что расходится, что сохранить
     дословно (DOC-33).
  6. Забирает комментарии страницы (DOC-37) — в них часто лежит «сейчас на
     самом деле не так», и это честнейший источник в пространстве.

Чего прогон НЕ делает: не пишет в Confluence (клиент открыт только на
чтение), не создаёт черновиков документов, не удаляет ничего. Черновик —
следующий шаг, и он делается под присмотром человека.

Запуск:
    python scripts\\docs_import.py --space TECHDOC --limit 20
    python scripts\\docs_import.py --pages 3948112,3948113 --graph
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from confluence_client import ConfluenceError, client_from_env  # noqa: E402
from confluence_storage import parse_storage  # noqa: E402
from docs_classify import (Names, build_card, card_to_json,  # noqa: E402
                           render_card, suggest_verdict)

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORTED = REPO_ROOT / "techdocs" / "_imported"
DEFAULT_NAMES = REPO_ROOT / "techdocs" / "names.json"

# Рёбра графа, по которым проверяются заявления о связях. Направление не
# используется: по OPERATES_ON оно надёжно не восстанавливается (3.2),
# сверяется множество объектов.
QUERY_EDGES = """
MATCH (a:MetadataObject)-[r]-(b:MetadataObject)
WHERE type(r) IN ['OPERATES_ON', 'CALLS', 'REFERENCES']
RETURN a.full_name_eng AS a, b.full_name_eng AS b
"""


def load_edges() -> set[tuple[str, str]] | None:
    """Пары связанных объектов из графа. None — граф не спрашивали."""
    try:
        from docs_names_dump import neo4j_rows
        rows = neo4j_rows(QUERY_EDGES)
    except SystemExit as exc:
        print(f"Граф недоступен ({exc}). Связи не проверяются, "
              f"существование объектов — да.")
        return None
    edges = set()
    for row in rows:
        left, right = row.get("a"), row.get("b")
        if left and right:
            edges.add((left, right))
    print(f"Из графа взято пар связанных объектов: {len(edges)}")
    return edges


def page_meta(page: dict) -> dict:
    version = page.get("version") or {}
    return {
        "id": str(page.get("id", "")),
        "title": page.get("title", ""),
        "updated": version.get("when", ""),
        "author": ((version.get("by") or {}).get("displayName")
                   or (version.get("by") or {}).get("username") or ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт страниц и карточки (блок H)")
    parser.add_argument("--space", default=None, help="импортировать пространство целиком")
    parser.add_argument("--pages", default=None, help="список page_id через запятую")
    parser.add_argument("--limit", type=int, default=20,
                        help="потолок страниц за прогон (R18: 20 по умолчанию)")
    parser.add_argument("--names", default=str(DEFAULT_NAMES))
    parser.add_argument("--graph", action="store_true",
                        help="спросить у Neo4j связи для проверки утверждений")
    parser.add_argument("--comments", action="store_true", default=True,
                        help="забирать комментарии страниц (DOC-37)")
    args = parser.parse_args(argv)

    if not args.space and not args.pages:
        parser.error("укажите --space или --pages")

    names_path = Path(args.names)
    if not names_path.is_file():
        print(f"Нет словаря имён {names_path}. Соберите: "
              f"python scripts\\docs_names_dump.py")
        return 2
    names = Names.load(names_path)

    edges = load_edges() if args.graph else None
    client = client_from_env(allow_write=False, verbose=True)
    IMPORTED.mkdir(parents=True, exist_ok=True)

    if args.pages:
        page_ids = [p.strip() for p in args.pages.split(",") if p.strip()]
    else:
        print(f"Ищу страницы в {args.space} (потолок {args.limit})")
        page_ids = [str(p["id"]) for p in client.search_cql(
            f"space = {args.space} and type = page", limit=100,
            expand="version", max_pages=args.limit)]
    page_ids = page_ids[:args.limit]
    if not page_ids:
        print("Страниц не нашлось.")
        return 0

    summary = []
    for page in client.get_pages_batch(page_ids, expand="body.storage,version"):
        meta = page_meta(page)
        body = (((page.get("body") or {}).get("storage") or {}).get("value") or "")

        # R15: исходник ложится на диск ДО любого разбора.
        raw_path = IMPORTED / f"{meta['id']}.txt"
        raw_path.write_text(body, encoding="utf-8", newline="\n")

        parsed = parse_storage(body)
        card = build_card(meta, parsed, names, edges)

        if args.comments:
            try:
                comments = client.get_comments(meta["id"])
            except ConfluenceError as exc:
                comments = []
                print(f"  комментарии {meta['id']} не прочитались: {exc}")
            if comments:
                # DOC-37: в комментариях лежит «сейчас на самом деле не так».
                # Кладём рядом с исходником, в карточку не подмешиваем: это
                # отдельный источник, и человек должен видеть его отдельно.
                lines = []
                for comment in comments:
                    text = parse_storage(
                        (((comment.get("body") or {}).get("storage") or {}).get("value") or "")
                    ).text
                    if text.strip():
                        lines.append(text.strip())
                if lines:
                    (IMPORTED / f"{meta['id']}.comments.txt").write_text(
                        "\n\n---\n\n".join(lines), encoding="utf-8", newline="\n")

        card_json = IMPORTED / f"{meta['id']}.card.json"
        card_json.write_text(json.dumps(card_to_json(card), ensure_ascii=False, indent=2),
                             encoding="utf-8", newline="\n")
        (IMPORTED / f"{meta['id']}.card.txt").write_text(
            render_card(card), encoding="utf-8", newline="\n")

        counts = card.counts()
        summary.append((meta["id"], meta["title"], counts, suggest_verdict(card)))
        print(f"  {meta['id']} {meta['title'][:50]}: "
              f"утверждений {len(card.statements)}, "
              f"не проверяемо {counts['не проверяемо']}")

    print(f"\nРазобрано страниц: {len(summary)}. Записей в Confluence: 0.")
    print(f"Карточки и исходники: {IMPORTED}")
    print("\nПодсказки по вердиктам (решает человек):")
    for page_id, title, counts, verdict in summary:
        print(f"  {page_id} {title[:45]:45} → {verdict}")
    print("\nДальше карточки смотрит человек. Классификатор врёт — публикацию "
          "не включать, пока не починено (6.8).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
