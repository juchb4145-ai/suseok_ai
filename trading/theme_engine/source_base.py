from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from trading.theme_engine.models import ThemeMemberEvidence, ThemeSourcePayload


class BaseThemeSource(ABC):
    source_name: str = ""
    supports_live: bool = False

    def __init__(self) -> None:
        self.last_sync_at: datetime | None = None

    @abstractmethod
    def fetch_themes(self) -> list[ThemeSourcePayload]:
        raise NotImplementedError

    @abstractmethod
    def fetch_members(self, source_theme: ThemeSourcePayload) -> list[ThemeMemberEvidence]:
        raise NotImplementedError


class ExternalThemeSourceBase(BaseThemeSource):
    """Interface for licensed external theme/news providers.

    Concrete adapters should translate provider-specific payloads into
    ThemeSourcePayload and ThemeMemberEvidence.  This PR intentionally avoids
    crawling any remote site.
    """

    supports_live = False
