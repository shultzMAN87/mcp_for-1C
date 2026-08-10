"""
Юнит-тесты eval-runner'а без сети.

Покрываем:
  - predicates.evaluate: 5 типов + неизвестный тип + некорректные входы
  - metrics.aggregate: hard/soft pass, MRR, recall@k, разделение mrr_max_k
  - run_eval.load_dataset: комментарии, пустые строки, невалидный JSON,
    отсутствующие обязательные поля
  - run_eval._mrr_info_from_hard: порядок предикатов
  - run_eval.run_one через FakeMCPSession
  - report.generate_reports: выходные файлы существуют, содержат ключевые секции

Запуск:
    python3 evals/runner/tests.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from predicates import PredicateOutcome, evaluate
from metrics import aggregate
from run_eval import load_dataset, _mrr_info_from_hard, run_one
from report import generate_reports, utcnow
from mcp_client import ToolCallResult


def test_predicates():
    r = {"results": [
        {"name_ru": "СтрРазделить", "name_en": "StrSplit", "full_name": "СтрРазделить", "kind": "method"},
        {"name_ru": "СтрДлина",     "name_en": "StrLen",   "full_name": "СтрДлина",     "kind": "method"},
        {"name_ru": "СтрСоединить", "name_en": "StrConcat","full_name": "СтрСоединить", "kind": "method"},
    ]}
    cases = [
        ({"type": "non_empty"}, r, True),
        ({"type": "non_empty"}, {"results": []}, False),
        ({"type": "non_empty"}, "not a dict", False),
        ({"type": "results_count_at_least", "min": 3}, r, True),
        ({"type": "results_count_at_least", "min": 10}, r, False),
        ({"type": "name_in_top_k", "k": 5, "values": ["СтрРазделить"]}, r, True),
        ({"type": "name_in_top_k", "k": 5, "values": ["strsplit"]}, r, True),
        ({"type": "name_in_top_k", "k": 1, "values": ["СтрДлина"]}, r, False),
        ({"type": "name_in_top_k", "k": 2, "values": ["СтрДлина"]}, r, True),
        ({"type": "name_in_top_k", "k": 5, "values": ["НетТакого"]}, r, False),
        ({"type": "name_in_top_k", "k": 5, "values": []}, r, False),
        ({"type": "any_hit_kind", "k": 5, "kinds": ["method"]}, r, True),
        ({"type": "any_hit_kind", "k": 5, "kinds": ["event"]}, r, False),
        ({"type": "any_hit_kind", "k": 5, "kinds": []}, r, False),
        ({"type": "full_name_contains", "k": 5, "substr": "Раздел"}, r, True),
        ({"type": "full_name_contains", "k": 5, "substr": "РАЗДЕЛ"}, r, True),
        ({"type": "full_name_contains", "k": 5, "substr": "wtf"}, r, False),
        ({"type": "full_name_contains", "k": 5, "substr": ""}, r, False),
        ({"type": "bogus_type"}, r, False),
        ({}, r, False),
    ]
    fails = []
    for pred, res, expected in cases:
        out = evaluate(pred, res)
        if out.passed != expected:
            fails.append((pred, res, expected, out))
    assert not fails, "predicates failed: " + "\n".join(f"  {f}" for f in fails)

    out = evaluate({"type": "name_in_top_k", "k": 5, "values": ["СтрДлина"]}, r)
    assert out.match_rank == 2
    out = evaluate({"type": "name_in_top_k", "k": 5, "values": ["НетТакого"]}, r)
    assert out.match_rank is None
    out = evaluate({"type": "non_empty"}, r)
    assert out.match_rank is None
    out = evaluate({"type": "bogus"}, r)
    assert out.detail.get("error") == "unknown_predicate_type"

    print(f"[1/5] predicates: OK ({len(cases)} cases + rank checks)")


def test_metrics():
    ex = [
        {"id": "a", "tool": "t", "args": {}, "notes": "", "ok": True, "error": None,
         "is_error_flag": False, "duration_ms": 100.0, "search_type": "hybrid",
         "hard": [
             {"type": "non_empty", "passed": True, "detail": {}},
             {"type": "name_in_top_k", "passed": True, "detail": {}},
         ],
         "soft": [], "mrr_rank": 1, "mrr_max_k": 5, "response_preview": None},
        {"id": "b", "tool": "t", "args": {}, "notes": "", "ok": True, "error": None,
         "is_error_flag": False, "duration_ms": 200.0, "search_type": "hybrid",
         "hard": [{"type": "name_in_top_k", "passed": True, "detail": {}}],
         "soft": [{"type": "any_hit_kind", "passed": False, "detail": {}}],
         "mrr_rank": 3, "mrr_max_k": 5, "response_preview": None},
        {"id": "c", "tool": "t", "args": {}, "notes": "", "ok": True, "error": None,
         "is_error_flag": False, "duration_ms": 500.0, "search_type": "dense_only",
         "hard": [{"type": "name_in_top_k", "passed": False, "detail": {}}],
         "soft": [], "mrr_rank": None, "mrr_max_k": 5, "response_preview": None},
        {"id": "d", "tool": "t", "args": {}, "notes": "", "ok": False, "error": "timeout",
         "is_error_flag": False, "duration_ms": 60000.0, "search_type": None,
         "hard": [], "soft": [], "mrr_rank": None, "mrr_max_k": None,
         "response_preview": None},
        {"id": "e", "tool": "t", "args": {}, "notes": "", "ok": True, "error": None,
         "is_error_flag": False, "duration_ms": 80.0, "search_type": "hybrid",
         "hard": [{"type": "non_empty", "passed": True, "detail": {}}],
         "soft": [], "mrr_rank": None, "mrr_max_k": None, "response_preview": None},
    ]
    a = aggregate(ex)
    assert a["hard_passed"] == 3
    assert a["hard_pass_rate"] == 3 / 5
    assert a["examples_with_mrr"] == 3
    assert abs(a["recall_at_1"] - 1 / 3) < 1e-9
    assert abs(a["recall_at_5"] - 2 / 3) < 1e-9
    assert abs(a["mrr"] - (1 + 1 / 3 + 0) / 3) < 1e-9
    assert a["soft_total"] == 1
    assert a["soft_pass_rate"] == 0.0
    assert a["transport_errors"] == 1
    assert a["search_type_dist"] == {"hybrid": 3, "dense_only": 1, "unknown": 1}

    empty = aggregate([])
    assert empty["total"] == 0
    assert empty["hard_pass_rate"] == 0.0
    assert empty["mrr"] is None

    print(f"[2/5] metrics: OK (hard={a['hard_passed']}/{a['total']}, "
          f"MRR={a['mrr']:.3f}, recall@5={a['recall_at_5']:.3f})")


def test_load_dataset():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("// комментарий\n")
        f.write("\n")
        f.write('{"id":"t1","tool":"platform_help_search","args":{"query":"q"},"expect":{"hard":[],"soft":[]}}\n')
        f.write('{"id":"t2","tool":"platform_help_search","args":{},"expect":{}}\n')
        f.write("not-json-{broken\n")
        f.write("123\n")
        f.write('{"no_id":true}\n')
        path = Path(f.name)

    loaded = load_dataset(path)
    assert len(loaded) == 2
    assert loaded[0]["id"] == "t1"
    assert loaded[1]["id"] == "t2"

    try:
        load_dataset(Path("/tmp/__not_exists__.jsonl"))
        assert False
    except FileNotFoundError:
        pass

    real = load_dataset(Path(__file__).resolve().parents[1] / "datasets" / "platform_help.jsonl")
    assert len(real) == 10
    for ex in real:
        assert ex["tool"] in ("platform_help_search", "platform_help_lookup")
        assert "hard" in ex["expect"] and "soft" in ex["expect"]

    print(f"[3/5] load_dataset: OK (шаблон: {len(real)} примеров)")


def test_mrr_info():
    ex = {"expect": {"hard": [{"type": "non_empty"}, {"type": "name_in_top_k", "k": 5, "values": ["X"]}]}}
    outcomes = [
        PredicateOutcome(type="non_empty", passed=True, detail={}),
        PredicateOutcome(type="name_in_top_k", passed=True, detail={}, match_rank=2),
    ]
    r, k = _mrr_info_from_hard(ex, outcomes)
    assert (r, k) == (2, 5)

    outcomes2 = [
        PredicateOutcome(type="non_empty", passed=True, detail={}),
        PredicateOutcome(type="name_in_top_k", passed=False, detail={}, match_rank=None),
    ]
    r, k = _mrr_info_from_hard(ex, outcomes2)
    assert (r, k) == (None, 5)

    ex3 = {"expect": {"hard": [{"type": "non_empty"}]}}
    out3 = [PredicateOutcome(type="non_empty", passed=True, detail={})]
    r, k = _mrr_info_from_hard(ex3, out3)
    assert (r, k) == (None, None)

    print("[4/5] _mrr_info_from_hard: OK")


class FakeSession:
    """Имитация MCPSession для тестов."""
    def __init__(self, responses):
        self._responses = responses

    async def call_tool(self, name, arguments):
        key = (arguments.get("query") or arguments.get("name") or "__default__")
        return self._responses.get(key, self._responses.get("__default__"))


async def _async_test_run_one_and_report():
    ok_response = ToolCallResult(
        ok=True, parsed={
            "search_type": "hybrid",
            "results": [
                {"name_ru": "СтрРазделить", "name_en": "StrSplit", "full_name": "СтрРазделить", "kind": "method"},
                {"name_ru": "СтрДлина", "name_en": "StrLen", "full_name": "СтрДлина", "kind": "method"},
            ] + [{"name_ru": f"X{i}", "full_name": f"X{i}", "kind": "method"} for i in range(10)],
        },
        raw_text="...", error=None, duration_ms=150.5, is_error_flag=False,
    )
    empty_response = ToolCallResult(
        ok=True, parsed={"search_type": "dense_only", "results": []},
        raw_text="...", error=None, duration_ms=50.0, is_error_flag=False,
    )
    transport_err = ToolCallResult(
        ok=False, parsed=None, raw_text=None,
        error="timeout after 60s", duration_ms=60000.0, is_error_flag=False,
    )

    session = FakeSession({
        "разделить": ok_response,
        "пусто": empty_response,
        "сбой": transport_err,
    })

    examples = [
        {"id": "ok-001", "tool": "platform_help_search",
         "args": {"query": "разделить"}, "notes": "норм",
         "expect": {
             "hard": [
                 {"type": "non_empty"},
                 {"type": "name_in_top_k", "k": 5, "values": ["СтрРазделить"]},
             ],
             "soft": [{"type": "any_hit_kind", "k": 5, "kinds": ["method"]}],
         }},
        {"id": "empty-002", "tool": "platform_help_search",
         "args": {"query": "пусто"}, "notes": "пустой ответ",
         "expect": {
             "hard": [
                 {"type": "non_empty"},
                 {"type": "name_in_top_k", "k": 5, "values": ["СтрРазделить"]},
             ],
             "soft": [],
         }},
        {"id": "err-003", "tool": "platform_help_search",
         "args": {"query": "сбой"}, "notes": "транспорт",
         "expect": {"hard": [{"type": "non_empty"}], "soft": []}},
    ]

    results = []
    for ex in examples:
        r = await run_one(session, ex)
        results.append(r)

    r0 = results[0]
    assert r0["ok"] is True
    assert r0["search_type"] == "hybrid"
    assert all(p["passed"] for p in r0["hard"])
    assert r0["mrr_rank"] == 1
    assert r0["mrr_max_k"] == 5
    assert len(r0["response_preview"]["results"]) == 5
    assert r0["response_preview"]["search_type"] == "hybrid"

    r1 = results[1]
    assert r1["ok"] is True
    assert not any(p["passed"] for p in r1["hard"])
    assert r1["mrr_rank"] is None
    assert r1["mrr_max_k"] == 5
    assert r1["response_preview"]["results"] == []

    r2 = results[2]
    assert r2["ok"] is False
    assert "timeout" in (r2["error"] or "")
    assert r2["hard"] == []
    assert r2["mrr_rank"] is None and r2["mrr_max_k"] is None

    summary = aggregate(results)
    assert summary["hard_passed"] == 1
    assert summary["hard_pass_rate"] == 1 / 3
    assert summary["transport_errors"] == 1
    assert summary["examples_with_mrr"] == 2

    started = utcnow()
    finished = utcnow()
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        dataset_path = Path("/fake/mydataset.jsonl")
        json_p, md_p = generate_reports(
            out_dir=out_dir, dataset_path=dataset_path,
            tool_endpoint="http://fake:8003/sse",
            examples=results, aggregate_summary=summary,
            started_at=started, finished_at=finished,
        )
        assert json_p.exists() and md_p.exists()

        payload = json.loads(json_p.read_text(encoding="utf-8"))
        assert payload["summary"]["hard_passed"] == 1
        assert len(payload["examples"]) == 3

        md = md_p.read_text(encoding="utf-8")
        assert "Eval report" in md
        assert "## Summary" in md
        assert "## Per-example" in md
        assert "## Failures (hard)" in md
        assert "empty-002" in md
        assert "err-003" in md
        assert "**ERROR**" in md

    print("[5/5] run_one + report: OK")


def test_run_one_and_report():
    asyncio.run(_async_test_run_one_and_report())


if __name__ == "__main__":
    test_predicates()
    test_metrics()
    test_load_dataset()
    test_mrr_info()
    test_run_one_and_report()
    print("\n=== all tests passed ===")
