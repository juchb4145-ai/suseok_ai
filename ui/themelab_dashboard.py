from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

try:
    from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
    from PyQt5.QtGui import QBrush, QColor
    from PyQt5.QtWidgets import (
        QAbstractItemView,
        QButtonGroup,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QSizePolicy,
        QSplitter,
        QTableView,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError:  # pragma: no cover - lets non-UI tests import the pure ViewModel builder.
    class _QtFallback:
        DisplayRole = 0
        ToolTipRole = 1
        ForegroundRole = 2
        TextAlignmentRole = 3
        UserRole = 256
        Horizontal = 1
        AlignRight = 2
        AlignVCenter = 4
        AlignCenter = 8
        Vertical = 2

    Qt = _QtFallback()

    class QModelIndex:
        def isValid(self) -> bool:
            return False

        def row(self) -> int:
            return -1

        def column(self) -> int:
            return -1

    class _FallbackObject:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                return None

            return _noop

    class QAbstractTableModel(_FallbackObject):
        def beginResetModel(self) -> None:
            pass

        def endResetModel(self) -> None:
            pass

    class QSortFilterProxyModel(_FallbackObject):
        def invalidateFilter(self) -> None:
            pass

    class QBrush(_FallbackObject):
        pass

    class QColor(_FallbackObject):
        pass

    class QSizePolicy:
        Expanding = 0

    class QAbstractItemView:
        SelectRows = 0
        SingleSelection = 0
        NoEditTriggers = 0

    QWidget = QFrame = QGroupBox = QLabel = QPushButton = QSplitter = QTableView = QTabWidget = QTextEdit = _FallbackObject
    QButtonGroup = QGridLayout = QHBoxLayout = QVBoxLayout = _FallbackObject

from trading.theme_engine.lab import LabGateStatus, ThemeLabFlowResult
from ui.formatters import format_money, format_percent
from ui.style import THEME_LAB_COLORS, badge_style, tone_for_value


CHART_STATUS_READY = "READY"
CHART_STATUS_NO_CANDLE = "NO_CANDLE_DATA"
CHART_STATUS_QUOTE_ONLY = "QUOTE_ONLY"
CHART_STATUS_STALE = "STALE"
CHART_STATUS_UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class ThemeLabConditionStatus:
    condition_name: str
    purpose: str
    resolved_index: str = "UNKNOWN"
    registered: bool = False
    screen_no: str = ""
    include_count: int = 0
    remove_count: int = 0
    last_event_at: str = ""
    warning: str = ""


@dataclass(frozen=True)
class ThemeLabDataQuality:
    status: str = "BROKEN"
    quote_stale_count: int = 0
    prev_close_missing_count: int = 0
    candle_missing_count: int = 0
    vwap_missing_count: int = 0
    session_high_missing_count: int = 0
    vi_status_supported: bool = False
    theme_mapping_missing_count: int = 0
    watchset_size: int = 0
    realtime_subscription_count: int = 0
    realtime_subscription_limit: int = 0


@dataclass(frozen=True)
class ChartUniverseItem:
    symbol: str
    name: str
    type: str
    reason: str
    priority: int
    has_candle_data: bool
    chart_data_status: str
    last_candle_at: str = ""


@dataclass(frozen=True)
class ChartMarker:
    ts: str
    marker_type: str
    label: str
    source: str


@dataclass(frozen=True)
class ThemeLabDashboardState:
    market: Any | None = None
    condition_statuses: tuple[ThemeLabConditionStatus, ...] = ()
    data_quality: ThemeLabDataQuality = field(default_factory=ThemeLabDataQuality)
    ranked_themes: tuple[Any, ...] = ()
    selected_theme_id: str = ""
    selected_symbol: str = ""
    watchset: tuple[Any, ...] = ()
    gate_decisions: tuple[Any, ...] = ()
    price_location_results: dict[str, Any] = field(default_factory=dict)
    risk_results: dict[str, Any] = field(default_factory=dict)
    entry_candidates: tuple[Any, ...] = ()
    chart_universe: tuple[ChartUniverseItem, ...] = ()
    selected_chart_symbol: str = ""
    selected_chart_type: str = "index"
    chart_data_status_by_symbol: dict[str, str] = field(default_factory=dict)
    candle_series_by_symbol: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    chart_markers_by_symbol: dict[str, tuple[ChartMarker, ...]] = field(default_factory=dict)
    default_chart_symbol: str = "KOSDAQ"
    index_charts: tuple[ChartUniverseItem, ...] = ()
    last_updated_at: str = ""


@dataclass(frozen=True)
class DashboardChartConfig:
    enabled: bool = True
    max_chart_symbols: int = 50
    max_watchset_chart_symbols: int = 20
    include_indices: bool = True
    include_ready_candidates: bool = True
    include_positions: bool = True
    default_timeframe: str = "1m"
    supported_timeframes: tuple[str, ...] = ("1m", "3m", "5m")
    stale_candle_sec: int = 15


def build_theme_lab_dashboard_state(
    result: ThemeLabFlowResult | None,
    *,
    condition_statuses: Iterable[ThemeLabConditionStatus] = (),
    candle_series_by_symbol: dict[str, Iterable[Any]] | None = None,
    quote_only_symbols: Iterable[str] = (),
    position_symbols: Iterable[str] = (),
    selected_theme_id: str = "",
    selected_symbol: str = "",
    config: DashboardChartConfig | None = None,
    now: datetime | None = None,
) -> ThemeLabDashboardState:
    config = config or DashboardChartConfig()
    candle_map = {str(symbol): tuple(candles or ()) for symbol, candles in (candle_series_by_symbol or {}).items()}
    quote_only = {str(symbol) for symbol in quote_only_symbols}
    position_set = {str(symbol) for symbol in position_symbols}
    if result is None:
        return ThemeLabDashboardState(
            condition_statuses=tuple(condition_statuses),
            candle_series_by_symbol=candle_map,
            last_updated_at=_format_time(now),
        )

    watchset = _sorted_watchset(result.watchset)
    selected_symbol = selected_symbol or _default_selected_symbol(result, candle_map)
    selected_theme_id = selected_theme_id or (result.themes[0].theme_name if result.themes else "")
    data_quality = _data_quality_from_result(result)
    chart_universe = _build_chart_universe(
        result,
        candle_map,
        quote_only,
        position_set,
        selected_theme_id=selected_theme_id,
        selected_symbol=selected_symbol,
        config=config,
    )
    status_by_symbol = {item.symbol: item.chart_data_status for item in chart_universe}
    selected_chart = _select_chart_item(chart_universe, selected_symbol)
    entry_candidates = tuple(
        item for item in watchset if _enum_value(item.final_gate_status or item.gate_status) in {"READY", "READY_SMALL"}
    )
    return ThemeLabDashboardState(
        market=result.market,
        condition_statuses=tuple(condition_statuses),
        data_quality=data_quality,
        ranked_themes=tuple(result.themes),
        selected_theme_id=selected_theme_id,
        selected_symbol=selected_symbol,
        watchset=tuple(watchset),
        gate_decisions=tuple(result.gate_decisions),
        price_location_results={item.symbol: item.price_location_status for item in watchset},
        risk_results={item.symbol: item.risk_level for item in watchset},
        entry_candidates=entry_candidates,
        chart_universe=chart_universe,
        selected_chart_symbol=selected_chart.symbol if selected_chart else "KOSDAQ",
        selected_chart_type=selected_chart.type if selected_chart else "index",
        chart_data_status_by_symbol=status_by_symbol,
        candle_series_by_symbol=candle_map,
        chart_markers_by_symbol=_build_chart_markers(watchset),
        default_chart_symbol=_default_selected_symbol(result, candle_map),
        index_charts=tuple(item for item in chart_universe if item.type == "index"),
        last_updated_at=_format_time(now),
    )


class ThemeLabDashboardWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._state = ThemeLabDashboardState()
        self._selected_status_filter = "ALL"
        self.setObjectName("themeLabDashboard")
        self.setStyleSheet(theme_lab_stylesheet())
        self._build_ui()

    def set_state(self, state: ThemeLabDashboardState) -> None:
        self._state = state
        self._render_header(state)
        self.theme_model.set_rows(list(state.ranked_themes))
        self.watch_model.set_rows(list(state.watchset))
        self.watch_proxy.set_status_filter(self._selected_status_filter)
        self.order_model.set_rows(list(state.entry_candidates))
        self._render_chart(state)
        self._render_detail(state)
        self._render_conditions(state)
        self._render_data_quality(state)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)
        self.market_header = QLabel("ThemeLabFlow 대시보드 대기 중")
        self.market_header.setObjectName("marketHeader")
        outer.addWidget(self.market_header)

        body = QSplitter()
        body.setObjectName("themeLabBodySplitter")
        body.addWidget(self._build_theme_panel())
        body.addWidget(self._build_chart_panel())
        body.addWidget(self._build_gate_panel())
        body.setSizes([330, 560, 360])
        outer.addWidget(body, 3)

        bottom = QSplitter()
        bottom.setOrientation(Qt.Vertical)
        bottom.addWidget(self._build_watch_panel())
        bottom.addWidget(self._build_ops_panel())
        bottom.setSizes([360, 170])
        outer.addWidget(bottom, 2)

    def _build_theme_panel(self) -> QWidget:
        box = _panel("Theme Rank")
        layout = QVBoxLayout(box)
        self.theme_model = ThemeRankTableModel()
        self.theme_table = _table(self.theme_model)
        self.theme_table.selectionModel().selectionChanged.connect(lambda *_args: self._theme_clicked())
        layout.addWidget(self.theme_table)
        return box

    def _build_chart_panel(self) -> QWidget:
        box = _panel("Chart Focus")
        layout = QVBoxLayout(box)
        top = QHBoxLayout()
        self.chart_title = QLabel("KOSDAQ")
        self.chart_title.setObjectName("chartTitle")
        top.addWidget(self.chart_title, 1)
        self.timeframe_group = QButtonGroup(self)
        for label in ("1m", "3m", "5m"):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName("timeframeButton")
            button.setChecked(label == "1m")
            self.timeframe_group.addButton(button)
            top.addWidget(button)
        layout.addLayout(top)
        self.chart_canvas = CandleChartPlaceholder()
        layout.addWidget(self.chart_canvas, 1)
        return box

    def _build_gate_panel(self) -> QWidget:
        box = _panel("Gate Detail")
        layout = QVBoxLayout(box)
        self.gate_badge = QLabel("OBSERVE")
        self.gate_badge.setObjectName("gateStatus")
        self.gate_summary = QLabel("선택된 종목 없음")
        self.gate_summary.setWordWrap(True)
        self.gate_detail = QTextEdit()
        self.gate_detail.setReadOnly(True)
        layout.addWidget(self.gate_badge)
        layout.addWidget(self.gate_summary)
        layout.addWidget(self.gate_detail, 1)
        return box

    def _build_watch_panel(self) -> QWidget:
        box = _panel("WatchSet / Order Candidates")
        layout = QVBoxLayout(box)
        filters = QHBoxLayout()
        self.watch_filter_group = QButtonGroup(self)
        for status in ("ALL", "READY", "READY_SMALL", "WAIT", "BLOCKED", "OBSERVE"):
            button = QPushButton("전체" if status == "ALL" else status)
            button.setCheckable(True)
            button.setChecked(status == "ALL")
            button.clicked.connect(lambda _checked, value=status: self._set_watch_filter(value))
            self.watch_filter_group.addButton(button)
            filters.addWidget(button)
        filters.addStretch(1)
        layout.addLayout(filters)

        tabs = QTabWidget()
        self.watch_model = ThemeLabWatchTableModel()
        self.watch_proxy = ThemeLabWatchFilterProxyModel()
        self.watch_proxy.setSourceModel(self.watch_model)
        self.watch_table = _table(self.watch_proxy)
        self.watch_table.selectionModel().selectionChanged.connect(lambda *_args: self._watch_clicked())
        self.order_model = ThemeLabOrderTableModel()
        self.order_table = _table(self.order_model)
        tabs.addTab(self.watch_table, "WatchSet")
        tabs.addTab(self.order_table, "주문 후보")
        layout.addWidget(tabs, 1)
        return box

    def _build_ops_panel(self) -> QWidget:
        tabs = QTabWidget()
        self.condition_view = QTextEdit()
        self.condition_view.setReadOnly(True)
        self.data_quality_view = QTextEdit()
        self.data_quality_view.setReadOnly(True)
        tabs.addTab(self.condition_view, "조건식 상태")
        tabs.addTab(self.data_quality_view, "Data Quality")
        return tabs

    def _theme_clicked(self) -> None:
        row = self.theme_table.currentIndex().row()
        if row < 0:
            return
        theme = self.theme_model.row_at(row)
        if theme is None:
            return
        selected_theme_id = getattr(theme, "theme_name", "")
        state = build_theme_lab_dashboard_state(
            _result_from_state(self._state),
            condition_statuses=self._state.condition_statuses,
            candle_series_by_symbol=self._state.candle_series_by_symbol,
            selected_theme_id=selected_theme_id,
            now=None,
        )
        self.set_state(state)

    def _watch_clicked(self) -> None:
        index = self.watch_table.currentIndex()
        if not index.isValid():
            return
        source_index = self.watch_proxy.mapToSource(index)
        item = self.watch_model.row_at(source_index.row())
        if item is None:
            return
        state = build_theme_lab_dashboard_state(
            _result_from_state(self._state),
            condition_statuses=self._state.condition_statuses,
            candle_series_by_symbol=self._state.candle_series_by_symbol,
            selected_theme_id=self._state.selected_theme_id,
            selected_symbol=item.symbol,
            now=None,
        )
        self.set_state(state)

    def _set_watch_filter(self, status: str) -> None:
        self._selected_status_filter = status
        self.watch_proxy.set_status_filter(status)

    def _render_header(self, state: ThemeLabDashboardState) -> None:
        if state.market is None:
            self.market_header.setText("[대기] ThemeLabFlow 결과가 아직 없습니다 | live 데이터 없으면 mock으로 대체하지 않습니다")
            return
        market = state.market
        data_tone = "warning" if state.data_quality.status in {"WARNING", "DEGRADED"} else "danger" if state.data_quality.status == "BROKEN" else "success"
        self.market_header.setText(
            f"[{_enum_value(market.market_status)}] "
            f"KOSPI {format_percent(market.kospi_return_pct)} / KOSDAQ {format_percent(market.kosdaq_return_pct)} | "
            f"+3% {market.market_strong_count} | +5% {market.market_leader_count} | "
            f"조건식 {_registered_count(state.condition_statuses)}/{len(state.condition_statuses) or 3} 정상 | "
            f"WatchSet {state.data_quality.watchset_size} | Data {state.data_quality.status} | {state.last_updated_at or '-'}"
        )
        self.market_header.setProperty("tone", data_tone)

    def _render_chart(self, state: ThemeLabDashboardState) -> None:
        symbol = state.selected_chart_symbol or "KOSDAQ"
        status = state.chart_data_status_by_symbol.get(symbol, CHART_STATUS_NO_CANDLE)
        selected_watch = _watch_by_symbol(state.watchset, state.selected_symbol or symbol)
        gate = _enum_value(selected_watch.final_gate_status if selected_watch else "")
        self.chart_title.setText(f"{_chart_name(state, symbol)} [{symbol}] [{status}]" + (f" [{gate}]" if gate else ""))
        self.chart_canvas.set_chart(
            symbol=symbol,
            candles=state.candle_series_by_symbol.get(symbol, ()),
            status=status,
            missing_notes=_missing_notes(selected_watch, status),
        )

    def _render_detail(self, state: ThemeLabDashboardState) -> None:
        item = _watch_by_symbol(state.watchset, state.selected_symbol)
        if item is None:
            self.gate_badge.setText("OBSERVE")
            self.gate_badge.setStyleSheet(badge_style("muted"))
            self.gate_summary.setText("선택된 WatchSet 종목이 없습니다.")
            self.gate_detail.setPlainText("강한 테마가 형성되면 WatchSet과 Gate Detail이 표시됩니다.")
            return
        gate = _enum_value(item.final_gate_status or item.gate_status)
        self.gate_badge.setText(gate)
        self.gate_badge.setStyleSheet(badge_style(tone_for_value(gate)))
        self.gate_summary.setText(_summary_message(item))
        self.gate_detail.setPlainText(_gate_detail_text(item))

    def _render_conditions(self, state: ThemeLabDashboardState) -> None:
        if not state.condition_statuses:
            self.condition_view.setPlainText("조건식 상태 데이터 없음\n테마랩_생존_-1 / 테마랩_강세_3 / 테마랩_주도_5 등록 상태를 확인할 수 없습니다.")
            return
        lines = []
        for item in state.condition_statuses:
            status = "정상" if item.registered else "미등록"
            warning = f" / {item.warning}" if item.warning else ""
            lines.append(
                f"{item.condition_name} [{item.purpose}] {status}\n"
                f"  index={item.resolved_index} screen={item.screen_no or 'UNKNOWN'} "
                f"include={item.include_count} remove={item.remove_count} last={item.last_event_at or '-'}{warning}"
            )
        self.condition_view.setPlainText("\n\n".join(lines))

    def _render_data_quality(self, state: ThemeLabDashboardState) -> None:
        dq = state.data_quality
        self.data_quality_view.setPlainText(
            "\n".join(
                [
                    f"상태: {dq.status}",
                    f"quote stale: {dq.quote_stale_count}",
                    f"전일종가 누락: {dq.prev_close_missing_count}",
                    f"분봉 누락: {dq.candle_missing_count}",
                    f"VWAP 누락: {dq.vwap_missing_count}",
                    f"session high 누락: {dq.session_high_missing_count}",
                    f"VI 상태: {'지원' if dq.vi_status_supported else '미지원'}",
                    f"테마 매핑 누락: {dq.theme_mapping_missing_count}",
                    f"WatchSet: {dq.watchset_size}",
                    f"실시간 구독: {dq.realtime_subscription_count} / {dq.realtime_subscription_limit or 'UNKNOWN'}",
                ]
            )
        )


