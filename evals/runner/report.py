"""
Формирование отчётов по результатам прогона.

На выходе пишем два файла с одинаковым basename:
- {basename}.json — машиночитаемый, вся информация.
- {basename}.md   — человеко-читаемый: таблица, блок итогов, список проваленных предикатов.

Имя файла по умолчанию:
    {dataset_stem}_{YYYYMMDD_HHMMSS}.{json,md}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def _fmt_ms(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.0f} ms"


def _fmt_mrr_cell(example: dict) -> str:
    k = example.get("mrr_max_k")
    if k is None:
        return "—"
    rank = example.get("mrr_rank")
    if rank is None:
        return f"—/{k}"
    return str(rank)


def _fmt_predicates_cell(outcomes: list[dict]) -> str:
    if not outcomes:
        return "—"
    passed = sum(1 for o in outcomes if o["passed"])
    total = len(outcomes)
    head = f"{passed}/{total}"
    if passed == total:
        return head
    failed = [o["type"] for o in outcomes if not o["passed"]]
    fshort = ", ".join(failed[:3]) + ("…" if len(failed) > 3 else "")
    return f"{head} ✗{fshort}"


def generate_reports(
    out_dir: Path,
    dataset_path: Path,
    tool_endpoint: str,
    examples: list[dict],
    aggregate_summary: dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = dataset_path.stem
    ts = finished_at.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{stem}_{ts}.json"
    md_path = out_dir / f"{stem}_{ts}.md"

    payload = {
        "meta": {
            "dataset": str(dataset_path),
            "endpoint": tool_endpoint,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_s": (finished_at - started_at).total_seconds(),
            "examples_total": len(examples),
        },
        "summary": aggregate_summary,
        "examples": examples,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append(f"# Eval report — `{stem}`")
    lines.append("")
    lines.append(f"- **Dataset:** `{dataset_path}`")
    lines.append(f"- **Endpoint:** `{tool_endpoint}`")
    lines.append(f"- **Started:**  {started_at.isoformat(timespec='seconds')}")
    lines.append(f"- **Finished:** {finished_at.isoformat(timespec='seconds')}")
    lines.append(f"- **Duration:** {(finished_at - started_at).total_seconds():.1f} s")
    lines.append("")

    s = aggregate_summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total examples | {s['total']} |")
    lines.append(f"| Hard pass-rate | **{s['hard_passed']}/{s['total']}** ({_fmt_pct(s['hard_pass_rate'])}) |")
    if s['soft_total']:
        lines.append(f"| Soft pass-rate | {s['soft_passed']}/{s['soft_total']} ({_fmt_pct(s['soft_pass_rate'])}) |")
    else:
        lines.append("| Soft pass-rate | — (no soft predicates) |")
    if s['examples_with_mrr']:
        lines.append(f"| Recall@1 (n={s['examples_with_mrr']}) | {_fmt_pct(s['recall_at_1'])} |")
        lines.append(f"| Recall@5 (n={s['examples_with_mrr']}) | {_fmt_pct(s['recall_at_5'])} |")
        lines.append(f"| Recall@10 (n={s['examples_with_mrr']}) | {_fmt_pct(s['recall_at_10'])} |")
        mrr_val = s['mrr']
        lines.append(f"| MRR | {mrr_val:.3f} |" if mrr_val is not None else "| MRR | — |")
    else:
        lines.append("| Recall@K / MRR | — (no hard `name_in_top_k`) |")
    lat = s['latency_ms']
    lines.append(f"| Latency min / median / p95 / max | {_fmt_ms(lat['min'])} / {_fmt_ms(lat['median'])} / {_fmt_ms(lat['p95'])} / {_fmt_ms(lat['max'])} |")
    if s['transport_errors']:
        lines.append(f"| **Transport errors** | {s['transport_errors']} |")
    if s['tool_errors']:
        lines.append(f"| **Tool errors (isError)** | {s['tool_errors']} |")
    if s['search_type_dist']:
        dist_str = ", ".join(f"{k}={v}" for k, v in sorted(s['search_type_dist'].items()))
        lines.append(f"| Search modes | {dist_str} |")
    lines.append("")

    lines.append("## Per-example")
    lines.append("")
    lines.append("| id | tool | hard | soft | MRR rank | latency | mode | notes |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for ex in examples:
        if not ex["ok"]:
            lines.append(
                f"| `{ex['id']}` | `{ex['tool']}` | "
                f"**ERROR** | — | — | {_fmt_ms(ex.get('duration_ms'))} | — | "
                f"{ex.get('error') or ''} |"
            )
            continue
        hard_cell = _fmt_predicates_cell(ex.get("hard", []))
        soft_cell = _fmt_predicates_cell(ex.get("soft", []))
        mode = ex.get("search_type") or "—"
        notes = (ex.get("notes") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{ex['id']}` | `{ex['tool']}` | "
            f"{hard_cell} | {soft_cell} | {_fmt_mrr_cell(ex)} | "
            f"{_fmt_ms(ex.get('duration_ms'))} | {mode} | {notes} |"
        )
    lines.append("")

    failed = [ex for ex in examples if not ex["ok"] or any(not p["passed"] for p in ex.get("hard", []))]
    if failed:
        lines.append("## Failures (hard)")
        lines.append("")
        for ex in failed:
            lines.append(f"### `{ex['id']}` — `{ex['tool']}`")
            lines.append("")
            lines.append("**args:**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(ex.get("args", {}), ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
            if not ex["ok"]:
                lines.append(f"Transport error: `{ex.get('error')}`")
                lines.append("")
                continue
            if ex.get("is_error_flag"):
                lines.append("`result.isError=True` — tool сам вернул ошибку.")
                lines.append("")
            failed_preds = [p for p in ex.get("hard", []) if not p["passed"]]
            if failed_preds:
                lines.append("**Failed hard predicates:**")
                lines.append("")
                for p in failed_preds:
                    lines.append(f"- `{p['type']}` — detail:")
                    lines.append("  ```json")
                    for ln in json.dumps(p.get("detail", {}), ensure_ascii=False, indent=2).splitlines():
                        lines.append("  " + ln)
                    lines.append("  ```")
                lines.append("")
            parsed = ex.get("response_preview") or {}
            hits = parsed.get("results") if isinstance(parsed, dict) else None
            if isinstance(hits, list) and hits:
                lines.append("**Top-3 actual hits:**")
                lines.append("")
                for idx, h in enumerate(hits[:3], start=1):
                    nr = h.get("name_ru") or ""
                    ne = h.get("name_en") or ""
                    fn = h.get("full_name") or ""
                    knd = h.get("kind") or ""
                    sc = h.get("score")
                    sc_str = f" (score={sc})" if sc is not None else ""
                    lines.append(f"{idx}. `{fn or nr or ne}` — kind=`{knd}`, name_ru=`{nr}`, name_en=`{ne}`{sc_str}")
                lines.append("")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
