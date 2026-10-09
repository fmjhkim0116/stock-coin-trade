"""Score-based exit rules combining price trend and foreign/institution flows.

Pure standard library. Every score at index ``i`` uses data up to and including
bar ``i`` only; the backtest fills the resulting order at the *next* bar's open,
so there is no look-ahead.

Price bars are dicts with ``open, high, low, close, volume`` (oldest first).
Flow rows are dicts with ``date`` (ISO ``YYYY-MM-DD``), ``foreign`` and
``institution`` net-buy amounts (negative = net selling).
"""
from dataclasses import dataclass, asdict


@dataclass
class ExitParams:
    ma_fast: int = 20
    ma_slow: int = 60
    low_window: int = 20
    atr_window: int = 14
    atr_mult: float = 3.0
    high_window: int = 22
    volume_mult: float = 1.5
    flow_days: int = 5
    flow_ratio: float = 0.05
    joint_sell_days: int = 3
    reduce_at: int = 4
    exit_at: int = 6
    confirm_days: int = 3

    def validate(self):
        if self.ma_fast < 2 or self.ma_slow <= self.ma_fast:
            raise ValueError("ma_slow must be greater than ma_fast (>= 2)")
        if self.reduce_at < 1 or self.exit_at <= self.reduce_at:
            raise ValueError("exit_at must be greater than reduce_at (>= 1)")
        for name in ("confirm_days", "low_window", "atr_window", "high_window", "flow_days", "joint_sell_days"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.atr_mult <= 0 or self.volume_mult <= 0 or self.flow_ratio < 0:
            raise ValueError("atr_mult, volume_mult must be > 0 and flow_ratio >= 0")
        return self


def _mean(values):
    return sum(values) / len(values) if values else None


def true_ranges(bars):
    out = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar["high"] - bar["low"])
        else:
            prev = bars[i - 1]["close"]
            out.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev)))
    return out


def wilder_atr(bars, window=14):
    """Wilder ATR; ``None`` until ``window`` bars are available."""
    trs = true_ranges(bars)
    out, atr = [], None
    for i, tr in enumerate(trs):
        if i + 1 < window:
            out.append(None)
        elif i + 1 == window:
            atr = sum(trs[:window]) / window
            out.append(atr)
        else:
            atr = (atr * (window - 1) + tr) / window
            out.append(atr)
    return out


def _flow_by_date(flows):
    return {row["date"]: row for row in flows or []}


def compute_scores(bars, flows=None, params=None):
    """Return one dict per bar: ``score``, ``action``, and the fired ``reasons``.

    Components (max 9): close < fast MA (+1); close < slow MA or below the prior
    ``low_window`` low (+2); close below highest close of ``high_window`` bars minus
    ``atr_mult`` x ATR (+2); high-volume down bar (+1); foreign net selling over
    ``flow_days`` as a share of average turnover (+1); foreign and institutions
    both net selling ``joint_sell_days`` days in a row (+2).
    Flow points are skipped (not guessed) for dates without flow data. Because price
    signals alone rarely reach ``exit_at``, a score held at ``reduce_at`` or above for
    ``confirm_days`` consecutive bars is escalated to EXIT.
    """
    p = (params or ExitParams()).validate()
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    atr = wilder_atr(bars, p.atr_window)
    flow_map = _flow_by_date(flows)
    # Turnover and per-day flows aligned to bars (None when no flow row).
    turnover = [b["close"] * b["volume"] for b in bars]
    foreign = [flow_map[b["date"]]["foreign"] if b.get("date") in flow_map else None for b in bars]
    inst = [flow_map[b["date"]]["institution"] if b.get("date") in flow_map else None for b in bars]

    results, streak = [], 0
    for i, bar in enumerate(bars):
        score, reasons = 0, []
        ma_fast = _mean(closes[i - p.ma_fast + 1:i + 1]) if i + 1 >= p.ma_fast else None
        ma_slow = _mean(closes[i - p.ma_slow + 1:i + 1]) if i + 1 >= p.ma_slow else None
        if ma_fast is not None and bar["close"] < ma_fast:
            score += 1; reasons.append(f"close<MA{p.ma_fast}")
        broke_slow = ma_slow is not None and bar["close"] < ma_slow
        broke_low = i >= p.low_window and bar["close"] < min(b["low"] for b in bars[i - p.low_window:i])
        if broke_slow or broke_low:
            score += 2
            reasons.append(f"close<MA{p.ma_slow}" if broke_slow else f"close<{p.low_window}d low")
        if atr[i] is not None and i + 1 >= p.high_window:
            stop = max(closes[i - p.high_window + 1:i + 1]) - p.atr_mult * atr[i]
            if bar["close"] < stop:
                score += 2; reasons.append(f"ATRx{p.atr_mult:g} trailing stop")
        if i >= 20:
            avg_vol = _mean(volumes[i - 20:i])
            if avg_vol and volumes[i] > p.volume_mult * avg_vol and bar["close"] < closes[i - 1] and bar["close"] < bar["open"]:
                score += 1; reasons.append("high-volume down bar")
        window = foreign[max(0, i - p.flow_days + 1):i + 1]
        if i + 1 >= p.flow_days and all(v is not None for v in window) and i >= 20:
            avg_turn = _mean(turnover[i - 20:i])
            if avg_turn and -sum(window) / (avg_turn * p.flow_days) >= p.flow_ratio:
                score += 1; reasons.append(f"foreign {p.flow_days}d net sell")
        if i + 1 >= p.joint_sell_days:
            f_win = foreign[i - p.joint_sell_days + 1:i + 1]
            i_win = inst[i - p.joint_sell_days + 1:i + 1]
            if all(v is not None and v < 0 for v in f_win) and all(v is not None and v < 0 for v in i_win):
                score += 2; reasons.append(f"foreign+inst sell {p.joint_sell_days}d")
        streak = streak + 1 if score >= p.reduce_at else 0
        if score >= p.exit_at or streak >= p.confirm_days:
            action = "EXIT"
            if score < p.exit_at:
                reasons.append(f"REDUCE level held {p.confirm_days}d")
        else:
            action = "REDUCE" if score >= p.reduce_at else "HOLD"
        results.append({"date": bar.get("date"), "score": score, "action": action, "reasons": reasons})
    return results


