"""Dashboard API: indices & briefing, investor-flow ranking, overtime movers, DART fundamentals,
holdings & watchlist, and the condition-expression lab.

Honesty rules: every payload carries its source and as-of time; an upstream failure becomes
``available: false`` with a reason — never a made-up number. Offline fallback quotes from
``stock_market`` (flagged ``simulated``) are filtered out of anything shown as market data.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request, session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import condition_lab
import dart_client
import stock_market
from admin import _require_admin
from db import engine, session_scope
from flows import parse_flows_csv
from models import StockPosition, StockWatchlist
from quant import _db as quant_db, _ensure_flow_schema, _load_bars_and_flows

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")

INDEX_SYMBOLS = [
    ("KOSPI", "^KS11", "지수"), ("KOSDAQ", "^KQ11", "지수"), ("S&P 500", "^GSPC", "지수"), ("나스닥", "^IXIC", "지수"),
    ("달러/원", "KRW=X", "환율"), ("미 10년물 금리(%)", "^TNX", "금리"), ("WTI 유가($)", "CL=F", "원자재"),
]
_ttl_cache: dict = {}
_ttl_lock = threading.Lock()
MAX_CSV_BYTES = 1_000_000
SYMBOL_RE = re.compile(r"^\d{6}$")


def _cached(key, ttl, build):
    now = time.time()
    with _ttl_lock:
        hit = _ttl_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = build()
    with _ttl_lock:
        _ttl_cache[key] = (now, value)
    return value


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dashboard_tables():
    statement = """CREATE TABLE IF NOT EXISTS stock_watchlist (
      stock_watchlist_id BIGINT AUTO_INCREMENT PRIMARY KEY,
      member_id BIGINT NOT NULL,
      symbol VARCHAR(20) NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uq_stock_watchlist_member_symbol (member_id, symbol),
      CONSTRAINT fk_stock_watchlist_member FOREIGN KEY (member_id) REFERENCES member(member_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    with engine.begin() as conn:
        conn.execute(text(statement))


# ── 1. indices & briefing ─────────────────────────────────────────────────────
def fetch_index(symbol):
    """Strict index quote from Yahoo Finance; raises on any failure (no fallback values)."""
    import yfinance as yf
    hist = yf.Ticker(symbol).history(period="5d")
    if hist.empty or len(hist) < 2:
        raise ValueError("시세 이력이 부족합니다.")
    last, prev = hist.iloc[-1], hist.iloc[-2]
    price, prev_close = float(last["Close"]), float(prev["Close"])
    return {"price": round(price, 2), "change": round(price - prev_close, 2),
            "changeRate": round((price - prev_close) / prev_close * 100, 2) if prev_close else None,
            "asOf": hist.index[-1].strftime("%Y-%m-%d")}


def get_indices():
    def one(item):
        name, symbol, group = item
        try:
            return {"name": name, "symbol": symbol, "group": group, "available": True, **fetch_index(symbol)}
        except Exception as exc:  # noqa: BLE001 - any upstream failure must degrade to "unavailable"
            return {"name": name, "symbol": symbol, "group": group, "available": False, "reason": exc.__class__.__name__}
    with ThreadPoolExecutor(max_workers=len(INDEX_SYMBOLS)) as pool:
        return list(pool.map(one, INDEX_SYMBOLS))


def _live_quotes():
    return [q for q in stock_market.get_dashboard_stock_quotes().values() if not q.get("simulated")]


def build_briefing(indices, quotes):
    """Deterministic summary of the numbers we actually have; no commentary beyond the data."""
    bullets, by_name = [], {i["name"]: i for i in indices if i.get("available")}
    ks, kq = by_name.get("KOSPI"), by_name.get("KOSDAQ")
    if ks and kq and ks["changeRate"] is not None and kq["changeRate"] is not None:
        up, down = ks["changeRate"] > 0, ks["changeRate"] < 0
        tone = "모두 상승" if up and kq["changeRate"] > 0 else "모두 하락" if down and kq["changeRate"] < 0 else "방향이 엇갈림"
        bullets.append({"kind": "market", "text": f"국내 증시: KOSPI {ks['price']:,.2f}({ks['changeRate']:+.2f}%), "
                                                  f"KOSDAQ {kq['price']:,.2f}({kq['changeRate']:+.2f}%) — {tone}"})
    for name, label in (("S&P 500", "미국"), ("나스닥", "미국")):
        item = by_name.get(name)
        if item and item["changeRate"] is not None:
            bullets.append({"kind": "global", "text": f"{label} {name} {item['price']:,.2f}({item['changeRate']:+.2f}%) · 기준일 {item['asOf']}"})
    for name in ("달러/원", "미 10년물 금리(%)", "WTI 유가($)"):
        item = by_name.get(name)
        if item and item["changeRate"] is not None:
            bullets.append({"kind": "macro", "text": f"{name} {item['price']:,.2f}({item['changeRate']:+.2f}%)"})
    movers = sorted(quotes, key=lambda q: q.get("changeRate", 0), reverse=True)
    if movers:
        top = ", ".join(f"{q['name']}({q['changeRate']:+.2f}%)" for q in movers[:3] if q.get("changeRate", 0) > 0)
        low = ", ".join(f"{q['name']}({q['changeRate']:+.2f}%)" for q in movers[::-1][:3] if q.get("changeRate", 0) < 0)
        if top:
            bullets.append({"kind": "movers", "text": f"등록 종목 중 상승 상위: {top}"})
        if low:
            bullets.append({"kind": "movers", "text": f"등록 종목 중 하락 상위: {low}"})
    return bullets


@dashboard_bp.get("/overview")
def overview():
    indices = _cached("indices", 60, get_indices)
    quotes = _cached("live_quotes", 30, _live_quotes)
    return jsonify({
        "asOf": _now(), "indices": indices, "briefing": build_briefing(indices, quotes),
        "sources": ["Yahoo Finance(yfinance) 일봉 종가 기준 — 장중 실시간이 아닐 수 있음", "Naver 증권 다종목 시세(등록 종목)"],
        "limits": ["브리핑은 위 수치만으로 자동 생성한 요약이며 뉴스·해설은 포함하지 않습니다.",
                   "수신에 실패한 항목은 표시하지 않고 '수신 실패'로 남깁니다."],
        "quotesAvailable": bool(quotes),
    })


# ── 2. investor flows & overtime ──────────────────────────────────────────────
def rank_flows(rows, top_n=15):
    """rows: dicts with symbol, name, foreign_net, institution_net, days -> three rankings."""
    def pick(key, positive_only=True):
        items = [r for r in rows if (r[key] > 0 or not positive_only)]
        items.sort(key=lambda r: r[key], reverse=True)
        return [{"rank": i, **r} for i, r in enumerate(items[:top_n], start=1)]
    both = [dict(r, combined=r["foreign_net"] + r["institution_net"]) for r in rows
            if r["foreign_net"] > 0 and r["institution_net"] > 0]
    both.sort(key=lambda r: r["combined"], reverse=True)
    return {"foreign": pick("foreign_net"), "institution": pick("institution_net"),
            "both": [{"rank": i, **r} for i, r in enumerate(both[:top_n], start=1)]}


@dashboard_bp.get("/flows")
def flows_top():
    try:
        days = max(1, min(int(request.args.get("days", 1)), 20))
        with quant_db().begin() as conn:
            _ensure_flow_schema(conn)
            data = conn.execute(text("""
                WITH recent AS (SELECT DISTINCT flow_date FROM investor_flows ORDER BY flow_date DESC LIMIT :days)
                SELECT symbol, max(name) AS name, sum(foreign_net) AS foreign_net, sum(institution_net) AS institution_net,
                       count(*) AS days, max(flow_date) AS last_date, min(flow_date) AS first_date
                FROM investor_flows WHERE flow_date IN (SELECT flow_date FROM recent) GROUP BY symbol
            """), {"days": days}).mappings().all()
    except (ValueError, SQLAlchemyError) as exc:
        return jsonify({"available": False, "reason": f"수급 데이터를 읽지 못했습니다: {exc.__class__.__name__}"}), 200
    if not data:
        return jsonify({"available": False, "reason": "업로드된 수급 데이터가 없습니다. 관리자가 투자자별 매매동향 CSV를 업로드해야 합니다."})
    rows = [{"symbol": r["symbol"], "name": r["name"] or stock_market.STOCKS.get(r["symbol"], {}).get("name") or r["symbol"],
             "foreign_net": float(r["foreign_net"]), "institution_net": float(r["institution_net"]), "days": r["days"]}
            for r in data]
    return jsonify({"available": True, "days": days, "asOf": str(max(r["last_date"] for r in data)),
                    "from": str(min(r["first_date"] for r in data)), "symbols": len(rows),
                    "source": "관리자가 업로드한 투자자별 매매동향 CSV(금액 단위는 파일 그대로)",
                    "note": "순위는 업로드된 종목 범위 안에서의 순위입니다. 전 종목 순위가 아닐 수 있습니다.", **rank_flows(rows)})


@dashboard_bp.post("/flows/upload")
def flows_upload():
    if not _require_admin():
        return jsonify({"message": "관리자만 업로드할 수 있습니다."}), 403
    try:
        upload = request.files.get("file")
        raw = upload.read(MAX_CSV_BYTES + 1) if upload else request.get_data(cache=False)[:MAX_CSV_BYTES + 1]
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError("CSV는 1MB 이하만 업로드할 수 있습니다.")
        try:
            body = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            body = raw.decode("cp949")
        rows = parse_flows_csv(body, max_rows=30000, multi_symbol=True)
        if not rows:
            raise ValueError("데이터 행이 없습니다.")
        with quant_db().begin() as conn:
            _ensure_flow_schema(conn)
            conn.execute(text("""
                INSERT INTO investor_flows(symbol, flow_date, foreign_net, institution_net, individual_net, name)
                VALUES(:symbol, :date, :foreign, :institution, :individual, :name)
                ON CONFLICT(symbol, flow_date) DO UPDATE SET foreign_net=EXCLUDED.foreign_net,
                  institution_net=EXCLUDED.institution_net, individual_net=EXCLUDED.individual_net,
                  name=COALESCE(EXCLUDED.name, investor_flows.name), source='csv_upload', uploaded_at=now()
            """), [{"symbol": r["symbol"], "date": r["date"], "foreign": r["foreign"], "institution": r["institution"],
                    "individual": r.get("individual"), "name": r.get("name") or None} for r in rows])
        return jsonify({"rows": len(rows), "symbols": len({r["symbol"] for r in rows}),
                        "start": min(r["date"] for r in rows), "end": max(r["date"] for r in rows)}), 201
    except (ValueError, UnicodeDecodeError) as exc:
        return jsonify({"message": f"업로드 실패: {exc}"}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"저장 실패: {exc.__class__.__name__}"}), 503


def parse_overtime(row, info):
    """Naver ``basic`` payload -> overtime quote, or ``None`` if the symbol has no overtime price."""
    over = (row or {}).get("overMarketPriceInfo")
    if not isinstance(over, dict):
        return None
    price = stock_market._to_number(over.get("overPrice"))
    try:
        rate = float(str(over.get("fluctuationsRatio", "")).replace(",", ""))
    except ValueError:
        return None
    if price <= 0:
        return None
    change = stock_market._to_number(over.get("compareToPreviousClosePrice"))
    if rate < 0 and change > 0:
        change = -change
    return {"symbol": info["symbol"], "name": info["name"], "market": info.get("market", ""), "price": price,
            "change": change, "changeRate": round(rate, 2), "status": over.get("overMarketStatus", ""),
            "tradedAt": over.get("localTradedAt", "")}


def _overtime_universe():
    universe = {s: {"symbol": s, **info} for s, info in stock_market.STOCKS.items()}
    member_id = session.get("member_id")
    if member_id:
        with session_scope() as db:
            extra = [r.symbol for r in db.query(StockWatchlist).filter(StockWatchlist.member_id == member_id)] + \
                    [r.symbol for r in db.query(StockPosition).filter(StockPosition.member_id == member_id)]
        for symbol in extra:
            info = stock_market.get_stock_info(symbol)
            if info:
                universe.setdefault(symbol, {"symbol": symbol, **info})
    return list(universe.values())[:40]


def fetch_overtime(info):
    response = requests.get(f"{stock_market.NAVER_STOCK_API}/{info['symbol']}/basic",
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}, timeout=5)
    response.raise_for_status()
    return parse_overtime(response.json(), info)


