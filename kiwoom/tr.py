from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TrResponseMeta:
    screen_no: str = ""
    rq_name: str = ""
    tr_code: str = ""
    record_name: str = ""
    prev_next: str = ""
    error_code: str = ""
    message: str = ""
    splm_msg: str = ""


@dataclass
class TrPage:
    meta: TrResponseMeta
    single: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class TrRequestResult:
    tr_code: str = ""
    rq_name: str = ""
    pages: list[TrPage] = field(default_factory=list)
    request_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    complete: bool = True
    parser_metadata: dict[str, object] = field(default_factory=dict)

    @property
    def rows(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for page in self.pages:
            result.extend(page.rows)
        return result

    @property
    def merged_rows(self) -> list[dict[str, str]]:
        return self.rows

    @property
    def merged_single(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for page in self.pages:
            for key, value in dict(page.single or {}).items():
                if str(value or "") or key not in merged:
                    merged[key] = str(value or "")
        return merged

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def prev_next_sequence(self) -> list[str]:
        return [str(page.meta.prev_next or "") for page in self.pages]


class KiwoomTrRunner:
    def __init__(
        self,
        client,
        *,
        request_delay_ms: int = 1200,
        timeout_sec: int = 20,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        process_events: Optional[Callable[[], None]] = None,
    ) -> None:
        self.client = client
        self.request_delay_ms = max(0, int(request_delay_ms))
        self.timeout_sec = max(1, int(timeout_sec))
        self.clock = clock
        self.sleeper = sleeper
        self.process_events = process_events or _process_qt_events
        self._pending_page: Optional[TrPage] = None
        self._active_request: Optional[dict] = None
        self._sent_request_count = 0
        signal = getattr(client, "tr_data_received", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(self._handle_tr_data)

    def request_pages(
        self,
        *,
        tr_code: str,
        rq_name: str,
        inputs: dict[str, object],
        fields: list[str],
        screen_no: str = "8700",
    ) -> TrRequestResult:
        result = TrRequestResult(tr_code=str(tr_code), rq_name=str(rq_name))
        prev_next = 0
        while True:
            if self._sent_request_count > 0 and self.request_delay_ms:
                self.sleeper(self.request_delay_ms / 1000.0)
            for input_name, value in inputs.items():
                self.client.set_input_value(str(input_name), str(value))
            page = self._request_page(result, tr_code, rq_name, prev_next, screen_no, fields)
            if page is None:
                break
            result.pages.append(page)
            if str(page.meta.prev_next).strip() != "2":
                break
            prev_next = 2
        if not result.pages:
            result.warnings.append(f"TR_EMPTY:{tr_code}:{rq_name}")
            result.complete = False
        return result

    def request_capture(
        self,
        *,
        tr_code: str,
        rq_name: str,
        inputs: dict[str, object],
        single_fields: list[str] | tuple[str, ...] | None = None,
        multi_fields: list[str] | tuple[str, ...] | None = None,
        screen_no: str = "8700",
        max_pages: int = 20,
    ) -> TrRequestResult:
        result = TrRequestResult(
            tr_code=str(tr_code),
            rq_name=str(rq_name),
            parser_metadata={
                "capture_version": "kiwoom_tr_capture_v2",
                "single_fields": list(single_fields or []),
                "multi_fields": list(multi_fields or []),
                "max_pages": max(1, int(max_pages or 20)),
            },
        )
        prev_next = 0
        while result.page_count < max(1, int(max_pages or 20)):
            if self._sent_request_count > 0 and self.request_delay_ms:
                self.sleeper(self.request_delay_ms / 1000.0)
            for input_name, value in inputs.items():
                self.client.set_input_value(str(input_name), str(value))
            page = self._request_page(
                result,
                tr_code,
                rq_name,
                prev_next,
                screen_no,
                list(multi_fields or []),
                single_fields=list(single_fields or []),
            )
            if page is None:
                result.complete = False
                break
            result.pages.append(page)
            if str(page.meta.prev_next).strip() != "2":
                break
            prev_next = 2
        else:
            result.complete = False
            result.warnings.append(f"TR_MAX_PAGES_REACHED:{tr_code}:{rq_name}:{max_pages}")
        if not result.pages:
            result.warnings.append(f"TR_EMPTY:{tr_code}:{rq_name}")
            result.complete = False
        return result

    def _request_page(
        self,
        result: TrRequestResult,
        tr_code: str,
        rq_name: str,
        prev_next: int,
        screen_no: str,
        fields: list[str],
        single_fields: list[str] | None = None,
    ) -> Optional[TrPage]:
        self._pending_page = None
        self._active_request = {
            "result": result,
            "tr_code": tr_code,
            "rq_name": rq_name,
            "fields": list(fields),
            "single_fields": list(single_fields or []),
        }
        result_code = int(self.client.comm_rq_data(str(rq_name), str(tr_code), int(prev_next), str(screen_no)) or 0)
        result.request_count += 1
        self._sent_request_count += 1
        if result_code < 0:
            result.errors.append(f"TR_REQUEST_FAILED:{tr_code}:{rq_name}:{result_code}")
            self._active_request = None
            return None

        deadline = self.clock() + self.timeout_sec
        while self._pending_page is None and self.clock() < deadline:
            self.process_events()
            if self._pending_page is None:
                self.sleeper(0.01)
        if self._pending_page is None:
            result.errors.append(f"TR_TIMEOUT:{tr_code}:{rq_name}:{prev_next}")
            self._active_request = None
            return None
        page = self._pending_page
        self._pending_page = None
        self._active_request = None
        meta = page.meta
        if meta.tr_code and str(meta.tr_code).lower() != str(tr_code).lower():
            result.warnings.append(f"TR_CODE_MISMATCH:{tr_code}:{meta.tr_code}")
        if str(meta.error_code or "").strip() not in {"", "0"}:
            result.errors.append(f"TR_RESPONSE_ERROR:{tr_code}:{rq_name}:{meta.error_code}:{meta.message}")
        return page

    def _extract_rows(self, result: TrRequestResult, meta: TrResponseMeta, fields: list[str]) -> list[dict[str, str]]:
        record_name, repeat_count, record_candidates = self._repeat_count(result, meta)
        rows: list[dict[str, str]] = []
        for index in range(max(0, repeat_count)):
            row: dict[str, str] = {}
            for field_name in fields:
                try:
                    value = self.client.get_comm_data(
                        meta.tr_code or result.tr_code,
                        record_name,
                        index,
                        field_name,
                    )
                except Exception as exc:
                    result.warnings.append(f"TR_FIELD_READ_FAILED:{result.tr_code}:{field_name}:{exc}")
                    value = ""
                row[field_name] = str(value or "").strip()
            rows.append(row)
        if not rows:
            single_row = self._extract_single_row(result, meta, fields, record_candidates)
            if single_row:
                result.warnings.append(
                    f"TR_SINGLE_ROW_FALLBACK:{result.tr_code}:{result.rq_name}:"
                    f"record={single_row.get('_record_name') or '-'}"
                )
                single_row.pop("_record_name", None)
                return [single_row]
        if not rows:
            result.warnings.append(
                f"TR_PAGE_EMPTY:{result.tr_code}:{result.rq_name}:record={record_name or '-'}:"
                f"event_record={meta.record_name or '-'}"
            )
        return rows

    def _extract_single_row(
        self,
        result: TrRequestResult,
        meta: TrResponseMeta,
        fields: list[str],
        record_candidates: list[str],
    ) -> dict[str, str]:
        tr_code = meta.tr_code or result.tr_code
        for record_name in record_candidates:
            row: dict[str, str] = {}
            for field_name in fields:
                try:
                    value = self.client.get_comm_data(tr_code, record_name, 0, field_name)
                except Exception as exc:
                    result.warnings.append(f"TR_SINGLE_FIELD_READ_FAILED:{result.tr_code}:{record_name}:{field_name}:{exc}")
                    value = ""
                row[field_name] = str(value or "").strip()
            if any(value for value in row.values()):
                row["_record_name"] = record_name
                return row
        return {}

    def _repeat_count(self, result: TrRequestResult, meta: TrResponseMeta) -> tuple[str, int, list[str]]:
        tr_code = meta.tr_code or result.tr_code
        candidates = _dedupe(
            [
                meta.rq_name,
                result.rq_name,
                meta.record_name,
                result.tr_code,
                str(result.tr_code).upper(),
                str(result.tr_code).lower(),
            ]
        )
        failures: list[str] = []
        for record_name in candidates:
            try:
                repeat_count = int(self.client.get_repeat_count(tr_code, record_name) or 0)
            except Exception as exc:
                failures.append(f"{record_name}:{exc}")
                continue
            if repeat_count > 0:
                return record_name, repeat_count, candidates
        if failures:
            result.errors.append(f"TR_REPEAT_COUNT_FAILED:{result.tr_code}:{result.rq_name}:{';'.join(failures)}")
        return candidates[0] if candidates else result.rq_name, 0, candidates

    def _extract_single(self, result: TrRequestResult, meta: TrResponseMeta, fields: list[str]) -> dict[str, str]:
        if not fields:
            return {}
        tr_code = meta.tr_code or result.tr_code
        record_candidates = _dedupe([meta.rq_name, result.rq_name, meta.record_name, result.tr_code, str(result.tr_code).upper(), str(result.tr_code).lower()])
        single: dict[str, str] = {}
        record_name = record_candidates[0] if record_candidates else result.rq_name
        for field_name in fields:
            value = ""
            last_error = ""
            for candidate in record_candidates:
                try:
                    raw = self.client.get_comm_data(tr_code, candidate, 0, field_name)
                    value = str(raw or "").strip()
                    record_name = candidate
                    if value:
                        break
                except Exception as exc:
                    last_error = str(exc)
                    continue
            if last_error and not value:
                result.warnings.append(f"TR_SINGLE_FIELD_READ_FAILED:{result.tr_code}:{record_name}:{field_name}:{last_error}")
            single[field_name] = value
        return single

    def _handle_tr_data(self, *args) -> None:
        values = list(args) + [""] * 9
        meta = TrResponseMeta(
            screen_no=str(values[0] or ""),
            rq_name=str(values[1] or ""),
            tr_code=str(values[2] or ""),
            record_name=str(values[3] or ""),
            prev_next=str(values[4] or ""),
            error_code=str(values[6] or ""),
            message=str(values[7] or ""),
            splm_msg=str(values[8] or ""),
        )
        request = self._active_request
        if request is None:
            self._pending_page = TrPage(meta=meta, rows=[])
            return
        rows = self._extract_rows(request["result"], meta, request["fields"])
        single = self._extract_single(request["result"], meta, request.get("single_fields") or [])
        self._pending_page = TrPage(meta=meta, single=single, rows=rows)


def _process_qt_events() -> None:
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def _dedupe(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
