import math
import os
import sys
import unittest
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-stock-backend"))

import portfolio_lab as pl  # noqa: E402


def make_series(n=400, seed=3):
    rng = np.random.default_rng(seed)
    base = rng.normal(0.0004, 0.012, n)
    r = {"A": base, "B": base * 0.9 + rng.normal(0, 0.003, n), "C": rng.normal(0.0004, 0.010, n),
         "D": -base * 0.6 + rng.normal(0, 0.006, n), "E": rng.normal(0.0002, 0.004, n)}
    start = date(2025, 1, 1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]
    out = {}
    for sym, rets in r.items():
        close, prices = 100.0, [100.0]
        for x in rets:
            close *= 1 + x
            prices.append(close)
        out[sym] = list(zip(days, prices))
    return out


class DataTests(unittest.TestCase):
    def test_alignment_and_drop(self):
        series = make_series()
        series["SHORT"] = series["A"][:30]
        symbols, dates, returns, dropped = pl.align_returns(series)
        self.assertNotIn("SHORT", symbols)
        self.assertIn("SHORT", dropped)
        self.assertEqual(returns.shape, (len(dates), len(symbols)))
        self.assertEqual(len(dates), 400)
        with self.assertRaises(pl.PortfolioError):
            pl.align_returns({"A": series["A"]})

    def test_correlation_ranking(self):
        symbols, _, returns, _ = pl.align_returns(make_series())
        report = pl.correlation_report(symbols, returns, base=["A"])
        ranked = [r["symbol"] for r in report["diversifiers"]]
        self.assertEqual(ranked[0], "D")  # negatively correlated with A
        self.assertEqual(ranked[-1], "B")  # nearly a copy of A
        self.assertLess(report["diversifiers"][0]["corrWithBase"], 0)
        self.assertGreater(report["diversifiers"][-1]["corrWithBase"], 0.9)
        pair = report["lowestPairs"][0]
        self.assertEqual({pair["a"], pair["b"]}, {"A", "D"} if pair["corr"] < -0.5 else {pair["a"], pair["b"]})
        matrix = np.array(report["matrix"])
        self.assertTrue(np.allclose(np.diag(matrix), 1.0) and np.allclose(matrix, matrix.T))
        avg_mode = pl.correlation_report(symbols, returns)
        self.assertEqual(avg_mode["mode"], "average")
        self.assertEqual(len(avg_mode["diversifiers"]), len(symbols))


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.symbols, _, self.returns, _ = pl.align_returns(make_series())

    def test_weights_valid_and_cap_respected(self):
        result = pl.efficient_frontier(self.symbols, self.returns, max_weight=0.4)
        for key, portfolio in result["portfolios"].items():
            weights = portfolio["weights"]
            self.assertAlmostEqual(sum(weights.values()), 1.0, delta=0.03, msg=key)
            self.assertLessEqual(max(weights.values()), 0.4 + 1e-3, key)
        self.assertLessEqual(result["portfolios"]["minVol"]["volatility"], result["portfolios"]["maxSharpe"]["volatility"] + 1e-9)
        self.assertGreaterEqual(result["portfolios"]["maxSharpe"]["sharpe"], result["portfolios"]["minVol"]["sharpe"] - 1e-9)
        vols = [p["volatility"] for p in result["frontier"]]
        rets = [p["expectedReturn"] for p in result["frontier"]]
        self.assertEqual(vols, sorted(vols))
        self.assertEqual(rets, sorted(rets))  # an efficient frontier only rises with risk

    def test_min_vol_prefers_low_vol_and_diversifying_assets(self):
        weights = pl.efficient_frontier(self.symbols, self.returns, max_weight=0.6)["portfolios"]["minVol"]["weights"]
        self.assertGreater(weights.get("E", 0), 0.3)  # E is by far the least volatile

    def test_deterministic(self):
        a = pl.efficient_frontier(self.symbols, self.returns)
        b = pl.efficient_frontier(self.symbols, self.returns)
        self.assertEqual(a["portfolios"]["maxSharpe"], b["portfolios"]["maxSharpe"])

    def test_unreachable_target_is_reported(self):
        result = pl.efficient_frontier(self.symbols, self.returns, target=(0.9, 1.0))
        self.assertNotIn("target", result["portfolios"])
        self.assertTrue(result["notes"] and "만들 수 없었습니다" in result["notes"][0])

    def test_infeasible_cap(self):
        with self.assertRaises(pl.PortfolioError):
            pl.efficient_frontier(self.symbols, self.returns, max_weight=0.1)
        with self.assertRaises(pl.PortfolioError):
            pl.efficient_frontier(self.symbols[:1], self.returns[:, :1])


