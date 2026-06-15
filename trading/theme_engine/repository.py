from __future__ import annotations

import json
import sqlite3
from typing import Optional

from trading.theme_engine.models import (
    CanonicalTheme,
    RelationType,
    SourceTheme,
    ThemeActivitySnapshot,
    ThemeAlias,
    ThemeEvidenceType,
    ThemeMemberEvidence,
    ThemeMembership,
    ThemeRankItem,
    ThemeSourceSyncResult,
    ThemeSourceSyncRun,
    ThemeStatus,
)
from trading.theme_engine.normalizer import normalize_stock_code, normalize_theme_name


class ThemeEngineRepository:
    """SQLite repository for dynamic-only canonical theme data."""

    def __init__(self, db_or_conn) -> None:
        self.conn: sqlite3.Connection = getattr(db_or_conn, "conn", db_or_conn)
        self.conn.row_factory = sqlite3.Row

    def upsert_canonical_theme(self, theme: CanonicalTheme) -> CanonicalTheme:
        status = _enum_value(theme.status)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_themes(
                    theme_id, canonical_name, display_name, theme_group, status,
                    confidence, trade_eligible, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(theme_id) DO UPDATE SET
                    canonical_name=excluded.canonical_name,
                    display_name=excluded.display_name,
                    theme_group=excluded.theme_group,
                    status=excluded.status,
                    confidence=excluded.confidence,
                    trade_eligible=excluded.trade_eligible,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    theme.theme_id,
                    theme.canonical_name,
                    theme.display_name,
                    theme.theme_group,
                    status,
                    float(theme.confidence),
                    int(theme.trade_eligible),
                ),
            )
        saved = self.get_canonical_theme(theme.theme_id)
        if saved is None:
            raise RuntimeError(f"failed to save canonical theme {theme.theme_id}")
        return saved

    def get_canonical_theme(self, theme_id: str) -> Optional[CanonicalTheme]:
        row = self.conn.execute("SELECT * FROM canonical_themes WHERE theme_id = ?", (theme_id,)).fetchone()
        return _row_to_theme(row) if row else None

    def list_canonical_themes(self, status: str | None = None) -> list[CanonicalTheme]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM canonical_themes ORDER BY canonical_name").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM canonical_themes WHERE status = ? ORDER BY canonical_name",
                (_enum_value(status),),
            ).fetchall()
        return [_row_to_theme(row) for row in rows]

    def upsert_alias(self, theme_id: str, alias: str, source: str = "") -> ThemeAlias:
        normalized = normalize_theme_name(alias)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_aliases(theme_id, alias, normalized_alias, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(theme_id, normalized_alias, source) DO UPDATE SET
                    alias=excluded.alias
                """,
                (theme_id, alias, normalized, source),
            )
        row = self.conn.execute(
            """
            SELECT * FROM theme_aliases
            WHERE theme_id = ? AND normalized_alias = ? AND source = ?
            """,
            (theme_id, normalized, source),
        ).fetchone()
        return _row_to_alias(row)

    def find_alias(self, normalized_alias: str) -> Optional[str]:
        normalized = normalize_theme_name(normalized_alias)
        row = self.conn.execute(
            """
            SELECT theme_id FROM theme_aliases
            WHERE normalized_alias = ?
            ORDER BY CASE WHEN source = '' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        return str(row["theme_id"]) if row else None

    def upsert_source_theme(self, source_theme: SourceTheme) -> SourceTheme:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO source_theme_catalog(
                    source, source_theme_id, source_theme_name, normalized_name,
                    matched_theme_id, match_confidence, raw_payload_hash,
                    last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(source, source_theme_id, normalized_name) DO UPDATE SET
                    source_theme_name=excluded.source_theme_name,
                    matched_theme_id=excluded.matched_theme_id,
                    match_confidence=excluded.match_confidence,
                    raw_payload_hash=excluded.raw_payload_hash,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    source_theme.source,
                    source_theme.source_theme_id,
                    source_theme.source_theme_name,
                    source_theme.normalized_name,
                    source_theme.matched_theme_id,
                    float(source_theme.match_confidence),
                    source_theme.raw_payload_hash,
                ),
            )
        row = self.conn.execute(
            """
            SELECT * FROM source_theme_catalog
            WHERE source = ? AND source_theme_id = ? AND normalized_name = ?
            """,
            (source_theme.source, source_theme.source_theme_id, source_theme.normalized_name),
        ).fetchone()
        return _row_to_source_theme(row)

    def add_member_evidence(self, evidence: ThemeMemberEvidence) -> ThemeMemberEvidence:
        evidence.stock_code = normalize_stock_code(evidence.stock_code)
        relation = _enum_value(evidence.relation_type)
        evidence_type = _enum_value(evidence.evidence_type)
        existing = self.conn.execute(
            """
            SELECT * FROM theme_member_evidence
            WHERE theme_id = ? AND stock_code = ? AND source = ?
              AND evidence_type = ? AND relation_type = ? AND reason = ?
            ORDER BY id LIMIT 1
            """,
            (
                evidence.theme_id,
                evidence.stock_code,
                evidence.source,
                evidence_type,
                relation,
                evidence.reason,
            ),
        ).fetchone()
        with self.conn:
            if existing:
                self.conn.execute(
                    """
                    UPDATE theme_member_evidence
                    SET stock_name = COALESCE(NULLIF(?, ''), stock_name),
                        confidence = MAX(confidence, ?),
                        last_seen_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (evidence.stock_name, float(evidence.confidence), int(existing["id"])),
                )
                evidence_id = int(existing["id"])
            else:
                cursor = self.conn.execute(
                    """
                    INSERT INTO theme_member_evidence(
                        theme_id, stock_code, stock_name, source, evidence_type,
                        relation_type, reason, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.theme_id,
                        evidence.stock_code,
                        evidence.stock_name,
                        evidence.source,
                        evidence_type,
                        relation,
                        evidence.reason,
                        float(evidence.confidence),
                    ),
                )
                evidence_id = int(cursor.lastrowid)
        row = self.conn.execute("SELECT * FROM theme_member_evidence WHERE id = ?", (evidence_id,)).fetchone()
        return _row_to_evidence(row)

    def list_member_evidence(self, theme_id: str, stock_code: str | None = None) -> list[ThemeMemberEvidence]:
        params: list[object] = [theme_id]
        query = "SELECT * FROM theme_member_evidence WHERE theme_id = ?"
        if stock_code is not None:
            query += " AND stock_code = ?"
            params.append(normalize_stock_code(stock_code))
        query += " ORDER BY stock_code, source, id"
        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_evidence(row) for row in rows]

    def list_all_member_evidence(self) -> list[ThemeMemberEvidence]:
        rows = self.conn.execute("SELECT * FROM theme_member_evidence ORDER BY theme_id, stock_code, id").fetchall()
        return [_row_to_evidence(row) for row in rows]

    def upsert_current_membership(self, membership: ThemeMembership) -> ThemeMembership:
        membership.stock_code = normalize_stock_code(membership.stock_code)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_membership_current(
                    theme_id, stock_code, stock_name, membership_score, relation_type,
                    source_count, active, trade_eligible, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(theme_id, stock_code) DO UPDATE SET
                    stock_name=excluded.stock_name,
                    membership_score=excluded.membership_score,
                    relation_type=excluded.relation_type,
                    source_count=excluded.source_count,
                    active=excluded.active,
                    trade_eligible=excluded.trade_eligible,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    membership.theme_id,
                    membership.stock_code,
                    membership.stock_name,
                    float(membership.membership_score),
                    _enum_value(membership.relation_type),
                    int(membership.source_count),
                    int(membership.active),
                    int(membership.trade_eligible),
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM theme_membership_current WHERE theme_id = ? AND stock_code = ?",
            (membership.theme_id, membership.stock_code),
        ).fetchone()
        return _row_to_membership(row)

    def delete_current_memberships_for_theme(self, theme_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM theme_membership_current WHERE theme_id = ?", (theme_id,))

    def purge_sources(self, sources: list[str] | tuple[str, ...] | set[str]) -> dict[str, int]:
        source_names = sorted({str(source or "").strip() for source in sources if str(source or "").strip()})
        if not source_names:
            return {
                "source_count": 0,
                "affected_theme_count": 0,
                "evidence_deleted": 0,
                "catalog_deleted": 0,
                "alias_deleted": 0,
                "membership_deleted": 0,
            }
        placeholders = ",".join("?" for _ in source_names)
        affected_rows = self.conn.execute(
            f"SELECT DISTINCT theme_id FROM theme_member_evidence WHERE source IN ({placeholders})",
            tuple(source_names),
        ).fetchall()
        affected_theme_ids = sorted({str(row["theme_id"]) for row in affected_rows if str(row["theme_id"] or "")})
        with self.conn:
            evidence_deleted = self.conn.execute(
                f"DELETE FROM theme_member_evidence WHERE source IN ({placeholders})",
                tuple(source_names),
            ).rowcount
            catalog_deleted = self.conn.execute(
                f"DELETE FROM source_theme_catalog WHERE source IN ({placeholders})",
                tuple(source_names),
            ).rowcount
            alias_deleted = self.conn.execute(
                f"DELETE FROM theme_aliases WHERE source IN ({placeholders})",
                tuple(source_names),
            ).rowcount
            membership_deleted = 0
            if affected_theme_ids:
                theme_placeholders = ",".join("?" for _ in affected_theme_ids)
                membership_deleted = self.conn.execute(
                    f"DELETE FROM theme_membership_current WHERE theme_id IN ({theme_placeholders})",
                    tuple(affected_theme_ids),
                ).rowcount
                self.conn.execute(
                    f"""
                    UPDATE canonical_themes
                    SET status = ?, trade_eligible = 0, confidence = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE theme_id IN ({theme_placeholders})
                      AND theme_id NOT IN (SELECT DISTINCT theme_id FROM theme_member_evidence)
                    """,
                    (ThemeStatus.STALE.value, *affected_theme_ids),
                )
        return {
            "source_count": len(source_names),
            "affected_theme_count": len(affected_theme_ids),
            "evidence_deleted": int(evidence_deleted),
            "catalog_deleted": int(catalog_deleted),
            "alias_deleted": int(alias_deleted),
            "membership_deleted": int(membership_deleted),
        }

    def get_members_by_theme(self, theme_id: str, active: bool = True) -> list[ThemeMembership]:
        query = "SELECT * FROM theme_membership_current WHERE theme_id = ?"
        params: list[object] = [theme_id]
        if active:
            query += " AND active = 1"
        query += " ORDER BY membership_score DESC, stock_code"
        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_membership(row) for row in rows]

    def list_members_by_theme_ids(
        self,
        theme_ids: list[str] | tuple[str, ...],
        *,
        active: bool = True,
    ) -> dict[str, list[ThemeMembership]]:
        ids = [str(theme_id or "").strip() for theme_id in theme_ids if str(theme_id or "").strip()]
        if not ids:
            return {}
        seen: set[str] = set()
        ordered_ids = []
        for theme_id in ids:
            if theme_id in seen:
                continue
            seen.add(theme_id)
            ordered_ids.append(theme_id)
        placeholders = ",".join("?" for _ in ordered_ids)
        query = f"SELECT * FROM theme_membership_current WHERE theme_id IN ({placeholders})"
        params: list[object] = list(ordered_ids)
        if active:
            query += " AND active = 1"
        query += " ORDER BY theme_id, membership_score DESC, stock_code"
        rows = self.conn.execute(query, params).fetchall()
        grouped: dict[str, list[ThemeMembership]] = {theme_id: [] for theme_id in ordered_ids}
        for row in rows:
            membership = _row_to_membership(row)
            grouped.setdefault(membership.theme_id, []).append(membership)
        return grouped

    def theme_input_signature(
        self,
        *,
        statuses: list[str] | tuple[str, ...] | set[str] | None = None,
        active: bool = True,
    ) -> tuple[int, int, str, str]:
        status_values = [_enum_value(status) for status in (statuses or []) if _enum_value(status)]
        theme_where = ""
        theme_params: list[object] = []
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            theme_where = f"WHERE status IN ({placeholders})"
            theme_params.extend(status_values)
        theme_row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS theme_count,
                   COALESCE(MAX(updated_at), '') AS max_theme_updated_at
            FROM canonical_themes
            {theme_where}
            """,
            theme_params,
        ).fetchone()
        member_where = []
        member_params: list[object] = []
        if active:
            member_where.append("m.active = 1")
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            member_where.append(f"t.status IN ({placeholders})")
            member_params.extend(status_values)
        member_clause = "WHERE " + " AND ".join(member_where) if member_where else ""
        member_row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS member_count,
                   COALESCE(MAX(m.updated_at), '') AS max_member_updated_at
            FROM theme_membership_current m
            JOIN canonical_themes t ON t.theme_id = m.theme_id
            {member_clause}
            """,
            member_params,
        ).fetchone()
        return (
            int(theme_row["theme_count"] or 0) if theme_row else 0,
            int(member_row["member_count"] or 0) if member_row else 0,
            str(theme_row["max_theme_updated_at"] or "") if theme_row else "",
            str(member_row["max_member_updated_at"] or "") if member_row else "",
        )

    def get_themes_by_stock(self, stock_code: str, active: bool = True) -> list[ThemeMembership]:
        query = "SELECT * FROM theme_membership_current WHERE stock_code = ?"
        params: list[object] = [normalize_stock_code(stock_code)]
        if active:
            query += " AND active = 1"
        query += " ORDER BY trade_eligible DESC, membership_score DESC, theme_id"
        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_membership(row) for row in rows]

    def list_current_memberships(
        self,
        *,
        active: bool | None = None,
        trade_eligible: bool | None = None,
    ) -> list[ThemeMembership]:
        query = "SELECT * FROM theme_membership_current"
        clauses = []
        params: list[object] = []
        if active is not None:
            clauses.append("active = ?")
            params.append(int(active))
        if trade_eligible is not None:
            clauses.append("trade_eligible = ?")
            params.append(int(trade_eligible))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY trade_eligible DESC, membership_score DESC, source_count DESC, stock_code"
        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_membership(row) for row in rows]

    def save_activity_snapshot(self, snapshot: ThemeActivitySnapshot) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO theme_activity_snapshots(
                    theme_id, theme_name, theme_score, rank, rank_delta_1m,
                    rank_delta_5m, weighted_return_pct, turnover, turnover_strength,
                    breadth, rising_count, falling_count, total_count, leader_code,
                    leader_name, leader_return_pct, leader_turnover, leader_gap,
                    top3_concentration, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.theme_id,
                    snapshot.theme_name,
                    float(snapshot.theme_score),
                    int(snapshot.rank),
                    int(snapshot.rank_delta_1m),
                    int(snapshot.rank_delta_5m),
                    float(snapshot.weighted_return_pct),
                    float(snapshot.turnover),
                    float(snapshot.turnover_strength),
                    float(snapshot.breadth),
                    int(snapshot.rising_count),
                    int(snapshot.falling_count),
                    int(snapshot.total_count),
                    snapshot.leader_code,
                    snapshot.leader_name,
                    float(snapshot.leader_return_pct),
                    float(snapshot.leader_turnover),
                    float(snapshot.leader_gap),
                    float(snapshot.top3_concentration),
                    json.dumps(snapshot.details, ensure_ascii=False),
                ),
            )

    def latest_activity_snapshots(self, limit: int = 100) -> list[ThemeActivitySnapshot]:
        rows = self.conn.execute(
            """
            SELECT * FROM theme_activity_snapshots
            ORDER BY created_at DESC, rank ASC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [_row_to_activity(row) for row in rows]

    def get_latest_theme_rank(self, top_n: int = 20) -> list[ThemeRankItem]:
        rows = self.conn.execute(
            """
            WITH latest AS (
                SELECT theme_id, MAX(id) AS latest_id
                FROM theme_activity_snapshots
                GROUP BY theme_id
            )
            SELECT s.*, c.status, c.trade_eligible
            FROM theme_activity_snapshots s
            JOIN latest l ON l.latest_id = s.id
            LEFT JOIN canonical_themes c ON c.theme_id = s.theme_id
            ORDER BY s.rank ASC, s.theme_score DESC
            LIMIT ?
            """,
            (int(top_n),),
        ).fetchall()
        return [_row_to_rank_item(row) for row in rows]

    def count_current_memberships(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM theme_membership_current").fetchone()
        return int(row["count"]) if row else 0

    def save_source_sync_run(self, result: ThemeSourceSyncResult) -> ThemeSourceSyncRun:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO theme_source_sync_runs(
                    source, started_at, finished_at, status, theme_count,
                    member_count, error_count, message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.source,
                    result.started_at,
                    result.finished_at,
                    result.status,
                    int(result.theme_count),
                    int(result.member_count),
                    int(result.error_count),
                    result.message,
                    json.dumps(result.details, ensure_ascii=False),
                ),
            )
        row = self.conn.execute("SELECT * FROM theme_source_sync_runs WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return _row_to_sync_run(row)

    def latest_source_sync_runs(self, limit: int = 20) -> list[ThemeSourceSyncRun]:
        rows = self.conn.execute(
            """
            SELECT * FROM theme_source_sync_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [_row_to_sync_run(row) for row in rows]

    def latest_source_sync_run(self, source: str | None = None) -> ThemeSourceSyncRun | None:
        if source is None:
            row = self.conn.execute(
                "SELECT * FROM theme_source_sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM theme_source_sync_runs WHERE source = ? ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
        return _row_to_sync_run(row) if row else None


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _row_to_theme(row: sqlite3.Row) -> CanonicalTheme:
    return CanonicalTheme(
        theme_id=row["theme_id"],
        canonical_name=row["canonical_name"],
        display_name=row["display_name"],
        theme_group=row["theme_group"],
        status=ThemeStatus(row["status"]),
        confidence=float(row["confidence"]),
        trade_eligible=bool(row["trade_eligible"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        updated_at=row["updated_at"],
    )


def _row_to_alias(row: sqlite3.Row) -> ThemeAlias:
    return ThemeAlias(
        id=int(row["id"]),
        theme_id=row["theme_id"],
        alias=row["alias"],
        normalized_alias=row["normalized_alias"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _row_to_source_theme(row: sqlite3.Row) -> SourceTheme:
    return SourceTheme(
        id=int(row["id"]),
        source=row["source"],
        source_theme_id=row["source_theme_id"],
        source_theme_name=row["source_theme_name"],
        normalized_name=row["normalized_name"],
        matched_theme_id=row["matched_theme_id"],
        match_confidence=float(row["match_confidence"]),
        raw_payload_hash=row["raw_payload_hash"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        updated_at=row["updated_at"],
    )


def _row_to_evidence(row: sqlite3.Row) -> ThemeMemberEvidence:
    return ThemeMemberEvidence(
        id=int(row["id"]),
        theme_id=row["theme_id"],
        stock_code=row["stock_code"],
        stock_name=row["stock_name"],
        source=row["source"],
        evidence_type=ThemeEvidenceType(row["evidence_type"]),
        relation_type=RelationType(row["relation_type"]),
        reason=row["reason"],
        confidence=float(row["confidence"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        updated_at=row["updated_at"],
    )


def _row_to_membership(row: sqlite3.Row) -> ThemeMembership:
    return ThemeMembership(
        theme_id=row["theme_id"],
        stock_code=row["stock_code"],
        stock_name=row["stock_name"],
        membership_score=float(row["membership_score"]),
        relation_type=RelationType(row["relation_type"]),
        source_count=int(row["source_count"]),
        active=bool(row["active"]),
        trade_eligible=bool(row["trade_eligible"]),
        updated_at=row["updated_at"],
    )


def _row_to_activity(row: sqlite3.Row) -> ThemeActivitySnapshot:
    return ThemeActivitySnapshot(
        id=int(row["id"]),
        created_at=row["created_at"],
        theme_id=row["theme_id"],
        theme_name=row["theme_name"],
        theme_score=float(row["theme_score"]),
        rank=int(row["rank"]),
        rank_delta_1m=int(row["rank_delta_1m"]),
        rank_delta_5m=int(row["rank_delta_5m"]),
        weighted_return_pct=float(row["weighted_return_pct"]),
        turnover=float(row["turnover"]),
        turnover_strength=float(row["turnover_strength"]),
        breadth=float(row["breadth"]),
        rising_count=int(row["rising_count"]),
        falling_count=int(row["falling_count"]),
        total_count=int(row["total_count"]),
        leader_code=row["leader_code"],
        leader_name=row["leader_name"],
        leader_return_pct=float(row["leader_return_pct"]),
        leader_turnover=float(row["leader_turnover"]),
        leader_gap=float(row["leader_gap"]),
        top3_concentration=float(row["top3_concentration"]),
        details=dict(json.loads(row["details_json"] or "{}")),
    )


def _row_to_rank_item(row: sqlite3.Row) -> ThemeRankItem:
    details = dict(json.loads(row["details_json"] or "{}"))
    return ThemeRankItem(
        rank=int(row["rank"]),
        theme_id=row["theme_id"],
        theme_name=row["theme_name"],
        theme_score=float(row["theme_score"]),
        status=ThemeStatus(row["status"] or ThemeStatus.CANDIDATE.value),
        trade_eligible=bool(row["trade_eligible"]),
        rank_delta_1m=int(row["rank_delta_1m"]),
        rank_delta_5m=int(row["rank_delta_5m"]),
        weighted_return_pct=float(row["weighted_return_pct"]),
        turnover=float(row["turnover"]),
        turnover_strength=float(row["turnover_strength"]),
        breadth=float(row["breadth"]),
        rising_count=int(row["rising_count"]),
        total_count=int(row["total_count"]),
        leader_code=row["leader_code"],
        leader_name=row["leader_name"],
        leader_return_pct=float(row["leader_return_pct"]),
        leader_gap=float(row["leader_gap"]),
        top3_concentration=float(row["top3_concentration"]),
        details=details,
    )


def _row_to_sync_run(row: sqlite3.Row) -> ThemeSourceSyncRun:
    return ThemeSourceSyncRun(
        id=int(row["id"]),
        source=row["source"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        theme_count=int(row["theme_count"]),
        member_count=int(row["member_count"]),
        error_count=int(row["error_count"]),
        message=row["message"],
        details=dict(json.loads(row["details_json"] or "{}")),
    )
