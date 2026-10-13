"""Portfolio math for the AI Sniper (efficient frontier) and AI Shield (Kelly) portfolios.

Deterministic numpy code with no network access; every figure is derived from the prices / trade
results passed in. Results are *historical estimates*, not forecasts: expected returns are shrunk
toward the cross-sectional mean and the covariance toward its diagonal because raw sample estimates
are very noisy. Targets such as "8-10% a year" are checked against that data and reported as
unreachable when the data says so.
"""
import math
from datetime import date

import numpy as np

TRADING_DAYS = 252
COV_SHRINK = 0.10  # weight on the diagonal when stabilising the covariance matrix


class PortfolioError(ValueError):
    """Bad input or insufficient data; the message is safe to show to the user."""


# ── data alignment ────────────────────────────────────────────────────────────
def align_returns(series, min_obs=60):
    """series: {symbol: [(iso_date, close), ...]} -> (symbols, dates, returns[T, N], dropped).

    Uses the dates every kept symbol shares. If that intersection is shorter than ``min_obs`` the
    symbol with the fewest observations is dropped (and reported) until it is long enough.
    """
    cleaned = {s: {d: float(c) for d, c in rows if c and c > 0} for s, rows in series.items()}
    cleaned = {s: v for s, v in cleaned.items() if v}
    dropped = [s for s in series if s not in cleaned]
    symbols = list(cleaned)
    while True:
        if len(symbols) < 2:
            raise PortfolioError("공통 거래일이 충분한 종목이 2개 미만입니다. 종목을 바꾸거나 데이터 기간을 확인하세요.")
        common = sorted(set.intersection(*(set(cleaned[s]) for s in symbols)))
        if len(common) > min_obs:
            break
        worst = min(symbols, key=lambda s: len(cleaned[s]))
        symbols.remove(worst)
        dropped.append(worst)
    prices = np.array([[cleaned[s][d] for s in symbols] for d in common])
    return symbols, common[1:], prices[1:] / prices[:-1] - 1.0, dropped


# ── correlation / diversifiers ────────────────────────────────────────────────
def correlation_report(symbols, returns, base=None, top_pairs=5):
    n = len(symbols)
    corr = np.corrcoef(returns, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0)
    avg = [(float(corr[i].sum() - 1.0) / (n - 1)) for i in range(n)]
    pairs = sorted(({"a": symbols[i], "b": symbols[j], "corr": round(float(corr[i, j]), 3)}
                    for i in range(n) for j in range(i + 1, n)), key=lambda p: p["corr"])
    base = [s for s in (base or []) if s in symbols]
    if base:
        basket = returns[:, [symbols.index(s) for s in base]].mean(axis=1)
        ranked = []
        for i, s in enumerate(symbols):
            if s in base:
                continue
            c = float(np.corrcoef(returns[:, i], basket)[0, 1]) if np.std(returns[:, i]) and np.std(basket) else 0.0
            ranked.append({"symbol": s, "corrWithBase": round(c, 3), "avgCorr": round(avg[i], 3)})
        ranked.sort(key=lambda r: r["corrWithBase"])
        mode = "base"
    else:
        ranked = sorted(({"symbol": s, "corrWithBase": None, "avgCorr": round(avg[i], 3)} for i, s in enumerate(symbols)),
                        key=lambda r: r["avgCorr"])
        mode = "average"
    return {"labels": symbols, "matrix": [[round(float(v), 2) for v in row] for row in corr],
            "lowestPairs": pairs[:top_pairs], "diversifiers": ranked, "mode": mode, "base": base}


# ── efficient frontier ────────────────────────────────────────────────────────
def _cap_weights(w, cap):
    """Clip rows of ``w`` to ``cap`` and give the excess to the uncapped names (rows sum to 1)."""
    for _ in range(25):
        excess = np.clip(w - cap, 0, None).sum(axis=1)
        if not (excess > 1e-12).any():
            break
        w = np.minimum(w, cap)
        free = w < cap - 1e-12
        room = (w * free).sum(axis=1)
        w = w + np.where(free, w / np.where(room[:, None] > 0, room[:, None], 1.0), 0.0) * excess[:, None]
    return w / w.sum(axis=1, keepdims=True)


def _mdd(portfolio_returns):
    equity = np.cumprod(1.0 + portfolio_returns)
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1.0).min())


