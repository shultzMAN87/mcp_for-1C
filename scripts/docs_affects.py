#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-3 / DOC-20. Затронутые объекты из спеки → очередь ревизии документов.

Ни одного фреймворка в коде нет и не будет
───────────────────────────────────────────
Контур завязан не на инструмент управления спеками и не на редактор, а на
одно поле в спеке — `affects`, список объектов метаданных, которых коснулась
задача. Что именно порождает спеку, скрипту безразлично: он читает файл и
достаёт из него список тремя способами, потому что три способа покрывают
почти любой формат, который встречается на практике.

Инструмент управления спеками — самая недолговечная часть процесса: его
меняют чаще, чем язык, редактор и трекер. Поэтому его имени здесь нет и
быть не должно.

  1. YAML-фронтматтер в markdown-файле:      ---\\naffects: [Catalog.X]\\n---
  2. Весь файл как YAML или JSON:            {"affects": ["Catalog.X"]}
  3. Раздел в markdown:                      ## Затронутые объекты
                                             - Catalog.X

Третий способ — для форматов, где фронтматтера нет вообще: заголовок ищется
по смыслу (affects / затронут / touches), дальше берутся пункты списка и
содержимое блока кода до следующего заголовка. Ключевое: НИ ОДИН из способов
не знает имени фреймворка. Появится четвёртый формат — сюда добавится
четвёртая функция, а всё остальное в контуре не шелохнётся.

Что делает дальше
─────────────────
Сверяет имена со словарём (DOC-38): имя вне словаря — ошибка, а не
замечание, иначе поле молча зарастает опечатками. Затем находит документы,
у которых `targets` пересекается с `affects`, и ставит их в очередь ревизии.

Definition of Done задачи формулируется не как «обновить документацию» — это
не проверяется и потому не делается, — а как «заполнить affects», что
проверяется машинно вот этой командой (R6).

Запуск:
    python scripts\\docs_affects.py спека.md
    python scripts\\docs_affects.py спеки\\*.md --queue
    python scripts\\docs_affects.py спека.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("Нужен pyyaml: pip install pyyaml\n")
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO_ROOT / "techdocs"
QUEUE_FILE = REPO_ROOT / "techdocs" / "_queue" / "review.jsonl"

AFFECTS_KEYS = ("affects", "затронутые_объекты", "touches")
# Заголовок раздела ищется по смыслу, а не по точному тексту: формулировка у
# всех разная, а слово одно из трёх.
SECTION_MARKERS = ("affects", "затронут", "touches")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ─── Три способа достать affects ─────────────────────────────────────────

def from_frontmatter(text: str) -> list[str] | None:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(normalized[4:end + 1])
    except yaml.YAMLError:
        return None
    return _pick(data)


def from_whole_file(text: str) -> list[str] | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return _pick(json.loads(stripped))
        except json.JSONDecodeError:
            return None
    try:
        return _pick(yaml.safe_load(stripped))
    except yaml.YAMLError:
        return None


def from_section(text: str) -> list[str] | None:
    lines = text.replace("\r\n", "\n").split("\n")
    collected: list[str] = []
    inside = False
    in_fence = False
    for line in lines:
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading and not in_fence:
            title = heading.group(1).casefold()
            inside = any(marker in title for marker in SECTION_MARKERS)
            continue
        if not inside:
            continue
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        item = re.match(r"^\s*[-*+]\s+(.+?)\s*$", line)
        if item:
            collected.append(item.group(1).strip("`\"' "))
            continue
        if in_fence:
            # Внутри блока кода допускаем и «affects:», и голый список.
            bare = line.strip().strip("`\"' ")
            if bare and not bare.endswith(":"):
                collected.append(bare)
    return collected or None


def _pick(data: Any) -> list[str] | None:
    if not isinstance(data, dict):
        return None
    for key in AFFECTS_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
    return None


def extract_affects(text: str) -> tuple[list[str], str]:
    """Возвращает (имена, каким способом достали)."""
    for reader, label in ((from_frontmatter, "фронтматтер"),
                          (from_whole_file, "файл целиком"),
                          (from_section, "раздел в markdown")):
        found = reader(text)
        if found:
            return found, label
    return [], "не найдено"


