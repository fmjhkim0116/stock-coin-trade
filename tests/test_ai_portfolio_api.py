"""AI Sniper / AI Shield API tests. Price loading and the Anthropic call are faked; the SQLite-backed
ledger tests use the real ORM models. Live Naver/DB loading and MariaDB DDL are not exercised."""
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-stock-backend"))

from flask import Flask  # noqa: E402
from sqlalchemy import BigInteger, create_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import ai_portfolio  # noqa: E402
import models  # noqa: E402
import portfolio_lab  # noqa: E402


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def bars_from_returns(rets, start=date(2025, 1, 1), vol=1000.0):
    out, close = [], 100.0
    for i, r in enumerate(rets):
        prev = close
        close *= 1 + r
        out.append({"date": (start + timedelta(days=i)).isoformat(), "open": prev, "high": max(prev, close) * 1.002,
                    "low": min(prev, close) * 0.998, "close": close, "volume": vol})
    return out


def universe(n=400, seed=3):
    rng = np.random.default_rng(seed)
    base = rng.normal(0.0004, 0.012, n)
    r = {"000001": base, "000002": base * 0.9 + rng.normal(0, 0.003, n), "000003": rng.normal(0.0004, 0.010, n),
         "000004": -base * 0.6 + rng.normal(0, 0.006, n), "000005": rng.normal(0.0002, 0.004, n)}
    return {s: bars_from_returns(x) for s, x in r.items()}


