"""Endpoint tests for the exit-score API. The database layer is replaced by fakes,
so SQL is NOT exercised here; run against PostgreSQL (docker compose) to verify it."""
import io
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-stock-backend"))

import quant  # noqa: E402
from flask import Flask  # noqa: E402


def bars(n_up=100, n_down=30):
    closes = [100 + i * 0.8 for i in range(n_up)]
    closes += [closes[-1] - i * 2.5 for i in range(1, n_down + 1)]
    out, d = [], date(2026, 1, 1)
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        out.append({"date": (d + timedelta(days=i)).isoformat(), "open": prev, "high": max(prev, c) * 1.005,
                    "low": min(prev, c) * 0.995, "close": c, "volume": 1000.0})
    return out


class FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, statement, params=None):
        self.sink.append((str(statement), params))


class ExitApiTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(quant.quant_bp)
        self.client = app.test_client()
        self.executed = []
        self._orig = (quant._db, quant._load_bars_and_flows)
        sink = self.executed

        class Engine:
            @contextmanager
            def begin(self_inner):
                yield FakeConn(sink)
        quant._db = lambda: Engine()
        self.bars = bars()
        quant._load_bars_and_flows = lambda conn, symbol, max_bars=1000: (self.bars, [])

    def tearDown(self):
        quant._db, quant._load_bars_and_flows = self._orig

    def post_csv(self, text, symbol="005930", multipart=False):
        url = f"/api/quant/investor-flows?symbol={symbol}"
        if multipart:
            return self.client.post(url, data={"file": (io.BytesIO(text.encode("utf-8")), "f.csv")},
                                    content_type="multipart/form-data")
        return self.client.post(url, data=text.encode("utf-8"), content_type="text/csv")

    def test_upload_stores_rows_for_both_body_styles(self):
        csv_text = "일자,외국인,기관\n2026.10.07.,\"-1,000\",200\n2026.10.08.,-500,(50)\n"
        for multipart in (False, True):
            response = self.post_csv(csv_text, multipart=multipart)
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["rows"], 2)
        upserts = [p for s, p in self.executed if "INSERT INTO investor_flows" in s]
        self.assertEqual(upserts[0][1]["institution"], -50.0)

    def test_upload_rejects_bad_input(self):
        self.assertEqual(self.post_csv("date,foreign\n2026-01-01,1\n").status_code, 400)
        self.assertEqual(self.post_csv("date,foreign,institution\n2026-01-01,1,2\n", symbol="bad;sym").status_code, 400)
        self.assertEqual(self.post_csv("x" * 1_000_001).status_code, 400)
        self.assertEqual(self.post_csv("date,foreign,institution\n").status_code, 400)

    def test_exit_score_endpoint(self):
        response = self.client.get("/api/quant/exit-score?symbol=005930&limit=5")
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(len(body["rows"]), 5)
        self.assertEqual(body["latest"]["action"], "EXIT")

    def test_exit_score_needs_history(self):
        self.bars = bars(20, 5)
        self.assertEqual(self.client.get("/api/quant/exit-score?symbol=005930").status_code, 400)

    def test_backtest_endpoint_and_param_validation(self):
        response = self.client.post("/api/quant/exit-backtest", json={"symbol": "005930"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertGreater(body["tradeCount"], 1)
        self.assertEqual(body["params"]["exit_at"], 6)
        bad = self.client.post("/api/quant/exit-backtest", json={"symbol": "005930", "reduceAt": 8, "exitAt": 3})
        self.assertEqual(bad.status_code, 400)
        garbage = self.client.post("/api/quant/exit-backtest", json={"symbol": "005930", "atrMult": "abc"})
        self.assertEqual(garbage.status_code, 400)


if __name__ == "__main__":
    unittest.main()
