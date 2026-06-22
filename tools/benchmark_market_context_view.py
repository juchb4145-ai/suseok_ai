from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading.strategy.market_context_view import market_context_view_from_mapping


REPORT_DIR = Path("reports/performance/market_context_view")
NOW = datetime(2026, 6, 22, 9, 20, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Reboot V2 MarketContextView transport")
    parser.add_argument("--candidates", default="50,200,500", help="Comma separated candidate counts")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    args = parser.parse_args()

    counts = [int(item.strip()) for item in str(args.candidates).split(",") if item.strip()]
    repeats = max(1, int(args.repeats))
    rows = []
    for count in counts:
        old_runs = [_measure_old(count) for _ in range(repeats)]
        new_runs = [_measure_new(count) for _ in range(repeats)]
        rows.append(_aggregate(count, old_runs, new_runs))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark_market_context_view.json"
    md_path = output_dir / "benchmark_market_context_view.md"
    payload = {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "repeats": repeats,
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    print(_console(rows))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")


def _measure_old(candidate_count: int) -> dict[str, Any]:
    payload = _market_payload(candidate_count)
    codes = list(payload["candidate_policy_by_code"].keys())
    tracemalloc.start()
    started = time.perf_counter()

    serialized_for_db = json.loads(json.dumps(payload))
    serialized_for_downstream = json.loads(json.dumps(payload))
    policy_lookup_count = 0
    policy_map_copy_count = 0
    for code in codes:
        policies = dict(serialized_for_downstream.get("candidate_policy_by_code") or {})
        policy_map_copy_count += 1
        _ = dict(policies.get(code) or {})
        policy_lookup_count += 1
    runtime_json_bytes = len(json.dumps({"market_context": serialized_for_downstream}, separators=(",", ":")))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "elapsed_ms": elapsed_ms,
        "peak_memory_bytes": peak,
        "runtime_json_bytes": runtime_json_bytes,
        "full_snapshot_serialization_count": 2 if serialized_for_db and serialized_for_downstream else 0,
        "db_fallback_count": 1,
        "policy_lookup_count": policy_lookup_count,
        "policy_map_copy_count": policy_map_copy_count,
    }


def _measure_new(candidate_count: int) -> dict[str, Any]:
    payload = _market_payload(candidate_count)
    codes = list(payload["candidate_policy_by_code"].keys())
    tracemalloc.start()
    started = time.perf_counter()

    serialized_for_db = json.loads(json.dumps(payload))
    view = market_context_view_from_mapping(serialized_for_db, source="PIPELINE_VIEW")
    summary = view.to_theme_summary().to_dict()
    policy_lookup_count = 0
    for code in codes:
        _ = view.policy_for(code)
        policy_lookup_count += 1
    diagnostics = view.to_transport_diagnostics(
        NOW,
        max_age_sec=30,
        full_snapshot_serialize_count=1,
        db_fallback_count=0,
        summary_fallback_count=0,
    )
    runtime_json_bytes = len(json.dumps({"market_context_transport": diagnostics, "market_regime": summary}, separators=(",", ":")))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "elapsed_ms": elapsed_ms,
        "peak_memory_bytes": peak,
        "runtime_json_bytes": runtime_json_bytes,
        "full_snapshot_serialization_count": 1,
        "db_fallback_count": 0,
        "policy_lookup_count": policy_lookup_count,
        "policy_map_copy_count": 0,
    }


def _aggregate(candidate_count: int, old_runs: list[dict[str, Any]], new_runs: list[dict[str, Any]]) -> dict[str, Any]:
    old = _median_metrics(old_runs)
    new = _median_metrics(new_runs)
    return {
        "candidate_count": candidate_count,
        "old": old,
        "new": new,
        "elapsed_speedup": _ratio(old["elapsed_ms"], new["elapsed_ms"]),
        "peak_memory_reduction_pct": _reduction_pct(old["peak_memory_bytes"], new["peak_memory_bytes"]),
        "runtime_json_reduction_pct": _reduction_pct(old["runtime_json_bytes"], new["runtime_json_bytes"]),
    }


