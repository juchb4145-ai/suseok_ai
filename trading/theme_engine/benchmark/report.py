from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "trade_date",
    "external_theme_name",
    "internal_theme_id",
    "internal_theme_name",
    "external_rank",
    "internal_rank",
    "member_jaccard_score",
    "top5_overlap_ratio",
    "leader_match",
    "mismatch_reasons",
    "external_top5",
    "internal_top5",
    "external_only_stocks",
    "internal_only_stocks",
]


def write_benchmark_reports(result: dict, out_dir: str | Path, trade_date: str) -> dict:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"theme_benchmark_{trade_date}"
    paths = {
        "json": output_dir / f"{stem}.json",
        "csv": output_dir / f"{stem}.csv",
        "markdown": output_dir / f"{stem}.md",
    }
    paths["json"].write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(result, paths["csv"], trade_date)
    paths["markdown"].write_text(_markdown(result, trade_date), encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


def _write_csv(result: dict, path: Path, trade_date: str) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for theme in list(result.get("themes") or []):
            writer.writerow(_csv_row(result, theme, trade_date))


def _csv_row(result: dict, theme: dict, trade_date: str) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "external_theme_name": theme.get("external_theme_name", ""),
        "internal_theme_id": theme.get("internal_theme_id", ""),
        "internal_theme_name": theme.get("internal_theme_name", ""),
        "external_rank": theme.get("external_rank", ""),
        "internal_rank": theme.get("internal_rank", ""),
        "member_jaccard_score": theme.get("member_jaccard_score", ""),
        "top5_overlap_ratio": theme.get("top5_overlap_ratio", ""),
        "leader_match": theme.get("leader_match", ""),
        "mismatch_reasons": _join(theme.get("mismatch_reasons")),
        "external_top5": _join(_theme_top5(result, theme, "external")),
        "internal_top5": _join(_theme_top5(result, theme, "internal")),
        "external_only_stocks": _join(theme.get("external_only_stocks")),
        "internal_only_stocks": _join(theme.get("internal_only_stocks")),
    }


def _markdown(result: dict, trade_date: str) -> str:
    summary = dict(result.get("summary") or {})
    lines = [
        "# Theme Benchmark Report",
        "",
        "## Summary",
        f"- trade_date: {trade_date}",
        f"- source: {result.get('source', '')}",
        f"- matched_theme_count: {summary.get('matched_theme_count', 0)}",
        f"- avg_member_jaccard: {summary.get('avg_member_jaccard', 0)}",
        f"- avg_top5_overlap: {summary.get('avg_top5_overlap', 0)}",
        f"- leader_match_rate: {summary.get('leader_match_rate', 0)}",
        "",
        "## Top Matched Themes",
    ]
    lines.extend(_theme_table(_top_matched(result)))
    lines.extend(["", "## Top Mismatched Themes"])
    lines.extend(_theme_table(_top_mismatched(result)))
    lines.extend(["", "## External-only Themes"])
    lines.extend(_external_only_table(result))
    lines.extend(["", "## Internal-only Themes"])
    lines.extend(_internal_only_table(result))
    lines.extend(["", "## Alias Candidates"])
    lines.extend(_alias_candidate_table(result))
    lines.extend(["", "## Leader Mismatch Details"])
    lines.extend(_leader_mismatch_table(result))
    return "\n".join(lines).rstrip() + "\n"


def _top_matched(result: dict) -> list[dict]:
    return sorted(list(result.get("themes") or []), key=lambda item: float(item.get("top5_overlap_ratio") or 0), reverse=True)[:10]


def _top_mismatched(result: dict) -> list[dict]:
    return sorted(
        list(result.get("themes") or []),
        key=lambda item: (len(list(item.get("mismatch_reasons") or [])), -float(item.get("top5_overlap_ratio") or 0)),
        reverse=True,
    )[:10]


def _theme_table(themes: list[dict]) -> list[str]:
    lines = [
        "| External Theme | Internal Theme | Ranks | Member Jaccard | Top5 Overlap | Leader | Reasons |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    if not themes:
        lines.append("| - | - | - | - | - | - | - |")
        return lines
    for theme in themes:
        lines.append(
            "| {external} | {internal} | {ranks} | {member:.4g} | {top5:.4g} | {leader} | {reasons} |".format(
                external=_md(theme.get("external_theme_name")),
                internal=_md(theme.get("internal_theme_name")),
                ranks=f"{theme.get('external_rank', '')}/{theme.get('internal_rank', '')}",
                member=float(theme.get("member_jaccard_score") or 0),
                top5=float(theme.get("top5_overlap_ratio") or 0),
                leader="match" if theme.get("leader_match") else "mismatch",
                reasons=_md(_join(theme.get("mismatch_reasons")) or "-"),
            )
        )
    return lines


def _external_only_table(result: dict) -> list[str]:
    rows = list(result.get("missing_external_themes") or [])
    lines = ["| External Theme | Hint | Rank | Reasons |", "| --- | --- | ---: | --- |"]
    if not rows:
        lines.append("| - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            f"| {_md(row.get('external_theme_name'))} | {_md(row.get('canonical_theme_hint'))} | {row.get('external_rank', '')} | {_md(_join(row.get('mismatch_reasons')))} |"
        )
    return lines


def _internal_only_table(result: dict) -> list[str]:
    rows = list(result.get("internal_only_themes") or [])
    lines = ["| Internal Theme ID | Internal Theme | Rank | Reasons |", "| --- | --- | ---: | --- |"]
    if not rows:
        lines.append("| - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            f"| {_md(row.get('internal_theme_id'))} | {_md(row.get('internal_theme_name'))} | {row.get('internal_rank', '')} | {_md(_join(row.get('mismatch_reasons')))} |"
        )
    return lines


def _alias_candidate_table(result: dict) -> list[str]:
    rows = [
        row
        for row in list(result.get("alias_candidates") or [])
        if "THEME_ALIAS_MISSING" in list(row.get("mismatch_reasons") or [])
    ]
    lines = ["| External Theme | Hint | Normalized External | Normalized Hint |", "| --- | --- | --- | --- |"]
    if not rows:
        lines.append("| - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            "| {external} | {hint} | {normalized_external} | {normalized_hint} |".format(
                external=_md(row.get("external_theme_name")),
                hint=_md(row.get("canonical_theme_hint")),
                normalized_external=_md(row.get("normalized_external_theme_name")),
                normalized_hint=_md(row.get("normalized_canonical_theme_hint")),
            )
        )
    return lines


def _leader_mismatch_table(result: dict) -> list[str]:
    rows = [theme for theme in list(result.get("themes") or []) if not bool(theme.get("leader_match"))]
    lines = ["| External Theme | Internal Theme | Leader Rank Delta | Reasons |", "| --- | --- | ---: | --- |"]
    if not rows:
        lines.append("| - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            f"| {_md(row.get('external_theme_name'))} | {_md(row.get('internal_theme_name'))} | {row.get('leader_rank_delta', '')} | {_md(_join(row.get('mismatch_reasons')))} |"
        )
    return lines


def _theme_top5(result: dict, theme: dict, side: str) -> list[str]:
    key = "external_top5_by_theme" if side == "external" else "internal_top5_by_theme"
    values = dict(result.get(key) or {})
    theme_key = theme.get("external_theme_name") if side == "external" else theme.get("internal_theme_id")
    return list(values.get(theme_key) or [])


def _join(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, str):
        return values
    return ",".join(str(value) for value in list(values or []))


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
