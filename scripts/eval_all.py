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

# Разбор soft-промахов живёт рядом, в scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def count_examples(dataset: Path) -> int:
    """
    Сколько примеров в датасете сейчас — без JSON-разбора каждой строки:
    считаются непустые строки, не начинающиеся с `//`.

    Нужно для `FIX-25`: если в отчёте примеров меньше, чем в датасете, то
    отчёт снят на другой его редакции, и цифры относятся не к тому, что
    лежит на диске.
    """
    try:
        lines = dataset.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    return sum(1 for s in (l.strip() for l in lines)
               if s and not s.startswith("//"))


def report_verdict(*, rc, latest_name, prev_name, report_total,
                   want_examples, summary_only) -> tuple[str | None, str]:
    """
    Можно ли верить показанию по этому датасету.

    `FIX-25`. Вынесено отдельной функцией не ради красоты: логика «этот
    отчёт от этого прогона» проверяется тестом, а до сих пор она жила
    внутри `main()` и не проверялась ничем — потому и отсутствовала.

    Возвращает (вид проблемы, текст в строку сводки). `None` — показанию
    верить можно.
    """
    if rc:
        return "not_run", f"ПРОГОН НЕ СОСТОЯЛСЯ (код {rc})"
    if not latest_name:
        return "no_report", "отчёта нет"
    if not summary_only and prev_name and latest_name == prev_name:
        # Прогон вернул ноль и не создал отчёта. Без этой ветки в сводку
        # пошли бы прошлые цифры.
        return "stale_file", f"ОТЧЁТ НЕ ОБНОВИЛСЯ: {latest_name}"
    if want_examples and report_total and report_total != want_examples:
        # Ветка, которая поймала бы 18 августа раньше всех: в датасете
        # справки 27 примеров, а сводка показывала 20.
        return "stale_edition", (f"ОТЧЁТ ОТ ДРУГОЙ РЕДАКЦИИ: в нём "
                                 f"{report_total} примеров, в датасете "
                                 f"{want_examples}")
    return None, ""


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

    stale: list[str] = []
    not_run: list[tuple[str, int]] = []

    for d in datasets:
        # FIX-25. Код возврата прогона собирался в `run_codes` и не
        # читался ни разу. 18 августа Docker Desktop был выключен: все
        # пять прогонов упали с «failed to connect to the docker API»,
        # сводка прочла отчёты недельной давности и написала «OK: все
        # датасеты дали 100% hard, регрессов нет».
        #
        # Та же семья, что FIX-23 и FIX-16: правдоподобный неверный
        # результат вместо отказа. Здесь опаснее прочих — это
        # ЕДИНСТВЕННЫЙ прибор, которым проверяют, не сломалось ли
        # качество.
        rc_run = run_codes.get(d.stem)
        reports = latest_reports(d.stem, 1)
        prev_path_check = before.get(d.stem)
        latest_name = reports[0].name if reports else None
        report_total = row_from_report(reports[0])["total"] if reports else 0

        problem, note = report_verdict(
            rc=rc_run,
            latest_name=latest_name,
            prev_name=prev_path_check.name if prev_path_check else None,
            report_total=report_total,
            want_examples=count_examples(d),
            summary_only=args.summary_only,
        )
        if problem:
            if problem == "not_run":
                not_run.append((d.stem, rc_run))
            elif problem == "no_report":
                missing.append(d.stem)
            else:
                stale.append(d.stem)
            print(f"{d.stem:<18} {'—':>9} {'—':>9} {'—':>6} {'—':>7}  {note}")
            continue

        now = row_from_report(reports[0])
        hard = f"{now['hard_passed']}/{now['total']}"
        soft = f"{now['soft_passed']}/{now['soft_total']}" if now["soft_total"] else "—"
        print(f"{d.stem:<18} {hard:>9} {soft:>9} {_fmt_mrr(now['mrr']):>6} "
              f"{_fmt_ms(now['median_ms']):>7}  {now['report']}")

        # Soft-промахи называются по именам прямо здесь. Раньше сводка
        # печатала «7/9» и на этом останавливалась, а имена лежали в
        # .md-отчёте — четыре промаха прожили так несколько недель. Число
        # без имени не измерение, а лампочка без надписи.
        if now["soft_total"] and now["soft_passed"] < now["soft_total"]:
            try:
                from soft_misses import misses            # noqa: E402
                names = [f"{m['id']} ({m['type']})"
                         for m in misses(REPORTS_DIR / now["report"])]
            except Exception:
                names = []
            if names:
                print(f"{'':<18} soft мимо: {', '.join(names)}")
                print(f"{'':<18} подробности: python scripts/soft_misses.py {d.stem}")

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
    if not_run:
        print("ПРОГОН НЕ СОСТОЯЛСЯ: "
              + ", ".join(f"{s} (код {c})" for s, c in not_run))
        # Причину печатает сам прогон — она выше по экрану, и гадать за
        # него не надо: первая редакция этой подсказки уверенно называла
        # выключенный Docker, а в первом же случае дело было в
        # непересобранном образе раннера.
        print("  Причина — в выводе прогона выше по экрану (частые: не "
              "пересобран образ раннера после правки предикатов, "
              "выключенный Docker Desktop, недоступный сервер).")
        print("  Цифры прошлых прогонов в сводку НЕ идут: старое "
              "показание, выданное за новое, хуже отсутствия показания "
              "(FIX-25).")
        rc = 1
    if stale:
        print(f"ПОКАЗАНИЯ НЕ ОТ ЭТОГО ПРОГОНА: {', '.join(sorted(set(stale)))}")
        rc = 1
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
    else:
        print("Сводке верить нельзя — сначала устраните перечисленное выше.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
