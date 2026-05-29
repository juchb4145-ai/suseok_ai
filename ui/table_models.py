from __future__ import annotations

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt5.QtGui import QBrush, QColor

from trading.strategy.models import Candidate, CandidateState


class CandidateTableModel(QAbstractTableModel):
    CandidateRole = Qt.UserRole + 1
    CandidateIdRole = Qt.UserRole + 2
    SearchTextRole = Qt.UserRole + 3
    StateRole = Qt.UserRole + 4
    RecoverRole = Qt.UserRole + 5
    ThemeMappedRole = Qt.UserRole + 6

    headers = [
        "Code",
        "Name",
        "State",
        "Block",
        "Recover",
        "Best Theme",
        "Gate Key",
        "Sub Status",
        "Last Seen",
        "Expires",
        "Reasons",
    ]

    _state_backgrounds = {
        CandidateState.READY: QColor("#e9f7ef"),
        CandidateState.BLOCKED: QColor("#ffefe0"),
        CandidateState.WATCHING: QColor("#eef4ff"),
        CandidateState.DETECTED: QColor("#f5f7fb"),
    }

    def __init__(
        self,
        candidates: list[Candidate] | None = None,
        mapped_codes: set[str] | None = None,
        theme_text_by_code: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._candidates: list[Candidate] = list(candidates or [])
        self._mapped_codes: set[str] = set(mapped_codes or set())
        self._theme_text_by_code: dict[str, str] = dict(theme_text_by_code or {})

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._candidates)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._candidates)):
            return None
        candidate = self._candidates[index.row()]
        if role == Qt.DisplayRole:
            return self._display_value(candidate, index.column())
        if role == Qt.ToolTipRole:
            return self._display_value(candidate, index.column())
        if role == Qt.BackgroundRole and index.column() == 2:
            color = self._state_backgrounds.get(candidate.state)
            return QBrush(color) if color is not None else None
        if role == self.CandidateRole:
            return candidate
        if role == self.CandidateIdRole:
            return candidate.id
        if role == self.SearchTextRole:
            return self._search_text(candidate)
        if role == self.StateRole:
            return candidate.state.value
        if role == self.RecoverRole:
            return bool(candidate.can_recover)
        if role == self.ThemeMappedRole:
            return candidate.code in self._mapped_codes
        return None

    def set_candidates(
        self,
        candidates: list[Candidate],
        mapped_codes: set[str] | None = None,
        theme_text_by_code: dict[str, str] | None = None,
    ) -> None:
        self.beginResetModel()
        self._candidates = list(candidates)
        self._mapped_codes = set(mapped_codes or set())
        self._theme_text_by_code = dict(theme_text_by_code or {})
        self.endResetModel()

    def candidate_at(self, row: int) -> Candidate | None:
        if 0 <= row < len(self._candidates):
            return self._candidates[row]
        return None

    def candidate_by_id(self, candidate_id: int | None) -> Candidate | None:
        for candidate in self._candidates:
            if candidate.id == candidate_id:
                return candidate
        return None

    @classmethod
    def _display_value(cls, candidate: Candidate, column: int) -> str:
        metadata = cls._metadata(candidate)
        values = [
            candidate.code,
            candidate.name,
            candidate.state.value,
            candidate.block_type.value,
            "Y" if candidate.can_recover else "",
            cls._metadata_text(metadata, "best_theme_id"),
            cls._metadata_text(metadata, "best_gate_result_key"),
            cls._metadata_text(metadata, "sub_status"),
            candidate.last_seen_at,
            candidate.expires_at,
            cls.block_reason_summary(metadata),
        ]
        return str(values[column] or "") if 0 <= column < len(values) else ""

    def _search_text(self, candidate: Candidate) -> str:
        metadata = self._metadata(candidate)
        parts = [
            candidate.code,
            candidate.name,
            candidate.market,
            candidate.state.value,
            candidate.block_type.value,
            " ".join(candidate.condition_names),
            " ".join(candidate.theme_ids),
            self._theme_text_by_code.get(candidate.code, ""),
            self._metadata_text(metadata, "best_theme_id"),
            self._metadata_text(metadata, "best_gate_result_key"),
            self._metadata_text(metadata, "sub_status"),
            self.block_reason_summary(metadata),
        ]
        return " ".join(str(part or "") for part in parts).lower()

    @staticmethod
    def _metadata(candidate: Candidate) -> dict:
        return candidate.metadata if isinstance(candidate.metadata, dict) else {}

    @staticmethod
    def _metadata_text(metadata: dict, key: str) -> str:
        value = metadata.get(key, "")
        return "" if value is None else str(value)

    @staticmethod
    def block_reason_summary(metadata: dict) -> str:
        reasons = []
        for record in dict(metadata.get("block_reasons_by_theme", {})).values():
            if isinstance(record, dict):
                reasons.extend(record.get("reason_codes", []))
        return ", ".join(str(reason) for reason in reasons[:5])


class CandidateFilterProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self._search_text = ""
        self._state_filter = ""
        self._recover_only = False
        self._theme_filter = ""

    def set_search_text(self, text: str) -> None:
        self._search_text = str(text or "").strip().lower()
        self.invalidateFilter()

    def set_state_filter(self, state: str) -> None:
        self._state_filter = "" if state in {"", "ALL", "전체"} else str(state)
        self.invalidateFilter()

    def set_recover_only(self, enabled: bool) -> None:
        self._recover_only = bool(enabled)
        self.invalidateFilter()

    def set_theme_filter(self, value: str) -> None:
        self._theme_filter = "" if value in {"", "ALL", "전체"} else str(value)
        self.invalidateFilter()

    def clear_filters(self) -> None:
        self._search_text = ""
        self._state_filter = ""
        self._recover_only = False
        self._theme_filter = ""
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        source_index = model.index(source_row, 0, source_parent)
        if self._state_filter:
            state = model.data(source_index, CandidateTableModel.StateRole)
            if state != self._state_filter:
                return False
        if self._recover_only and not model.data(source_index, CandidateTableModel.RecoverRole):
            return False
        if self._theme_filter:
            mapped = bool(model.data(source_index, CandidateTableModel.ThemeMappedRole))
            if self._theme_filter == "mapped" and not mapped:
                return False
            if self._theme_filter == "unmapped" and mapped:
                return False
        if self._search_text:
            text = model.data(source_index, CandidateTableModel.SearchTextRole) or ""
            return self._search_text in text
        return True