@dashboard_bp.get("/overtime")
def overtime():
    universe = _overtime_universe()

    def one(info):
        try:
            return fetch_overtime(info), None
        except Exception as exc:  # noqa: BLE001
            return None, exc.__class__.__name__
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, universe))
    quotes = [q for q, _ in results if q]
    failed = sum(1 for _, err in results if err)
    risers = sorted((q for q in quotes if q["changeRate"] > 0), key=lambda q: q["changeRate"], reverse=True)[:15]
    if not quotes:
        return jsonify({"available": False, "reason": "시간외 시세를 받지 못했습니다(장 시간 외이거나 수신 실패).",
                        "checked": len(universe), "failed": failed})
    return jsonify({"available": True, "asOf": _now(), "checked": len(universe), "withOvertime": len(quotes), "failed": failed,
                    "rows": [{"rank": i, **q} for i, q in enumerate(risers, start=1)],
                    "source": "Naver 증권 종목별 시간외 시세", "note": "등록 종목과 내 보유·관심종목 범위에서만 집계합니다."})


# ── 3. fundamentals & contract disclosures ────────────────────────────────────
def _dart_guard(fn):
    try:
        return fn()
    except dart_client.DartNotConfigured as exc:
        return jsonify({"available": False, "configured": False, "reason": str(exc)})
    except (dart_client.DartError, requests.RequestException) as exc:
        return jsonify({"available": False, "configured": True, "reason": f"DART 조회 실패: {exc}"}), 200