def backtest(bars, flows=None, params=None, initial_cash=10_000_000.0, fee_rate=0.00015, slippage=0.0005):
    """Long-only simulation. Orders decided on bar ``i`` fill at bar ``i+1`` open.

    Enter (fully) when flat, in an uptrend (close > fast MA > slow MA) and score <= 1.
    Sell half on REDUCE, everything on EXIT; restore the half when score <= 1 again
    in an uptrend. Fees and slippage are charged on every fill.
    """
    p = (params or ExitParams()).validate()
    scores = compute_scores(bars, flows, p)
    closes = [b["close"] for b in bars]
    cash, shares, full_shares = float(initial_cash), 0, 0
    trades, equity = [], []

    def fill(i, side, qty, reason):
        nonlocal cash, shares
        px = bars[i]["open"] * (1 + slippage if side == "BUY" else 1 - slippage)
        cost = px * qty * fee_rate
        cash += (-px * qty - cost) if side == "BUY" else (px * qty - cost)
        shares += qty if side == "BUY" else -qty
        trades.append({"date": bars[i].get("date"), "side": side, "price": px, "quantity": qty, "reason": reason})

    for i in range(len(bars)):
        if i > 0:  # execute the order decided at the previous close
            prev = scores[i - 1]
            ma_f = _mean(closes[i - 1 - p.ma_fast + 1:i]) if i >= p.ma_fast else None
            ma_s = _mean(closes[i - 1 - p.ma_slow + 1:i]) if i >= p.ma_slow else None
            uptrend = ma_f is not None and ma_s is not None and closes[i - 1] > ma_f > ma_s
            if shares == 0 and uptrend and prev["score"] <= 1:
                px = bars[i]["open"] * (1 + slippage)
                qty = int(cash / (px * (1 + fee_rate)))
                if qty > 0:
                    full_shares = qty
                    fill(i, "BUY", qty, "uptrend entry")
            elif shares > 0 and prev["action"] == "EXIT":
                fill(i, "SELL", int(shares), "; ".join(prev["reasons"]) or "exit score")
                full_shares = 0
            elif shares > 0 and prev["action"] == "REDUCE" and shares == full_shares:
                qty = int(shares // 2)
                if qty > 0:
                    fill(i, "SELL", qty, "; ".join(prev["reasons"]) or "reduce score")
            elif 0 < shares < full_shares and uptrend and prev["score"] <= 1:
                px = bars[i]["open"] * (1 + slippage)
                qty = min(int(full_shares - shares), int(cash / (px * (1 + fee_rate))))
                if qty > 0:
                    fill(i, "BUY", qty, "restore after score cleared")
        equity.append(cash + shares * closes[i])

    peak, mdd = equity[0] if equity else 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak:
            mdd = min(mdd, value / peak - 1)
    sells = [t for t in trades if t["side"] == "SELL"]
    buy_hold = (closes[-1] / bars[0]["open"] - 1) if bars else 0.0
    return {
        "params": asdict(p),
        "total_return": equity[-1] / initial_cash - 1 if equity else 0.0,
        "buy_and_hold_return": buy_hold,
        "max_drawdown": mdd,
        "trade_count": len(trades),
        "exit_count": len(sells),
        "trades": trades,
        "equity": equity,
        "last_signal": scores[-1] if scores else None,
    }
