from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kiwoom.chejan import ChejanParseStatus, KiwoomChejanParser  # noqa: E402
from trading.broker.chejan_capture import validate_redaction  # noqa: E402
from trading.broker.models import GatewayEvent  # noqa: E402
from trading_app.gateway_event_consumer import GatewayEventCodec  # noqa: E402


REQUIRED_CASES = {
    "order_accepted",
    "order_rejected",
    "partial_fill",
    "full_fill",
    "cancel_accepted",
    "cancelled",
    "balance_increase",
    "balance_zero",
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate_fixture_dir(args.fixture_dir, output_dir=args.output_dir)
    except Exception as exc:
        print(f"validation error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": report["status"], "recommendation": report["recommendation"], "output_dir": report["output_dir"]}, ensure_ascii=False, sort_keys=True))
    if report["status"] == "PASS":
        return 0
    if report["status"] == "HOLD":
        return 2
    return 1


def validate_fixture_dir(fixture_dir: str | Path, *, output_dir: str | Path) -> dict[str, Any]:
    fixture_path = Path(fixture_dir).expanduser()
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)
    manifest_path = fixture_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    parser = KiwoomChejanParser()
    codec = GatewayEventCodec()
    cases = list(manifest.get("cases") or [])
    if not cases:
        cases = [{"case_id": path.stem, "file": path.name, "expected_event_kind": ""} for path in fixture_path.glob("*.json") if path.name != "manifest.json"]
    results: list[dict[str, Any]] = []
    unknown_fids: Counter[str] = Counter()
    field_coverage: Counter[str] = Counter()
    classification: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    covered_cases: set[str] = {str(item.get("case_id") or item.get("name") or "") for item in cases if item.get("case_id") or item.get("name")}
    observed_broker_envs: set[str] = set()
    for case in cases:
        case_id = str(case.get("case_id") or case.get("name") or "")
        file_name = str(case.get("file") or f"{case_id}.json")
        payload = json.loads((fixture_path / file_name).read_text(encoding="utf-8"))
        if payload.get("broker_env"):
            observed_broker_envs.add(str(payload.get("broker_env") or "").upper())
        raw_fids = dict(payload.get("raw_fids") or payload.get("fids") or {})
        result = parser.parse(
            gubun=str(payload.get("gubun", case.get("gubun", ""))),
            item_count=int(payload.get("item_count", len(raw_fids)) or 0),
            fid_list=str(payload.get("fid_list", "")),
            raw_fids=raw_fids,
        )
        event = GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload(), event_id=f"fixture-{case_id}")
        decoded = codec.decode(event)
        redaction = validate_redaction(payload)
        for fid in result.present_fids:
            field_coverage[str(fid)] += 1
        for fid in result.unknown_fids:
            unknown_fids[str(fid)] += 1
        expected_kind = str(case.get("expected_event_kind") or "")
        expected_canonical = str(case.get("expected_canonical") or "")
        case_failures = []
        if expected_kind and result.event_kind != expected_kind:
            case_failures.append({"case_id": case_id, "reason": "EVENT_KIND_MISMATCH", "expected": expected_kind, "actual": result.event_kind})
        if expected_canonical and decoded.canonical_type != expected_canonical:
            case_failures.append({"case_id": case_id, "reason": "CANONICAL_MISMATCH", "expected": expected_canonical, "actual": decoded.canonical_type})
        if not redaction["ok"]:
            case_failures.append({"case_id": case_id, "reason": "REDACTION_FAILED", "leaks": redaction["leaks"]})
        if result.parse_status == ChejanParseStatus.INVALID and str(case.get("expected_status") or "") != ChejanParseStatus.INVALID.value:
            case_failures.append({"case_id": case_id, "reason": "UNEXPECTED_INVALID", "missing": result.missing_required_fields})
        covered_cases.add(_coverage_case_id(result))
        failures.extend(case_failures)
        classification.append(
            {
                "case_id": case_id,
                "gubun": result.gubun,
                "event_kind": result.event_kind,
                "gateway_event_type": result.gateway_event_type,
                "canonical_type": decoded.canonical_type,
                "parse_status": str(result.parse_status.value if isinstance(result.parse_status, ChejanParseStatus) else result.parse_status),
            }
        )
        results.append(
            {
                "case_id": case_id,
                "status": "FAIL" if case_failures else "PASS",
                "failures": case_failures,
                "parse_result": result.to_dict(),
                "decoded": {
                    "canonical_type": decoded.canonical_type,
                    "ignored": decoded.ignored,
                    "ignore_reason": decoded.ignore_reason,
                    "dedupe_key": decoded.dedupe_key,
                },
            }
        )
    source = str(manifest.get("source") or "")
    if not source:
        source = "KIWOOM_SIMULATION" if "SIMULATION" in observed_broker_envs else "SYNTHETIC"
    missing_cases = sorted(REQUIRED_CASES - covered_cases)
    status = "FAIL" if failures else "PASS"
    recommendation = "READY_FOR_RECONCILE_TR_PILOT"
    if source != "KIWOOM_SIMULATION" or missing_cases:
        status = "HOLD" if not failures else "FAIL"
        recommendation = "READY_FOR_KIWOOM_PARSER_VALIDATION"
    report = {
        "status": status,
        "recommendation": recommendation,
        "fixture_dir": str(fixture_path),
        "output_dir": str(output_path),
        "manifest": manifest,
        "source": source,
        "case_count": len(results),
        "missing_required_cases": missing_cases,
        "results": results,
        "failures": failures,
        "field_coverage": dict(field_coverage),
        "unknown_fids": dict(unknown_fids),
        "classification_matrix": classification,
    }
    (output_path / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (output_path / "field_coverage.json").write_text(json.dumps(dict(field_coverage), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_path / "unknown_fids.json").write_text(json.dumps(dict(unknown_fids), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_path / "classification_matrix.json").write_text(json.dumps(classification, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_path / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_path / "summary.md").write_text(_summary(report), encoding="utf-8")
    return report


def _summary(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Kiwoom Chejan Parser Validation",
            "",
            f"- Status: `{report['status']}`",
            f"- Recommendation: `{report['recommendation']}`",
            f"- Source: `{report['source']}`",
            f"- Case count: `{report['case_count']}`",
            f"- Missing required cases: `{', '.join(report['missing_required_cases']) or 'none'}`",
            f"- Failure count: `{len(report['failures'])}`",
            "",
        ]
    )


def _coverage_case_id(result: Any) -> str:
    kind = str(getattr(result, "event_kind", "") or "")
    payload = dict(getattr(result, "canonical_payload", {}) or {})
    if kind == "order_fill":
        remaining = payload.get("remaining_quantity")
        try:
            return "full_fill" if int(remaining or 0) <= 0 else "partial_fill"
        except (TypeError, ValueError):
            return "partial_fill"
    if kind == "position_delta":
        try:
            return "balance_zero" if int(payload.get("quantity") or 0) <= 0 else "balance_increase"
        except (TypeError, ValueError):
            return "balance_increase"
    return {
        "order_accepted": "order_accepted",
        "order_rejected": "order_rejected",
        "order_cancel_accepted": "cancel_accepted",
        "order_cancelled": "cancelled",
    }.get(kind, kind)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate sanitized Kiwoom Chejan fixtures against the canonical parser.")
    parser.add_argument("--fixture-dir", required=True)
    parser.add_argument("--output-dir", default="reports/kiwoom_chejan_validation")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
