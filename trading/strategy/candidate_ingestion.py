from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.broker.models import BrokerConditionEvent
from trading.strategy.candidates import normalize_code
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
)


class CandidateSourceEventType(str, Enum):
    CONDITION_SEARCH = "condition_search"
    OPENING_BURST = "opening_burst"
    MANUAL_WATCH = "manual_watch"
    THEME_BOARD = "theme_board"


SOURCE_PRIORITY = {
    CandidateSourceEventType.OPENING_BURST.value: 400,
    CandidateSourceEventType.CONDITION_SEARCH.value: 300,
    CandidateSourceEventType.MANUAL_WATCH.value: 200,
    CandidateSourceEventType.THEME_BOARD.value: 100,
}

SOURCE_TO_CANDIDATE_SOURCE = {
    CandidateSourceEventType.CONDITION_SEARCH.value: CandidateSourceType.CONDITION_SEARCH,
    CandidateSourceEventType.OPENING_BURST.value: CandidateSourceType.OPENING_BURST,
    CandidateSourceEventType.MANUAL_WATCH.value: CandidateSourceType.MANUAL_WATCH,
    CandidateSourceEventType.THEME_BOARD.value: CandidateSourceType.THEME_BOARD,
}


@dataclass(frozen=True)
class CandidateSourceEvent:
    trade_date: str
    code: str
    name: str = ""
    source_type: str = CandidateSourceEventType.CONDITION_SEARCH.value
    source_id: str = ""
    source_rank: int = 0
    source_score: float = 0.0
    theme_id: str = ""
    theme_name: str = ""
    stock_role: str = ""
    reason_codes: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    detected_at: str = ""

    def normalized(self, *, now: datetime | None = None) -> "CandidateSourceEvent":
        detected_at = self.detected_at or _format_time(now)
        return CandidateSourceEvent(
            trade_date=str(self.trade_date or _trade_date(detected_at)),
            code=normalize_code(self.code),
            name=str(self.name or ""),
            source_type=str(self.source_type or CandidateSourceEventType.CONDITION_SEARCH.value),
            source_id=str(self.source_id or ""),
            source_rank=max(0, _int(self.source_rank)),
            source_score=max(0.0, _float(self.source_score)),
            theme_id=str(self.theme_id or ""),
            theme_name=str(self.theme_name or ""),
            stock_role=str(self.stock_role or ""),
            reason_codes=_dedupe(self.reason_codes),
            raw_payload=_jsonable(self.raw_payload or {}),
            detected_at=detected_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self.normalized()))


@dataclass(frozen=True)
class CandidateIngestionResult:
    candidate: Candidate | None = None
    source_event: CandidateSourceEvent | None = None
    created: bool = False
    merged: bool = False
    removed: bool = False
    ignored: bool = False
    reason: str = ""


