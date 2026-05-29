from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Union

from kiwoom.client import ExecutionEvent, OrderResult
from trading.models import BuyLeg, LegStatus, WatchItem
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    FillPolicy,
    IndicatorSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    ExitDecision,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.conditions import ConditionProfile
from trading.strategy.themes import ThemeMapping


class TradingDatabase:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watch_items (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                budget INTEGER NOT NULL,
                stop_loss_price INTEGER NOT NULL,
                tick_threshold INTEGER NOT NULL,
                take_profit_rate REAL NOT NULL,
                take_profit_sell_percent REAL NOT NULL,
                auto_buy_enabled INTEGER NOT NULL,
                auto_sell_enabled INTEGER NOT NULL,
                take_profit_done INTEGER NOT NULL,
                current_price INTEGER NOT NULL,
                average_price REAL NOT NULL,
                holding_quantity INTEGER NOT NULL,
                legs_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ok INTEGER NOT NULL,
                result_code INTEGER NOT NULL,
                message TEXT NOT NULL,
                request_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                code TEXT NOT NULL,
                order_no TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL,
                filled_quantity INTEGER NOT NULL,
                remaining_quantity INTEGER NOT NULL,
                tag TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS condition_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_name TEXT NOT NULL UNIQUE,
                strategy_profile TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 0,
                purpose TEXT NOT NULL DEFAULT '',
                last_resolved_index INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS theme_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT '',
                theme_id TEXT NOT NULL,
                theme_name TEXT NOT NULL DEFAULT '',
                sub_theme TEXT NOT NULL DEFAULT '',
                strategy_profile TEXT,
                is_large_cap INTEGER NOT NULL DEFAULT 0,
                is_leader_candidate INTEGER NOT NULL DEFAULT 0,
                base_priority INTEGER NOT NULL DEFAULT 0,
                is_signal_stock INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                memo TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, theme_id)
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT '',
                strategy_profile TEXT,
                sources_json TEXT NOT NULL DEFAULT '[]',
                priority INTEGER NOT NULL DEFAULT 0,
                theme_ids_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL,
                detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL DEFAULT '',
                condition_names_json TEXT NOT NULL DEFAULT '[]',
                block_type TEXT NOT NULL DEFAULT 'none',
                recheck_after_sec INTEGER NOT NULL DEFAULT 0,
                can_recover INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(trade_date, code)
            );
            CREATE TABLE IF NOT EXISTS candidate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                source TEXT,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS indicator_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                price INTEGER NOT NULL DEFAULT 0,
                vwap REAL,
                ema20_5m REAL,
                base_line_120 REAL,
                envelope_mid REAL,
                day_high INTEGER NOT NULL DEFAULT 0,
                day_low INTEGER NOT NULL DEFAULT 0,
                day_mid REAL,
                prev_high INTEGER NOT NULL DEFAULT 0,
                prev_low INTEGER NOT NULL DEFAULT 0,
                pullback_pct REAL,
                volume_reaccel INTEGER NOT NULL DEFAULT 0,
                failed_low_break_rebound INTEGER NOT NULL DEFAULT 0,
                chase_risk INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS gate_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                gate_name TEXT NOT NULL,
                passed INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                grade TEXT NOT NULL DEFAULT '',
                block_type TEXT NOT NULL DEFAULT 'none',
                can_recover INTEGER NOT NULL DEFAULT 0,
                recheck_after_sec INTEGER NOT NULL DEFAULT 0,
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS entry_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                entry_type TEXT NOT NULL DEFAULT '',
                base_price_source TEXT NOT NULL DEFAULT '',
                limit_price INTEGER NOT NULL DEFAULT 0,
                tick_offset INTEGER NOT NULL DEFAULT 0,
                max_chase_pct REAL NOT NULL DEFAULT 0,
                split_plan_json TEXT NOT NULL DEFAULT '[]',
                order_timeout_sec INTEGER NOT NULL DEFAULT 0,
                cancel_condition_json TEXT NOT NULL DEFAULT '{}',
                retry_policy_json TEXT NOT NULL DEFAULT '{}',
                confirmation_signal_json TEXT NOT NULL DEFAULT '[]',
                fill_policy TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS virtual_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                entry_plan_id INTEGER,
                status TEXT NOT NULL,
                limit_price INTEGER NOT NULL DEFAULT 0,
                virtual_fill_price INTEGER NOT NULL DEFAULT 0,
                fill_policy TEXT NOT NULL DEFAULT 'normal',
                submitted_at TEXT NOT NULL DEFAULT '',
                filled_at TEXT NOT NULL DEFAULT '',
                cancelled_at TEXT NOT NULL DEFAULT '',
                unfilled_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS virtual_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                virtual_order_id INTEGER,
                entry_price INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL DEFAULT '',
                closed_at TEXT NOT NULL DEFAULT '',
                close_price INTEGER NOT NULL DEFAULT 0,
                close_reason TEXT NOT NULL DEFAULT '',
                max_return_pct REAL NOT NULL DEFAULT 0,
                max_drawdown_pct REAL NOT NULL DEFAULT 0,
                realized_return_pct REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS exit_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                virtual_position_id INTEGER,
                decision_type TEXT NOT NULL DEFAULT '',
                trigger_price INTEGER NOT NULL DEFAULT 0,
                filled INTEGER NOT NULL DEFAULT 0,
                fill_policy TEXT NOT NULL DEFAULT 'normal',
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trade_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                virtual_position_id INTEGER,
                final_status TEXT NOT NULL DEFAULT '',
                max_return_5m REAL,
                max_return_10m REAL,
                max_return_20m REAL,
                max_drawdown_20m REAL,
                missed_reason TEXT NOT NULL DEFAULT '',
                false_negative_flag INTEGER NOT NULL DEFAULT 0,
                false_positive_flag INTEGER NOT NULL DEFAULT 0,
                expired_but_later_rallied INTEGER NOT NULL DEFAULT 0,
                blocked_but_later_rallied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS strategy_runtime_settings (
                config_key TEXT PRIMARY KEY,
                config_version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_trade_date_state_code
                ON candidates(trade_date, state, code);
            CREATE INDEX IF NOT EXISTS idx_candidates_trade_date_code
                ON candidates(trade_date, code);
            CREATE INDEX IF NOT EXISTS idx_candidate_events_candidate_id_created_at
                ON candidate_events(candidate_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_theme_mappings_code_enabled
                ON theme_mappings(code, enabled);
            CREATE INDEX IF NOT EXISTS idx_theme_mappings_theme_enabled
                ON theme_mappings(theme_id, enabled);
            """
        )
        self._ensure_column("indicator_snapshots", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_trade_review_columns()
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_reviews_unique_key
                ON trade_reviews(trade_date, candidate_id, theme_id, review_key)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_reviews_created_status
                ON trade_reviews(created_at, final_status)
            """
        )
        self.conn.commit()

    def load_watch_items(self) -> list[WatchItem]:
        rows = self.conn.execute("SELECT * FROM watch_items ORDER BY code").fetchall()
        return [self._row_to_item(row) for row in rows]

    def save_watch_item(self, item: WatchItem) -> None:
        self.conn.execute(
            """
            INSERT INTO watch_items (
                code, name, budget, stop_loss_price, tick_threshold,
                take_profit_rate, take_profit_sell_percent, auto_buy_enabled,
                auto_sell_enabled, take_profit_done, current_price,
                average_price, holding_quantity, legs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                budget=excluded.budget,
                stop_loss_price=excluded.stop_loss_price,
                tick_threshold=excluded.tick_threshold,
                take_profit_rate=excluded.take_profit_rate,
                take_profit_sell_percent=excluded.take_profit_sell_percent,
                auto_buy_enabled=excluded.auto_buy_enabled,
                auto_sell_enabled=excluded.auto_sell_enabled,
                take_profit_done=excluded.take_profit_done,
                current_price=excluded.current_price,
                average_price=excluded.average_price,
                holding_quantity=excluded.holding_quantity,
                legs_json=excluded.legs_json
            """,
            (
                item.code,
                item.name,
                item.budget,
                item.stop_loss_price,
                item.tick_threshold,
                item.take_profit_rate,
                item.take_profit_sell_percent,
                int(item.auto_buy_enabled),
                int(item.auto_sell_enabled),
                int(item.take_profit_done),
                item.current_price,
                item.average_price,
                item.holding_quantity,
                json.dumps([self._leg_to_dict(leg) for leg in item.legs], ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def update_market_snapshot(self, item: WatchItem) -> None:
        self.save_watch_item(item)

    def delete_watch_item(self, code: str) -> None:
        self.conn.execute("DELETE FROM watch_items WHERE code = ?", (code,))
        self.conn.commit()

    def save_order_result(self, result: OrderResult) -> None:
        self.conn.execute(
            "INSERT INTO order_results(ok, result_code, message, request_json) VALUES (?, ?, ?, ?)",
            (
                int(result.ok),
                result.code,
                result.message,
                json.dumps(result.request.__dict__, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def save_execution(self, event: ExecutionEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO executions(
                code, order_no, side, quantity, price, filled_quantity, remaining_quantity, tag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.code,
                event.order_no,
                event.side,
                event.quantity,
                event.price,
                event.filled_quantity,
                event.remaining_quantity,
                event.tag,
            ),
        )
        self.conn.commit()

    def save_log(self, message: str) -> None:
        self.conn.execute("INSERT INTO logs(message) VALUES (?)", (message,))
        self.conn.commit()

    def recent_logs(self, limit: int = 200) -> list[str]:
        rows = self.conn.execute(
            "SELECT created_at, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [f"{row['created_at']} {row['message']}" for row in reversed(rows)]

    def save_candidate(self, candidate: Candidate) -> Candidate:
        with self.conn:
            return self._save_candidate_no_commit(candidate)

    def load_candidate(self, trade_date: str, code: str) -> Optional[Candidate]:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE trade_date = ? AND code = ?",
            (trade_date, code),
        ).fetchone()
        return self._row_to_candidate(row) if row else None

    def load_candidate_by_id(self, candidate_id: int) -> Optional[Candidate]:
        row = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        return self._row_to_candidate(row) if row else None

    def list_candidates(
        self,
        trade_date: Optional[str] = None,
        state: Optional[Union[CandidateState, str]] = None,
    ) -> list[Candidate]:
        query = "SELECT * FROM candidates"
        clauses = []
        params = []
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value if isinstance(state, CandidateState) else str(state))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY trade_date, code"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def save_candidate_event(self, event: CandidateEvent) -> CandidateEvent:
        with self.conn:
            return self._save_candidate_event_no_commit(event)

    def list_candidate_events(self, candidate_id: int) -> list[CandidateEvent]:
        rows = self.conn.execute(
            "SELECT * FROM candidate_events WHERE candidate_id = ? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [self._row_to_candidate_event(row) for row in rows]

    def save_candidate_with_events(self, candidate: Candidate, events: Iterable[CandidateEvent]) -> Candidate:
        with self.conn:
            saved = self._save_candidate_no_commit(candidate)
            for event in events:
                if event.candidate_id is None:
                    event.candidate_id = saved.id
                self._save_candidate_event_no_commit(event)
            return saved

    def transition_candidate_with_events(self, candidate: Candidate, events: Iterable[CandidateEvent]) -> Candidate:
        return self.save_candidate_with_events(candidate, events)

    def save_indicator_snapshot(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        with self.conn:
            return self._save_indicator_snapshot_no_commit(snapshot)

    def list_indicator_snapshots(self, candidate_id: int) -> list[IndicatorSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM indicator_snapshots WHERE candidate_id = ? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [self._row_to_indicator_snapshot(row) for row in rows]

    def upsert_condition_profile(self, profile: ConditionProfile) -> ConditionProfile:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO condition_profiles(
                    condition_name, strategy_profile, enabled, priority, purpose,
                    last_resolved_index, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(condition_name) DO UPDATE SET
                    strategy_profile=excluded.strategy_profile,
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    purpose=excluded.purpose,
                    last_resolved_index=excluded.last_resolved_index,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    profile.condition_name,
                    profile.strategy_profile.value,
                    int(profile.enabled),
                    int(profile.priority),
                    profile.purpose,
                    profile.last_resolved_index,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM condition_profiles WHERE condition_name = ?",
                (profile.condition_name,),
            ).fetchone()
            return self._row_to_condition_profile(row)

    def list_condition_profiles(self, enabled: Optional[bool] = None) -> list[ConditionProfile]:
        query = "SELECT * FROM condition_profiles"
        params: list[int] = []
        if enabled is not None:
            query += " WHERE enabled = ?"
            params.append(int(enabled))
        query += " ORDER BY priority DESC, condition_name"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_condition_profile(row) for row in rows]

    def update_condition_last_resolved_index(self, condition_name: str, condition_index: int) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE condition_profiles
                SET last_resolved_index = ?, updated_at = CURRENT_TIMESTAMP
                WHERE condition_name = ?
                """,
                (int(condition_index), condition_name),
            )

    def upsert_theme_mapping(self, mapping: ThemeMapping) -> ThemeMapping:
        from trading.strategy.candidates import normalize_code

        mapping.code = normalize_code(mapping.code)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_mappings(
                    code, name, market, theme_id, theme_name, sub_theme, strategy_profile,
                    is_large_cap, is_leader_candidate, base_priority, is_signal_stock,
                    enabled, memo, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(code, theme_id) DO UPDATE SET
                    name=excluded.name,
                    market=excluded.market,
                    theme_name=excluded.theme_name,
                    sub_theme=excluded.sub_theme,
                    strategy_profile=excluded.strategy_profile,
                    is_large_cap=excluded.is_large_cap,
                    is_leader_candidate=excluded.is_leader_candidate,
                    base_priority=excluded.base_priority,
                    is_signal_stock=excluded.is_signal_stock,
                    enabled=excluded.enabled,
                    memo=excluded.memo,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    mapping.code,
                    mapping.name,
                    mapping.market,
                    mapping.theme_id,
                    mapping.theme_name,
                    mapping.sub_theme,
                    mapping.strategy_profile.value if mapping.strategy_profile else None,
                    int(mapping.is_large_cap),
                    int(mapping.is_leader_candidate),
                    int(mapping.base_priority),
                    int(mapping.is_signal_stock),
                    int(mapping.enabled),
                    mapping.memo,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM theme_mappings WHERE code = ? AND theme_id = ?",
                (mapping.code, mapping.theme_id),
            ).fetchone()
            return self._row_to_theme_mapping(row)

    def list_theme_mappings(self, enabled: Optional[bool] = None) -> list[ThemeMapping]:
        query = "SELECT * FROM theme_mappings"
        params: list[int] = []
        if enabled is not None:
            query += " WHERE enabled = ?"
            params.append(int(enabled))
        query += " ORDER BY theme_id, code"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_theme_mapping(row) for row in rows]

    def theme_mappings_for_code(self, code: str, enabled: Optional[bool] = True) -> list[ThemeMapping]:
        from trading.strategy.candidates import normalize_code

        query = "SELECT * FROM theme_mappings WHERE code = ?"
        params: list[object] = [normalize_code(code)]
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(int(enabled))
        query += " ORDER BY base_priority DESC, theme_id"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_theme_mapping(row) for row in rows]

    def theme_members(self, theme_id: str, enabled: Optional[bool] = True) -> list[ThemeMapping]:
        query = "SELECT * FROM theme_mappings WHERE theme_id = ?"
        params: list[object] = [theme_id]
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(int(enabled))
        query += " ORDER BY base_priority DESC, code"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_theme_mapping(row) for row in rows]

    def save_entry_plan(self, plan: EntryPlan) -> EntryPlan:
        with self.conn:
            if plan.id is None:
                cursor = self.conn.execute(
                    """
                    INSERT INTO entry_plans(
                        candidate_id, entry_type, base_price_source, limit_price, tick_offset,
                        max_chase_pct, split_plan_json, order_timeout_sec, cancel_condition_json,
                        retry_policy_json, confirmation_signal_json, fill_policy, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._entry_plan_params(plan),
                )
                plan_id = cursor.lastrowid
            else:
                self.conn.execute(
                    """
                    UPDATE entry_plans SET
                        candidate_id = ?,
                        entry_type = ?,
                        base_price_source = ?,
                        limit_price = ?,
                        tick_offset = ?,
                        max_chase_pct = ?,
                        split_plan_json = ?,
                        order_timeout_sec = ?,
                        cancel_condition_json = ?,
                        retry_policy_json = ?,
                        confirmation_signal_json = ?,
                        fill_policy = ?,
                        created_at = ?
                    WHERE id = ?
                    """,
                    self._entry_plan_params(plan) + (plan.id,),
                )
                plan_id = plan.id
            row = self.conn.execute("SELECT * FROM entry_plans WHERE id = ?", (plan_id,)).fetchone()
            return self._row_to_entry_plan(row)

    def list_entry_plans(self, candidate_id: int) -> list[EntryPlan]:
        rows = self.conn.execute(
            "SELECT * FROM entry_plans WHERE candidate_id = ? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [self._row_to_entry_plan(row) for row in rows]

    def load_entry_plan(self, entry_plan_id: int) -> Optional[EntryPlan]:
        row = self.conn.execute("SELECT * FROM entry_plans WHERE id = ?", (entry_plan_id,)).fetchone()
        return self._row_to_entry_plan(row) if row else None

    def find_entry_plan(
        self,
        candidate_id: int,
        theme_id: str,
        gate_result_key: str,
        entry_type: str,
    ) -> Optional[EntryPlan]:
        rows = self.conn.execute(
            "SELECT * FROM entry_plans WHERE candidate_id = ? AND entry_type = ? ORDER BY id DESC",
            (candidate_id, entry_type),
        ).fetchall()
        for row in rows:
            plan = self._row_to_entry_plan(row)
            if (
                str(plan.cancel_condition.get("theme_id") or "") == str(theme_id or "")
                and str(plan.cancel_condition.get("gate_result_key") or "") == str(gate_result_key or "")
            ):
                return plan
        return None

    def save_virtual_order(self, order: VirtualOrder) -> VirtualOrder:
        with self.conn:
            if order.id is None:
                cursor = self.conn.execute(
                    """
                    INSERT INTO virtual_orders(
                        candidate_id, entry_plan_id, status, limit_price, virtual_fill_price,
                        fill_policy, submitted_at, filled_at, cancelled_at, unfilled_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._virtual_order_params(order),
                )
                order_id = cursor.lastrowid
            else:
                self.conn.execute(
                    """
                    UPDATE virtual_orders SET
                        candidate_id = ?,
                        entry_plan_id = ?,
                        status = ?,
                        limit_price = ?,
                        virtual_fill_price = ?,
                        fill_policy = ?,
                        submitted_at = ?,
                        filled_at = ?,
                        cancelled_at = ?,
                        unfilled_reason = ?
                    WHERE id = ?
                    """,
                    self._virtual_order_params(order) + (order.id,),
                )
                order_id = order.id
            row = self.conn.execute("SELECT * FROM virtual_orders WHERE id = ?", (order_id,)).fetchone()
            return self._row_to_virtual_order(row)

    def list_virtual_orders(self, candidate_id: int) -> list[VirtualOrder]:
        rows = self.conn.execute(
            "SELECT * FROM virtual_orders WHERE candidate_id = ? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        return [self._row_to_virtual_order(row) for row in rows]

    def list_virtual_orders_by_status(self, status: Union[VirtualOrderStatus, str]) -> list[VirtualOrder]:
        status_value = status.value if isinstance(status, VirtualOrderStatus) else str(status)
        rows = self.conn.execute(
            "SELECT * FROM virtual_orders WHERE status = ? ORDER BY id",
            (status_value,),
        ).fetchall()
        return [self._row_to_virtual_order(row) for row in rows]

    def find_active_virtual_order(self, candidate_id: int, theme_id: str, entry_type: str) -> Optional[VirtualOrder]:
        rows = self.conn.execute(
            """
            SELECT vo.*, ep.cancel_condition_json, ep.entry_type
            FROM virtual_orders vo
            JOIN entry_plans ep ON ep.id = vo.entry_plan_id
            WHERE vo.candidate_id = ? AND vo.status = ?
            ORDER BY vo.id DESC
            """,
            (candidate_id, VirtualOrderStatus.SUBMITTED.value),
        ).fetchall()
        for row in rows:
            cancel_condition = dict(json.loads(row["cancel_condition_json"] or "{}"))
            if cancel_condition.get("theme_id") == theme_id and row["entry_type"] == entry_type:
                return self._row_to_virtual_order(row)
        return None

    def save_virtual_position(self, position: VirtualPosition) -> VirtualPosition:
        with self.conn:
            saved = self._save_virtual_position_no_commit(position)
        return saved

    def load_open_virtual_position(self, candidate_id: int) -> Optional[VirtualPosition]:
        row = self.conn.execute(
            """
            SELECT * FROM virtual_positions
            WHERE candidate_id = ? AND closed_at = ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        return self._row_to_virtual_position(row) if row else None

    def load_virtual_position_by_order(self, virtual_order_id: int) -> Optional[VirtualPosition]:
        row = self.conn.execute(
            "SELECT * FROM virtual_positions WHERE virtual_order_id = ? ORDER BY id DESC LIMIT 1",
            (virtual_order_id,),
        ).fetchone()
        return self._row_to_virtual_position(row) if row else None

    def list_virtual_positions(self, candidate_id: Optional[int] = None) -> list[VirtualPosition]:
        if candidate_id is None:
            rows = self.conn.execute("SELECT * FROM virtual_positions ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM virtual_positions WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        return [self._row_to_virtual_position(row) for row in rows]

    def list_open_virtual_positions(self) -> list[VirtualPosition]:
        rows = self.conn.execute(
            "SELECT * FROM virtual_positions WHERE closed_at = '' ORDER BY id"
        ).fetchall()
        return [self._row_to_virtual_position(row) for row in rows]

    def save_exit_decision(self, decision: ExitDecision) -> ExitDecision:
        with self.conn:
            saved = self._save_exit_decision_no_commit(decision)
        return saved

    def list_exit_decisions(self, virtual_position_id: int) -> list[ExitDecision]:
        rows = self.conn.execute(
            "SELECT * FROM exit_decisions WHERE virtual_position_id = ? ORDER BY id",
            (virtual_position_id,),
        ).fetchall()
        return [self._row_to_exit_decision(row) for row in rows]

    def close_virtual_position_with_decision(
        self,
        position: VirtualPosition,
        decision: ExitDecision,
    ) -> tuple[VirtualPosition, ExitDecision]:
        with self.conn:
            saved_position = self._save_virtual_position_no_commit(position)
            decision.virtual_position_id = saved_position.id
            saved_decision = self._save_exit_decision_no_commit(decision)
        return saved_position, saved_decision

    def save_trade_review(self, review: TradeReview) -> TradeReview:
        if review.candidate_id is None:
            raise ValueError("candidate_id is required for TradeReview")
        with self.conn:
            saved = self._save_trade_review_no_commit(review)
        return saved

    def list_trade_reviews(self, candidate_id: Optional[int] = None) -> list[TradeReview]:
        if candidate_id is None:
            rows = self.conn.execute("SELECT * FROM trade_reviews ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trade_reviews WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        return [self._row_to_trade_review(row) for row in rows]

    def latest_trade_reviews(self, limit: int = 200) -> list[TradeReview]:
        rows = self.conn.execute(
            "SELECT * FROM trade_reviews ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_trade_review(row) for row in reversed(rows)]

    def load_strategy_runtime_setting(self, config_key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM strategy_runtime_settings WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        return dict(row) if row else None

    def save_strategy_runtime_setting(
        self,
        config_key: str,
        config_version: int,
        config_json: str,
    ) -> dict:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO strategy_runtime_settings(
                    config_key, config_version, config_json, updated_at
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    config_json=excluded.config_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (config_key, int(config_version), config_json),
            )
            row = self.conn.execute(
                "SELECT * FROM strategy_runtime_settings WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            return dict(row)

    def close(self) -> None:
        self.conn.close()

    def _save_candidate_no_commit(self, candidate: Candidate) -> Candidate:
        self.conn.execute(
            """
            INSERT INTO candidates (
                trade_date, code, name, market, strategy_profile, sources_json, priority,
                theme_ids_json, state, detected_at, last_seen_at, expires_at,
                condition_names_json, block_type, recheck_after_sec, can_recover, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, code) DO UPDATE SET
                name=excluded.name,
                market=excluded.market,
                strategy_profile=excluded.strategy_profile,
                sources_json=excluded.sources_json,
                priority=excluded.priority,
                theme_ids_json=excluded.theme_ids_json,
                state=excluded.state,
                detected_at=excluded.detected_at,
                last_seen_at=excluded.last_seen_at,
                expires_at=excluded.expires_at,
                condition_names_json=excluded.condition_names_json,
                block_type=excluded.block_type,
                recheck_after_sec=excluded.recheck_after_sec,
                can_recover=excluded.can_recover,
                metadata_json=excluded.metadata_json
            """,
            (
                candidate.trade_date,
                candidate.code,
                candidate.name,
                candidate.market,
                candidate.strategy_profile.value if candidate.strategy_profile else None,
                json.dumps([source.value for source in candidate.sources], ensure_ascii=False),
                candidate.priority,
                json.dumps(candidate.theme_ids, ensure_ascii=False),
                candidate.state.value,
                candidate.detected_at,
                candidate.last_seen_at,
                candidate.expires_at,
                json.dumps(candidate.condition_names, ensure_ascii=False),
                candidate.block_type.value,
                candidate.recheck_after_sec,
                int(candidate.can_recover),
                json.dumps(candidate.metadata, ensure_ascii=False),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE trade_date = ? AND code = ?",
            (candidate.trade_date, candidate.code),
        ).fetchone()
        return self._row_to_candidate(row)

    def _save_candidate_event_no_commit(self, event: CandidateEvent) -> CandidateEvent:
        cursor = self.conn.execute(
            """
            INSERT INTO candidate_events(
                candidate_id, event_type, from_state, to_state, source, reason, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.candidate_id,
                event.event_type,
                event.from_state.value if event.from_state else None,
                event.to_state.value if event.to_state else None,
                event.source.value if event.source else None,
                event.reason,
                event.created_at,
                json.dumps(event.payload, ensure_ascii=False),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM candidate_events WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return self._row_to_candidate_event(row)

    def _save_indicator_snapshot_no_commit(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        cursor = self.conn.execute(
            """
            INSERT INTO indicator_snapshots(
                candidate_id, code, created_at, price, vwap, ema20_5m, base_line_120,
                envelope_mid, day_high, day_low, day_mid, prev_high, prev_low,
                pullback_pct, volume_reaccel, failed_low_break_rebound, chase_risk,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.candidate_id,
                snapshot.code,
                snapshot.created_at,
                snapshot.price,
                snapshot.vwap,
                snapshot.ema20_5m,
                snapshot.base_line_120,
                snapshot.envelope_mid,
                snapshot.day_high,
                snapshot.day_low,
                snapshot.day_mid,
                snapshot.prev_high,
                snapshot.prev_low,
                snapshot.pullback_pct,
                int(snapshot.volume_reaccel),
                int(snapshot.failed_low_break_rebound),
                int(snapshot.chase_risk),
                json.dumps(snapshot.metadata, ensure_ascii=False),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM indicator_snapshots WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return self._row_to_indicator_snapshot(row)

    def _save_virtual_position_no_commit(self, position: VirtualPosition) -> VirtualPosition:
        if position.id is None:
            cursor = self.conn.execute(
                """
                INSERT INTO virtual_positions(
                    candidate_id, virtual_order_id, entry_price, quantity, opened_at,
                    closed_at, close_price, close_reason, max_return_pct,
                    max_drawdown_pct, realized_return_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._virtual_position_params(position),
            )
            position_id = cursor.lastrowid
        else:
            self.conn.execute(
                """
                UPDATE virtual_positions SET
                    candidate_id = ?,
                    virtual_order_id = ?,
                    entry_price = ?,
                    quantity = ?,
                    opened_at = ?,
                    closed_at = ?,
                    close_price = ?,
                    close_reason = ?,
                    max_return_pct = ?,
                    max_drawdown_pct = ?,
                    realized_return_pct = ?
                WHERE id = ?
                """,
                self._virtual_position_params(position) + (position.id,),
            )
            position_id = position.id
        row = self.conn.execute("SELECT * FROM virtual_positions WHERE id = ?", (position_id,)).fetchone()
        return self._row_to_virtual_position(row)

    def _save_exit_decision_no_commit(self, decision: ExitDecision) -> ExitDecision:
        cursor = self.conn.execute(
            """
            INSERT INTO exit_decisions(
                virtual_position_id, decision_type, trigger_price, filled,
                fill_policy, reason_codes_json, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.virtual_position_id,
                decision.decision_type,
                decision.trigger_price,
                int(decision.filled),
                decision.fill_policy.value,
                json.dumps(decision.reason_codes, ensure_ascii=False),
                json.dumps(decision.details, ensure_ascii=False),
                decision.created_at,
            ),
        )
        row = self.conn.execute("SELECT * FROM exit_decisions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_exit_decision(row)

    def _save_trade_review_no_commit(self, review: TradeReview) -> TradeReview:
        if not review.trade_date:
            review.trade_date = ""
        if not review.theme_id:
            review.theme_id = ""
        if not review.review_key:
            review.review_key = _default_review_key(review)
        self.conn.execute(
            """
            INSERT INTO trade_reviews(
                candidate_id, trade_date, code, name, market, theme_id, theme_name,
                strategy_profile, gate_result_key, review_key, entry_plan_id,
                virtual_order_id, virtual_position_id, final_grade, final_status,
                virtual_order_status, exit_reason, entry_price, exit_price,
                max_return_5m, max_return_10m, max_return_20m, max_drawdown_20m,
                missed_reason, false_negative_flag, false_positive_flag,
                expired_but_later_rallied, blocked_but_later_rallied,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, candidate_id, theme_id, review_key) DO UPDATE SET
                code=excluded.code,
                name=excluded.name,
                market=excluded.market,
                theme_name=excluded.theme_name,
                strategy_profile=excluded.strategy_profile,
                gate_result_key=excluded.gate_result_key,
                entry_plan_id=excluded.entry_plan_id,
                virtual_order_id=excluded.virtual_order_id,
                virtual_position_id=excluded.virtual_position_id,
                final_grade=excluded.final_grade,
                final_status=excluded.final_status,
                virtual_order_status=excluded.virtual_order_status,
                exit_reason=excluded.exit_reason,
                entry_price=excluded.entry_price,
                exit_price=excluded.exit_price,
                max_return_5m=excluded.max_return_5m,
                max_return_10m=excluded.max_return_10m,
                max_return_20m=excluded.max_return_20m,
                max_drawdown_20m=excluded.max_drawdown_20m,
                missed_reason=excluded.missed_reason,
                false_negative_flag=excluded.false_negative_flag,
                false_positive_flag=excluded.false_positive_flag,
                expired_but_later_rallied=excluded.expired_but_later_rallied,
                blocked_but_later_rallied=excluded.blocked_but_later_rallied,
                details_json=excluded.details_json,
                created_at=excluded.created_at
            """,
            self._trade_review_params(review),
        )
        row = self.conn.execute(
            """
            SELECT * FROM trade_reviews
            WHERE trade_date = ? AND candidate_id = ? AND theme_id = ? AND review_key = ?
            """,
            (review.trade_date, review.candidate_id, review.theme_id, review.review_key),
        ).fetchone()
        return self._row_to_trade_review(row)

    @staticmethod
    def _entry_plan_params(plan: EntryPlan) -> tuple:
        return (
            plan.candidate_id,
            plan.entry_type,
            plan.base_price_source,
            plan.limit_price,
            plan.tick_offset,
            plan.max_chase_pct,
            json.dumps(plan.split_plan, ensure_ascii=False),
            plan.order_timeout_sec,
            json.dumps(plan.cancel_condition, ensure_ascii=False),
            json.dumps(plan.retry_policy, ensure_ascii=False),
            json.dumps(plan.confirmation_signal, ensure_ascii=False),
            plan.fill_policy.value,
            plan.created_at,
        )

    @staticmethod
    def _virtual_order_params(order: VirtualOrder) -> tuple:
        return (
            order.candidate_id,
            order.entry_plan_id,
            order.status.value,
            order.limit_price,
            order.virtual_fill_price,
            order.fill_policy.value,
            order.submitted_at,
            order.filled_at,
            order.cancelled_at,
            order.unfilled_reason,
        )

    @staticmethod
    def _virtual_position_params(position: VirtualPosition) -> tuple:
        return (
            position.candidate_id,
            position.virtual_order_id,
            position.entry_price,
            position.quantity,
            position.opened_at,
            position.closed_at,
            position.close_price,
            position.close_reason,
            position.max_return_pct,
            position.max_drawdown_pct,
            position.realized_return_pct,
        )

    @staticmethod
    def _trade_review_params(review: TradeReview) -> tuple:
        return (
            review.candidate_id,
            review.trade_date,
            review.code,
            review.name,
            review.market,
            review.theme_id,
            review.theme_name,
            review.strategy_profile,
            review.gate_result_key,
            review.review_key,
            review.entry_plan_id,
            review.virtual_order_id,
            review.virtual_position_id,
            review.final_grade,
            review.final_status.value if isinstance(review.final_status, ReviewFinalStatus) else review.final_status,
            review.virtual_order_status,
            review.exit_reason,
            review.entry_price,
            review.exit_price,
            review.max_return_5m,
            review.max_return_10m,
            review.max_return_20m,
            review.max_drawdown_20m,
            review.missed_reason,
            int(review.false_negative_flag),
            int(review.false_positive_flag),
            int(review.expired_but_later_rallied),
            int(review.blocked_but_later_rallied),
            json.dumps(review.details, ensure_ascii=False),
            review.created_at,
        )

    @staticmethod
    def _leg_to_dict(leg: BuyLeg) -> dict:
        return {
            "index": leg.index,
            "target_price": leg.target_price,
            "weight_percent": leg.weight_percent,
            "status": leg.status.value,
            "order_no": leg.order_no,
            "ordered_quantity": leg.ordered_quantity,
            "filled_quantity": leg.filled_quantity,
        }

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> WatchItem:
        legs = []
        for raw_leg in json.loads(row["legs_json"]):
            legs.append(
                BuyLeg(
                    index=int(raw_leg["index"]),
                    target_price=int(raw_leg["target_price"]),
                    weight_percent=float(raw_leg["weight_percent"]),
                    status=LegStatus(raw_leg.get("status", LegStatus.WAITING.value)),
                    order_no=raw_leg.get("order_no", ""),
                    ordered_quantity=int(raw_leg.get("ordered_quantity", 0)),
                    filled_quantity=int(raw_leg.get("filled_quantity", 0)),
                )
            )
        return WatchItem(
            code=row["code"],
            name=row["name"],
            budget=int(row["budget"]),
            stop_loss_price=int(row["stop_loss_price"]),
            tick_threshold=int(row["tick_threshold"]),
            take_profit_rate=float(row["take_profit_rate"]),
            take_profit_sell_percent=float(row["take_profit_sell_percent"]),
            auto_buy_enabled=bool(row["auto_buy_enabled"]),
            auto_sell_enabled=bool(row["auto_sell_enabled"]),
            take_profit_done=bool(row["take_profit_done"]),
            current_price=int(row["current_price"]),
            average_price=float(row["average_price"]),
            holding_quantity=int(row["holding_quantity"]),
            legs=legs,
        )

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> Candidate:
        strategy_profile = row["strategy_profile"]
        return Candidate(
            id=int(row["id"]),
            trade_date=row["trade_date"],
            code=row["code"],
            name=row["name"],
            market=row["market"],
            strategy_profile=StrategyProfile(strategy_profile) if strategy_profile else None,
            sources=[CandidateSourceType(source) for source in json.loads(row["sources_json"])],
            priority=int(row["priority"]),
            theme_ids=list(json.loads(row["theme_ids_json"])),
            state=CandidateState(row["state"]),
            detected_at=row["detected_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            condition_names=list(json.loads(row["condition_names_json"])),
            block_type=BlockType(row["block_type"]),
            recheck_after_sec=int(row["recheck_after_sec"]),
            can_recover=bool(row["can_recover"]),
            metadata=dict(json.loads(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_candidate_event(row: sqlite3.Row) -> CandidateEvent:
        from_state = row["from_state"]
        to_state = row["to_state"]
        source = row["source"]
        return CandidateEvent(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            event_type=row["event_type"],
            from_state=CandidateState(from_state) if from_state else None,
            to_state=CandidateState(to_state) if to_state else None,
            source=CandidateSourceType(source) if source else None,
            reason=row["reason"],
            created_at=row["created_at"],
            payload=dict(json.loads(row["payload_json"])),
        )

    @staticmethod
    def _row_to_condition_profile(row: sqlite3.Row) -> ConditionProfile:
        return ConditionProfile(
            id=int(row["id"]),
            condition_name=row["condition_name"],
            strategy_profile=StrategyProfile(row["strategy_profile"]),
            enabled=bool(row["enabled"]),
            priority=int(row["priority"]),
            purpose=row["purpose"],
            last_resolved_index=int(row["last_resolved_index"]) if row["last_resolved_index"] is not None else None,
        )

    @staticmethod
    def _row_to_indicator_snapshot(row: sqlite3.Row) -> IndicatorSnapshot:
        keys = set(row.keys())
        metadata_json = row["metadata_json"] if "metadata_json" in keys else "{}"
        return IndicatorSnapshot(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            code=row["code"],
            created_at=row["created_at"],
            price=int(row["price"]),
            vwap=row["vwap"],
            ema20_5m=row["ema20_5m"],
            base_line_120=row["base_line_120"],
            envelope_mid=row["envelope_mid"],
            day_high=int(row["day_high"]),
            day_low=int(row["day_low"]),
            day_mid=row["day_mid"],
            prev_high=int(row["prev_high"]),
            prev_low=int(row["prev_low"]),
            pullback_pct=row["pullback_pct"],
            volume_reaccel=bool(row["volume_reaccel"]),
            failed_low_break_rebound=bool(row["failed_low_break_rebound"]),
            chase_risk=bool(row["chase_risk"]),
            metadata=dict(json.loads(metadata_json or "{}")),
        )

    @staticmethod
    def _row_to_theme_mapping(row: sqlite3.Row) -> ThemeMapping:
        strategy_profile = row["strategy_profile"]
        return ThemeMapping(
            id=int(row["id"]) if row["id"] is not None else None,
            code=row["code"],
            name=row["name"],
            market=row["market"],
            theme_id=row["theme_id"],
            theme_name=row["theme_name"],
            sub_theme=row["sub_theme"],
            strategy_profile=StrategyProfile(strategy_profile) if strategy_profile else None,
            is_large_cap=bool(row["is_large_cap"]),
            is_leader_candidate=bool(row["is_leader_candidate"]),
            base_priority=int(row["base_priority"]),
            is_signal_stock=bool(row["is_signal_stock"]),
            enabled=bool(row["enabled"]),
            memo=row["memo"],
        )

    @staticmethod
    def _row_to_entry_plan(row: sqlite3.Row) -> EntryPlan:
        return EntryPlan(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            entry_type=row["entry_type"],
            base_price_source=row["base_price_source"],
            limit_price=int(row["limit_price"]),
            tick_offset=int(row["tick_offset"]),
            max_chase_pct=float(row["max_chase_pct"]),
            split_plan=list(json.loads(row["split_plan_json"])),
            order_timeout_sec=int(row["order_timeout_sec"]),
            cancel_condition=dict(json.loads(row["cancel_condition_json"])),
            retry_policy=dict(json.loads(row["retry_policy_json"])),
            confirmation_signal=list(json.loads(row["confirmation_signal_json"])),
            fill_policy=FillPolicy(row["fill_policy"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_virtual_order(row: sqlite3.Row) -> VirtualOrder:
        return VirtualOrder(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            entry_plan_id=int(row["entry_plan_id"]) if row["entry_plan_id"] is not None else None,
            status=VirtualOrderStatus(row["status"]),
            limit_price=int(row["limit_price"]),
            virtual_fill_price=int(row["virtual_fill_price"]),
            fill_policy=FillPolicy(row["fill_policy"]),
            submitted_at=row["submitted_at"],
            filled_at=row["filled_at"],
            cancelled_at=row["cancelled_at"],
            unfilled_reason=row["unfilled_reason"],
        )

    @staticmethod
    def _row_to_virtual_position(row: sqlite3.Row) -> VirtualPosition:
        return VirtualPosition(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            virtual_order_id=int(row["virtual_order_id"]) if row["virtual_order_id"] is not None else None,
            entry_price=int(row["entry_price"]),
            quantity=int(row["quantity"]),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            close_price=int(row["close_price"]),
            close_reason=row["close_reason"],
            max_return_pct=float(row["max_return_pct"]),
            max_drawdown_pct=float(row["max_drawdown_pct"]),
            realized_return_pct=float(row["realized_return_pct"]),
        )

    @staticmethod
    def _row_to_exit_decision(row: sqlite3.Row) -> ExitDecision:
        return ExitDecision(
            id=int(row["id"]),
            virtual_position_id=int(row["virtual_position_id"]) if row["virtual_position_id"] is not None else None,
            decision_type=row["decision_type"],
            trigger_price=int(row["trigger_price"]),
            filled=bool(row["filled"]),
            fill_policy=FillPolicy(row["fill_policy"]),
            reason_codes=list(json.loads(row["reason_codes_json"])),
            details=dict(json.loads(row["details_json"])),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_trade_review(row: sqlite3.Row) -> TradeReview:
        keys = set(row.keys())
        return TradeReview(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            trade_date=row["trade_date"] if "trade_date" in keys else "",
            code=row["code"] if "code" in keys else "",
            name=row["name"] if "name" in keys else "",
            market=row["market"] if "market" in keys else "",
            theme_id=row["theme_id"] if "theme_id" in keys else "",
            theme_name=row["theme_name"] if "theme_name" in keys else "",
            strategy_profile=row["strategy_profile"] if "strategy_profile" in keys else "",
            gate_result_key=row["gate_result_key"] if "gate_result_key" in keys else "",
            review_key=row["review_key"] if "review_key" in keys else "",
            entry_plan_id=int(row["entry_plan_id"]) if "entry_plan_id" in keys and row["entry_plan_id"] is not None else None,
            virtual_order_id=int(row["virtual_order_id"]) if "virtual_order_id" in keys and row["virtual_order_id"] is not None else None,
            virtual_position_id=int(row["virtual_position_id"]) if row["virtual_position_id"] is not None else None,
            final_grade=row["final_grade"] if "final_grade" in keys else "",
            final_status=row["final_status"],
            virtual_order_status=row["virtual_order_status"] if "virtual_order_status" in keys else "",
            exit_reason=row["exit_reason"] if "exit_reason" in keys else "",
            entry_price=int(row["entry_price"]) if "entry_price" in keys else 0,
            exit_price=int(row["exit_price"]) if "exit_price" in keys else 0,
            max_return_5m=row["max_return_5m"],
            max_return_10m=row["max_return_10m"],
            max_return_20m=row["max_return_20m"],
            max_drawdown_20m=row["max_drawdown_20m"],
            missed_reason=row["missed_reason"],
            false_negative_flag=bool(row["false_negative_flag"]),
            false_positive_flag=bool(row["false_positive_flag"]),
            expired_but_later_rallied=bool(row["expired_but_later_rallied"]),
            blocked_but_later_rallied=bool(row["blocked_but_later_rallied"]),
            details=dict(json.loads(row["details_json"] if "details_json" in keys else "{}")),
            created_at=row["created_at"],
        )

    def _ensure_trade_review_columns(self) -> None:
        columns = {
            "trade_date": "TEXT NOT NULL DEFAULT ''",
            "code": "TEXT NOT NULL DEFAULT ''",
            "name": "TEXT NOT NULL DEFAULT ''",
            "market": "TEXT NOT NULL DEFAULT ''",
            "theme_id": "TEXT NOT NULL DEFAULT ''",
            "theme_name": "TEXT NOT NULL DEFAULT ''",
            "strategy_profile": "TEXT NOT NULL DEFAULT ''",
            "gate_result_key": "TEXT NOT NULL DEFAULT ''",
            "review_key": "TEXT NOT NULL DEFAULT ''",
            "entry_plan_id": "INTEGER",
            "virtual_order_id": "INTEGER",
            "final_grade": "TEXT NOT NULL DEFAULT ''",
            "virtual_order_status": "TEXT NOT NULL DEFAULT ''",
            "exit_reason": "TEXT NOT NULL DEFAULT ''",
            "entry_price": "INTEGER NOT NULL DEFAULT 0",
            "exit_price": "INTEGER NOT NULL DEFAULT 0",
            "details_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            self._ensure_column("trade_reviews", name, definition)

    def _ensure_column(self, table_name: str, column_name: str, column_definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name in existing:
            return
        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _default_review_key(review: TradeReview) -> str:
    status = review.final_status.value if isinstance(review.final_status, ReviewFinalStatus) else str(review.final_status)
    return f"{review.gate_result_key}:{status}:{review.virtual_order_id or ''}:{review.virtual_position_id or ''}"
