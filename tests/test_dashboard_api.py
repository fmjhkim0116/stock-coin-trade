"""Dashboard API tests. External services (Yahoo, Naver, DART) and the PostgreSQL quant DB are faked,
so these cover the Python logic, payload shapes and failure handling -- not live responses or SQL."""
import io
import json
import math
import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-stock-backend"))

from flask import Flask  # noqa: E402
from sqlalchemy import BigInteger, create_engine  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import dart_client  # noqa: E402
import dashboard  # noqa: E402
import models  # noqa: E402


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):  # rowid alias so autoincrement works in tests
    return "INTEGER"


def bars(n=140):
    out = []
    for i in range(n):
        c = 100 + 15 * math.sin(i / 10) + i * 0.1
        p = 100 + 15 * math.sin((i - 1) / 10) + (i - 1) * 0.1 if i else c
        out.append({"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": p, "high": max(p, c) * 1.01,
                    "low": min(p, c) * 0.99, "close": c, "volume": 1000.0})
    return out


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeQuantConn:
    def __init__(self, log, rows=None):
        self.log, self.rows = log, rows or []

    def execute(self, statement, params=None):
        self.log.append((str(statement), params))
        return Result(self.rows if "FROM investor_flows WHERE flow_date IN" in str(statement) else [])


class ApiCase(unittest.TestCase):
    def setUp(self):
        dashboard._ttl_cache.clear()
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(dashboard.dashboard_bp)
        self.client = app.test_client()
        self.log, self.flow_rows = [], []
        log, owner = self.log, self

        class Engine:
            @contextmanager
            def begin(self_inner):
                yield FakeQuantConn(log, owner.flow_rows)
        patches = [mock.patch.object(dashboard, "quant_db", lambda: Engine())]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def login(self, member_id=1):
        with self.client.session_transaction() as sess:
            sess["member_id"] = member_id


class OverviewTests(ApiCase):
    def test_unavailable_items_are_flagged_not_invented(self):
        def fake_index(symbol):
            if symbol == "^GSPC":
                raise RuntimeError("blocked")
            return {"price": 2600.0, "change": 13.0, "changeRate": 0.5, "asOf": "2026-10-08"}
        quotes = {"A": {"symbol": "A", "name": "가", "changeRate": 3.0, "simulated": True},
                  "B": {"symbol": "B", "name": "나", "changeRate": 2.0}, "C": {"symbol": "C", "name": "다", "changeRate": -1.5}}
        with mock.patch.object(dashboard, "fetch_index", fake_index), \
                mock.patch.object(dashboard.stock_market, "get_dashboard_stock_quotes", return_value=quotes):
            body = self.client.get("/api/dashboard/overview").get_json()
        spx = next(i for i in body["indices"] if i["name"] == "S&P 500")
        self.assertFalse(spx["available"])
        self.assertNotIn("price", spx)
        text = " ".join(b["text"] for b in body["briefing"])
        self.assertIn("모두 상승", text)
        self.assertNotIn("S&P 500", text)  # failed index not mentioned
        self.assertIn("나(+2.00%)", text)
        self.assertNotIn("가(+3.00%)", text)  # simulated fallback quote excluded

    def test_all_sources_down(self):
        with mock.patch.object(dashboard, "fetch_index", side_effect=RuntimeError("x")), \
                mock.patch.object(dashboard.stock_market, "get_dashboard_stock_quotes", return_value={}):
            body = self.client.get("/api/dashboard/overview").get_json()
        self.assertEqual(body["briefing"], [])
        self.assertFalse(body["quotesAvailable"])
        self.assertTrue(all(not i["available"] for i in body["indices"]))