@dashboard_bp.get("/disclosures/contracts")
def contract_disclosures():
    days = max(1, min(int(request.args.get("days", 7) or 7), 30))
    return _dart_guard(lambda: jsonify({
        "available": True, "asOf": _now(), "days": days, "source": "OpenDART 거래소공시(단일판매·공급계약)",
        "rows": _cached(("contracts", days), 1800, lambda: dart_client.fetch_contract_disclosures(days)),
        "note": "계약금액·상대방은 공시 원문에서 확인하세요(링크)."}))


@dashboard_bp.get("/fundamentals")
def fundamentals():
    def build():
        year = dart_client.default_business_year()
        corp_map = dart_client.corp_code_map()
        universe = _overtime_universe()

        def one(info):
            corp = corp_map.get(info["symbol"])
            if not corp:
                return {"symbol": info["symbol"], "name": info["name"], "status": "판단 불가", "reason": "DART 고유번호 없음"}
            try:
                fin = _cached(("fin", corp, year), 86_400, lambda: dart_client.fetch_financials(corp, year))
                return {"symbol": info["symbol"], "name": info["name"], **dart_client.analyze_fundamentals(fin)}
            except (dart_client.DartError, requests.RequestException) as exc:
                return {"symbol": info["symbol"], "name": info["name"], "status": "판단 불가", "reason": str(exc)[:80]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(one, universe))
        order = {"흑자전환": 0, "적자 축소": 1, "영업이익 증가": 2}
        rows.sort(key=lambda r: (order.get(r["status"], 9), r["symbol"]))
        return jsonify({"available": True, "asOf": _now(), "businessYear": year, "rows": rows,
                        "source": f"OpenDART 단일회사 주요계정 {year}년 사업보고서(연결 우선)",
                        "note": "ROE=당기순이익/기말 자본총계, 부채비율=부채총계/자본총계로 단순 계산했습니다. "
                                "분기 실적은 반영하지 않으므로 최근 분기 턴어라운드는 놓칠 수 있습니다."})
    return _dart_guard(build)


