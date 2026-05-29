from __future__ import annotations

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QMainWindow, QSplitter, QTableView


ORG_NAME = "suseok_ai"
APP_NAME = "ui"


def settings() -> QSettings:
    return QSettings(ORG_NAME, APP_NAME)


def save_window_state(store: QSettings, window: QMainWindow, key: str = "main_window") -> None:
    try:
        store.setValue(f"{key}/geometry", window.saveGeometry())
        store.setValue(f"{key}/state", window.saveState())
    except Exception:
        return


def restore_window_state(store: QSettings, window: QMainWindow, key: str = "main_window") -> None:
    try:
        geometry = store.value(f"{key}/geometry")
        if geometry:
            window.restoreGeometry(geometry)
        state = store.value(f"{key}/state")
        if state:
            window.restoreState(state)
    except Exception:
        return


def save_splitter_state(store: QSettings, splitter: QSplitter, key: str) -> None:
    try:
        store.setValue(f"{key}/state", splitter.saveState())
        store.setValue(f"{key}/sizes", splitter.sizes())
    except Exception:
        return


def restore_splitter_state(store: QSettings, splitter: QSplitter, key: str, default_sizes: list[int] | None = None) -> None:
    try:
        state = store.value(f"{key}/state")
        if state and splitter.restoreState(state):
            return
        sizes = store.value(f"{key}/sizes", default_sizes or [])
        if isinstance(sizes, list) and sizes:
            splitter.setSizes([int(size) for size in sizes])
        elif default_sizes:
            splitter.setSizes(default_sizes)
    except Exception:
        if default_sizes:
            splitter.setSizes(default_sizes)


def save_table_state(store: QSettings, table: QTableView, key: str) -> None:
    try:
        header = table.horizontalHeader()
        model = table.model()
        column_count = model.columnCount() if model is not None else header.count()
        store.setValue(f"{key}/header_state", header.saveState())
        store.setValue(f"{key}/column_widths", [table.columnWidth(column) for column in range(column_count)])
        store.setValue(f"{key}/sort_column", int(header.sortIndicatorSection()))
        store.setValue(f"{key}/sort_order", int(header.sortIndicatorOrder()))
    except Exception:
        return


def restore_table_state(
    store: QSettings,
    table: QTableView,
    key: str,
    default_widths: list[int] | None = None,
    *,
    default_sort_column: int | None = None,
    default_sort_order: Qt.SortOrder = Qt.AscendingOrder,
) -> None:
    try:
        header = table.horizontalHeader()
        header_state = store.value(f"{key}/header_state")
        restored_header = bool(header_state and header.restoreState(header_state))
        widths = store.value(f"{key}/column_widths", default_widths or [])
        if not restored_header and isinstance(widths, list) and widths:
            for column, width in enumerate(widths):
                try:
                    table.setColumnWidth(column, int(width))
                except (TypeError, ValueError):
                    if default_widths and column < len(default_widths):
                        table.setColumnWidth(column, int(default_widths[column]))
        elif not restored_header and default_widths:
            for column, width in enumerate(default_widths):
                table.setColumnWidth(column, int(width))
        sort_column = _int_value(store.value(f"{key}/sort_column"), default_sort_column)
        sort_order = _int_value(store.value(f"{key}/sort_order"), int(default_sort_order))
        if sort_column is not None and sort_column >= 0 and table.model() is not None:
            if sort_column < table.model().columnCount():
                order = Qt.DescendingOrder if sort_order == int(Qt.DescendingOrder) else Qt.AscendingOrder
                table.sortByColumn(sort_column, order)
    except Exception:
        if default_widths:
            for column, width in enumerate(default_widths):
                table.setColumnWidth(column, int(width))


def _int_value(value, fallback: int | None) -> int | None:
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
