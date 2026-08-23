#!/usr/bin/env python3
"""
EVAL-7 — что агент позвал и сколько раз.
=========================================

Зачем
─────
`evals/manual-prompts.md` считает сценарий пройденным по СПИСКУ ВЫЗОВОВ, а
не по тексту ответа. Прогон 22 августа шёл без журнала, и треть отметок
оказалась косвенной: «похоже, сходил». Хуже всего это било по единственной
проверке, ради которой строка «обязано быть вызвано» и заводилась: один
вызов `explain_diagnostics` со всем списком кодов и пять вызовов подряд
дают одинаковый ответ на экране.

Что делает скрипт
─────────────────
Читает журналы из `evals/journal/` (пишут все пять серверов, см.
`1c-mcp-suite/tool_journal.py` и ключ `--usage-log` у чужого `v8std`),
режет их по меткам сценариев и печатает готовые для бланка строки:

    Сценарий 7 — 12:03:11…12:03:48
      bsl_check_code ×1, v8std_explain_diagnostics ×1

Как пользоваться на прогоне
───────────────────────────
    python scripts/journal_report.py --mark 7     # перед сценарием 7
    …задаёте вопрос в Cursor, ждёте ответ…
    python scripts/journal_report.py --mark 8     # перед сценарием 8
    …
    python scripts/journal_report.py              # в конце: таблица целиком
    python scripts/journal_report.py --markdown   # то же, готовым куском
                                                  # для бланка прогона

Метка — это просто строка со временем; никакой связи с сервером у неё
нет. Забыли поставить — вызовы уедут в предыдущий сценарий, и это будет
видно по времени.

Почему не по счётчикам `TOOL-1`
────────────────────────────────
Они не знают времени каждого вызова, то есть нарезать по сценариям нечем,
а разность снимков не покрывает `v8std` (у чужого сервера нет нашего
`*_stats`) и `query-builder` (у него нет `*_stats` вовсе). Журнал покрывает
все пять серверов, потому что каждый пишет сам.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

# Отметки старта серверов, собранные последним чтением журнала. Список, а
# не возврат функции: подписи `read_journal` держатся тесты и вызовы, а
# отметки нужны ровно в одном месте — в объяснении пустого журнала.
STARTS: list[dict] = []
JOURNAL_DIR = ROOT / "evals" / "journal"
MARKS_NAME = "marks.jsonl"

# По какому префиксу видно сервер, если строку писал чужой журнал: он
# кладёт только `ts` и `tool`. Список не выдуман — это префиксы имён
# инструментов пяти серверов набора.
SERVER_BY_PREFIX = (
    ("metadata_", "metadata-graph"),
    ("code_", "metadata-graph"),
    ("query_", "query-builder"),
    ("bsl_", "bsl-checker"),
    ("platform_help_", "platform-help"),
    ("v8std_", "v8std"),
)


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def server_of(entry: dict) -> str:
    """
    Чей это вызов. Своё поле важнее догадки по имени: сервер может
    переехать, а префикс инструмента — остаться.
    """
    if entry.get("server"):
        return str(entry["server"])
    tool = str(entry.get("tool", ""))
    for prefix, server in SERVER_BY_PREFIX:
        if tool.startswith(prefix):
            return server
    return "неизвестный"


def read_journal(directory: Path) -> tuple[list[dict], list[str]]:
    """
    Все вызовы из всех журналов каталога, отсортированные по времени.

    Второе значение — жалобы: файл не читается, строка не разбирается. Их
    печатают, а не глотают: журнал, который тихо потерял половину строк,
    хуже отсутствующего — по нему сделают вывод.

    Строки старта (`event: start`) вызовами не считаются и в список не
    попадают — их отдельно собирает `servers_started`.
    """
    calls: list[dict] = []
    complaints: list[str] = []
    STARTS.clear()
    if not directory.is_dir():
        return calls, [f"нет каталога {directory}"]
    for path in sorted(directory.glob("*.jsonl")):
        if path.name == MARKS_NAME:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            complaints.append(f"{path.name}: не читается ({exc})")
            continue
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                complaints.append(f"{path.name}:{n}: строка не разбирается")
                continue
            ts = _parse_ts(entry.get("ts"))
            if entry.get("event") == "start":
                # Отметка «сервер поднялся и журнал ведёт». Вызовом не
                # является, но доказывает, что рычаг включён, — ради этого
                # и заведена.
                if ts is not None and entry.get("server"):
                    STARTS.append({"ts": ts, "server": str(entry["server"]),
                                   "tools": entry.get("tools")})
                continue
            tool = entry.get("tool")
            if ts is None or not tool:
                complaints.append(f"{path.name}:{n}: нет времени или имени")
                continue
            calls.append({"ts": ts, "tool": str(tool),
                          "server": server_of(entry),
                          "ok": entry.get("ok", True),
                          "file": path.name})
    calls.sort(key=lambda c: c["ts"])
    return calls, complaints


def read_marks(directory: Path) -> list[dict]:
    path = directory / MARKS_NAME
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        out.append({"ts": ts, "name": str(entry.get("name", "")).strip()})
    out.sort(key=lambda m: m["ts"])
    return out


def add_mark(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MARKS_NAME
    entry = {"ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
             "name": name}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def split_by_marks(calls: list[dict], marks: list[dict]) -> list[dict]:
    """
    Разложить вызовы по окнам между метками.

    Вызовы до первой метки не выбрасываются, а собираются в окно «до
    первой метки»: это обычно прогрев и проверка стенда, и молча потерять
    их нельзя — иначе итог не сойдётся с числом строк в журнале.
    """
    windows: list[dict] = []
    if not marks:
        return [{"name": "весь журнал", "start": None, "end": None,
                 "calls": list(calls)}]
    if calls and calls[0]["ts"] < marks[0]["ts"]:
        windows.append({"name": "до первой метки", "start": None,
                        "end": marks[0]["ts"], "calls": []})
    for i, mark in enumerate(marks):
        end = marks[i + 1]["ts"] if i + 1 < len(marks) else None
        windows.append({"name": mark["name"] or f"метка {i + 1}",
                        "start": mark["ts"], "end": end, "calls": []})
    for call in calls:
        for window in windows:
            if window["start"] is not None and call["ts"] < window["start"]:
                continue
            if window["end"] is not None and call["ts"] >= window["end"]:
                continue
            window["calls"].append(call)
            break
    return windows


def tally(calls: list[dict]) -> str:
    """«bsl_check_code ×1, v8std_explain_diagnostics ×2» — строка бланка."""
    if not calls:
        return "—"
    counts = Counter(c["tool"] for c in calls)
    return ", ".join(f"{tool} ×{n}" for tool, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%H:%M:%S") if dt else "—"


def _window_span(window: dict) -> str:
    if not window["calls"]:
        return _hhmm(window["start"])
    return f"{_hhmm(window['calls'][0]['ts'])}…{_hhmm(window['calls'][-1]['ts'])}"


def render(windows: list[dict], markdown: bool = False) -> str:
    lines: list[str] = []
    if markdown:
        lines.append("| Сценарий | Вызовы | Всего |")
        lines.append("|---|---|---|")
        for window in windows:
            lines.append(f"| {window['name']} | {tally(window['calls'])} "
                         f"| {len(window['calls'])} |")
        return "\n".join(lines)
    for window in windows:
        lines.append(f"{window['name']} — {_window_span(window)}")
        lines.append(f"  {tally(window['calls'])}")
        failed = [c for c in window["calls"] if not c["ok"]]
        if failed:
            # Упавший вызов — это вызов. Отдельной строкой, потому что в
            # бланке «обязано быть вызвано» он засчитывается, а «ответ
            # получен» — нет.
            lines.append(f"  из них закончились ошибкой: {tally(failed)}")
    return "\n".join(lines)


def servers_seen(calls: list[dict]) -> list[str]:
    return sorted({c["server"] for c in calls})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="EVAL-7: какие инструменты вызваны и сколько раз.")
    ap.add_argument("--mark", metavar="ИМЯ",
                    help="поставить метку начала сценария и выйти")
    ap.add_argument("--dir", default=str(JOURNAL_DIR),
                    help="каталог журналов (по умолчанию evals/journal)")
    ap.add_argument("--since", metavar="15m",
                    help="только вызовы за последние N минут (m) или часов (h)")
    ap.add_argument("--markdown", action="store_true",
                    help="таблица для бланка прогона")
    args = ap.parse_args(argv)

    directory = Path(args.dir)

    if args.mark:
        path = add_mark(directory, args.mark)
        print(f"метка «{args.mark}» поставлена: {path}")
        return 0

    calls, complaints = read_journal(directory)
    for complaint in complaints:
        print(f"⚠ {complaint}", file=sys.stderr)

    if not calls and STARTS:
        # Журнал включён, вызовов ещё не было. Это НОРМА: так выглядит
        # каталог сразу после перезапуска серверов, до первого вопроса
        # агенту. Прежняя редакция печатала здесь три шага починки — то
        # есть норма читалась как отказ, зеркало дефекта, с которым
        # борются FIX-3 и OBS-1.
        servers = ", ".join(sorted({s["server"] for s in STARTS}))
        print("Журнал включён, вызовов пока нет.")
        print(f"  ведут журнал: {servers}")
        print("  Задайте вопрос агенту в Cursor и повторите команду.")
        return 0

    if not calls:
        # Пустой журнал и выключенный журнал — разные вещи, и разбирать их
        # придётся посреди прогона. Поэтому здесь не «нет данных», а что
        # именно проверить.
        print("Вызовов в журнале нет, и ни один сервер не отметился при "
              "старте.")
        print()
        print("Если прогон уже шёл, журнал, скорее всего, выключен:")
        print("  1. в .env задать MCP_TOOL_JOURNAL=/journal/calls.jsonl")
        print("     и V8STD_USAGE_LOG=/journal/v8std.jsonl")
        print("  2. docker compose up -d --force-recreate <пять серверов>")
        print("     (именно up --force-recreate: restart не подхватывает")
        print("      ни новый образ, ни новые переменные)")
        print("  3. в логе старта каждый сервер называет путь журнала либо")
        print("     говорит, что журнал выключен")
        return 1

    if args.since:
        m = re.fullmatch(r"(\d+)\s*([mh])", args.since.strip())
        if not m:
            print("--since ждёт вид 15m или 2h", file=sys.stderr)
            return 2
        delta = timedelta(minutes=int(m.group(1))) if m.group(2) == "m" \
            else timedelta(hours=int(m.group(1)))
        edge = datetime.now(timezone.utc) - delta
        calls = [c for c in calls if c["ts"] >= edge]

    marks = read_marks(directory)
    windows = split_by_marks(calls, marks)
    print(render(windows, markdown=args.markdown))
    print()
    print(f"Всего вызовов: {len(calls)}; серверов в журнале: "
          f"{', '.join(servers_seen(calls))}")
    if "v8std" not in servers_seen(calls):
        # Именно к этому серверу относится отметка, ради которой заведён
        # журнал. Молчание тут значит «не проверено», а не «не звали».
        print("⚠ вызовов v8std в журнале нет. Если по сценарию он должен "
              "был отвечать — проверьте V8STD_USAGE_LOG у контейнера "
              "v8std-mcp: без него чужой сервер журнал не ведёт")
    return 0


if __name__ == "__main__":
    sys.exit(main())