class FlowTests(ApiCase):
    def test_rank_flows(self):
        rows = [{"symbol": str(i), "name": f"s{i}", "foreign_net": f, "institution_net": g, "days": 1}
                for i, (f, g) in enumerate([(100, 50), (80, -10), (-30, 90), (60, 70), (-5, -5)])]
        ranked = dashboard.rank_flows(rows, top_n=2)
        self.assertEqual([r["symbol"] for r in ranked["foreign"]], ["0", "1"])
        self.assertEqual([r["symbol"] for r in ranked["institution"]], ["2", "3"])
        self.assertEqual([r["symbol"] for r in ranked["both"]], ["0", "3"])
        self.assertEqual(ranked["foreign"][0]["rank"], 1)

    def test_flows_endpoint_and_empty_state(self):
        self.assertFalse(self.client.get("/api/dashboard/flows").get_json()["available"])
        self.flow_rows = [{"symbol": "005930", "name": None, "foreign_net": 500, "institution_net": 100, "days": 1,
                           "last_date": "2026-10-08", "first_date": "2026-10-08"}]
        body = self.client.get("/api/dashboard/flows?days=3").get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["foreign"][0]["name"], "삼성전자")  # falls back to the registered name
        self.assertEqual(body["asOf"], "2026-10-08")
        params = [p for s, p in self.log if "recent" in s][-1]
        self.assertEqual(params["days"], 3)

    def test_upload_requires_admin_and_validates(self):
        csv_text = "종목코드,종목명,일자,외국인,기관\nA005930,삼성전자,2026-10-08,\"1,000\",-5\n000660,SK하이닉스,2026-10-08,20,30\n"
        with mock.patch.object(dashboard, "_require_admin", return_value=None):
            self.assertEqual(self.client.post("/api/dashboard/flows/upload", data=csv_text.encode()).status_code, 403)
        with mock.patch.object(dashboard, "_require_admin", return_value="admin"):
            ok = self.client.post("/api/dashboard/flows/upload", data={"file": (io.BytesIO(csv_text.encode()), "f.csv")},
                                  content_type="multipart/form-data")
            self.assertEqual(ok.status_code, 201, ok.get_data(as_text=True))
            self.assertEqual(ok.get_json()["symbols"], 2)
            batch = next(p for s, p in self.log if "INSERT INTO investor_flows" in s)
            self.assertEqual({r["symbol"] for r in batch}, {"005930", "000660"})
            self.assertEqual(self.client.post("/api/dashboard/flows/upload", data=b"date,foreign,institution\n").status_code, 400)
            self.assertEqual(self.client.post("/api/dashboard/flows/upload", data=b"x" * 1_000_001).status_code, 400)

    def test_parse_overtime(self):
        info = {"symbol": "005930", "name": "삼성전자", "market": "KOSPI"}
        good = {"overMarketPriceInfo": {"overPrice": "75,400", "fluctuationsRatio": "-1.30", "compareToPreviousClosePrice": "1,000",
                                        "overMarketStatus": "OPEN", "localTradedAt": "2026-10-08T16:10:00+09:00"}}
        parsed = dashboard.parse_overtime(good, info)
        self.assertEqual((parsed["price"], parsed["change"], parsed["changeRate"]), (75400, -1000, -1.3))
        self.assertIsNone(dashboard.parse_overtime({}, info))
        self.assertIsNone(dashboard.parse_overtime({"overMarketPriceInfo": {"overPrice": "0", "fluctuationsRatio": "1"}}, info))
        self.assertIsNone(dashboard.parse_overtime({"overMarketPriceInfo": {"overPrice": "10", "fluctuationsRatio": "n/a"}}, info))

    def test_overtime_endpoint_ranks_risers_only(self):
        universe = [{"symbol": s, "name": s, "market": "KOSPI"} for s in ("A", "B", "C", "D")]
        data = {"A": 2.0, "B": 5.5, "C": -3.0}

        def fake(info):
            if info["symbol"] == "D":
                raise RuntimeError("timeout")
            return {"symbol": info["symbol"], "name": info["name"], "market": "KOSPI", "price": 1, "change": 1,
                    "changeRate": data[info["symbol"]], "status": "", "tradedAt": ""}
        with mock.patch.object(dashboard, "_overtime_universe", return_value=universe), mock.patch.object(dashboard, "fetch_overtime", fake):
            body = self.client.get("/api/dashboard/overtime").get_json()
        self.assertEqual([r["symbol"] for r in body["rows"]], ["B", "A"])
        self.assertEqual((body["checked"], body["withOvertime"], body["failed"]), (4, 3, 1))
        with mock.patch.object(dashboard, "_overtime_universe", return_value=universe), \
                mock.patch.object(dashboard, "fetch_overtime", side_effect=RuntimeError("x")):
            self.assertFalse(self.client.get("/api/dashboard/overtime").get_json()["available"])


