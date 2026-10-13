"""Condition-expression lab: evaluate user conditions on OHLCV bars and back-test the signals.

Pure standard library. Expressions are parsed with ``ast`` and evaluated by a small whitelisted
interpreter (no ``eval``), so user input can never call arbitrary Python.

Language (all series are per-bar lists; ``None`` during indicator warm-up and treated as False):
  names      open high low close volume foreign_net inst_net
  indicators ma(n) ema(n) rsi(n) vma(n) atr(n) hh(n) ll(n) chg(n) sma(x, n) ref(x, k) exit_score()
  helpers    cross_up(a, b) cross_down(a, b) abs(x) max(a, b) min(a, b)
  operators  + - * /  < <= > >= == !=  and or not
  hh(n)/ll(n) are the highest high / lowest low of the *previous* n bars (today excluded) so that
  ``close > hh(20)`` is a real breakout. ``chg(n)`` is the n-bar close change as a fraction.

Look-ahead: a condition on bar ``i`` only sees bars ``<= i``. The back-tests fill at the *next*
bar's open (stop-loss / take-profit fill at the stop level, or the open on a gap).
"""
import ast
import statistics

from exit_score import ExitParams, compute_scores, wilder_atr

MAX_EXPR_LEN = 300
MAX_NODES = 80
MAX_PERIOD = 250

PRESETS = [
    {"key": "golden_cross", "name": "골든크로스 (5/20일선)", "buy": "cross_up(ma(5), ma(20))",
     "sell": "cross_down(ma(5), ma(20))", "needs_flows": False},
    {"key": "rsi_rebound", "name": "RSI 30 상향 돌파", "buy": "cross_up(rsi(14), 30)",
     "sell": "rsi(14) > 70", "needs_flows": False},
    {"key": "volume_breakout", "name": "거래량 급증 + 20일 고가 돌파", "buy": "close > hh(20) and volume > 2 * vma(20)",
     "sell": "close < ma(10)", "needs_flows": False},
    {"key": "pullback", "name": "상승추세 눌림목 (20일선 회복)",
     "buy": "ma(20) > ma(60) and close > ma(20) and ref(close, 1) <= ref(ma(20), 1)",
     "sell": "close < ma(60)", "needs_flows": False},
    {"key": "flow_buy", "name": "외국인·기관 동반 순매수 + 20일선 위", "buy":
     "foreign_net > 0 and inst_net > 0 and close > ma(20)", "sell": "foreign_net < 0 and inst_net < 0",
     "needs_flows": True},
    {"key": "exit_score", "name": "20일선 위 보유 → 매도 점수 6 이상 청산", "buy": "close > ma(20) and ma(20) > ma(60)",
     "sell": "exit_score() >= 6", "needs_flows": False},
]


class ConditionError(ValueError):
    """Invalid expression or arguments; the message is safe to show to the user."""


# ── series helpers ────────────────────────────────────────────────────────────
def _map2(a, b, fn, n):
    out = []
    for i in range(n):
        x = a[i] if isinstance(a, list) else a
        y = b[i] if isinstance(b, list) else b
        out.append(None if x is None or y is None else fn(x, y))
    return out


def _sma(x, n_period):
    out, window_sum, count = [], 0.0, 0
    for i, value in enumerate(x):
        if value is None:
            out.append(None)
            window_sum, count = 0.0, 0  # warm-up gap: restart the window
            continue
        window_sum += value
        count += 1
        if count > n_period:
            window_sum -= x[i - n_period]
            count -= 1
        out.append(window_sum / n_period if count == n_period else None)
    return out


def _ema(x, n_period):
    k = 2 / (n_period + 1)
    out, prev = [], None
    for value in x:
        if value is None:
            out.append(None)
            continue
        prev = value if prev is None else value * k + prev * (1 - k)
        out.append(prev)
    seen = 0
    for i, value in enumerate(x):  # blank the warm-up so EMA matches SMA-style availability
        seen += value is not None
        if seen < n_period:
            out[i] = None
    return out


