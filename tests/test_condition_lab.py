import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python-stock-backend"))

import condition_lab as lab  # noqa: E402


def make_bars(closes, vol=1000.0):
    bars, d = [], date(2026, 1, 1)
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        bars.append({"date": (d + timedelta(days=i)).isoformat(), "open": prev, "high": max(prev, c) * 1.005,
                     "low": min(prev, c) * 0.995, "close": c, "volume": vol})
    return bars


def wave(n=240):
    import math
    return [100 + 20 * math.sin(i / 12) + i * 0.1 for i in range(n)]


class ExpressionTests(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars(wave())

    def test_all_presets_parse_and_run(self):
        for preset in lab.PRESETS:
            for key in ("buy", "sell"):
                signal = lab.evaluate(self.bars, None, preset[key])
                self.assertEqual(len(signal), len(self.bars), preset["key"])

    def test_close_above_ma_matches_manual(self):
        signal = lab.evaluate(self.bars, None, "close > ma(20)")
        closes = [b["close"] for b in self.bars]
        for i in range(19, len(closes)):
            self.assertEqual(signal[i], closes[i] > sum(closes[i - 19:i + 1]) / 20)
        self.assertFalse(any(signal[:19]))  # warm-up is False, never guessed

    def test_breakout_uses_previous_bars_only(self):
        closes = [100.0] * 30 + [130.0]
        bars = make_bars(closes)
        signal = lab.evaluate(bars, None, "close > hh(20)")
        self.assertTrue(signal[-1])
        self.assertFalse(any(signal[:-1]))

    def test_cross_and_chained_comparison(self):
        bars = make_bars([10, 9, 8, 9, 11, 12])
        signal = lab.evaluate(bars, None, "cross_up(close, 10)")
        self.assertEqual([i for i, v in enumerate(signal) if v], [4])
        self.assertTrue(lab.evaluate(bars, None, "8 <= close <= 12")[3])

    def test_no_lookahead(self):
        full = lab.evaluate(self.bars, None, "cross_up(ma(5), ma(20)) or rsi(14) < 30")
        for cut in (50, 100, 180):
            self.assertEqual(lab.evaluate(self.bars[:cut], None, "cross_up(ma(5), ma(20)) or rsi(14) < 30"), full[:cut])

    def test_flow_variables_need_data(self):
        flows = [{"date": b["date"], "foreign": 1.0, "institution": -1.0} for b in self.bars]
        self.assertFalse(any(lab.evaluate(self.bars, None, "foreign_net > 0")))
        self.assertTrue(all(lab.evaluate(self.bars, flows, "foreign_net > 0")))
        self.assertFalse(any(lab.evaluate(self.bars, flows, "foreign_net > 0 and inst_net > 0")))

    def test_rejects_unsafe_or_invalid(self):
        for bad in ["__import__('os').system('x')", "close.real > 1", "open('/etc/passwd')", "[1,2][0] > 0",
                    "lambda: 1", "close > ma(0)", "close > ma(999)", "close > ma(close)", "nope > 1", "ma(20, 5) > 1",
                    "close ** 2 > 1", "", "x" * 400, "close > 'a'", "ma(n=20) > 1", "close"]:
            with self.assertRaises(lab.ConditionError, msg=bad):
                lab.evaluate(self.bars, None, bad)

    def test_too_complex(self):
        with self.assertRaises(lab.ConditionError):
            lab.evaluate(self.bars, None, " and ".join(["close > 1"] * 40))


class BacktestTests(unittest.TestCase):
    def test_signal_study_stats(self):
        bars = make_bars(wave())
        study = lab.signal_study(bars, None, "cross_up(ma(5), ma(20))")
        self.assertGreater(study["signal_count"], 2)
        h5 = next(s for s in study["stats"] if s["horizon"] == 5)
        self.assertLessEqual(h5["count"], study["signal_count"])
        self.assertTrue(0 <= h5["win_rate"] <= 1)

    def test_trades_fill_after_signal_and_costs_matter(self):
        bars = make_bars(wave())
        free = lab.trade_backtest(bars, None, "cross_up(ma(5), ma(20))", "cross_down(ma(5), ma(20))", fee_rate=0, slippage=0)
        costly = lab.trade_backtest(bars, None, "cross_up(ma(5), ma(20))", "cross_down(ma(5), ma(20))", fee_rate=0.01, slippage=0.01)
        self.assertGreater(free["trade_count"], 0)
        self.assertGreater(free["total_return"], costly["total_return"])
        buy = lab.evaluate(bars, None, "cross_up(ma(5), ma(20))")
        index = {b["date"]: i for i, b in enumerate(bars)}
        for trade in free["trades"]:
            self.assertTrue(buy[index[trade["entry_date"]] - 1])  # decided on the previous close

    def test_stop_loss_caps_loss_even_on_gap(self):
        closes = [100.0] * 25 + [101, 102, 103] + [60.0] * 5
        bars = make_bars(closes)
        bars[28].update(open=60.0, high=60.5, low=59.5, close=60.0)  # opens far below the stop level
        result = lab.trade_backtest(bars, None, "close > ma(20)", None, max_hold=0, stop_loss=0.1, fee_rate=0, slippage=0)
        stop = next(t for t in result["trades"] if t["reason"] == "손절")
        self.assertLess(stop["return"], -0.3)  # gap below the stop fills at the open, not the stop level
        self.assertGreater(stop["return"], -0.45)

    def test_max_hold_and_no_signal(self):
        bars = make_bars(wave())
        result = lab.trade_backtest(bars, None, "close > ma(20)", None, max_hold=5)
        self.assertTrue(all(t["bars_held"] <= 5 for t in result["trades"]))
        none = lab.trade_backtest(bars, None, "close > 1000000")
        self.assertEqual((none["trade_count"], none["total_return"]), (0, 0.0))


if __name__ == "__main__":
    unittest.main()
