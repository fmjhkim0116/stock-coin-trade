"""AI Sniper (efficient frontier / low-correlation assets) and AI Shield (Kelly sizing, monthly sweep).

All numbers come from ``portfolio_lab`` / ``condition_lab`` using real price history; the "AI Claude"
text is an optional explanation written from those numbers (``/explain``) and never the source of a
figure. The Shield ledger is a *simulated bookkeeping* record: nothing here places orders or moves money.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Blueprint, jsonify, request, session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import condition_lab
import portfolio_lab
import stock_market
from dashboard import _cached, _login_required, _naver_bars, _now, _overtime_universe, quant_db
from db import session_scope
from models import ShieldAccount, ShieldLedger

ai_portfolio_bp = Blueprint("ai_portfolio", __name__, url_prefix="/api/dashboard/ai")

SYMBOL_RE = re.compile(r"^[0-9A-Z_\-]{1,12}$")
MAX_SYMBOLS = 15
SURGE_EXPR = "close > hh(20) and volume > 2 * vma(20) and chg(5) > 0.05"
SURGE_EXIT = "close < ma(10)"
EXPLAIN_MODEL = os.environ.get("AI_EXPLAIN_MODEL", "claude-haiku-4-5-20251001")


# ── helpers ───────────────────────────────────────────────────────────────────
def _clean_symbols(raw, limit=MAX_SYMBOLS):
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out = []
    for item in raw or []:
        symbol = str(item).strip().upper()
        if not symbol:
            continue
        if not SYMBOL_RE.match(symbol):
            raise portfolio_lab.PortfolioError(f"올바르지 않은 종목코드입니다: {symbol[:20]}")
        if symbol not in out:
            out.append(symbol)
    if len(out) > limit:
        raise portfolio_lab.PortfolioError(f"종목은 최대 {limit}개까지 입력할 수 있습니다.")
    return out


def _name(symbol):
    return stock_market.STOCKS.get(symbol, {}).get("name", symbol)


def _bars(symbol, source):
    """OHLCV bars for one symbol. 'naver' is strict (raises); 'db' reads the quant PostgreSQL store."""
    if source == "naver":
        return _cached(("bars", symbol), 1800, lambda: _naver_bars(symbol))
    from quant import _load_bars_and_flows
    with quant_db().begin() as conn:
        bars, _ = _load_bars_and_flows(conn, symbol)
    return bars


def _load_all(symbols, source):
    """-> ({symbol: bars}, {symbol: error message}) fetched in parallel; failures never abort the batch."""
    def one(symbol):
        try:
            return symbol, _bars(symbol, source), None
        except Exception as exc:  # noqa: BLE001 - upstream / DB failure -> report per symbol
            return symbol, None, f"{exc.__class__.__name__}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, symbols))
    return {s: b for s, b, e in results if b}, {s: e or "데이터 없음" for s, b, e in results if not b}


def _source(data):
    source = str(data.get("source", "naver")).lower()
    if source not in ("naver", "db"):
        raise portfolio_lab.PortfolioError("source는 naver 또는 db여야 합니다.")
    return source


def _num(data, key, default, low, high):
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        raise portfolio_lab.PortfolioError(f"{key} 값이 숫자가 아닙니다.")
    return max(low, min(value, high))


def _default_universe():
    try:
        return [u["symbol"] for u in _overtime_universe()][:MAX_SYMBOLS]
    except Exception:  # noqa: BLE001 - e.g. no request context / DB down: fall back to registered stocks
        return list(stock_market.STOCKS)[:MAX_SYMBOLS]


def _error(exc, status=400):
    return jsonify({"message": str(exc)}), status


# ── AI Sniper: correlation + efficient frontier ───────────────────────────────
@ai_portfolio_bp.post("/sniper")
def sniper():
    data = request.get_json(silent=True) or {}
    try:
        symbols = _clean_symbols(data.get("symbols")) or _default_universe()
        base = _clean_symbols(data.get("base"))
        source = _source(data)
        if len(symbols) < 3:
            raise portfolio_lab.PortfolioError("상관관계를 비교하려면 종목이 3개 이상 필요합니다.")
        bars, failed = _load_all(symbols, source)
        series = {s: [(b["date"], b["close"]) for b in rows] for s, rows in bars.items()}
        kept, dates, returns, dropped = portfolio_lab.align_returns(series)
        corr = portfolio_lab.correlation_report(kept, returns, base=base)
        frontier = portfolio_lab.efficient_frontier(
            kept, returns, rf=_num(data, "rf", 0.03, 0, 0.2), max_weight=_num(data, "maxWeight", 0.4, 0.1, 1.0),
            target=(_num(data, "targetLow", 0.08, -0.5, 2), _num(data, "targetHigh", 0.10, -0.5, 2)),
            shrink=_num(data, "shrink", 0.5, 0, 1))
        return jsonify({
            "asOf": _now(), "source": "Naver 일봉(수정주가)" if source == "naver" else "PostgreSQL market_data",
            "period": {"from": dates[0], "to": dates[-1], "days": len(dates)},
            "names": {s: _name(s) for s in kept}, "dropped": dropped, "failed": failed,
            "correlation": corr, "frontier": frontier,
            "notice": "과거 일간 수익률 기반 추정입니다. 기대수익은 평균 회귀(shrink)로 보정했고 미래 수익을 보장하지 않습니다. "
                      "목표(시장 방어 + 연 8~10%)는 데이터가 허용할 때만 표시됩니다."})
    except portfolio_lab.PortfolioError as exc:
        return _error(exc)


# ── AI Shield: surge candidates + Kelly ───────────────────────────────────────
def _surge_metrics(bars):
    closes, volumes = [b["close"] for b in bars], [b["volume"] for b in bars]
    if len(bars) < 25:
        return None
    avg_vol = sum(volumes[-21:-1]) / 20
    return {"chg5": closes[-1] / closes[-6] - 1, "volumeRatio": volumes[-1] / avg_vol if avg_vol else None,
            "price": closes[-1], "date": bars[-1]["date"]}


@ai_portfolio_bp.get("/shield/candidates")
def shield_candidates():
    try:
        source = _source(request.args)
        symbols = _clean_symbols(request.args.get("symbols", "")) or _default_universe()
        bars, failed = _load_all(symbols, source)
        rows = []
        for symbol, series in bars.items():
            if len(series) < 60:
                continue
            if condition_lab.evaluate(series, None, SURGE_EXPR)[-1]:
                metrics = _surge_metrics(series)
                if metrics:
                    rows.append({"symbol": symbol, "name": _name(symbol), **metrics})
        rows.sort(key=lambda r: r["chg5"], reverse=True)
        return jsonify({"asOf": _now(), "rule": SURGE_EXPR, "checked": len(bars), "failed": failed, "rows": rows,
                        "note": "'급등주'는 20일 고가 돌파 + 거래량 2배 + 5일 +5% 조건을 마지막 거래일에 충족한 종목입니다. "
                                "이미 오른 종목이므로 추격 매수 위험이 있고, 조건 충족이 상승을 보장하지 않습니다."})
    except (portfolio_lab.PortfolioError, condition_lab.ConditionError) as exc:
        return _error(exc)


@ai_portfolio_bp.post("/shield/kelly")
def shield_kelly():
    data = request.get_json(silent=True) or {}
    try:
        capital = _num(data, "capital", 10_000_000, 0, 1e12)
        fraction = _num(data, "fraction", 0.5, 0.05, 1.0)
        cap = _num(data, "cap", 0.25, 0.01, 1.0)
        total_cap = _num(data, "totalCap", 1.0, 0.01, 1.0)
        warnings = ["켈리 비율은 승률·손익비 추정 오차에 매우 민감합니다. 풀 켈리(1배)는 변동이 매우 커서 ½ 이하를 권장합니다.",
                    "연 50% 이상은 목표일 뿐 수익을 보장하지 않으며, 이 전략은 큰 손실도 날 수 있습니다."]
        if str(data.get("mode", "pooled")) == "manual":
            p, b = _num(data, "p", 0.5, 0, 1), _num(data, "b", 1.0, 0.01, 100)
            full = portfolio_lab.kelly_fraction(p, b)
            rec = max(0.0, min(cap, fraction * full))
            return jsonify({"mode": "manual", "p": p, "b": b, "fullKelly": round(full, 4), "recommended": round(rec, 4),
                            "amount": int(round(capital * rec)), "fraction": fraction, "cap": cap,
                            "status": "베팅 가능" if rec > 0 else "베팅 금지",
                            "drawdownProb": {f"{c:g}": round(portfolio_lab.drawdown_probability(c), 4) for c in (1.0, 0.5, 0.25)},
                            "warnings": warnings + ["직접 입력한 승률·손익비는 표본 크기를 알 수 없어 신뢰구간을 적용하지 못했습니다."]})
        source = _source(data)
        symbols = _clean_symbols(data.get("symbols")) or _default_universe()
        buy = str(data.get("buy") or SURGE_EXPR)
        sell = str(data.get("sell") if data.get("sell") is not None else SURGE_EXIT)
        condition_lab.parse_expression(buy)
        if sell.strip():
            condition_lab.parse_expression(sell)
        params = dict(max_hold=int(_num(data, "maxHold", 20, 0, 250)), stop_loss=_num(data, "stopLoss", 0.08, 0, 0.9) or None,
                      take_profit=_num(data, "takeProfit", 0, 0, 5) or None)
        bars, failed = _load_all(symbols, source)
        pooled, per_symbol, candidates = [], [], []
        for symbol, series in bars.items():
            if len(series) < 60:
                failed[symbol] = f"가격 데이터 부족({len(series)}일)"
                continue
            result = condition_lab.trade_backtest(series, None, buy, sell, **params)
            pooled += result["trade_returns"]
            per_symbol.append({"symbol": symbol, "name": _name(symbol), "trades": result["trade_count"]})
            if result["signal_today"]:
                candidates.append(symbol)
        stats = portfolio_lab.kelly_from_trades(pooled, fraction=fraction, cap=cap, min_trades=int(_num(data, "minTrades", 20, 5, 500)))
        allocation = portfolio_lab.allocate_bets(candidates, stats["recommended"], total_cap)
        for row in allocation:
            row.update(name=_name(row["symbol"]), amount=int(round(capital * row["fraction"])))
        return jsonify({"mode": "pooled", "asOf": _now(), "buy": buy, "sell": sell, "params": {**params, "fraction": fraction, "cap": cap, "totalCap": total_cap},
                        "symbolsUsed": len(per_symbol), "failed": failed, "perSymbol": per_symbol, "stats": stats,
                        "candidates": allocation, "capital": int(capital),
                        "totalBet": int(round(sum(r["amount"] for r in allocation))), "warnings": warnings,
                        "notice": "승률·손익비는 선택한 종목들의 과거 신호 거래를 합쳐 추정했습니다(다음 거래일 시가 체결, 수수료·슬리피지 반영). "
                                  "권장 비율은 승률의 95% 신뢰하한으로 계산한 켈리 비율에 켈리 배수를 곱하고 종목당 한도로 제한한 값입니다."})
    except (portfolio_lab.PortfolioError, condition_lab.ConditionError) as exc:
        return _error(exc)


# ── AI Shield: monthly sweep (simulated ledger) ───────────────────────────────
def _account_json(acct):
    if not acct:
        return {"exists": False}
    return {"exists": True, "risky": acct.risky, "safe": acct.safe, "baseline": acct.baseline,
            "lastRebalanceMonth": acct.last_rebalance_month, "total": acct.risky + acct.safe}


def _sweep_inputs(data, acct):
    """Money inputs are validated, not clamped: a negative value or a 0% ratio is an error, not a silent 0 / 1%."""
    try:
        equity, ratio = float(data.get("riskyEquity", acct.risky)), float(data.get("ratio", 0.5))
    except (TypeError, ValueError):
        raise portfolio_lab.PortfolioError("평가금액과 이전 비율은 숫자여야 합니다.")
    if not 0 <= equity <= 1e13:
        raise portfolio_lab.PortfolioError("베팅 계좌 평가금액은 0 이상이어야 합니다.")
    return equity, ratio


@ai_portfolio_bp.get("/shield/account")
def shield_account():
    denied = _login_required()
    if denied:
        return denied
    member_id = session["member_id"]
    with session_scope() as db:
        acct = db.get(ShieldAccount, member_id)
        ledger = db.query(ShieldLedger).filter(ShieldLedger.member_id == member_id).order_by(ShieldLedger.shield_ledger_id.desc()).limit(12).all()
        return jsonify({**_account_json(acct), "currentMonth": portfolio_lab.month_key(),
                        "doneThisMonth": bool(acct and acct.last_rebalance_month == portfolio_lab.month_key()),
                        "ledger": [{"month": r.month, "riskyBefore": r.risky_before, "gain": r.gain, "swept": r.swept,
                                    "riskyAfter": r.risky_after, "safeAfter": r.safe_after,
                                    "at": r.created_at.isoformat() if r.created_at else None} for r in ledger],
                        "note": "모의 장부입니다. 실제 주문이나 이체는 일어나지 않으며, 증권사 계좌의 실제 이동은 직접 해야 합니다."})


@ai_portfolio_bp.put("/shield/account")
def shield_account_set():
    denied = _login_required()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        capital = int(_num(data, "capital", 0, 0, 1e13))
        if capital <= 0:
            raise portfolio_lab.PortfolioError("시작 금액은 0보다 커야 합니다.")
    except portfolio_lab.PortfolioError as exc:
        return _error(exc)
    member_id = session["member_id"]
    with session_scope() as db:
        acct = db.get(ShieldAccount, member_id)
        if acct and acct.last_rebalance_month and not data.get("reset"):
            return jsonify({"message": "이미 리밸런싱 기록이 있습니다. 장부를 처음부터 다시 시작하려면 reset을 확인하세요."}), 409
        if acct:
            db.query(ShieldLedger).filter(ShieldLedger.member_id == member_id).delete()
            acct.risky, acct.safe, acct.baseline, acct.last_rebalance_month = capital, 0, capital, None
        else:
            acct = ShieldAccount(member_id=member_id, risky=capital, safe=0, baseline=capital)
            db.add(acct)
        db.flush()
        return jsonify(_account_json(acct)), 201


@ai_portfolio_bp.post("/shield/rebalance/preview")
def shield_preview():
    denied = _login_required()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with session_scope() as db:
        acct = db.get(ShieldAccount, session["member_id"])
        if not acct:
            return jsonify({"message": "먼저 시작 금액으로 Shield 장부를 만드세요."}), 404
        try:
            equity, ratio = _sweep_inputs(data, acct)
            plan = portfolio_lab.plan_sweep(equity, acct.baseline, acct.safe, ratio)
        except portfolio_lab.PortfolioError as exc:
            return _error(exc)
        return jsonify({"plan": plan, "doneThisMonth": acct.last_rebalance_month == portfolio_lab.month_key()})


@ai_portfolio_bp.post("/shield/rebalance")
def shield_rebalance():
    denied = _login_required()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    member_id, month = session["member_id"], portfolio_lab.month_key()
    with session_scope() as db:
        acct = db.query(ShieldAccount).filter(ShieldAccount.member_id == member_id).with_for_update().first()
        if not acct:
            return jsonify({"message": "먼저 시작 금액으로 Shield 장부를 만드세요."}), 404
        if acct.last_rebalance_month == month:
            return jsonify({"message": f"{month} 리밸런싱은 이미 완료했습니다. 월 1회만 실행할 수 있습니다.", "doneThisMonth": True}), 409
        try:
            equity, ratio = _sweep_inputs(data, acct)
            plan = portfolio_lab.plan_sweep(equity, acct.baseline, acct.safe, ratio)
        except portfolio_lab.PortfolioError as exc:
            return _error(exc)
        acct.risky, acct.safe, acct.baseline, acct.last_rebalance_month = plan["riskyAfter"], plan["safeAfter"], plan["newBaseline"], month
        db.add(ShieldLedger(member_id=member_id, month=month, risky_before=plan["riskyBefore"], gain=plan["gain"],
                            swept=plan["swept"], risky_after=plan["riskyAfter"], safe_after=plan["safeAfter"]))
        db.flush()
        return jsonify({"plan": plan, "account": _account_json(acct)}), 201


# ── Claude explanation (optional) ─────────────────────────────────────────────
def call_claude(prompt, api_key):
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": EXPLAIN_MODEL, "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]}, timeout=40)
    response.raise_for_status()
    return "".join(block.get("text", "") for block in response.json().get("content", []) if block.get("type") == "text").strip()


EXPLAIN_RULES = ("아래 JSON은 서버가 계산한 결과입니다. 이 숫자만 근거로 한국어로 6문장 이내로 쉽게 해석하세요. "
                 "JSON에 없는 숫자·종목·전망을 만들어내지 말고, 수익을 보장하는 표현은 쓰지 마세요. "
                 "마지막에 이 결과의 한계(과거 데이터 기반, 추정 오차)를 한 문장으로 덧붙이세요.")


@ai_portfolio_bp.post("/explain")
def explain():
    denied = _login_required()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    kind = {"sniper": "상관관계와 효율적 투자선 결과", "kelly": "켈리 공식 베팅 비율 결과"}.get(data.get("kind"))
    if not kind:
        return jsonify({"message": "kind는 sniper 또는 kelly여야 합니다."}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"available": False, "reason": "ANTHROPIC_API_KEY가 설정되지 않아 AI 해설을 만들 수 없습니다. 계산 결과는 그대로 유효합니다."})
    payload = json.dumps(data.get("result"), ensure_ascii=False)[:12000]
    try:
        text_out = call_claude(f"{EXPLAIN_RULES}\n\n[{kind}]\n{payload}", api_key)
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"available": False, "reason": f"AI 해설 호출에 실패했습니다: {exc.__class__.__name__}"})
    return jsonify({"available": True, "text": text_out, "model": EXPLAIN_MODEL,
                    "note": "해설은 AI가 위 계산 결과를 바탕으로 작성한 글이며 투자 권유가 아닙니다."})
