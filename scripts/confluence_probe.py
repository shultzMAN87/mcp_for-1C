#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-5. Пробник Confluence API. Закрывает Р-1 фактами, а не перепиской.

Отвечает на вопросы:
  - Cloud или Data Center, какая версия;
  - доступен ли REST извне и какая авторизация работает;
  - какой формат тела принимает и отдаёт;
  - как ведёт себя при конфликте версий;
  - плодит ли повторный прогон без изменений новую версию (R3);
  - доступна ли статистика просмотров (нужна для приоритизации, 6.6);
  - какие заголовки лимитов приходят (R19);
  - переживает ли кириллица заголовок и тело (R9).

Без --write не делает ни одного запроса на запись.

Запуск:
    python scripts\\confluence_probe.py --space TECHDOC
    python scripts\\confluence_probe.py --space TECHDOC --write
    python scripts\\confluence_probe.py --space TECHDOC --write --cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# B-3 / FAIL-2: защита потоков ровно в той форме, которую требует
# scripts/tests_host_scripts.py. Не encoding="utf-8": подмена кодировки дала
# бы кракозябры в cp1251-консоли, а замена — всего лишь «?» вместо галочки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from confluence_client import ConfluenceError, client_from_env  # noqa: E402

TEST_TITLE = "Пробник autodoc — можно удалить"
TEST_BODY = (
    "<p>Проверка кириллицы: Справочник.Контрагенты, обмен «заказами» — тире, кавычки.</p>"
    "<ac:structured-macro ac:name=\"code\" ac:schema-version=\"1\">"
    "<ac:parameter ac:name=\"language\">text</ac:parameter>"
    "<ac:plain-text-body><![CDATA[Процедура ОбменМП() КонецПроцедуры]]></ac:plain-text-body>"
    "</ac:structured-macro>"
)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Пробник Confluence (DOC-5, Р-1)")
    parser.add_argument("--space", required=True, help="ключ пространства для теста")
    parser.add_argument("--write", action="store_true",
                        help="разрешить создание и обновление тестовой страницы")
    parser.add_argument("--cleanup", action="store_true", help="удалить тестовую страницу в конце")
    parser.add_argument("--report", default="confluence_probe_report.json")
    args = parser.parse_args(argv)

    report: dict = {"space": args.space, "write_enabled": args.write}
    client = client_from_env(allow_write=args.write, verbose=True)

    section("Тип развёртывания (Р-1)")
    try:
        info = client.detect_deployment()
    except ConfluenceError as exc:
        print(f"REST недоступен: {exc}")
        print("Это ответ на Р-1: без доступа к REST извне вся конструкция не поедет.")
        return 1
    report["deployment"] = info
    for key, value in info.items():
        print(f"  {key}: {value}")

    section("Версия и системная информация")
    version_info = {}
    for path in ("/rest/applinks/1.0/manifest", "/rest/api/settings/systemInfo"):
        try:
            response = client.request("GET", path)
            version_info[path] = response.text[:400]
            print(f"  {path}: {response.status_code}")
        except ConfluenceError as exc:
            print(f"  {path}: недоступно ({str(exc)[:120]})")
    report["version_probe"] = version_info

    section("Пространство и объём (черновая оценка для DOC-30)")
    try:
        space = client.get_json(f"/rest/api/space/{args.space}")
        print(f"  {space.get('key')}: {space.get('name')}")
        report["space_name"] = space.get("name")
    except ConfluenceError as exc:
        print(f"  пространство не читается: {exc}")
        return 1

    started = time.monotonic()
    sample = list(client.search_cql(f"space = {args.space} and type = page",
                                    limit=100, max_pages=250))
    elapsed = time.monotonic() - started
    print(f"  прочитано {len(sample)} страниц за {elapsed:.1f} с пакетными запросами")
    report["sample_pages"] = len(sample)
    report["sample_seconds"] = round(elapsed, 1)

    section("Заголовки лимитов (R19)")
    if client.rate_log and client.rate_log.is_file():
        print(f"  журнал: {client.rate_log}")
        report["rate_log"] = str(client.rate_log)
    else:
        print("  сервер не прислал ни одного заголовка лимитов за прогон")
        report["rate_log"] = None

    section("Статистика просмотров (нужна для приоритизации 6.6)")
    views_available = False
    if sample:
        page_id = sample[0]["id"]
        for path in (f"/rest/api/analytics/content/{page_id}/views",
                     f"/rest/analytics/1.0/publicpage/viewsummary/{page_id}"):
            try:
                response = client.request("GET", path)
                print(f"  {path}: {response.status_code} {response.text[:120]}")
                views_available = True
                break
            except ConfluenceError as exc:
                print(f"  {path}: нет ({str(exc)[:100]})")
    report["views_available"] = views_available
    if not views_available:
        print("  вывод: приоритизировать только по графу, пункт 2 из 6.6 отпадает")

    if not args.write:
        section("Запись")
        print("  пропущена: нет флага --write. Ни одного запроса на запись не сделано.")
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")
        print(f"\nОтчёт: {args.report}")
        return 0

    section("Создание страницы с кириллицей (R9)")
    created = client.create_page(args.space, f"{TEST_TITLE} {int(time.time())}", TEST_BODY)
    page_id = created["id"]
    print(f"  создана page_id={page_id}, версия {created['version']['number']}")
    report["created_page_id"] = page_id

    fetched = client.get_page(page_id)
    body = fetched["body"]["storage"]["value"]
    print(f"  тело вернулось в representation={fetched['body']['storage']['representation']}")
    print(f"  кириллица уцелела: {'Контрагенты' in body}")
    print(f"  макрос кода уцелел: {'structured-macro' in body}")
    report["cyrillic_ok"] = "Контрагенты" in body
    report["macro_ok"] = "structured-macro" in body

    section("Повторная запись тем же телом (R3)")
    before = int(client.get_page(page_id)["version"]["number"])
    client.update_page(page_id, fetched["title"], body, message="пробник: то же тело")
    after = int(client.get_page(page_id)["version"]["number"])
    print(f"  версия {before} → {after}")
    print("  вывод: сервер сам версии не схлопывает, сравнение body_hash обязательно"
          if after > before else "  вывод: сервер не создал версию на идентичном теле")
    report["identical_write_bumps_version"] = after > before

    section("Конфликт версий (камень 1)")
    stale = client.get_page(page_id, expand="version")
    client.update_page(page_id, fetched["title"], body + "<p>правка 1</p>", message="правка 1")
    try:
        client.request("PUT", f"/rest/api/content/{page_id}", write=True, json={
            "id": page_id, "type": "page", "title": fetched["title"],
            "version": {"number": int(stale["version"]["number"]) + 1},
            "body": {"storage": {"value": body + "<p>правка 2</p>",
                                 "representation": "storage"}},
        })
        print("  устаревшая версия принята без ошибки — опасно, полагаться только на body_hash")
        report["stale_version_rejected"] = False
    except ConfluenceError as exc:
        print(f"  устаревшая версия отклонена: {str(exc)[:160]}")
        report["stale_version_rejected"] = True

    section("Метки (камень 15)")
    client.add_labels(page_id, ["autodoc", "probe"])
    labels = client.get_page(page_id, expand="metadata.labels")["metadata"]["labels"]["results"]
    print(f"  метки: {[l['name'] for l in labels]}")

    if args.cleanup:
        client.request("DELETE", f"/rest/api/content/{page_id}", write=True)
        print(f"\nТестовая страница {page_id} удалена.")
    else:
        print(f"\nТестовая страница {page_id} оставлена. Удалите руками или прогоните --cleanup.")

    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8", newline="\n")
    print(f"Отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
