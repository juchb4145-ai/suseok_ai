from storage.db import TradingDatabase
from trading_app.api import _handle_market_symbols_event


def test_market_symbols_event_upserts_kiwoom_symbol_master(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        saved = _handle_market_symbols_event(
            db,
            {
                "markets": [
                    {"market_code": "0", "market": "KOSPI", "symbols": ["A084670"]},
                    {"market_code": "10", "market": "KOSDAQ", "symbols": [{"code": "035720", "name": "Kakao"}]},
                ]
            },
        )

        assert saved == 2
        rows = {row["code"]: row for row in db.list_kiwoom_symbol_master(["084670", "035720"])}
        assert rows["084670"]["market"] == "KOSPI"
        assert rows["084670"]["market_code"] == "0"
        assert rows["035720"]["market"] == "KOSDAQ"
        assert rows["035720"]["name"] == "Kakao"
    finally:
        db.close()