def efficient_frontier(symbols, returns, rf=0.03, max_weight=0.4, target=(0.08, 0.10), shrink=0.5,
                       n_samples=20000, seed=7):
    n = len(symbols)
    if n < 2:
        raise PortfolioError("종목이 2개 이상 필요합니다.")
    if max_weight * n < 1 - 1e-9:
        raise PortfolioError(f"최대 비중 {max_weight:.0%}로는 {n}개 종목으로 100%를 채울 수 없습니다. 종목을 늘리거나 한도를 올리세요.")
    raw_mu = returns.mean(axis=0) * TRADING_DAYS
    mu = (1 - shrink) * raw_mu + shrink * raw_mu.mean()
    cov = np.cov(returns, rowvar=False) * TRADING_DAYS
    cov = (1 - COV_SHRINK) * cov + COV_SHRINK * np.diag(np.diag(cov))
    rng = np.random.default_rng(seed)
    half = n_samples // 2
    w = np.vstack([rng.dirichlet(np.ones(n), half), rng.dirichlet(np.full(n, 0.3), n_samples - half), np.full((1, n), 1.0 / n)])
    w = _cap_weights(w, max_weight)
    ret = w @ mu
    var = np.einsum("ij,jk,ik->i", w, cov, w)
    vol = np.sqrt(np.maximum(var, 1e-18))
    sharpe = (ret - rf) / vol

    def describe(idx, label):
        weights = w[idx]
        port = returns @ weights
        return {"label": label, "weights": {s: round(float(x), 4) for s, x in zip(symbols, weights) if x >= 0.005},
                "expectedReturn": round(float(ret[idx]), 4), "volatility": round(float(vol[idx]), 4),
                "sharpe": round(float(sharpe[idx]), 3), "historicalReturn": round(float(port.mean() * TRADING_DAYS), 4),
                "historicalVolatility": round(float(port.std(ddof=1) * math.sqrt(TRADING_DAYS)), 4),
                "historicalMaxDrawdown": round(_mdd(port), 4)}

    portfolios = {"minVol": describe(int(vol.argmin()), "최소 변동성 (시장 방어형)"),
                  "maxSharpe": describe(int(sharpe.argmax()), "최대 샤프 (위험 대비 수익)")}
    lo, hi = target
    in_band = np.where((ret >= lo) & (ret <= hi))[0]
    notes = []
    if len(in_band):
        portfolios["target"] = describe(int(in_band[vol[in_band].argmin()]), f"목표 수익대 {lo:.0%}~{hi:.0%} 중 최저 위험")
    else:
        notes.append(f"과거 데이터 기준으로 기대수익 {lo:.0%}~{hi:.0%}인 포트폴리오를 만들 수 없었습니다"
                     f"(달성 가능한 기대수익 범위 {ret.min():.1%} ~ {ret.max():.1%}).")
    # frontier curve: best expected return in each volatility bucket
    edges = np.linspace(vol.min(), vol.max(), 16)
    curve = []
    for a, b in zip(edges[:-1], edges[1:]):
        idx = np.where((vol >= a) & (vol <= b))[0]
        if len(idx):
            k = idx[ret[idx].argmax()]
            curve.append({"volatility": round(float(vol[k]), 4), "expectedReturn": round(float(ret[k]), 4)})
    efficient, best = [], -1e9
    for point in curve:  # keep only points that raise return as risk rises
        if point["expectedReturn"] > best:
            efficient.append(point)
            best = point["expectedReturn"]
    return {"portfolios": portfolios, "frontier": efficient, "assets": [
        {"symbol": s, "expectedReturn": round(float(mu[i]), 4), "volatility": round(float(math.sqrt(cov[i, i])), 4)} for i, s in enumerate(symbols)],
        "observations": int(returns.shape[0]), "rf": rf, "shrink": shrink, "maxWeight": max_weight, "notes": notes}


# ── Kelly criterion ───────────────────────────────────────────────────────────
def kelly_fraction(p, b):
    """Full-Kelly fraction f* = p - (1-p)/b for win probability p and payoff ratio b (avg win / avg loss)."""
    if not 0 <= p <= 1 or b <= 0:
        raise PortfolioError("승률은 0~1, 손익비는 0보다 커야 합니다.")
    return p - (1 - p) / b


