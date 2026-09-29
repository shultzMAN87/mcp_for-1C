#!/usr/bin/env python3
"""
HELP-COPY: копирование справки платформы 1С (.hbk) в ./platform-help-data.

Зачем отдельный скрипт
──────────────────────
Раньше шаг описывался словами «скопируйте содержимое bin», и на нём
ошибались тремя способами: копировали весь bin (английские *_root.hbk
раздували каталог вдвое, а индексатор их всё равно отбрасывает),
копировали не из той версии платформы или не копировали вовсе — и
platform-help-data оставался пустым, а справка молча не индексировалась.

Берём только русскую справку — файлы *_ru.hbk. Индексатор по умолчанию
(HBK_INDEX_LANG=ru) читает ровно их.

Откуда берём (по убыванию приоритета)
─────────────────────────────────────
  1. ключ --src;
  2. переменная ONEC_BIN_DIR — из окружения оболочки, затем из .env;
  3. автопоиск: самая новая версия в
       %ProgramFiles%\\1cv8\\<версия>\\bin
       %ProgramFiles(x86)%\\1cv8\\<версия>\\bin

В --src и ONEC_BIN_DIR можно указать и каталог bin, и каталог версии
(…\\1cv8\\8.3.27.1606), и корень …\\1cv8 — тогда берётся самая новая версия.

Время изменения файлов сохраняется (copy2). Это важно: индексатор считает
fingerprint по имени, размеру и mtime, и повторное копирование тех же
файлов не должно вызывать двухчасовую переиндексацию.

Рядом кладётся SOURCE.json — откуда и из какой версии платформы взята
справка. Каталог platform-help-data в .gitignore, файл никуда не уходит.

Использование (Windows):
    python scripts\\fetch_platform_help.py                 # скопировать
    python scripts\\fetch_platform_help.py --dry-run       # показать план
    python scripts\\fetch_platform_help.py --list          # найденные версии
    python scripts\\fetch_platform_help.py --clean         # + удалить лишние .hbk
    python scripts\\fetch_platform_help.py --src "C:\\Program Files\\1cv8\\8.3.27.1606\\bin"

Коды выхода:
    0 — всё скопировано (или уже актуально)
    1 — не найден каталог платформы или в нём нет *_ru.hbk
    2 — часть файлов скопировать не удалось
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# B-3/FAIL-2: печать не должна ронять скрипт в cp1251-консоли.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "platform-help-data"
ENV_VAR = "ONEC_BIN_DIR"
PATTERN = "*_ru.hbk"
SOURCE_FILE = "SOURCE.json"

VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


# ─── .env ────────────────────────────────────────────────────────────────────

def read_env_value(name: str) -> str:
    """Значение из окружения, иначе из .env. Формат .env — как в check_prereqs."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = ROOT / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        return value
    return ""


# ─── Поиск каталога платформы ────────────────────────────────────────────────

def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in name.split("."))


