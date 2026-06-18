from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading.reliability.models import ReliabilityReport


class ReliabilityReportWriter:
    def __init__(self, *, output_dir: str | Path = "reports/reliability") -> None:
        self.output_dir = Path(output_dir).expanduser()

    def write(self, report: ReliabilityReport) -> dict[str, str]:
        run_dir = Path(report.report_dir or self.output_dir / report.run_id).expanduser()
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = report.to_dict()
        paths = {
            "qualification": run_dir / "qualification.json",
            "summary": run_dir / "summary.md",
            "metrics": run_dir / "metrics.json",
            "scenario_results": run_dir / "scenario_results.json",
            "failures": run_dir / "failures.json",
        }
        paths["qualification"].write_text(_json(payload), encoding="utf-8")
        paths["metrics"].write_text(_json(payload.get("metrics") or {}), encoding="utf-8")
        paths["scenario_results"].write_text(_json(payload.get("scenarios") or []), encoding="utf-8")
        paths["failures"].write_text(_json({"failures": payload.get("failures") or [], "hard_gate_failures": payload.get("hard_gate_failures") or []}), encoding="utf-8")
        paths["summary"].write_text(_summary_markdown(payload), encoding="utf-8")
        return {key: str(value) for key, value in paths.items()}


class ReliabilityReportReader:
    def __init__(self, *, output_dir: str | Path = "reports/reliability") -> None:
        self.output_dir = Path(output_dir).expanduser()

    def latest(self) -> dict[str, Any]:
        runs = self.list_runs(limit=1)
        if not runs:
            return {"status": "NOT_RUN", "runs": []}
        return self.get_run(str(runs[0]["run_id"]))

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.output_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in self.output_dir.iterdir():
            report_path = path / "qualification.json"
            if not report_path.exists():
                continue
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append(
                {
                    "run_id": payload.get("run_id") or path.name,
                    "profile": payload.get("profile", ""),
                    "status": payload.get("status", ""),
                    "recommendation": payload.get("recommendation", ""),
                    "started_at": payload.get("started_at", ""),
                    "finished_at": payload.get("finished_at", ""),
                    "duration_sec": payload.get("duration_sec", 0),
                    "report_dir": str(path),
                }
            )
        items.sort(key=lambda item: str(item.get("finished_at") or item.get("started_at") or ""), reverse=True)
        return items[: max(0, int(limit))]

    def get_run(self, run_id: str) -> dict[str, Any]:
        safe = Path(str(run_id)).name
        report_path = self.output_dir / safe / "qualification.json"
        if not report_path.exists():
            return {"status": "NOT_FOUND", "run_id": safe}
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"status": "ERROR", "run_id": safe, "error": str(exc)}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def _summary_markdown(report: dict[str, Any]) -> str:
    scenarios = list(report.get("scenarios") or [])
    failures = list(report.get("failures") or []) + list(report.get("hard_gate_failures") or [])
    not_run = list(report.get("not_run") or [])
    lines = [
        f"# Reliability Qualification {report.get('run_id', '')}",
        "",
        f"- Profile: `{report.get('profile', '')}`",
        f"- Status: `{report.get('status', '')}`",
        f"- Recommendation: `{report.get('recommendation', '')}`",
        f"- Duration: `{report.get('duration_sec', 0):.2f}s`",
        f"- Scenario count: `{len(scenarios)}`",
        f"- Failure count: `{len(failures)}`",
        f"- Not run: `{len(not_run)}`",
        f"- Order command count: `{report.get('metrics', {}).get('order_command_count', 0)}`",
        "",
        "## Executed Scenarios",
    ]
    for item in scenarios:
        lines.append(f"- `{item.get('scenario_id', '')}`: `{item.get('status', '')}`")
    if not_run:
        lines.extend(["", "## Not Run"])
        lines.extend(f"- `{item}`" for item in not_run)
    if failures:
        lines.extend(["", "## Failures"])
        for item in failures:
            lines.append(f"- `{item.get('metric') or item.get('scenario_id') or item.get('reason')}`: {item}")
    lines.extend(
        [
            "",
            "## Safety Evidence",
            "",
            f"- `TRADING_SEND_ORDER_ALLOWED`: `{report.get('config', {}).get('send_order_allowed', False)}`",
            f"- `observe_only`: `{report.get('config', {}).get('observe_only', True)}`",
            f"- `db_path`: `{report.get('config', {}).get('db_path', '')}`",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["ReliabilityReportReader", "ReliabilityReportWriter"]