def wilson_lower(wins, n, z=1.645):
    """One-sided 95% lower confidence bound of a win rate."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def drawdown_probability(fraction_of_kelly, level=0.5):
    """P(wealth ever falls to ``level`` of its start) when betting c x Kelly: level**(2/c - 1) (idealised)."""
    c = fraction_of_kelly
    return 1.0 if c >= 2 else float(level ** (2.0 / c - 1.0))


def kelly_from_trades(returns, fraction=0.5, cap=0.25, min_trades=20):
    """Size a bet from a list of per-trade returns (e.g. +0.12, -0.05).

    ``recommended`` uses the *lower confidence bound* of the win rate (so thin samples shrink the
    bet) times ``fraction``, capped at ``cap``. Below ``min_trades`` samples nothing is recommended.
    """
    n = len(returns)
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r <= 0]
    out = {"trades": n, "wins": len(wins), "minTrades": min_trades, "fraction": fraction, "cap": cap}
    if n == 0 or not wins or not losses:
        out.update(status="표본 부족", reason="수익 거래와 손실 거래가 모두 있어야 손익비를 계산할 수 있습니다.", recommended=0.0)
        return out
    p = len(wins) / n
    avg_win, avg_loss = sum(wins) / len(wins), sum(losses) / len(losses)
    b = avg_win / avg_loss if avg_loss > 0 else float("inf")
    p_low = wilson_lower(len(wins), n)
    full = kelly_fraction(p, b)
    conservative = kelly_fraction(p_low, b)
    out.update(winRate=round(p, 4), winRateLow=round(p_low, 4), avgWin=round(avg_win, 4), avgLoss=round(avg_loss, 4),
               payoffRatio=round(b, 3), fullKelly=round(full, 4), conservativeKelly=round(conservative, 4),
               drawdownProb={f"{c:g}": round(drawdown_probability(c), 4) for c in (1.0, 0.5, 0.25)})
    if n < min_trades:
        out.update(status="표본 부족", reason=f"거래 표본이 {n}건입니다. 최소 {min_trades}건 이상일 때만 베팅 비율을 제안합니다.", recommended=0.0)
    elif full <= 0:
        out.update(status="베팅 금지", reason="기대값이 0 이하입니다(켈리 비율 ≤ 0). 이 조건으로는 베팅하지 않는 것이 수학적으로 맞습니다.", recommended=0.0)
    elif conservative <= 0:
        out.update(status="우위 불확실", reason="승률의 신뢰하한을 쓰면 기대값이 0 이하입니다. 우위가 통계적으로 확인되지 않았습니다.", recommended=0.0)
    else:
        out.update(status="베팅 가능", reason="", recommended=round(min(cap, fraction * conservative), 4))
    return out


def allocate_bets(candidates, per_bet, total_cap=1.0):
    """Give each candidate ``per_bet`` of capital, scaling all down if their sum exceeds ``total_cap``."""
    if per_bet <= 0 or not candidates:
        return [{"symbol": c, "fraction": 0.0} for c in candidates]
    scale = min(1.0, total_cap / (per_bet * len(candidates)))
    return [{"symbol": c, "fraction": round(per_bet * scale, 4)} for c in candidates]


# ── monthly sweep (rebalancing) ───────────────────────────────────────────────
def month_key(day=None):
    day = day or date.today()
    return f"{day.year:04d}-{day.month:02d}"


def plan_sweep(risky_equity, baseline, safe_balance, ratio=0.5):
    """Move ``ratio`` of the gain above the high-water ``baseline`` from the betting account to safety."""
    if risky_equity < 0 or baseline < 0 or safe_balance < 0:
        raise PortfolioError("금액은 0 이상이어야 합니다.")
    if not 0 < ratio <= 1:
        raise PortfolioError("이전 비율은 0보다 크고 100% 이하여야 합니다.")
    gain = max(0, risky_equity - baseline)
    swept = int(round(gain * ratio))
    risky_after = risky_equity - swept
    return {"riskyBefore": int(round(risky_equity)), "baseline": int(round(baseline)), "gain": int(round(gain)), "swept": swept,
            "riskyAfter": int(round(risky_after)), "safeAfter": int(round(safe_balance + swept)),
            "newBaseline": int(round(risky_after if gain > 0 else baseline)), "ratio": ratio,
            "message": ("기준선 대비 수익이 없어 이전할 금액이 없습니다. 기준선은 유지됩니다(손실 만회 전까지 이전 없음)." if gain <= 0 else
                        f"수익 {int(round(gain)):,}원 중 {swept:,}원을 안전 계좌로 옮기고 기준선을 {int(round(risky_after)):,}원으로 올립니다.")}
