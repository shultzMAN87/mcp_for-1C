#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DOC-1. Валидатор корпуса технической документации (techdocs/).

Проверяет то, что перечислено в критерии приёмки DOC-1:
  - отсутствие обязательных полей фронтматтера (JSON Schema);
  - имя в targets вне словаря имён (techdocs/names.json, DOC-38);
  - дубль id по корпусу;
  - неизвестный type;
  - неуникальный title среди publish: true (R10);
  - правку документа с source: reviewed вне ветки ревизии (Р-5 вариант A, DOC-42).

Корпус лежит в techdocs/, а не в docs/: каталог docs/ в этом репозитории уже
занят документацией самого набора, и его состав стережёт scripts/tests_docs_canon.py.

Работает при остановленном Neo4j: словарь имён берётся из файла, а не из графа.
Ненулевой код возврата при любой ошибке — годится для run_all_tests.py (DOC-4).

Запуск:
    python scripts\\docs_validate.py
    python scripts\\docs_validate.py --fix
    python scripts\\docs_validate.py --base origin/main
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("Нужен pyyaml: pip install pyyaml\n")
    raise SystemExit(2)

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    sys.stderr.write("Нужен jsonschema: pip install jsonschema\n")
    raise SystemExit(2)


class _Loader(yaml.SafeLoader):
    """Даты держим строками: updated по схеме — строка, а PyYAML сам приводит
    2026-08-30 к datetime.date и валидатор ругался бы на собственный эталон."""


class _Dumper(yaml.SafeDumper):
    """Симметрично: при --fix дата не должна обрасти кавычками."""

    def increase_indent(self, flow=False, indentless=False):  # noqa: D102
        return super().increase_indent(flow, False)


for _resolvers in _Loader.yaml_implicit_resolvers.values():
    _resolvers[:] = [(tag, regexp) for tag, regexp in _resolvers
                     if tag != "tag:yaml.org,2002:timestamp"]
for _resolvers in _Dumper.yaml_implicit_resolvers.values():
    _resolvers[:] = [(tag, regexp) for tag, regexp in _resolvers
                     if tag != "tag:yaml.org,2002:timestamp"]


# R9: кириллица в выводе не должна ронять прогон под Windows.
# B-3 / FAIL-2: защита потоков ровно в той форме, которую требует
# scripts/tests_host_scripts.py. Не encoding="utf-8": подмена кодировки дала
# бы кракозябры в cp1251-консоли, а замена — всего лишь «?» вместо галочки.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCS = REPO_ROOT / "techdocs"
DEFAULT_SCHEMA = REPO_ROOT / "schema" / "docs-frontmatter.schema.json"
DOC_TYPES = ("process", "http-service", "object")
REVISION_BRANCH_PREFIX = "docs/rev-"


@dataclass
class Problem:
    path: Path
    message: str
    level: str = "error"  # error | warning

    def render(self, root: Path) -> str:
        try:
            where = self.path.relative_to(root.parent)
        except ValueError:
            where = self.path
        mark = "ОШИБКА " if self.level == "error" else "предупр."
        return f"{mark} {where}: {self.message}"


@dataclass
class Document:
    path: Path
    front: dict[str, Any]
    body: str
    front_raw: str
    newline: str = "\n"
    problems: list[Problem] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.front.get("id", ""))

    @property
    def publish(self) -> bool:
        explicit = self.front.get("publish")
        if isinstance(explicit, bool):
            return explicit
        # По умолчанию: object живёт только в MCP (3.4), остальное едет в Confluence.
        return self.front.get("type") != "object"


# --------------------------------------------------------------------------
# Чтение файлов
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """utf-8, BOM терпим на входе (R9), на выходе не пишем."""
    return path.read_text(encoding="utf-8-sig")