class CandleChartPlaceholder(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("candleChart")
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        self.title = QLabel("분봉 데이터 없음")
        self.title.setObjectName("chartEmptyTitle")
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(self.title, alignment=Qt.AlignCenter)
        layout.addWidget(self.notes, alignment=Qt.AlignCenter)
        layout.addStretch(1)

    def set_chart(self, *, symbol: str, candles: Iterable[Any], status: str, missing_notes: Iterable[str]) -> None:
        candles = tuple(candles or ())
        if status == CHART_STATUS_READY and candles:
            self.title.setText(f"{symbol} 캔들 {len(candles)}개 수신")
            self.notes.setText("CandleChart abstraction: 실제 OHLC 렌더러 연결 지점입니다. VWAP/전일종가/마커는 데이터가 있을 때만 표시합니다.")
            return
        title_by_status = {
            CHART_STATUS_QUOTE_ONLY: "실시간 현재가만 수신 중, 분봉 데이터 없음",
            CHART_STATUS_STALE: "오래된 분봉 데이터",
            CHART_STATUS_UNSUPPORTED: "차트 미지원",
        }
        self.title.setText(title_by_status.get(status, "분봉 데이터 없음"))
        self.notes.setText(" / ".join(missing_notes) or "없는 데이터는 UNKNOWN / 미지원 / 데이터 없음으로 표시합니다.")


class _BaseTableModel(QAbstractTableModel):
    headers: list[str] = []

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[Any] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def set_rows(self, rows: list[Any]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> Any | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


class ThemeRankTableModel(_BaseTableModel):
    headers = ["#", "테마", "상태", "생존", "강세", "주도", "대금", "대장"]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        item = self.row_at(index.row())
        if item is None:
            return None
        if role in {Qt.DisplayRole, Qt.ToolTipRole}:
            values = [
                index.row() + 1,
                item.theme_name,
                _enum_value(item.theme_status),
                f"{item.alive_count}/{item.eligible_total_members}",
                f"{item.strong_count} ({item.strong_ratio:.0%})",
                f"{item.leader_count} ({item.leader_ratio:.0%})",
                format_money(item.theme_turnover_krw),
                item.top_leader_name or item.top_leader_symbol or "-",
            ]
            return str(values[index.column()])
        if role == Qt.ForegroundRole and index.column() == 2:
            return QBrush(QColor(THEME_LAB_COLORS.get(tone_for_value(_enum_value(item.theme_status)), THEME_LAB_COLORS["text_primary"])))
        return None


class ThemeLabWatchTableModel(_BaseTableModel):
    StatusRole = Qt.UserRole + 200
    headers = ["상태", "코드", "종목", "테마", "역할", "등락", "대금", "조건", "가격위치", "리스크", "비중", "재확인", "사유"]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        item = self.row_at(index.row())
        if item is None:
            return None
        gate = _enum_value(item.final_gate_status or item.gate_status)
        if role in {Qt.DisplayRole, Qt.ToolTipRole}:
            values = [
                gate,
                item.symbol,
                item.name,
                item.primary_theme,
                _enum_value(item.stock_role),
                format_percent(item.return_pct),
                format_money(item.turnover_krw),
                item.condition_level,
                _enum_value(item.price_location_status),
                _enum_value(item.risk_level),
                f"{item.position_size_multiplier:.2g}배",
                f"{item.recheck_after_sec}s" if item.recheck_after_sec else "-",
                _summary_message(item),
            ]
            return str(values[index.column()])
        if role == self.StatusRole:
            return gate
        if role == Qt.ForegroundRole and index.column() in {0, 9}:
            return QBrush(QColor(THEME_LAB_COLORS.get(tone_for_value(gate), THEME_LAB_COLORS["text_primary"])))
        if role == Qt.TextAlignmentRole and index.column() in {5, 6, 7, 10, 11}:
            return Qt.AlignRight | Qt.AlignVCenter
        return None


class ThemeLabWatchFilterProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self._status_filter = "ALL"

    def set_status_filter(self, status: str) -> None:
        self._status_filter = status
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._status_filter == "ALL":
            return True
        model = self.sourceModel()
        if model is None:
            return True
        status = model.data(model.index(source_row, 0, source_parent), ThemeLabWatchTableModel.StatusRole)
        return status == self._status_filter


class ThemeLabOrderTableModel(_BaseTableModel):
    headers = ["우선", "코드", "종목", "테마", "역할", "상태", "비중", "진입 기준", "손절 기준", "사유"]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        item = self.row_at(index.row())
        if item is None:
            return None
        if role in {Qt.DisplayRole, Qt.ToolTipRole}:
            values = [
                index.row() + 1,
                item.symbol,
                item.name,
                item.primary_theme,
                _enum_value(item.stock_role),
                _enum_value(item.final_gate_status or item.gate_status),
                f"{item.position_size_multiplier:.2g}배",
                _entry_reference(item),
                _stop_reference(item),
                _summary_message(item),
            ]
            return str(values[index.column()])
        if role == Qt.TextAlignmentRole and index.column() in {0, 6}:
            return Qt.AlignRight | Qt.AlignVCenter
        return None


def _build_chart_universe(
    result: ThemeLabFlowResult,
    candle_map: dict[str, tuple[Any, ...]],
    quote_only: set[str],
    position_set: set[str],
    *,
    selected_theme_id: str,
    selected_symbol: str,
    config: DashboardChartConfig,
) -> tuple[ChartUniverseItem, ...]:
    if not config.enabled:
        return ()
    items: dict[str, ChartUniverseItem] = {}

    def add(symbol: str, name: str, item_type: str, reason: str, priority: int) -> None:
        if not symbol or len(items) >= config.max_chart_symbols:
            return
        status = _chart_status(symbol, candle_map, quote_only)
        has_candle = status == CHART_STATUS_READY
        existing = items.get(symbol)
        if existing is None or priority < existing.priority:
            items[symbol] = ChartUniverseItem(symbol, name or symbol, item_type, reason, priority, has_candle, status, _last_candle_at(candle_map.get(symbol, ())))

    if config.include_indices:
        add("KOSPI", "KOSPI", "index", "INDEX", 10)
        add("KOSDAQ", "KOSDAQ", "index", "INDEX", 11)
    if config.include_ready_candidates:
        for watch in result.watchset:
            gate = _enum_value(watch.final_gate_status or watch.gate_status)
            if gate in {"READY", "READY_SMALL"}:
                add(watch.symbol, watch.name, "stock", gate, 20 if gate == "READY" else 30)
    if config.include_positions:
        for symbol in sorted(position_set):
            watch = _watch_by_symbol(result.watchset, symbol)
            add(symbol, watch.name if watch else symbol, "stock", "POSITION", 40)
    count = 0
    for watch in _sorted_watchset(result.watchset):
        if count >= config.max_watchset_chart_symbols:
            break
        if watch.condition_level <= 1 and _enum_value(watch.final_gate_status or watch.gate_status) == "OBSERVE":
            continue
        if _enum_value(watch.final_gate_status or watch.gate_status) == "BLOCKED" and not watch.recheck_after_sec:
            continue
        if watch.symbol in items:
            continue
        add(watch.symbol, watch.name, "stock", "WATCHSET", 50 + count)
        count += 1
    for theme in result.themes:
        if theme.theme_name != selected_theme_id:
            continue
        leader_symbols = [theme.top_leader_symbol]
        for watch in result.watchset:
            if watch.primary_theme == theme.theme_name and _enum_value(watch.stock_role) in {"LEADER", "CO_LEADER"}:
                leader_symbols.append(watch.symbol)
        for offset, symbol in enumerate(dict.fromkeys(leader_symbols)):
            watch = _watch_by_symbol(result.watchset, symbol)
            add(symbol, watch.name if watch else symbol, "stock", "THEME_LEADER", 60 + offset)
    if selected_symbol and _chart_status(selected_symbol, candle_map, quote_only) == CHART_STATUS_READY:
        watch = _watch_by_symbol(result.watchset, selected_symbol)
        add(selected_symbol, watch.name if watch else selected_symbol, "stock", "USER_SELECTED", 5)
    return tuple(sorted(items.values(), key=lambda item: (item.priority, item.symbol)))


def _data_quality_from_result(result: ThemeLabFlowResult) -> ThemeLabDataQuality:
    raw = dict(result.data_quality or {})
    status = str(raw.get("status") or "OK")
    counts = {
        "quote_stale_count": int(raw.get("quote_stale_count") or 0),
        "prev_close_missing_count": int(raw.get("prev_close_missing_count") or raw.get("missing_prev_close_count") or 0),
        "candle_missing_count": int(raw.get("candle_missing_count") or 0),
        "vwap_missing_count": int(raw.get("vwap_missing_count") or 0),
        "session_high_missing_count": int(raw.get("session_high_missing_count") or 0),
        "theme_mapping_missing_count": int(raw.get("theme_mapping_missing_count") or 0),
    }
    if status == "OK" and any(counts[key] for key in counts):
        status = "WARNING"
    if counts["candle_missing_count"] or counts["quote_stale_count"] >= 5:
        status = "DEGRADED"
    return ThemeLabDataQuality(
        status=status,
        vi_status_supported=bool(raw.get("vi_status_supported", False)),
        watchset_size=len(result.watchset),
        realtime_subscription_count=int(raw.get("realtime_subscription_count") or 0),
        realtime_subscription_limit=int(raw.get("realtime_subscription_limit") or 0),
        **counts,
    )


def _build_chart_markers(watchset: Iterable[Any]) -> dict[str, tuple[ChartMarker, ...]]:
    markers: dict[str, list[ChartMarker]] = {}
    for item in watchset:
        ts = getattr(item, "calculated_at", "")
        if not ts:
            continue
        gate = _enum_value(item.final_gate_status or item.gate_status)
        if gate in {"READY", "READY_SMALL", "WAIT", "BLOCKED"}:
            markers.setdefault(item.symbol, []).append(ChartMarker(ts, gate, gate, "gate"))
    return {symbol: tuple(values) for symbol, values in markers.items()}


def _result_from_state(state: ThemeLabDashboardState) -> ThemeLabFlowResult | None:
    if state.market is None:
        return None
    return ThemeLabFlowResult(
        market=state.market,
        themes=state.ranked_themes,
        watchset=state.watchset,
        gate_decisions=state.gate_decisions,
        data_quality={
            "status": state.data_quality.status,
            "quote_stale_count": state.data_quality.quote_stale_count,
            "prev_close_missing_count": state.data_quality.prev_close_missing_count,
            "candle_missing_count": state.data_quality.candle_missing_count,
            "vwap_missing_count": state.data_quality.vwap_missing_count,
            "session_high_missing_count": state.data_quality.session_high_missing_count,
            "vi_status_supported": int(state.data_quality.vi_status_supported),
            "theme_mapping_missing_count": state.data_quality.theme_mapping_missing_count,
            "realtime_subscription_count": state.data_quality.realtime_subscription_count,
            "realtime_subscription_limit": state.data_quality.realtime_subscription_limit,
        },
    )


def _sorted_watchset(watchset: Iterable[Any]) -> list[Any]:
    order = {"READY": 0, "READY_SMALL": 1, "WAIT": 2, "OBSERVE": 3, "BLOCKED": 4}
    role_order = {"LEADER": 0, "CO_LEADER": 1, "FOLLOWER": 2, "LATE_LAGGARD": 3, "WEAK_MEMBER": 4, "OVERHEATED": 5}
    return sorted(
        watchset,
        key=lambda item: (
            order.get(_enum_value(item.final_gate_status or item.gate_status), 9),
            role_order.get(_enum_value(item.stock_role), 9),
            -float(item.turnover_krw or 0),
            -float(item.price_location_score or 0),
        ),
    )


def _default_selected_symbol(result: ThemeLabFlowResult, candle_map: dict[str, tuple[Any, ...]]) -> str:
    for gate in (LabGateStatus.READY, LabGateStatus.READY_SMALL):
        for item in _sorted_watchset(result.watchset):
            if (item.final_gate_status or item.gate_status) == gate:
                return item.symbol
    for theme in result.themes:
        if _enum_value(theme.theme_status) == "LEADING_THEME" and theme.top_leader_symbol:
            return theme.top_leader_symbol
    return "KOSDAQ" if "KOSDAQ" in candle_map or True else "KOSPI"


def _select_chart_item(items: Iterable[ChartUniverseItem], selected_symbol: str) -> ChartUniverseItem | None:
    items = tuple(items)
    if selected_symbol:
        for item in items:
            if item.symbol == selected_symbol:
                return item
    for status in ("READY", "READY_SMALL", "THEME_LEADER", "INDEX"):
        for item in items:
            if item.reason == status and item.chart_data_status == CHART_STATUS_READY:
                return item
    for item in items:
        if item.symbol == "KOSDAQ":
            return item
    return items[0] if items else None


def _chart_status(symbol: str, candle_map: dict[str, tuple[Any, ...]], quote_only: set[str]) -> str:
    if symbol in candle_map and len(candle_map[symbol]) > 0:
        return CHART_STATUS_READY
    if symbol in quote_only:
        return CHART_STATUS_QUOTE_ONLY
    return CHART_STATUS_NO_CANDLE


def _last_candle_at(candles: Iterable[Any]) -> str:
    candles = tuple(candles or ())
    if not candles:
        return ""
    last = candles[-1]
    if isinstance(last, dict):
        return str(last.get("ts") or last.get("time") or last.get("datetime") or "")
    return str(getattr(last, "ts", "") or getattr(last, "time", "") or getattr(last, "datetime", ""))


def _watch_by_symbol(watchset: Iterable[Any], symbol: str) -> Any | None:
    if not symbol:
        return None
    for item in watchset:
        if item.symbol == symbol:
            return item
    return None


def _summary_message(item: Any) -> str:
    gate = _enum_value(item.final_gate_status or item.gate_status)
    role = _enum_value(item.stock_role)
    location = _enum_value(item.price_location_status)
    reasons = tuple(item.risk_reason_codes or item.price_location_reason_codes or ())
    if gate == "READY_SMALL":
        return f"{role} 흐름은 유효하지만 {location} 기준으로 소액 진입, {item.position_size_multiplier:.2g}배 비중"
    if gate == "READY":
        return f"{role} / {location} 조건으로 진입 가능, {item.position_size_multiplier:.2g}배 비중"
    if gate == "WAIT":
        return f"{location} 또는 리스크 확인 필요로 WAIT" + (f" ({', '.join(reasons[:2])})" if reasons else "")
    if gate == "BLOCKED":
        return "진입 차단: " + (", ".join(reasons[:3]) if reasons else "리스크 필터 차단")
    return item.watch_reason or "관찰 단계"


def _gate_detail_text(item: Any) -> str:
    missing = _missing_notes(item, "")
    lines = [
        f"종목: {item.name or '-'} ({item.symbol})",
        f"테마: {item.primary_theme or 'UNKNOWN'} / 역할: {_enum_value(item.stock_role)}",
        f"등락률: {format_percent(item.return_pct)} / 거래대금: {format_money(item.turnover_krw)}",
        f"조건식 단계: {item.condition_level}",
        "",
        "[가격 위치]",
        f"상태: {_enum_value(item.price_location_status)} / 점수: {item.price_location_score:.1f}",
        f"고점 이격: {_optional_pct(item.distance_to_session_high_pct)}",
        f"VWAP 이격: {_optional_pct(item.vwap_gap_pct)}",
        f"상한가 이격: {_optional_pct(item.upper_limit_gap_pct)}",
        f"돌파 기준 이격: {_optional_pct(item.breakout_level_gap_pct)}",
        f"지지선 이격: {_optional_pct(item.support_gap_pct)}",
        "",
        "[리스크]",
        f"risk_level: {_enum_value(item.risk_level)}",
        f"reason: {', '.join(item.risk_reason_codes or item.price_location_reason_codes or ()) or '-'}",
        f"VI: {'활성' if item.vi_active else '미지원/비활성'}",
        f"VI 해제 후: {item.seconds_since_vi_release if item.seconds_since_vi_release else 'UNKNOWN'}",
        "",
        "[없는 데이터]",
        "\n".join(missing) if missing else "누락 플래그 없음",
    ]
    return "\n".join(lines)


def _missing_notes(item: Any | None, status: str) -> list[str]:
    notes: list[str] = []
    if status == CHART_STATUS_NO_CANDLE:
        notes.append("분봉 데이터 없음")
    if status == CHART_STATUS_QUOTE_ONLY:
        notes.append("실시간 현재가만 수신 중")
    if item is None:
        return notes
    flags = set(item.price_location_data_quality_flags or ())
    if item.vwap_gap_pct is None or "MISSING_VWAP" in flags:
        notes.append("VWAP 데이터 없음")
    if item.distance_to_session_high_pct is None or "MISSING_SESSION_HIGH" in flags:
        notes.append("session_high 데이터 없음")
    if not item.vi_active and not item.seconds_since_vi_release:
        notes.append("VI 미지원")
    return notes


def _entry_reference(item: Any) -> str:
    if item.breakout_level_gap_pct is not None:
        return f"돌파 기준 {item.breakout_level_gap_pct:+.2f}%"
    if item.vwap_gap_pct is not None:
        return f"VWAP {item.vwap_gap_pct:+.2f}%"
    return "UNKNOWN"


def _stop_reference(item: Any) -> str:
    if item.support_gap_pct is not None:
        return f"지지선 {item.support_gap_pct:+.2f}%"
    if item.pullback_from_high_pct is not None:
        return f"고점대비 {item.pullback_from_high_pct:.2f}%"
    return "UNKNOWN"


def _chart_name(state: ThemeLabDashboardState, symbol: str) -> str:
    if symbol in {"KOSPI", "KOSDAQ"}:
        return symbol
    watch = _watch_by_symbol(state.watchset, symbol)
    return watch.name if watch and watch.name else symbol


def _registered_count(items: Iterable[ThemeLabConditionStatus]) -> int:
    return sum(1 for item in items if item.registered)


def _optional_pct(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{float(value):+.2f}%"


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _format_time(value: datetime | None) -> str:
    return (value or datetime.now()).strftime("%H:%M:%S")


def _panel(title: str) -> QGroupBox:
    box = QGroupBox(title)
    box.setObjectName("themeLabPanel")
    return box


def _table(model) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    return table


def theme_lab_stylesheet() -> str:
    return f"""
    QWidget#themeLabDashboard {{
        background: {THEME_LAB_COLORS["app_bg"]};
        color: {THEME_LAB_COLORS["text_primary"]};
    }}
    QLabel#marketHeader {{
        background: {THEME_LAB_COLORS["panel_bg_alt"]};
        color: {THEME_LAB_COLORS["text_primary"]};
        border: 1px solid {THEME_LAB_COLORS["border_active"]};
        border-radius: 6px;
        padding: 10px 12px;
        font-weight: 700;
    }}
    QGroupBox#themeLabPanel {{
        background: {THEME_LAB_COLORS["panel_bg"]};
        border: 1px solid {THEME_LAB_COLORS["border"]};
        border-radius: 6px;
        margin-top: 18px;
        padding: 10px;
        font-weight: 700;
    }}
    QGroupBox#themeLabPanel::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {THEME_LAB_COLORS["text_secondary"]};
    }}
    QTableView {{
        background: {THEME_LAB_COLORS["panel_bg_alt"]};
        alternate-background-color: {THEME_LAB_COLORS["panel_bg"]};
        color: {THEME_LAB_COLORS["text_primary"]};
        gridline-color: {THEME_LAB_COLORS["border"]};
        border: 1px solid {THEME_LAB_COLORS["border"]};
        selection-background-color: {THEME_LAB_COLORS["panel_hover"]};
        selection-color: {THEME_LAB_COLORS["text_primary"]};
    }}
    QHeaderView::section {{
        background: {THEME_LAB_COLORS["panel_bg"]};
        color: {THEME_LAB_COLORS["text_secondary"]};
        border: 0;
        border-bottom: 1px solid {THEME_LAB_COLORS["border"]};
        padding: 6px;
        font-weight: 700;
    }}
    QTextEdit, QFrame#candleChart {{
        background: #090D12;
        color: {THEME_LAB_COLORS["text_primary"]};
        border: 1px solid {THEME_LAB_COLORS["border"]};
        border-radius: 6px;
    }}
    QLabel#chartTitle {{
        font-size: 15px;
        font-weight: 800;
        color: {THEME_LAB_COLORS["text_primary"]};
    }}
    QLabel#chartEmptyTitle {{
        font-size: 18px;
        font-weight: 800;
        color: {THEME_LAB_COLORS["text_secondary"]};
    }}
    QLabel#gateStatus {{
        font-size: 20px;
    }}
    QPushButton {{
        background: {THEME_LAB_COLORS["panel_bg_alt"]};
        color: {THEME_LAB_COLORS["text_secondary"]};
        border: 1px solid {THEME_LAB_COLORS["border"]};
        border-radius: 4px;
        padding: 6px 10px;
    }}
    QPushButton:checked {{
        color: {THEME_LAB_COLORS["text_primary"]};
        border-color: {THEME_LAB_COLORS["info"]};
        background: {THEME_LAB_COLORS["panel_hover"]};
    }}
    """