class DartTests(ApiCase):
    def test_parsers(self):
        rows = [{"corp_name": "A사", "stock_code": "000001", "corp_cls": "K", "report_nm": "단일판매ㆍ공급계약체결", "rcept_no": "20261008000123", "rcept_dt": "20261008"},
                {"corp_name": "B사", "stock_code": "000002", "corp_cls": "Y", "report_nm": "[기재정정]단일판매ㆍ공급계약체결(자율공시)", "rcept_no": "20261008000124", "rcept_dt": "20261008"},
                {"corp_name": "C사", "stock_code": "000003", "corp_cls": "Y", "report_nm": "주주총회소집결의", "rcept_no": "1", "rcept_dt": "20261008"}]
        parsed = dart_client.parse_contract_disclosures(rows)
        self.assertEqual([r["company"] for r in parsed], ["A사", "B사"])
        self.assertTrue(parsed[1]["correction"])
        self.assertEqual(parsed[0]["date"], "2026-10-08")
        self.assertTrue(parsed[0]["url"].endswith("20261008000123"))
        xml = b"<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list><list><corp_code>1</corp_code><stock_code> </stock_code></list></result>"
        self.assertEqual(dart_client.parse_corp_codes(xml), {"005930": "00126380"})
        self.assertEqual(dart_client.to_number("(1,234)"), -1234.0)
        self.assertIsNone(dart_client.to_number("-"))

    def test_financials_and_turnaround(self):
        rows = [{"account_nm": "영업이익", "fs_div": "OFS", "thstrm_amount": "999", "frmtrm_amount": "1"},
                {"account_nm": "영업이익", "fs_div": "CFS", "thstrm_amount": "500", "frmtrm_amount": "-200"},
                {"account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": "10,000", "frmtrm_amount": "8,000"},
                {"account_nm": "당기순이익", "fs_div": "CFS", "thstrm_amount": "300", "frmtrm_amount": "-100"},
                {"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": "2,000", "frmtrm_amount": "1,700"},
                {"account_nm": "부채총계", "fs_div": "CFS", "thstrm_amount": "1,000", "frmtrm_amount": "900"}]
        fin = dart_client.parse_financials(rows)
        self.assertEqual(fin["operating_income"]["current"], 500)  # consolidated wins over separate
        result = dart_client.analyze_fundamentals(fin)
        self.assertTrue(result["turnaround"])
        self.assertEqual(result["status"], "흑자전환")
        self.assertAlmostEqual(result["revenueGrowth"], 0.25)
        self.assertAlmostEqual(result["roe"], 0.15)
        self.assertAlmostEqual(result["debtRatio"], 0.5)
        self.assertAlmostEqual(result["operatingMargin"], 0.05)
        self.assertEqual(dart_client.analyze_fundamentals({})["status"], "판단 불가")
        for prev, cur, label in [(-5, -2, "적자 축소"), (-2, -5, "적자 지속"), (5, -1, "적자전환"), (5, 9, "영업이익 증가"), (9, 5, "영업이익 감소")]:
            self.assertEqual(dart_client.analyze_fundamentals({"operating_income": {"current": cur, "previous": prev}})["status"], label)

    def test_not_configured_is_explicit(self):
        with mock.patch.dict(os.environ, {"DART_API_KEY": ""}):
            for path in ("/api/dashboard/disclosures/contracts", "/api/dashboard/fundamentals"):
                body = self.client.get(path).get_json()
                self.assertEqual((body["available"], body["configured"]), (False, False), path)

    def test_fundamentals_endpoint_orders_turnaround_first(self):
        universe = [{"symbol": "000001", "name": "A"}, {"symbol": "000002", "name": "B"}, {"symbol": "000003", "name": "C"}]
        fins = {"c1": {"operating_income": {"current": 5, "previous": 9}}, "c2": {"operating_income": {"current": 5, "previous": -9}}}
        with mock.patch.object(dashboard, "_overtime_universe", return_value=universe), \
                mock.patch.object(dart_client, "corp_code_map", return_value={"000001": "c1", "000002": "c2"}), \
                mock.patch.object(dart_client, "fetch_financials", lambda corp, year: fins[corp]):
            body = self.client.get("/api/dashboard/fundamentals").get_json()
        self.assertEqual([r["symbol"] for r in body["rows"]], ["000002", "000001", "000003"])
        self.assertEqual(body["rows"][2]["reason"], "DART 고유번호 없음")


