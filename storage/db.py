from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Union

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
        self.conn.execute(
            "INSERT INTO order_results(ok, result_code, message, request_json) VALUES (?, ?, ?, ?)",
            (
                int(result.ok),
                result.code,
                result.message,
                json.dumps(result.request.to_dict(), ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def save_execution(self, event: BrokerExecutionEvent) -> None:
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
                    payload_size_bytes, total_wall_ms,
                    gateway_queue_wait_ms, gateway_post_ms, core_receive_ms,
                    core_persist_ms, core_dispatch_wait_ms, long_poll_wait_ms,
                    gateway_receive_wait_ms, gateway_local_queue_wait_ms,
                    rate_limit_wait_ms, gateway_execute_ms, ack_round_trip_ms,
                    ws_send_ms, ws_receive_ms, ws_reconnect_count, ws_message_sequence,
                    clock_skew_warning, stage_ms_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                    success=excluded.success,
                    error=excluded.error,
                    experiment_id=excluded.experiment_id,
                    scenario=excluded.scenario,
                    connection_id=excluded.connection_id,
                    websocket_session_id=excluded.websocket_session_id,
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

    def list_gateway_transport_experiments(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                experiment_id,
                scenario,
                GROUP_CONCAT(DISTINCT transport_mode) AS transport_modes,
                COUNT(*) AS sample_count,
                MIN(created_at) AS started_at,
                MAX(created_at) AS ended_at
            FROM gateway_transport_latency_samples
            WHERE experiment_id != ''
            GROUP BY experiment_id, scenario
            ORDER BY ended_at DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit or 50)), max(0, int(offset or 0))),
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


def _safe_json_loads(value: object, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default