# ── 4. holdings & watchlist ───────────────────────────────────────────────────
def _login_required():
    if not session.get("member_id"):
        return jsonify({"error": "UNAUTHORIZED", "message": "로그인이 필요합니다."}), 401
    return None


def _quote_or_none(symbol):
    try:
        quote = stock_market.get_quote_cached(symbol)
    except Exception:  # noqa: BLE001
        return None
    return quote


@dashboard_bp.get("/holdings")
def holdings():
    denied = _login_required()
    if denied:
        return denied
    member_id = session["member_id"]
    with session_scope() as db:
        positions = [(p.symbol, p.quantity, p.avg_price) for p in db.query(StockPosition).filter(StockPosition.member_id == member_id)]
    rows = []
    for symbol, quantity, avg in positions:
        info = stock_market.get_stock_info(symbol) or {}
        quote = _quote_or_none(symbol)
        price = quote["price"] if quote else None
        rows.append({"symbol": symbol, "name": info.get("name", symbol), "quantity": quantity, "avgPrice": avg,
                     "price": price, "changeRate": quote.get("changeRate") if quote else None,
                     "evalAmount": price * quantity if price else None,
                     "pnl": (price - avg) * quantity if price else None,
                     "pnlRate": round((price - avg) / avg * 100, 2) if price and avg else None,
                     "simulated": bool(quote and quote.get("simulated"))})
    total_eval = sum(r["evalAmount"] for r in rows if r["evalAmount"] is not None)
    total_cost = sum(r["avgPrice"] * r["quantity"] for r in rows if r["evalAmount"] is not None)
    return jsonify({"asOf": _now(), "rows": rows, "totalEval": total_eval,
                    "totalPnl": total_eval - total_cost, "totalPnlRate": round((total_eval - total_cost) / total_cost * 100, 2) if total_cost else None,
                    "note": "모의투자 포지션입니다. '시뮬레이션 가격' 표시는 실시간 시세를 받지 못해 기준가로 대체된 값입니다."})