def _rsi(close, n_period):
    out = [None] * len(close)
    if len(close) <= n_period:
        return out
    gains = [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    avg_gain = sum(gains[:n_period]) / n_period
    avg_loss = sum(losses[:n_period]) / n_period
    for i in range(n_period, len(close)):
        if i > n_period:
            avg_gain = (avg_gain * (n_period - 1) + gains[i - 1]) / n_period
            avg_loss = (avg_loss * (n_period - 1) + losses[i - 1]) / n_period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def _shift(x, k):
    n = len(x)
    return [x[i - k] if i - k >= 0 else None for i in range(n)]


def _rolling_prev(x, n_period, fn):
    out = []
    for i in range(len(x)):
        window = x[i - n_period:i] if i >= n_period else None
        out.append(fn(window) if window and None not in window else None)
    return out


def _truthy(series):
    return [bool(v) if v is not None else False for v in series]


# ── evaluator ─────────────────────────────────────────────────────────────────
class _Evaluator:
    def __init__(self, bars, flows):
        self.n = len(bars)
        self.bars = bars
        self.cache = {}
        flow_map = {row["date"]: row for row in flows or []}
        # plain floats: numpy scalars would make comparisons return numpy.bool_, which the type check rejects
        self.series = {
            "open": [float(b["open"]) for b in bars], "high": [float(b["high"]) for b in bars],
            "low": [float(b["low"]) for b in bars], "close": [float(b["close"]) for b in bars],
            "volume": [float(b["volume"]) for b in bars],
            "foreign_net": [float(flow_map[b["date"]]["foreign"]) if b.get("date") in flow_map else None for b in bars],
            "inst_net": [float(flow_map[b["date"]]["institution"]) if b.get("date") in flow_map else None for b in bars],
        }

    # periods must be integer literals so users cannot request huge windows from data
    @staticmethod
    def _period(value, name):
        if isinstance(value, list) or isinstance(value, bool) or int(value) != value or not 1 <= value <= MAX_PERIOD:
            raise ConditionError(f"{name}의 기간은 1~{MAX_PERIOD} 사이 정수여야 합니다.")
        return int(value)

    def _cached(self, key, build):
        if key not in self.cache:
            self.cache[key] = build()
        return self.cache[key]

    def call(self, name, args):
        n, s = self.n, self.series
        arity = {"ma": 1, "ema": 1, "rsi": 1, "vma": 1, "atr": 1, "hh": 1, "ll": 1, "chg": 1, "sma": 2, "ref": 2,
                 "exit_score": 0, "cross_up": 2, "cross_down": 2, "abs": 1, "max": 2, "min": 2}
        if name not in arity:
            raise ConditionError(f"사용할 수 없는 함수입니다: {name}")
        if len(args) != arity[name]:
            raise ConditionError(f"{name}()은 인자 {arity[name]}개가 필요합니다.")
        if name in ("ma", "ema", "rsi", "vma", "atr", "hh", "ll", "chg"):
            p = self._period(args[0], name)
            return self._cached((name, p), lambda: {
                "ma": lambda: _sma(s["close"], p), "ema": lambda: _ema(s["close"], p),
                "rsi": lambda: _rsi(s["close"], p), "vma": lambda: _sma(s["volume"], p),
                "atr": lambda: wilder_atr(self.bars, p),
                "hh": lambda: _rolling_prev(s["high"], p, max), "ll": lambda: _rolling_prev(s["low"], p, min),
                "chg": lambda: [None if i < p else s["close"][i] / s["close"][i - p] - 1 for i in range(n)],
            }[name]())
        if name == "sma":
            if not isinstance(args[0], list):
                raise ConditionError("sma()의 첫 인자는 시계열이어야 합니다.")
            return _sma(args[0], self._period(args[1], name))
        if name == "ref":
            if not isinstance(args[0], list):
                raise ConditionError("ref()의 첫 인자는 시계열이어야 합니다.")
            k = args[1]
            if isinstance(k, list) or int(k) != k or not 0 <= k <= MAX_PERIOD:
                raise ConditionError(f"ref()의 이동 기간은 0~{MAX_PERIOD} 사이 정수여야 합니다.")
            return _shift(args[0], int(k))
        if name == "exit_score":
            return self._cached(("exit_score",), lambda: [r["score"] for r in compute_scores(
                self.bars, [{"date": d, "foreign": f, "institution": i} for d, f, i in zip(
                    (b.get("date") for b in self.bars), s["foreign_net"], s["inst_net"]) if f is not None and i is not None],
                ExitParams())])
        if name in ("cross_up", "cross_down"):
            a, b = args
            a_prev, b_prev = (_shift(a, 1) if isinstance(a, list) else a), (_shift(b, 1) if isinstance(b, list) else b)
            if name == "cross_up":
                now = _map2(a, b, lambda x, y: x > y, n)
                before = _map2(a_prev, b_prev, lambda x, y: x <= y, n)
            else:
                now = _map2(a, b, lambda x, y: x < y, n)
                before = _map2(a_prev, b_prev, lambda x, y: x >= y, n)
            return [bool(x and y) if x is not None and y is not None else None for x, y in zip(now, before)]
        if name == "abs":
            return [None if v is None else abs(v) for v in args[0]] if isinstance(args[0], list) else abs(args[0])
        fn = max if name == "max" else min
        return _map2(args[0], args[1], lambda x, y: fn(x, y), n)

    def node(self, node):
        n = self.n
        if isinstance(node, ast.Expression):
            return self.node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ConditionError("숫자만 사용할 수 있습니다.")
            return float(node.value) if isinstance(node.value, float) else node.value
        if isinstance(node, ast.Name):
            if node.id not in self.series:
                raise ConditionError(f"알 수 없는 이름입니다: {node.id}")
            return self.series[node.id]
        if isinstance(node, ast.UnaryOp):
            value = self.node(node.operand)
            if isinstance(node.op, ast.USub):
                return [None if v is None else -v for v in value] if isinstance(value, list) else -value
            if isinstance(node.op, ast.Not):
                return [None if v is None else not v for v in value] if isinstance(value, list) else (not value)
            raise ConditionError("지원하지 않는 단항 연산입니다.")
        if isinstance(node, ast.BinOp):
            ops = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y, ast.Mult: lambda x, y: x * y,
                   ast.Div: lambda x, y: x / y if y else None}
            if type(node.op) not in ops:
                raise ConditionError("사칙연산(+ - * /)만 사용할 수 있습니다.")
            fn = ops[type(node.op)]
            a, b = self.node(node.left), self.node(node.right)
            if not isinstance(a, list) and not isinstance(b, list):
                result = fn(a, b)
                if result is None:
                    raise ConditionError("0으로 나눌 수 없습니다.")
                return result
            return _map2(a, b, fn, n)
        if isinstance(node, ast.Compare):
            ops = {ast.Lt: lambda x, y: x < y, ast.LtE: lambda x, y: x <= y, ast.Gt: lambda x, y: x > y,
                   ast.GtE: lambda x, y: x >= y, ast.Eq: lambda x, y: x == y, ast.NotEq: lambda x, y: x != y}
            left = self.node(node.left)
            result = None
            for op, comparator in zip(node.ops, node.comparators):
                if type(op) not in ops:
                    raise ConditionError("비교 연산자는 < <= > >= == != 만 사용할 수 있습니다.")
                right = self.node(comparator)
                step = _map2(left, right, ops[type(op)], n)
                result = step if result is None else [None if a is None or b is None else (a and b)
                                                      for a, b in zip(result, step)]
                left = right
            return result
        if isinstance(node, ast.BoolOp):
            values = [self.node(v) for v in node.values]
            for v in values:
                if not isinstance(v, list):
                    raise ConditionError("and/or에는 비교식을 사용하세요.")
            truthy = [_truthy(v) for v in values]
            combine = all if isinstance(node.op, ast.And) else any
            return [combine(col) for col in zip(*truthy)]
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ConditionError("함수는 ma(20)처럼 이름과 위치 인자로만 호출할 수 있습니다.")
            return self.call(node.func.id, [self.node(a) for a in node.args])
        raise ConditionError("지원하지 않는 문법입니다. 비교식과 함수 호출만 사용할 수 있습니다.")