def split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any] | None, str, str, str]:
    newline = "\r\n" if "\r\n" in raw else "\n"
    normalized = raw.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return None, "", "", newline
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return None, "", "", newline
    front_raw = normalized[4:end + 1]
    body = normalized[end + 5:]
    try:
        front = yaml.load(front_raw, Loader=_Loader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"фронтматтер не разбирается как YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise ValueError("фронтматтер должен быть отображением ключ-значение")
    return front, body, front_raw, newline


def iter_doc_files(docs_root: Path) -> Iterable[Path]:
    """Обходим только каталоги типов. _proposed обходим отдельно, служебное пропускаем."""
    for type_dir in DOC_TYPES:
        base = docs_root / type_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name.startswith("_"):
                continue  # _TEMPLATE.md и прочее служебное
            yield path
    proposed = docs_root / "_proposed"
    if proposed.is_dir():
        for path in sorted(proposed.rglob("*.md")):
            if path.name.startswith("_TEMPLATE"):
                continue
            yield path


# --------------------------------------------------------------------------
# Словарь имён (DOC-38)
# --------------------------------------------------------------------------

class NameDictionary:
    """Три формы имени (FIX-18) → канон full_name_eng.

    Своей нормализации здесь нет намеренно: словарь строит docs_names_dump.py,
    валидатор только ищет по нему. Сравнение регистронезависимое (камень 11).
    """

    def __init__(self, aliases: dict[str, str], procedures: dict[str, str], meta: dict[str, Any]):
        self._aliases = aliases
        self._procedures = procedures
        self.meta = meta

    @classmethod
    def load(cls, path: Path) -> "NameDictionary":
        data = json.loads(read_text(path))
        aliases = {k.casefold(): v for k, v in (data.get("aliases") or {}).items()}
        for canon in data.get("canonical") or []:
            aliases.setdefault(canon.casefold(), canon)
        procedures = {k.casefold(): v for k, v in (data.get("procedures") or {}).items()}
        return cls(aliases, procedures, data.get("meta") or {})

    @classmethod
    def empty(cls) -> "NameDictionary":
        return cls({}, {}, {})

    @property
    def is_empty(self) -> bool:
        return not self._aliases

    def canon(self, name: str) -> str | None:
        return self._aliases.get(name.strip().casefold())

    def is_known_procedure(self, name: str) -> bool:
        return name.strip().casefold() in self._procedures


# --------------------------------------------------------------------------
# Git-контур (Р-5 вариант A, DOC-42)
# --------------------------------------------------------------------------

def git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout


def current_branch() -> str | None:
    out = git("rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if out else None


def changed_files(base: str | None) -> set[Path] | None:
    """Изменённые .md: рабочее дерево + индекс, плюс дифф от base, если задан."""
    status = git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        return None
    changed: set[Path] = set()
    for line in status.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        if " -> " in rel:  # переименование
            rel = rel.split(" -> ", 1)[1]
        changed.add((REPO_ROOT / rel).resolve())
    if base:
        diff = git("diff", "--name-only", f"{base}...HEAD")
        if diff is None:
            diff = git("diff", "--name-only", base)
        for rel in (diff or "").splitlines():
            if rel.strip():
                changed.add((REPO_ROOT / rel.strip()).resolve())
    return changed


# --------------------------------------------------------------------------
# Проверки
# --------------------------------------------------------------------------

def check_document(doc: Document, validator: Draft202012Validator, names: NameDictionary,
                   fix: bool) -> bool:
    """Возвращает True, если фронтматтер был изменён режимом --fix."""
    changed = False

    for err in sorted(validator.iter_errors(doc.front), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(корень)"
        doc.problems.append(Problem(doc.path, f"схема: {where}: {err.message}"))

    # Имена в targets: канон full_name_eng, на входе терпим три формы (3.1).
    targets = doc.front.get("targets")
    if isinstance(targets, list):
        canonized: list[str] = []
        for raw_name in targets:
            if not isinstance(raw_name, str):
                canonized.append(raw_name)
                continue
            name = raw_name.strip()
            if names.is_empty:
                canonized.append(name)
                continue
            canon = names.canon(name)
            if canon is None:
                if names.is_known_procedure(name):
                    canonized.append(name)
                    continue
                if "." in name and name.count(".") >= 2:
                    # Похоже на процедуру: словарь ведётся по объектам, поэтому предупреждение.
                    doc.problems.append(Problem(
                        doc.path,
                        f"targets: '{name}' не найдено в словаре; похоже на процедуру — не проверяется",
                        level="warning",
                    ))
                    canonized.append(name)
                    continue
                doc.problems.append(Problem(
                    doc.path, f"targets: имя '{name}' вне словаря имён (DOC-38)"))
                canonized.append(name)
                continue
            if canon != name:
                if fix:
                    changed = True
                else:
                    doc.problems.append(Problem(
                        doc.path,
                        f"targets: '{name}' не канон, канон — '{canon}'. Чинится --fix"))
            canonized.append(canon)
        if fix and changed:
            doc.front["targets"] = canonized

    # fp обязан описывать только то, что есть в targets.
    fp = doc.front.get("fp")
    if isinstance(fp, dict) and isinstance(targets, list):
        known = {t.strip().casefold() for t in targets if isinstance(t, str)}
        if not names.is_empty:
            known |= {(names.canon(t) or t).casefold() for t in targets if isinstance(t, str)}
        for key in fp:
            if key.casefold() not in known:
                doc.problems.append(Problem(doc.path, f"fp: ключ '{key}' отсутствует в targets"))

    # _proposed — черновики, там не бывает reviewed (Р-5, DOC-42).
    if "_proposed" in doc.path.parts and doc.front.get("source") == "reviewed":
        doc.problems.append(Problem(
            doc.path, "документ в _proposed не может иметь source: reviewed"))

    # Тип должен совпадать с каталогом.
    doc_type = doc.front.get("type")
    if doc_type in DOC_TYPES and "_proposed" not in doc.path.parts:
        if doc.path.parent.name != doc_type:
            doc.problems.append(Problem(
                doc.path, f"type: '{doc_type}', а файл лежит в '{doc.path.parent.name}/'"))

    # Тело не должно быть пустым: документ без содержания проходит схему, но бесполезен.
    if len(doc.body.strip()) < 40:
        doc.problems.append(Problem(doc.path, "тело документа пустое или короче 40 символов"))

    return changed


def check_corpus(docs: list[Document]) -> list[Problem]:
    problems: list[Problem] = []

    by_id: dict[str, list[Document]] = {}
    for doc in docs:
        if doc.id:
            by_id.setdefault(doc.id, []).append(doc)
    for doc_id, group in by_id.items():
        if len(group) > 1:
            where = ", ".join(str(d.path.name) for d in group)
            for doc in group:
                problems.append(Problem(doc.path, f"дубль id '{doc_id}' (файлы: {where})"))

    # R10: заголовки уникальны в пределах пространства Confluence.
    by_title: dict[str, list[Document]] = {}
    for doc in docs:
        if doc.publish and isinstance(doc.front.get("title"), str):
            by_title.setdefault(doc.front["title"].strip().casefold(), []).append(doc)
    for title, group in by_title.items():
        if len(group) > 1:
            where = ", ".join(str(d.path.name) for d in group)
            for doc in group:
                problems.append(Problem(
                    doc.path, f"неуникальный title среди publish: true (файлы: {where})"))

    # page_id не может быть занят двумя документами: публикация затрёт друг друга.
    by_page: dict[str, list[Document]] = {}
    for doc in docs:
        page_id = (doc.front.get("confluence") or {}).get("page_id")
        if page_id:
            by_page.setdefault(str(page_id), []).append(doc)
    for page_id, group in by_page.items():
        if len(group) > 1:
            for doc in group:
                problems.append(Problem(doc.path, f"page_id {page_id} указан в нескольких документах"))

    return problems


def check_reviewed_edits(docs: list[Document], base: str | None) -> list[Problem]:
    """Правка reviewed вне ветки ревизии — ошибка (Р-5 вариант A)."""
    branch = current_branch()
    if branch is None:
        return [Problem(REPO_ROOT, "git недоступен, проверка ветки ревизии пропущена", "warning")]
    if branch.startswith(REVISION_BRANCH_PREFIX):
        return []
    changed = changed_files(base)
    if changed is None:
        return [Problem(REPO_ROOT, "git status не отработал, проверка ветки пропущена", "warning")]
    problems = []
    for doc in docs:
        if doc.front.get("source") != "reviewed":
            continue
        if doc.path.resolve() in changed:
            problems.append(Problem(
                doc.path,
                f"документ source: reviewed изменён вне ветки ревизии (ветка '{branch}', "
                f"ожидалась {REVISION_BRANCH_PREFIX}<дата>). Черновики кладутся в techdocs/_proposed/",
            ))
    return problems


# --------------------------------------------------------------------------
# --fix
# --------------------------------------------------------------------------

def rewrite(doc: Document) -> None:
    """Перезаписывает фронтматтер каноном. Комментарии во фронтматтере теряются."""
    dumped = yaml.dump(
        doc.front, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=4096)
    text = f"---\n{dumped}---\n{doc.body}"
    if doc.newline != "\n":
        text = text.replace("\n", doc.newline)
    doc.path.write_text(text, encoding="utf-8", newline="")


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Валидатор корпуса docs/ (DOC-1)")
    parser.add_argument("--root", default=str(DEFAULT_DOCS), help="корень корпуса, по умолчанию techdocs/")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--names", default=None, help="по умолчанию <root>/names.json")
    parser.add_argument("--fix", action="store_true",
                        help="привести targets к канону full_name_eng и сохранить")
    parser.add_argument("--base", default=None,
                        help="git-ref для диффа при проверке правок reviewed, например origin/main")
    parser.add_argument("--no-git", action="store_true", help="не проверять ветку ревизии")
    parser.add_argument("--quiet", action="store_true", help="печатать только ошибки")
    args = parser.parse_args(argv)

    docs_root = Path(args.root).resolve()
    if not docs_root.is_dir():
        sys.stderr.write(f"Каталог не найден: {docs_root}\n")
        return 2

    schema_path = Path(args.schema).resolve()
    if not schema_path.is_file():
        sys.stderr.write(f"Схема не найдена: {schema_path}\n")
        return 2
    validator = Draft202012Validator(json.loads(read_text(schema_path)))

    names_path = Path(args.names) if args.names else docs_root / "names.json"
    problems: list[Problem] = []
    if names_path.is_file():
        names = NameDictionary.load(names_path)
    else:
        names = NameDictionary.empty()
        problems.append(Problem(
            names_path,
            "словарь имён не найден, проверка targets пропущена. Соберите: "
            "python scripts\\docs_names_dump.py",
            level="warning",
        ))

    docs: list[Document] = []
    fixed: list[Path] = []
    for path in iter_doc_files(docs_root):
        raw = read_text(path)
        try:
            front, body, front_raw, newline = split_frontmatter(raw, path)
        except ValueError as exc:
            problems.append(Problem(path, str(exc)))
            continue
        if front is None:
            problems.append(Problem(path, "нет фронтматтера: файл должен начинаться с '---'"))
            continue
        doc = Document(path=path, front=front, body=body, front_raw=front_raw, newline=newline)
        if check_document(doc, validator, names, args.fix):
            rewrite(doc)
            fixed.append(path)
        docs.append(doc)
        problems.extend(doc.problems)

    problems.extend(check_corpus(docs))
    if not args.no_git:
        problems.extend(check_reviewed_edits(docs, args.base))

    errors = [p for p in problems if p.level == "error"]
    warnings = [p for p in problems if p.level == "warning"]

    for problem in errors:
        print(problem.render(docs_root))
    if not args.quiet:
        for problem in warnings:
            print(problem.render(docs_root))
        for path in fixed:
            print(f"исправлено {path.name}: targets приведены к канону")

    print(f"\nДокументов: {len(docs)}. Ошибок: {len(errors)}. Предупреждений: {len(warnings)}.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