class WatchlistTests(ApiCase):
    def setUp(self):
        super().setUp()
        engine = create_engine("sqlite://")
        models.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        @contextmanager
        def scope():
            db = Session()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        p = mock.patch.object(dashboard, "session_scope", scope)
        p.start()
        self.addCleanup(p.stop)
        with scope() as db:
            db.add_all([models.Member(member_id=1, username="a", email="a@a", password="x", asset=1),
                        models.Member(member_id=2, username="b", email="b@b", password="x", asset=1)])
            db.add(models.StockPosition(member_id=1, symbol="005930", quantity=10, avg_price=70000))
        quote = lambda symbol: {"price": 77000, "changeRate": 1.5} if symbol == "005930" else (_ for _ in ()).throw(RuntimeError("down"))  # noqa: E731
        p2 = mock.patch.object(dashboard.stock_market, "get_quote_cached", quote)
        p2.start()
        self.addCleanup(p2.stop)

    def test_requires_login(self):
        for method, path in (("get", "/holdings"), ("get", "/watchlist"), ("put", "/watchlist/005930"), ("delete", "/watchlist/005930")):
            self.assertEqual(getattr(self.client, method)("/api/dashboard" + path).status_code, 401, path)

    def test_holdings_pnl_and_missing_quote(self):
        self.login()
        with dashboard.session_scope() as db:
            db.add(models.StockPosition(member_id=1, symbol="000660", quantity=1, avg_price=100))
        body = self.client.get("/api/dashboard/holdings").get_json()
        by = {r["symbol"]: r for r in body["rows"]}
        self.assertEqual((by["005930"]["pnl"], by["005930"]["pnlRate"]), (70000, 10.0))
        self.assertIsNone(by["000660"]["price"])  # failed quote -> null, not a fake price
        self.assertIsNone(by["000660"]["pnl"])
        self.assertEqual(body["totalPnl"], 70000)  # only priced rows counted

    def test_watchlist_crud_and_isolation(self):
        self.login(1)
        self.assertEqual(self.client.put("/api/dashboard/watchlist/005930").status_code, 201)
        self.assertFalse(self.client.put("/api/dashboard/watchlist/005930").get_json()["added"])
        self.assertEqual(self.client.put("/api/dashboard/watchlist/12345").status_code, 400)
        with mock.patch.object(dashboard.stock_market, "get_stock_info", return_value=None):
            self.assertEqual(self.client.put("/api/dashboard/watchlist/999999").status_code, 404)
        rows = self.client.get("/api/dashboard/watchlist").get_json()["rows"]
        self.assertEqual([(r["symbol"], r["price"]) for r in rows], [("005930", 77000)])
        self.login(2)
        self.assertEqual(self.client.get("/api/dashboard/watchlist").get_json()["rows"], [])
        self.assertFalse(self.client.delete("/api/dashboard/watchlist/005930").get_json()["removed"])
        self.login(1)
        self.assertTrue(self.client.delete("/api/dashboard/watchlist/005930").get_json()["removed"])

    def test_watchlist_limit(self):
        self.login(1)
        with dashboard.session_scope() as db:
            db.add_all(models.StockWatchlist(member_id=1, symbol=f"{i:06d}") for i in range(1, 51))
        self.assertEqual(self.client.put("/api/dashboard/watchlist/005930").status_code, 400)


class ConditionApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(dashboard, "_load_bars_and_flows", lambda conn, symbol: (bars(), []))
        p.start()
        self.addCleanup(p.stop)

    def post(self, **payload):
        return self.client.post("/api/dashboard/conditions/backtest", json={"symbol": "005930", "buy": "cross_up(ma(5), ma(20))", **payload})

    def test_presets(self):
        body = self.client.get("/api/dashboard/conditions/presets").get_json()
        self.assertGreaterEqual(len(body["presets"]), 5)

    def test_backtest_ok(self):
        response = self.post(sell="cross_down(ma(5), ma(20))", stopLoss=0.08, maxHold=15)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["bars"], 140)
        self.assertGreater(body["study"]["signal_count"], 0)
        self.assertIn("trade", body)
        self.assertEqual(body["trade"]["trades"][0]["quantity"] > 0, True)

    def test_validation_errors(self):
        self.assertEqual(self.post(buy="__import__('os')").status_code, 400)
        self.assertEqual(self.post(buy="close").status_code, 400)
        self.assertEqual(self.post(symbol="../etc").status_code, 400)
        self.assertEqual(self.post(source="yahoo").status_code, 400)
        self.assertEqual(self.post(feeRate="abc").status_code, 400)
        self.assertEqual(self.post(sell="ma(0) > 1").status_code, 400)

    def test_naver_source_survives_db_outage(self):
        def boom(conn, symbol):
            raise SQLAlchemyError("db down")
        with mock.patch.object(dashboard, "_load_bars_and_flows", boom), mock.patch.object(dashboard, "_naver_bars", lambda symbol: bars()):
            self.assertEqual(self.post(source="naver").status_code, 200)
            self.assertEqual(self.post(source="db").status_code, 503)

    def test_insufficient_data_and_naver_failure(self):
        with mock.patch.object(dashboard, "_load_bars_and_flows", lambda conn, symbol: (bars(30), [])):
            self.assertEqual(self.post().status_code, 400)
        with mock.patch.object(dashboard, "_naver_bars", side_effect=RuntimeError("blocked")):
            response = self.post(source="naver")
        self.assertEqual(response.status_code, 502)
        self.assertIn("가격 데이터를 가져오지 못했습니다", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
