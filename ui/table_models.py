from __future__ import annotations

from datetime import date, datetime, timedelta

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt5.QtGui import QBrush, QColor, QFont

from trading.models import LegStatus, WatchItem
from trading.strategy.models import Candidate, CandidateState, TradeReview


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


class ReviewTableModel(QAbstractTableModel):
    ReviewRole = Qt.UserRole + 20
    ReviewIdRole = Qt.UserRole + 21
    SearchTextRole = Qt.UserRole + 22
    SortRole = Qt.UserRole + 23

    headers = [
        "시각",
        "코드",
        "종목명",
        "시장",
        "테마",
        "등급",
        "상태",
        "주문",
        "청산",
        "5m",
        "10m",
        "20m",
        "20m DD",
        "Missed",
        "False+",
        "사유",
    ]

    _numeric_columns = {
        9: "max_return_5m",
        10: "max_return_10m",
        11: "max_return_20m",
        12: "max_drawdown_20m",
    }

    def __init__(self, reviews: list[TradeReview] | None = None) -> None:
        super().__init__()
        self._reviews: list[TradeReview] = list(reviews or [])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._reviews)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._reviews)):
            return None
        review = self._reviews[index.row()]
        if role == Qt.DisplayRole:
            return self._display_value(review, index.column())
        if role == Qt.ToolTipRole:
            return self._display_value(review, index.column())
        if role == Qt.ForegroundRole:
            return self._foreground(review, index.column())
        if role == Qt.BackgroundRole:
            return self._background(review, index.column())
        if role == self.ReviewRole:
            return review
        if role == self.ReviewIdRole:
            return review.id
        if role == self.SearchTextRole:
            return self._search_text(review)
        if role == self.SortRole:
            return self._sort_value(review, index.column())
        return None

    def set_reviews(self, reviews: list[TradeReview]) -> None:
        self.beginResetModel()
        self._reviews = list(reviews)
        self.endResetModel()

    def review_at(self, row: int) -> TradeReview | None:
        if 0 <= row < len(self._reviews):
            return self._reviews[row]
        return None

    def review_by_id(self, review_id: int | None) -> TradeReview | None:
        for review in self._reviews:
            if review.id == review_id:
                return review
        return None

    @classmethod
    def _display_value(cls, review: TradeReview, column: int) -> str:
        values = [
            review.created_at,
            review.code,
            review.name,
            review.market,
            review.theme_name or review.theme_id,
            review.final_grade,
            review.final_status,
            review.virtual_order_status,
            review.exit_reason,
            cls._metric(review.max_return_5m),
            cls._metric(review.max_return_10m),
            cls._metric(review.max_return_20m),
            cls._metric(review.max_drawdown_20m),
            "Y" if review.false_negative_flag else "",
            "Y" if review.false_positive_flag else "",
            cls.missed_reason(review),
        ]
        return str(values[column] or "") if 0 <= column < len(values) else ""

    @classmethod
    def _sort_value(cls, review: TradeReview, column: int):
        if column in cls._numeric_columns:
            value = getattr(review, cls._numeric_columns[column])
            return float(value) if value is not None else -999999.0
        if column == 13:
            return int(bool(review.false_negative_flag))
        if column == 14:
            return int(bool(review.false_positive_flag))
        return cls._display_value(review, column)

    @classmethod
    def _search_text(cls, review: TradeReview) -> str:
        parts = [
            review.created_at,
            review.trade_date,
            review.code,
            review.name,
            review.market,
            review.theme_id,
            review.theme_name,
            review.final_grade,
            review.final_status,
            review.virtual_order_status,
            review.exit_reason,
            review.missed_reason,
            str(review.details or {}),
        ]
        return " ".join(str(part or "") for part in parts).lower()

    @staticmethod
    def _metric(value) -> str:
        return "" if value is None else f"{float(value):.2f}"

    @staticmethod
    def missed_reason(review: TradeReview) -> str:
        return (
            review.missed_reason
            or str(review.details.get("false_negative_type") or "")
            or str(review.details.get("missed_reason") or "")
            or str(review.details.get("reason") or "")
        )

    @staticmethod
    def review_date(review: TradeReview) -> str:
        if review.trade_date:
            return review.trade_date
        return str(review.created_at or "")[:10]

    @staticmethod
    def _foreground(review: TradeReview, column: int):
        if column in {9, 10, 11}:
            value = getattr(review, ReviewTableModel._numeric_columns[column])
            if value is None:
                return None
            return QBrush(QColor("#17633a" if float(value) > 0 else "#9f1d1d" if float(value) < 0 else "#1f2937"))
        if column == 12:
            value = review.max_drawdown_20m
            if value is None:
                return None
            return QBrush(QColor("#9f1d1d" if float(value) <= -5.0 else "#8a3d00" if float(value) < 0 else "#1f2937"))
        if column == 14 and review.false_positive_flag:
            return QBrush(QColor("#9f1d1d"))
        return None

    @staticmethod
    def _background(review: TradeReview, column: int):
        if column == 13 and review.false_negative_flag:
            return QBrush(QColor("#ffefe0"))
        if column == 14 and review.false_positive_flag:
            return QBrush(QColor("#ffe8e8"))
        if column in {5, 6, 7}:
            return QBrush(QColor("#f5f7fb"))
        return None


class ReviewFilterProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(ReviewTableModel.SortRole)
        self._search_text = ""
        self._date_range = "전체"
        self._start_date = ""
        self._end_date = ""
        self._status_filter = "전체"
        self._grade_filter = "전체"
        self._false_negative_only = False
        self._false_positive_only = False
        self._metric_thresholds: dict[str, float | None] = {"5m": None, "10m": None, "20m": None, "20m_dd": None}

    def set_filters(
        self,
        *,
        search_text: str,
        date_range: str,
        start_date: str,
        end_date: str,
        status_filter: str,
        grade_filter: str,
        false_negative_only: bool,
        false_positive_only: bool,
        metric_thresholds: dict[str, float | None],
    ) -> None:
        self._search_text = str(search_text or "").strip().lower()
        self._date_range = str(date_range or "전체")
        self._start_date = str(start_date or "")
        self._end_date = str(end_date or "")
        self._status_filter = str(status_filter or "전체")
        self._grade_filter = str(grade_filter or "전체")
        self._false_negative_only = bool(false_negative_only)
        self._false_positive_only = bool(false_positive_only)
        self._metric_thresholds = dict(metric_thresholds or {})
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        review = self._review(source_row, source_parent)
        if review is None:
            return True
        if self._search_text:
            model = self.sourceModel()
            text = model.data(model.index(source_row, 0, source_parent), ReviewTableModel.SearchTextRole) or ""
            if self._search_text not in text:
                return False
        if not self._date_matches(review):
            return False
        if not self._status_matches(review):
            return False
        if not self._grade_matches(review):
            return False
        if self._false_negative_only and not review.false_negative_flag:
            return False
        if self._false_positive_only and not review.false_positive_flag:
            return False
        return self._metrics_match(review)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        left_value = self.sourceModel().data(left, ReviewTableModel.SortRole)
        right_value = self.sourceModel().data(right, ReviewTableModel.SortRole)
        return left_value < right_value

    def _review(self, source_row: int, source_parent: QModelIndex) -> TradeReview | None:
        model = self.sourceModel()
        if model is None:
            return None
        return model.data(model.index(source_row, 0, source_parent), ReviewTableModel.ReviewRole)

    def _date_matches(self, review: TradeReview) -> bool:
        if self._date_range == "전체":
            return True
        review_date = _parse_date(ReviewTableModel.review_date(review))
        if review_date is None:
            return False
        today = date.today()
        if self._date_range == "오늘":
            return review_date == today
        if self._date_range == "최근 3일":
            return today - timedelta(days=2) <= review_date <= today
        if self._date_range == "최근 7일":
            return today - timedelta(days=6) <= review_date <= today
        start = _parse_date(self._start_date)
        end = _parse_date(self._end_date)
        if start is not None and review_date < start:
            return False
        if end is not None and review_date > end:
            return False
        return True

    def _status_matches(self, review: TradeReview) -> bool:
        value = self._status_filter
        if value in {"", "전체"}:
            return True
        status = str(review.final_status or "")
        lowered = status.lower()
        if value == "entered":
            return "virtual" in lowered or bool(review.virtual_order_status)
        if value == "missed":
            return bool(review.false_negative_flag or ReviewTableModel.missed_reason(review) or "miss" in lowered or "plan_not_created" in lowered)
        if value == "blocked":
            return "blocked" in lowered
        if value == "expired":
            return "expired" in lowered
        return status == value

    def _grade_matches(self, review: TradeReview) -> bool:
        value = self._grade_filter
        grade = str(review.final_grade or "")
        if value in {"", "전체"}:
            return True
        if value == "빈 값/미분류":
            return not grade
        return grade == value

    def _metrics_match(self, review: TradeReview) -> bool:
        checks = [
            ("5m", review.max_return_5m),
            ("10m", review.max_return_10m),
            ("20m", review.max_return_20m),
        ]
        for key, value in checks:
            threshold = self._metric_thresholds.get(key)
            if threshold is not None and (value is None or float(value) < float(threshold)):
                return False
        dd_threshold = self._metric_thresholds.get("20m_dd")
        if dd_threshold is not None:
            target = -abs(float(dd_threshold))
            if review.max_drawdown_20m is None or float(review.max_drawdown_20m) > target:
                return False
        return True


