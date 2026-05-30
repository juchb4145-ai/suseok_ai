from __future__ import annotations

from trading.theme_engine.models import RelationType, ThemeEvidenceType, ThemeMemberEvidence, ThemeSourcePayload
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.source_base import BaseThemeSource


class KiwoomThemeSource(BaseThemeSource):
    """Adapter shell for Kiwoom theme APIs.

    The live OpenAPI calls are intentionally thin wrappers so tests can mock the
    client.  Real opt90001/opt90002 hardening belongs in a follow-up PR.
    """

    source_name = "kiwoom"
    supports_live = True

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def fetch_themes(self) -> list[ThemeSourcePayload]:
        raw = _call_first_available(self.client, ["get_theme_group_list", "GetThemeGroupList"])
        themes = parse_theme_group_list(raw)
        return [
            ThemeSourcePayload(
                source=self.source_name,
                source_theme_id=theme_id,
                source_theme_name=theme_name,
                raw_payload={"raw": raw},
            )
            for theme_id, theme_name in themes
        ]

    def fetch_members(self, source_theme: ThemeSourcePayload) -> list[ThemeMemberEvidence]:
        raw = _call_first_available(
            self.client,
            ["get_theme_group_code", "GetThemeGroupCode"],
            source_theme.source_theme_id,
        )
        members = parse_theme_member_codes(raw)
        result = []
        for code in members:
            name = _call_first_available(self.client, ["get_master_code_name", "GetMasterCodeName"], code) or ""
            result.append(
                ThemeMemberEvidence(
                    theme_id="",
                    stock_code=normalize_stock_code(code),
                    stock_name=str(name),
                    source=self.source_name,
                    evidence_type=ThemeEvidenceType.SOURCE_MEMBER,
                    relation_type=RelationType.SAME_INDUSTRY,
                    reason=source_theme.source_theme_name,
                    confidence=0.75,
                )
            )
        return result


def parse_theme_group_list(raw) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [(str(key), str(value)) for key, value in raw.items()]
    text = str(raw)
    result = []
    for chunk in text.replace("\n", ";").split(";"):
        if not chunk.strip():
            continue
        if "^" in chunk:
            theme_id, name = chunk.split("^", 1)
        elif "|" in chunk:
            theme_id, name = chunk.split("|", 1)
        else:
            theme_id, name = "", chunk
        result.append((theme_id.strip(), name.strip()))
    return result


def parse_theme_member_codes(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [normalize_stock_code(str(value)) for value in raw if normalize_stock_code(str(value))]
    result = []
    for chunk in str(raw).replace("\n", ";").replace(",", ";").split(";"):
        code = normalize_stock_code(chunk.strip().split("^", 1)[0])
        if code:
            result.append(code)
    return result


def _call_first_available(client, names: list[str], *args):
    for name in names:
        fn = getattr(client, name, None)
        if callable(fn):
            return fn(*args)
    return None
