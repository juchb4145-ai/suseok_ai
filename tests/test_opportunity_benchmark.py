from datetime import datetime

from fastapi.testclient import TestClient

import trading_app.api as api
from storage.db import TradingDatabase
from trading.strategy.opportunity_benchmark import OpportunityBenchmarkConfig, OpportunityBenchmarkService


TRADE_DATE = "2026-06-22"


def test_benchmark_uses_opt10032_without_candidate_or_theme_sources(tmp_path):
    db = TradingDatabase(str(tmp_path / "benchmark.db"))
    _seed_opening_batch(db, "09:00", [_row("000001", 1, price=1000, turnover=10_000_000)])

    report = OpportunityBenchmarkService(db, config=_config()).build_report(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T09:01:00",
        persist=True,
    )

    assert report["source_batch_count"] == 1
    assert report["observation_count"] == 1
    assert report["episode_count"] == 1
    assert report["candidate_not_captured_episode_count"] == 1
    assert report["candidate_capture_rate"]["numerator"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM gateway_commands").fetchone()[0] == 0


def test_intraday_raw_price_recovery_and_episode_reentry_gap(tmp_path):
    db = TradingDatabase(str(tmp_path / "reentry.db"))
    _seed_intraday_batch(db, "09:20", "MORNING", [_row("000001", 1, price=1010, turnover=10_000_000, parsed_price=False)])
    _seed_intraday_batch(db, "09:30", "MORNING", [_row("000001", 2, price=1020, turnover=11_000_000, parsed_price=False)])
    _seed_intraday_batch(db, "10:00", "MORNING", [_row("000001", 1, price=1030, turnover=12_000_000, parsed_price=False)])

    report = OpportunityBenchmarkService(db, config=_config(reentry_gap_sec=1200)).build_report(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T10:01:00",
        persist=True,
    )

    assert report["observation_count"] == 3
    assert {item["price_source"] for item in report["observations"]} == {"OPT10032_RAW_RECOVERED"}
    assert report["episode_count"] == 2
    assert sorted(item["generation_seq"] for item in report["episodes"]) == [1, 2]


def test_candidate_link_windows_are_instance_based(tmp_path):
    db = TradingDatabase(str(tmp_path / "links.db"))
    codes = ["000001", "000002", "000003", "000004", "000005", "000006"]
    _seed_opening_batch(db, "09:00", [_row(code, index, price=1000 + index, turnover=10_000_000 + index) for index, code in enumerate(codes, start=1)])
    for code, ci, first_seen in [
        ("000001", "ci-pre", "2026-06-22T08:59:30"),
        ("000002", "ci-1m", "2026-06-22T09:00:30"),
        ("000003", "ci-5m", "2026-06-22T09:03:00"),
        ("000004", "ci-15m", "2026-06-22T09:10:00"),
        ("000005", "ci-after", "2026-06-22T09:20:00"),
    ]:
        _seed_candidate_episode(db, code, ci, first_seen)

    report = OpportunityBenchmarkService(db, config=_config()).build_report(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T09:21:00",
        persist=True,
    )
    windows = {item["detection_window"] for item in report["candidate_links"]}

    assert {"PREEXISTING", "WITHIN_1M", "WITHIN_5M", "WITHIN_15M", "AFTER_15M", "NOT_CAPTURED"} <= windows
    assert report["strict_link_count"] == 5
    assert report["candidate_captured_episode_count"] == 5
    assert report["candidate_not_captured_episode_count"] == 1


def test_outcome_pending_until_horizon_and_uses_only_cutoff_data(tmp_path):
    db = TradingDatabase(str(tmp_path / "outcome.db"))
    _seed_opening_batch(db, "09:00", [_row("000001", 1, price=1000, turnover=10_000_000)])
    _seed_intraday_batch(db, "09:05", "MORNING", [_row("000001", 1, price=1100, turnover=11_000_000, parsed_price=False)])
    _seed_intraday_batch(db, "09:15", "MORNING", [_row("000001", 1, price=900, turnover=12_000_000, parsed_price=False)])

    pending = OpportunityBenchmarkService(db, config=_config(horizons_min=(5,))).build_report(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T09:04:59",
        source_cutoff_at="2026-06-22T09:04:59",
        persist=False,
    )
    complete = OpportunityBenchmarkService(db, config=_config(horizons_min=(5,))).build_report(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T09:06:00",
        source_cutoff_at="2026-06-22T09:06:00",
        persist=False,
    )

    assert pending["outcomes"][0]["label_status"] == "PENDING"
    assert complete["outcomes"][0]["label_status"] == "COMPLETE"
    assert complete["outcomes"][0]["return_pct"] == 10.0
    assert complete["outcomes"][0]["mae_pct"] == 0.0
    assert all(item["observed_at"] <= "2026-06-22T09:06:00" for item in complete["observations"])


def test_opportunity_benchmark_api_rebuild_requires_token_and_filters(monkeypatch, tmp_path):
    db_path = tmp_path / "api.db"
    db = TradingDatabase(str(db_path))
    _seed_opening_batch(db, "09:00", [_row("000001", 1, price=1000, turnover=10_000_000)])
    OpportunityBenchmarkService(db, config=_config()).build_report(trade_date=TRADE_DATE, as_of="2026-06-22T09:01:00", persist=True)
    db.close()
    monkeypatch.setenv("TRADING_CORE_TOKEN", "secret")
    monkeypatch.setattr(api, "open_database", lambda: TradingDatabase(str(db_path)))
    monkeypatch.setattr(api, "close_database", lambda db: db.close())
    monkeypatch.setattr(api.runtime_supervisor, "snapshot", lambda: {"strategy_baseline": _baseline()})
    client = TestClient(api.app)

    denied = client.post("/api/ops/opportunity-benchmark/rebuild", json={"rebuild_reason": "test"})
    allowed = client.post(
        "/api/ops/opportunity-benchmark/rebuild?trade_date=2026-06-22",
        json={"rebuild_reason": "test", "source_cutoff_at": "2026-06-22T09:01:00"},
        headers={"X-Local-Token": "secret"},
    )
    episodes = client.get("/api/ops/opportunity-benchmark/episodes?trade_date=2026-06-22&candidate_capture_status=NOT_CAPTURED").json()
    missing = client.get("/api/ops/opportunity-benchmark/episodes/missing")

    assert denied.status_code in {401, 403}
    assert allowed.status_code == 200
    assert allowed.json()["analysis_only"] is True
    assert allowed.json()["gateway_command_created"] is False
    assert episodes["items"][0]["candidate_capture_status"] == "NOT_CAPTURED"
    assert missing.status_code == 404


def _config(**overrides):
    data = {
        "enabled": True,
        "sources": ("OPENING_OPT10032", "INTRADAY_OPT10032"),
        "max_rank": 100,
        "reentry_gap_sec": 1200,
        "horizons_min": (5, 15, 25, 60),
    }
    data.update(overrides)
    return OpportunityBenchmarkConfig(**data)


def _seed_opening_batch(db, batch_time, rows):
    db.save_opening_turnover_seed_batch(
        {
            "trade_date": TRADE_DATE,
            "batch_time": batch_time,
            "command_id": f"cmd-open-{batch_time}",
            "row_count": len(rows),
            "parsed_count": len(rows),
            "parser_status": "OK",
            "raw": {
                "ack_payload": {
                    "idempotency_key": f"opening_burst:seed:{TRADE_DATE}:{batch_time.replace(':', '')}",
                    "inputs": {"시장구분": "000", "관리종목포함": "0", "거래소구분": "3"},
                    "top_n": 100,
                },
                "source_row_count": len(rows),
                "stored_row_count": len(rows),
            },
            "rows": rows,
        }
    )


def _seed_intraday_batch(db, bucket, phase, rows):
    db.save_intraday_theme_discovery_batch(
        {
            "trade_date": TRADE_DATE,
            "observed_at": f"{TRADE_DATE}T{bucket}:00",
            "session_phase": phase,
            "bucket": bucket,
            "command_id": f"cmd-intra-{bucket}",
            "idempotency_key": f"intraday_theme_discovery:seed:{TRADE_DATE}:{phase}:{bucket}:hash",
            "status": "OK",
            "parser_status": "OK",
            "row_count": len(rows),
            "parsed_count": len(rows),
            "rows": rows,
        }
    )


def _seed_candidate_episode(db, code, instance_id, first_seen):
    db.save_candidate_funnel_episodes(
        [
            {
                "schema_version": "candidate_funnel_episode.v1",
                "trade_date": TRADE_DATE,
                "candidate_instance_id": instance_id,
                "candidate_id": None,
                "candidate_generation_seq": 1,
                "code": code,
                "name": f"종목{code}",
                "first_seen_at": first_seen,
                "last_seen_at": first_seen,
                "max_stage_ordinal": 12,
                "current_stage": "CHAMPION_VALID_OBSERVE",
                "reached_stages": ["SOURCE_DETECTED", "CANDIDATE_CREATED", "CHAMPION_VALID_OBSERVE"],
                "stage_first_reached_at": {"SOURCE_DETECTED": first_seen, "CANDIDATE_CREATED": first_seen, "CHAMPION_VALID_OBSERVE": first_seen},
                "attribution_confidence": "HIGH",
                "baseline_role": "CHAMPION",
                "fingerprint": f"{instance_id}:fp",
                "updated_at": first_seen,
            }
        ]
    )


def _row(code, rank, *, price, turnover, parsed_price=True):
    raw = {
        "종목코드": f"A{code}",
        "종목명": f"종목{code}",
        "순위": str(rank),
        "거래대금": str(turnover),
        "등락률": "1.5",
        "현재가": f"-{price}",
        "현재거래량": "1000",
    }
    return {
        "stock_code": code,
        "stock_name": f"종목{code}",
        "rank": rank,
        "turnover_krw": turnover,
        "current_turnover_krw": turnover,
        "change_rate_pct": 1.5,
        "current_price": price if parsed_price else 0,
        "volume": 1000 if parsed_price else 0,
        "parser_status": "OK",
        "raw": raw,
    }


def _baseline():
    return {
        "baseline_id": "leader_first_pullback_v1",
        "baseline_version": "1.0.0",
        "config_hash": "hash-clean",
        "git_sha": "abcdef",
    }