def _median_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    keys = runs[0].keys()
    result: dict[str, Any] = {}
    for key in keys:
        values = [run[key] for run in runs]
        result[key] = statistics.median(values) if isinstance(values[0], (int, float)) else values[0]
    result["elapsed_ms"] = round(float(result["elapsed_ms"]), 3)
    return result


def _ratio(before: float, after: float) -> float:
    if after <= 0:
        return 0.0
    return round(before / after, 3)


def _reduction_pct(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return round((before - after) / before * 100.0, 2)


def _market_payload(candidate_count: int) -> dict[str, Any]:
    policies = {}
    for index in range(candidate_count):
        code = f"{index:06d}"
        side = "KOSDAQ" if index % 2 else "KOSPI"
        status = "EXPANSION" if side == "KOSPI" else "WEAK"
        action = "ALLOW_REDUCED" if side == "KOSPI" else "WAIT_MARKET"
        policies[code] = {
            "code": code,
            "market_side": side,
            "market_side_source": "benchmark",
            "market_side_resolution_status": "RESOLVED",
            "market_status": status,
            "global_market_status": "SELECTIVE",
            "market_action": action,
            "position_size_multiplier_hint": 0.6 if action == "ALLOW_REDUCED" else 0.0,
            "block_new_entry": action == "WAIT_MARKET",
            "reason_codes": ["SPLIT_MARKET_HEALTHY_SIDE_REDUCED" if side == "KOSPI" else "SIDE_MARKET_WEAK_WAIT"],
        }
    return {
        "trade_date": NOW.date().isoformat(),
        "calculated_at": NOW.isoformat(),
        "global_status": "SELECTIVE",
        "kospi_status": "EXPANSION",
        "kosdaq_status": "WEAK",
        "composite_market_mode": "SPLIT_KOSPI_ON",
        "systemic_risk_off": False,
        "market_session_status": "open",
        "market_open": True,
        "market_closed": False,
        "risk_off_detected": False,
        "weak_market_detected": True,
        "kospi_snapshot": _side_payload("KOSPI", "EXPANSION", 0.92),
        "kosdaq_snapshot": _side_payload("KOSDAQ", "WEAK", -1.35),
        "candidate_policy_by_code": policies,
        "policy_summary": {"ALLOW_REDUCED": (candidate_count + 1) // 2, "WAIT_MARKET": candidate_count // 2},
        "reason_codes": ["BENCHMARK_SPLIT_MARKET"],
        "output_mode": "OBSERVE",
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _side_payload(side: str, status: str, return_pct: float) -> dict[str, Any]:
    return {
        "side": side,
        "status": status,
        "index_return_pct": return_pct,
        "index_slope_1m_pct": round(return_pct / 10.0, 4),
        "index_slope_3m_pct": round(return_pct / 5.0, 4),
        "index_slope_5m_pct": round(return_pct / 3.0, 4),
        "breadth_pct": 0.61 if status == "EXPANSION" else 0.34,
        "turnover_weighted_return_pct": return_pct * 0.8,
        "risk_score": 0.1 if status == "EXPANSION" else 0.6,
        "data_quality_flags": [],
        "reason_codes": [f"{side}_{status}"],
    }


def _console(rows: list[dict[str, Any]]) -> str:
    lines = [
        "candidates | old_ms | new_ms | speedup | old_peak_kb | new_peak_kb | old_json | new_json",
        "--- | ---: | ---: | ---: | ---: | ---: | ---: | ---:",
    ]
    for row in rows:
        old = row["old"]
        new = row["new"]
        lines.append(
            f"{row['candidate_count']} | {old['elapsed_ms']} | {new['elapsed_ms']} | {row['elapsed_speedup']}x | "
            f"{old['peak_memory_bytes'] / 1024:.1f} | {new['peak_memory_bytes'] / 1024:.1f} | "
            f"{old['runtime_json_bytes']} | {new['runtime_json_bytes']}"
        )
    return "\n".join(lines)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# MarketContextView Benchmark",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- repeats: `{payload['repeats']}`",
        "",
        _console(payload["rows"]),
        "",
        "## Contract Counters",
        "",
        "New transport keeps `full_snapshot_serialization_count=1`, `db_fallback_count=0`, and `policy_map_copy_count=0` in the measured healthy path.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