class CandidateIngestionService:
    def __init__(
        self,
        db: Any,
        *,
        clock=None,
        default_ttl_minutes: int = 30,
    ) -> None:
        self.db = db
        self.clock = clock or datetime.now
        self.default_ttl_minutes = max(1, int(default_ttl_minutes))

    def ingest(self, source_event: CandidateSourceEvent) -> CandidateIngestionResult:
        event = source_event.normalized(now=self.clock())
        if not event.code:
            self._save_source_event(event, candidate_id=None, status="IGNORED", reason="CODE_MISSING")
            return CandidateIngestionResult(source_event=event, ignored=True, reason="CODE_MISSING")
        if str(event.raw_payload.get("event_type") or "").lower() == "remove":
            return self.remove_source(event)

        existing = self.db.load_candidate(event.trade_date, event.code)
        created = existing is None
        candidate = self._new_candidate(event) if existing is None else existing
        previous_state = candidate.state
        if candidate.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
            candidate.state = CandidateState.DETECTED
            candidate.block_type = BlockType.NONE
            candidate.can_recover = False
            candidate.recheck_after_sec = 0
        if candidate.state is None:
            candidate.state = CandidateState.DETECTED
        self._merge_candidate(candidate, event)
        saved = self.db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_source_ingested" if not created else "candidate_detected",
                    from_state=None if created else previous_state,
                    to_state=candidate.state,
                    source=_candidate_source(event.source_type),
                    reason="candidate source event",
                    created_at=event.detected_at,
                    payload=event.to_dict(),
                )
            ],
        )
        self._save_source_event(event, candidate_id=saved.id, status="INGESTED", reason="")
        return CandidateIngestionResult(
            candidate=saved,
            source_event=event,
            created=created,
            merged=not created,
            reason="CREATED" if created else "MERGED",
        )

    def remove_source(self, source_event: CandidateSourceEvent) -> CandidateIngestionResult:
        event = source_event.normalized(now=self.clock())
        candidate = self.db.load_candidate(event.trade_date, event.code)
        if candidate is None:
            self._save_source_event(event, candidate_id=None, status="IGNORED", reason="CANDIDATE_NOT_FOUND")
            return CandidateIngestionResult(source_event=event, ignored=True, reason="CANDIDATE_NOT_FOUND")
        previous_state = candidate.state
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        source_map = dict(ingestion.get("source_map") or {})
        key = _source_key(event)
        if key in source_map:
            entry = dict(source_map[key])
            entry["active"] = False
            entry["removed_at"] = event.detected_at
            source_map[key] = entry
        ingestion["source_map"] = source_map
        active_sources = [entry for entry in source_map.values() if bool(dict(entry).get("active", True))]
        ingestion["active_source_types"] = _dedupe([dict(entry).get("source_type", "") for entry in active_sources])
        metadata["candidate_ingestion"] = ingestion
        metadata["source_removed_at"] = event.detected_at
        candidate.metadata = metadata
        if not active_sources and candidate.state in {
            CandidateState.DETECTED,
            CandidateState.HYDRATING,
            CandidateState.WAIT_DATA,
            CandidateState.WATCHING,
        }:
            candidate.state = CandidateState.REMOVED
        candidate.last_seen_at = event.detected_at
        saved = self.db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_source_removed",
                    from_state=previous_state,
                    to_state=candidate.state,
                    source=_candidate_source(event.source_type),
                    reason="candidate source remove",
                    created_at=event.detected_at,
                    payload=event.to_dict(),
                )
            ],
        )
        self._save_source_event(event, candidate_id=saved.id, status="REMOVED", reason="")
        return CandidateIngestionResult(candidate=saved, source_event=event, removed=True, reason="REMOVED")

    def handle_condition_event(self, condition_event: BrokerConditionEvent, *, trade_date: str | None = None) -> CandidateIngestionResult:
        source_event = source_event_from_condition_event(
            condition_event,
            trade_date=trade_date or self._trade_date(),
            now=self.clock(),
        )
        if condition_event.event_type == "remove":
            return self.remove_source(source_event)
        return self.ingest(source_event)

    def ingest_opening_burst_result(self, result: Any, *, trade_date: str | None = None) -> list[CandidateIngestionResult]:
        events = source_events_from_opening_burst_result(result, trade_date=trade_date or self._trade_date())
        return [self.ingest(event) for event in events]

    def _new_candidate(self, event: CandidateSourceEvent) -> Candidate:
        return Candidate(
            trade_date=event.trade_date,
            code=event.code,
            name=event.name,
            sources=[],
            state=CandidateState.DETECTED,
            detected_at=event.detected_at,
            last_seen_at=event.detected_at,
            expires_at=_format_time(self.clock() + timedelta(minutes=self.default_ttl_minutes)),
            metadata={},
        )

    def _merge_candidate(self, candidate: Candidate, event: CandidateSourceEvent) -> None:
        candidate.name = event.name or candidate.name
        source = _candidate_source(event.source_type)
        if source is not None and source not in candidate.sources:
            candidate.sources.append(source)
        if event.theme_id and event.theme_id not in candidate.theme_ids:
            candidate.theme_ids.append(event.theme_id)
        if event.source_type == CandidateSourceEventType.CONDITION_SEARCH.value and event.source_id:
            if event.source_id not in candidate.condition_names:
                candidate.condition_names.append(event.source_id)
        candidate.priority = max(int(candidate.priority or 0), SOURCE_PRIORITY.get(event.source_type, 0) + max(0, 1000 - event.source_rank))
        candidate.last_seen_at = event.detected_at
        if not candidate.detected_at:
            candidate.detected_at = event.detected_at
        if not candidate.expires_at:
            candidate.expires_at = _format_time(self.clock() + timedelta(minutes=self.default_ttl_minutes))

        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        source_map = dict(ingestion.get("source_map") or {})
        source_map[_source_key(event)] = _source_event_summary(event, active=True)
        source_entries = list(source_map.values())
        primary = _primary_source(source_entries)
        ingestion.update(
            {
                "source_map": source_map,
                "active_source_types": _dedupe([dict(entry).get("source_type", "") for entry in source_entries if dict(entry).get("active", True)]),
                "primary_source": str(primary.get("source_type") or ""),
                "primary_source_id": str(primary.get("source_id") or ""),
                "primary_theme_id": str(primary.get("theme_id") or ""),
                "theme_name": str(primary.get("theme_name") or ""),
                "stock_role": str(primary.get("stock_role") or ""),
                "score": float(primary.get("source_score") or 0.0),
                "reason_codes": _dedupe(
                    [
                        *list(metadata.get("reason_codes") or []),
                        *[reason for entry in source_entries for reason in list(dict(entry).get("reason_codes") or [])],
                    ]
                ),
                "last_source_event_at": event.detected_at,
            }
        )
        metadata["candidate_ingestion"] = ingestion
        metadata["primary_source"] = ingestion["primary_source"]
        metadata["primary_source_id"] = ingestion["primary_source_id"]
        metadata["primary_theme_id"] = ingestion["primary_theme_id"]
        metadata["theme_name"] = ingestion["theme_name"]
        metadata["stock_role"] = ingestion["stock_role"]
        metadata["source_score"] = ingestion["score"]
        metadata["reason_codes"] = ingestion["reason_codes"]
        metadata.setdefault("candidate_generation_seq", 1)
        metadata.setdefault("candidate_instance_id", f"{candidate.trade_date}:{candidate.code}:1")
        if event.theme_id:
            metadata["best_theme_id"] = event.theme_id
        candidate.metadata = metadata

    def _save_source_event(self, event: CandidateSourceEvent, *, candidate_id: int | None, status: str, reason: str) -> None:
        saver = getattr(self.db, "save_candidate_source_event", None)
        if not callable(saver):
            return
        saver(
            {
                **event.to_dict(),
                "candidate_id": candidate_id,
                "status": status,
                "reason": reason,
            }
        )

    def _trade_date(self) -> str:
        return self.clock().date().isoformat()


