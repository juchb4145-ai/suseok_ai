from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.theme_engine.lab import ThemeLabConfig, ThemeLabFlowEngine, ThemeLabFlowResult
from trading.theme_engine.models import StockSnapshot, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository


class ThemeLabRuntimePipeline:
    def __init__(
        self,
        *,
        db,
        market_data: MarketDataStore,
        market_index_store: MarketIndexStore,
        interval_sec: int = 3,
        engine: Optional[ThemeLabFlowEngine] = None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.market_index_store = market_index_store
        self.interval_sec = max(1, int(interval_sec))
        self.engine = engine or ThemeLabFlowEngine(ThemeLabConfig())
        self.last_run_at: Optional[datetime] = None
        self.last_result: Optional[ThemeLabFlowResult] = None
        self._warnings: list[str] = []

    def run_if_due(self, now: datetime) -> Optional[ThemeLabFlowResult]:
        current = now.replace(microsecond=0)
        if self.last_run_at is not None and current < self.last_run_at + timedelta(seconds=self.interval_sec):
            return None
        self.last_run_at = current
        return self.run(current)

    def run(self, now: datetime) -> ThemeLabFlowResult:
        self._warnings = []
        repository = ThemeEngineRepository(self.db)
        theme_inputs = self._theme_inputs(repository)
        snapshots = self._snapshots(theme_inputs)
        if not theme_inputs:
            self._warnings.append("THEME_LAB_MAPPING_EMPTY")
        if not snapshots:
            self._warnings.append("THEME_LAB_QUOTES_EMPTY")
        missing_prev_close = [code for code, snapshot in snapshots.items() if not _prev_close(snapshot)]
        if missing_prev_close:
            self._warnings.append("THEME_LAB_PREV_CLOSE_MISSING")
        result = self.engine.run_pipeline(
            theme_inputs=theme_inputs,
            snapshots=snapshots,
            kospi_return_pct=self.market_index_store.state("KOSPI").change_rate,
            kosdaq_return_pct=self.market_index_store.state("KOSDAQ").change_rate,
            calculated_at=now.isoformat(),
        )
        self.last_result = result
        self._save_result(result, now)
        return result

    def watchset_codes(self) -> list[str]:
        if self.last_result is None:
            return []
        return [
            normalize_code(item.symbol)
            for item in self.last_result.watchset
            if normalize_code(item.symbol) and int(item.condition_level or 0) >= 2
        ]

    def drain_warnings(self) -> list[str]:
        warnings = list(self._warnings)
        self._warnings = []
        return warnings

    def _theme_inputs(self, repository: ThemeEngineRepository) -> list[tuple[str, str, list[ThemeMembership]]]:
        themes = [
            theme
            for theme in repository.list_canonical_themes()
            if _enum_value(theme.status) in {ThemeStatus.ACTIVE.value, ThemeStatus.WATCH.value, ThemeStatus.CANDIDATE.value}
        ]
        inputs: list[tuple[str, str, list[ThemeMembership]]] = []
        for theme in themes:
            members = repository.get_members_by_theme(theme.theme_id, active=True)
            if members:
                inputs.append((theme.theme_id, theme.display_name or theme.canonical_name, members))
        return inputs

    def _snapshots(self, theme_inputs: list[tuple[str, str, list[ThemeMembership]]]) -> dict[str, StockSnapshot]:
        codes = {
            normalize_code(member.stock_code)
            for _, _, members in theme_inputs
            for member in members
            if normalize_code(member.stock_code)
        }
        snapshots: dict[str, StockSnapshot] = {}
        for code in sorted(codes):
            tick = self.market_data.latest_tick(code)
            if tick is None:
                continue
            snapshots[code] = _stock_snapshot_from_tick(tick)
        return snapshots

    def _save_result(self, result: ThemeLabFlowResult, now: datetime) -> None:
        payload = _result_payload(result)
        save = getattr(self.db, "save_theme_lab_flow_result", None)
        if callable(save):
            save(now.isoformat(), payload)


def _stock_snapshot_from_tick(tick: StrategyTick) -> StockSnapshot:
    metadata = dict(tick.metadata or {})
    return StockSnapshot(
        stock_code=tick.code,
        stock_name=str(metadata.get("name") or metadata.get("stock_name") or ""),
        current_price=float(tick.price or 0),
        change_rate=float(tick.change_rate or 0.0),
        volume=int(tick.cum_volume or 0),
        turnover=float(tick.trade_value or 0.0),
        execution_strength=float(tick.execution_strength or 0.0),
        best_bid=float(tick.best_bid or 0),
        best_ask=float(tick.best_ask or 0),
        session_high=float(metadata.get("session_high") or metadata.get("day_high") or 0),
        session_low=float(metadata.get("session_low") or metadata.get("day_low") or 0),
        ts=tick.timestamp.isoformat() if tick.timestamp else "",
        updated_at=tick.timestamp.isoformat() if tick.timestamp else "",
        metadata=metadata,
    )


def _prev_close(snapshot: StockSnapshot) -> float:
    for key in ("prev_close", "previous_close", "yesterday_close"):
        try:
            value = float((snapshot.metadata or {}).get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _result_payload(result: ThemeLabFlowResult) -> dict:
    return {
        "market_status": _asdict(result.market),
        "theme_rankings": [_asdict(item) for item in result.themes],
        "theme_condition_snapshots": [_asdict(item) for item in result.themes],
        "condition_hit_snapshots": [
            _asdict(hit)
            for theme in result.themes
            for hit in theme.member_hits
        ],
        "watchset_snapshots": [_asdict(item) for item in result.watchset],
        "gate_decisions": [_asdict(item) for item in result.gate_decisions],
        "data_quality": dict(result.data_quality),
    }


def _asdict(value) -> dict:
    def normalize(item):
        if hasattr(item, "value"):
            return item.value
        if isinstance(item, tuple):
            return [normalize(child) for child in item]
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {str(key): normalize(child) for key, child in item.items()}
        return item

    return normalize(asdict(value))


def dumps_result_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)
