#!/usr/bin/env python3
"""
STD-1: забор корпуса стандартов v8std для локального MCP-сервера.

Почему не `git clone`, как предполагал PLAN-STD §4
──────────────────────────────────────────────────
В репозитории zeegin/v8std каталог `docs/ai/` стоит в `.gitignore`. То есть
клон даёт markdown-корпус, но НЕ даёт `pages.jsonl` и `search-vectors.jsonl` —
а именно они и есть индекс, который читает MCP-сервер. Собрать их из клона
можно (`generate_ai_artifacts.py` + `generate_search_vectors.py`), но эти
генераторы тянут за собой Pillow, PyYAML, zensical и внутреннюю раскладку
их скриптов — то есть весь чужой тулчейн ради двух файлов, которые автор и
так публикует собранными.

Поэтому забираем ровно то, что нужно для запуска:
  - два артефакта индекса с v8std.ru;
  - пять python-модулей сервера + правила разбора сниппетов из репозитория.

Git на хосте не нужен, сеть нужна только здесь — контейнер потом работает
офлайн. Системный прокси подхватывается автоматически: urllib читает
HTTP_PROXY / HTTPS_PROXY из окружения.

Что получается на выходе (каталог ./v8std-data, он в .gitignore):

    v8std-data/
      zensical.toml            ← маркер корня: по нему их код находит правила
      retrieval-rules.yml      ← алиасы и сигнатуры вызовов для explain_snippet
      scripts/*.py             ← сам MCP-сервер (5 модулей)
      docs/ai/pages.jsonl      ← индекс страниц
      docs/ai/search-vectors.jsonl
      FETCH.json               ← ref, коммит, дата, sha256 и размер каждого файла

`FETCH.json` — это ответ на риск «актуальность» из PLAN-STD §6: по нему
всегда видно, насколько индекс отстал и чем именно он получен.

Использование:
    python3 scripts/fetch_v8std.py              # забрать/обновить
    python3 scripts/fetch_v8std.py --check      # только отчёт о состоянии
    python3 scripts/fetch_v8std.py --ref v1.2.3 # зафиксировать версию скриптов

Коды выхода:
    0 — всё на месте
    1 — не удалось скачать (сеть, прокси, 404 после переезда файла)
    2 — --check при отсутствующем или неполном каталоге
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "v8std-data"

REPO = "zeegin/v8std"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
API_COMMIT = "https://api.github.com/repos/{repo}/commits/{ref}"

# Артефакты индекса. В git их нет — берём опубликованные с сайта.
SITE_FILES = {
    "docs/ai/pages.jsonl": "https://v8std.ru/ai/pages.jsonl",
    "docs/ai/search-vectors.jsonl": "https://v8std.ru/ai/search-vectors.jsonl",
}

# Модули сервера. Список получен чтением импортов, а не догадкой:
# v8std_mcp_server → v8std_mcp_index → {v8std_retrieval_rules,
# v8std_search_features}, плюс atomic_files. Если автор переложит файл,
# скрипт упадёт с явным 404 и именем файла — молча деградировать нельзя.
REPO_FILES = [
    "scripts/v8std_mcp_server.py",
    "scripts/v8std_mcp_index.py",
    "scripts/v8std_retrieval_rules.py",
    "scripts/v8std_search_features.py",
    "scripts/atomic_files.py",
    "retrieval-rules.yml",
    "zensical.toml",
]

# Файлы, без которых сервер вообще не поднимется.
REQUIRED = [
    "scripts/v8std_mcp_server.py",
    "scripts/v8std_mcp_index.py",
    "scripts/v8std_retrieval_rules.py",
    "scripts/v8std_search_features.py",
    "scripts/atomic_files.py",
    "docs/ai/pages.jsonl",
]

USER_AGENT = "1c-mcp-suite/fetch_v8std (+https://github.com/zeegin/v8std)"
TIMEOUT = 60


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / 1024 / 1024:.2f} МБ"


def resolve_commit(ref: str) -> str:
    """Коммит, соответствующий ref. Не критично: при отказе пишем пусто."""
    try:
        raw = _get(API_COMMIT.format(repo=REPO, ref=ref))
        return json.loads(raw).get("sha", "")[:12]
    except Exception:
        return ""


def load_manifest() -> dict:
    path = TARGET / "FETCH.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def do_check() -> int:
    manifest = load_manifest()
    if not manifest:
        print("v8std-data/: пусто или нет FETCH.json.")
        print("Забрать корпус:  python3 scripts/fetch_v8std.py")
        return 2

    missing = [rel for rel in REQUIRED if not (TARGET / rel).is_file()]
    fetched_at = manifest.get("fetched_at", "")
    age = ""
    try:
        dt = datetime.fromisoformat(fetched_at)
        days = (datetime.now(timezone.utc) - dt).days
        age = f", возраст {days} дн."
    except Exception:
        pass

    print(f"Корпус v8std: ref={manifest.get('ref', '?')} "
          f"commit={manifest.get('commit') or '—'} от {fetched_at}{age}")
    print(f"Файлов в манифесте: {len(manifest.get('files', {}))}")
    if missing:
        print("НЕ ХВАТАЕТ (сервер не поднимется):")
        for rel in missing:
            print(f"  - {rel}")
        return 2
    print("Все обязательные файлы на месте.")
    return 0


def fetch(ref: str, force: bool) -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    old = load_manifest()
    old_files = old.get("files", {})

    jobs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in REPO_FILES:
        if rel in seen:
            continue
        seen.add(rel)
        jobs.append((rel, RAW.format(repo=REPO, ref=ref, path=rel)))
    for rel, url in SITE_FILES.items():
        jobs.append((rel, url))

    files: dict[str, dict] = {}
    changed: list[str] = []
    errors: list[str] = []

    for rel, url in jobs:
        dest = TARGET / rel
        try:
            data = _get(url)
        except urllib.error.HTTPError as exc:
            errors.append(f"{rel}: HTTP {exc.code} ({url})")
            continue
        except Exception as exc:
            errors.append(f"{rel}: {type(exc).__name__}: {exc} ({url})")
            continue

        digest = _sha256(data)
        was = old_files.get(rel, {}).get("sha256")
        if was != digest or force or not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            changed.append(rel)

        files[rel] = {"sha256": digest, "size": len(data), "url": url}
        print(f"  {'обновлён' if rel in changed else 'без изменений'}: "
              f"{rel} ({_fmt_size(len(data))})")

    hard_missing = [rel for rel in REQUIRED if rel not in files]
    if hard_missing:
        print("\nОШИБКА: не удалось получить обязательные файлы:", file=sys.stderr)
        for rel in hard_missing:
            print(f"  - {rel}", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print("\nЕсли у вас включён системный прокси (v2rayN и подобные), "
              "экспортируйте HTTP_PROXY/HTTPS_PROXY перед запуском.", file=sys.stderr)
        return 1

    manifest = {
        "repo": REPO,
        "ref": ref,
        "commit": resolve_commit(ref),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": "CC0-1.0 (No Rights Reserved)",
        "files": files,
    }
    (TARGET / "FETCH.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    pages = TARGET / "docs/ai/pages.jsonl"
    page_count = sum(1 for _ in pages.open(encoding="utf-8")) if pages.is_file() else 0

    print()
    print(f"Каталог:   {TARGET}")
    print(f"ref:       {ref}  commit: {manifest['commit'] or '—'}")
    print(f"Страниц в индексе: {page_count}")
    print(f"Изменилось файлов: {len(changed)} из {len(files)}")
    if errors:
        print("\nНеобязательные файлы не скачались (это не мешает запуску):")
        for err in errors:
            print(f"  {err}")
    print("\nДальше:  docker compose up -d v8std-mcp")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Забор корпуса стандартов v8std (STD-1).")
    ap.add_argument("--ref", default=os.environ.get("V8STD_REF", "main"),
                    help="Ветка или тег репозитория zeegin/v8std.")
    ap.add_argument("--force", action="store_true",
                    help="Перезаписать файлы даже при совпадении sha256.")
    ap.add_argument("--check", action="store_true",
                    help="Ничего не качать, только отчёт о состоянии каталога.")
    args = ap.parse_args()

    if args.check:
        return do_check()
    return fetch(args.ref, args.force)


if __name__ == "__main__":
    sys.exit(main())
