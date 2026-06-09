from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Union
from uuid import uuid4

from trading.broker.models import BrokerExecutionEvent, BrokerOrderResult
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
    PositionContextSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    ExitDecision,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.conditions import ConditionProfile


class TradingDatabase:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._migrate()

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")

    def _migrate(self) -> None:
        self._archive_legacy_theme_mappings()
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
            CREATE TABLE IF NOT EXISTS canonical_themes (
                theme_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                theme_group TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'CANDIDATE',
                confidence REAL NOT NULL DEFAULT 0,
                trade_eligible INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS theme_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                normalized_alias TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(theme_id, normalized_alias, source)
            );
            CREATE TABLE IF NOT EXISTS source_theme_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_theme_id TEXT NOT NULL DEFAULT '',
                source_theme_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                matched_theme_id TEXT NOT NULL DEFAULT '',
                match_confidence REAL NOT NULL DEFAULT 0,
                raw_payload_hash TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_theme_id, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS theme_member_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'unknown',
                reason TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS theme_membership_current (
                theme_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL DEFAULT '',
                membership_score REAL NOT NULL DEFAULT 0,
                relation_type TEXT NOT NULL DEFAULT 'unknown',
                source_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                trade_eligible INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(theme_id, stock_code)
            );
            CREATE TABLE IF NOT EXISTS kiwoom_symbol_master (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL,
                market_code TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'kiwoom_code_list',
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS theme_activity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                theme_id TEXT NOT NULL,
                theme_name TEXT NOT NULL DEFAULT '',
                theme_score REAL NOT NULL DEFAULT 0,
                rank INTEGER NOT NULL DEFAULT 0,
                rank_delta_1m INTEGER NOT NULL DEFAULT 0,
                rank_delta_5m INTEGER NOT NULL DEFAULT 0,
                weighted_return_pct REAL NOT NULL DEFAULT 0,
                turnover REAL NOT NULL DEFAULT 0,
                turnover_strength REAL NOT NULL DEFAULT 0,
                breadth REAL NOT NULL DEFAULT 0,
                rising_count INTEGER NOT NULL DEFAULT 0,
                falling_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                leader_code TEXT NOT NULL DEFAULT '',
                leader_name TEXT NOT NULL DEFAULT '',
                leader_return_pct REAL NOT NULL DEFAULT 0,
                leader_turnover REAL NOT NULL DEFAULT 0,
                leader_gap REAL NOT NULL DEFAULT 0,
                top3_concentration REAL NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS dynamic_theme_clusters (
                cluster_id TEXT PRIMARY KEY,
                matched_theme_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'CANDIDATE',
                stock_codes_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                score REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS theme_source_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                theme_count INTEGER NOT NULL DEFAULT 0,
                member_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS hybrid_gate_validation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL DEFAULT '',
                candidate_source TEXT NOT NULL DEFAULT '',
                hybrid_status TEXT NOT NULL,
                hybrid_score REAL NOT NULL DEFAULT 0,
                hybrid_position_tier TEXT NOT NULL DEFAULT '',
                hybrid_primary_reason TEXT NOT NULL DEFAULT '',
                hybrid_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                theme_id TEXT NOT NULL DEFAULT '',
                theme_name TEXT NOT NULL DEFAULT '',
                theme_status TEXT NOT NULL DEFAULT '',
                theme_score REAL NOT NULL DEFAULT 0,
                theme_rank INTEGER NOT NULL DEFAULT 0,
                theme_rank_delta_1m INTEGER NOT NULL DEFAULT 0,
                theme_rank_delta_5m INTEGER NOT NULL DEFAULT 0,
                theme_breadth REAL NOT NULL DEFAULT 0,
                rising_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                leader_gap REAL NOT NULL DEFAULT 0,
                top3_concentration REAL NOT NULL DEFAULT 0,
                rank_in_theme INTEGER NOT NULL DEFAULT 0,
                leader_type TEXT NOT NULL DEFAULT '',
                membership_score REAL NOT NULL DEFAULT 0,
                relation_type TEXT NOT NULL DEFAULT '',
                source_count INTEGER NOT NULL DEFAULT 0,
                entry_timing_score REAL NOT NULL DEFAULT 0,
                chase_risk TEXT NOT NULL DEFAULT '',
                market_score REAL NOT NULL DEFAULT 0,
                risk_score REAL NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS theme_lab_flow_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                calculated_at TEXT NOT NULL,
                market_status_json TEXT NOT NULL DEFAULT '{}',
                theme_rankings_json TEXT NOT NULL DEFAULT '[]',
                theme_condition_snapshots_json TEXT NOT NULL DEFAULT '[]',
                condition_hit_snapshots_json TEXT NOT NULL DEFAULT '[]',
                watchset_snapshots_json TEXT NOT NULL DEFAULT '[]',
                gate_decisions_json TEXT NOT NULL DEFAULT '[]',
                data_quality_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS dashboard_operator_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                trade_date TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'themelab_dashboard',
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                symbol TEXT,
                stock_name TEXT,
                primary_theme TEXT,
                stock_role TEXT,
                candidate_instance_id TEXT,
                from_status TEXT,
                to_status TEXT,
                gate_status TEXT,
                display_status TEXT,
                message_ko TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                snoozed_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_events_trade_date_occurred
                ON dashboard_operator_events(trade_date, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_events_trade_date_severity
                ON dashboard_operator_events(trade_date, severity);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_events_trade_date_category
                ON dashboard_operator_events(trade_date, category);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_events_symbol_trade_date
                ON dashboard_operator_events(symbol, trade_date);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_events_candidate_instance
                ON dashboard_operator_events(candidate_instance_id);
            CREATE TABLE IF NOT EXISTS dashboard_operator_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL UNIQUE,
                trade_date TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'themelab_dashboard',
                requested_by TEXT,
                event_id TEXT,
                symbol TEXT,
                stock_name TEXT,
                candidate_instance_id TEXT,
                requires_token INTEGER NOT NULL DEFAULT 0,
                confirmation_required INTEGER NOT NULL DEFAULT 1,
                endpoint TEXT,
                request_payload_json TEXT NOT NULL DEFAULT '{}',
                response_payload_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_trade_date_requested
                ON dashboard_operator_actions(trade_date, requested_at);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_type_trade_date
                ON dashboard_operator_actions(action_type, trade_date);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_status_trade_date
                ON dashboard_operator_actions(status, trade_date);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_event
                ON dashboard_operator_actions(event_id);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_symbol_trade_date
                ON dashboard_operator_actions(symbol, trade_date);
            CREATE INDEX IF NOT EXISTS idx_dashboard_operator_actions_candidate_instance
                ON dashboard_operator_actions(candidate_instance_id);
            CREATE TABLE IF NOT EXISTS dashboard_postmarket_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT NOT NULL UNIQUE,
                trade_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                review_scope TEXT NOT NULL,
                symbol TEXT,
                stock_name TEXT,
                primary_theme TEXT,
                stock_role TEXT,
                candidate_instance_id TEXT,
                event_id TEXT,
                event_type TEXT,
                source_status TEXT,
                block_reason TEXT,
                block_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                base_time TEXT,
                base_price REAL,
                price_1m REAL,
                price_3m REAL,
                price_5m REAL,
                price_10m REAL,
                price_close_or_last REAL,
                return_1m_pct REAL,
                return_3m_pct REAL,
                return_5m_pct REAL,
                return_10m_pct REAL,
                return_close_or_last_pct REAL,
                outcome_label TEXT NOT NULL,
                confidence TEXT NOT NULL,
                confidence_reason TEXT,
                recommendation_ko TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_trade_date_generated
                ON dashboard_postmarket_reviews(trade_date, generated_at);
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_trade_date_outcome
                ON dashboard_postmarket_reviews(trade_date, outcome_label);
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_trade_date_event_type
                ON dashboard_postmarket_reviews(trade_date, event_type);
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_symbol_trade_date
                ON dashboard_postmarket_reviews(symbol, trade_date);
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_candidate_instance
                ON dashboard_postmarket_reviews(candidate_instance_id);
            CREATE INDEX IF NOT EXISTS idx_dashboard_postmarket_reviews_event
                ON dashboard_postmarket_reviews(event_id);
            CREATE TABLE IF NOT EXISTS market_side_confirmation_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                session_id TEXT NOT NULL,
                market_side TEXT NOT NULL,
                raw_status TEXT NOT NULL DEFAULT '',
                confirmed_status TEXT NOT NULL DEFAULT '',
                previous_confirmed_status TEXT NOT NULL DEFAULT '',
                confirmation_pending INTEGER NOT NULL DEFAULT 0,
                recovery_pending INTEGER NOT NULL DEFAULT 0,
                weak_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                risk_off_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                healthy_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                last_breadth_pct REAL,
                last_index_return_pct REAL,
                last_turnover_weighted_return_pct REAL,
                last_source TEXT NOT NULL DEFAULT '',
                last_trust_level TEXT NOT NULL DEFAULT '',
                last_data_quality_flags_json TEXT NOT NULL DEFAULT '[]',
                last_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                source_conflict INTEGER NOT NULL DEFAULT 0,
                source_conflict_count INTEGER NOT NULL DEFAULT 0,
                last_source_conflict_at TEXT NOT NULL DEFAULT '',
                last_status_changed_at TEXT NOT NULL DEFAULT '',
                last_confirmed_at TEXT NOT NULL DEFAULT '',
                last_recovered_at TEXT NOT NULL DEFAULT '',
                wait_started_at TEXT NOT NULL DEFAULT '',
                last_cycle_id TEXT NOT NULL DEFAULT '',
                last_evaluated_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                state_version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(trade_date, session_id, market_side, state_version)
            );
            CREATE TABLE IF NOT EXISTS market_side_confirmation_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                session_id TEXT NOT NULL,
                market_side TEXT NOT NULL,
                cycle_id TEXT NOT NULL DEFAULT '',
                previous_raw_status TEXT NOT NULL DEFAULT '',
                new_raw_status TEXT NOT NULL DEFAULT '',
                previous_confirmed_status TEXT NOT NULL DEFAULT '',
                new_confirmed_status TEXT NOT NULL DEFAULT '',
                previous_confirmation_pending INTEGER NOT NULL DEFAULT 0,
                new_confirmation_pending INTEGER NOT NULL DEFAULT 0,
                previous_recovery_pending INTEGER NOT NULL DEFAULT 0,
                new_recovery_pending INTEGER NOT NULL DEFAULT 0,
                weak_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                risk_off_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                healthy_consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                breadth_pct REAL,
                index_return_pct REAL,
                turnover_weighted_return_pct REAL,
                source TEXT NOT NULL DEFAULT '',
                trust_level TEXT NOT NULL DEFAULT '',
                source_conflict INTEGER NOT NULL DEFAULT 0,
                transition_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                transition_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(trade_date, session_id, market_side, cycle_id, transition_type)
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
                leg_index INTEGER NOT NULL DEFAULT 1,
                weight_pct REAL NOT NULL DEFAULT 100,
                status TEXT NOT NULL,
                limit_price INTEGER NOT NULL DEFAULT 0,
                virtual_fill_price INTEGER NOT NULL DEFAULT 0,
                fill_policy TEXT NOT NULL DEFAULT 'normal',
                submitted_at TEXT NOT NULL DEFAULT '',
                filled_at TEXT NOT NULL DEFAULT '',
                cancelled_at TEXT NOT NULL DEFAULT '',
                unfilled_reason TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
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
                realized_return_pct REAL NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
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
            CREATE TABLE IF NOT EXISTS position_context_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                candidate_id INTEGER,
                candidate_instance_id TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                trade_date TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                capture_reason TEXT NOT NULL DEFAULT '',
                theme_id TEXT NOT NULL DEFAULT '',
                theme_name TEXT NOT NULL DEFAULT '',
                theme_score REAL,
                theme_status TEXT NOT NULL DEFAULT '',
                leader_count INTEGER,
                strong_count INTEGER,
                breadth_status TEXT NOT NULL DEFAULT '',
                leader_code TEXT NOT NULL DEFAULT '',
                leader_return_pct REAL,
                leader_vwap_status TEXT NOT NULL DEFAULT '',
                leader_support_broken INTEGER NOT NULL DEFAULT 0,
                index_market TEXT NOT NULL DEFAULT '',
                index_status TEXT NOT NULL DEFAULT '',
                index_return_pct REAL,
                market_status TEXT NOT NULL DEFAULT '',
                market_risk_status TEXT NOT NULL DEFAULT '',
                risk_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS position_context_history_prune_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cutoff_at TEXT NOT NULL DEFAULT '',
                pruned_context_history_rows INTEGER NOT NULL DEFAULT 0,
                retained_context_history_rows INTEGER NOT NULL DEFAULT 0,
                oldest_retained_context_at TEXT NOT NULL DEFAULT '',
                prune_error_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
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
            CREATE TABLE IF NOT EXISTS gateway_commands (
                command_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL DEFAULT '',
                command_type TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                idempotency_key TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                command_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                result_payload_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                dispatched_at TEXT NOT NULL DEFAULT '',
                acked_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                trade_date TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS gateway_command_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                status_from TEXT NOT NULL DEFAULT '',
                status_to TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gateway_command_dedupe_keys (
                dedupe_key TEXT PRIMARY KEY,
                command_id TEXT NOT NULL,
                command_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS gateway_transport_latency_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id TEXT UNIQUE NOT NULL,
                trace_id TEXT NOT NULL DEFAULT '',
                trade_date TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT '',
                event_id TEXT NOT NULL DEFAULT '',
                command_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT NOT NULL DEFAULT '',
                transport_mode TEXT NOT NULL DEFAULT 'rest_long_poll',
                experiment_id TEXT NOT NULL DEFAULT '',
                scenario TEXT NOT NULL DEFAULT '',
                connection_id TEXT NOT NULL DEFAULT '',
                websocket_session_id TEXT NOT NULL DEFAULT '',
                ws_session_id TEXT NOT NULL DEFAULT '',
                ws_connection_id TEXT NOT NULL DEFAULT '',
                ws_connection_state TEXT NOT NULL DEFAULT '',
                ws_fallback_reason TEXT NOT NULL DEFAULT '',
                session_loss_count INTEGER NOT NULL DEFAULT 0,
                duplicate_ack_count INTEGER NOT NULL DEFAULT 0,
                unknown_ack_count INTEGER NOT NULL DEFAULT 0,
                payload_size_bytes INTEGER NOT NULL DEFAULT 0,
                total_wall_ms REAL,
                gateway_queue_wait_ms REAL,
                gateway_post_ms REAL,
                core_receive_ms REAL,
                core_persist_ms REAL,
                core_dispatch_wait_ms REAL,
                long_poll_wait_ms REAL,
                gateway_receive_wait_ms REAL,
                gateway_local_queue_wait_ms REAL,
                rate_limit_wait_ms REAL,
                gateway_execute_ms REAL,
                ack_round_trip_ms REAL,
                ws_send_ms REAL,
                ws_receive_ms REAL,
                ws_reconnect_count INTEGER NOT NULL DEFAULT 0,
                ws_message_sequence INTEGER,
                clock_skew_warning INTEGER NOT NULL DEFAULT 0,
                stage_ms_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gateway_transport_latency_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                transport_mode TEXT NOT NULL DEFAULT 'rest_long_poll',
                experiment_id TEXT NOT NULL DEFAULT '',
                scenario TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '{}',
                recommendation_json TEXT NOT NULL DEFAULT '{}',
                generated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS runtime_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                warning_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS runtime_order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT UNIQUE NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT '',
                dry_run INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                price INTEGER NOT NULL DEFAULT 0,
                order_amount INTEGER NOT NULL DEFAULT 0,
                order_type INTEGER NOT NULL DEFAULT 0,
                hoga TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL DEFAULT '',
                strategy_name TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER,
                entry_plan_id INTEGER,
                virtual_order_id INTEGER,
                virtual_position_id INTEGER,
                trade_review_id INTEGER,
                leg_index INTEGER,
                entry_type TEXT NOT NULL DEFAULT '',
                order_phase TEXT NOT NULL DEFAULT 'entry',
                exit_decision_id INTEGER,
                exit_decision_type TEXT NOT NULL DEFAULT '',
                exit_reason TEXT NOT NULL DEFAULT '',
                exit_percent REAL,
                exit_quantity INTEGER,
                remaining_quantity INTEGER,
                position_entry_price INTEGER,
                position_quantity INTEGER,
                position_opened_at TEXT NOT NULL DEFAULT '',
                position_closed_at TEXT NOT NULL DEFAULT '',
                position_max_return_pct REAL,
                position_max_drawdown_pct REAL,
                realized_return_pct REAL,
                virtual_exit_price INTEGER,
                gate_reason TEXT NOT NULL DEFAULT '',
                gate_status TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                duplicate_of TEXT NOT NULL DEFAULT '',
                safety_json TEXT NOT NULL DEFAULT '{}',
                live_safety_json TEXT NOT NULL DEFAULT '{}',
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_order_intent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status_from TEXT NOT NULL DEFAULT '',
                status_to TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_decision_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT UNIQUE NOT NULL,
                runtime_cycle_id TEXT NOT NULL DEFAULT '',
                trade_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decision_at TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER,
                candidate_instance_id TEXT NOT NULL DEFAULT '',
                candidate_generation_seq INTEGER NOT NULL DEFAULT 0,
                code TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                theme_name TEXT NOT NULL DEFAULT '',
                strategy_name TEXT NOT NULL DEFAULT '',
                strategy_version TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                gate_status TEXT NOT NULL DEFAULT '',
                gate_reason TEXT NOT NULL DEFAULT '',
                reason_status TEXT NOT NULL DEFAULT '',
                reason_family TEXT NOT NULL DEFAULT '',
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                block_type TEXT NOT NULL DEFAULT '',
                action_type TEXT NOT NULL DEFAULT '',
                action_result TEXT NOT NULL DEFAULT '',
                price REAL,
                change_rate REAL,
                trade_value REAL,
                execution_strength REAL,
                vwap REAL,
                momentum_1m REAL,
                momentum_3m REAL,
                momentum_5m REAL,
                gate_score REAL,
                hybrid_score REAL,
                theme_score REAL,
                data_status TEXT NOT NULL DEFAULT '',
                data_quality_issues_json TEXT NOT NULL DEFAULT '[]',
                order_intent_id TEXT NOT NULL DEFAULT '',
                entry_plan_id INTEGER,
                virtual_order_id INTEGER,
                virtual_position_id INTEGER,
                exit_decision_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS live_sim_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_intent_id TEXT UNIQUE NOT NULL,
                command_id TEXT NOT NULL DEFAULT '',
                entry_plan_id INTEGER,
                candidate_id INTEGER,
                virtual_order_id INTEGER,
                virtual_position_id INTEGER,
                exit_decision_id INTEGER,
                candidate_instance_id TEXT NOT NULL DEFAULT '',
                trade_date TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                account_id_masked TEXT NOT NULL DEFAULT '',
                order_mode TEXT NOT NULL DEFAULT 'LIVE_SIM',
                broker TEXT NOT NULL DEFAULT 'KIWOOM',
                broker_env TEXT NOT NULL DEFAULT 'SIMULATION',
                order_leg INTEGER NOT NULL DEFAULT 1,
                side TEXT NOT NULL DEFAULT '',
                order_type TEXT NOT NULL DEFAULT '',
                requested_qty INTEGER NOT NULL DEFAULT 0,
                requested_price INTEGER NOT NULL DEFAULT 0,
                submitted_qty INTEGER NOT NULL DEFAULT 0,
                submitted_price INTEGER NOT NULL DEFAULT 0,
                broker_order_id TEXT NOT NULL DEFAULT '',
                broker_original_order_id TEXT NOT NULL DEFAULT '',
                broker_response_code TEXT NOT NULL DEFAULT '',
                broker_response_message TEXT NOT NULL DEFAULT '',
                order_status TEXT NOT NULL DEFAULT 'CREATED',
                submitted_at TEXT NOT NULL DEFAULT '',
                accepted_at TEXT NOT NULL DEFAULT '',
                rejected_at TEXT NOT NULL DEFAULT '',
                first_fill_at TEXT NOT NULL DEFAULT '',
                last_fill_at TEXT NOT NULL DEFAULT '',
                cancelled_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS live_sim_order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_intent_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                status_from TEXT NOT NULL DEFAULT '',
                status_to TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS live_sim_cancel_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cancel_intent_id TEXT UNIQUE NOT NULL,
                original_order_id TEXT NOT NULL DEFAULT '',
                broker_order_id TEXT NOT NULL DEFAULT '',
                command_id TEXT NOT NULL DEFAULT '',
                trade_date TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                cancel_qty INTEGER NOT NULL DEFAULT 0,
                cancel_reason TEXT NOT NULL DEFAULT '',
                order_mode TEXT NOT NULL DEFAULT 'LIVE_SIM',
                account_id_masked TEXT NOT NULL DEFAULT '',
                candidate_instance_id TEXT NOT NULL DEFAULT '',
                entry_plan_id INTEGER,
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'CREATED',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                submitted_at TEXT NOT NULL DEFAULT '',
                accepted_at TEXT NOT NULL DEFAULT '',
                rejected_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS live_sim_fill_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_intent_id TEXT NOT NULL DEFAULT '',
                broker_order_id TEXT NOT NULL DEFAULT '',
                fill_id TEXT NOT NULL DEFAULT '',
                event_id TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                account_id_masked TEXT NOT NULL DEFAULT '',
                fill_qty INTEGER NOT NULL DEFAULT 0,
                fill_price INTEGER NOT NULL DEFAULT 0,
                cumulative_fill_qty INTEGER NOT NULL DEFAULT 0,
                remaining_qty INTEGER NOT NULL DEFAULT 0,
                fill_amount INTEGER NOT NULL DEFAULT 0,
                commission REAL NOT NULL DEFAULT 0,
                tax REAL NOT NULL DEFAULT 0,
                event_time TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL DEFAULT '',
                raw_event_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(broker_order_id, fill_id)
            );
            CREATE TABLE IF NOT EXISTS live_sim_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT UNIQUE NOT NULL,
                candidate_instance_id TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                account_id_masked TEXT NOT NULL DEFAULT '',
                order_mode TEXT NOT NULL DEFAULT 'LIVE_SIM',
                opened_at TEXT NOT NULL DEFAULT '',
                closed_at TEXT NOT NULL DEFAULT '',
                entry_qty INTEGER NOT NULL DEFAULT 0,
                entry_avg_price INTEGER NOT NULL DEFAULT 0,
                current_qty INTEGER NOT NULL DEFAULT 0,
                realized_qty INTEGER NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                realized_pnl_pct REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl_pct REAL NOT NULL DEFAULT 0,
                max_favorable_excursion_pct REAL NOT NULL DEFAULT 0,
                max_adverse_excursion_pct REAL NOT NULL DEFAULT 0,
                stop_loss_price INTEGER NOT NULL DEFAULT 0,
                take_profit_price INTEGER NOT NULL DEFAULT 0,
                max_hold_exit_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'OPEN',
                details_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS live_sim_runtime_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNKNOWN',
                reason TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS live_sim_reconcile_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                trigger TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                reason_codes_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS dry_run_performance_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                grouped_json TEXT NOT NULL DEFAULT '{}',
                false_signal_json TEXT NOT NULL DEFAULT '{}',
                recommendation_json TEXT NOT NULL DEFAULT '[]',
                filters_json TEXT NOT NULL DEFAULT '{}',
                generated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dry_run_performance_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER,
                virtual_order_id INTEGER,
                virtual_position_id INTEGER,
                trade_review_id INTEGER,
                entry_intent_id TEXT NOT NULL DEFAULT '',
                exit_intent_ids_json TEXT NOT NULL DEFAULT '[]',
                final_status TEXT NOT NULL DEFAULT '',
                realized_return_pct REAL,
                max_return_20m REAL,
                max_drawdown_20m REAL,
                dry_run_false_positive_type TEXT NOT NULL DEFAULT '',
                dry_run_false_negative_type TEXT NOT NULL DEFAULT '',
                quality_bucket TEXT NOT NULL DEFAULT '',
                item_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dry_run_threshold_ab_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE NOT NULL,
                trade_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '{}',
                candidates_json TEXT NOT NULL DEFAULT '[]',
                scenarios_json TEXT NOT NULL DEFAULT '[]',
                results_json TEXT NOT NULL DEFAULT '{}',
                recommendations_json TEXT NOT NULL DEFAULT '[]',
                filters_json TEXT NOT NULL DEFAULT '{}',
                generated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dry_run_threshold_ab_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                parameter_name TEXT NOT NULL DEFAULT '',
                label_ko TEXT NOT NULL DEFAULT '',
                baseline_value TEXT NOT NULL DEFAULT '',
                candidate_value TEXT NOT NULL DEFAULT '',
                recommendation_grade TEXT NOT NULL DEFAULT '',
                expected_net_benefit_score REAL,
                avoided_false_positive_count INTEGER DEFAULT 0,
                newly_created_false_negative_count INTEGER DEFAULT 0,
                opportunity_loss_delta INTEGER DEFAULT 0,
                sample_count INTEGER DEFAULT 0,
                confidence REAL,
                candidate_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS strategy_runtime_settings (
                config_key TEXT PRIMARY KEY,
                config_version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                strategy_name TEXT NOT NULL DEFAULT '',
                profile_name TEXT NOT NULL DEFAULT '',
                profile_version TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                effective_from TEXT NOT NULL DEFAULT '',
                effective_to TEXT NOT NULL DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{}',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_trade_date_state_code
                ON candidates(trade_date, state, code);
            CREATE INDEX IF NOT EXISTS idx_candidates_trade_date_code
                ON candidates(trade_date, code);
            CREATE INDEX IF NOT EXISTS idx_candidate_events_candidate_id_created_at
                ON candidate_events(candidate_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_canonical_themes_status
                ON canonical_themes(status);
            CREATE INDEX IF NOT EXISTS idx_canonical_themes_trade_eligible
                ON canonical_themes(trade_eligible);
            CREATE INDEX IF NOT EXISTS idx_theme_aliases_normalized
                ON theme_aliases(normalized_alias);
            CREATE INDEX IF NOT EXISTS idx_source_theme_catalog_matched
                ON source_theme_catalog(matched_theme_id);
            CREATE INDEX IF NOT EXISTS idx_theme_member_evidence_theme_stock
                ON theme_member_evidence(theme_id, stock_code);
            CREATE INDEX IF NOT EXISTS idx_theme_member_evidence_stock
                ON theme_member_evidence(stock_code);
            CREATE INDEX IF NOT EXISTS idx_theme_membership_current_stock
                ON theme_membership_current(stock_code);
            CREATE INDEX IF NOT EXISTS idx_theme_membership_current_theme
                ON theme_membership_current(theme_id);
            CREATE INDEX IF NOT EXISTS idx_theme_activity_snapshots_created_rank
                ON theme_activity_snapshots(created_at, rank);
            CREATE INDEX IF NOT EXISTS idx_dynamic_theme_clusters_status
                ON dynamic_theme_clusters(status);
            CREATE INDEX IF NOT EXISTS idx_theme_source_sync_runs_source_started
                ON theme_source_sync_runs(source, started_at);
            CREATE INDEX IF NOT EXISTS idx_market_side_confirmation_state_lookup
                ON market_side_confirmation_state(trade_date, session_id, market_side, state_version);
            CREATE INDEX IF NOT EXISTS idx_market_side_confirmation_state_expires
                ON market_side_confirmation_state(expires_at);
            CREATE INDEX IF NOT EXISTS idx_market_side_confirmation_transitions_lookup
                ON market_side_confirmation_transitions(trade_date, session_id, market_side, created_at);
            CREATE INDEX IF NOT EXISTS idx_market_side_confirmation_transitions_type
                ON market_side_confirmation_transitions(transition_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_trade_date
                ON hybrid_gate_validation_events(trade_date);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_stock_code
                ON hybrid_gate_validation_events(stock_code);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_status
                ON hybrid_gate_validation_events(hybrid_status);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_theme_id
                ON hybrid_gate_validation_events(theme_id);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_score
                ON hybrid_gate_validation_events(hybrid_score);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_membership
                ON hybrid_gate_validation_events(membership_score);
            CREATE INDEX IF NOT EXISTS idx_hybrid_validation_reason
                ON hybrid_gate_validation_events(hybrid_primary_reason);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_status_created_at
                ON gateway_commands(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_type_created_at
                ON gateway_commands(command_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_dedupe_key
                ON gateway_commands(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_idempotency_key
                ON gateway_commands(idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_trade_date
                ON gateway_commands(trade_date);
            CREATE INDEX IF NOT EXISTS idx_gateway_commands_updated_at
                ON gateway_commands(updated_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_events_command_id
                ON gateway_command_events(command_id, id);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_events_type_created_at
                ON gateway_command_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_dedupe_command_id
                ON gateway_command_dedupe_keys(command_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_dedupe_type_trade_date
                ON gateway_command_dedupe_keys(command_type, trade_date);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_dedupe_expires_at
                ON gateway_command_dedupe_keys(expires_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_trade_date_created_at
                ON gateway_transport_latency_samples(trade_date, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_direction_created_at
                ON gateway_transport_latency_samples(direction, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_message_type_created_at
                ON gateway_transport_latency_samples(message_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_command_id
                ON gateway_transport_latency_samples(command_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_event_id
                ON gateway_transport_latency_samples(event_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_transport_mode
                ON gateway_transport_latency_samples(transport_mode);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_reports_trade_date
                ON gateway_transport_latency_reports(trade_date, generated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_type_created_at
                ON runtime_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_cycles_started_at
                ON runtime_cycles(started_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_cycles_status
                ON runtime_cycles(status);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_trade_date_created_at
                ON runtime_order_intents(trade_date, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_code_created_at
                ON runtime_order_intents(code, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_candidate_id
                ON runtime_order_intents(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_virtual_order_id
                ON runtime_order_intents(virtual_order_id);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_status
                ON runtime_order_intents(status);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_dedupe_key
                ON runtime_order_intents(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_idempotency_key
                ON runtime_order_intents(idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intent_events_intent_id
                ON runtime_order_intent_events(intent_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intent_events_type_created_at
                ON runtime_order_intent_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_trade_date_at
                ON strategy_decision_events(trade_date, decision_at, id);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_code_at
                ON strategy_decision_events(code, decision_at);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_candidate
                ON strategy_decision_events(candidate_id, decision_at);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_candidate_instance
                ON strategy_decision_events(candidate_instance_id, decision_at);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_gate_status
                ON strategy_decision_events(gate_status, trade_date);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_action
                ON strategy_decision_events(action_type, action_result, trade_date);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_reason
                ON strategy_decision_events(reason_status, reason_family, trade_date);
            CREATE INDEX IF NOT EXISTS idx_strategy_decision_events_order_intent
                ON strategy_decision_events(order_intent_id);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_trade_date
                ON live_sim_orders(trade_date, created_at);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_code_status
                ON live_sim_orders(code, order_status);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_idempotency_key
                ON live_sim_orders(idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_dedupe_key
                ON live_sim_orders(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_command_id
                ON live_sim_orders(command_id);
            CREATE INDEX IF NOT EXISTS idx_live_sim_orders_broker_order_id
                ON live_sim_orders(broker_order_id);
            CREATE INDEX IF NOT EXISTS idx_live_sim_order_events_order
                ON live_sim_order_events(order_intent_id, id);
            CREATE INDEX IF NOT EXISTS idx_live_sim_cancel_orders_original
                ON live_sim_cancel_orders(original_order_id, status);
            CREATE INDEX IF NOT EXISTS idx_live_sim_cancel_orders_broker
                ON live_sim_cancel_orders(broker_order_id, status);
            CREATE INDEX IF NOT EXISTS idx_live_sim_cancel_orders_idempotency
                ON live_sim_cancel_orders(idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_live_sim_fill_events_order
                ON live_sim_fill_events(order_intent_id, id);
            CREATE INDEX IF NOT EXISTS idx_live_sim_positions_code_status
                ON live_sim_positions(code, status);
            CREATE INDEX IF NOT EXISTS idx_live_sim_reconcile_events_status
                ON live_sim_reconcile_events(status, started_at);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_reports_trade_date
                ON dry_run_performance_reports(trade_date, generated_at);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_items_report_id
                ON dry_run_performance_items(report_id);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_items_code
                ON dry_run_performance_items(code);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_items_candidate_id
                ON dry_run_performance_items(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_items_false_positive_type
                ON dry_run_performance_items(dry_run_false_positive_type);
            CREATE INDEX IF NOT EXISTS idx_dry_run_performance_items_false_negative_type
                ON dry_run_performance_items(dry_run_false_negative_type);
            CREATE INDEX IF NOT EXISTS idx_dry_run_threshold_ab_reports_trade_date
                ON dry_run_threshold_ab_reports(trade_date, generated_at);
            CREATE INDEX IF NOT EXISTS idx_dry_run_threshold_ab_candidates_report_id
                ON dry_run_threshold_ab_candidates(report_id);
            CREATE INDEX IF NOT EXISTS idx_dry_run_threshold_ab_candidates_category
                ON dry_run_threshold_ab_candidates(category);
            CREATE INDEX IF NOT EXISTS idx_dry_run_threshold_ab_candidates_grade
                ON dry_run_threshold_ab_candidates(recommendation_grade);
            """
        )
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_position_context_history_position
                ON position_context_history(position_id, captured_at, id);
            CREATE INDEX IF NOT EXISTS idx_position_context_history_trade_date
                ON position_context_history(trade_date, captured_at);
            CREATE INDEX IF NOT EXISTS idx_position_context_history_captured_at
                ON position_context_history(captured_at);
            """
        )
        self._ensure_column("indicator_snapshots", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("virtual_orders", "leg_index", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("virtual_orders", "weight_pct", "REAL NOT NULL DEFAULT 100")
        self._ensure_column("virtual_orders", "details_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("virtual_positions", "details_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_strategy_runtime_settings_columns()
        self._ensure_runtime_order_intent_columns()
        self._ensure_runtime_order_intent_indexes()
        self._ensure_gateway_transport_latency_columns()
        self._ensure_gateway_transport_latency_indexes()
        self._seed_legacy_strategy_runtime_settings()
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

    def save_order_result(self, result: BrokerOrderResult) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO order_results(ok, result_code, message, request_json) VALUES (?, ?, ?, ?)",
                (
                    int(result.ok),
                    result.code,
                    result.message,
                    json.dumps(result.request.to_dict(), ensure_ascii=False),
                ),
            )
        self._sync_live_sim_order_result(result)

    def save_execution(self, event: BrokerExecutionEvent) -> None:
        with self.conn:
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
        self._sync_live_sim_execution(event)

    def _sync_live_sim_order_result(self, result: BrokerOrderResult) -> None:
        lookup = result.idempotency_key or result.request.idempotency_key
        order = self.find_live_sim_order_by_idempotency(lookup) if lookup else None
        if order is None and result.command_id:
            order = self.find_live_sim_order_by_command_id(result.command_id)
        if order is None:
            return
        now = str(result.raw.get("timestamp") or result.raw.get("received_at") or "")
        if not now:
            from trading.broker.models import utc_timestamp

            now = utc_timestamp()
        status_from = str(order.get("order_status") or "")
        if result.ok:
            status_to = "ACCEPTED"
            reason_codes = _merge_reason_codes(order, ["ORDER_RECONCILED_FROM_KIWOOM"])
            updates = {
                "command_id": result.command_id or order.get("command_id"),
                "broker_order_id": result.order_no or order.get("broker_order_id"),
                "broker_response_code": str(result.code),
                "broker_response_message": result.message,
                "order_status": status_to,
                "accepted_at": now,
                "updated_at": now,
                "reason_codes": reason_codes,
                "details": {
                    **dict(order.get("details") or {}),
                    "order_result": result.to_dict(),
                    "order_result_ok": True,
                },
            }
        else:
            status_to = "REJECTED"
            reason_codes = _merge_reason_codes(order, ["LIVE_SIM_ORDER_REJECTED"])
            updates = {
                "command_id": result.command_id or order.get("command_id"),
                "broker_response_code": str(result.code),
                "broker_response_message": result.message,
                "order_status": status_to,
                "rejected_at": now,
                "updated_at": now,
                "reason_codes": reason_codes,
                "details": {
                    **dict(order.get("details") or {}),
                    "order_result": result.to_dict(),
                    "order_result_ok": False,
                },
            }
        saved = self.update_live_sim_order(str(order.get("order_intent_id") or ""), updates)
        self.append_live_sim_order_event(
            str(order.get("order_intent_id") or ""),
            "order_result",
            status_from=status_from,
            status_to=str((saved or updates).get("order_status") or status_to),
            message=result.message or status_to,
            payload=result.to_dict(),
            created_at=now,
        )

    def _sync_live_sim_execution(self, event: BrokerExecutionEvent) -> None:
        fill_link_method = ""
        order = self.find_live_sim_order_by_broker_order_id(event.order_no)
        if order is not None:
            fill_link_method = "broker_order_id"
        if order is None and event.command_id:
            order = self.find_live_sim_order_by_command_id(event.command_id)
            if order is not None:
                fill_link_method = "command_id"
        if order is None and event.idempotency_key:
            order = self.find_live_sim_order_by_idempotency(event.idempotency_key)
            if order is not None:
                fill_link_method = "idempotency_key"
        if order is None and event.order_no:
            order = self.find_live_sim_order_by_execution_fingerprint(event)
            if order is not None:
                fill_link_method = "execution_fingerprint"
        if order is None:
            self._sync_manual_live_sim_execution(event)
            return
        now = str(event.timestamp or "")
        order_intent_id = str(order.get("order_intent_id") or "")
        broker_order_id = str(event.order_no or order.get("broker_order_id") or "")
        cumulative_fill_qty = _execution_cumulative_fill_qty(event)
        previous_cumulative_fill_qty = self._previous_live_sim_cumulative_fill_qty(
            order_intent_id=order_intent_id,
            broker_order_id=broker_order_id,
        )
        fill_qty = max(0, cumulative_fill_qty - previous_cumulative_fill_qty)
        remaining_qty = max(0, int(event.remaining_quantity or 0))
        fill_price = max(0, int(event.price or 0))
        raw_event = {
            **event.to_dict(),
            "reported_filled_quantity": int(event.filled_quantity or 0),
            "computed_fill_qty": fill_qty,
            "computed_cumulative_fill_qty": cumulative_fill_qty,
            "previous_cumulative_fill_qty": previous_cumulative_fill_qty,
            "fill_link_method": fill_link_method,
            "manual_intervention": False,
        }
        fill_id = event.execution_id or f"{event.order_no}:{event.filled_quantity}:{event.remaining_quantity}:{event.price}:{now}"
        fill_payload = {
            "order_intent_id": order_intent_id,
            "broker_order_id": broker_order_id,
            "fill_id": fill_id,
            "event_id": event.execution_id,
            "code": event.code or order.get("code"),
            "side": event.side or order.get("side"),
            "account_id_masked": order.get("account_id_masked"),
            "fill_qty": fill_qty,
            "fill_price": fill_price,
            "cumulative_fill_qty": cumulative_fill_qty,
            "remaining_qty": remaining_qty,
            "fill_amount": fill_qty * fill_price,
            "commission": event.raw.get("commission", 0),
            "tax": event.raw.get("tax", 0),
            "event_time": now,
            "received_at": now,
            "raw_event": raw_event,
        }
        inserted, fill = self.save_live_sim_fill_event(fill_payload)
        if not inserted:
            return
        status_from = str(order.get("order_status") or "")
        if fill_qty <= 0 and cumulative_fill_qty <= 0:
            status_to = status_from or "ACCEPTED"
            reason = "LIVE_SIM_EXECUTION_ACK_TRACKED"
        else:
            status_to = "PARTIAL_FILLED" if remaining_qty > 0 else "FILLED"
            if status_from == "FILLED" and status_to == "PARTIAL_FILLED":
                status_to = status_from
            if fill_qty <= 0:
                reason = "LIVE_SIM_EXECUTION_NO_DELTA_TRACKED"
            else:
                reason = "LIVE_SIM_PARTIAL_FILL_TRACKED" if status_to == "PARTIAL_FILLED" else "ORDER_RECONCILED_FROM_KIWOOM"
        details = dict(order.get("details") or {})
        position = dict(details.get("position") or {})
        if fill_qty > 0:
            position = self.upsert_live_sim_position_from_fill(order, fill, exit_guard=dict(details.get("exit_guard") or {}))
        updates = {
            "broker_order_id": broker_order_id,
            "order_status": status_to,
            "first_fill_at": order.get("first_fill_at") or (now if fill_qty > 0 else ""),
            "last_fill_at": now,
            "updated_at": now,
            "reason_codes": _merge_reason_codes(order, [reason]),
            "details": {
                **details,
                "fill_link_method": fill_link_method,
                "last_fill": fill,
                "position": position,
            },
        }
        saved = self.update_live_sim_order(str(order.get("order_intent_id") or ""), updates)
        self.append_live_sim_order_event(
            str(order.get("order_intent_id") or ""),
            "execution",
            status_from=status_from,
            status_to=str((saved or updates).get("order_status") or status_to),
            message=reason,
            payload={"execution": event.to_dict(), "fill": fill, "position": position},
            created_at=now,
        )

    def _sync_manual_live_sim_execution(self, event: BrokerExecutionEvent) -> None:
        now = str(event.timestamp or "")
        broker_order_id = str(event.order_no or "")
        cumulative_fill_qty = _execution_cumulative_fill_qty(event)
        previous_cumulative_fill_qty = self._previous_live_sim_cumulative_fill_qty(
            order_intent_id="",
            broker_order_id=broker_order_id,
        )
        fill_qty = max(0, cumulative_fill_qty - previous_cumulative_fill_qty)
        if fill_qty <= 0 and cumulative_fill_qty <= 0:
            return
        account_id_masked = _mask_account(
            str(
                event.account
                or event.raw.get("account_id_masked")
                or event.raw.get("account")
                or ""
            )
        )
        position = self._find_live_sim_position_for_manual_execution(event, account_id_masked=account_id_masked)
        if position and not account_id_masked:
            account_id_masked = str(position.get("account_id_masked") or "")
        raw_event = {
            **event.to_dict(),
            "reported_filled_quantity": int(event.filled_quantity or 0),
            "computed_fill_qty": fill_qty,
            "computed_cumulative_fill_qty": cumulative_fill_qty,
            "previous_cumulative_fill_qty": previous_cumulative_fill_qty,
            "fill_link_method": "manual_unmatched_execution",
            "manual_intervention": True,
        }
        fill_id = event.execution_id or f"{event.order_no}:{event.filled_quantity}:{event.remaining_quantity}:{event.price}:{now}"
        fill_payload = {
            "order_intent_id": "",
            "broker_order_id": broker_order_id,
            "fill_id": fill_id,
            "event_id": event.execution_id,
            "code": event.code,
            "side": event.side,
            "account_id_masked": account_id_masked,
            "fill_qty": fill_qty,
            "fill_price": max(0, int(event.price or 0)),
            "cumulative_fill_qty": cumulative_fill_qty,
            "remaining_qty": max(0, int(event.remaining_quantity or 0)),
            "fill_amount": fill_qty * max(0, int(event.price or 0)),
            "commission": event.raw.get("commission", 0),
            "tax": event.raw.get("tax", 0),
            "event_time": now,
            "received_at": now,
            "raw_event": raw_event,
        }
        inserted, fill = self.save_live_sim_fill_event(fill_payload)
        if not inserted:
            return
        updated_position: dict = {}
        if fill_qty > 0 and position is not None:
            order = {
                "code": position.get("code") or event.code,
                "name": position.get("name") or "",
                "side": event.side,
                "account_id_masked": account_id_masked,
                "candidate_instance_id": position.get("candidate_instance_id") or "",
            }
            updated_position = self.upsert_live_sim_position_from_fill(order, fill)
            updated_position = self.save_live_sim_position(
                {
                    **updated_position,
                    "details": {
                        **dict(updated_position.get("details") or {}),
                        "manual_intervention": True,
                        "last_manual_execution": fill,
                    },
                    "updated_at": now,
                }
            )
        elif fill_qty > 0 and str(event.side or "").lower() == "buy":
            order = {
                "code": event.code,
                "name": "",
                "side": event.side,
                "account_id_masked": account_id_masked,
                "candidate_instance_id": "MANUAL_INTERVENTION",
            }
            updated_position = self.upsert_live_sim_position_from_fill(order, fill)
            updated_position = self.save_live_sim_position(
                {
                    **updated_position,
                    "status": "RECONCILE_REQUIRED",
                    "details": {
                        **dict(updated_position.get("details") or {}),
                        "manual_intervention": True,
                        "external_position_detected": True,
                        "last_manual_execution": fill,
                    },
                    "updated_at": now,
                }
            )
        self.save_live_sim_runtime_health(
            "reconcile",
            status="RECONCILE_REQUIRED",
            reason="LIVE_SIM_MANUAL_EXECUTION_DETECTED",
            details={
                "code": event.code,
                "side": event.side,
                "broker_order_id": broker_order_id,
                "account_id_masked": account_id_masked,
                "position_id": updated_position.get("position_id") if updated_position else "",
            },
            updated_at=now,
        )

    def _find_live_sim_position_for_manual_execution(
        self,
        event: BrokerExecutionEvent,
        *,
        account_id_masked: str,
    ) -> Optional[dict]:
        code = str(event.code or "")
        if not code:
            return None
        statuses = {"OPEN", "PARTIAL", "EXIT_ORDERED", "RECONCILE_REQUIRED"}
        positions = [
            position
            for position in self.list_live_sim_positions(
                code=code,
                account_id_masked=account_id_masked or None,
                limit=50,
            )
            if str(position.get("status") or "") in statuses
            and int(position.get("current_qty") or 0) > 0
        ]
        if len(positions) == 1:
            return positions[0]
        return None

    def _previous_live_sim_cumulative_fill_qty(self, *, order_intent_id: str, broker_order_id: str) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if order_intent_id:
            clauses.append("order_intent_id = ?")
            params.append(order_intent_id)
        if broker_order_id:
            clauses.append("broker_order_id = ?")
            params.append(broker_order_id)
        if not clauses:
            return 0
        row = self.conn.execute(
            f"""
            SELECT COALESCE(MAX(cumulative_fill_qty), 0) AS cumulative_fill_qty
            FROM live_sim_fill_events
            WHERE {' OR '.join(clauses)}
            """,
            tuple(params),
        ).fetchone()
        return max(0, int(row["cumulative_fill_qty"] or 0)) if row else 0

    def save_log(self, message: str) -> None:
        self.conn.execute("INSERT INTO logs(message) VALUES (?)", (message,))
        self.conn.commit()

    def recent_logs(self, limit: int = 200) -> list[str]:
        rows = self.conn.execute(
            "SELECT created_at, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [f"{row['created_at']} {row['message']}" for row in reversed(rows)]

    def save_runtime_event(self, event_type: str, status: str = "", message: str = "", payload: Optional[dict] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_events(event_type, status, message, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, status, message, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)),
        )
        self.conn.commit()

    def save_runtime_cycle(
        self,
        *,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        status: str,
        snapshot: Optional[dict] = None,
        warning_count: int = 0,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_cycles(
                started_at, finished_at, duration_ms, status, snapshot_json, warning_count, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                int(duration_ms or 0),
                status,
                json.dumps(snapshot or {}, ensure_ascii=False, sort_keys=True, default=str),
                int(warning_count or 0),
                error,
            ),
        )
        self.conn.commit()

    def latest_runtime_cycles(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, started_at, finished_at, duration_ms, status,
                   snapshot_json, warning_count, error
            FROM runtime_cycles
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "snapshot_json"},
                "snapshot": json.loads(row["snapshot_json"] or "{}"),
            }
            for row in rows
        ]

    def save_runtime_order_intent(self, record: dict) -> dict:
        payload = dict(record or {})
        now = payload.get("created_at") or payload.get("updated_at") or ""
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", payload["created_at"])
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_order_intents(
                    intent_id, trade_date, source, mode, dry_run, status, reason,
                    account, code, side, quantity, price, order_amount, order_type,
                    hoga, tag, strategy_name, candidate_id, entry_plan_id,
                    virtual_order_id, virtual_position_id, trade_review_id,
                    leg_index, entry_type, order_phase, exit_decision_id,
                    exit_decision_type, exit_reason, exit_percent, exit_quantity,
                    remaining_quantity, position_entry_price, position_quantity,
                    position_opened_at, position_closed_at, position_max_return_pct,
                    position_max_drawdown_pct, realized_return_pct, virtual_exit_price,
                    gate_reason, gate_status,
                    idempotency_key, dedupe_key, duplicate_of, safety_json,
                    live_safety_json, request_json, response_json, metadata_json,
                    created_at, updated_at
                ) VALUES (
                    :intent_id, :trade_date, :source, :mode, :dry_run, :status, :reason,
                    :account, :code, :side, :quantity, :price, :order_amount, :order_type,
                    :hoga, :tag, :strategy_name, :candidate_id, :entry_plan_id,
                    :virtual_order_id, :virtual_position_id, :trade_review_id,
                    :leg_index, :entry_type, :order_phase, :exit_decision_id,
                    :exit_decision_type, :exit_reason, :exit_percent, :exit_quantity,
                    :remaining_quantity, :position_entry_price, :position_quantity,
                    :position_opened_at, :position_closed_at, :position_max_return_pct,
                    :position_max_drawdown_pct, :realized_return_pct, :virtual_exit_price,
                    :gate_reason, :gate_status,
                    :idempotency_key, :dedupe_key, :duplicate_of, :safety_json,
                    :live_safety_json, :request_json, :response_json, :metadata_json,
                    :created_at, :updated_at
                )
                """,
                _runtime_order_intent_params(payload),
            )
        return self.get_runtime_order_intent(str(payload.get("intent_id") or "")) or payload

    def update_runtime_order_intent_response(self, intent_id: str, response: dict, *, status: str = "", reason: str = "") -> bool:
        updated_at = str(response.get("updated_at") or response.get("created_at") or "")
        if not updated_at:
            from trading.broker.models import utc_timestamp

            updated_at = utc_timestamp()
        fields = ["response_json = ?", "updated_at = ?"]
        params: list[object] = [json.dumps(response or {}, ensure_ascii=False, sort_keys=True, default=str), updated_at]
        if status:
            fields.append("status = ?")
            params.append(status)
        if reason:
            fields.append("reason = ?")
            params.append(reason)
        params.append(intent_id)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE runtime_order_intents SET {', '.join(fields)} WHERE intent_id = ?",
                tuple(params),
            )
        return cursor.rowcount > 0

    def link_runtime_order_intent_review(self, intent_id: str, trade_review_id: int) -> bool:
        from trading.broker.models import utc_timestamp

        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE runtime_order_intents
                SET trade_review_id = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (int(trade_review_id), utc_timestamp(), intent_id),
            )
        return cursor.rowcount > 0

    def get_runtime_order_intent(self, intent_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM runtime_order_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        return _row_to_runtime_order_intent(row) if row else None

    def find_runtime_order_intent_by_dedupe(self, dedupe_key: str) -> Optional[dict]:
        if not dedupe_key:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM runtime_order_intents
            WHERE dedupe_key = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (dedupe_key,),
        ).fetchone()
        return _row_to_runtime_order_intent(row) if row else None

    def find_runtime_order_intent_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        if not idempotency_key:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM runtime_order_intents
            WHERE idempotency_key = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        return _row_to_runtime_order_intent(row) if row else None

    def append_runtime_order_intent_event(
        self,
        intent_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: Optional[dict] = None,
        created_at: str = "",
    ) -> None:
        if not created_at:
            from trading.broker.models import utc_timestamp

            created_at = utc_timestamp()
        self.conn.execute(
            """
            INSERT INTO runtime_order_intent_events(
                intent_id, event_type, status_from, status_to, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                event_type,
                status_from,
                status_to,
                message,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                created_at,
            ),
        )
        self.conn.commit()

    def save_strategy_decision_events(self, events: Iterable[dict]) -> int:
        rows = [_strategy_decision_event_params(event) for event in events if isinstance(event, dict)]
        if not rows:
            return 0
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR IGNORE INTO strategy_decision_events(
                    decision_id, runtime_cycle_id, trade_date, created_at, decision_at,
                    candidate_id, candidate_instance_id, candidate_generation_seq,
                    code, name, theme_name, strategy_name, strategy_version, config_hash,
                    gate_status, gate_reason, reason_status, reason_family, reason_codes_json,
                    block_type, action_type, action_result,
                    price, change_rate, trade_value, execution_strength, vwap,
                    momentum_1m, momentum_3m, momentum_5m,
                    gate_score, hybrid_score, theme_score,
                    data_status, data_quality_issues_json,
                    order_intent_id, entry_plan_id, virtual_order_id, virtual_position_id,
                    exit_decision_id, details_json
                ) VALUES (
                    :decision_id, :runtime_cycle_id, :trade_date, :created_at, :decision_at,
                    :candidate_id, :candidate_instance_id, :candidate_generation_seq,
                    :code, :name, :theme_name, :strategy_name, :strategy_version, :config_hash,
                    :gate_status, :gate_reason, :reason_status, :reason_family, :reason_codes_json,
                    :block_type, :action_type, :action_result,
                    :price, :change_rate, :trade_value, :execution_strength, :vwap,
                    :momentum_1m, :momentum_3m, :momentum_5m,
                    :gate_score, :hybrid_score, :theme_score,
                    :data_status, :data_quality_issues_json,
                    :order_intent_id, :entry_plan_id, :virtual_order_id, :virtual_position_id,
                    :exit_decision_id, :details_json
                )
                """,
                rows,
            )
        return int(self.conn.total_changes - before)

    def list_strategy_decision_events(
        self,
        *,
        trade_date: Optional[str] = None,
        code: Optional[str] = None,
        theme_name: Optional[str] = None,
        gate_status: Optional[str] = None,
        action_type: Optional[str] = None,
        action_result: Optional[str] = None,
        reason_status: Optional[str] = None,
        reason_family: Optional[str] = None,
        window_sec: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = _strategy_decision_event_filters(
            trade_date=trade_date,
            code=code,
            theme_name=theme_name,
            gate_status=gate_status,
            action_type=action_type,
            action_result=action_result,
            reason_status=reason_status,
            reason_family=reason_family,
            window_sec=window_sec,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM strategy_decision_events
            {where}
            ORDER BY decision_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_strategy_decision_event(row) for row in rows]

    def strategy_decision_event_count(
        self,
        *,
        trade_date: Optional[str] = None,
        code: Optional[str] = None,
        theme_name: Optional[str] = None,
        gate_status: Optional[str] = None,
        action_type: Optional[str] = None,
        action_result: Optional[str] = None,
        reason_status: Optional[str] = None,
        reason_family: Optional[str] = None,
        window_sec: Optional[int] = None,
    ) -> int:
        clauses, params = _strategy_decision_event_filters(
            trade_date=trade_date,
            code=code,
            theme_name=theme_name,
            gate_status=gate_status,
            action_type=action_type,
            action_result=action_result,
            reason_status=reason_status,
            reason_family=reason_family,
            window_sec=window_sec,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM strategy_decision_events {where}",
            tuple(params),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def get_strategy_decision_event(self, decision_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM strategy_decision_events WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return _row_to_strategy_decision_event(row) if row else None

    def strategy_decision_summary(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
    ) -> dict:
        clauses, params = _strategy_decision_event_filters(trade_date=trade_date, window_sec=window_sec)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM strategy_decision_events
            {where}
            ORDER BY decision_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        events = [_row_to_strategy_decision_event(row) for row in rows]
        return _strategy_decision_summary(events, trade_date=trade_date or "", window_sec=window_sec)

    def save_live_sim_order(self, record: dict) -> dict:
        payload = _live_sim_order_params(record)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_orders(
                    order_intent_id, command_id, entry_plan_id, candidate_id,
                    virtual_order_id, virtual_position_id, exit_decision_id,
                    candidate_instance_id, trade_date, code, name, account_id_masked,
                    order_mode, broker, broker_env, order_leg, side, order_type,
                    requested_qty, requested_price, submitted_qty, submitted_price,
                    broker_order_id, broker_original_order_id, broker_response_code,
                    broker_response_message, order_status, submitted_at, accepted_at,
                    rejected_at, first_fill_at, last_fill_at, cancelled_at, updated_at,
                    idempotency_key, dedupe_key, reason_codes_json, details_json
                ) VALUES (
                    :order_intent_id, :command_id, :entry_plan_id, :candidate_id,
                    :virtual_order_id, :virtual_position_id, :exit_decision_id,
                    :candidate_instance_id, :trade_date, :code, :name, :account_id_masked,
                    :order_mode, :broker, :broker_env, :order_leg, :side, :order_type,
                    :requested_qty, :requested_price, :submitted_qty, :submitted_price,
                    :broker_order_id, :broker_original_order_id, :broker_response_code,
                    :broker_response_message, :order_status, :submitted_at, :accepted_at,
                    :rejected_at, :first_fill_at, :last_fill_at, :cancelled_at, :updated_at,
                    :idempotency_key, :dedupe_key, :reason_codes_json, :details_json
                )
                ON CONFLICT(order_intent_id) DO UPDATE SET
                    command_id=excluded.command_id,
                    broker_order_id=excluded.broker_order_id,
                    broker_response_code=excluded.broker_response_code,
                    broker_response_message=excluded.broker_response_message,
                    order_status=excluded.order_status,
                    submitted_at=excluded.submitted_at,
                    accepted_at=excluded.accepted_at,
                    rejected_at=excluded.rejected_at,
                    first_fill_at=excluded.first_fill_at,
                    last_fill_at=excluded.last_fill_at,
                    cancelled_at=excluded.cancelled_at,
                    updated_at=excluded.updated_at,
                    reason_codes_json=excluded.reason_codes_json,
                    details_json=excluded.details_json
                """,
                payload,
            )
        return self.get_live_sim_order(str(payload.get("order_intent_id") or "")) or dict(record or {})

    def update_live_sim_order(self, order_intent_id: str, updates: dict) -> Optional[dict]:
        current = self.get_live_sim_order(order_intent_id)
        if current is None:
            return None
        payload = {**current, **dict(updates or {})}
        return self.save_live_sim_order(payload)

    def get_live_sim_order(self, order_intent_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM live_sim_orders WHERE order_intent_id = ?",
            (order_intent_id,),
        ).fetchone()
        return _row_to_live_sim_order(row) if row else None

    def find_live_sim_order_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        if not idempotency_key:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM live_sim_orders
            WHERE idempotency_key = ?
              AND order_status IN ('CREATED', 'SUBMITTING', 'SUBMITTED', 'ACCEPTED', 'PARTIAL_FILLED', 'UNKNOWN_SUBMIT', 'CANCEL_REQUESTED')
            ORDER BY id ASC
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        return _row_to_live_sim_order(row) if row else None

    def find_live_sim_order_by_command_id(self, command_id: str) -> Optional[dict]:
        if not command_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM live_sim_orders WHERE command_id = ? ORDER BY id DESC LIMIT 1",
            (command_id,),
        ).fetchone()
        return _row_to_live_sim_order(row) if row else None

    def find_live_sim_order_by_broker_order_id(self, broker_order_id: str) -> Optional[dict]:
        if not broker_order_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM live_sim_orders WHERE broker_order_id = ? ORDER BY id DESC LIMIT 1",
            (broker_order_id,),
        ).fetchone()
        return _row_to_live_sim_order(row) if row else None

    def find_live_sim_order_by_execution_fingerprint(self, event: BrokerExecutionEvent) -> Optional[dict]:
        if not event.order_no:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM live_sim_orders
            WHERE broker_order_id = ''
              AND code = ?
              AND lower(side) = lower(?)
              AND submitted_qty = ?
              AND submitted_price = ?
              AND order_status IN ('SUBMITTED', 'ACCEPTED', 'PARTIAL_FILLED')
            ORDER BY
              CASE
                WHEN accepted_at != '' THEN accepted_at
                WHEN submitted_at != '' THEN submitted_at
                ELSE created_at
              END DESC,
              id DESC
            LIMIT 1
            """,
            (
                event.code,
                event.side,
                int(event.quantity or 0),
                int(event.price or 0),
            ),
        ).fetchone()
        return _row_to_live_sim_order(row) if row else None

    def list_live_sim_orders(
        self,
        *,
        trade_date: Optional[str] = None,
        status: Optional[str] = None,
        code: Optional[str] = None,
        side: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if status:
            clauses.append("order_status = ?")
            params.append(status)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if side:
            clauses.append("side = ?")
            params.append(side)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM live_sim_orders
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_live_sim_order(row) for row in rows]

    def append_live_sim_order_event(
        self,
        order_intent_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: Optional[dict] = None,
        created_at: str = "",
    ) -> None:
        if not created_at:
            from trading.broker.models import utc_timestamp

            created_at = utc_timestamp()
        self.conn.execute(
            """
            INSERT INTO live_sim_order_events(
                order_intent_id, event_type, status_from, status_to, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_intent_id,
                event_type,
                status_from,
                status_to,
                message,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                created_at,
            ),
        )
        self.conn.commit()

    def save_live_sim_cancel_order(self, record: dict) -> dict:
        payload = _live_sim_cancel_params(record)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_cancel_orders(
                    cancel_intent_id, original_order_id, broker_order_id, command_id,
                    trade_date, code, side, cancel_qty, cancel_reason, order_mode,
                    account_id_masked, candidate_instance_id, entry_plan_id,
                    idempotency_key, status, attempts, created_at, submitted_at,
                    accepted_at, rejected_at, updated_at, reason_codes_json,
                    details_json
                ) VALUES (
                    :cancel_intent_id, :original_order_id, :broker_order_id, :command_id,
                    :trade_date, :code, :side, :cancel_qty, :cancel_reason, :order_mode,
                    :account_id_masked, :candidate_instance_id, :entry_plan_id,
                    :idempotency_key, :status, :attempts, :created_at, :submitted_at,
                    :accepted_at, :rejected_at, :updated_at, :reason_codes_json,
                    :details_json
                )
                ON CONFLICT(cancel_intent_id) DO UPDATE SET
                    command_id=excluded.command_id,
                    status=excluded.status,
                    attempts=excluded.attempts,
                    submitted_at=excluded.submitted_at,
                    accepted_at=excluded.accepted_at,
                    rejected_at=excluded.rejected_at,
                    updated_at=excluded.updated_at,
                    reason_codes_json=excluded.reason_codes_json,
                    details_json=excluded.details_json
                """,
                payload,
            )
        return self.get_live_sim_cancel_order(str(payload.get("cancel_intent_id") or "")) or dict(record or {})

    def get_live_sim_cancel_order(self, cancel_intent_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM live_sim_cancel_orders WHERE cancel_intent_id = ?",
            (cancel_intent_id,),
        ).fetchone()
        return _row_to_live_sim_cancel(row) if row else None

    def find_live_sim_cancel_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        if not idempotency_key:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM live_sim_cancel_orders
            WHERE idempotency_key = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        return _row_to_live_sim_cancel(row) if row else None

    def find_pending_live_sim_cancel(
        self,
        *,
        broker_order_id: str = "",
        original_order_id: str = "",
        code: str = "",
        account_id_masked: str = "",
    ) -> Optional[dict]:
        clauses = ["status IN ('CREATED', 'QUEUED', 'SUBMITTED', 'CANCEL_REQUESTED', 'CANCEL_SUBMITTING', 'UNKNOWN_SUBMIT')"]
        params: list[object] = []
        if broker_order_id:
            clauses.append("broker_order_id = ?")
            params.append(broker_order_id)
        if original_order_id:
            clauses.append("original_order_id = ?")
            params.append(original_order_id)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if account_id_masked:
            clauses.append("account_id_masked = ?")
            params.append(account_id_masked)
        row = self.conn.execute(
            f"""
            SELECT * FROM live_sim_cancel_orders
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_to_live_sim_cancel(row) if row else None

    def list_live_sim_cancel_orders(
        self,
        *,
        status: Optional[str] = None,
        code: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if code:
            clauses.append("code = ?")
            params.append(code)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM live_sim_cancel_orders
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_live_sim_cancel(row) for row in rows]

    def save_live_sim_fill_event(self, payload: dict) -> tuple[bool, dict]:
        params = _live_sim_fill_params(payload)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO live_sim_fill_events(
                    order_intent_id, broker_order_id, fill_id, event_id, code, side,
                    account_id_masked, fill_qty, fill_price, cumulative_fill_qty,
                    remaining_qty, fill_amount, commission, tax, event_time,
                    received_at, raw_event_json
                ) VALUES (
                    :order_intent_id, :broker_order_id, :fill_id, :event_id, :code, :side,
                    :account_id_masked, :fill_qty, :fill_price, :cumulative_fill_qty,
                    :remaining_qty, :fill_amount, :commission, :tax, :event_time,
                    :received_at, :raw_event_json
                )
                """,
                params,
            )
        inserted = cursor.rowcount > 0
        row = self.conn.execute(
            "SELECT * FROM live_sim_fill_events WHERE broker_order_id = ? AND fill_id = ?",
            (params["broker_order_id"], params["fill_id"]),
        ).fetchone()
        return inserted, (_row_to_live_sim_fill(row) if row else dict(payload or {}))

    def upsert_live_sim_position_from_fill(self, order: dict, fill: dict, *, exit_guard: Optional[dict] = None) -> dict:
        side = str(fill.get("side") or order.get("side") or "").lower()
        code = str(order.get("code") or fill.get("code") or "")
        account_id_masked = str(order.get("account_id_masked") or fill.get("account_id_masked") or "")
        candidate_instance_id = str(order.get("candidate_instance_id") or "")
        position_id = f"LIVE_SIM:{account_id_masked}:{code}:{candidate_instance_id or 'no_ci'}"
        existing = self.conn.execute(
            "SELECT * FROM live_sim_positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        current = _row_to_live_sim_position(existing) if existing else None
        fill_qty = max(0, int(fill.get("fill_qty") or 0))
        fill_price = max(0, int(fill.get("fill_price") or 0))
        now = str(fill.get("event_time") or fill.get("received_at") or "")
        exit_cfg = dict(exit_guard or {})
        if current is None:
            current = {
                "position_id": position_id,
                "candidate_instance_id": candidate_instance_id,
                "code": code,
                "name": order.get("name", ""),
                "account_id_masked": account_id_masked,
                "order_mode": "LIVE_SIM",
                "opened_at": now,
                "closed_at": "",
                "entry_qty": 0,
                "entry_avg_price": 0,
                "current_qty": 0,
                "realized_qty": 0,
                "realized_pnl": 0.0,
                "realized_pnl_pct": 0.0,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
                "max_favorable_excursion_pct": 0.0,
                "max_adverse_excursion_pct": 0.0,
                "stop_loss_price": 0,
                "take_profit_price": 0,
                "max_hold_exit_at": "",
                "status": "OPEN",
                "details": {},
                "updated_at": now,
            }
        if side == "buy":
            old_qty = int(current.get("current_qty") or 0)
            old_avg = int(current.get("entry_avg_price") or 0)
            new_qty = old_qty + fill_qty
            if new_qty > 0:
                current["entry_avg_price"] = int(round(((old_avg * old_qty) + (fill_price * fill_qty)) / new_qty)) if old_qty else fill_price
            current["entry_qty"] = int(current.get("entry_qty") or 0) + fill_qty
            current["current_qty"] = new_qty
            current["status"] = "OPEN"
            current["stop_loss_price"] = _price_from_pct(int(current["entry_avg_price"]), float(exit_cfg.get("stop_loss_pct") or -2.0))
            current["take_profit_price"] = _price_from_pct(int(current["entry_avg_price"]), float(exit_cfg.get("take_profit_pct") or 5.0))
            if not current.get("max_hold_exit_at"):
                current["max_hold_exit_at"] = _add_minutes(now, int(exit_cfg.get("max_hold_minutes") or 60))
        elif side == "sell":
            entry_avg = int(current.get("entry_avg_price") or fill_price)
            sell_qty = min(fill_qty, int(current.get("current_qty") or 0))
            current["current_qty"] = max(0, int(current.get("current_qty") or 0) - sell_qty)
            current["realized_qty"] = int(current.get("realized_qty") or 0) + sell_qty
            current["realized_pnl"] = float(current.get("realized_pnl") or 0.0) + float((fill_price - entry_avg) * sell_qty)
            basis = max(1.0, float(entry_avg * max(1, int(current.get("realized_qty") or sell_qty or 1))))
            current["realized_pnl_pct"] = round(float(current["realized_pnl"]) / basis * 100.0, 6)
            if int(current.get("current_qty") or 0) <= 0:
                current["status"] = "CLOSED"
                current["closed_at"] = now
        current["updated_at"] = now
        params = _live_sim_position_params(current)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_positions(
                    position_id, candidate_instance_id, code, name, account_id_masked,
                    order_mode, opened_at, closed_at, entry_qty, entry_avg_price,
                    current_qty, realized_qty, realized_pnl, realized_pnl_pct,
                    unrealized_pnl, unrealized_pnl_pct, max_favorable_excursion_pct,
                    max_adverse_excursion_pct, stop_loss_price, take_profit_price,
                    max_hold_exit_at, status, details_json, updated_at
                ) VALUES (
                    :position_id, :candidate_instance_id, :code, :name, :account_id_masked,
                    :order_mode, :opened_at, :closed_at, :entry_qty, :entry_avg_price,
                    :current_qty, :realized_qty, :realized_pnl, :realized_pnl_pct,
                    :unrealized_pnl, :unrealized_pnl_pct, :max_favorable_excursion_pct,
                    :max_adverse_excursion_pct, :stop_loss_price, :take_profit_price,
                    :max_hold_exit_at, :status, :details_json, :updated_at
                )
                ON CONFLICT(position_id) DO UPDATE SET
                    closed_at=excluded.closed_at,
                    entry_qty=excluded.entry_qty,
                    entry_avg_price=excluded.entry_avg_price,
                    current_qty=excluded.current_qty,
                    realized_qty=excluded.realized_qty,
                    realized_pnl=excluded.realized_pnl,
                    realized_pnl_pct=excluded.realized_pnl_pct,
                    unrealized_pnl=excluded.unrealized_pnl,
                    unrealized_pnl_pct=excluded.unrealized_pnl_pct,
                    max_favorable_excursion_pct=excluded.max_favorable_excursion_pct,
                    max_adverse_excursion_pct=excluded.max_adverse_excursion_pct,
                    stop_loss_price=excluded.stop_loss_price,
                    take_profit_price=excluded.take_profit_price,
                    max_hold_exit_at=excluded.max_hold_exit_at,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    updated_at=excluded.updated_at
                """,
                params,
            )
        row = self.conn.execute("SELECT * FROM live_sim_positions WHERE position_id = ?", (position_id,)).fetchone()
        return _row_to_live_sim_position(row) if row else current

    def save_live_sim_position(self, record: dict) -> dict:
        params = _live_sim_position_params(record)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_positions(
                    position_id, candidate_instance_id, code, name, account_id_masked,
                    order_mode, opened_at, closed_at, entry_qty, entry_avg_price,
                    current_qty, realized_qty, realized_pnl, realized_pnl_pct,
                    unrealized_pnl, unrealized_pnl_pct, max_favorable_excursion_pct,
                    max_adverse_excursion_pct, stop_loss_price, take_profit_price,
                    max_hold_exit_at, status, details_json, updated_at
                ) VALUES (
                    :position_id, :candidate_instance_id, :code, :name, :account_id_masked,
                    :order_mode, :opened_at, :closed_at, :entry_qty, :entry_avg_price,
                    :current_qty, :realized_qty, :realized_pnl, :realized_pnl_pct,
                    :unrealized_pnl, :unrealized_pnl_pct, :max_favorable_excursion_pct,
                    :max_adverse_excursion_pct, :stop_loss_price, :take_profit_price,
                    :max_hold_exit_at, :status, :details_json, :updated_at
                )
                ON CONFLICT(position_id) DO UPDATE SET
                    closed_at=excluded.closed_at,
                    entry_qty=excluded.entry_qty,
                    entry_avg_price=excluded.entry_avg_price,
                    current_qty=excluded.current_qty,
                    realized_qty=excluded.realized_qty,
                    realized_pnl=excluded.realized_pnl,
                    realized_pnl_pct=excluded.realized_pnl_pct,
                    unrealized_pnl=excluded.unrealized_pnl,
                    unrealized_pnl_pct=excluded.unrealized_pnl_pct,
                    max_favorable_excursion_pct=excluded.max_favorable_excursion_pct,
                    max_adverse_excursion_pct=excluded.max_adverse_excursion_pct,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    updated_at=excluded.updated_at
                """,
                params,
            )
        row = self.conn.execute("SELECT * FROM live_sim_positions WHERE position_id = ?", (params["position_id"],)).fetchone()
        return _row_to_live_sim_position(row) if row else dict(record or {})

    def get_live_sim_position(self, position_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM live_sim_positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return _row_to_live_sim_position(row) if row else None

    def list_live_sim_positions(
        self,
        *,
        status: Optional[str] = None,
        code: Optional[str] = None,
        account_id_masked: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if account_id_masked:
            clauses.append("account_id_masked = ?")
            params.append(account_id_masked)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM live_sim_positions
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_live_sim_position(row) for row in rows]

    def save_live_sim_runtime_health(
        self,
        component: str,
        *,
        status: str,
        reason: str = "",
        consecutive_failures: int = 0,
        details: Optional[dict] = None,
        updated_at: str = "",
    ) -> dict:
        if not updated_at:
            from trading.broker.models import utc_timestamp

            updated_at = utc_timestamp()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_runtime_health(
                    component, status, reason, consecutive_failures, updated_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    consecutive_failures=excluded.consecutive_failures,
                    updated_at=excluded.updated_at,
                    details_json=excluded.details_json
                """,
                (
                    component,
                    status,
                    reason,
                    int(consecutive_failures or 0),
                    updated_at,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        return self.get_live_sim_runtime_health(component) or {}

    def get_live_sim_runtime_health(self, component: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM live_sim_runtime_health WHERE component = ?",
            (component,),
        ).fetchone()
        return _row_to_live_sim_health(row) if row else None

    def save_live_sim_reconcile_event(self, payload: dict) -> dict:
        params = _live_sim_reconcile_params(payload)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO live_sim_reconcile_events(
                    event_id, trigger, status, reason, started_at, completed_at,
                    payload_json, reason_codes_json
                ) VALUES (
                    :event_id, :trigger, :status, :reason, :started_at, :completed_at,
                    :payload_json, :reason_codes_json
                )
                ON CONFLICT(event_id) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    completed_at=excluded.completed_at,
                    payload_json=excluded.payload_json,
                    reason_codes_json=excluded.reason_codes_json
                """,
                params,
            )
        row = self.conn.execute(
            "SELECT * FROM live_sim_reconcile_events WHERE event_id = ?",
            (params["event_id"],),
        ).fetchone()
        return _row_to_live_sim_reconcile(row) if row else dict(payload or {})

    def live_sim_summary(self, *, trade_date: Optional[str] = None) -> dict:
        params: list[object] = []
        where = ""
        if trade_date:
            where = "WHERE trade_date = ?"
            params.append(trade_date)
        rows = self.conn.execute(
            f"SELECT order_status, COUNT(*) AS count FROM live_sim_orders {where} GROUP BY order_status",
            tuple(params),
        ).fetchall()
        counts = {str(row["order_status"]): int(row["count"] or 0) for row in rows}
        cancel_rows = self.conn.execute("SELECT status, cancel_reason, COUNT(*) AS count FROM live_sim_cancel_orders GROUP BY status, cancel_reason").fetchall()
        cancel_counts = {
            (str(row["status"] or ""), str(row["cancel_reason"] or "")): int(row["count"] or 0)
            for row in cancel_rows
        }
        reason_rows = self.conn.execute(
            """
            SELECT reason_codes_json FROM live_sim_orders
            UNION ALL
            SELECT reason_codes_json FROM live_sim_cancel_orders
            UNION ALL
            SELECT reason_codes_json FROM live_sim_reconcile_events
            """
        ).fetchall()
        reason_counts: dict[str, int] = {}
        for row in reason_rows:
            for code in _safe_json_loads(row["reason_codes_json"], []):
                text = str(code or "")
                if text:
                    reason_counts[text] = reason_counts.get(text, 0) + 1
        positions = self.conn.execute("SELECT status, realized_pnl, realized_pnl_pct FROM live_sim_positions").fetchall()
        position_status_counts: dict[str, int] = {}
        for row in positions:
            status = str(row["status"] or "")
            position_status_counts[status] = position_status_counts.get(status, 0) + 1
        manual_row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM live_sim_fill_events
            WHERE raw_event_json LIKE '%"manual_intervention": true%'
            """
        ).fetchone()
        manual_intervention_count = int(manual_row["count"] or 0) if manual_row else 0
        reconcile_required_count = counts.get("RECONCILE_REQUIRED", 0) + position_status_counts.get("RECONCILE_REQUIRED", 0)
        realized = [float(row["realized_pnl"] or 0.0) for row in positions]
        realized_pct = [float(row["realized_pnl_pct"] or 0.0) for row in positions if float(row["realized_pnl_pct"] or 0.0) != 0.0]
        return {
            "ledger_ok": reconcile_required_count == 0 and counts.get("UNKNOWN_SUBMIT", 0) == 0,
            "submitted_order_count": counts.get("SUBMITTED", 0) + counts.get("ACCEPTED", 0),
            "accepted_order_count": counts.get("ACCEPTED", 0),
            "rejected_order_count": counts.get("REJECTED", 0) + counts.get("FAILED", 0),
            "reconcile_required_order_count": counts.get("RECONCILE_REQUIRED", 0),
            "reconcile_required_position_count": position_status_counts.get("RECONCILE_REQUIRED", 0),
            "reconcile_required_count": reconcile_required_count,
            "manual_intervention_count": manual_intervention_count,
            "filled_order_count": counts.get("FILLED", 0),
            "partial_fill_count": counts.get("PARTIAL_FILLED", 0),
            "cancelled_order_count": counts.get("CANCELLED", 0),
            "duplicate_order_blocked_count": counts.get("DUPLICATE", 0),
            "unknown_submit_count": counts.get("UNKNOWN_SUBMIT", 0),
            "open_position_count": sum(1 for row in positions if str(row["status"]) in {"OPEN", "PARTIAL"}),
            "opened_position_count": sum(1 for row in positions if str(row["status"]) in {"OPEN", "PARTIAL"}),
            "closed_position_count": sum(1 for row in positions if str(row["status"]) in {"CLOSED", "FORCE_CLOSED"}),
            "unfilled_buy_cancel_due_count": reason_counts.get("LIVE_SIM_UNFILLED_BUY_CANCEL_DUE", 0),
            "cancel_order_queued_count": reason_counts.get("LIVE_SIM_CANCEL_ORDER_QUEUED", 0),
            "cancel_order_submitted_count": cancel_counts.get(("SUBMITTED", "unfilled_buy"), 0)
            + cancel_counts.get(("SUBMITTED", "unfilled_sell"), 0)
            + cancel_counts.get(("SUBMITTED", "partial_remainder"), 0),
            "cancel_order_accepted_count": reason_counts.get("LIVE_SIM_CANCEL_ORDER_ACCEPTED", 0),
            "cancel_order_rejected_count": reason_counts.get("LIVE_SIM_CANCEL_ORDER_REJECTED", 0),
            "duplicate_cancel_blocked_count": reason_counts.get("LIVE_SIM_CANCEL_DUPLICATE_BLOCKED", 0),
            "partial_remainder_cancel_count": reason_counts.get("LIVE_SIM_PARTIAL_REMAINDER_CANCEL_DUE", 0),
            "stop_loss_triggered_count": reason_counts.get("LIVE_SIM_STOP_LOSS_TRIGGERED", 0),
            "take_profit_triggered_count": reason_counts.get("LIVE_SIM_TAKE_PROFIT_TRIGGERED", 0),
            "max_hold_exit_triggered_count": reason_counts.get("LIVE_SIM_MAX_HOLD_EXIT_TRIGGERED", 0),
            "market_close_liquidation_triggered_count": reason_counts.get("LIVE_SIM_MARKET_CLOSE_LIQUIDATION_TRIGGERED", 0),
            "exit_order_submitted_count": reason_counts.get("LIVE_SIM_EXIT_ORDER_SUBMITTED", 0)
            + reason_counts.get("LIVE_SIM_EXIT_ORDER_QUEUED", 0),
            "exit_duplicate_blocked_count": reason_counts.get("LIVE_SIM_EXIT_DUPLICATE_BLOCKED", 0),
            "exit_order_failed_count": reason_counts.get("LIVE_SIM_EXIT_ORDER_BLOCKED", 0),
            "reconcile_started_count": reason_counts.get("LIVE_SIM_RECONCILE_STARTED", 0),
            "reconcile_completed_count": reason_counts.get("LIVE_SIM_RECONCILE_COMPLETED", 0),
            "reconcile_failed_count": reason_counts.get("LIVE_SIM_RECONCILE_FAILED", 0),
            "reconcile_on_startup_count": reason_counts.get("LIVE_SIM_RECONCILE_ON_STARTUP", 0),
            "reconcile_on_reconnect_count": reason_counts.get("LIVE_SIM_RECONCILE_ON_RECONNECT", 0),
            "orders_reconciled_count": reason_counts.get("LIVE_SIM_RECONCILE_ORDER_FILLED_FROM_BROKER", 0)
            + reason_counts.get("LIVE_SIM_RECONCILE_ORDER_CANCELLED_FROM_BROKER", 0),
            "positions_reconciled_count": reason_counts.get("LIVE_SIM_RECONCILE_POSITION_SYNCED", 0),
            "external_position_detected_count": reason_counts.get("LIVE_SIM_RECONCILE_EXTERNAL_POSITION_DETECTED", 0),
            "buy_blocked_reconcile_required_count": reason_counts.get("LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED", 0),
            "buy_blocked_exit_monitor_unhealthy_count": reason_counts.get("LIVE_SIM_BUY_BLOCKED_EXIT_MONITOR_UNHEALTHY", 0),
            "buy_blocked_pending_cancel_count": reason_counts.get("LIVE_SIM_BUY_BLOCKED_PENDING_CANCEL", 0),
            "buy_blocked_unknown_submit_count": reason_counts.get("LIVE_SIM_BUY_BLOCKED_UNKNOWN_SUBMIT", 0),
            "buy_blocked_reconcile_failure_count": reason_counts.get("LIVE_SIM_BUY_BLOCKED_RECONCILE_FAILURE_LIMIT", 0),
            "live_real_blocked_count": reason_counts.get("LIVE_REAL_ORDER_BLOCKED", 0),
            "win_count": sum(1 for value in realized if value > 0),
            "loss_count": sum(1 for value in realized if value < 0),
            "realized_pnl_total": round(sum(realized), 4),
            "realized_pnl_pct_avg": round(sum(realized_pct) / len(realized_pct), 6) if realized_pct else 0.0,
        }

    def list_runtime_order_intents(
        self,
        *,
        trade_date: Optional[str] = None,
        status: Optional[str] = None,
        code: Optional[str] = None,
        candidate_id: Optional[int] = None,
        side: Optional[str] = None,
        order_phase: Optional[str] = None,
        virtual_position_id: Optional[int] = None,
        exit_decision_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if candidate_id is not None:
            clauses.append("candidate_id = ?")
            params.append(int(candidate_id))
        if side:
            clauses.append("side = ?")
            params.append(side)
        if order_phase:
            clauses.append("order_phase = ?")
            params.append(order_phase)
        if virtual_position_id is not None:
            clauses.append("virtual_position_id = ?")
            params.append(int(virtual_position_id))
        if exit_decision_id is not None:
            clauses.append("exit_decision_id = ?")
            params.append(int(exit_decision_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM runtime_order_intents
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_runtime_order_intent(row) for row in rows]

    def runtime_order_intent_summary(self, *, trade_date: Optional[str] = None) -> dict:
        params: list[object] = []
        where = ""
        if trade_date:
            where = "WHERE trade_date = ?"
            params.append(trade_date)
        rows = self.conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM runtime_order_intents
            {where}
            GROUP BY status
            """,
            tuple(params),
        ).fetchall()
        status_counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        duplicate_event_query = """
            SELECT COUNT(*) AS count
            FROM runtime_order_intent_events
            WHERE event_type = 'duplicate_rejected'
        """
        duplicate_event_params: tuple = ()
        if trade_date:
            duplicate_event_query += " AND substr(created_at, 1, 10) = ?"
            duplicate_event_params = (trade_date,)
        duplicate_events = self.conn.execute(duplicate_event_query, duplicate_event_params).fetchone()
        duplicate_count = int(duplicate_events["count"] or 0) if duplicate_events else 0
        total = sum(status_counts.values())
        live_rows = self.conn.execute(
            f"""
            SELECT status, live_safety_json, reason, code, strategy_name, side, order_phase,
                   exit_decision_type, exit_reason
            FROM runtime_order_intents
            {where}
            """,
            tuple(params),
        ).fetchall()
        live_would_pass = 0
        exit_live_would_pass = 0
        exit_live_would_reject = 0
        reject_reasons: dict[str, int] = {}
        by_code: dict[str, int] = {}
        by_strategy_name: dict[str, int] = {}
        by_side: dict[str, int] = {}
        by_order_phase: dict[str, int] = {}
        exit_by_decision_type: dict[str, int] = {}
        exit_by_reason: dict[str, int] = {}
        exit_status_counts: dict[str, int] = {}
        for row in live_rows:
            live_safety = _safe_json_loads(row["live_safety_json"], {})
            if bool(live_safety.get("ok")):
                live_would_pass += 1
            else:
                reason = str(live_safety.get("reason") or row["reason"] or "UNKNOWN")
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            side = str(row["side"] or "")
            order_phase = str(row["order_phase"] or "")
            if side:
                by_side[side] = by_side.get(side, 0) + 1
            if order_phase:
                by_order_phase[order_phase] = by_order_phase.get(order_phase, 0) + 1
            if order_phase == "exit" or side == "sell":
                status = str(row["status"] or "")
                exit_status_counts[status] = exit_status_counts.get(status, 0) + 1
                if bool(live_safety.get("ok")):
                    exit_live_would_pass += 1
                else:
                    exit_live_would_reject += 1
                decision_type = str(row["exit_decision_type"] or "")
                if decision_type:
                    exit_by_decision_type[decision_type] = exit_by_decision_type.get(decision_type, 0) + 1
                exit_reason = str(row["exit_reason"] or row["reason"] or "")
                if exit_reason:
                    exit_by_reason[exit_reason] = exit_by_reason.get(exit_reason, 0) + 1
            code = str(row["code"] or "")
            if code:
                by_code[code] = by_code.get(code, 0) + 1
            strategy_name = str(row["strategy_name"] or "")
            if strategy_name:
                by_strategy_name[strategy_name] = by_strategy_name.get(strategy_name, 0) + 1
        return {
            "total": total,
            "accepted": status_counts.get("DRY_RUN_ACCEPTED", 0) + status_counts.get("ACCEPTED", 0),
            "rejected": status_counts.get("DRY_RUN_REJECTED", 0) + status_counts.get("REJECTED", 0),
            "duplicate": status_counts.get("DUPLICATE", 0) + duplicate_count,
            "entry_total": by_order_phase.get("entry", 0),
            "exit_total": by_order_phase.get("exit", 0),
            "buy_total": by_side.get("buy", 0),
            "sell_total": by_side.get("sell", 0),
            "exit_accepted": exit_status_counts.get("DRY_RUN_ACCEPTED", 0) + exit_status_counts.get("ACCEPTED", 0),
            "exit_rejected": exit_status_counts.get("DRY_RUN_REJECTED", 0) + exit_status_counts.get("REJECTED", 0),
            "exit_duplicate": exit_status_counts.get("DUPLICATE", 0),
            "observe_skipped": status_counts.get("OBSERVE_SKIPPED", 0),
            "live_blocked": status_counts.get("LIVE_BLOCKED", 0),
            "error": status_counts.get("ERROR", 0),
            "live_would_pass": live_would_pass,
            "live_would_reject": max(0, total - live_would_pass),
            "exit_live_would_pass": exit_live_would_pass,
            "exit_live_would_reject": exit_live_would_reject,
            "top_reject_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(reject_reasons.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
            "by_code": [
                {"code": code, "count": count}
                for code, count in sorted(by_code.items(), key=lambda item: item[1], reverse=True)[:20]
            ],
            "by_strategy_name": [
                {"strategy_name": strategy_name, "count": count}
                for strategy_name, count in sorted(by_strategy_name.items(), key=lambda item: item[1], reverse=True)[:20]
            ],
            "by_side": [
                {"side": side, "count": count}
                for side, count in sorted(by_side.items(), key=lambda item: item[1], reverse=True)
            ],
            "by_order_phase": [
                {"order_phase": phase, "count": count}
                for phase, count in sorted(by_order_phase.items(), key=lambda item: item[1], reverse=True)
            ],
            "exit_by_decision_type": [
                {"decision_type": decision_type, "count": count}
                for decision_type, count in sorted(exit_by_decision_type.items(), key=lambda item: item[1], reverse=True)
            ],
            "exit_by_reason": [
                {"reason": reason, "count": count}
                for reason, count in sorted(exit_by_reason.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
            "status_counts": status_counts,
        }

    def list_runtime_order_intent_events(self, intent_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, intent_id, event_type, status_from, status_to, message,
                   payload_json, created_at
            FROM runtime_order_intent_events
            WHERE intent_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (intent_id, max(1, int(limit or 100))),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "payload_json"},
                "payload": _safe_json_loads(row["payload_json"], {}),
            }
            for row in rows
        ]

    def list_runtime_order_intents_for_analysis(
        self,
        *,
        trade_date: Optional[str] = None,
        strategy_name: Optional[str] = None,
        code: Optional[str] = None,
        side: Optional[str] = None,
        order_phase: Optional[str] = None,
        include_rejected: bool = True,
        include_duplicates: bool = False,
        limit: int = 10000,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if side:
            clauses.append("side = ?")
            params.append(side)
        if order_phase:
            clauses.append("order_phase = ?")
            params.append(order_phase)
        if not include_rejected:
            clauses.append("status NOT IN ('DRY_RUN_REJECTED', 'REJECTED', 'LIVE_BLOCKED')")
        if not include_duplicates:
            clauses.append("status != 'DUPLICATE'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM runtime_order_intents
            {where}
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 10000)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_runtime_order_intent(row) for row in rows]

    def list_trade_reviews_for_analysis(
        self,
        *,
        trade_date: Optional[str] = None,
        code: Optional[str] = None,
        strategy_name: Optional[str] = None,
        limit: int = 10000,
        offset: int = 0,
    ) -> list[TradeReview]:
        query = "SELECT * FROM trade_reviews"
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if code:
            clauses.append("code = ?")
            params.append(code)
        if strategy_name:
            clauses.append("strategy_profile = ?")
            params.append(strategy_name)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id ASC LIMIT ? OFFSET ?"
        rows = self.conn.execute(
            query,
            tuple(params + [max(1, int(limit or 10000)), max(0, int(offset or 0))]),
        ).fetchall()
        return [self._row_to_trade_review(row) for row in rows]

    def list_virtual_positions_for_analysis(self) -> list[VirtualPosition]:
        rows = self.conn.execute("SELECT * FROM virtual_positions ORDER BY id ASC").fetchall()
        return [self._row_to_virtual_position(row) for row in rows]

    def list_exit_decisions_for_analysis(self) -> list[ExitDecision]:
        rows = self.conn.execute("SELECT * FROM exit_decisions ORDER BY id ASC").fetchall()
        return [self._row_to_exit_decision(row) for row in rows]

    def save_dry_run_performance_report(self, report: dict) -> dict:
        report_id = str(report.get("report_id") or "")
        if not report_id:
            raise ValueError("report_id is required")
        summary = dict(report.get("summary") or {})
        grouped = dict(report.get("grouped") or {})
        false_signal = dict(report.get("false_signal_summary") or {})
        recommendations = list(report.get("recommendations") or [])
        filters = dict(report.get("filters") or {})
        items = list(report.get("items") or [])
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO dry_run_performance_reports(
                    report_id, trade_date, status, summary_json, grouped_json,
                    false_signal_json, recommendation_json, filters_json, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    status=excluded.status,
                    summary_json=excluded.summary_json,
                    grouped_json=excluded.grouped_json,
                    false_signal_json=excluded.false_signal_json,
                    recommendation_json=excluded.recommendation_json,
                    filters_json=excluded.filters_json,
                    generated_at=excluded.generated_at
                """,
                (
                    report_id,
                    str(report.get("trade_date") or filters.get("trade_date") or ""),
                    str(report.get("status") or "READY"),
                    json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(grouped, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(false_signal, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(recommendations, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(filters, ensure_ascii=False, sort_keys=True, default=str),
                    str(report.get("generated_at") or ""),
                ),
            )
            self.conn.execute("DELETE FROM dry_run_performance_items WHERE report_id = ?", (report_id,))
            for item in items:
                self.conn.execute(
                    """
                    INSERT INTO dry_run_performance_items(
                        report_id, lifecycle_id, trade_date, code, candidate_id,
                        virtual_order_id, virtual_position_id, trade_review_id,
                        entry_intent_id, exit_intent_ids_json, final_status,
                        realized_return_pct, max_return_20m, max_drawdown_20m,
                        dry_run_false_positive_type, dry_run_false_negative_type,
                        quality_bucket, item_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        str(item.get("lifecycle_id") or ""),
                        str(item.get("trade_date") or ""),
                        str(item.get("code") or ""),
                        item.get("candidate_id"),
                        item.get("virtual_order_id"),
                        item.get("virtual_position_id"),
                        item.get("trade_review_id"),
                        str(item.get("entry_intent_id") or ""),
                        json.dumps(list(item.get("exit_intent_ids") or []), ensure_ascii=False),
                        str(item.get("final_status") or ""),
                        item.get("realized_return_pct"),
                        item.get("max_return_20m"),
                        item.get("max_drawdown_20m"),
                        str(item.get("dry_run_false_positive_type") or ""),
                        str(item.get("dry_run_false_negative_type") or ""),
                        str(item.get("quality_bucket") or ""),
                        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )
        return self.get_dry_run_performance_report(report_id) or {"report_id": report_id}

    def list_dry_run_performance_reports(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, report_id, trade_date, status, summary_json, false_signal_json,
                   recommendation_json, filters_json, generated_at, created_at
            FROM dry_run_performance_reports
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit or 50)), max(0, int(offset or 0))),
        ).fetchall()
        return [_row_to_dry_run_performance_report(row, include_grouped=False, include_items=False) for row in rows]

    def get_dry_run_performance_report(self, report_id: str, *, include_items: bool = True) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM dry_run_performance_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _row_to_dry_run_performance_report(row, include_grouped=True, include_items=False)
        if include_items:
            payload["items"] = self.list_dry_run_performance_items(report_id, limit=10000)
        return payload

    def list_dry_run_performance_items(self, report_id: str, *, limit: int = 1000, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM dry_run_performance_items
            WHERE report_id = ?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (report_id, max(1, int(limit or 1000)), max(0, int(offset or 0))),
        ).fetchall()
        return [_row_to_dry_run_performance_item(row) for row in rows]

    def save_dry_run_threshold_ab_report(self, report: dict) -> dict:
        report_id = str(report.get("report_id") or "")
        if not report_id:
            raise ValueError("report_id is required")
        summary = dict(report.get("summary") or {})
        candidates = list(report.get("candidates") or [])
        scenarios = list(report.get("scenarios") or [])
        results = dict(report.get("results") or {})
        recommendations = list(report.get("recommendations") or [])
        filters = dict(report.get("filters") or {})
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO dry_run_threshold_ab_reports(
                    report_id, trade_date, status, summary_json, candidates_json,
                    scenarios_json, results_json, recommendations_json, filters_json, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    status=excluded.status,
                    summary_json=excluded.summary_json,
                    candidates_json=excluded.candidates_json,
                    scenarios_json=excluded.scenarios_json,
                    results_json=excluded.results_json,
                    recommendations_json=excluded.recommendations_json,
                    filters_json=excluded.filters_json,
                    generated_at=excluded.generated_at
                """,
                (
                    report_id,
                    str(report.get("trade_date") or filters.get("trade_date") or ""),
                    str(report.get("status") or "READY"),
                    json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(candidates, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(scenarios, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(results, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(recommendations, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(filters, ensure_ascii=False, sort_keys=True, default=str),
                    str(report.get("generated_at") or ""),
                ),
            )
            self.conn.execute("DELETE FROM dry_run_threshold_ab_candidates WHERE report_id = ?", (report_id,))
            for candidate in candidates:
                result = results.get(str(candidate.get("candidate_id") or ""), {})
                recommendation = dict(result.get("recommendation") or {})
                delta = dict(result.get("delta") or {})
                self.conn.execute(
                    """
                    INSERT INTO dry_run_threshold_ab_candidates(
                        report_id, candidate_id, category, parameter_name, label_ko,
                        baseline_value, candidate_value, recommendation_grade,
                        expected_net_benefit_score, avoided_false_positive_count,
                        newly_created_false_negative_count, opportunity_loss_delta,
                        sample_count, confidence, candidate_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        str(candidate.get("candidate_id") or ""),
                        str(candidate.get("category") or ""),
                        str(candidate.get("parameter_name") or ""),
                        str(candidate.get("label_ko") or ""),
                        str(candidate.get("baseline_value") or ""),
                        str(candidate.get("candidate_value") or ""),
                        str(recommendation.get("grade") or candidate.get("recommendation_grade") or ""),
                        recommendation.get("expected_net_benefit_score"),
                        int(delta.get("avoided_false_positive_count") or 0),
                        int(delta.get("newly_created_false_negative_count") or 0),
                        int(delta.get("opportunity_loss_delta") or 0),
                        int(recommendation.get("sample_count") or 0),
                        recommendation.get("confidence"),
                        json.dumps({**candidate, "result": result}, ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )
        return self.get_dry_run_threshold_ab_report(report_id) or {"report_id": report_id}

    def list_dry_run_threshold_ab_reports(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, report_id, trade_date, status, summary_json,
                   recommendations_json, filters_json, generated_at, created_at
            FROM dry_run_threshold_ab_reports
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit or 50)), max(0, int(offset or 0))),
        ).fetchall()
        return [_row_to_dry_run_threshold_ab_report(row, include_details=False) for row in rows]

    def get_dry_run_threshold_ab_report(self, report_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM dry_run_threshold_ab_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _row_to_dry_run_threshold_ab_report(row, include_details=True)
        payload["candidate_rows"] = self.list_dry_run_threshold_ab_candidates(report_id, limit=10000)
        return payload

    def list_dry_run_threshold_ab_candidates(self, report_id: str, *, limit: int = 1000, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM dry_run_threshold_ab_candidates
            WHERE report_id = ?
            ORDER BY
                CASE recommendation_grade
                    WHEN 'STRONG_CANDIDATE' THEN 0
                    WHEN 'WATCH_CANDIDATE' THEN 1
                    WHEN 'RISKY_CANDIDATE' THEN 2
                    WHEN 'DATA_INSUFFICIENT' THEN 3
                    ELSE 4
                END,
                expected_net_benefit_score DESC,
                id ASC
            LIMIT ? OFFSET ?
            """,
            (report_id, max(1, int(limit or 1000)), max(0, int(offset or 0))),
        ).fetchall()
        return [_row_to_dry_run_threshold_ab_candidate(row) for row in rows]

    def save_gateway_transport_latency_sample(self, sample: dict) -> dict:
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id:
            raise ValueError("sample_id is required")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO gateway_transport_latency_samples(
                    sample_id, trace_id, trade_date, direction, message_type,
                    event_id, command_id, request_id, source, success, error,
                    transport_mode, experiment_id, scenario, connection_id, websocket_session_id,
                    ws_session_id, ws_connection_id, ws_connection_state, ws_fallback_reason,
                    session_loss_count, duplicate_ack_count, unknown_ack_count,
                    payload_size_bytes, total_wall_ms,
                    gateway_queue_wait_ms, gateway_post_ms, core_receive_ms,
                    core_persist_ms, core_dispatch_wait_ms, long_poll_wait_ms,
                    gateway_receive_wait_ms, gateway_local_queue_wait_ms,
                    rate_limit_wait_ms, gateway_execute_ms, ack_round_trip_ms,
                    ws_send_ms, ws_receive_ms, ws_reconnect_count, ws_message_sequence,
                    clock_skew_warning, stage_ms_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                    success=excluded.success,
                    error=excluded.error,
                    experiment_id=excluded.experiment_id,
                    scenario=excluded.scenario,
                    connection_id=excluded.connection_id,
                    websocket_session_id=excluded.websocket_session_id,
                    ws_session_id=excluded.ws_session_id,
                    ws_connection_id=excluded.ws_connection_id,
                    ws_connection_state=excluded.ws_connection_state,
                    ws_fallback_reason=excluded.ws_fallback_reason,
                    session_loss_count=excluded.session_loss_count,
                    duplicate_ack_count=excluded.duplicate_ack_count,
                    unknown_ack_count=excluded.unknown_ack_count,
                    payload_size_bytes=excluded.payload_size_bytes,
                    total_wall_ms=excluded.total_wall_ms,
                    gateway_queue_wait_ms=excluded.gateway_queue_wait_ms,
                    gateway_post_ms=excluded.gateway_post_ms,
                    core_receive_ms=excluded.core_receive_ms,
                    core_persist_ms=excluded.core_persist_ms,
                    core_dispatch_wait_ms=excluded.core_dispatch_wait_ms,
                    long_poll_wait_ms=excluded.long_poll_wait_ms,
                    gateway_receive_wait_ms=excluded.gateway_receive_wait_ms,
                    gateway_local_queue_wait_ms=excluded.gateway_local_queue_wait_ms,
                    rate_limit_wait_ms=excluded.rate_limit_wait_ms,
                    gateway_execute_ms=excluded.gateway_execute_ms,
                    ack_round_trip_ms=excluded.ack_round_trip_ms,
                    ws_send_ms=excluded.ws_send_ms,
                    ws_receive_ms=excluded.ws_receive_ms,
                    ws_reconnect_count=excluded.ws_reconnect_count,
                    ws_message_sequence=excluded.ws_message_sequence,
                    clock_skew_warning=excluded.clock_skew_warning,
                    stage_ms_json=excluded.stage_ms_json,
                    metadata_json=excluded.metadata_json
                """,
                (
                    sample_id,
                    str(sample.get("trace_id") or ""),
                    str(sample.get("trade_date") or str(sample.get("created_at") or "")[:10]),
                    str(sample.get("direction") or ""),
                    str(sample.get("message_type") or ""),
                    str(sample.get("event_id") or ""),
                    str(sample.get("command_id") or ""),
                    str(sample.get("request_id") or ""),
                    str(sample.get("source") or ""),
                    int(bool(sample.get("success", True))),
                    str(sample.get("error") or ""),
                    str(sample.get("transport_mode") or "rest_long_poll"),
                    str(sample.get("experiment_id") or (sample.get("metadata") or {}).get("experiment_id") or ""),
                    str(sample.get("scenario") or (sample.get("metadata") or {}).get("scenario") or ""),
                    str(sample.get("connection_id") or (sample.get("metadata") or {}).get("connection_id") or ""),
                    str(sample.get("websocket_session_id") or (sample.get("metadata") or {}).get("websocket_session_id") or ""),
                    str(sample.get("ws_session_id") or (sample.get("metadata") or {}).get("ws_session_id") or (sample.get("metadata") or {}).get("websocket_session_id") or ""),
                    str(sample.get("ws_connection_id") or (sample.get("metadata") or {}).get("ws_connection_id") or (sample.get("metadata") or {}).get("connection_id") or ""),
                    str(sample.get("ws_connection_state") or (sample.get("metadata") or {}).get("ws_connection_state") or ""),
                    str(sample.get("ws_fallback_reason") or (sample.get("metadata") or {}).get("ws_fallback_reason") or ""),
                    int(sample.get("session_loss_count") or (sample.get("metadata") or {}).get("session_loss_count") or 0),
                    int(sample.get("duplicate_ack_count") or (sample.get("metadata") or {}).get("duplicate_ack_count") or 0),
                    int(sample.get("unknown_ack_count") or (sample.get("metadata") or {}).get("unknown_ack_count") or 0),
                    int(sample.get("payload_size_bytes") or 0),
                    sample.get("total_wall_ms"),
                    sample.get("gateway_queue_wait_ms"),
                    sample.get("gateway_post_ms"),
                    sample.get("core_receive_ms"),
                    sample.get("core_persist_ms"),
                    sample.get("core_dispatch_wait_ms"),
                    sample.get("long_poll_wait_ms"),
                    sample.get("gateway_receive_wait_ms"),
                    sample.get("gateway_local_queue_wait_ms"),
                    sample.get("rate_limit_wait_ms"),
                    sample.get("gateway_execute_ms"),
                    sample.get("ack_round_trip_ms"),
                    sample.get("ws_send_ms"),
                    sample.get("ws_receive_ms"),
                    int(sample.get("ws_reconnect_count") or 0),
                    sample.get("ws_message_sequence"),
                    int(bool(sample.get("clock_skew_warning"))),
                    json.dumps(sample.get("stage_ms") or {}, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(sample.get("metadata") or {}, ensure_ascii=False, sort_keys=True, default=str),
                    str(sample.get("created_at") or ""),
                ),
            )
        return self.get_gateway_transport_latency_sample(sample_id) or {"sample_id": sample_id}

    def get_gateway_transport_latency_sample(self, sample_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM gateway_transport_latency_samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        return _row_to_gateway_transport_latency_sample(row) if row else None

    def find_gateway_transport_latency_sample_by_ws_message(
        self,
        *,
        ws_session_id: str,
        ws_message_sequence: int,
        message_type: str,
        event_id: str = "",
        command_id: str = "",
    ) -> Optional[dict]:
        clauses = ["ws_session_id = ?", "ws_message_sequence = ?", "message_type = ?"]
        params: list[object] = [ws_session_id, int(ws_message_sequence or 0), message_type]
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if command_id:
            clauses.append("command_id = ?")
            params.append(command_id)
        row = self.conn.execute(
            f"""
            SELECT * FROM gateway_transport_latency_samples
            WHERE {" AND ".join(clauses)}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_to_gateway_transport_latency_sample(row) if row else None

    def update_gateway_transport_latency_sample_stage(
        self,
        sample_id: str,
        *,
        stage_updates: dict,
        metadata_updates: Optional[dict] = None,
    ) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT stage_ms_json, metadata_json FROM gateway_transport_latency_samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        if row is None:
            return None
        stage = _safe_json_loads(row["stage_ms_json"], {})
        metadata = _safe_json_loads(row["metadata_json"], {})
        stage.update({key: value for key, value in dict(stage_updates or {}).items() if value is not None})
        metadata.update({key: value for key, value in dict(metadata_updates or {}).items() if value is not None})
        with self.conn:
            self.conn.execute(
                """
                UPDATE gateway_transport_latency_samples
                SET stage_ms_json = ?, metadata_json = ?
                WHERE sample_id = ?
                """,
                (
                    json.dumps(stage, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                    sample_id,
                ),
            )
        return self.get_gateway_transport_latency_sample(sample_id)

    def list_gateway_transport_latency_samples(
        self,
        *,
        trade_date: Optional[str] = None,
        direction: Optional[str] = None,
        message_type: Optional[str] = None,
        command_id: Optional[str] = None,
        event_id: Optional[str] = None,
        transport_mode: Optional[str] = None,
        experiment_id: Optional[str] = None,
        scenario: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if message_type:
            clauses.append("message_type = ?")
            params.append(message_type)
        if command_id:
            clauses.append("command_id = ?")
            params.append(command_id)
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if transport_mode:
            clauses.append("transport_mode = ?")
            params.append(transport_mode)
        if experiment_id:
            clauses.append("experiment_id = ?")
            params.append(experiment_id)
        if scenario:
            clauses.append("scenario = ?")
            params.append(scenario)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM gateway_transport_latency_samples
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 100)), max(0, int(offset or 0))]),
        ).fetchall()
        return [_row_to_gateway_transport_latency_sample(row) for row in rows]

    def latest_gateway_transport_errors(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM gateway_transport_latency_samples
            WHERE success = 0 OR error != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit or 10)),),
        ).fetchall()
        return [_row_to_gateway_transport_latency_sample(row) for row in rows]

    def list_gateway_transport_experiments(
        self,
        *,
        experiment_id: Optional[str] = None,
        scenario: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses = ["experiment_id != ''"]
        params: list[object] = []
        if experiment_id:
            clauses.append("experiment_id = ?")
            params.append(experiment_id)
        if scenario:
            clauses.append("scenario = ?")
            params.append(scenario)
        where = " AND ".join(clauses)
        rows = self.conn.execute(
            f"""
            SELECT
                experiment_id,
                scenario,
                GROUP_CONCAT(DISTINCT transport_mode) AS transport_modes,
                COUNT(*) AS sample_count,
                MIN(created_at) AS started_at,
                MAX(created_at) AS ended_at
            FROM gateway_transport_latency_samples
            WHERE {where}
            GROUP BY experiment_id, scenario
            ORDER BY ended_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [max(1, int(limit or 50)), max(0, int(offset or 0))]),
        ).fetchall()
        return [
            {
                "experiment_id": row["experiment_id"],
                "scenario": row["scenario"],
                "transport_modes": [item for item in str(row["transport_modes"] or "").split(",") if item],
                "sample_count": int(row["sample_count"] or 0),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            }
            for row in rows
        ]

    def save_gateway_transport_latency_report(self, report: dict) -> dict:
        report_id = str(report.get("report_id") or "")
        if not report_id:
            raise ValueError("report_id is required")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO gateway_transport_latency_reports(
                    report_id, trade_date, transport_mode, experiment_id, scenario, status, summary_json,
                    recommendation_json, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    transport_mode=excluded.transport_mode,
                    experiment_id=excluded.experiment_id,
                    scenario=excluded.scenario,
                    status=excluded.status,
                    summary_json=excluded.summary_json,
                    recommendation_json=excluded.recommendation_json,
                    generated_at=excluded.generated_at
                """,
                (
                    report_id,
                    str(report.get("trade_date") or ""),
                    str(report.get("transport_mode") or "rest_long_poll"),
                    str(report.get("experiment_id") or report.get("filters", {}).get("experiment_id") or ""),
                    str(report.get("scenario") or report.get("filters", {}).get("scenario") or ""),
                    str(report.get("status") or "READY"),
                    json.dumps(report.get("summary") or {}, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(report.get("websocket_recommendation") or {}, ensure_ascii=False, sort_keys=True, default=str),
                    str(report.get("generated_at") or ""),
                ),
            )
        return self.get_gateway_transport_latency_report(report_id) or {"report_id": report_id}

    def list_gateway_transport_latency_reports(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT * FROM gateway_transport_latency_reports
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit or 50)), max(0, int(offset or 0))),
        ).fetchall()
        return [_row_to_gateway_transport_latency_report(row) for row in rows]

    def get_gateway_transport_latency_report(self, report_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM gateway_transport_latency_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return _row_to_gateway_transport_latency_report(row) if row else None

    def prune_gateway_transport_latency_samples(self, older_than_sec: int) -> int:
        if older_than_sec <= 0:
            return 0
        with self.conn:
            cursor = self.conn.execute(
                """
                DELETE FROM gateway_transport_latency_samples
                WHERE created_at < datetime('now', ?)
                """,
                (f"-{int(older_than_sec)} seconds",),
            )
        return int(cursor.rowcount or 0)

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

    def save_theme_lab_flow_result(self, calculated_at: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_lab_flow_snapshots(
                    calculated_at, market_status_json, theme_rankings_json,
                    theme_condition_snapshots_json, condition_hit_snapshots_json,
                    watchset_snapshots_json, gate_decisions_json, data_quality_json,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(calculated_at or ""),
                    json.dumps(payload.get("market_status") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("theme_rankings") or [], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("theme_condition_snapshots") or [], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("condition_hit_snapshots") or [], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("watchset_snapshots") or [], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("gate_decisions") or [], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.get("data_quality") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def latest_theme_lab_flow_result(self) -> dict:
        row = self.conn.execute(
            """
            SELECT *
            FROM theme_lab_flow_snapshots
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return {}
        payload = _safe_json_loads(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("market_status", _safe_json_loads(row["market_status_json"], {}))
        payload.setdefault("theme_rankings", _safe_json_loads(row["theme_rankings_json"], []))
        payload.setdefault("theme_condition_snapshots", _safe_json_loads(row["theme_condition_snapshots_json"], []))
        payload.setdefault("condition_hit_snapshots", _safe_json_loads(row["condition_hit_snapshots_json"], []))
        payload.setdefault("watchset_snapshots", _safe_json_loads(row["watchset_snapshots_json"], []))
        payload.setdefault("gate_decisions", _safe_json_loads(row["gate_decisions_json"], []))
        payload.setdefault("data_quality", _safe_json_loads(row["data_quality_json"], {}))
        payload["created_at"] = row["created_at"]
        payload["calculated_at"] = row["calculated_at"]
        return payload

    def save_operator_event(self, event: dict) -> bool:
        normalized = _normalize_operator_event(event)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO dashboard_operator_events(
                    event_id, trade_date, occurred_at, received_at, source,
                    event_type, severity, category, symbol, stock_name,
                    primary_theme, stock_role, candidate_instance_id,
                    from_status, to_status, gate_status, display_status,
                    message_ko, payload_json, acknowledged_at, acknowledged_by,
                    hidden, snoozed_until
                ) VALUES (
                    :event_id, :trade_date, :occurred_at, :received_at, :source,
                    :event_type, :severity, :category, :symbol, :stock_name,
                    :primary_theme, :stock_role, :candidate_instance_id,
                    :from_status, :to_status, :gate_status, :display_status,
                    :message_ko, :payload_json, :acknowledged_at, :acknowledged_by,
                    :hidden, :snoozed_until
                )
                """,
                normalized,
            )
        return cursor.rowcount == 1

    def save_operator_events(self, events: list[dict]) -> dict:
        inserted = 0
        duplicate = 0
        rejected = 0
        for event in events or []:
            try:
                if self.save_operator_event(event):
                    inserted += 1
                else:
                    duplicate += 1
            except (TypeError, ValueError, sqlite3.Error):
                rejected += 1
        return {"inserted_count": inserted, "duplicate_count": duplicate, "rejected_count": rejected}

    def list_operator_events(
        self,
        trade_date: str,
        *,
        severity: str | None = None,
        category: str | None = None,
        symbol: str | None = None,
        include_acknowledged: bool = True,
        include_hidden: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        where = ["trade_date = ?"]
        params: list[object] = [str(trade_date or "")]
        if severity:
            where.append("severity = ?")
            params.append(str(severity).upper())
        if category:
            where.append("category = ?")
            params.append(str(category).lower())
        if symbol:
            where.append("symbol = ?")
            params.append(str(symbol))
        if not include_acknowledged:
            where.append("acknowledged_at IS NULL")
        if not include_hidden:
            where.append("hidden = 0")
        normalized_limit = max(1, min(1000, int(limit or 200)))
        params.append(normalized_limit)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM dashboard_operator_events
            WHERE {" AND ".join(where)}
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_operator_event_row_to_dict(row) for row in rows]

    def acknowledge_operator_event(self, event_id: str, acknowledged_by: str | None = None) -> int:
        return self.acknowledge_operator_events([event_id], acknowledged_by=acknowledged_by)

    def acknowledge_operator_events(self, event_ids: list[str], acknowledged_by: str | None = None) -> int:
        ids = [str(event_id or "") for event_id in event_ids or [] if str(event_id or "")]
        if not ids:
            return 0
        acknowledged_at = datetime.now().isoformat(timespec="seconds")
        updated = 0
        with self.conn:
            for event_id in ids:
                cursor = self.conn.execute(
                    """
                    UPDATE dashboard_operator_events
                    SET acknowledged_at = COALESCE(acknowledged_at, ?),
                        acknowledged_by = COALESCE(NULLIF(?, ''), acknowledged_by)
                    WHERE event_id = ?
                    """,
                    (acknowledged_at, str(acknowledged_by or ""), event_id),
                )
                updated += cursor.rowcount
        return updated

    def hide_operator_event(self, event_id: str) -> int:
        return self.hide_operator_events([event_id])

    def hide_operator_events(self, event_ids: list[str]) -> int:
        ids = [str(event_id or "") for event_id in event_ids or [] if str(event_id or "")]
        if not ids:
            return 0
        updated = 0
        with self.conn:
            for event_id in ids:
                cursor = self.conn.execute(
                    "UPDATE dashboard_operator_events SET hidden = 1 WHERE event_id = ?",
                    (event_id,),
                )
                updated += cursor.rowcount
        return updated

    def summarize_operator_events(self, trade_date: str) -> dict:
        events = self.list_operator_events(trade_date, include_acknowledged=True, include_hidden=False, limit=1000)
        severity_counts = Counter(str(event.get("severity") or "").upper() for event in events)
        type_counts = Counter(str(event.get("event_type") or "") for event in events)
        symbol_counts = Counter(str(event.get("symbol") or "") for event in events if event.get("symbol"))
        theme_counts = Counter(str(event.get("primary_theme") or "") for event in events if event.get("primary_theme"))
        return {
            "trade_date": str(trade_date or ""),
            "total_count": len(events),
            "critical_count": severity_counts.get("CRITICAL", 0),
            "warning_count": severity_counts.get("WARNING", 0),
            "opportunity_count": severity_counts.get("OPPORTUNITY", 0),
            "info_count": severity_counts.get("INFO", 0),
            "ready_event_count": type_counts.get("BUY_READY_NEW", 0),
            "ready_small_event_count": type_counts.get("BUY_READY_SMALL_NEW", 0),
            "live_guard_blocked_count": type_counts.get("READY_BUT_LIVE_BLOCKED", 0),
            "order_intent_created_count": type_counts.get("ORDER_INTENT_CREATED", 0),
            "virtual_order_created_count": type_counts.get("VIRTUAL_ORDER_CREATED", 0),
            "market_wait_started_count": type_counts.get("MARKET_WAIT_STARTED", 0),
            "data_quality_degraded_count": type_counts.get("DATA_QUALITY_DEGRADED", 0),
            "chase_risk_blocked_count": type_counts.get("CHASE_RISK_BLOCKED", 0),
            "gateway_disconnected_count": type_counts.get("GATEWAY_DISCONNECTED", 0),
            "snapshot_stale_count": type_counts.get("SNAPSHOT_STALE", 0),
            "by_event_type": dict(type_counts),
            "by_symbol": [{"symbol": symbol, "count": count} for symbol, count in symbol_counts.most_common(20)],
            "by_theme": [{"primary_theme": theme, "count": count} for theme, count in theme_counts.most_common(20)],
        }

    def get_operator_event(self, event_id: str) -> Optional[dict]:
        if not event_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM dashboard_operator_events WHERE event_id = ?",
            (str(event_id),),
        ).fetchone()
        return _operator_event_row_to_dict(row) if row else None

    def snooze_operator_event(self, event_id: str, snoozed_until: str) -> int:
        if not event_id:
            return 0
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE dashboard_operator_events SET snoozed_until = ? WHERE event_id = ?",
                (str(snoozed_until or ""), str(event_id)),
            )
        return cursor.rowcount

    def save_operator_action(self, action: dict) -> dict:
        normalized = _normalize_operator_action(action)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO dashboard_operator_actions(
                    action_id, trade_date, requested_at, completed_at,
                    action_type, status, source, requested_by, event_id,
                    symbol, stock_name, candidate_instance_id, requires_token,
                    confirmation_required, endpoint, request_payload_json,
                    response_payload_json, error_message
                ) VALUES (
                    :action_id, :trade_date, :requested_at, :completed_at,
                    :action_type, :status, :source, :requested_by, :event_id,
                    :symbol, :stock_name, :candidate_instance_id, :requires_token,
                    :confirmation_required, :endpoint, :request_payload_json,
                    :response_payload_json, :error_message
                )
                """,
                normalized,
            )
        return self.get_operator_action(normalized["action_id"]) or dict(normalized)

    def get_operator_action(self, action_id: str) -> Optional[dict]:
        if not action_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM dashboard_operator_actions WHERE action_id = ?",
            (str(action_id),),
        ).fetchone()
        return _operator_action_row_to_dict(row) if row else None

    def update_operator_action_status(
        self,
        action_id: str,
        status: str,
        response: Optional[dict] = None,
        error_message: Optional[str] = None,
    ) -> Optional[dict]:
        if not action_id:
            return None
        completed_at = datetime.now().isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """
                UPDATE dashboard_operator_actions
                SET status = ?,
                    completed_at = ?,
                    response_payload_json = ?,
                    error_message = ?
                WHERE action_id = ?
                """,
                (
                    str(status or "").upper(),
                    completed_at,
                    json.dumps(response or {}, ensure_ascii=False, sort_keys=True, default=str),
                    str(error_message or "") or None,
                    str(action_id),
                ),
            )
        return self.get_operator_action(action_id)

    def list_operator_actions(
        self,
        trade_date: str,
        *,
        action_type: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        event_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        where = ["trade_date = ?"]
        params: list[object] = [str(trade_date or "")]
        if action_type:
            where.append("action_type = ?")
            params.append(str(action_type).upper())
        if status:
            where.append("status = ?")
            params.append(str(status).upper())
        if symbol:
            where.append("symbol = ?")
            params.append(str(symbol))
        if event_id:
            where.append("event_id = ?")
            params.append(str(event_id))
        normalized_limit = max(1, min(1000, int(limit or 100)))
        normalized_offset = max(0, int(offset or 0))
        params.extend([normalized_limit, normalized_offset])
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM dashboard_operator_actions
            WHERE {" AND ".join(where)}
            ORDER BY requested_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [_operator_action_row_to_dict(row) for row in rows]

    def summarize_operator_actions(self, trade_date: str) -> dict:
        actions = self.list_operator_actions(trade_date, limit=1000)
        status_counts = Counter(str(action.get("status") or "").upper() for action in actions)
        type_counts = Counter(str(action.get("action_type") or "") for action in actions)
        return {
            "trade_date": str(trade_date or ""),
            "total_count": len(actions),
            "pending_count": status_counts.get("PENDING", 0),
            "running_count": status_counts.get("RUNNING", 0),
            "success_count": status_counts.get("SUCCESS", 0),
            "failed_count": status_counts.get("FAILED", 0),
            "blocked_count": status_counts.get("BLOCKED", 0),
            "skipped_count": status_counts.get("SKIPPED", 0),
            "by_status": dict(status_counts),
            "by_action_type": dict(type_counts),
        }

    def save_postmarket_review_item(self, item: dict) -> bool:
        normalized = _normalize_postmarket_review_item(item)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO dashboard_postmarket_reviews(
                    review_id, trade_date, generated_at, review_scope,
                    symbol, stock_name, primary_theme, stock_role,
                    candidate_instance_id, event_id, event_type, source_status,
                    block_reason, block_reason_codes_json, base_time, base_price,
                    price_1m, price_3m, price_5m, price_10m,
                    price_close_or_last, return_1m_pct, return_3m_pct,
                    return_5m_pct, return_10m_pct, return_close_or_last_pct,
                    outcome_label, confidence, confidence_reason,
                    recommendation_ko, payload_json
                ) VALUES (
                    :review_id, :trade_date, :generated_at, :review_scope,
                    :symbol, :stock_name, :primary_theme, :stock_role,
                    :candidate_instance_id, :event_id, :event_type, :source_status,
                    :block_reason, :block_reason_codes_json, :base_time, :base_price,
                    :price_1m, :price_3m, :price_5m, :price_10m,
                    :price_close_or_last, :return_1m_pct, :return_3m_pct,
                    :return_5m_pct, :return_10m_pct, :return_close_or_last_pct,
                    :outcome_label, :confidence, :confidence_reason,
                    :recommendation_ko, :payload_json
                )
                """,
                normalized,
            )
        return cursor.rowcount == 1

    def save_postmarket_review_items(self, items: list[dict]) -> dict:
        inserted = 0
        duplicate = 0
        rejected = 0
        for item in items or []:
            try:
                if self.save_postmarket_review_item(item):
                    inserted += 1
                else:
                    duplicate += 1
            except (TypeError, ValueError, sqlite3.Error):
                rejected += 1
        return {"inserted_count": inserted, "duplicate_count": duplicate, "rejected_count": rejected}

    def list_postmarket_review_items(
        self,
        trade_date: str,
        *,
        review_scope: str | None = None,
        outcome_label: str | None = None,
        event_type: str | None = None,
        symbol: str | None = None,
        primary_theme: str | None = None,
        min_return_5m_pct: float | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        where = ["trade_date = ?"]
        params: list[object] = [str(trade_date or "")]
        if review_scope:
            where.append("review_scope = ?")
            params.append(str(review_scope).lower())
        if outcome_label:
            where.append("outcome_label = ?")
            params.append(str(outcome_label).upper())
        if event_type:
            where.append("event_type = ?")
            params.append(str(event_type).upper())
        if symbol:
            where.append("symbol = ?")
            params.append(str(symbol))
        if primary_theme:
            where.append("primary_theme = ?")
            params.append(str(primary_theme))
        if min_return_5m_pct is not None:
            where.append("return_5m_pct >= ?")
            params.append(float(min_return_5m_pct))
        normalized_limit = max(1, min(1000, int(limit or 200)))
        normalized_offset = max(0, int(offset or 0))
        params.extend([normalized_limit, normalized_offset])
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM dashboard_postmarket_reviews
            WHERE {" AND ".join(where)}
            ORDER BY generated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [_postmarket_review_row_to_dict(row) for row in rows]

    def summarize_postmarket_reviews(self, trade_date: str) -> dict:
        items = self.list_postmarket_review_items(trade_date, limit=1000)
        outcome_counts = Counter(str(item.get("outcome_label") or "").upper() for item in items)
        type_counts = Counter(str(item.get("event_type") or "") for item in items if item.get("event_type"))
        reason_counts = Counter(str(item.get("block_reason") or "") for item in items if item.get("block_reason"))
        symbol_counts = Counter(str(item.get("symbol") or "") for item in items if item.get("symbol"))
        theme_counts = Counter(str(item.get("primary_theme") or "") for item in items if item.get("primary_theme"))
        return {
            "trade_date": str(trade_date or ""),
            "total_count": len(items),
            "ready_count": type_counts.get("BUY_READY_NEW", 0),
            "ready_small_count": type_counts.get("BUY_READY_SMALL_NEW", 0),
            "ready_without_order_count": sum(
                1
                for item in items
                if str(item.get("event_type") or "").upper() in {"BUY_READY_NEW", "BUY_READY_SMALL_NEW", "READY_BUT_LIVE_BLOCKED"}
                and not bool((item.get("payload") or {}).get("has_order"))
            ),
            "ready_but_live_blocked_count": type_counts.get("READY_BUT_LIVE_BLOCKED", 0),
            "order_intent_created_count": type_counts.get("ORDER_INTENT_CREATED", 0),
            "virtual_order_created_count": type_counts.get("VIRTUAL_ORDER_CREATED", 0),
            "data_wait_count": type_counts.get("DATA_QUALITY_DEGRADED", 0) + type_counts.get("SNAPSHOT_STALE", 0),
            "market_wait_count": type_counts.get("MARKET_WAIT_STARTED", 0),
            "chase_blocked_count": type_counts.get("CHASE_RISK_BLOCKED", 0),
            "late_chase_temp_wait_count": type_counts.get("LATE_CHASE_TEMP_WAIT", 0),
            "observe_count": type_counts.get("READY_TO_WAIT", 0),
            "blocked_count": sum(
                type_counts.get(event_type, 0)
                for event_type in {
                    "READY_TO_WAIT",
                    "MARKET_WAIT_STARTED",
                    "DATA_QUALITY_DEGRADED",
                    "SNAPSHOT_STALE",
                    "GATEWAY_DISCONNECTED",
                    "CHASE_RISK_BLOCKED",
                    "LATE_CHASE_TEMP_WAIT",
                }
            ),
            "missed_opportunity_count": outcome_counts.get("MISSED_OPPORTUNITY", 0),
            "good_block_count": outcome_counts.get("GOOD_BLOCK", 0),
            "review_needed_count": outcome_counts.get("REVIEW_NEEDED", 0),
            "protected_from_chase_count": outcome_counts.get("PROTECTED_FROM_CHASE", 0),
            "protected_from_loss_count": outcome_counts.get("PROTECTED_FROM_CHASE", 0) + outcome_counts.get("GOOD_BLOCK", 0),
            "data_insufficient_count": outcome_counts.get("DATA_INSUFFICIENT", 0),
            "uncertain_block_count": outcome_counts.get("REVIEW_NEEDED", 0) + outcome_counts.get("DATA_INSUFFICIENT", 0),
            "neutral_count": outcome_counts.get("NEUTRAL", 0),
            "by_outcome_label": dict(outcome_counts),
            "by_event_type": dict(type_counts),
            "by_block_reason": [{"block_reason": reason, "count": count} for reason, count in reason_counts.most_common(20)],
            "by_symbol": [{"symbol": symbol, "count": count} for symbol, count in symbol_counts.most_common(20)],
            "by_theme": [{"primary_theme": theme, "count": count} for theme, count in theme_counts.most_common(20)],
            "top_missed_opportunities": _top_postmarket_items(items, "MISSED_OPPORTUNITY"),
            "top_good_blocks": _top_postmarket_items(items, "GOOD_BLOCK"),
            "top_review_needed": _top_postmarket_items(items, "REVIEW_NEEDED"),
        }

    def delete_postmarket_reviews_for_date(self, trade_date: str, review_scope: str | None = None) -> int:
        where = ["trade_date = ?"]
        params: list[object] = [str(trade_date or "")]
        if review_scope:
            where.append("review_scope = ?")
            params.append(str(review_scope).lower())
        with self.conn:
            cursor = self.conn.execute(
                f"DELETE FROM dashboard_postmarket_reviews WHERE {' AND '.join(where)}",
                params,
            )
        return cursor.rowcount

    def rebuild_postmarket_reviews(self, trade_date: str, review_scope: str = "postmarket") -> dict:
        from trading_app.dashboard_postmarket_review import build_postmarket_review

        report = build_postmarket_review(self, trade_date=trade_date, review_scope=review_scope)
        result = self.save_postmarket_review_items(list(report.get("items") or []))
        return {**report, **result}

    def upsert_market_side_confirmation_state(self, payload: dict) -> dict:
        normalized = _market_side_confirmation_state_params(payload)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO market_side_confirmation_state(
                    trade_date, session_id, market_side, raw_status, confirmed_status,
                    previous_confirmed_status, confirmation_pending, recovery_pending,
                    weak_consecutive_cycles, risk_off_consecutive_cycles, healthy_consecutive_cycles,
                    last_breadth_pct, last_index_return_pct, last_turnover_weighted_return_pct,
                    last_source, last_trust_level, last_data_quality_flags_json, last_reason_codes_json,
                    source_conflict, source_conflict_count, last_source_conflict_at,
                    last_status_changed_at, last_confirmed_at, last_recovered_at, wait_started_at,
                    last_cycle_id, last_evaluated_at, updated_at, created_at, expires_at, state_version
                ) VALUES (
                    :trade_date, :session_id, :market_side, :raw_status, :confirmed_status,
                    :previous_confirmed_status, :confirmation_pending, :recovery_pending,
                    :weak_consecutive_cycles, :risk_off_consecutive_cycles, :healthy_consecutive_cycles,
                    :last_breadth_pct, :last_index_return_pct, :last_turnover_weighted_return_pct,
                    :last_source, :last_trust_level, :last_data_quality_flags_json, :last_reason_codes_json,
                    :source_conflict, :source_conflict_count, :last_source_conflict_at,
                    :last_status_changed_at, :last_confirmed_at, :last_recovered_at, :wait_started_at,
                    :last_cycle_id, :last_evaluated_at, :updated_at, :created_at, :expires_at, :state_version
                )
                ON CONFLICT(trade_date, session_id, market_side, state_version) DO UPDATE SET
                    raw_status=excluded.raw_status,
                    confirmed_status=excluded.confirmed_status,
                    previous_confirmed_status=excluded.previous_confirmed_status,
                    confirmation_pending=excluded.confirmation_pending,
                    recovery_pending=excluded.recovery_pending,
                    weak_consecutive_cycles=excluded.weak_consecutive_cycles,
                    risk_off_consecutive_cycles=excluded.risk_off_consecutive_cycles,
                    healthy_consecutive_cycles=excluded.healthy_consecutive_cycles,
                    last_breadth_pct=excluded.last_breadth_pct,
                    last_index_return_pct=excluded.last_index_return_pct,
                    last_turnover_weighted_return_pct=excluded.last_turnover_weighted_return_pct,
                    last_source=excluded.last_source,
                    last_trust_level=excluded.last_trust_level,
                    last_data_quality_flags_json=excluded.last_data_quality_flags_json,
                    last_reason_codes_json=excluded.last_reason_codes_json,
                    source_conflict=excluded.source_conflict,
                    source_conflict_count=excluded.source_conflict_count,
                    last_source_conflict_at=excluded.last_source_conflict_at,
                    last_status_changed_at=excluded.last_status_changed_at,
                    last_confirmed_at=excluded.last_confirmed_at,
                    last_recovered_at=excluded.last_recovered_at,
                    wait_started_at=excluded.wait_started_at,
                    last_cycle_id=excluded.last_cycle_id,
                    last_evaluated_at=excluded.last_evaluated_at,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                normalized,
            )
        row = self.conn.execute(
            """
            SELECT *
            FROM market_side_confirmation_state
            WHERE trade_date = ? AND session_id = ? AND market_side = ? AND state_version = ?
            """,
            (
                normalized["trade_date"],
                normalized["session_id"],
                normalized["market_side"],
                normalized["state_version"],
            ),
        ).fetchone()
        return _row_to_market_side_confirmation_state(row) if row else normalized

    def load_market_side_confirmation_states(
        self,
        *,
        trade_date: str,
        session_id: str,
        state_version: int,
    ) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_side_confirmation_state
            WHERE trade_date = ? AND session_id = ? AND state_version = ?
            ORDER BY market_side
            """,
            (str(trade_date or ""), str(session_id or ""), int(state_version or 0)),
        ).fetchall()
        return [_row_to_market_side_confirmation_state(row) for row in rows]

    def load_any_market_side_confirmation_states(self, *, trade_date: str, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_side_confirmation_state
            WHERE trade_date = ? AND session_id = ?
            ORDER BY market_side, state_version DESC
            """,
            (str(trade_date or ""), str(session_id or "")),
        ).fetchall()
        return [_row_to_market_side_confirmation_state(row) for row in rows]

    def load_market_side_confirmation_states_for_trade_date(self, *, trade_date: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_side_confirmation_state
            WHERE trade_date = ?
            ORDER BY session_id DESC, market_side, state_version DESC
            """,
            (str(trade_date or ""),),
        ).fetchall()
        return [_row_to_market_side_confirmation_state(row) for row in rows]

    def load_recent_market_side_confirmation_states(self, *, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM market_side_confirmation_state
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit or 20)),),
        ).fetchall()
        return [_row_to_market_side_confirmation_state(row) for row in rows]

    def save_market_side_confirmation_transition(self, payload: dict) -> bool:
        normalized = _market_side_confirmation_transition_params(payload)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO market_side_confirmation_transitions(
                    trade_date, session_id, market_side, cycle_id,
                    previous_raw_status, new_raw_status,
                    previous_confirmed_status, new_confirmed_status,
                    previous_confirmation_pending, new_confirmation_pending,
                    previous_recovery_pending, new_recovery_pending,
                    weak_consecutive_cycles, risk_off_consecutive_cycles, healthy_consecutive_cycles,
                    breadth_pct, index_return_pct, turnover_weighted_return_pct,
                    source, trust_level, source_conflict, transition_reason_codes_json,
                    transition_type, created_at
                ) VALUES (
                    :trade_date, :session_id, :market_side, :cycle_id,
                    :previous_raw_status, :new_raw_status,
                    :previous_confirmed_status, :new_confirmed_status,
                    :previous_confirmation_pending, :new_confirmation_pending,
                    :previous_recovery_pending, :new_recovery_pending,
                    :weak_consecutive_cycles, :risk_off_consecutive_cycles, :healthy_consecutive_cycles,
                    :breadth_pct, :index_return_pct, :turnover_weighted_return_pct,
                    :source, :trust_level, :source_conflict, :transition_reason_codes_json,
                    :transition_type, :created_at
                )
                """,
                normalized,
            )
        return cursor.rowcount > 0

    def list_market_side_confirmation_transitions(
        self,
        *,
        trade_date: str = "",
        session_id: str = "",
        market_side: str = "",
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if market_side:
            clauses.append("market_side = ?")
            params.append(market_side)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM market_side_confirmation_transitions
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params + [max(1, int(limit or 100))]),
        ).fetchall()
        return [_row_to_market_side_confirmation_transition(row) for row in rows]

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
                        candidate_id, entry_plan_id, leg_index, weight_pct, status,
                        limit_price, virtual_fill_price, fill_policy, submitted_at,
                        filled_at, cancelled_at, unfilled_reason, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        leg_index = ?,
                        weight_pct = ?,
                        status = ?,
                        limit_price = ?,
                        virtual_fill_price = ?,
                        fill_policy = ?,
                        submitted_at = ?,
                        filled_at = ?,
                        cancelled_at = ?,
                        unfilled_reason = ?,
                        details_json = ?
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

    def save_position_context_snapshot(self, snapshot: PositionContextSnapshot) -> PositionContextSnapshot:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO position_context_history(
                    position_id, candidate_id, candidate_instance_id, code, trade_date,
                    captured_at, capture_reason, theme_id, theme_name, theme_score,
                    theme_status, leader_count, strong_count, breadth_status,
                    leader_code, leader_return_pct, leader_vwap_status, leader_support_broken,
                    index_market, index_status, index_return_pct, market_status,
                    market_risk_status, risk_reason_codes_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._position_context_params(snapshot),
            )
        row = self.conn.execute("SELECT * FROM position_context_history WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_position_context_snapshot(row)

    def list_position_context_history(self, position_id: int, *, limit: int = 100) -> list[PositionContextSnapshot]:
        rows = self.conn.execute(
            """
            SELECT * FROM position_context_history
            WHERE position_id = ?
            ORDER BY captured_at ASC, id ASC
            LIMIT ?
            """,
            (position_id, int(limit or 100)),
        ).fetchall()
        return [self._row_to_position_context_snapshot(row) for row in rows]

    def latest_position_context_snapshot(
        self,
        position_id: int,
        *,
        before_at: str = "",
        capture_reason: str = "",
    ) -> Optional[PositionContextSnapshot]:
        clauses = ["position_id = ?"]
        params: list[object] = [position_id]
        if before_at:
            clauses.append("captured_at < ?")
            params.append(before_at)
        if capture_reason:
            clauses.append("capture_reason = ?")
            params.append(capture_reason)
        row = self.conn.execute(
            f"""
            SELECT * FROM position_context_history
            WHERE {' AND '.join(clauses)}
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return self._row_to_position_context_snapshot(row) if row else None

    def list_position_context_history_for_analysis(
        self,
        *,
        trade_date: Optional[str] = None,
        position_ids: Optional[Iterable[int]] = None,
        limit: int = 10000,
    ) -> list[PositionContextSnapshot]:
        clauses: list[str] = []
        params: list[object] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        ids = [int(value) for value in (position_ids or []) if value is not None]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"position_id IN ({placeholders})")
            params.extend(ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM position_context_history
            {where}
            ORDER BY captured_at ASC, id ASC
            LIMIT ?
            """,
            tuple(params + [int(limit or 10000)]),
        ).fetchall()
        return [self._row_to_position_context_snapshot(row) for row in rows]

    def prune_position_context_history(
        self,
        *,
        cutoff_at: str,
        batch_size: int = 1000,
        created_at: str = "",
        details: Optional[dict] = None,
    ) -> dict:
        pruned = 0
        error_count = 0
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    DELETE FROM position_context_history
                    WHERE id IN (
                        SELECT id FROM position_context_history
                        WHERE captured_at < ?
                        ORDER BY captured_at ASC, id ASC
                        LIMIT ?
                    )
                    """,
                    (cutoff_at, max(1, int(batch_size or 1000))),
                )
                pruned = int(cursor.rowcount if cursor.rowcount is not None else 0)
        except Exception:
            error_count = 1
        retained_row = self.conn.execute(
            """
            SELECT COUNT(*) AS retained_count, MIN(captured_at) AS oldest_retained
            FROM position_context_history
            """
        ).fetchone()
        summary = {
            "created_at": created_at or "",
            "cutoff_at": cutoff_at,
            "pruned_context_history_rows": pruned,
            "retained_context_history_rows": int(retained_row["retained_count"] or 0) if retained_row else 0,
            "oldest_retained_context_at": str(retained_row["oldest_retained"] or "") if retained_row else "",
            "prune_error_count": error_count,
        }
        self.save_position_context_prune_run({**summary, "details": dict(details or {})})
        return summary

    def save_position_context_prune_run(self, summary: dict) -> dict:
        payload = dict(summary or {})
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO position_context_history_prune_runs(
                    created_at, cutoff_at, pruned_context_history_rows,
                    retained_context_history_rows, oldest_retained_context_at,
                    prune_error_count, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("created_at") or ""),
                    str(payload.get("cutoff_at") or ""),
                    int(payload.get("pruned_context_history_rows") or 0),
                    int(payload.get("retained_context_history_rows") or 0),
                    str(payload.get("oldest_retained_context_at") or ""),
                    int(payload.get("prune_error_count") or 0),
                    json.dumps(payload.get("details") or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        return {**payload, "id": cursor.lastrowid}

    def latest_position_context_prune_summary(self) -> dict:
        row = self.conn.execute(
            """
            SELECT * FROM position_context_history_prune_runs
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return {
                "pruned_context_history_rows": 0,
                "retained_context_history_rows": 0,
                "oldest_retained_context_at": "",
                "prune_error_count": 0,
            }
        return {
            "id": int(row["id"]),
            "created_at": row["created_at"],
            "cutoff_at": row["cutoff_at"],
            "pruned_context_history_rows": int(row["pruned_context_history_rows"] or 0),
            "retained_context_history_rows": int(row["retained_context_history_rows"] or 0),
            "oldest_retained_context_at": row["oldest_retained_context_at"],
            "prune_error_count": int(row["prune_error_count"] or 0),
            "details": dict(json.loads(row["details_json"] or "{}")),
        }

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

    def load_virtual_position(self, virtual_position_id: int) -> Optional[VirtualPosition]:
        row = self.conn.execute("SELECT * FROM virtual_positions WHERE id = ?", (virtual_position_id,)).fetchone()
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

    def load_exit_decision(self, exit_decision_id: int) -> Optional[ExitDecision]:
        row = self.conn.execute("SELECT * FROM exit_decisions WHERE id = ?", (exit_decision_id,)).fetchone()
        return self._row_to_exit_decision(row) if row else None

    def load_virtual_order(self, virtual_order_id: int) -> Optional[VirtualOrder]:
        row = self.conn.execute("SELECT * FROM virtual_orders WHERE id = ?", (virtual_order_id,)).fetchone()
        return self._row_to_virtual_order(row) if row else None

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

    def list_trade_reviews(
        self,
        candidate_id: Optional[int] = None,
        trade_date: Optional[str] = None,
    ) -> list[TradeReview]:
        rows = self._trade_review_rows(candidate_id=candidate_id, trade_date=trade_date)
        return [self._row_to_trade_review(row) for row in rows]

    def list_trade_reviews_for_date(self, trade_date: str) -> list[TradeReview]:
        rows = self._trade_review_rows(trade_date=trade_date)
        return [self._row_to_trade_review(row) for row in rows]

    def _trade_review_rows(
        self,
        candidate_id: Optional[int] = None,
        trade_date: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM trade_reviews"
        clauses = []
        params = []
        if candidate_id is not None:
            clauses.append("candidate_id = ?")
            params.append(candidate_id)
        if trade_date is not None:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        return self.conn.execute(query, params).fetchall()

    def latest_trade_reviews(self, limit: int = 200) -> list[TradeReview]:
        rows = self.conn.execute(
            "SELECT * FROM trade_reviews ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_trade_review(row) for row in reversed(rows)]

    def load_trade_review(self, trade_review_id: int) -> Optional[TradeReview]:
        row = self.conn.execute("SELECT * FROM trade_reviews WHERE id = ?", (trade_review_id,)).fetchone()
        return self._row_to_trade_review(row) if row else None

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

    def load_strategy_runtime_settings_profile(
        self,
        strategy_name: str,
        profile_name: str,
        profile_version: str,
        *,
        now: str = "",
    ) -> Optional[dict]:
        params = [strategy_name, profile_name, profile_version]
        query = """
            SELECT * FROM strategy_runtime_settings
            WHERE strategy_name = ?
              AND profile_name = ?
              AND profile_version = ?
              AND enabled = 1
        """
        if now:
            query += """
              AND (effective_from = '' OR effective_from <= ?)
              AND (effective_to = '' OR effective_to > ?)
            """
            params.extend([now, now])
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def save_strategy_runtime_settings_profile(self, payload: dict) -> dict:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO strategy_runtime_settings(
                    config_key, config_version, config_json,
                    strategy_name, profile_name, profile_version, mode, enabled,
                    effective_from, effective_to, settings_json, description, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    config_json=excluded.config_json,
                    strategy_name=excluded.strategy_name,
                    profile_name=excluded.profile_name,
                    profile_version=excluded.profile_version,
                    mode=excluded.mode,
                    enabled=excluded.enabled,
                    effective_from=excluded.effective_from,
                    effective_to=excluded.effective_to,
                    settings_json=excluded.settings_json,
                    description=excluded.description,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    payload["config_key"],
                    int(payload.get("config_version") or 1),
                    payload.get("config_json") or "{}",
                    payload.get("strategy_name") or "",
                    payload.get("profile_name") or "",
                    payload.get("profile_version") or "",
                    payload.get("mode") or "",
                    int(payload.get("enabled", 1)),
                    payload.get("effective_from") or "",
                    payload.get("effective_to") or "",
                    payload.get("settings_json") or "{}",
                    payload.get("description") or "",
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM strategy_runtime_settings WHERE config_key = ?",
                (payload["config_key"],),
            ).fetchone()
            return dict(row)

    def close(self) -> None:
        self.conn.close()

    def upsert_kiwoom_symbol_master(self, rows: Iterable[dict]) -> int:
        cleaned: list[dict] = []
        for item in rows:
            code = _clean_stock_code(item.get("code"))
            market = str(item.get("market") or "").strip().upper()
            if not code or market not in {"KOSPI", "KOSDAQ"}:
                continue
            cleaned.append(
                {
                    "code": code,
                    "name": str(item.get("name") or ""),
                    "market": market,
                    "market_code": str(item.get("market_code") or ""),
                    "source": str(item.get("source") or "kiwoom_code_list"),
                    "raw": dict(item.get("raw") or {}),
                }
            )
        if not cleaned:
            return 0
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO kiwoom_symbol_master(
                    code, name, market, market_code, source, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(code) DO UPDATE SET
                    name=COALESCE(NULLIF(excluded.name, ''), kiwoom_symbol_master.name),
                    market=excluded.market,
                    market_code=excluded.market_code,
                    source=excluded.source,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                [
                    (
                        item["code"],
                        item["name"],
                        item["market"],
                        item["market_code"],
                        item["source"],
                        json.dumps(item["raw"], ensure_ascii=False, sort_keys=True),
                    )
                    for item in cleaned
                ],
            )
        return len(cleaned)

    def list_kiwoom_symbol_master(self, codes: Iterable[str]) -> list[dict]:
        clean_codes = sorted({_clean_stock_code(code) for code in codes if _clean_stock_code(code)})
        if not clean_codes:
            return []
        placeholders = ",".join("?" for _ in clean_codes)
        rows = self.conn.execute(
            f"""
            SELECT code, name, market, market_code, source, raw_json, updated_at
            FROM kiwoom_symbol_master
            WHERE code IN ({placeholders})
            ORDER BY code
            """,
            tuple(clean_codes),
        ).fetchall()
        return [
            {
                "code": row["code"],
                "name": row["name"],
                "market": row["market"],
                "market_code": row["market_code"],
                "source": row["source"],
                "raw": _safe_json_loads(row["raw_json"], {}),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _archive_legacy_theme_mappings(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'theme_mappings'"
        ).fetchone()
        if row is None:
            return
        with self.conn:
            archive_exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'legacy_theme_mappings_archive'"
            ).fetchone()
            if archive_exists is None:
                self.conn.execute("ALTER TABLE theme_mappings RENAME TO legacy_theme_mappings_archive")
            else:
                self.conn.execute("DROP TABLE theme_mappings")

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
                    max_drawdown_pct, realized_return_pct, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    realized_return_pct = ?,
                    details_json = ?
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

    @staticmethod
    def _position_context_params(snapshot: PositionContextSnapshot) -> tuple:
        return (
            snapshot.position_id,
            snapshot.candidate_id,
            snapshot.candidate_instance_id,
            snapshot.code,
            snapshot.trade_date,
            snapshot.captured_at,
            snapshot.capture_reason,
            snapshot.theme_id,
            snapshot.theme_name,
            snapshot.theme_score,
            snapshot.theme_status,
            snapshot.leader_count,
            snapshot.strong_count,
            snapshot.breadth_status,
            snapshot.leader_code,
            snapshot.leader_return_pct,
            snapshot.leader_vwap_status,
            int(snapshot.leader_support_broken),
            snapshot.index_market,
            snapshot.index_status,
            snapshot.index_return_pct,
            snapshot.market_status,
            snapshot.market_risk_status,
            json.dumps(snapshot.risk_reason_codes, ensure_ascii=False),
            json.dumps(snapshot.metadata, ensure_ascii=False),
        )

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
            int(order.leg_index or 1),
            float(order.weight_pct if order.weight_pct is not None else 100.0),
            order.status.value,
            order.limit_price,
            order.virtual_fill_price,
            order.fill_policy.value,
            order.submitted_at,
            order.filled_at,
            order.cancelled_at,
            order.unfilled_reason,
            json.dumps(order.details, ensure_ascii=False),
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
            json.dumps(position.details, ensure_ascii=False),
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
        keys = set(row.keys())
        return VirtualOrder(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            entry_plan_id=int(row["entry_plan_id"]) if row["entry_plan_id"] is not None else None,
            leg_index=int(row["leg_index"]) if "leg_index" in keys else 1,
            weight_pct=float(row["weight_pct"]) if "weight_pct" in keys else 100.0,
            status=VirtualOrderStatus(row["status"]),
            limit_price=int(row["limit_price"]),
            virtual_fill_price=int(row["virtual_fill_price"]),
            fill_policy=FillPolicy(row["fill_policy"]),
            submitted_at=row["submitted_at"],
            filled_at=row["filled_at"],
            cancelled_at=row["cancelled_at"],
            unfilled_reason=row["unfilled_reason"],
            details=dict(json.loads(row["details_json"] if "details_json" in keys else "{}")),
        )

    @staticmethod
    def _row_to_virtual_position(row: sqlite3.Row) -> VirtualPosition:
        keys = set(row.keys())
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
            details=dict(json.loads(row["details_json"] if "details_json" in keys else "{}")),
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
    def _row_to_position_context_snapshot(row: sqlite3.Row) -> PositionContextSnapshot:
        return PositionContextSnapshot(
            id=int(row["id"]),
            position_id=int(row["position_id"]) if row["position_id"] is not None else None,
            candidate_id=int(row["candidate_id"]) if row["candidate_id"] is not None else None,
            candidate_instance_id=row["candidate_instance_id"],
            code=row["code"],
            trade_date=row["trade_date"],
            captured_at=row["captured_at"],
            capture_reason=row["capture_reason"],
            theme_id=row["theme_id"],
            theme_name=row["theme_name"],
            theme_score=float(row["theme_score"]) if row["theme_score"] is not None else None,
            theme_status=row["theme_status"],
            leader_count=int(row["leader_count"]) if row["leader_count"] is not None else None,
            strong_count=int(row["strong_count"]) if row["strong_count"] is not None else None,
            breadth_status=row["breadth_status"],
            leader_code=row["leader_code"],
            leader_return_pct=float(row["leader_return_pct"]) if row["leader_return_pct"] is not None else None,
            leader_vwap_status=row["leader_vwap_status"],
            leader_support_broken=bool(row["leader_support_broken"]),
            index_market=row["index_market"],
            index_status=row["index_status"],
            index_return_pct=float(row["index_return_pct"]) if row["index_return_pct"] is not None else None,
            market_status=row["market_status"],
            market_risk_status=row["market_risk_status"],
            risk_reason_codes=list(json.loads(row["risk_reason_codes_json"] or "[]")),
            metadata=dict(json.loads(row["metadata_json"] or "{}")),
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

    def _ensure_strategy_runtime_settings_columns(self) -> None:
        columns = {
            "strategy_name": "TEXT NOT NULL DEFAULT ''",
            "profile_name": "TEXT NOT NULL DEFAULT ''",
            "profile_version": "TEXT NOT NULL DEFAULT ''",
            "mode": "TEXT NOT NULL DEFAULT ''",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "effective_from": "TEXT NOT NULL DEFAULT ''",
            "effective_to": "TEXT NOT NULL DEFAULT ''",
            "settings_json": "TEXT NOT NULL DEFAULT '{}'",
            "description": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in columns.items():
            self._ensure_column("strategy_runtime_settings", name, definition)

    def _ensure_runtime_order_intent_columns(self) -> None:
        columns = {
            "order_phase": "TEXT NOT NULL DEFAULT 'entry'",
            "exit_decision_id": "INTEGER",
            "exit_decision_type": "TEXT NOT NULL DEFAULT ''",
            "exit_reason": "TEXT NOT NULL DEFAULT ''",
            "exit_percent": "REAL",
            "exit_quantity": "INTEGER",
            "remaining_quantity": "INTEGER",
            "position_entry_price": "INTEGER",
            "position_quantity": "INTEGER",
            "position_opened_at": "TEXT NOT NULL DEFAULT ''",
            "position_closed_at": "TEXT NOT NULL DEFAULT ''",
            "position_max_return_pct": "REAL",
            "position_max_drawdown_pct": "REAL",
            "realized_return_pct": "REAL",
            "virtual_exit_price": "INTEGER",
        }
        for name, definition in columns.items():
            self._ensure_column("runtime_order_intents", name, definition)
        self.conn.execute(
            """
            UPDATE runtime_order_intents
            SET order_phase = CASE WHEN side = 'sell' THEN 'exit' ELSE 'entry' END
            WHERE order_phase = ''
            """
        )

    def _ensure_runtime_order_intent_indexes(self) -> None:
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_order_phase
                ON runtime_order_intents(order_phase);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_side_created_at
                ON runtime_order_intents(side, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_virtual_position_id
                ON runtime_order_intents(virtual_position_id);
            CREATE INDEX IF NOT EXISTS idx_runtime_order_intents_exit_decision_id
                ON runtime_order_intents(exit_decision_id);
            """
        )

    def _ensure_gateway_transport_latency_columns(self) -> None:
        sample_columns = {
            "experiment_id": "TEXT NOT NULL DEFAULT ''",
            "scenario": "TEXT NOT NULL DEFAULT ''",
            "connection_id": "TEXT NOT NULL DEFAULT ''",
            "websocket_session_id": "TEXT NOT NULL DEFAULT ''",
            "ws_session_id": "TEXT NOT NULL DEFAULT ''",
            "ws_connection_id": "TEXT NOT NULL DEFAULT ''",
            "ws_connection_state": "TEXT NOT NULL DEFAULT ''",
            "ws_fallback_reason": "TEXT NOT NULL DEFAULT ''",
            "session_loss_count": "INTEGER NOT NULL DEFAULT 0",
            "duplicate_ack_count": "INTEGER NOT NULL DEFAULT 0",
            "unknown_ack_count": "INTEGER NOT NULL DEFAULT 0",
            "ws_send_ms": "REAL",
            "ws_receive_ms": "REAL",
            "ws_reconnect_count": "INTEGER NOT NULL DEFAULT 0",
            "ws_message_sequence": "INTEGER",
        }
        for name, definition in sample_columns.items():
            self._ensure_column("gateway_transport_latency_samples", name, definition)
        report_columns = {
            "experiment_id": "TEXT NOT NULL DEFAULT ''",
            "scenario": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in report_columns.items():
            self._ensure_column("gateway_transport_latency_reports", name, definition)

    def _ensure_gateway_transport_latency_indexes(self) -> None:
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_experiment_id
                ON gateway_transport_latency_samples(experiment_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_scenario_transport
                ON gateway_transport_latency_samples(scenario, transport_mode);
            CREATE INDEX IF NOT EXISTS idx_gateway_transport_latency_ws_session_id
                ON gateway_transport_latency_samples(ws_session_id);
            """
        )

    def _seed_legacy_strategy_runtime_settings(self) -> None:
        from trading.strategy.runtime_settings import legacy_profile_payload

        payload = legacy_profile_payload()
        exists = self.conn.execute(
            "SELECT 1 FROM strategy_runtime_settings WHERE config_key = ?",
            (payload["config_key"],),
        ).fetchone()
        if exists:
            return
        self.conn.execute(
            """
            INSERT INTO strategy_runtime_settings(
                config_key, config_version, config_json,
                strategy_name, profile_name, profile_version, mode, enabled,
                effective_from, effective_to, settings_json, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["config_key"],
                int(payload.get("config_version") or 1),
                payload.get("config_json") or "{}",
                payload.get("strategy_name") or "",
                payload.get("profile_name") or "",
                payload.get("profile_version") or "",
                payload.get("mode") or "",
                int(payload.get("enabled", 1)),
                payload.get("effective_from") or "",
                payload.get("effective_to") or "",
                payload.get("settings_json") or "{}",
                payload.get("description") or "",
            ),
        )

    def _ensure_column(self, table_name: str, column_name: str, column_definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name in existing:
            return
        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _default_review_key(review: TradeReview) -> str:
    status = review.final_status.value if isinstance(review.final_status, ReviewFinalStatus) else str(review.final_status)
    return f"{review.gate_result_key}:{status}:{review.virtual_order_id or ''}:{review.virtual_position_id or ''}"


def _strategy_decision_event_params(payload: dict) -> dict:
    now = str(payload.get("created_at") or payload.get("decision_at") or datetime.now().isoformat(timespec="seconds"))
    decision_at = str(payload.get("decision_at") or now)
    return {
        "decision_id": str(payload.get("decision_id") or f"decision:{uuid4().hex}"),
        "runtime_cycle_id": str(payload.get("runtime_cycle_id") or payload.get("cycle_id") or ""),
        "trade_date": str(payload.get("trade_date") or _trade_date_from_timestamp(decision_at) or ""),
        "created_at": now,
        "decision_at": decision_at,
        "candidate_id": _nullable_int(payload.get("candidate_id")),
        "candidate_instance_id": str(payload.get("candidate_instance_id") or ""),
        "candidate_generation_seq": int(payload.get("candidate_generation_seq") or 0),
        "code": _clean_stock_code(payload.get("code")) or str(payload.get("code") or ""),
        "name": str(payload.get("name") or ""),
        "theme_name": str(payload.get("theme_name") or ""),
        "strategy_name": str(payload.get("strategy_name") or ""),
        "strategy_version": str(payload.get("strategy_version") or ""),
        "config_hash": str(payload.get("config_hash") or ""),
        "gate_status": str(payload.get("gate_status") or ""),
        "gate_reason": str(payload.get("gate_reason") or ""),
        "reason_status": str(payload.get("reason_status") or ""),
        "reason_family": str(payload.get("reason_family") or ""),
        "reason_codes_json": _json_list(payload.get("reason_codes_json", payload.get("reason_codes", []))),
        "block_type": str(payload.get("block_type") or ""),
        "action_type": str(payload.get("action_type") or ""),
        "action_result": str(payload.get("action_result") or ""),
        "price": _nullable_float(payload.get("price")),
        "change_rate": _nullable_float(payload.get("change_rate")),
        "trade_value": _nullable_float(payload.get("trade_value")),
        "execution_strength": _nullable_float(payload.get("execution_strength")),
        "vwap": _nullable_float(payload.get("vwap")),
        "momentum_1m": _nullable_float(payload.get("momentum_1m")),
        "momentum_3m": _nullable_float(payload.get("momentum_3m")),
        "momentum_5m": _nullable_float(payload.get("momentum_5m")),
        "gate_score": _nullable_float(payload.get("gate_score")),
        "hybrid_score": _nullable_float(payload.get("hybrid_score")),
        "theme_score": _nullable_float(payload.get("theme_score")),
        "data_status": str(payload.get("data_status") or ""),
        "data_quality_issues_json": _json_list(payload.get("data_quality_issues_json", payload.get("data_quality_issues", []))),
        "order_intent_id": str(payload.get("order_intent_id") or payload.get("intent_id") or ""),
        "entry_plan_id": _nullable_int(payload.get("entry_plan_id")),
        "virtual_order_id": _nullable_int(payload.get("virtual_order_id")),
        "virtual_position_id": _nullable_int(payload.get("virtual_position_id")),
        "exit_decision_id": _nullable_int(payload.get("exit_decision_id")),
        "details_json": _json_payload(_sanitize_decision_details(payload.get("details_json", payload.get("details", {})))),
    }


def _strategy_decision_event_filters(
    *,
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    theme_name: Optional[str] = None,
    gate_status: Optional[str] = None,
    action_type: Optional[str] = None,
    action_result: Optional[str] = None,
    reason_status: Optional[str] = None,
    reason_family: Optional[str] = None,
    window_sec: Optional[int] = None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if trade_date:
        clauses.append("trade_date = ?")
        params.append(str(trade_date))
    if code:
        clauses.append("code = ?")
        params.append(_clean_stock_code(code) or str(code))
    if theme_name:
        clauses.append("theme_name = ?")
        params.append(str(theme_name))
    if gate_status:
        clauses.append("gate_status = ?")
        params.append(str(gate_status))
    if action_type:
        clauses.append("action_type = ?")
        params.append(str(action_type))
    if action_result:
        clauses.append("action_result = ?")
        params.append(str(action_result))
    if reason_status:
        clauses.append("reason_status = ?")
        params.append(str(reason_status))
    if reason_family:
        clauses.append("reason_family = ?")
        params.append(str(reason_family))
    if window_sec is not None:
        clauses.append("julianday(replace(substr(decision_at, 1, 19), 'T', ' ')) >= julianday('now', ?)")
        params.append(f"-{max(1, int(window_sec or 1))} seconds")
    return clauses, params


def _row_to_strategy_decision_event(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["reason_codes"] = _safe_json_loads(data.get("reason_codes_json"), [])
    data["data_quality_issues"] = _safe_json_loads(data.get("data_quality_issues_json"), [])
    data["details"] = _safe_json_loads(data.get("details_json"), {})
    return data


def _strategy_decision_summary(events: list[dict], *, trade_date: str = "", window_sec: Optional[int] = None) -> dict:
    def key(event: dict) -> str:
        return str(
            event.get("candidate_instance_id")
            or event.get("candidate_id")
            or event.get("code")
            or event.get("decision_id")
            or ""
        )

    by_action: dict[str, set[str]] = {
        "detected": set(),
        "evaluated": set(),
        "ready": set(),
        "wait": set(),
        "blocked": set(),
        "entry_plan": set(),
        "order_intent": set(),
        "open_position": set(),
        "exit_decision": set(),
    }
    ready_keys: set[str] = set()
    order_any_keys: set[str] = set()
    block_reasons: Counter[str] = Counter()
    wait_reasons: Counter[str] = Counter()
    data_quality: Counter[str] = Counter()
    major_reasons: Counter[str] = Counter()
    order_rejected_count = 0
    exit_decision_count = 0

    for event in events:
        event_key = key(event)
        if event_key:
            by_action["detected"].add(event_key)
        action_type = str(event.get("action_type") or "")
        action_result = str(event.get("action_result") or "")
        gate_status = str(event.get("gate_status") or "")
        if action_type == "EVALUATE":
            by_action["evaluated"].add(event_key)
        if action_type == "READY" or gate_status == "READY":
            by_action["ready"].add(event_key)
            ready_keys.add(event_key)
        if action_type == "WAIT" or gate_status == "WAIT":
            by_action["wait"].add(event_key)
        if action_type == "BLOCK" or gate_status == "BLOCKED":
            by_action["blocked"].add(event_key)
        if action_type == "ENTRY_PLAN" and action_result == "ACCEPTED":
            by_action["entry_plan"].add(event_key)
        if action_type == "ENTRY_ORDER_INTENT":
            order_any_keys.add(event_key)
            if action_result in {"ACCEPTED", "DUPLICATE"}:
                by_action["order_intent"].add(event_key)
            if action_result == "REJECTED":
                order_rejected_count += 1
        if action_type == "HOLD" and event.get("virtual_position_id") is not None:
            by_action["open_position"].add(event_key)
        if action_type == "EXIT_DECISION":
            by_action["exit_decision"].add(event_key)
            exit_decision_count += 1

        reason_codes = [str(reason) for reason in event.get("reason_codes") or [] if str(reason)]
        major_reasons.update(_major_reason_keys(reason_codes))
        if action_type == "BLOCK" or gate_status == "BLOCKED":
            block_reasons.update(reason_codes or [str(event.get("gate_reason") or "UNKNOWN")])
        if action_type == "WAIT" or gate_status == "WAIT":
            wait_reasons.update(reason_codes or [str(event.get("gate_reason") or "UNKNOWN")])
        data_quality.update(str(issue) for issue in event.get("data_quality_issues") or [] if str(issue))

    return {
        "trade_date": trade_date,
        "window_sec": window_sec,
        "event_count": len(events),
        "funnel": {name: len(values) for name, values in by_action.items()},
        "top_block_reasons": _counter_rows(block_reasons),
        "top_wait_reasons": _counter_rows(wait_reasons),
        "top_data_quality_issues": _counter_rows(data_quality),
        "major_reason_distribution": _counter_rows(major_reasons, limit=20),
        "ready_without_order_count": len(ready_keys - order_any_keys),
        "order_rejected_count": order_rejected_count,
        "exit_decision_count": exit_decision_count,
    }


def _major_reason_keys(reason_codes: list[str]) -> list[str]:
    result: list[str] = []
    for reason in reason_codes:
        text = str(reason or "").upper()
        if "DATA_INSUFFICIENT" in text:
            result.append("DATA_INSUFFICIENT")
        if text == "VI_UNKNOWN" or "VI_UNKNOWN" in text:
            result.append("VI_UNKNOWN")
        if "RISK_OFF" in text or text in {"MARKET_RISK_OFF", "RISK_OFF_ENTRY"}:
            result.append("RISK_OFF")
        if "LATE_CHASE" in text or "CHASE_RISK" in text:
            result.append("LATE_CHASE")
    return list(dict.fromkeys(result))


def _counter_rows(counter: Counter[str], limit: int = 10) -> list[dict]:
    return [{"reason": key, "count": int(count)} for key, count in counter.most_common(limit) if key]


def _nullable_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_decision_details(value: object) -> object:
    parsed = _safe_json_loads(value, {}) if isinstance(value, str) else value
    sensitive_terms = ("token", "secret", "password", "authorization", "account")

    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                key_text = str(key)
                if any(term in key_text.lower() for term in sensitive_terms):
                    continue
                result[key_text] = clean(child)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return clean(parsed if parsed is not None else {})


def _runtime_order_intent_params(payload: dict) -> dict:
    json_fields = {
        "safety_json": payload.get("safety_json", payload.get("safety", {})),
        "live_safety_json": payload.get("live_safety_json", payload.get("live_safety", {})),
        "request_json": payload.get("request_json", payload.get("request", {})),
        "response_json": payload.get("response_json", payload.get("response", {})),
        "metadata_json": payload.get("metadata_json", payload.get("metadata", {})),
    }
    normalized = {
        "intent_id": str(payload.get("intent_id") or ""),
        "trade_date": str(payload.get("trade_date") or ""),
        "source": str(payload.get("source") or ""),
        "mode": str(payload.get("mode") or ""),
        "dry_run": int(bool(payload.get("dry_run", True))),
        "status": str(payload.get("status") or ""),
        "reason": str(payload.get("reason") or ""),
        "account": str(payload.get("account") or ""),
        "code": str(payload.get("code") or ""),
        "side": str(payload.get("side") or ""),
        "quantity": int(payload.get("quantity") or 0),
        "price": int(payload.get("price") or 0),
        "order_amount": int(payload.get("order_amount") or 0),
        "order_type": int(payload.get("order_type") or 0),
        "hoga": str(payload.get("hoga") or ""),
        "tag": str(payload.get("tag") or ""),
        "strategy_name": str(payload.get("strategy_name") or ""),
        "candidate_id": payload.get("candidate_id"),
        "entry_plan_id": payload.get("entry_plan_id"),
        "virtual_order_id": payload.get("virtual_order_id"),
        "virtual_position_id": payload.get("virtual_position_id"),
        "trade_review_id": payload.get("trade_review_id"),
        "leg_index": payload.get("leg_index"),
        "entry_type": str(payload.get("entry_type") or ""),
        "order_phase": str(payload.get("order_phase") or ("exit" if str(payload.get("side") or "") == "sell" else "entry")),
        "exit_decision_id": payload.get("exit_decision_id"),
        "exit_decision_type": str(payload.get("exit_decision_type") or ""),
        "exit_reason": str(payload.get("exit_reason") or ""),
        "exit_percent": payload.get("exit_percent"),
        "exit_quantity": payload.get("exit_quantity"),
        "remaining_quantity": payload.get("remaining_quantity"),
        "position_entry_price": payload.get("position_entry_price"),
        "position_quantity": payload.get("position_quantity"),
        "position_opened_at": str(payload.get("position_opened_at") or ""),
        "position_closed_at": str(payload.get("position_closed_at") or ""),
        "position_max_return_pct": payload.get("position_max_return_pct"),
        "position_max_drawdown_pct": payload.get("position_max_drawdown_pct"),
        "realized_return_pct": payload.get("realized_return_pct"),
        "virtual_exit_price": payload.get("virtual_exit_price"),
        "gate_reason": str(payload.get("gate_reason") or ""),
        "gate_status": str(payload.get("gate_status") or ""),
        "idempotency_key": str(payload.get("idempotency_key") or ""),
        "dedupe_key": str(payload.get("dedupe_key") or ""),
        "duplicate_of": str(payload.get("duplicate_of") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "updated_at": str(payload.get("updated_at") or payload.get("created_at") or ""),
    }
    for key, value in json_fields.items():
        if isinstance(value, str):
            normalized[key] = value or "{}"
        else:
            normalized[key] = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
    return normalized


def _market_side_confirmation_state_params(payload: dict) -> dict:
    now = str(payload.get("updated_at") or payload.get("created_at") or "")
    created_at = str(payload.get("created_at") or now)
    return {
        "trade_date": str(payload.get("trade_date") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "market_side": str(payload.get("market_side") or payload.get("side") or ""),
        "raw_status": str(payload.get("raw_status") or payload.get("current_raw_status") or ""),
        "confirmed_status": str(payload.get("confirmed_status") or ""),
        "previous_confirmed_status": str(payload.get("previous_confirmed_status") or ""),
        "confirmation_pending": int(bool(payload.get("confirmation_pending"))),
        "recovery_pending": int(bool(payload.get("recovery_pending"))),
        "weak_consecutive_cycles": int(payload.get("weak_consecutive_cycles") or 0),
        "risk_off_consecutive_cycles": int(payload.get("risk_off_consecutive_cycles") or 0),
        "healthy_consecutive_cycles": int(payload.get("healthy_consecutive_cycles") or 0),
        "last_breadth_pct": _nullable_float(payload.get("last_breadth_pct")),
        "last_index_return_pct": _nullable_float(payload.get("last_index_return_pct")),
        "last_turnover_weighted_return_pct": _nullable_float(payload.get("last_turnover_weighted_return_pct")),
        "last_source": str(payload.get("last_source") or ""),
        "last_trust_level": str(payload.get("last_trust_level") or ""),
        "last_data_quality_flags_json": json.dumps(payload.get("last_data_quality_flags") or [], ensure_ascii=False, sort_keys=True, default=str),
        "last_reason_codes_json": json.dumps(payload.get("last_reason_codes") or payload.get("reason_codes") or [], ensure_ascii=False, sort_keys=True, default=str),
        "source_conflict": int(bool(payload.get("source_conflict"))),
        "source_conflict_count": int(payload.get("source_conflict_count") or 0),
        "last_source_conflict_at": str(payload.get("last_source_conflict_at") or ""),
        "last_status_changed_at": str(payload.get("last_status_changed_at") or ""),
        "last_confirmed_at": str(payload.get("last_confirmed_at") or ""),
        "last_recovered_at": str(payload.get("last_recovered_at") or ""),
        "wait_started_at": str(payload.get("wait_started_at") or payload.get("market_wait_started_at") or ""),
        "last_cycle_id": str(payload.get("last_cycle_id") or payload.get("cycle_id") or ""),
        "last_evaluated_at": str(payload.get("last_evaluated_at") or ""),
        "updated_at": now,
        "created_at": created_at,
        "expires_at": str(payload.get("expires_at") or ""),
        "state_version": int(payload.get("state_version") or 0),
    }


def _market_side_confirmation_transition_params(payload: dict) -> dict:
    return {
        "trade_date": str(payload.get("trade_date") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "market_side": str(payload.get("market_side") or payload.get("side") or ""),
        "cycle_id": str(payload.get("cycle_id") or ""),
        "previous_raw_status": str(payload.get("previous_raw_status") or ""),
        "new_raw_status": str(payload.get("new_raw_status") or payload.get("current_raw_status") or ""),
        "previous_confirmed_status": str(payload.get("previous_confirmed_status") or ""),
        "new_confirmed_status": str(payload.get("new_confirmed_status") or payload.get("confirmed_status") or ""),
        "previous_confirmation_pending": int(bool(payload.get("previous_confirmation_pending"))),
        "new_confirmation_pending": int(bool(payload.get("new_confirmation_pending") or payload.get("confirmation_pending"))),
        "previous_recovery_pending": int(bool(payload.get("previous_recovery_pending"))),
        "new_recovery_pending": int(bool(payload.get("new_recovery_pending") or payload.get("recovery_pending"))),
        "weak_consecutive_cycles": int(payload.get("weak_consecutive_cycles") or 0),
        "risk_off_consecutive_cycles": int(payload.get("risk_off_consecutive_cycles") or 0),
        "healthy_consecutive_cycles": int(payload.get("healthy_consecutive_cycles") or 0),
        "breadth_pct": _nullable_float(payload.get("breadth_pct") if "breadth_pct" in payload else payload.get("last_breadth_pct")),
        "index_return_pct": _nullable_float(payload.get("index_return_pct") if "index_return_pct" in payload else payload.get("last_index_return_pct")),
        "turnover_weighted_return_pct": _nullable_float(
            payload.get("turnover_weighted_return_pct")
            if "turnover_weighted_return_pct" in payload
            else payload.get("last_turnover_weighted_return_pct")
        ),
        "source": str(payload.get("source") or payload.get("last_source") or ""),
        "trust_level": str(payload.get("trust_level") or payload.get("last_trust_level") or ""),
        "source_conflict": int(bool(payload.get("source_conflict"))),
        "transition_reason_codes_json": json.dumps(payload.get("transition_reason_codes") or payload.get("reason_codes") or [], ensure_ascii=False, sort_keys=True, default=str),
        "transition_type": str(payload.get("transition_type") or ""),
        "created_at": str(payload.get("created_at") or ""),
    }


def _row_to_market_side_confirmation_state(row: sqlite3.Row) -> dict:
    data = {key: row[key] for key in row.keys()}
    data["confirmation_pending"] = bool(data.get("confirmation_pending"))
    data["recovery_pending"] = bool(data.get("recovery_pending"))
    data["source_conflict"] = bool(data.get("source_conflict"))
    data["last_data_quality_flags"] = _safe_json_loads(data.get("last_data_quality_flags_json"), [])
    data["last_reason_codes"] = _safe_json_loads(data.get("last_reason_codes_json"), [])
    return data


def _row_to_market_side_confirmation_transition(row: sqlite3.Row) -> dict:
    data = {key: row[key] for key in row.keys()}
    data["previous_confirmation_pending"] = bool(data.get("previous_confirmation_pending"))
    data["new_confirmation_pending"] = bool(data.get("new_confirmation_pending"))
    data["previous_recovery_pending"] = bool(data.get("previous_recovery_pending"))
    data["new_recovery_pending"] = bool(data.get("new_recovery_pending"))
    data["source_conflict"] = bool(data.get("source_conflict"))
    data["transition_reason_codes"] = _safe_json_loads(data.get("transition_reason_codes_json"), [])
    return data


def _nullable_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_runtime_order_intent(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("dry_run",):
        data[key] = bool(data.get(key))
    for key in ("safety_json", "live_safety_json", "request_json", "response_json", "metadata_json"):
        public_key = key[:-5]
        data[public_key] = _safe_json_loads(data.get(key), {})
    data["live_would_pass"] = bool(data.get("live_safety", {}).get("ok"))
    data["live_reject_reason"] = "" if data["live_would_pass"] else str(data.get("live_safety", {}).get("reason") or "")
    return data


def _live_sim_order_params(payload: dict) -> dict:
    details = payload.get("details") if "details" in payload else payload.get("details_json", {})
    reason_codes = payload.get("reason_codes") if "reason_codes" in payload else payload.get("reason_codes_json", [])
    now = str(payload.get("updated_at") or payload.get("created_at") or "")
    account = str(payload.get("account_id_masked") or payload.get("account") or "")
    return {
        "order_intent_id": str(payload.get("order_intent_id") or payload.get("intent_id") or ""),
        "command_id": str(payload.get("command_id") or ""),
        "entry_plan_id": payload.get("entry_plan_id"),
        "candidate_id": payload.get("candidate_id"),
        "virtual_order_id": payload.get("virtual_order_id"),
        "virtual_position_id": payload.get("virtual_position_id"),
        "exit_decision_id": payload.get("exit_decision_id"),
        "candidate_instance_id": str(payload.get("candidate_instance_id") or ""),
        "trade_date": str(payload.get("trade_date") or ""),
        "code": str(payload.get("code") or ""),
        "name": str(payload.get("name") or ""),
        "account_id_masked": _mask_account(account),
        "order_mode": str(payload.get("order_mode") or "LIVE_SIM"),
        "broker": str(payload.get("broker") or "KIWOOM"),
        "broker_env": str(payload.get("broker_env") or "SIMULATION"),
        "order_leg": int(payload.get("order_leg") or payload.get("leg_index") or 1),
        "side": str(payload.get("side") or ""),
        "order_type": str(payload.get("order_type") or ""),
        "requested_qty": int(payload.get("requested_qty") or payload.get("quantity") or 0),
        "requested_price": int(payload.get("requested_price") or payload.get("price") or 0),
        "submitted_qty": int(payload.get("submitted_qty") or payload.get("quantity") or 0),
        "submitted_price": int(payload.get("submitted_price") or payload.get("price") or 0),
        "broker_order_id": str(payload.get("broker_order_id") or payload.get("order_no") or ""),
        "broker_original_order_id": str(payload.get("broker_original_order_id") or ""),
        "broker_response_code": str(payload.get("broker_response_code") or ""),
        "broker_response_message": str(payload.get("broker_response_message") or ""),
        "order_status": str(payload.get("order_status") or payload.get("status") or "CREATED"),
        "submitted_at": str(payload.get("submitted_at") or ""),
        "accepted_at": str(payload.get("accepted_at") or ""),
        "rejected_at": str(payload.get("rejected_at") or ""),
        "first_fill_at": str(payload.get("first_fill_at") or ""),
        "last_fill_at": str(payload.get("last_fill_at") or ""),
        "cancelled_at": str(payload.get("cancelled_at") or ""),
        "updated_at": now,
        "idempotency_key": str(payload.get("idempotency_key") or ""),
        "dedupe_key": str(payload.get("dedupe_key") or ""),
        "reason_codes_json": reason_codes
        if isinstance(reason_codes, str)
        else json.dumps(list(reason_codes or []), ensure_ascii=False, sort_keys=True, default=str),
        "details_json": details
        if isinstance(details, str)
        else json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True, default=str),
    }


def _row_to_live_sim_order(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["reason_codes"] = _safe_json_loads(data.get("reason_codes_json"), [])
    data["details"] = _safe_json_loads(data.get("details_json"), {})
    return data


def _live_sim_cancel_params(payload: dict) -> dict:
    details = payload.get("details") if "details" in payload else payload.get("details_json", {})
    reason_codes = payload.get("reason_codes") if "reason_codes" in payload else payload.get("reason_codes_json", [])
    now = str(payload.get("updated_at") or payload.get("created_at") or "")
    return {
        "cancel_intent_id": str(payload.get("cancel_intent_id") or ""),
        "original_order_id": str(payload.get("original_order_id") or payload.get("order_intent_id") or ""),
        "broker_order_id": str(payload.get("broker_order_id") or ""),
        "command_id": str(payload.get("command_id") or ""),
        "trade_date": str(payload.get("trade_date") or ""),
        "code": str(payload.get("code") or ""),
        "side": str(payload.get("side") or ""),
        "cancel_qty": int(payload.get("cancel_qty") or 0),
        "cancel_reason": str(payload.get("cancel_reason") or ""),
        "order_mode": str(payload.get("order_mode") or "LIVE_SIM"),
        "account_id_masked": _mask_account(str(payload.get("account_id_masked") or payload.get("account") or "")),
        "candidate_instance_id": str(payload.get("candidate_instance_id") or ""),
        "entry_plan_id": payload.get("entry_plan_id"),
        "idempotency_key": str(payload.get("idempotency_key") or ""),
        "status": str(payload.get("status") or "CREATED"),
        "attempts": int(payload.get("attempts") or 0),
        "created_at": str(payload.get("created_at") or now),
        "submitted_at": str(payload.get("submitted_at") or ""),
        "accepted_at": str(payload.get("accepted_at") or ""),
        "rejected_at": str(payload.get("rejected_at") or ""),
        "updated_at": now,
        "reason_codes_json": reason_codes
        if isinstance(reason_codes, str)
        else json.dumps(list(reason_codes or []), ensure_ascii=False, sort_keys=True, default=str),
        "details_json": details
        if isinstance(details, str)
        else json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True, default=str),
    }


def _row_to_live_sim_cancel(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["reason_codes"] = _safe_json_loads(data.get("reason_codes_json"), [])
    data["details"] = _safe_json_loads(data.get("details_json"), {})
    return data


def _execution_cumulative_fill_qty(event: BrokerExecutionEvent) -> int:
    order_qty = max(0, int(event.quantity or 0))
    remaining_qty = max(0, int(event.remaining_quantity or 0))
    reported_fill_qty = max(0, int(event.filled_quantity or 0))
    if order_qty > 0 and (
        remaining_qty > 0
        or reported_fill_qty > 0
    ):
        return max(0, min(order_qty, order_qty - remaining_qty))
    return reported_fill_qty


def _live_sim_fill_params(payload: dict) -> dict:
    raw_event = payload.get("raw_event_json", payload.get("raw_event", {}))
    fill_qty = int(payload.get("fill_qty") or payload.get("filled_quantity") or 0)
    fill_price = int(payload.get("fill_price") or payload.get("price") or 0)
    return {
        "order_intent_id": str(payload.get("order_intent_id") or ""),
        "broker_order_id": str(payload.get("broker_order_id") or payload.get("order_no") or ""),
        "fill_id": str(payload.get("fill_id") or payload.get("execution_id") or ""),
        "event_id": str(payload.get("event_id") or ""),
        "code": str(payload.get("code") or ""),
        "side": str(payload.get("side") or ""),
        "account_id_masked": _mask_account(str(payload.get("account_id_masked") or payload.get("account") or "")),
        "fill_qty": fill_qty,
        "fill_price": fill_price,
        "cumulative_fill_qty": int(payload.get("cumulative_fill_qty") or 0),
        "remaining_qty": int(payload.get("remaining_qty") or payload.get("remaining_quantity") or 0),
        "fill_amount": int(payload.get("fill_amount") or (fill_qty * max(0, fill_price))),
        "commission": float(payload.get("commission") or 0.0),
        "tax": float(payload.get("tax") or 0.0),
        "event_time": str(payload.get("event_time") or payload.get("timestamp") or ""),
        "received_at": str(payload.get("received_at") or payload.get("event_time") or payload.get("timestamp") or ""),
        "raw_event_json": raw_event
        if isinstance(raw_event, str)
        else json.dumps(dict(raw_event or {}), ensure_ascii=False, sort_keys=True, default=str),
    }


def _row_to_live_sim_fill(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["raw_event"] = _safe_json_loads(data.get("raw_event_json"), {})
    return data


def _live_sim_position_params(payload: dict) -> dict:
    details = payload.get("details") if "details" in payload else payload.get("details_json", {})
    return {
        "position_id": str(payload.get("position_id") or ""),
        "candidate_instance_id": str(payload.get("candidate_instance_id") or ""),
        "code": str(payload.get("code") or ""),
        "name": str(payload.get("name") or ""),
        "account_id_masked": _mask_account(str(payload.get("account_id_masked") or payload.get("account") or "")),
        "order_mode": str(payload.get("order_mode") or "LIVE_SIM"),
        "opened_at": str(payload.get("opened_at") or ""),
        "closed_at": str(payload.get("closed_at") or ""),
        "entry_qty": int(payload.get("entry_qty") or 0),
        "entry_avg_price": int(payload.get("entry_avg_price") or 0),
        "current_qty": int(payload.get("current_qty") or 0),
        "realized_qty": int(payload.get("realized_qty") or 0),
        "realized_pnl": float(payload.get("realized_pnl") or 0.0),
        "realized_pnl_pct": float(payload.get("realized_pnl_pct") or 0.0),
        "unrealized_pnl": float(payload.get("unrealized_pnl") or 0.0),
        "unrealized_pnl_pct": float(payload.get("unrealized_pnl_pct") or 0.0),
        "max_favorable_excursion_pct": float(payload.get("max_favorable_excursion_pct") or 0.0),
        "max_adverse_excursion_pct": float(payload.get("max_adverse_excursion_pct") or 0.0),
        "stop_loss_price": int(payload.get("stop_loss_price") or 0),
        "take_profit_price": int(payload.get("take_profit_price") or 0),
        "max_hold_exit_at": str(payload.get("max_hold_exit_at") or ""),
        "status": str(payload.get("status") or "OPEN"),
        "details_json": details
        if isinstance(details, str)
        else json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True, default=str),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _row_to_live_sim_position(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["details"] = _safe_json_loads(data.get("details_json"), {})
    return data


def _row_to_live_sim_health(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["details"] = _safe_json_loads(data.get("details_json"), {})
    return data


def _live_sim_reconcile_params(payload: dict) -> dict:
    reason_codes = payload.get("reason_codes_json", payload.get("reason_codes", []))
    body = payload.get("payload_json", payload.get("payload", {}))
    return {
        "event_id": str(payload.get("event_id") or ""),
        "trigger": str(payload.get("trigger") or ""),
        "status": str(payload.get("status") or ""),
        "reason": str(payload.get("reason") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "completed_at": str(payload.get("completed_at") or ""),
        "payload_json": body
        if isinstance(body, str)
        else json.dumps(dict(body or {}), ensure_ascii=False, sort_keys=True, default=str),
        "reason_codes_json": reason_codes
        if isinstance(reason_codes, str)
        else json.dumps(list(reason_codes or []), ensure_ascii=False, sort_keys=True, default=str),
    }


def _row_to_live_sim_reconcile(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = _safe_json_loads(data.get("payload_json"), {})
    data["reason_codes"] = _safe_json_loads(data.get("reason_codes_json"), [])
    return data


def _merge_reason_codes(order: dict, additions: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for code in list(order.get("reason_codes") or []) + [str(item) for item in additions if item]:
        if code and code not in merged:
            merged.append(code)
    return merged


def _mask_account(account: str) -> str:
    text = str(account or "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


def _price_from_pct(base_price: int, pct: float) -> int:
    if base_price <= 0:
        return 0
    return int(round(float(base_price) * (1.0 + float(pct or 0.0) / 100.0)))


def _add_minutes(timestamp: str, minutes: int) -> str:
    text = str(timestamp or "")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (parsed + timedelta(minutes=max(0, int(minutes or 0)))).isoformat(timespec="seconds")


def _row_to_dry_run_performance_report(
    row: sqlite3.Row,
    *,
    include_grouped: bool,
    include_items: bool,
) -> dict:
    payload = {
        "id": int(row["id"]),
        "report_id": row["report_id"],
        "trade_date": row["trade_date"],
        "status": row["status"],
        "summary": _safe_json_loads(row["summary_json"], {}),
        "false_signal_summary": _safe_json_loads(row["false_signal_json"], {}),
        "recommendations": _safe_json_loads(row["recommendation_json"], []),
        "filters": _safe_json_loads(row["filters_json"], {}),
        "generated_at": row["generated_at"],
        "created_at": row["created_at"],
    }
    if include_grouped and "grouped_json" in row.keys():
        payload["grouped"] = _safe_json_loads(row["grouped_json"], {})
    if include_items:
        payload["items"] = []
    return payload


def _row_to_dry_run_performance_item(row: sqlite3.Row) -> dict:
    item = _safe_json_loads(row["item_json"], {})
    if not isinstance(item, dict):
        item = {}
    item.setdefault("id", int(row["id"]))
    item.setdefault("report_id", row["report_id"])
    item.setdefault("lifecycle_id", row["lifecycle_id"])
    item.setdefault("trade_date", row["trade_date"])
    item.setdefault("code", row["code"])
    item.setdefault("candidate_id", row["candidate_id"])
    item.setdefault("virtual_order_id", row["virtual_order_id"])
    item.setdefault("virtual_position_id", row["virtual_position_id"])
    item.setdefault("trade_review_id", row["trade_review_id"])
    item.setdefault("entry_intent_id", row["entry_intent_id"])
    item.setdefault("exit_intent_ids", _safe_json_loads(row["exit_intent_ids_json"], []))
    item.setdefault("final_status", row["final_status"])
    item.setdefault("realized_return_pct", row["realized_return_pct"])
    item.setdefault("max_return_20m", row["max_return_20m"])
    item.setdefault("max_drawdown_20m", row["max_drawdown_20m"])
    item.setdefault("dry_run_false_positive_type", row["dry_run_false_positive_type"])
    item.setdefault("dry_run_false_negative_type", row["dry_run_false_negative_type"])
    item.setdefault("quality_bucket", row["quality_bucket"])
    item.setdefault("created_at", row["created_at"])
    return item


def _row_to_dry_run_threshold_ab_report(row: sqlite3.Row, *, include_details: bool) -> dict:
    payload = {
        "id": int(row["id"]),
        "report_id": row["report_id"],
        "trade_date": row["trade_date"],
        "status": row["status"],
        "summary": _safe_json_loads(row["summary_json"], {}),
        "recommendations": _safe_json_loads(row["recommendations_json"], []),
        "filters": _safe_json_loads(row["filters_json"], {}),
        "generated_at": row["generated_at"],
        "created_at": row["created_at"],
    }
    if include_details:
        payload["candidates"] = _safe_json_loads(row["candidates_json"], [])
        payload["scenarios"] = _safe_json_loads(row["scenarios_json"], [])
        payload["results"] = _safe_json_loads(row["results_json"], {})
    return payload


def _row_to_dry_run_threshold_ab_candidate(row: sqlite3.Row) -> dict:
    payload = _safe_json_loads(row["candidate_json"], {})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("id", int(row["id"]))
    payload.setdefault("report_id", row["report_id"])
    payload.setdefault("candidate_id", row["candidate_id"])
    payload.setdefault("category", row["category"])
    payload.setdefault("parameter_name", row["parameter_name"])
    payload.setdefault("label_ko", row["label_ko"])
    payload.setdefault("baseline_value", row["baseline_value"])
    payload.setdefault("candidate_value", row["candidate_value"])
    payload.setdefault("recommendation_grade", row["recommendation_grade"])
    payload.setdefault("expected_net_benefit_score", row["expected_net_benefit_score"])
    payload.setdefault("avoided_false_positive_count", row["avoided_false_positive_count"])
    payload.setdefault("newly_created_false_negative_count", row["newly_created_false_negative_count"])
    payload.setdefault("opportunity_loss_delta", row["opportunity_loss_delta"])
    payload.setdefault("sample_count", row["sample_count"])
    payload.setdefault("confidence", row["confidence"])
    payload.setdefault("created_at", row["created_at"])
    return payload


def _row_to_gateway_transport_latency_sample(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["success"] = bool(data.get("success"))
    data["clock_skew_warning"] = bool(data.get("clock_skew_warning"))
    data["stage_ms"] = _safe_json_loads(data.get("stage_ms_json"), {})
    data["metadata"] = _safe_json_loads(data.get("metadata_json"), {})
    return data


def _row_to_gateway_transport_latency_report(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "report_id": row["report_id"],
        "trade_date": row["trade_date"],
        "transport_mode": row["transport_mode"],
        "experiment_id": row["experiment_id"] if "experiment_id" in row.keys() else "",
        "scenario": row["scenario"] if "scenario" in row.keys() else "",
        "status": row["status"],
        "summary": _safe_json_loads(row["summary_json"], {}),
        "websocket_recommendation": _safe_json_loads(row["recommendation_json"], {}),
        "generated_at": row["generated_at"],
        "created_at": row["created_at"],
    }


def _normalize_operator_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("operator event must be a dict")
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    event_type = str(event.get("event_type") or event.get("type") or "").strip()
    severity = str(event.get("severity") or "").strip().upper()
    category = str(event.get("category") or "").strip().lower()
    message = str(event.get("message_ko") or event.get("message") or "").strip()
    occurred_at = str(event.get("occurred_at") or event.get("created_at") or datetime.now().isoformat(timespec="seconds")).strip()
    if not event_id or not event_type or not severity or not category or not message:
        raise ValueError("operator event missing required fields")
    trade_date = str(event.get("trade_date") or _trade_date_from_timestamp(occurred_at) or datetime.now().date().isoformat()).strip()
    payload = dict(event.get("payload") or event)
    return {
        "event_id": event_id,
        "trade_date": trade_date,
        "occurred_at": occurred_at,
        "received_at": str(event.get("received_at") or datetime.now().isoformat(timespec="seconds")),
        "source": str(event.get("source") or "themelab_dashboard"),
        "event_type": event_type,
        "severity": severity,
        "category": category,
        "symbol": str(event.get("symbol") or "") or None,
        "stock_name": str(event.get("stock_name") or "") or None,
        "primary_theme": str(event.get("primary_theme") or "") or None,
        "stock_role": str(event.get("stock_role") or "") or None,
        "candidate_instance_id": str(event.get("candidate_instance_id") or "") or None,
        "from_status": str(event.get("from_status") or "") or None,
        "to_status": str(event.get("to_status") or "") or None,
        "gate_status": str(event.get("gate_status") or "") or None,
        "display_status": str(event.get("display_status") or "") or None,
        "message_ko": message,
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        "acknowledged_at": str(event.get("acknowledged_at") or "") or None,
        "acknowledged_by": str(event.get("acknowledged_by") or "") or None,
        "hidden": 1 if bool(event.get("hidden")) else 0,
        "snoozed_until": str(event.get("snoozed_until") or "") or None,
    }


def _operator_event_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    payload = _safe_json_loads(data.pop("payload_json", "{}"), {})
    data["payload"] = payload if isinstance(payload, dict) else {}
    data["hidden"] = bool(data.get("hidden"))
    data["acknowledged"] = bool(data.get("acknowledged_at"))
    data["message"] = data.get("message_ko", "")
    data["type"] = data.get("event_type", "")
    return data


def _trade_date_from_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _normalize_operator_action(action: dict) -> dict:
    if not isinstance(action, dict):
        raise ValueError("operator action must be a dict")
    action_id = str(action.get("action_id") or action.get("id") or f"act_{uuid4().hex}").strip()
    action_type = str(action.get("action_type") or "").strip().upper()
    status = str(action.get("status") or "PENDING").strip().upper()
    requested_at = str(action.get("requested_at") or datetime.now().isoformat(timespec="seconds")).strip()
    trade_date = str(action.get("trade_date") or _trade_date_from_timestamp(requested_at) or datetime.now().date().isoformat()).strip()
    if not action_id or not action_type or not status:
        raise ValueError("operator action missing required fields")
    request_payload = action.get("request_payload")
    if request_payload is None:
        request_payload = action.get("request_payload_json")
    response_payload = action.get("response_payload")
    if response_payload is None:
        response_payload = action.get("response_payload_json")
    return {
        "action_id": action_id,
        "trade_date": trade_date,
        "requested_at": requested_at,
        "completed_at": str(action.get("completed_at") or "") or None,
        "action_type": action_type,
        "status": status,
        "source": str(action.get("source") or "themelab_dashboard"),
        "requested_by": str(action.get("requested_by") or "") or None,
        "event_id": str(action.get("event_id") or "") or None,
        "symbol": str(action.get("symbol") or "") or None,
        "stock_name": str(action.get("stock_name") or "") or None,
        "candidate_instance_id": str(action.get("candidate_instance_id") or "") or None,
        "requires_token": 1 if bool(action.get("requires_token")) else 0,
        "confirmation_required": 1 if bool(action.get("confirmation_required", True)) else 0,
        "endpoint": str(action.get("endpoint") or "") or None,
        "request_payload_json": _json_payload(request_payload),
        "response_payload_json": _json_payload(response_payload),
        "error_message": str(action.get("error_message") or "") or None,
    }


def _operator_action_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    request_payload = _safe_json_loads(data.pop("request_payload_json", "{}"), {})
    response_payload = _safe_json_loads(data.pop("response_payload_json", "{}"), {})
    data["request_payload"] = request_payload if isinstance(request_payload, dict) else {}
    data["response_payload"] = response_payload if isinstance(response_payload, dict) else {}
    data["requires_token"] = bool(data.get("requires_token"))
    data["confirmation_required"] = bool(data.get("confirmation_required"))
    return data


def _normalize_postmarket_review_item(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("postmarket review item must be a dict")
    generated_at = str(item.get("generated_at") or datetime.now().isoformat(timespec="seconds"))
    event_id = str(item.get("event_id") or "")
    candidate_instance_id = str(item.get("candidate_instance_id") or "")
    event_type = str(item.get("event_type") or "").upper()
    symbol = str(item.get("symbol") or "")
    review_id = str(item.get("review_id") or "").strip()
    if not review_id:
        review_id = ":".join(
            [
                "postmarket",
                str(item.get("trade_date") or _trade_date_from_timestamp(generated_at) or datetime.now().date().isoformat()),
                str(item.get("review_scope") or "postmarket"),
                event_id or candidate_instance_id or symbol or f"item_{uuid4().hex}",
                event_type or "UNKNOWN",
            ]
        )
    outcome_label = str(item.get("outcome_label") or "").strip().upper()
    confidence = str(item.get("confidence") or "LOW").strip().upper()
    if not outcome_label:
        raise ValueError("postmarket review item missing outcome_label")
    block_reason_codes = item.get("block_reason_codes")
    if block_reason_codes is None:
        block_reason_codes = item.get("block_reason_codes_json")
    return {
        "review_id": review_id,
        "trade_date": str(item.get("trade_date") or _trade_date_from_timestamp(generated_at) or datetime.now().date().isoformat()),
        "generated_at": generated_at,
        "review_scope": str(item.get("review_scope") or "postmarket").lower(),
        "symbol": symbol or None,
        "stock_name": str(item.get("stock_name") or "") or None,
        "primary_theme": str(item.get("primary_theme") or "") or None,
        "stock_role": str(item.get("stock_role") or "") or None,
        "candidate_instance_id": candidate_instance_id or None,
        "event_id": event_id or None,
        "event_type": event_type or None,
        "source_status": str(item.get("source_status") or "") or None,
        "block_reason": str(item.get("block_reason") or "") or None,
        "block_reason_codes_json": _json_list(block_reason_codes),
        "base_time": str(item.get("base_time") or "") or None,
        "base_price": _float_or_none(item.get("base_price")),
        "price_1m": _float_or_none(item.get("price_1m")),
        "price_3m": _float_or_none(item.get("price_3m")),
        "price_5m": _float_or_none(item.get("price_5m")),
        "price_10m": _float_or_none(item.get("price_10m")),
        "price_close_or_last": _float_or_none(item.get("price_close_or_last")),
        "return_1m_pct": _float_or_none(item.get("return_1m_pct")),
        "return_3m_pct": _float_or_none(item.get("return_3m_pct")),
        "return_5m_pct": _float_or_none(item.get("return_5m_pct")),
        "return_10m_pct": _float_or_none(item.get("return_10m_pct")),
        "return_close_or_last_pct": _float_or_none(item.get("return_close_or_last_pct")),
        "outcome_label": outcome_label,
        "confidence": confidence,
        "confidence_reason": str(item.get("confidence_reason") or "") or None,
        "recommendation_ko": str(item.get("recommendation_ko") or "") or None,
        "payload_json": _json_payload(item.get("payload") or item),
    }


def _postmarket_review_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["block_reason_codes"] = _safe_json_loads(data.pop("block_reason_codes_json", "[]"), [])
    payload = _safe_json_loads(data.pop("payload_json", "{}"), {})
    data["payload"] = payload if isinstance(payload, dict) else {}
    return data


def _top_postmarket_items(items: list[dict], outcome_label: str) -> list[dict]:
    filtered = [item for item in items if str(item.get("outcome_label") or "") == outcome_label]
    filtered.sort(key=lambda item: abs(float(item.get("return_5m_pct") or item.get("return_3m_pct") or 0)), reverse=True)
    return [
        {
            "review_id": item.get("review_id"),
            "symbol": item.get("symbol"),
            "stock_name": item.get("stock_name"),
            "event_type": item.get("event_type"),
            "return_3m_pct": item.get("return_3m_pct"),
            "return_5m_pct": item.get("return_5m_pct"),
            "recommendation_ko": item.get("recommendation_ko"),
        }
        for item in filtered[:10]
    ]


def _json_payload(value: object) -> str:
    if isinstance(value, str):
        parsed = _safe_json_loads(value, {})
        value = parsed if isinstance(parsed, (dict, list)) else {}
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_list(value: object) -> str:
    if isinstance(value, str):
        parsed = _safe_json_loads(value, [])
        value = parsed if isinstance(parsed, list) else [value] if value else []
    if value is None:
        value = []
    if not isinstance(value, list):
        value = list(value) if isinstance(value, tuple) else [value]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_json_loads(value: object, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _clean_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""