def parse_expression(expr):
    expr = (expr or "").strip()
    if not expr:
        raise ConditionError("조건식을 입력하세요.")
    if len(expr) > MAX_EXPR_LEN:
        raise ConditionError(f"조건식은 {MAX_EXPR_LEN}자 이하여야 합니다.")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"문법 오류: {exc.msg}") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ConditionError("조건식이 너무 복잡합니다.")
    return tree


def evaluate(bars, flows, expr):
    """Boolean signal per bar (warm-up / missing data -> False)."""
    tree = parse_expression(expr)
    result = _Evaluator(bars, flows).node(tree)
    if not isinstance(result, list) or any(v is not None and not isinstance(v, bool) for v in result):
        raise ConditionError("조건식의 결과가 참/거짓이 아닙니다. 비교식으로 작성하세요. 예: close > ma(20)")
    return _truthy(result)


def _starts(signal):
    """Indices where the condition turns on (first bar of each streak)."""
    return [i for i, on in enumerate(signal) if on and (i == 0 or not signal[i - 1])]


def signal_study(bars, flows, buy_expr, horizons=(1, 5, 10, 20)):
    """Forward-return statistics after each signal start: next open -> close ``h`` bars later."""
    signal = evaluate(bars, flows, buy_expr)
    starts = _starts(signal)
    stats = []
    for h in horizons:
        returns = [bars[i + h]["close"] / bars[i + 1]["open"] - 1 for i in starts
                   if i + h < len(bars) and bars[i + 1]["open"] > 0]
        stats.append({
            "horizon": h, "count": len(returns),
            "mean": statistics.fmean(returns) if returns else None,
            "median": statistics.median(returns) if returns else None,
            "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else None,
            "best": max(returns) if returns else None, "worst": min(returns) if returns else None,
        })
    return {"signal_count": len(starts), "signal_days": sum(signal),
            "recent_signals": [bars[i].get("date") for i in starts[-10:]],
            "signal_today": bool(signal[-1]) if signal else False, "stats": stats}