@dashboard_bp.get("/watchlist")
def watchlist():
    denied = _login_required()
    if denied:
        return denied
    member_id = session["member_id"]
    with session_scope() as db:
        symbols = [r.symbol for r in db.query(StockWatchlist).filter(StockWatchlist.member_id == member_id)
                   .order_by(StockWatchlist.created_at.desc())]
    rows = []
    for symbol in symbols:
        info = stock_market.get_stock_info(symbol) or {}
        quote = _quote_or_none(symbol)
        rows.append({"symbol": symbol, "name": info.get("name", symbol), "market": info.get("market", ""),
                     "price": quote["price"] if quote else None, "changeRate": quote.get("changeRate") if quote else None,
                     "simulated": bool(quote and quote.get("simulated"))})
    return jsonify({"asOf": _now(), "rows": rows, "limit": 50})


@dashboard_bp.put("/watchlist/<symbol>")
def watchlist_add(symbol):
    denied = _login_required()
    if denied:
        return denied
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return jsonify({"message": "종목코드는 숫자 6자리입니다."}), 400
    info = stock_market.get_stock_info(symbol)
    if not info:
        return jsonify({"message": "상장 종목에서 찾을 수 없는 코드입니다."}), 404
    member_id = session["member_id"]
    with session_scope() as db:
        query = db.query(StockWatchlist).filter(StockWatchlist.member_id == member_id)
        if query.filter(StockWatchlist.symbol == symbol).first():
            return jsonify({"symbol": symbol, "name": info["name"], "added": False})
        if query.count() >= 50:
            return jsonify({"message": "관심종목은 최대 50개까지 담을 수 있습니다."}), 400
        db.add(StockWatchlist(member_id=member_id, symbol=symbol))
    return jsonify({"symbol": symbol, "name": info["name"], "added": True}), 201


