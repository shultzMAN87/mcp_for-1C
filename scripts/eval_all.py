#!/usr/bin/env python3
"""
EVAL-3 — все датасеты одной командой со сводкой.
================================================

Зачем. За одну сессию захода по `PLAN-5` датасеты гонялись вручную семь
раз: три команды `scripts/eval.py --dataset …`, потом глазами по трём
отчётам, потом сравнение с предыдущими отчётами в `evals/reports/` — тоже
глазами, по именам файлов с таймстампом.

Что делает:
  1. Находит датасеты сам, по маске `evals/datasets/*.jsonl`. Захардкоженный
     список разошёлся бы с реальностью ровно так же, как разошлись
     `gen_lockfiles.sh` и `.ps1`.
  2. Гоняет каждый через `scripts/eval.py` (сервер определяется по имени
     датасета там же, в `DATASET_SERVER`).
  3. Читает свежие отчёты и печатает одну таблицу.
  4. Сравнивает с предыдущим отчётом по тому же датасету и показывает
     дельту. Просадка hard — это регресс, и он должен быть видно, а не
     вычисляться человеком из двух json.

Код возврата ненулевой, если хоть один датасет не дал 100% hard или если
случился регресс против прошлого прогона (последнее отключается
`--no-regression-check`).

`DOC-3` попутно: в сводке печатается имя файла отчёта. Числа в документах
проекта разошлись с реальностью именно потому, что рядом с ними не стояло,
откуда они взяты.

Запуск:
    python3 scripts/eval_all.py
    python3 scripts/eval_all.py --only platform_help v8std
    python3 scripts/eval_all.py --local
    python3 scripts/eval_all.py --summary-only     # не гонять, показать последнее
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# B-3. Печать не должна ронять скрипт.
#
# `FAIL-2`: на приёмке 15 августа набор упал с UnicodeEncodeError на знаке
# ⚠ — консоль была cp1251, а в строке стоял символ, которого в ней нет.
# Тогда починили сервер справки и дочерние процессы run_all_tests, но сами
# хостовые скрипты остались: у них вывод уходит в консоль напрямую, и
# `$OutputEncoding` в PowerShell тут не помогает — он про то, чем консоль
# ЧИТАЕТ вывод, а не чем Python его кодирует.
#
# Воспроизводится одной строкой:
#     PYTHONIOENCODING=cp1251 python3 scripts/eval_all.py --summary-only
#
# errors=replace, а не encoding=utf-8: подмена кодировки дала бы кракозябры
# в cp1251-консоли, а замена — всего лишь «?» вместо галочки. Испортить
# украшение можно, уронить diagnostics-скрипт нельзя. Особенно
# check_prereqs: к нему идут именно тогда, когда что-то не работает.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = ROOT / "evals" / "datasets"
REPORTS_DIR = ROOT / "evals" / "reports"

# Датасеты, которые не являются мерилом качества: `probe` — служебный
# однопримерный набор для проверки транспорта.
SKIP_STEMS = {"probe"}


def find_datasets(only: list[str] | None) -> list[Path]:
    out = []
    for path in sorted(DATASETS_DIR.glob("*.jsonl")):
        if path.stem in SKIP_STEMS:
            continue
        if only and path.stem not in only:
            continue
        out.append(path)
    return out


def latest_reports(stem: str, limit: int = 2) -> list[Path]:
    """Последние отчёты по датасету, свежий первым."""
    return sorted(REPORTS_DIR.glob(f"{stem}_*.json"), reverse=True)[:limit]


def read_report(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def row_from_report(path: Path) -> dict:
    data = read_report(path) or {}
    s = data.get("summary") or {}
    return {
        "report": path.name,
        "hard_passed": s.get("hard_passed", 0),
        "total": s.get("total", 0),
        "soft_passed": s.get("soft_passed", 0),
        "soft_total": s.get("soft_total", 0),
        "mrr": s.get("mrr"),
        "median_ms": (s.get("latency_ms") or {}).get("median"),
        "transport_errors": s.get("transport_errors", 0),
        "tool_errors": s.get("tool_errors", 0),
    }


def _fmt_mrr(v) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) else "—"


def _fmt_ms(v) -> str:
    return f"{v:.0f}" if isinstance(v, (int, float)) else "—"


def run_one(dataset: Path, local: bool, limit: int, no_deps: bool = False) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "eval.py"),
           "--dataset", f"evals/datasets/{dataset.name}"]
    if local:
        cmd.append("--local")
    if no_deps:
        cmd.append("--no-deps")
    if limit:
        cmd += ["--limit", str(limit)]
    print(f"\n{'─' * 70}\n[eval-all] {dataset.stem}\n{'─' * 70}", flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="EVAL-3: все датасеты одной командой.")
    ap.add_argument("--only", nargs="*", default=None,
                    help="только эти датасеты (по имени без .jsonl)")
    ap.add_argument("--local", action="store_true",
                    help="гнать runner на хосте, а не через docker compose")
    ap.add_argument("--limit", type=int, default=0,
                    help="первые N примеров каждого датасета")
    ap.add_argument("--no-deps", action="store_true",
                    help="не поднимать зависимости eval-runner (A-8): нужно, "
                         "когда сервис намеренно остановлен")
    ap.add_argument("--summary-only", action="store_true",
                    help="ничего не гонять, показать сводку по последним отчётам")
    ap.add_argument("--no-regression-check", action="store_true",
                    help="не считать просадку против прошлого прогона провалом")
    args = ap.parse_args()

    datasets = find_datasets(args.only)
    if not datasets:
        print(f"Датасетов не найдено в {DATASETS_DIR}", file=sys.stderr)
        return 2

    # Отчёты, которые лежали ДО прогона: с ними и сравниваем.
    before = {d.stem: (latest_reports(d.stem, 1) or [None])[0] for d in datasets}

    t0 = time.monotonic()
    run_codes: dict[str, int] = {}
    if not args.summary_only:
        for d in datasets:
            run_codes[d.stem] = run_one(d, args.local, args.limit, args.no_deps)

    # ─ Сводка ─
    print("\n" + "=" * 78)
    print("EVAL-3: сводка")
    print("=" * 78)
    header = f"{'датасет':<18} {'hard':>9} {'soft':>9} {'MRR':>6} {'мс':>7}  отчёт"
    print(header)
    print("-" * 78)

    failed: list[str] = []
    regressed: list[str] = []
    missing: list[str] = []

    for d in datasets:
        reports = latest_reports(d.stem, 1)
        if not reports:
            missing.append(d.stem)
            print(f"{d.stem:<18} {'—':>9} {'—':>9} {'—':>6} {'—':>7}  отчёта нет")
            continue
        now = row_from_report(reports[0])
        hard = f"{now['hard_passed']}/{now['total']}"
        soft = f"{now['soft_passed']}/{now['soft_total']}" if now["soft_total"] else "—"
        print(f"{d.stem:<18} {hard:>9} {soft:>9} {_fmt_mrr(now['mrr']):>6} "
              f"{_fmt_ms(now['median_ms']):>7}  {now['report']}")

        if now["total"] and now["hard_passed"] < now["total"]:
            failed.append(d.stem)
        if now["transport_errors"] or now["tool_errors"]:
            failed.append(d.stem)

        # Дельта против того, что лежало до прогона.
        prev_path = before.get(d.stem)
        if prev_path and prev_path.name != now["report"]:
            prev = row_from_report(prev_path)
            d_hard = now["hard_passed"] - prev["hard_passed"]
            parts = []
            if d_hard:
                parts.append(f"hard {prev['hard_passed']}/{prev['total']} → "
                             f"{now['hard_passed']}/{now['total']}")
            if isinstance(now["mrr"], (int, float)) and isinstance(prev["mrr"], (int, float)):
                if abs(now["mrr"] - prev["mrr"]) >= 0.005:
                    parts.append(f"MRR {prev['mrr']:.3f} → {now['mrr']:.3f}")
            if parts:
                mark = "↓" if d_hard < 0 else "↑"
                print(f"{'':<18} {mark} против {prev_path.name}: {', '.join(parts)}")
            if d_hard < 0:
                regressed.append(d.stem)

    print("-" * 78)
    if not args.summary_only:
        print(f"прогон занял {time.monotonic() - t0:.0f} с")

    rc = 0
    if missing:
        print(f"⚠ без отчёта: {', '.join(missing)} — датасет не прогонялся ни разу")
        rc = 1
    if failed:
        print(f"ПРОВАЛ: hard не 100% или есть ошибки транспорта — "
              f"{', '.join(sorted(set(failed)))}")
        rc = 1
    if regressed and not args.no_regression_check:
        print(f"РЕГРЕСС против прошлого прогона: {', '.join(regressed)}")
        rc = 1
    if rc == 0:
        print("OK: все датасеты дали 100% hard, регрессов нет")
    return rc


if __name__ == "__main__":
    sys.exit(main())
