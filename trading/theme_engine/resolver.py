from __future__ import annotations

import hashlib
import json

from trading.theme_engine.models import CanonicalTheme, SourceTheme, ThemeStatus
from trading.theme_engine.normalizer import normalize_theme_name, suggest_theme_id
from trading.theme_engine.repository import ThemeEngineRepository


class ThemeCanonicalResolver:
    def __init__(self, repository: ThemeEngineRepository) -> None:
        self.repository = repository

    def match_or_create_theme(
        self,
        source: str,
        source_theme_name: str,
        source_theme_id: str = "",
    ) -> CanonicalTheme:
        normalized = normalize_theme_name(source_theme_name)
        theme_id = self.resolve_alias(source_theme_name)
        confidence = 1.0 if theme_id else 0.7
        if theme_id is None:
            theme_id = suggest_theme_id(source_theme_name)
            suffix = 2
            while True:
                existing = self.repository.get_canonical_theme(theme_id)
                if existing is None or normalize_theme_name(existing.canonical_name) == normalized:
                    break
                theme_id = f"{suggest_theme_id(source_theme_name)}_{suffix}"
                suffix += 1
            theme = CanonicalTheme(
                theme_id=theme_id,
                canonical_name=source_theme_name,
                display_name=source_theme_name,
                status=ThemeStatus.CANDIDATE,
                confidence=confidence,
                trade_eligible=False,
            )
            theme = self.repository.upsert_canonical_theme(theme)
            self.add_alias(theme.theme_id, source_theme_name, source="")
        else:
            theme = self.repository.get_canonical_theme(theme_id)
            if theme is None:
                theme = self.repository.upsert_canonical_theme(
                    CanonicalTheme(
                        theme_id=theme_id,
                        canonical_name=source_theme_name,
                        display_name=source_theme_name,
                        status=ThemeStatus.CANDIDATE,
                        confidence=confidence,
                    )
                )
        raw_hash = hashlib.sha1(
            json.dumps(
                {"source": source, "source_theme_id": source_theme_id, "source_theme_name": source_theme_name},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.repository.upsert_source_theme(
            SourceTheme(
                source=source,
                source_theme_id=source_theme_id,
                source_theme_name=source_theme_name,
                normalized_name=normalized,
                matched_theme_id=theme.theme_id,
                match_confidence=confidence,
                raw_payload_hash=raw_hash,
            )
        )
        self.add_alias(theme.theme_id, source_theme_name, source=source)
        return theme

    def resolve_alias(self, name: str) -> str | None:
        return self.repository.find_alias(normalize_theme_name(name))

    def add_alias(self, theme_id: str, alias: str, source: str = "") -> None:
        if alias:
            self.repository.upsert_alias(theme_id, alias, source)

    def merge_themes(self, source_theme_id: str, target_theme_id: str) -> None:
        with self.repository.conn:
            self.repository.conn.execute(
                """
                UPDATE source_theme_catalog
                SET matched_theme_id = ?, match_confidence = 1.0, updated_at = CURRENT_TIMESTAMP
                WHERE source_theme_id = ?
                """,
                (target_theme_id, source_theme_id),
            )

    def set_theme_status(self, theme_id: str, status: ThemeStatus, reason: str = "") -> None:
        theme = self.repository.get_canonical_theme(theme_id)
        if theme is None:
            return
        theme.status = status
        if reason:
            theme.theme_group = reason
        self.repository.upsert_canonical_theme(theme)
