"""
Агрегация метрик по результатам прогона.

Что считаем:
- pass_rate_hard   — доля примеров, где ВСЕ hard-предикаты прошли.
- pass_rate_soft   — доля примеров, где ВСЕ soft-предикаты прошли
                     (для примеров, где soft вообще есть).
- recall_at[k]     — для k in {1,5,10}, по примерам с hard name_in_top_k.
- mrr              — среднее 1/rank по тем же примерам; если не попали — 0.
- latency_ms       — min/median/p95/max по `call_tool`.
- search_type_dist — распределение режимов поиска.
"""
from __future__ import annotations

from statistics import median
from typing import Any


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def aggregate(example_results: list[dict]) -> dict[str, Any]:
    total = len(example_results)
    if total == 0:
        return {
            "total": 0,
            "hard_passed": 0, "hard_pass_rate": 0.0,
            "soft_total": 0, "soft_passed": 0, "soft_pass_rate": None,
            "examples_with_mrr": 0,
            "recall_at_1": None, "recall_at_5": None, "recall_at_10": None,
            "mrr": None,
            "latency_ms": {"min": 0, "median": 0, "p95": 0, "max": 0},
            "search_type_dist": {},
            "transport_errors": 0,
            "tool_errors": 0,
        }

    hard_passed = sum(
        1 for r in example_results
        if r["ok"] and all(p["passed"] for p in r.get("hard", []))
    )

    examples_with_soft = [r for r in example_results if r.get("soft")]
    soft_passed = sum(
        1 for r in examples_with_soft
        if r["ok"] and all(p["passed"] for p in r.get("soft", []))
    )

    mrr_examples = [r for r in example_results if r.get("mrr_max_k") is not None]
    ranks: list[int | None] = [r.get("mrr_rank") for r in mrr_examples]

    def _recall_at(k: int) -> float | None:
        if not mrr_examples:
            return None
        hit = sum(1 for rk in ranks if rk is not None and rk <= k)
        return hit / len(mrr_examples)

    def _mrr() -> float | None:
        if not mrr_examples:
            return None
        total_rr = 0.0
        for rk in ranks:
            if rk is not None:
                total_rr += 1.0 / rk
        return total_rr / len(mrr_examples)

    durations = [r["duration_ms"] for r in example_results if r["ok"]]

    search_type_dist: dict[str, int] = {}
    for r in example_results:
        st = r.get("search_type") or "unknown"
        search_type_dist[st] = search_type_dist.get(st, 0) + 1

    transport_errors = sum(1 for r in example_results if not r["ok"])
    tool_errors = sum(1 for r in example_results if r["ok"] and r.get("is_error_flag"))

    return {
        "total": total,
        "hard_passed": hard_passed,
        "hard_pass_rate": hard_passed / total,
        "soft_total": len(examples_with_soft),
        "soft_passed": soft_passed,
        "soft_pass_rate": (soft_passed / len(examples_with_soft)) if examples_with_soft else None,
        "examples_with_mrr": len(mrr_examples),
        "recall_at_1": _recall_at(1),
        "recall_at_5": _recall_at(5),
        "recall_at_10": _recall_at(10),
        "mrr": _mrr(),
        "latency_ms": {
            "min": min(durations) if durations else 0.0,
            "median": median(durations) if durations else 0.0,
            "p95": _percentile(durations, 0.95) if durations else 0.0,
            "max": max(durations) if durations else 0.0,
        },
        "search_type_dist": search_type_dist,
        "transport_errors": transport_errors,
        "tool_errors": tool_errors,
    }