def _program_files_roots() -> list[Path]:
    roots: list[Path] = []
    for var, fallback in (("ProgramFiles", r"C:\Program Files"),
                          ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        base = os.environ.get(var) or fallback
        p = Path(base) / "1cv8"
        if p not in roots:
            roots.append(p)
    return roots


def list_versions(onec_root: Path) -> list[tuple[str, Path]]:
    """Версии в каталоге …\\1cv8, у которых в bin есть русская справка."""
    found: list[tuple[str, Path]] = []
    if not onec_root.is_dir():
        return found
    for d in onec_root.iterdir():
        if not d.is_dir() or not VERSION_RE.match(d.name):
            continue
        bin_dir = d / "bin"
        if bin_dir.is_dir() and any(bin_dir.glob(PATTERN)):
            found.append((d.name, bin_dir))
    found.sort(key=lambda t: _version_key(t[0]), reverse=True)
    return found


def resolve_bin(raw: str) -> tuple[Path | None, str]:
    """
    Приводит указанный путь к каталогу bin. Возвращает (путь, пояснение);
    путь None — если ничего подходящего нет.
    """
    p = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not p.exists():
        return None, f"каталог не существует: {p}"
    if any(p.glob(PATTERN)):
        return p, "указан каталог bin"
    if (p / "bin").is_dir() and any((p / "bin").glob(PATTERN)):
        return p / "bin", "указан каталог версии, взят его bin"
    versions = list_versions(p)
    if versions:
        name, bin_dir = versions[0]
        return bin_dir, f"указан корень 1cv8, взята самая новая версия {name}"
    return None, f"в {p} нет файлов {PATTERN} (ни в самом каталоге, ни в bin)"


def autodetect() -> tuple[Path | None, str]:
    candidates: list[tuple[str, Path]] = []
    for root in _program_files_roots():
        candidates += list_versions(root)
    if not candidates:
        roots = ", ".join(str(r) for r in _program_files_roots())
        return None, f"платформа 1С не найдена ({roots})"
    candidates.sort(key=lambda t: _version_key(t[0]), reverse=True)
    name, bin_dir = candidates[0]
    return bin_dir, f"автопоиск: самая новая версия {name}"


def version_of(bin_dir: Path) -> str:
    parent = bin_dir.parent.name if bin_dir.name.lower() == "bin" else bin_dir.name
    return parent if VERSION_RE.match(parent) else "неизвестна"


# ─── Копирование ─────────────────────────────────────────────────────────────

def _same(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return False
    s, d = src.stat(), dst.stat()
    return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) < 2


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} МБ"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Копирует русскую справку платформы 1С (*_ru.hbk) в platform-help-data.")
    ap.add_argument("--src", help="каталог bin платформы (или каталог версии, или корень 1cv8)")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="куда копировать")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    ap.add_argument("--clean", action="store_true",
                    help="удалить из dest .hbk, которых нет в источнике (в т.ч. *_root.hbk)")
    ap.add_argument("--list", action="store_true", help="показать найденные версии платформы и выйти")
    args = ap.parse_args()

    if args.list:
        any_found = False
        for root in _program_files_roots():
            for name, bin_dir in list_versions(root):
                any_found = True
                n = len(list(bin_dir.glob(PATTERN)))
                print(f"  {name:<16} {bin_dir}  ({n} файлов {PATTERN})")
        if not any_found:
            print("Установленных версий платформы с русской справкой не найдено.")
        return 0

    # 1. Источник
    if args.src:
        src, why = resolve_bin(args.src)
        origin = "--src"
    elif read_env_value(ENV_VAR):
        src, why = resolve_bin(read_env_value(ENV_VAR))
        origin = f"{ENV_VAR} (.env / окружение)"
    else:
        src, why = autodetect()
        origin = "автопоиск"

    if src is None:
        print(f"✗ Источник справки не найден [{origin}]: {why}")
        print()
        print("Укажите каталог bin платформы одним из способов:")
        print(f"  • в .env:  {ENV_VAR}=C:\\Program Files\\1cv8\\8.3.27.1606\\bin")
        print("  • ключом: python scripts\\fetch_platform_help.py --src \"C:\\Program Files\\1cv8\\8.3.27.1606\\bin\"")
        print("Установленные версии: python scripts\\fetch_platform_help.py --list")
        return 1

    files = sorted(src.glob(PATTERN))
    dest = Path(args.dest)
    version = version_of(src)

    print(f"Источник:   {src}")
    print(f"            {why} [{origin}]")
    print(f"Платформа:  {version}")
    print(f"Назначение: {dest}")
    print(f"Файлов {PATTERN}: {len(files)}, всего {_mb(sum(f.stat().st_size for f in files))}")
    print("─" * 70)

    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    copied = skipped = errors = 0
    for f in files:
        target = dest / f.name
        if _same(f, target):
            skipped += 1
            continue
        action = "обновить" if target.exists() else "скопировать"
        if args.dry_run:
            print(f"  → {action:<12} {f.name:<24} {_mb(f.stat().st_size):>9}")
            copied += 1
            continue
        try:
            shutil.copy2(f, target)
            print(f"  ✓ {action:<12} {f.name:<24} {_mb(f.stat().st_size):>9}")
            copied += 1
        except OSError as e:
            print(f"  ✗ {f.name}: {e}")
            errors += 1

    # Лишние .hbk в назначении: английские *_root.hbk, файлы старой версии.
    wanted = {f.name.lower() for f in files}
    extra = sorted(p for p in dest.glob("*.hbk") if p.name.lower() not in wanted) if dest.is_dir() else []
    removed = 0
    if extra:
        if args.clean and not args.dry_run:
            for p in extra:
                try:
                    p.unlink()
                    print(f"  ✗ удалён     {p.name}")
                    removed += 1
                except OSError as e:
                    print(f"  ⚠ не удалось удалить {p.name}: {e}")
        else:
            verb = "будет удалено" if args.clean else "лишних .hbk (не из источника)"
            print(f"  ⚠ {verb}: {len(extra)} — {', '.join(p.name for p in extra[:6])}"
                  + (" …" if len(extra) > 6 else ""))
            if not args.clean:
                print("    Убрать их: добавьте ключ --clean")

    print("─" * 70)
    if args.dry_run:
        print(f"План: скопировать/обновить {copied}, уже актуальны {skipped}. Ничего не изменено (--dry-run).")
        return 0

    info = {
        "platform_version": version,
        "source": str(src),
        "pattern": PATTERN,
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {f.name: f.stat().st_size for f in files},
    }
    try:
        (dest / SOURCE_FILE).write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"⚠ Не удалось записать {SOURCE_FILE}: {e}")

    print(f"Итого: скопировано {copied}, уже актуальны {skipped}, удалено {removed}, ошибок {errors}")

    if copied:
        print()
        print("Дальше:")
        print("  • Индекса справки ещё нет — он соберётся сам при `docker compose up -d`.")
        print("  • Индекс уже есть (справка обновлена) — пересобрать его явно,")
        print("    по умолчанию индексатор непустую коллекцию не трогает:")
        print("      docker compose run --rm -e REINDEX_MODE=if_files_changed help-indexer")
        print("    Это 1,5–2 часа на CPU.")
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
