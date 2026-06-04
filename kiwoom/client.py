from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from trading.broker.models import (
    BrokerConditionEvent as ConditionCandidateEvent,
    BrokerExecutionEvent as ExecutionEvent,
    BrokerOrderRequest as OrderRequest,
    BrokerOrderResult as OrderResult,
    BrokerPriceTick,
    ConditionInfo,
    ConditionLoadState,
    Signal,
)


FID_CURRENT_PRICE = 10
FID_CHANGE_RATE = 12
FID_ACC_VOLUME = 13
FID_ACC_TRADE_VALUE = 14
FID_OPEN_PRICE = 16
FID_HIGH_PRICE = 17
FID_LOW_PRICE = 18
FID_TRADE_TIME = 20
FID_BEST_ASK = 27
FID_BEST_BID = 28
FID_EXECUTION_STRENGTH = 228

REALTIME_STOCK_FIDS = [
    FID_CURRENT_PRICE,
    FID_CHANGE_RATE,
    FID_ACC_VOLUME,
    FID_ACC_TRADE_VALUE,
    FID_OPEN_PRICE,
    FID_HIGH_PRICE,
    FID_LOW_PRICE,
    FID_TRADE_TIME,
    FID_BEST_ASK,
    FID_BEST_BID,
    FID_EXECUTION_STRENGTH,
]


ERROR_MESSAGES = {
    0: "정상처리",
    -10: "실패",
    -100: "사용자정보교환실패",
    -101: "서버접속실패",
    -102: "버전처리실패",
    -103: "개인방화벽실패",
    -104: "메모리보호실패",
    -105: "함수입력값오류",
    -106: "통신연결종료",
    -200: "시세조회과부하",
    -201: "전문작성초기화실패",
    -202: "전문작성입력값오류",
    -203: "데이터없음",
    -204: "조회가능한종목수초과",
    -205: "데이터수신실패",
    -206: "조회가능한FID수초과",
    -207: "실시간해제오류",
    -300: "주문 입력값오류",
    -301: "계좌비밀번호없음",
    -302: "타인계좌사용오류",
    -303: "주문가격 20억원 초과",
    -304: "주문가격 50억원 초과",
    -305: "주문수량 총발행주수 1% 초과",
    -306: "주문수량 총발행주수 3% 초과",
    -307: "주문전송실패",
    -308: "주문전송과부하",
    -309: "주문수량 300계약 초과",
    -310: "주문수량 500계약 초과",
    -340: "계좌정보없음",
    -500: "종목코드없음",
}


