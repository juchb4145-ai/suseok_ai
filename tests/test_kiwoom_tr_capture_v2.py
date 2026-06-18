from kiwoom.tr import KiwoomTrRunner
from trading.broker.models import Signal


class FakeClient:
    def __init__(self):
        self.tr_data_received = Signal()
        self.inputs = []
        self.requests = 0

    def set_input_value(self, key, value):
        self.inputs.append((key, value))

    def comm_rq_data(self, rq_name, tr_code, prev_next, screen_no):
        self.requests += 1
        next_flag = "2" if self.requests == 1 else "0"
        self.tr_data_received.emit(screen_no, rq_name, tr_code, rq_name, next_flag, "", "0", "", "")
        return 0

    def get_repeat_count(self, tr_code, record_name):
        return 1

    def get_comm_data(self, tr_code, record_name, index, field_name):
        values = {
            "예수금": "1,000,000",
            "주문번호": f"OID-{self.requests}",
            "종목코드": "A005930",
            "미체결수량": str(self.requests),
        }
        return values.get(field_name, "")


def test_request_capture_preserves_single_multi_and_pagination():
    runner = KiwoomTrRunner(FakeClient(), request_delay_ms=0, timeout_sec=1, process_events=lambda: None)

    result = runner.request_capture(
        tr_code="opw00001",
        rq_name="예수금상세현황요청",
        inputs={"계좌번호": "ACC_TOKEN_ONLY"},
        single_fields=["예수금"],
        multi_fields=["주문번호", "종목코드", "미체결수량"],
        max_pages=2,
    )

    assert result.complete is True
    assert result.page_count == 2
    assert result.prev_next_sequence == ["2", "0"]
    assert result.merged_single["예수금"] == "1,000,000"
    assert [row["주문번호"] for row in result.merged_rows] == ["OID-1", "OID-2"]


def test_request_capture_marks_incomplete_on_max_page_limit():
    runner = KiwoomTrRunner(FakeClient(), request_delay_ms=0, timeout_sec=1, process_events=lambda: None)

    result = runner.request_capture(
        tr_code="opt10075",
        rq_name="실시간미체결요청",
        inputs={},
        multi_fields=["주문번호"],
        max_pages=1,
    )

    assert result.complete is False
    assert any("TR_MAX_PAGES_REACHED" in warning for warning in result.warnings)