# ─── Сверка со словарём и очередь ────────────────────────────────────────

def load_names(path: Path) -> dict[str, str]:
    data = json.loads(read_text(path))
    aliases = {k.casefold(): v for k, v in (data.get("aliases") or {}).items()}
    for canon in data.get("canonical") or []:
        aliases.setdefault(canon.casefold(), canon)
    return aliases


def documents_with_targets(corpus: Path) -> list[tuple[Path, dict]]:
    result = []
    for type_dir in ("process", "http-service", "object"):
        base = corpus / type_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name.startswith("_"):
                continue
            raw = read_text(path).replace("\r\n", "\n")
            if not raw.startswith("---\n"):
                continue
            end = raw.find("\n---\n", 4)
            if end == -1:
                continue
            try:
                front = yaml.safe_load(raw[4:end + 1]) or {}
            except yaml.YAMLError:
                continue
            if isinstance(front, dict):
                result.append((path, front))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Затронутые объекты из спеки → очередь ревизии (DOC-20)")
    parser.add_argument("specs", nargs="+", help="файлы спек: .md, .yml, .yaml, .json")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--names", default=None, help="по умолчанию <corpus>/names.json")
    parser.add_argument("--queue", action="store_true",
                        help="дописать найденные документы в techdocs/_queue/review.jsonl")
    parser.add_argument("--json", action="store_true", help="вывод машине")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    names_path = Path(args.names) if args.names else corpus / "names.json"
    aliases = load_names(names_path) if names_path.is_file() else {}
    if not aliases:
        print(f"Словаря имён нет ({names_path}) — имена не проверяются. "
              f"Соберите: python scripts\\docs_names_dump.py")

    affected: list[str] = []
    unknown: list[str] = []
    per_spec: dict[str, dict] = {}

    for raw_path in args.specs:
        path = Path(raw_path)
        if not path.is_file():
            print(f"Нет файла: {path}")
            return 2
        names, how = extract_affects(read_text(path))
        canonized: list[str] = []
        for name in names:
            canon = aliases.get(name.casefold()) if aliases else name
            if canon is None:
                unknown.append(name)
                canonized.append(name)
            elif canon not in canonized:
                canonized.append(canon)
        per_spec[str(path)] = {"способ": how, "affects": canonized}
        for name in canonized:
            if name not in affected:
                affected.append(name)
        if not names:
            print(f"{path}: поле affects не найдено. Задача, которая ничего не "
                  f"затронула, — редкость; чаще поле просто не заполнили (R6).")

    matched = []
    for path, front in documents_with_targets(corpus):
        targets = [str(t) for t in (front.get("targets") or [])]
        hit = [t for t in targets if t in affected]
        if hit:
            matched.append({"документ": str(path.relative_to(corpus.parent)),
                            "id": front.get("id", ""),
                            "source": front.get("source", ""),
                            "по объектам": hit})

    if args.json:
        print(json.dumps({"специи": per_spec, "affects": affected,
                          "вне_словаря": unknown, "в_очередь": matched},
                         ensure_ascii=False, indent=2))
    else:
        for spec, info in per_spec.items():
            print(f"{spec}: {len(info['affects'])} объектов ({info['способ']})")
        if unknown:
            print(f"\nВне словаря имён: {', '.join(sorted(set(unknown)))}")
        print(f"\nДокументов в очередь ревизии: {len(matched)}")
        for item in matched:
            print(f"  {item['документ']} [{item['source']}] ← "
                  f"{', '.join(item['по объектам'])}")
        if not matched and affected:
            print("  ни один документ не покрывает эти объекты — "
                  "кандидат на заявку в очередь заявок")

    if args.queue and matched:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with QUEUE_FILE.open("a", encoding="utf-8", newline="\n") as fh:
            for item in matched:
                fh.write(json.dumps({"дата": date.today().isoformat(), **item},
                                    ensure_ascii=False) + "\n")
        print(f"\nДописано в {QUEUE_FILE}")

    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