class KiwoomClient:
    def __init__(self) -> None:
        try:
            from PyQt5.QAxContainer import QAxWidget
        except ImportError as exc:
            raise RuntimeError("PyQt5.QAxContainer is required in 32bit Python.") from exc

        self.connected = Signal()
        self.price_received = Signal()
        self.price_tick_received = Signal()
        self.order_result = Signal()
        self.execution_received = Signal()
        self.message_received = Signal()
        self.condition_state_changed = Signal()
        self.condition_load_result = Signal()
        self.condition_loaded = Signal()
        self.condition_tr_received = Signal()
        self.condition_real_received = Signal()
        self.condition_candidate_included = Signal()
        self.condition_candidate_removed = Signal()
        self.tr_data_received = Signal()
        self.condition_load_state = ConditionLoadState.IDLE
        self._conditions: list[ConditionInfo] = []
        self._realtime_screen_codes: dict[str, set[str]] = {}

        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        if self.ocx.isNull():
            self.ocx = QAxWidget("KHOpenAPI.KHOpenAPICtrl.1")
        if self.ocx.isNull():
            raise RuntimeError("Kiwoom OpenAPI ActiveX control is not registered.")

        self.ocx.OnEventConnect.connect(self._on_event_connect)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)
        self.ocx.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.ocx.OnReceiveMsg.connect(self._on_receive_msg)
        self.ocx.OnReceiveConditionVer.connect(self._on_receive_condition_ver)
        self.ocx.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.ocx.OnReceiveRealCondition.connect(self._on_receive_real_condition)
        self.ocx.OnReceiveTrData.connect(self._on_receive_tr_data)

    def login(self) -> int:
        return int(self.ocx.dynamicCall("CommConnect()"))

    def get_accounts(self) -> list[str]:
        raw = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO") or ""
        return [account for account in str(raw).split(";") if account]

    def get_user_id(self) -> str:
        return str(self.ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID") or "")

    def get_code_name(self, code: str) -> str:
        return str(self.ocx.dynamicCall("GetMasterCodeName(QString)", code) or "")

    def get_master_last_price(self, code: str) -> str:
        try:
            return str(self.ocx.dynamicCall("GetMasterLastPrice(QString)", str(code)) or "").strip()
        except Exception:
            return ""

    def register_realtime(self, codes: Iterable[str], screen_no: Optional[str] = None) -> None:
        code_list = [code for code in codes if code]
        fids = realtime_stock_fid_string()
        screen_map = self._realtime_screen_code_map()
        for index in range(0, len(code_list), 100):
            chunk = code_list[index : index + 100]
            chunk_screen_no = screen_no or f"{5000 + index // 100:04d}"
            screen_codes = screen_map.setdefault(chunk_screen_no, set())
            opt_type = "1" if screen_codes else "0"
            result = self.ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                chunk_screen_no,
                ";".join(chunk),
                fids,
                opt_type,
            )
            if int(result or 0) < 0:
                raise RuntimeError(f"실시간 등록 실패: {ERROR_MESSAGES.get(int(result), result)}")
            screen_codes.update(chunk)

    def remove_realtime(self, codes: Iterable[str], screen_no: Optional[str] = None) -> None:
        target_screen = screen_no or "ALL"
        screen_map = self._realtime_screen_code_map()
        for code in [code for code in codes if code]:
            self.ocx.dynamicCall("SetRealRemove(QString, QString)", target_screen, code)
            if target_screen == "ALL":
                for screen_codes in screen_map.values():
                    screen_codes.discard(code)
            else:
                screen_codes = screen_map.get(target_screen)
                if screen_codes is not None:
                    screen_codes.discard(code)
                    if not screen_codes:
                        screen_map.pop(target_screen, None)
        if target_screen == "ALL":
            self._realtime_screen_codes = {screen: codes for screen, codes in screen_map.items() if codes}

    def remove_all_realtime(self) -> None:
        self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        self._realtime_screen_code_map().clear()

    def _realtime_screen_code_map(self) -> dict[str, set[str]]:
        screen_map = getattr(self, "_realtime_screen_codes", None)
        if not isinstance(screen_map, dict):
            screen_map = {}
            self._realtime_screen_codes = screen_map
        return screen_map

    def load_conditions(self) -> int:
        self.condition_load_state = ConditionLoadState.LOADING
        self.condition_state_changed.emit(self.condition_load_state.value, "")
        result = int(self.ocx.dynamicCall("GetConditionLoad()") or 0)
        if result <= 0:
            self.condition_load_state = ConditionLoadState.FAILED
            self.condition_state_changed.emit(self.condition_load_state.value, "GetConditionLoad failed")
            self.condition_load_result.emit(False, "GetConditionLoad failed")
        return result

    def condition_name_list(self) -> list[ConditionInfo]:
        if self.condition_load_state != ConditionLoadState.LOADED:
            return []
        raw = str(self.ocx.dynamicCall("GetConditionNameList()") or "")
        self._conditions = parse_condition_name_list(raw)
        return list(self._conditions)

    def send_condition(
        self,
        screen_no: str,
        condition_name: str,
        condition_index: int,
        realtime: bool = True,
        search_type: Optional[int] = None,
    ) -> int:
        n_search = int(search_type if search_type is not None else (1 if realtime else 0))
        return int(
            self.ocx.dynamicCall(
                "SendCondition(QString, QString, int, int)",
                screen_no,
                condition_name,
                int(condition_index),
                n_search,
            )
            or 0
        )

    def stop_condition(self, screen_no: str, condition_name: str, condition_index: int) -> None:
        self.ocx.dynamicCall(
            "SendConditionStop(QString, QString, int)",
            screen_no,
            condition_name,
            int(condition_index),
        )

    def set_input_value(self, input_name: str, value: str) -> None:
        self.ocx.dynamicCall("SetInputValue(QString, QString)", str(input_name), str(value))

    def comm_rq_data(self, rq_name: str, tr_code: str, prev_next: int, screen_no: str) -> int:
        return int(
            self.ocx.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                str(rq_name),
                str(tr_code),
                int(prev_next),
                str(screen_no),
            )
            or 0
        )

    def get_repeat_count(self, tr_code: str, rq_name: str) -> int:
        return int(self.ocx.dynamicCall("GetRepeatCnt(QString, QString)", str(tr_code), str(rq_name)) or 0)

    def get_comm_data(self, tr_code: str, rq_name: str, index: int, item_name: str) -> str:
        value = self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            str(tr_code),
            str(rq_name),
            int(index),
            str(item_name),
        )
        return str(value or "").strip()

    def get_code_list_by_market(self, market_code: str) -> list[str]:
        raw = str(self.ocx.dynamicCall("GetCodeListByMarket(QString)", str(market_code)) or "")
        return [code.strip().replace("A", "") for code in raw.split(";") if code.strip()]

    def send_order(self, request: OrderRequest) -> OrderResult:
        result_code = int(
            self.ocx.dynamicCall(
                "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
                [
                    request.tag,
                    "0101",
                    request.account,
                    request.order_type,
                    request.code,
                    request.quantity,
                    request.price,
                    request.hoga,
                    request.original_order_no,
                ],
            )
        )
        result = OrderResult(
            ok=result_code == 0,
            code=result_code,
            message=ERROR_MESSAGES.get(result_code, str(result_code)),
            request=request,
        )
        self.order_result.emit(result)
        return result

    def cancel_order(self, account: str, code: str, quantity: int, original_order_no: str, tag: str) -> OrderResult:
        return self.send_order(
            OrderRequest(
                account=account,
                code=code,
                quantity=quantity,
                price=0,
                side="cancel_buy",
                tag=tag,
                order_type=3,
                hoga="00",
                original_order_no=original_order_no,
            )
        )

    def modify_buy_order(
        self,
        account: str,
        code: str,
        quantity: int,
        price: int,
        original_order_no: str,
        tag: str,
    ) -> OrderResult:
        return self.send_order(
            OrderRequest(
                account=account,
                code=code,
                quantity=quantity,
                price=price,
                side="modify_buy",
                tag=tag,
                order_type=5,
                hoga="00",
                original_order_no=original_order_no,
            )
        )

    def _on_event_connect(self, error_code: int) -> None:
        self.connected.emit(error_code == 0, int(error_code), ERROR_MESSAGES.get(int(error_code), str(error_code)))

    def _on_receive_msg(self, screen_no: str, rq_name: str, tr_code: str, message: str) -> None:
        self.message_received.emit(f"{screen_no} {rq_name} {tr_code}: {message}")

    def _on_receive_condition_ver(self, result: int, message: str) -> None:
        success = int(result) == 1
        self.condition_load_state = ConditionLoadState.LOADED if success else ConditionLoadState.FAILED
        self.condition_state_changed.emit(self.condition_load_state.value, str(message or ""))
        self.condition_load_result.emit(success, str(message or ""))
        if success:
            self.condition_loaded.emit(self.condition_name_list())

    def _on_receive_tr_condition(
        self,
        screen_no: str,
        code_list: str,
        condition_name: str,
        condition_index: int,
        next_flag: str,
    ) -> None:
        self.condition_tr_received.emit(
            str(screen_no or ""),
            str(code_list or ""),
            str(condition_name or ""),
            int(condition_index),
            str(next_flag or ""),
        )

    def _on_receive_real_condition(
        self,
        code: str,
        event_type: str,
        condition_name: str,
        condition_index: str,
    ) -> None:
        try:
            index = int(condition_index)
        except (TypeError, ValueError):
            index = -1
        self.condition_real_received.emit(str(code or ""), str(event_type or ""), str(condition_name or ""), index)

    def _on_receive_tr_data(
        self,
        screen_no: str,
        rq_name: str,
        tr_code: str,
        record_name: str,
        prev_next: str,
        data_length: int,
        error_code: str,
        message: str,
        splm_msg: str,
    ) -> None:
        self.tr_data_received.emit(
            str(screen_no or ""),
            str(rq_name or ""),
            str(tr_code or ""),
            str(record_name or ""),
            str(prev_next or ""),
            int(data_length or 0),
            str(error_code or ""),
            str(message or ""),
            str(splm_msg or ""),
        )

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str) -> None:
        raw_values = {fid: self._real_raw(code, fid) for fid in REALTIME_STOCK_FIDS}
        reason_codes: list[str] = []
        parse_fallback = False

        current, ok = _parse_real_int(raw_values.get(FID_CURRENT_PRICE))
        parse_fallback = parse_fallback or not ok
        change_rate, ok = _parse_real_float(raw_values.get(FID_CHANGE_RATE), abs_value=False)
        parse_fallback = parse_fallback or not ok
        volume, ok = _parse_real_int(raw_values.get(FID_ACC_VOLUME))
        parse_fallback = parse_fallback or not ok
        trade_value, ok = _parse_real_float(raw_values.get(FID_ACC_TRADE_VALUE), abs_value=True)
        parse_fallback = parse_fallback or not ok
        trade_value_unit = ""
        if trade_value > 0:
            # Kiwoom FID 14 (누적거래대금) is delivered in million KRW units.
            trade_value *= 1_000_000
            trade_value_unit = "million_krw"
        open_price, ok = _parse_real_int(raw_values.get(FID_OPEN_PRICE))
        parse_fallback = parse_fallback or not ok
        day_high, ok = _parse_real_int(raw_values.get(FID_HIGH_PRICE))
        parse_fallback = parse_fallback or not ok
        day_low, ok = _parse_real_int(raw_values.get(FID_LOW_PRICE))
        parse_fallback = parse_fallback or not ok
        best_ask, ok = _parse_real_int(raw_values.get(FID_BEST_ASK))
        parse_fallback = parse_fallback or not ok
        best_bid, ok = _parse_real_int(raw_values.get(FID_BEST_BID))
        parse_fallback = parse_fallback or not ok
        execution_strength, ok = _parse_real_float(raw_values.get(FID_EXECUTION_STRENGTH), abs_value=False)
        parse_fallback = parse_fallback or not ok
        execution_strength = max(0.0, execution_strength)
        trade_time = str(raw_values.get(FID_TRADE_TIME) or "").strip()

        if trade_value <= 0:
            reason_codes.append("TRADE_VALUE_MISSING")
            if current > 0 and volume > 0:
                trade_value = float(current * volume)
                reason_codes.append("TURNOVER_ESTIMATED")
        if execution_strength <= 0:
            reason_codes.append("EXECUTION_STRENGTH_MISSING")
        if day_high <= 0 or day_low <= 0:
            reason_codes.append("DAY_HIGH_LOW_MISSING")
        if best_ask <= 0 or best_bid <= 0:
            reason_codes.append("BEST_BID_ASK_MISSING")
        if parse_fallback:
            reason_codes.append("REAL_PARSE_FALLBACK")

        spread_price = max(0, best_ask - best_bid) if best_ask > 0 and best_bid > 0 else 0
        spread_ticks = _spread_ticks(best_bid, best_ask)
        if spread_price > 0:
            reason_codes.append("SPREAD_APPROXIMATED")

        metadata = {
            "real_type": str(real_type or ""),
            "trade_time": trade_time,
            "raw_fids_present": [fid for fid, value in raw_values.items() if _has_real_value(value)],
            "reason_codes": sorted(set(reason_codes)),
            "spread_price": spread_price,
        }
        if trade_value_unit:
            metadata["trade_value_unit"] = trade_value_unit
        self.price_received.emit(code, current, change_rate, volume, best_ask, best_bid)
        self.price_tick_received.emit(
            BrokerPriceTick(
                code=str(code or ""),
                price=current,
                change_rate=change_rate,
                volume=volume,
                best_ask=best_ask,
                best_bid=best_bid,
                trade_value=trade_value,
                execution_strength=execution_strength,
                spread_ticks=spread_ticks,
                trade_time=trade_time,
                open_price=open_price,
                day_high=day_high,
                day_low=day_low,
                metadata=metadata,
            )
        )

    def _on_receive_chejan_data(self, gubun: str, item_count: int, fid_list: str) -> None:
        order_no = self._chejan(9203)
        code = self._chejan(9001).replace("A", "")
        side_name = self._chejan(907)
        side = "buy" if side_name in {"2", "매수"} else "sell"
        quantity = self._parse_int(self._chejan(900))
        price = self._parse_int(self._chejan(901))
        filled = self._parse_int(self._chejan(911))
        remaining = self._parse_int(self._chejan(902))
        tag = self._chejan(920)
        if code and order_no:
            self.execution_received.emit(
                ExecutionEvent(
                    code=code,
                    order_no=order_no,
                    side=side,
                    quantity=quantity,
                    price=price,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    tag=tag,
                )
            )

    def _real_int(self, code: str, fid: int) -> int:
        return self._parse_int(self._real_raw(code, fid))

    def _real_float(self, code: str, fid: int) -> float:
        value, _ = _parse_real_float(self._real_raw(code, fid), abs_value=False)
        return value

    def _real_raw(self, code: str, fid: int) -> str:
        return str(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, fid) or "").strip()

    def _chejan(self, fid: int) -> str:
        return str(self.ocx.dynamicCall("GetChejanData(int)", fid) or "").strip()

    @staticmethod
    def _parse_int(value) -> int:
        raw = str(value or "").strip().replace("+", "").replace("-", "").replace(",", "")
        try:
            return abs(int(float(raw)))
        except ValueError:
            return 0