class Base(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(ai_portfolio.ai_portfolio_bp)
        self.client = app.test_client()
        self.data = universe()
        p = mock.patch.object(ai_portfolio, "_load_all", lambda symbols, source: (
            {s: self.data[s] for s in symbols if s in self.data}, {s: "데이터 없음" for s in symbols if s not in self.data}))
        p.start()
        self.addCleanup(p.stop)

    def login(self, member_id=1):
        with self.client.session_transaction() as sess:
            sess["member_id"] = member_id


class SniperTests(Base):
    def post(self, **payload):
        return self.client.post("/api/dashboard/ai/sniper", json=payload)

    def test_correlation_and_frontier(self):
        response = self.post(symbols=list(self.data), base=["000001"], maxWeight=0.5)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        ranked = [r["symbol"] for r in body["correlation"]["diversifiers"]]
        self.assertEqual(ranked[0], "000004")
        self.assertEqual(ranked[-1], "000002")
        self.assertEqual(body["period"]["days"], 399)  # 400 prices -> 399 daily returns
        for portfolio in body["frontier"]["portfolios"].values():
            self.assertAlmostEqual(sum(portfolio["weights"].values()), 1.0, delta=0.03)
        self.assertIn("보장하지 않", body["notice"])

    def test_unreachable_target_is_not_faked(self):
        body = self.post(symbols=list(self.data), targetLow=0.9, targetHigh=1.0).get_json()
        self.assertNotIn("target", body["frontier"]["portfolios"])
        self.assertTrue(body["frontier"]["notes"])

    def test_partial_failures_are_reported(self):
        body = self.post(symbols=list(self.data) + ["999999"]).get_json()
        self.assertEqual(body["failed"], {"999999": "데이터 없음"})
        self.assertNotIn("999999", body["names"])

    def test_validation(self):
        self.assertEqual(self.post(symbols=["000001", "000002"]).status_code, 400)        # < 3 symbols
        self.assertEqual(self.post(symbols=["000001", "000002", "../x"]).status_code, 400)
        self.assertEqual(self.post(symbols=[f"{i:06d}" for i in range(1, 20)]).status_code, 400)
        self.assertEqual(self.post(symbols=list(self.data), source="yahoo").status_code, 400)
        self.assertEqual(self.post(symbols=list(self.data), maxWeight="x").status_code, 400)
        self.assertEqual(self.post(symbols=list(self.data), maxWeight=0.1).status_code, 400)  # 5 * 10% < 100%

    def test_string_symbol_list_accepted(self):
        self.assertEqual(self.post(symbols="000001, 000002 000003;000004").status_code, 200)


class ShieldTests(Base):
    def surge_bars(self, surge_now):
        rets = [0.0] * 60
        if surge_now:
            rets += [0.01, 0.02, 0.02, 0.03, 0.03]
        else:
            rets += [0.0] * 5
        bars = bars_from_returns(rets)
        if surge_now:
            bars[-1]["volume"] = 5000.0
        return bars

    def test_candidates(self):
        self.data = {"000001": self.surge_bars(True), "000002": self.surge_bars(False)}
        body = self.client.get("/api/dashboard/ai/shield/candidates?symbols=000001,000002").get_json()
        self.assertEqual([r["symbol"] for r in body["rows"]], ["000001"])
        self.assertGreater(body["rows"][0]["chg5"], 0.05)
        self.assertGreater(body["rows"][0]["volumeRatio"], 2)
        self.assertIn("추격", body["note"])

    def kelly(self, **payload):
        return self.client.post("/api/dashboard/ai/shield/kelly", json=payload)

    def test_manual_kelly(self):
        body = self.kelly(mode="manual", p=0.6, b=1.0, capital=10_000_000, fraction=0.5, cap=0.25).get_json()
        self.assertAlmostEqual(body["fullKelly"], 0.2)
        self.assertAlmostEqual(body["recommended"], 0.1)
        self.assertEqual(body["amount"], 1_000_000)
        self.assertAlmostEqual(body["drawdownProb"]["0.5"], 0.125)
        losing = self.kelly(mode="manual", p=0.4, b=1.0).get_json()
        self.assertEqual((losing["recommended"], losing["status"]), (0.0, "베팅 금지"))
        capped = self.kelly(mode="manual", p=0.9, b=3.0, fraction=1.0, cap=0.1, capital=1_000_000).get_json()
        self.assertEqual((capped["recommended"], capped["amount"]), (0.1, 100_000))

    def test_pooled_kelly_with_candidates(self):
        returns = [0.10] * 36 + [-0.05] * 24

        def fake_backtest(series, flows, buy, sell, **kw):
            return {"trade_returns": returns if series is self.data["000001"] else [], "trade_count": len(returns),
                    "signal_today": series is not self.data["000002"]}
        with mock.patch.object(ai_portfolio.condition_lab, "trade_backtest", fake_backtest):
            body = self.kelly(symbols=["000001", "000002", "000003"], capital=10_000_000, fraction=0.5, cap=0.25, totalCap=0.3).get_json()
        stats = body["stats"]
        self.assertEqual((stats["trades"], stats["status"]), (60, "베팅 가능"))
        self.assertEqual({c["symbol"] for c in body["candidates"]}, {"000001", "000003"})
        self.assertLessEqual(sum(c["fraction"] for c in body["candidates"]), 0.3 + 1e-3)  # total cap respected
        self.assertEqual(body["totalBet"], sum(c["amount"] for c in body["candidates"]))
        self.assertTrue(any("보장하지 않" in w for w in body["warnings"]))

    def test_pooled_kelly_runs_end_to_end_and_refuses_thin_samples(self):
        body = self.kelly(symbols=list(self.data), buy="close > ma(20)", sell="close < ma(10)").get_json()
        self.assertIn(body["stats"]["status"], ("베팅 가능", "베팅 금지", "우위 불확실", "표본 부족"))
        if body["stats"]["status"] != "베팅 가능":
            self.assertEqual(body["stats"]["recommended"], 0.0)
            self.assertTrue(all(c["fraction"] == 0.0 for c in body["candidates"]))
        thin = self.kelly(symbols=["000001"], buy="close > hh(100)", sell="").get_json()
        self.assertEqual(thin["stats"]["status"], "표본 부족")

    def test_kelly_validation(self):
        self.assertEqual(self.kelly(symbols=["000001"], buy="__import__('os')").status_code, 400)
        self.assertEqual(self.kelly(mode="manual", p="abc").status_code, 400)
        self.assertEqual(self.kelly(symbols=["bad/sym"]).status_code, 400)


class LedgerTests(Base):
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
        p = mock.patch.object(ai_portfolio, "session_scope", scope)
        p.start()
        self.addCleanup(p.stop)
        with scope() as db:
            db.add_all([models.Member(member_id=1, username="a", email="a@a", password="x", asset=1),
                        models.Member(member_id=2, username="b", email="b@b", password="x", asset=1)])
        self.month = "2026-10"
        pm = mock.patch.object(portfolio_lab, "month_key", lambda day=None: self.month)
        pm.start()
        self.addCleanup(pm.stop)

    def call(self, method, path, **body):
        return getattr(self.client, method)("/api/dashboard/ai/shield" + path, json=body)

    def test_requires_login_and_account(self):
        for method, path in (("get", "/account"), ("put", "/account"), ("post", "/rebalance/preview"), ("post", "/rebalance")):
            self.assertEqual(self.call(method, path).status_code, 401, path)
        self.login()
        self.assertEqual(self.call("post", "/rebalance", riskyEquity=1).status_code, 404)
        self.assertFalse(self.call("get", "/account").get_json()["exists"])
        self.assertEqual(self.call("put", "/account", capital=0).status_code, 400)

    def test_monthly_sweep_once_per_month(self):
        self.login()
        self.assertEqual(self.call("put", "/account", capital=10_000_000).status_code, 201)
        preview = self.call("post", "/rebalance/preview", riskyEquity=12_000_000, ratio=0.5).get_json()
        self.assertEqual(preview["plan"]["swept"], 1_000_000)
        self.assertEqual(self.call("get", "/account").get_json()["risky"], 10_000_000)  # preview changes nothing

        done = self.call("post", "/rebalance", riskyEquity=12_000_000, ratio=0.5)
        self.assertEqual(done.status_code, 201, done.get_data(as_text=True))
        account = done.get_json()["account"]
        self.assertEqual((account["risky"], account["safe"], account["baseline"]), (11_000_000, 1_000_000, 11_000_000))
        self.assertEqual(account["risky"] + account["safe"], 12_000_000)

        again = self.call("post", "/rebalance", riskyEquity=20_000_000)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.call("get", "/account").get_json()["risky"], 11_000_000)  # rejected call changed nothing

        self.month = "2026-11"  # next month: allowed, and a loss below the baseline sweeps nothing
        nov = self.call("post", "/rebalance", riskyEquity=10_000_000).get_json()
        self.assertEqual((nov["plan"]["swept"], nov["account"]["baseline"], nov["account"]["safe"]), (0, 11_000_000, 1_000_000))
        info = self.call("get", "/account").get_json()
        self.assertEqual([r["month"] for r in info["ledger"]], ["2026-11", "2026-10"])
        self.assertTrue(info["doneThisMonth"])

    def test_validation_isolation_and_reset(self):
        self.login()
        self.call("put", "/account", capital=5_000_000)
        self.assertEqual(self.call("post", "/rebalance", riskyEquity=-5).status_code, 400)
        self.assertEqual(self.call("post", "/rebalance", riskyEquity="abc").status_code, 400)
        for bad_ratio in (0, -0.5, 1.5):
            self.assertEqual(self.call("post", "/rebalance", riskyEquity=6_000_000, ratio=bad_ratio).status_code, 400, bad_ratio)
        self.assertEqual(self.call("get", "/account").get_json()["lastRebalanceMonth"], None)  # nothing was applied
        self.assertEqual(self.call("post", "/rebalance", riskyEquity=6_000_000, ratio=0.5).status_code, 201)
        self.assertEqual(self.call("put", "/account", capital=1_000_000).status_code, 409)  # has history -> needs reset
        self.assertEqual(self.call("put", "/account", capital=1_000_000, reset=True).status_code, 201)
        self.assertEqual(self.call("get", "/account").get_json()["ledger"], [])
        self.login(2)
        self.assertFalse(self.call("get", "/account").get_json()["exists"])


