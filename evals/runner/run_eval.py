#!/usr/bin/env python3
"""
Eval-runner: прогоняет датасет запросов через MCP-tool и выдаёт отчёт.

Exit code:
    0 — все hard-предикаты прошли (И нет транспортных/tool-ошибок)
    1 — хоть один hard провалился, или транспортная ошибка
    2 — ошибка конфигурации (файл датасета не найден, пр.)

Один раз initialize(), одна SSE-сессия на весь прогон — иначе
platform-help будет дважды грузить dense/BM25-модели.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from mcp_client import MCPSession, ToolCallResult
from metrics import aggregate
from predicates import PredicateOutcome, evaluate
from report import generate_reports, utcnow


def load_dataset(path: Path) -> list[dict]:
    """
    Читает .jsonl, пропуская пустые строки и строки, начинающиеся с `//`.
    Ошибки парсинга отдельных строк — не критичны: warning и идём дальше.
    """
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")

    items: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  warn: {path}:{lineno}: json decode failed: {e}", file=sys.stderr)
            continue
        if not isinstance(obj, dict):
            print(f"  warn: {path}:{lineno}: not an object, skipped", file=sys.stderr)
            continue
        for req in ("id", "tool", "args", "expect"):
            if req not in obj:
                print(f"  warn: {path}:{lineno}: missing '{req}', skipped", file=sys.stderr)
                break
        else:
            items.append(obj)
    return items


def _mrr_info_from_hard(example: dict, outcomes: list[PredicateOutcome]) -> tuple[int | None, int | None]:
    """
    Возвращает (mrr_rank, mrr_max_k) — ранг первого name_in_top_k в hard.
    """
    hard_preds = example.get("expect", {}).get("hard", []) or []
    for pred, outcome in zip(hard_preds, outcomes):
        if outcome.type == "name_in_top_k":
            try:
                k = max(1, int(pred.get("k", 5)))
            except Exception:
                k = 5
            return outcome.match_rank, k
    return None, None


async def run_one(session: MCPSession, example: dict) -> dict:
    tool_name = example["tool"]
    args = example.get("args", {}) or {}
    expect = example.get("expect", {}) or {}

    call: ToolCallResult = await session.call_tool(tool_name, args)

    if not call.ok or call.parsed is None:
        return {
            "id": example["id"],
            "tool": tool_name,
            "args": args,
            "notes": example.get("notes", ""),
            "ok": False,
            "error": call.error or "unknown",
            "is_error_flag": call.is_error_flag,
            "duration_ms": call.duration_ms,
            "search_type": None,
            "hard": [], "soft": [],
            "mrr_rank": None, "mrr_max_k": None,
            "response_preview": None,
        }

    parsed = call.parsed
    search_type = parsed.get("search_type") if isinstance(parsed, dict) else None

    hard_out: list[PredicateOutcome] = [evaluate(p, parsed) for p in (expect.get("hard") or [])]
    soft_out: list[PredicateOutcome] = [evaluate(p, parsed) for p in (expect.get("soft") or [])]

    mrr_rank, mrr_max_k = _mrr_info_from_hard(example, hard_out)

    preview = None
    if isinstance(parsed, dict):
        results = parsed.get("results")
        if isinstance(results, list):
            preview = {"results": results[:5], "search_type": search_type}
        else:
            preview = {k: v for k, v in parsed.items() if k != "results"}
            preview["results"] = []

    return {
        "id": example["id"],
        "tool": tool_name,
        "args": args,
        "notes": example.get("notes", ""),
        "ok": True,
        "error": None,
        "is_error_flag": call.is_error_flag,
        "duration_ms": call.duration_ms,
        "search_type": search_type,
        "hard": [asdict(o) for o in hard_out],
        "soft": [asdict(o) for o in soft_out],
        "mrr_rank": mrr_rank,
        "mrr_max_k": mrr_max_k,
        "response_preview": preview,
    }


async def main_async(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset).resolve()
    out_dir = Path(args.out).resolve()

    try:
        examples_in = load_dataset(dataset_path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not examples_in:
        print(f"error: empty dataset at {dataset_path}", file=sys.stderr)
        return 2

    print(f"[eval] dataset:  {dataset_path}")
    print(f"[eval] endpoint: {args.endpoint}")
    print(f"[eval] examples: {len(examples_in)}")
    print(f"[eval] out:      {out_dir}")
    if args.limit:
        examples_in = examples_in[: args.limit]
        print(f"[eval] limit applied: first {len(examples_in)}")

    started = utcnow()

    results: list[dict] = []
    try:
        async with MCPSession(
            args.endpoint,
            init_timeout=args.init_timeout,
            call_timeout=args.call_timeout,
        ) as session:
            print("[eval] SSE initialized, running examples...", flush=True)
            for idx, ex in enumerate(examples_in, start=1):
                print(f"  [{idx:3d}/{len(examples_in)}] {ex['id']:12s} {ex['tool']}", flush=True)
                r = await run_one(session, ex)
                results.append(r)
                if not r["ok"]:
                    print(f"        ✗ ERROR: {r['error']}", flush=True)
                else:
                    hp = sum(1 for p in r["hard"] if p["passed"])
                    ht = len(r["hard"])
                    marker = "✓" if hp == ht else "✗"
                    print(f"        {marker} hard {hp}/{ht}  ({r['duration_ms']:.0f} ms, mode={r['search_type']})", flush=True)
    except Exception as e:
        print(f"[eval] FATAL session error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    finished = utcnow()
    summary = aggregate(results)

    json_path, md_path = generate_reports(
        out_dir=out_dir,
        dataset_path=dataset_path,
        tool_endpoint=args.endpoint,
        examples=results,
        aggregate_summary=summary,
        started_at=started,
        finished_at=finished,
    )

    print("")
    print("[eval] ─── Summary ─────────────────────────")
    print(f"       hard pass-rate: {summary['hard_passed']}/{summary['total']} "
          f"({(summary['hard_pass_rate'] * 100):.1f}%)")
    if summary["soft_total"]:
        print(f"       soft pass-rate: {summary['soft_passed']}/{summary['soft_total']} "
              f"({(summary['soft_pass_rate'] * 100):.1f}%)")
    if summary["examples_with_mrr"]:
        mrr = summary["mrr"]
        mrr_str = f"{mrr:.3f}" if mrr is not None else "—"
        print(f"       Recall@1/5/10:  "
              f"{(summary['recall_at_1'] or 0) * 100:.1f}% / "
              f"{(summary['recall_at_5'] or 0) * 100:.1f}% / "
              f"{(summary['recall_at_10'] or 0) * 100:.1f}%   "
              f"MRR={mrr_str}")
    lat = summary["latency_ms"]
    print(f"       latency:        "
          f"min={lat['min']:.0f} ms  median={lat['median']:.0f} ms  "
          f"p95={lat['p95']:.0f} ms  max={lat['max']:.0f} ms")
    if summary["transport_errors"]:
        print(f"       transport errors: {summary['transport_errors']}")
    if summary["tool_errors"]:
        print(f"       tool errors (isError): {summary['tool_errors']}")
    print("")
    print(f"[eval] reports:")
    print(f"         {json_path}")
    print(f"         {md_path}")

    if summary["hard_pass_rate"] >= 1.0 and summary["transport_errors"] == 0:
        return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP eval-runner")
    ap.add_argument("--dataset", required=True, help="Path to .jsonl dataset")
    ap.add_argument("--out", required=True, help="Output directory for reports")
    ap.add_argument(
        "--endpoint",
        default="http://mcp-platform-help:8003/sse",
        help="MCP SSE endpoint URL (default: http://mcp-platform-help:8003/sse)",
    )
    ap.add_argument("--init-timeout", type=float, default=120.0,
                    help="SSE initialize timeout in seconds (default 120)")
    ap.add_argument("--call-timeout", type=float, default=60.0,
                    help="Per-tool call_tool timeout in seconds (default 60)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Run only first N examples (0 = all)")
    args = ap.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[eval] interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