class KellyTests(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(pl.kelly_fraction(0.6, 1.0), 0.2)
        self.assertAlmostEqual(pl.kelly_fraction(0.5, 2.0), 0.25)
        self.assertLess(pl.kelly_fraction(0.4, 1.0), 0)
        with self.assertRaises(pl.PortfolioError):
            pl.kelly_fraction(1.2, 1)
        with self.assertRaises(pl.PortfolioError):
            pl.kelly_fraction(0.5, 0)

    def test_drawdown_probability(self):
        self.assertAlmostEqual(pl.drawdown_probability(1.0, 0.5), 0.5)
        self.assertAlmostEqual(pl.drawdown_probability(0.5, 0.5), 0.125)
        self.assertEqual(pl.drawdown_probability(2.5), 1.0)

    def test_wilson_bound(self):
        low = pl.wilson_lower(60, 100)
        self.assertTrue(0.5 < low < 0.6)
        self.assertLess(pl.wilson_lower(6, 10), pl.wilson_lower(60, 100))  # less data -> wider penalty
        self.assertEqual(pl.wilson_lower(0, 0), 0.0)

    def test_from_trades_sizing(self):
        trades = [0.10] * 36 + [-0.05] * 24  # p=0.6, b=2
        result = pl.kelly_from_trades(trades, fraction=0.5, cap=0.25)
        self.assertEqual(result["status"], "베팅 가능")
        self.assertAlmostEqual(result["fullKelly"], 0.6 - 0.4 / 2.0, places=3)
        self.assertLess(result["conservativeKelly"], result["fullKelly"])
        self.assertAlmostEqual(result["recommended"], 0.5 * result["conservativeKelly"], places=3)
        self.assertEqual(pl.kelly_from_trades(trades, fraction=1.0, cap=0.05)["recommended"], 0.05)

    def test_refuses_thin_or_negative_edges(self):
        thin = pl.kelly_from_trades([0.1, -0.05, 0.1, 0.1, -0.05])
        self.assertEqual((thin["status"], thin["recommended"]), ("표본 부족", 0.0))
        losing = pl.kelly_from_trades([0.05] * 20 + [-0.10] * 30)
        self.assertEqual((losing["status"], losing["recommended"]), ("베팅 금지", 0.0))
        marginal = pl.kelly_from_trades([0.1] * 11 + [-0.1] * 9)
        self.assertEqual(marginal["status"], "우위 불확실")
        self.assertEqual(pl.kelly_from_trades([])["status"], "표본 부족")
        self.assertEqual(pl.kelly_from_trades([0.1] * 30)["status"], "표본 부족")  # no losses to size against

    def test_allocation_scales_to_total_cap(self):
        alloc = pl.allocate_bets(["A", "B", "C", "D"], per_bet=0.25, total_cap=0.5)
        self.assertAlmostEqual(sum(a["fraction"] for a in alloc), 0.5, places=3)
        self.assertEqual([a["fraction"] for a in pl.allocate_bets(["A"], 0.1, 1.0)], [0.1])
        self.assertEqual(pl.allocate_bets([], 0.1), [])


class SweepTests(unittest.TestCase):
    def test_gain_is_split_and_baseline_ratchets_up(self):
        plan = pl.plan_sweep(risky_equity=12_000_000, baseline=10_000_000, safe_balance=3_000_000, ratio=0.5)
        self.assertEqual((plan["gain"], plan["swept"], plan["riskyAfter"], plan["safeAfter"], plan["newBaseline"]),
                         (2_000_000, 1_000_000, 11_000_000, 4_000_000, 11_000_000))
        # money is conserved
        self.assertEqual(plan["riskyAfter"] + plan["safeAfter"], 12_000_000 + 3_000_000)

    def test_no_sweep_below_high_water_mark(self):
        plan = pl.plan_sweep(9_000_000, 10_000_000, 1_000_000, 0.5)
        self.assertEqual((plan["swept"], plan["newBaseline"], plan["safeAfter"]), (0, 10_000_000, 1_000_000))

    def test_validation_and_month_key(self):
        for args in [(-1, 1, 1, .5), (1, 1, 1, 0), (1, 1, 1, 1.5)]:
            with self.assertRaises(pl.PortfolioError):
                pl.plan_sweep(*args)
        self.assertEqual(pl.month_key(date(2026, 3, 5)), "2026-03")


if __name__ == "__main__":
    unittest.main()
