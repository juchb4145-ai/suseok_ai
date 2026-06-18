from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.db import TradingDatabase
from trading.strategy.models import Candidate, CandidateEvent, CandidateSourceType, CandidateState
from trading.theme_engine.repository import ThemeEngineRepository


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"
DEFAULT_REASON = "HYDRATION_STALE_CLEANUP"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean Reboot V2 stale HYDRATING candidates.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path. Defaults to data/trader.sqlite3.")
    parser.add_argument("--trade-date", default="", help="Trade date to clean. Defaults to today.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Default is dry-run.")
    parser.add_argument("--enrich-themes", action="store_true", help="Attach current theme membership context while cleaning.")
    parser.add_argument("--sample-limit", type=int, default=20, help="Number of sample candidates to include.")
    return parser.parse_args()


def analyze_cleanup(
    db: TradingDatabase,
    *,
    trade_date: str,
    enrich_themes: bool = False,
    sample_limit: int = 20,
) -> dict[str, Any]:
    candidates = db.list_candidates(trade_date=trade_date, state=CandidateState.HYDRATING)
    return {
        "trade_date": trade_date,
        "mode": "DRY_RUN",
        "eligible_state": CandidateState.HYDRATING.value,
        "target_state": CandidateState.WAIT_DATA.value,
        "eligible_count": len(candidates),
        "enrich_themes": bool(enrich_themes),
        "state_counts": _state_counts(db, trade_date),
        "sample": [_candidate_sample(candidate) for candidate in candidates[: max(0, sample_limit)]],
    }


def apply_cleanup(
    db: TradingDatabase,
    *,
    trade_date: str,
    enrich_themes: bool = False,
    now: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    timestamp = now or _now()
    candidates = db.list_candidates(trade_date=trade_date, state=CandidateState.HYDRATING)
    repo = ThemeEngineRepository(db) if enrich_themes else None
    cleaned: list[dict[str, Any]] = []
    for candidate in candidates:
        previous_state = candidate.state
        metadata = dict(candidate.metadata or {})
        metadata["candidate_hydration"] = {
            **dict(metadata.get("candidate_hydration") or {}),
            "status": CandidateState.WAIT_DATA.value,
            "reason": DEFAULT_REASON,
            "cleaned_at": timestamp,
        }
        metadata["wait_data_reason"] = DEFAULT_REASON
        metadata["reason_codes"] = _dedupe([*list(metadata.get("reason_codes") or []), "WAIT_DATA", DEFAULT_REASON])
        candidate.metadata = metadata
        candidate.state = CandidateState.WAIT_DATA
        if enrich_themes and repo is not None:
            _attach_theme_context(candidate, repo, timestamp=timestamp)
        saved = db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_hydration_cleanup",
                    from_state=previous_state,
                    to_state=CandidateState.WAIT_DATA,
                    source=CandidateSourceType.THEME_BOARD,
                    reason=DEFAULT_REASON,
                    created_at=timestamp,
                    payload={
                        "reason": DEFAULT_REASON,
                        "enrich_themes": bool(enrich_themes),
                        "theme_ids": list(candidate.theme_ids or []),
                    },
                )
            ],
        )
        cleaned.append(_candidate_sample(saved))
    return {
        "trade_date": trade_date,
        "mode": "APPLY",
        "eligible_state": CandidateState.HYDRATING.value,
        "target_state": CandidateState.WAIT_DATA.value,
        "applied_count": len(cleaned),
        "enrich_themes": bool(enrich_themes),
        "state_counts": _state_counts(db, trade_date),
        "sample": cleaned[: max(0, sample_limit)],
    }


def main() -> int:
    args = parse_args()
    trade_date = args.trade_date or datetime.now().date().isoformat()
    db = TradingDatabase(str(Path(args.db).expanduser()))
    if args.apply:
        report = apply_cleanup(
            db,
            trade_date=trade_date,
            enrich_themes=args.enrich_themes,
            sample_limit=args.sample_limit,
        )
    else:
        report = analyze_cleanup(
            db,
            trade_date=trade_date,
            enrich_themes=args.enrich_themes,
            sample_limit=args.sample_limit,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _attach_theme_context(candidate: Candidate, repo: ThemeEngineRepository, *, timestamp: str) -> bool:
    memberships = repo.get_themes_by_stock(candidate.code, active=True)
    if not memberships:
        return False
    membership = memberships[0]
    theme_id = str(getattr(membership, "theme_id", "") or "")
    if not theme_id:
        return False
    theme_name = _theme_display_name(repo, theme_id)
    changed = False
    if theme_id not in candidate.theme_ids:
        candidate.theme_ids.append(theme_id)
        changed = True
    stock_name = str(getattr(membership, "stock_name", "") or "")
    if stock_name and not candidate.name:
        candidate.name = stock_name
        changed = True
    metadata = dict(candidate.metadata or {})
    ingestion = dict(metadata.get("candidate_ingestion") or {})
    if not ingestion.get("primary_theme_id"):
        ingestion["primary_theme_id"] = theme_id
        changed = True
    if not ingestion.get("theme_name"):
        ingestion["theme_name"] = theme_name
        changed = True
    ingestion["theme_context_attached_at"] = timestamp
    metadata["candidate_ingestion"] = ingestion
    metadata["primary_theme_id"] = str(metadata.get("primary_theme_id") or theme_id)
    metadata["theme_name"] = str(metadata.get("theme_name") or theme_name)
    metadata["best_theme_id"] = str(metadata.get("best_theme_id") or theme_id)
    metadata["theme_context_attached_at"] = timestamp
    candidate.metadata = metadata
    return changed


def _state_counts(db: TradingDatabase, trade_date: str) -> dict[str, int]:
    rows = db.conn.execute(
        """
        SELECT state, COUNT(*) AS count
        FROM candidates
        WHERE trade_date = ?
        GROUP BY state
        ORDER BY state
        """,
        (trade_date,),
    ).fetchall()
    return {str(row["state"] or ""): int(row["count"] or 0) for row in rows}


def _candidate_sample(candidate: Candidate) -> dict[str, Any]:
    metadata = dict(candidate.metadata or {})
    return {
        "id": candidate.id,
        "code": candidate.code,
        "name": candidate.name,
        "state": candidate.state.value if hasattr(candidate.state, "value") else str(candidate.state or ""),
        "theme_ids": list(candidate.theme_ids or []),
        "primary_theme_id": str(metadata.get("primary_theme_id") or dict(metadata.get("candidate_ingestion") or {}).get("primary_theme_id") or ""),
        "reason_codes": list(metadata.get("reason_codes") or []),
    }


def _theme_display_name(repo: ThemeEngineRepository, theme_id: str) -> str:
    theme = repo.get_canonical_theme(theme_id)
    if theme is None:
        return theme_id
    return str(getattr(theme, "display_name", "") or getattr(theme, "canonical_name", "") or theme_id)


def _dedupe(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