class MockKiwoomClient:
    def __init__(self) -> None:
        self.connected = Signal()
        self.price_received = Signal()
        self.price_tick_received = Signal()
        self.order_result = Signal()
        self.execution_received = Signal()
        self.message_received = Signal()
        self.condition_state_changed = Signal()
        self.condition_load_result = Signal()
        self.condition_loaded = Signal()
        self.condition_tr_received = Signal()
        self.condition_real_received = Signal()
        self.condition_candidate_included = Signal()
        self.condition_candidate_removed = Signal()
        self.tr_data_received = Signal()
        self.condition_load_state = ConditionLoadState.IDLE
        self.orders: list[OrderRequest] = []
        self.registered_codes: set[str] = set()
        self.registered_code_order: list[str] = []
        self.removed_codes: list[str] = []
        self.remove_all_count = 0
        self._conditions: list[ConditionInfo] = []
        self.send_condition_calls: list[dict] = []
        self.stop_condition_calls: list[dict] = []
        self.condition_load_calls = 0
        self.condition_send_failures: set[tuple[str, int, int]] = set()
        self._market_codes: dict[str, list[str]] = {"0": [], "10": []}
        self.tr_calls: list[dict] = []
        self._tr_inputs: dict[str, str] = {}
        self._tr_pages: dict[tuple[str, str], list[dict]] = {}
        self._current_tr_page: dict = {}
        self._last_prices: dict[str, str] = {}
        self._names: dict[str, str] = {
            "005930": "삼성전자",
            "000660": "SK하이닉스",
            "035420": "NAVER",
        }

    def login(self) -> int:
        self.connected.emit(True, 0, "MOCK 로그인 성공")
        return 0

    def get_accounts(self) -> list[str]:
        return ["1234567890"]

    def get_user_id(self) -> str:
        return "MOCK_USER"

    def get_code_name(self, code: str) -> str:
        return self._names.get(code, f"MOCK-{code}")

    def get_master_last_price(self, code: str) -> str:
        return str(self._last_prices.get(str(code), "") or "")

    def register_realtime(self, codes: Iterable[str], screen_no: Optional[str] = None) -> None:
        code_list = [code for code in codes if code]
        self.registered_codes.update(code_list)
        self.registered_code_order.extend(code_list)
        self.message_received.emit(f"MOCK 실시간 등록: {', '.join(code_list)}")

    def remove_realtime(self, codes: Iterable[str], screen_no: Optional[str] = None) -> None:
        code_list = [code for code in codes if code]
        for code in code_list:
            self.registered_codes.discard(code)
            self.removed_codes.append(code)
        self.message_received.emit(f"MOCK 실시간 해제: {', '.join(code_list)}")

    def remove_all_realtime(self) -> None:
        self.registered_codes.clear()
        self.registered_code_order.clear()
        self.remove_all_count += 1
        self.message_received.emit("MOCK 실시간 전체 해제")

    def load_conditions(self) -> int:
        self.condition_load_calls += 1
        self.condition_load_state = ConditionLoadState.LOADING
        self.condition_state_changed.emit(self.condition_load_state.value, "")
        return 1

    def condition_name_list(self) -> list[ConditionInfo]:
        return list(self._conditions)

    def send_condition(
        self,
        screen_no: str,
        condition_name: str,
        condition_index: int,
        realtime: bool = True,
        search_type: Optional[int] = None,
    ) -> int:
        n_search = int(search_type if search_type is not None else (1 if realtime else 0))
        call = {
            "screen_no": str(screen_no),
            "condition_name": str(condition_name),
            "condition_index": int(condition_index),
            "realtime": bool(realtime),
            "search_type": n_search,
        }
        self.send_condition_calls.append(call)
        if (str(condition_name), int(condition_index), n_search) in self.condition_send_failures:
            return 0
        return 1

    def stop_condition(self, screen_no: str, condition_name: str, condition_index: int) -> None:
        self.stop_condition_calls.append(
            {
                "screen_no": str(screen_no),
                "condition_name": str(condition_name),
                "condition_index": int(condition_index),
            }
        )

    def set_input_value(self, input_name: str, value: str) -> None:
        self._tr_inputs[str(input_name)] = str(value)

    def comm_rq_data(self, rq_name: str, tr_code: str, prev_next: int, screen_no: str) -> int:
        tr_code_text = str(tr_code)
        key = (tr_code_text.lower(), _mock_tr_key(tr_code_text, self._tr_inputs))
        pages = self._tr_pages.get(key, [])
        page = pages.pop(0) if pages else {"rows": [], "prev_next": "", "error_code": "", "message": ""}
        self._current_tr_page = page
        self.tr_calls.append(
            {
                "rq_name": str(rq_name),
                "tr_code": str(tr_code),
                "prev_next": int(prev_next),
                "screen_no": str(screen_no),
                "inputs": dict(self._tr_inputs),
            }
        )
        if int(page.get("request_code", 0) or 0) < 0:
            return int(page.get("request_code"))
        self.tr_data_received.emit(
            str(screen_no),
            str(rq_name),
            str(tr_code),
            str(page.get("record_name", "")),
            str(page.get("prev_next", "")),
            0,
            str(page.get("error_code", "")),
            str(page.get("message", "")),
            "",
        )
        return 0

    def get_repeat_count(self, tr_code: str, rq_name: str) -> int:
        return len(self._current_tr_page.get("rows", []))

    def get_comm_data(self, tr_code: str, rq_name: str, index: int, item_name: str) -> str:
        rows = self._current_tr_page.get("rows", [])
        if index < 0 or index >= len(rows):
            return ""
        return str(rows[index].get(str(item_name), "") or "").strip()

    def set_tr_pages(self, tr_code: str, key: str, pages: list[dict]) -> None:
        self._tr_pages[(str(tr_code).lower(), str(key))] = [dict(page) for page in pages]

    def set_market_codes(self, market_code: str, codes: list[str]) -> None:
        self._market_codes[str(market_code)] = [str(code).replace("A", "") for code in codes]

    def get_code_list_by_market(self, market_code: str) -> list[str]:
        return list(self._market_codes.get(str(market_code), []))

    def set_conditions(self, conditions: list[tuple[int, str]]) -> None:
        self._conditions = [ConditionInfo(index=int(index), name=str(name)) for index, name in conditions]

    def emit_condition_loaded(self) -> None:
        self.condition_loaded.emit(list(self._conditions))

    def emit_condition_load_result(self, success: bool, message: str = "") -> None:
        self.condition_load_state = ConditionLoadState.LOADED if success else ConditionLoadState.FAILED
        self.condition_state_changed.emit(self.condition_load_state.value, str(message or ""))
        self.condition_load_result.emit(bool(success), str(message or ""))
        if success:
            self.condition_loaded.emit(list(self._conditions))

    def emit_tr_condition(
        self,
        screen_no: str,
        code_list: str,
        condition_name: str,
        condition_index: int,
        next_flag: str = "",
    ) -> None:
        self.condition_tr_received.emit(
            str(screen_no),
            str(code_list),
            str(condition_name),
            int(condition_index),
            str(next_flag or ""),
        )

    def emit_real_condition(
        self,
        code: str,
        event_type: str,
        condition_name: str,
        condition_index: int,
    ) -> None:
        self.condition_real_received.emit(str(code), str(event_type), str(condition_name), int(condition_index))

    def emit_condition_include(
        self,
        condition_name: str,
        code: str,
        *,
        strategy_profile: str = "",
        purpose: str = "",
    ) -> None:
        event = ConditionCandidateEvent(
            condition_name=condition_name,
            code=code,
            condition_index=self._condition_index(condition_name),
            event_type="include",
            strategy_profile=strategy_profile,
            purpose=purpose,
        )
        self.condition_candidate_included.emit(event)

    def emit_condition_remove(
        self,
        condition_name: str,
        code: str,
        *,
        strategy_profile: str = "",
        purpose: str = "",
    ) -> None:
        event = ConditionCandidateEvent(
            condition_name=condition_name,
            code=code,
            condition_index=self._condition_index(condition_name),
            event_type="remove",
            strategy_profile=strategy_profile,
            purpose=purpose,
        )
        self.condition_candidate_removed.emit(event)

    def send_order(self, request: OrderRequest) -> OrderResult:
        self.orders.append(request)
        result = OrderResult(True, 0, "MOCK 주문 정상처리", request)
        self.order_result.emit(result)
        return result

    def cancel_order(self, account: str, code: str, quantity: int, original_order_no: str, tag: str) -> OrderResult:
        return self.send_order(
            OrderRequest(
                account=account,
                code=code,
                quantity=quantity,
                price=0,
                side="cancel_buy",
                tag=tag,
                order_type=3,
                original_order_no=original_order_no,
            )
        )

    def modify_buy_order(
        self,
        account: str,
        code: str,
        quantity: int,
        price: int,
        original_order_no: str,
        tag: str,
    ) -> OrderResult:
        return self.send_order(
            OrderRequest(
                account=account,
                code=code,
                quantity=quantity,
                price=price,
                side="modify_buy",
                tag=tag,
                order_type=5,
                original_order_no=original_order_no,
            )
        )

    def emit_price(
        self,
        code: str,
        price: int,
        change_rate: float = 0.0,
        volume: int = 0,
        *,
        best_ask: Optional[int] = None,
        best_bid: Optional[int] = None,
        trade_value: float = 0.0,
        execution_strength: float = 0.0,
        spread_ticks: int = 0,
        trade_time: str = "",
        open_price: int = 0,
        instrument_type: str = "stock",
        name: str = "",
        day_high: int = 0,
        day_low: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        ask = int(best_ask if best_ask is not None else price + 1)
        bid = int(best_bid if best_bid is not None else price - 1)
        if instrument_type == "stock" and not name and not day_high and not day_low:
            self.price_received.emit(code, price, change_rate, volume, ask, bid)
        else:
            self.price_received.emit(
                code,
                price,
                change_rate,
                volume,
                ask,
                bid,
                instrument_type=instrument_type,
                name=name,
                day_high=day_high,
                day_low=day_low,
            )
        self.price_tick_received.emit(
            BrokerPriceTick(
                code=str(code or ""),
                price=int(price or 0),
                change_rate=float(change_rate or 0.0),
                volume=int(volume or 0),
                best_ask=ask,
                best_bid=bid,
                trade_value=float(trade_value or 0.0),
                execution_strength=float(execution_strength or 0.0),
                spread_ticks=int(spread_ticks or 0),
                trade_time=str(trade_time or ""),
                open_price=int(open_price or 0),
                instrument_type=str(instrument_type or "stock"),
                name=str(name or ""),
                day_high=int(day_high or 0),
                day_low=int(day_low or 0),
                metadata=dict(metadata or {}),
            )
        )

    def _condition_index(self, condition_name: str) -> int:
        for condition in self._conditions:
            if condition.name == condition_name:
                return condition.index
        return -1

    def emit_execution(
        self,
        code: str,
        side: str,
        quantity: int,
        price: int,
        remaining_quantity: int = 0,
        tag: str = "",
    ) -> None:
        self.execution_received.emit(
            ExecutionEvent(
                code=code,
                order_no=f"M{len(self.orders):06d}",
                side=side,
                quantity=quantity,
                price=price,
                filled_quantity=quantity - remaining_quantity,
                remaining_quantity=remaining_quantity,
                tag=tag,
            )
        )


def parse_condition_name_list(raw: str) -> list[ConditionInfo]:
    conditions: list[ConditionInfo] = []
    for item in str(raw or "").split(";"):
        if not item:
            continue
        if "^" not in item:
            continue
        index_text, name = item.split("^", 1)
        try:
            index = int(index_text)
        except ValueError:
            continue
        conditions.append(ConditionInfo(index=index, name=name))
    return conditions


def realtime_stock_fid_string() -> str:
    return ";".join(str(fid) for fid in REALTIME_STOCK_FIDS)


def _parse_real_int(value: Any) -> tuple[int, bool]:
    text = str(value or "").strip().replace(",", "").replace("+", "")
    if not text:
        return 0, True
    try:
        return abs(int(float(text))), True
    except (TypeError, ValueError):
        return 0, False


def _parse_real_float(value: Any, *, abs_value: bool) -> tuple[float, bool]:
    text = str(value or "").strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0, True
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return 0.0, False
    return (abs(parsed) if abs_value else parsed), True


def _has_real_value(value: Any) -> bool:
    return str(value or "").strip() != ""


def _spread_ticks(best_bid: int, best_ask: int) -> int:
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return 0
    reference = best_bid or best_ask
    tick_size = _krx_stock_tick_size(reference)
    if tick_size <= 0:
        return 0
    return max(0, int(round((best_ask - best_bid) / tick_size)))


def _krx_stock_tick_size(price: int) -> int:
    price = abs(int(price or 0))
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _mock_tr_key(tr_code: str, inputs: dict[str, str]) -> str:
    if str(tr_code).lower() == "opt90002":
        return inputs.get("종목코드", "")
    return ""