class ExplainTests(Base):
    def post(self, **payload):
        return self.client.post("/api/dashboard/ai/explain", json=payload)

    def test_login_kind_and_missing_key(self):
        self.assertEqual(self.post(kind="sniper", result={}).status_code, 401)
        self.login()
        self.assertEqual(self.post(kind="other", result={}).status_code, 400)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            body = self.post(kind="sniper", result={"a": 1}).get_json()
        self.assertFalse(body["available"])
        self.assertIn("ANTHROPIC_API_KEY", body["reason"])

    def test_claude_gets_only_computed_result_and_failures_degrade(self):
        self.login()
        seen = {}

        def fake(prompt, key):
            seen["prompt"], seen["key"] = prompt, key
            return "해석입니다."
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), mock.patch.object(ai_portfolio, "call_claude", fake):
            body = self.post(kind="kelly", result={"stats": {"fullKelly": 0.2}}).get_json()
        self.assertEqual((body["available"], body["text"]), (True, "해석입니다."))
        self.assertIn('"fullKelly": 0.2', seen["prompt"])
        self.assertIn("만들어내지 말고", seen["prompt"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(ai_portfolio, "call_claude", side_effect=ai_portfolio.requests.ConnectionError("x")):
            body = self.post(kind="kelly", result={}).get_json()
        self.assertFalse(body["available"])


if __name__ == "__main__":
    unittest.main()