@dashboard_bp.delete("/watchlist/<symbol>")
def watchlist_remove(symbol):
    denied = _login_required()
    if denied:
        return denied
    symbol = symbol.strip().upper()
    with session_scope() as db:
        removed = db.query(StockWatchlist).filter(StockWatchlist.member_id == session["member_id"],
                                                  StockWatchlist.symbol == symbol).delete()
    return jsonify({"symbol": symbol, "removed": bool(removed)})


# ── 5. condition lab ──────────────────────────────────────────────────────────
@dashboard_bp.get("/conditions/presets")
def condition_presets():
    return jsonify({"presets": condition_lab.PRESETS, "help": condition_lab.__doc__})


def _naver_bars(symbol):
    candles = stock_market._fetch_naver_chart(symbol, "1y", include_ma=True)  # strict: raises, never simulated
    return [{"date": datetime.fromtimestamp(c["x"] / 1000).strftime("%Y-%m-%d"), "open": float(c["o"]), "high": float(c["h"]),
             "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"])} for c in candles]


def _num(data, key, default, low, high):
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        raise condition_lab.ConditionError(f"{key} 값이 숫자가 아닙니다.")
    return max(low, min(value, high))


@dashboard_bp.post("/conditions/backtest")
def condition_backtest():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper()
    source = str(data.get("source", "db")).lower()
    try:
        if not re.fullmatch(r"[0-9A-Z_\-]{1,20}", symbol):
            raise condition_lab.ConditionError("유효한 종목코드를 입력하세요.")
        if source not in ("db", "naver"):
            raise condition_lab.ConditionError("source는 db 또는 naver여야 합니다.")
        buy, sell = str(data.get("buy", "")), str(data.get("sell", ""))
        condition_lab.parse_expression(buy)  # validate early, before touching data sources
        if sell.strip():
            condition_lab.parse_expression(sell)
        stop = _num(data, "stopLoss", 0, 0, 0.9) or None
        take = _num(data, "takeProfit", 0, 0, 5) or None
        params = dict(max_hold=int(_num(data, "maxHold", 20, 0, 250)), stop_loss=stop, take_profit=take,
                      fee_rate=_num(data, "feeRate", 0.00015, 0, 0.02), slippage=_num(data, "slippage", 0.0005, 0, 0.02))
        try:
            with quant_db().begin() as conn:
                _ensure_flow_schema(conn)
                bars, flows = _load_bars_and_flows(conn, symbol)
        except SQLAlchemyError:
            if source == "db":
                raise
            bars, flows = [], []  # Naver prices don't need the DB; flows are just an optional extra
        if source == "naver":
            bars = _naver_bars(symbol)
        if len(bars) < 60:
            raise condition_lab.ConditionError(f"가격 데이터가 부족합니다({len(bars)}일). 최소 60일이 필요합니다.")
        return jsonify({
            "symbol": symbol, "source": "PostgreSQL market_data" if source == "db" else "Naver 일봉(수정주가)",
            "bars": len(bars), "from": bars[0]["date"], "to": bars[-1]["date"], "flowDays": len(flows),
            "study": condition_lab.signal_study(bars, flows, buy),
            "trade": condition_lab.trade_backtest(bars, flows, buy, sell, **params),
            "notice": "신호는 다음 거래일 시가에 체결된다고 가정하며 수수료·슬리피지를 반영했습니다. 과거 성과는 미래를 보장하지 않습니다."})
    except condition_lab.ConditionError as exc:
        return jsonify({"message": str(exc)}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"데이터베이스 조회 실패: {exc.__class__.__name__}"}), 503
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        return jsonify({"message": f"가격 데이터를 가져오지 못했습니다: {exc}"}), 502