def _parse_date(value: str) -> date | None:
    text = str(value or "")[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


class WatchItemTableModel(QAbstractTableModel):
    ItemRole = Qt.UserRole + 40
    CodeRole = Qt.UserRole + 41
    SearchTextRole = Qt.UserRole + 42
    SortRole = Qt.UserRole + 43
    HoldingRole = Qt.UserRole + 44
    AutoBuyRole = Qt.UserRole + 45
    OpenOrderRole = Qt.UserRole + 46
    StopRiskRole = Qt.UserRole + 47
    TakeProfitRole = Qt.UserRole + 48
    WatchingRole = Qt.UserRole + 49
    PendingOrderRole = Qt.UserRole + 50
    RiskToneRole = Qt.UserRole + 51
    LegToneRole = Qt.UserRole + 52

    headers = [
        "코드",
        "종목명",
        "현재가",
        "예산",
        "손절가",
        "1차",
        "1차%",
        "1차상태",
        "2차",
        "2차%",
        "2차상태",
        "3차",
        "3차%",
        "3차상태",
        "보유",
        "평단",
        "익절완료",
        "자동매수",
    ]

    _numeric_columns = {
        2: "current_price",
        3: "budget",
        4: "stop_loss_price",
        5: ("leg_price", 1),
        6: ("leg_weight", 1),
        8: ("leg_price", 2),
        9: ("leg_weight", 2),
        11: ("leg_price", 3),
        12: ("leg_weight", 3),
        14: "holding_quantity",
        15: "average_price",
    }

    def __init__(self, items: list[WatchItem] | None = None, *, mock_mode: bool = True, ordering_enabled: bool = False) -> None:
        super().__init__()
        self._items: list[WatchItem] = list(items or [])
        self._mock_mode = bool(mock_mode)
        self._ordering_enabled = bool(ordering_enabled)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        if role == Qt.DisplayRole:
            return self._display_value(item, index.column())
        if role == Qt.ToolTipRole:
            return self._display_value(item, index.column())
        if role == Qt.ForegroundRole:
            return self._foreground(item, index.column())
        if role == Qt.BackgroundRole:
            return self._background(item, index.column())
        if role == Qt.FontRole and index.column() == 14 and item.holding_quantity > 0:
            font = QFont()
            font.setBold(True)
            return font
        if role == self.ItemRole:
            return item
        if role == self.CodeRole:
            return item.code
        if role == self.SearchTextRole:
            return f"{item.code} {item.name}".lower()
        if role == self.SortRole:
            return self._sort_value(item, index.column())
        if role == self.HoldingRole:
            return item.holding_quantity > 0
        if role == self.AutoBuyRole:
            return bool(item.auto_buy_enabled)
        if role == self.OpenOrderRole:
            return self.has_open_order(item)
        if role == self.StopRiskRole:
            return self.stop_risk_tone(item) in {"warning", "danger"}
        if role == self.TakeProfitRole:
            return bool(item.take_profit_done)
        if role == self.WatchingRole:
            return any(leg.status == LegStatus.WATCHING for leg in item.legs)
        if role == self.PendingOrderRole:
            return any(leg.status in {LegStatus.ORDER_SENT, LegStatus.UNFILLED, LegStatus.PARTIALLY_FILLED} for leg in item.legs)
        if role == self.RiskToneRole:
            return self.row_tone(item)
        if role == self.LegToneRole:
            leg = self._leg_for_status_column(item, index.column())
            return self.leg_status_tone(leg.status) if leg is not None else "neutral"
        return None

    def set_items(self, items: list[WatchItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def set_runtime_context(self, *, mock_mode: bool, ordering_enabled: bool) -> None:
        self._mock_mode = bool(mock_mode)
        self._ordering_enabled = bool(ordering_enabled)
        if self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, self.columnCount() - 1),
                [Qt.BackgroundRole, self.RiskToneRole],
            )

    def item_at(self, row: int) -> WatchItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def item_by_code(self, code: str) -> WatchItem | None:
        for item in self._items:
            if item.code == code:
                return item
        return None

    @classmethod
    def _display_value(cls, item: WatchItem, column: int) -> str:
        values = [
            item.code,
            item.name,
            cls._money(item.current_price),
            cls._money(item.budget),
            cls._money(item.stop_loss_price),
            cls._money(item.leg(1).target_price),
            f"{item.leg(1).weight_percent:.1f}",
            item.leg(1).status.value,
            cls._money(item.leg(2).target_price),
            f"{item.leg(2).weight_percent:.1f}",
            item.leg(2).status.value,
            cls._money(item.leg(3).target_price),
            f"{item.leg(3).weight_percent:.1f}",
            item.leg(3).status.value,
            str(item.holding_quantity),
            f"{item.average_price:,.1f}",
            "Y" if item.take_profit_done else "",
            "Y" if item.auto_buy_enabled else "",
        ]
        return str(values[column] or "") if 0 <= column < len(values) else ""

    @classmethod
    def _sort_value(cls, item: WatchItem, column: int):
        mapping = cls._numeric_columns.get(column)
        if mapping is None:
            return cls._display_value(item, column)
        if isinstance(mapping, tuple):
            kind, leg_index = mapping
            leg = item.leg(leg_index)
            return float(leg.target_price if kind == "leg_price" else leg.weight_percent)
        return float(getattr(item, mapping))

    def _background(self, item: WatchItem, column: int):
        if column in {2, 4}:
            risk = self.stop_risk_tone(item)
            if risk == "danger":
                return QBrush(QColor("#ffe8e8"))
            if risk == "warning":
                return QBrush(QColor("#ffefe0"))
        if column in {7, 10, 13}:
            leg = self._leg_for_status_column(item, column)
            if leg is not None:
                return QBrush(QColor(self._tone_color(self.leg_status_tone(leg.status))))
        if column == 16 and item.take_profit_done:
            return QBrush(QColor("#e9f7ef"))
        if column == 17 and self.live_auto_buy_risk(item):
            return QBrush(QColor("#ffe8e8"))
        tone = self.row_tone(item)
        if tone == "danger":
            return QBrush(QColor("#fff1f1"))
        if tone == "warning":
            return QBrush(QColor("#fff8ee"))
        if tone == "info":
            return QBrush(QColor("#f5f9ff"))
        if tone == "success":
            return QBrush(QColor("#f4fbf7"))
        return None

    @staticmethod
    def _foreground(item: WatchItem, column: int):
        if column == 14 and item.holding_quantity > 0:
            return QBrush(QColor("#1d4f8f"))
        return None

    @staticmethod
    def _leg_for_status_column(item: WatchItem, column: int):
        if column == 7:
            return item.leg(1)
        if column == 10:
            return item.leg(2)
        if column == 13:
            return item.leg(3)
        return None

    @staticmethod
    def _money(value: int) -> str:
        return f"{int(value):,}" if value else ""

    @staticmethod
    def has_open_order(item: WatchItem) -> bool:
        return any(max(0, leg.ordered_quantity - leg.filled_quantity) > 0 or bool(leg.order_no) for leg in item.legs)

    @staticmethod
    def stop_risk_tone(item: WatchItem) -> str:
        if not item.current_price or not item.stop_loss_price:
            return "neutral"
        if item.current_price <= item.stop_loss_price:
            return "danger"
        if item.current_price <= item.stop_loss_price * 1.03:
            return "warning"
        return "neutral"

    @staticmethod
    def leg_status_tone(status) -> str:
        value = status.value if hasattr(status, "value") else str(status)
        if value == LegStatus.WAITING.value:
            return "muted"
        if value == LegStatus.WATCHING.value:
            return "info"
        if value in {LegStatus.ORDER_SENT.value, LegStatus.UNFILLED.value, LegStatus.PARTIALLY_FILLED.value}:
            return "warning"
        if value == LegStatus.FILLED.value:
            return "success"
        if any(token in value for token in ["오류", "실패", "취소"]):
            return "danger"
        return "neutral"

    def live_auto_buy_risk(self, item: WatchItem) -> bool:
        return bool(item.auto_buy_enabled and self._ordering_enabled and not self._mock_mode)

    def row_tone(self, item: WatchItem) -> str:
        if self.live_auto_buy_risk(item) or self.stop_risk_tone(item) == "danger":
            return "danger"
        if self.stop_risk_tone(item) == "warning" or self.has_open_order(item):
            return "warning"
        if item.take_profit_done:
            return "success"
        if item.holding_quantity > 0:
            return "info"
        return "neutral"

    @staticmethod
    def _tone_color(tone: str) -> str:
        return {
            "muted": "#f5f7fb",
            "info": "#eef4ff",
            "warning": "#ffefe0",
            "success": "#e9f7ef",
            "danger": "#ffe8e8",
        }.get(tone, "#ffffff")


class WatchItemFilterProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.setSortRole(WatchItemTableModel.SortRole)
        self._search_text = ""
        self._holding_only = False
        self._auto_buy_only = False
        self._open_order_only = False
        self._stop_risk_only = False
        self._take_profit_only = False
        self._watching_only = False
        self._pending_only = False

    def set_filters(
        self,
        *,
        search_text: str,
        holding_only: bool,
        auto_buy_only: bool,
        open_order_only: bool,
        stop_risk_only: bool,
        take_profit_only: bool,
        watching_only: bool,
        pending_only: bool,
    ) -> None:
        self._search_text = str(search_text or "").strip().lower()
        self._holding_only = bool(holding_only)
        self._auto_buy_only = bool(auto_buy_only)
        self._open_order_only = bool(open_order_only)
        self._stop_risk_only = bool(stop_risk_only)
        self._take_profit_only = bool(take_profit_only)
        self._watching_only = bool(watching_only)
        self._pending_only = bool(pending_only)
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        index = model.index(source_row, 0, source_parent)
        if self._search_text and self._search_text not in (model.data(index, WatchItemTableModel.SearchTextRole) or ""):
            return False
        checks = [
            (self._holding_only, WatchItemTableModel.HoldingRole),
            (self._auto_buy_only, WatchItemTableModel.AutoBuyRole),
            (self._open_order_only, WatchItemTableModel.OpenOrderRole),
            (self._stop_risk_only, WatchItemTableModel.StopRiskRole),
            (self._take_profit_only, WatchItemTableModel.TakeProfitRole),
            (self._watching_only, WatchItemTableModel.WatchingRole),
            (self._pending_only, WatchItemTableModel.PendingOrderRole),
        ]
        return all(not enabled or bool(model.data(index, role)) for enabled, role in checks)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        left_value = self.sourceModel().data(left, WatchItemTableModel.SortRole)
        right_value = self.sourceModel().data(right, WatchItemTableModel.SortRole)
        return left_value < right_value