def trade_backtest(bars, flows, buy_expr, sell_expr=None, max_hold=20, stop_loss=None, take_profit=None,
                   fee_rate=0.00015, slippage=0.0005, initial_cash=10_000_000.0):
    """All-in long-only simulation. Signals on bar ``i-1`` fill at bar ``i``'s open."""
    n = len(bars)
    buy = evaluate(bars, flows, buy_expr)
    sell = evaluate(bars, flows, sell_expr) if sell_expr and sell_expr.strip() else [False] * n
    cash, shares, entry_price, entry_i = float(initial_cash), 0, 0.0, 0
    trades, equity = [], []

    def close_out(i, raw_price, reason):
        nonlocal cash, shares
        price = raw_price * (1 - slippage)
        proceeds = price * shares * (1 - fee_rate)
        trades.append({"entry_date": bars[entry_i].get("date"), "exit_date": bars[i].get("date"),
                       "entry_price": entry_price, "exit_price": price, "quantity": shares,
                       "return": price * (1 - fee_rate) / entry_price - 1, "bars_held": i - entry_i, "reason": reason})
        cash += proceeds
        shares = 0

    for i in range(n):
        bar = bars[i]
        if shares:
            held = i - entry_i
            exit_px, reason = None, None
            if stop_loss and bar["low"] <= entry_price * (1 - stop_loss):
                exit_px, reason = min(bar["open"], entry_price * (1 - stop_loss)), "손절"
            elif take_profit and bar["high"] >= entry_price * (1 + take_profit):
                exit_px, reason = max(bar["open"], entry_price * (1 + take_profit)), "익절"
            elif sell[i - 1] if i else False:
                exit_px, reason = bar["open"], "매도 조건"
            elif max_hold and held >= max_hold:
                exit_px, reason = bar["open"], "최대 보유일"
            if exit_px is not None:
                close_out(i, exit_px, reason)
        elif i and buy[i - 1]:
            price = bar["open"] * (1 + slippage)
            qty = int(cash / (price * (1 + fee_rate))) if price > 0 else 0
            if qty > 0:
                cash -= price * qty * (1 + fee_rate)
                shares, entry_price, entry_i = qty, price * (1 + fee_rate), i
        equity.append(cash + shares * bar["close"])

    peak, mdd = equity[0] if equity else 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak:
            mdd = min(mdd, value / peak - 1)
    wins = [t for t in trades if t["return"] > 0]
    return {
        "total_return": equity[-1] / initial_cash - 1 if equity else 0.0,
        "buy_and_hold_return": bars[-1]["close"] / bars[0]["open"] - 1 if bars else 0.0,
        "max_drawdown": mdd, "trade_count": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_trade_return": statistics.fmean(t["return"] for t in trades) if trades else None,
        "open_position": bool(shares), "trades": trades[-30:], "trade_returns": [t["return"] for t in trades],
        "signal_today": bool(buy[-1]) if buy else False,
    }