def source_event_from_condition_event(
    event: BrokerConditionEvent,
    *,
    trade_date: str,
    now: datetime | None = None,
) -> CandidateSourceEvent:
    purpose = str(getattr(event, "purpose", "") or "")
    level = _condition_level(event.condition_name, purpose)
    role = "LEADER" if level == "LEADER" else ""
    return CandidateSourceEvent(
        trade_date=trade_date,
        code=event.code,
        name="",
        source_type=CandidateSourceEventType.CONDITION_SEARCH.value,
        source_id=event.condition_name,
        source_rank=max(0, int(event.condition_index or 0)),
        source_score=_condition_score(level),
        stock_role=role,
        reason_codes=_dedupe([f"condition_{level.lower()}", "CONDITION_INCLUDE_BOOSTER_ONLY"]),
        raw_payload={
            "condition_name": event.condition_name,
            "condition_index": event.condition_index,
            "event_type": event.event_type,
            "strategy_profile": getattr(event, "strategy_profile", ""),
            "purpose": purpose,
            "source": getattr(event, "source", "condition"),
        },
        detected_at=str(event.timestamp or _format_time(now)),
    )


def source_events_from_opening_burst_result(result: Any, *, trade_date: str) -> list[CandidateSourceEvent]:
    selected = list(getattr(result, "selected", ()) or [])
    if not selected:
        return []
    theme_by_code = _opening_theme_by_code(result)
    events: list[CandidateSourceEvent] = []
    calculated_at = str(getattr(result, "calculated_at", "") or "")
    for stock in selected:
        code = normalize_code(str(getattr(stock, "stock_code", "") or ""))
        if not code:
            continue
        theme = theme_by_code.get(code, {})
        role = _enum_value(getattr(stock, "role", ""))
        reason_codes = _dedupe([*list(getattr(stock, "reason_codes", ()) or []), "OPENING_BURST_OBSERVE_ONLY"])
        events.append(
            CandidateSourceEvent(
                trade_date=trade_date,
                code=code,
                name=str(getattr(stock, "stock_name", "") or ""),
                source_type=CandidateSourceEventType.OPENING_BURST.value,
                source_id=f"opening_burst:{theme.get('theme_id') or ''}:{code}",
                source_rank=max(0, _int(getattr(stock, "rank_in_theme", 0)) or _int(getattr(stock, "seed_rank", 0))),
                source_score=max(0.0, _float(getattr(stock, "stock_burst_score", 0.0))),
                theme_id=str(theme.get("theme_id") or ""),
                theme_name=str(theme.get("theme_name") or ""),
                stock_role=role,
                reason_codes=reason_codes,
                raw_payload=_jsonable(stock),
                detected_at=calculated_at,
            )
        )
    return events


