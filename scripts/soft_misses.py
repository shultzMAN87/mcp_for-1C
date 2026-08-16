#!/usr/bin/env python3
"""
Какие soft-предикаты промахнулись и почему.
===========================================

Зачем
─────
Приёмка 16 августа: hard 100% во всех пяти датасетах, а soft — 8/9, 11/13,
7/9. Пять промахов, из которых объяснён один (`mg-002`: ошибка в ожидании,
не в инструменте). Остальные четыре прожили незамеченными несколько недель:
сводка `eval_all.py` печатает только числа, а имена промахнувшихся
предикатов лежат в `.md`-отчёте, куда без повода не заглядывают.

Число без имени — не измерение, а тревожная лампочка без надписи.

Что делает
──────────
Читает СОХРАНЁННЫЕ отчёты (`evals/reports/*.json`) и печатает по каждому
непройденному soft-предикату: пример, инструмент, тип предиката, аргументы
и `detail` — почему не сошлось. Живой стенд не нужен, прогон не нужен:
ответы уже записаны в отчёт.

Soft-промах — не провал. Это либо ошибка в ожидании (как `mg-002`), либо
настоящая просадка качества. Разница видна только по `detail`, поэтому он
печатается целиком, а не сворачивается в галочку.

Запуск:
    python scripts/soft_misses.py                  # последний отчёт каждого датасета
    python scripts/soft_misses.py platform_help    # только этот датасет
    python scripts/soft_misses.py --report evals/reports/v8std_20260816_041839.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "evals" / "reports"
DATASETS = ROOT / "evals" / "datasets"

# B-3: вывод не должен падать на cp1251-консоли.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover — старый Python или подменённый поток
        pass


def latest_report(stem: str) -> Path | None:
    found = sorted(REPORTS.glob(f"{stem}_*.json"))
    return found[-1] if found else None


def dataset_stems() -> list[str]:
    """Список датасетов не ведётся руками — он вычисляется по каталогу."""
    return sorted(p.stem for p in DATASETS.glob("*.jsonl"))


def misses(report: Path) -> list[dict]:
    """Непройденные soft-предикаты одного отчёта."""
    payload = json.loads(report.read_text(encoding="utf-8"))
    out = []
    for ex in payload.get("examples", []):
        for pred in ex.get("soft", []) or []:
            if not pred.get("passed"):
                out.append({
                    "id": ex.get("id", "?"),
                    "tool": ex.get("tool", "?"),
                    "args": ex.get("args", {}),
                    "type": pred.get("type", "?"),
                    "detail": pred.get("detail", ""),
                    "notes": ex.get("notes", ""),
                })
    return out


def show(report: Path) -> int:
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    got = summary.get("soft_passed", 0)
    total = summary.get("soft_total", 0)
    found = misses(report)

    print(f"\n{'─' * 74}")
    print(f"{report.name}   soft {got}/{total}")
    print("─" * 74)

    if not total:
        print("  soft-предикатов в датасете нет")
        return 0
    if not found:
        print("  все soft пройдены")
        return 0

    for m in found:
        print(f"\n  {m['id']}  {m['tool']}")
        if m["args"]:
            print(f"    args:   {json.dumps(m['args'], ensure_ascii=False)}")
        print(f"    предикат: {m['type']}")
        detail = m["detail"]
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        for line in str(detail).splitlines() or [""]:
            print(f"    почему:   {line}")
        if m["notes"]:
            note = " ".join(m["notes"].split())
            print(f"    заметка:  {note[:300]}")
    print()
    print("  Промах — это либо ошибка в ожидании (тогда правится датасет),")
    print("  либо просадка качества (тогда правится инструмент). Различить")
    print("  можно только прочитав «почему» выше.")
    return len(found)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("datasets", nargs="*",
                    help="имена датасетов; по умолчанию — все")
    ap.add_argument("--report", help="конкретный файл отчёта")
    args = ap.parse_args()

    if args.report:
        path = Path(args.report)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            print(f"нет файла: {path}", file=sys.stderr)
            return 2
        show(path)
        return 0

    stems = args.datasets or dataset_stems()
    total_misses = 0
    absent = []
    for stem in stems:
        report = latest_report(stem)
        if not report:
            absent.append(stem)
            continue
        total_misses += show(report)

    if absent:
        print(f"\nБез отчёта: {', '.join(absent)} — датасет не прогонялся ни разу.")
    print(f"\nВсего непройденных soft-предикатов: {total_misses}")
    # Ненулевой код не возвращаем: soft-промах не провал, а повод посмотреть.
    return 0


if __name__ == "__main__":
    sys.exit(main())