def build_candidate_ingestion_snapshot(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    trade_date = trade_date or datetime.now().date().isoformat()
    candidates = list(db.list_candidates(trade_date=trade_date) or [])
    state_counts = Counter(str(candidate.state.value) for candidate in candidates)
    source_counts = Counter()
    top_wait = Counter()
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        for source_type in list(ingestion.get("active_source_types") or []):
            source_counts[str(source_type)] += 1
        if candidate.state == CandidateState.WAIT_DATA:
            for reason in list(metadata.get("reason_codes") or ingestion.get("reason_codes") or ["WAIT_DATA"]):
                top_wait[str(reason)] += 1
    hydration_summary = {}
    loader = getattr(db, "candidate_hydration_summary", None)
    if callable(loader):
        hydration_summary = dict(loader(trade_date=trade_date) or {})
    return {
        "trade_date": trade_date,
        "detected_count": state_counts.get(CandidateState.DETECTED.value, 0),
        "hydrating_count": state_counts.get(CandidateState.HYDRATING.value, 0),
        "watching_count": state_counts.get(CandidateState.WATCHING.value, 0),
        "wait_data_count": state_counts.get(CandidateState.WAIT_DATA.value, 0),
        "source_counts": dict(source_counts),
        "hydration_pending_count": int(hydration_summary.get("pending_count") or 0),
        "hydration_error_count": int(hydration_summary.get("error_count") or 0),
        "top_wait_data_reasons": [{"reason": reason, "count": count} for reason, count in top_wait.most_common(10)],
    }


def _source_event_summary(event: CandidateSourceEvent, *, active: bool) -> dict[str, Any]:
    return {
        "source_type": event.source_type,
        "source_id": event.source_id,
        "source_rank": event.source_rank,
        "source_score": event.source_score,
        "theme_id": event.theme_id,
        "theme_name": event.theme_name,
        "stock_role": event.stock_role,
        "reason_codes": list(event.reason_codes),
        "detected_at": event.detected_at,
        "active": active,
    }


def _source_key(event: CandidateSourceEvent) -> str:
    source_id = event.source_id or event.source_type
    return f"{event.source_type}:{source_id}"


def _primary_source(source_entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    entries = [dict(entry) for entry in source_entries if dict(entry).get("active", True)]
    if not entries:
        return {}
    return sorted(
        entries,
        key=lambda item: (
            SOURCE_PRIORITY.get(str(item.get("source_type") or ""), 0),
            _float(item.get("source_score")),
            -_int(item.get("source_rank")),
        ),
        reverse=True,
    )[0]


def _candidate_source(source_type: str) -> CandidateSourceType | None:
    return SOURCE_TO_CANDIDATE_SOURCE.get(str(source_type or ""))


def _condition_level(condition_name: str, purpose: str) -> str:
    text = f"{condition_name} {purpose}".lower()
    if "leader" in text or "주도" in text:
        return "LEADER"
    if "strong" in text or "강세" in text or "_3" in text:
        return "STRONG"
    return "ALIVE"


def _condition_score(level: str) -> float:
    return {"LEADER": 80.0, "STRONG": 55.0, "ALIVE": 25.0}.get(str(level or ""), 0.0)


def _opening_theme_by_code(result: Any) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for rank in list(getattr(result, "ranked_themes", ()) or []):
        snapshot = getattr(rank, "snapshot", None)
        stocks = list(getattr(snapshot, "stocks", ()) or []) if snapshot is not None else []
        for stock in stocks:
            code = normalize_code(str(getattr(stock, "stock_code", "") or ""))
            if not code:
                continue
            mapping.setdefault(
                code,
                {
                    "theme_id": str(getattr(rank, "theme_id", "") or ""),
                    "theme_name": str(getattr(rank, "theme_name", "") or ""),
                },
            )
    return mapping


def _format_time(value: datetime | None = None) -> str:
    return (value or datetime.now()).replace(microsecond=0).isoformat()


def _trade_date(timestamp: str) -> str:
    text = str(timestamp or "")
    return text[:10] if len(text) >= 10 else datetime.now().date().isoformat()


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    if hasattr(value, "value"):
        return value.value
    return value


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CandidateIngestionResult",
    "CandidateIngestionService",
    "CandidateSourceEvent",
    "CandidateSourceEventType",
    "build_candidate_ingestion_snapshot",
    "source_event_from_condition_event",
    "source_events_from_opening_burst_result",
]
