"""PostgreSQL-backed quant data browser and moving-average backtest API."""
import os
import math
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, jsonify, request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from exit_score import ExitParams, backtest as exit_backtest, compute_scores
from flows import parse_flows_csv


quant_bp = Blueprint("quant", __name__, url_prefix="/api/quant")
_engine = None


def _url():
    return os.environ.get(
        "QUANT_DATABASE_URL",
        "postgresql+psycopg://quant:quant@postgres:5432/quant_research",
    )


def _db():
    global _engine
    if _engine is None:
        _engine = create_engine(_url(), pool_pre_ping=True, pool_size=3, max_overflow=2)
    return _engine


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(result):
    return [{key: _number(value) for key, value in row.items()} for row in result.mappings()]


def _solve(matrix, vector):
    """Gaussian elimination for the small normal-equation systems used below."""
    size = len(vector)
    augmented = [list(matrix[index]) + [vector[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("팩터 데이터가 서로 너무 유사해 회귀분석을 할 수 없습니다.")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            ratio = augmented[row][column]
            augmented[row] = [value - ratio * base for value, base in zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(size)]


def _regression(rows, factors):
    # OLS: asset excess return = alpha + factor loadings * factor returns.
    x = [[1.0] + [float(row[name]) for name in factors] for row in rows]
    y = [float(row["asset_return"]) - float(row["risk_free"]) for row in rows]
    columns = len(x[0])
    xtx = [[sum(row[i] * row[j] for row in x) for j in range(columns)] for i in range(columns)]
    xty = [sum(row[i] * y[index] for index, row in enumerate(x)) for i in range(columns)]
    coefficients = _solve(xtx, xty)
    predicted = [sum(row[index] * coefficients[index] for index in range(columns)) for row in x]
    mean_y = sum(y) / len(y)
    total = sum((value - mean_y) ** 2 for value in y)
    residual = sum((value - predicted[index]) ** 2 for index, value in enumerate(y))
    return {"alpha_daily": coefficients[0], "loadings": dict(zip(factors, coefficients[1:])),
            "r_squared": 1 - residual / total if total else 0.0, "observations": len(rows)}


def _ensure_factor_schema(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS factor_returns (
          factor_date date PRIMARY KEY, risk_free numeric(12,8) NOT NULL, market_excess numeric(12,8) NOT NULL,
          smb numeric(12,8) NOT NULL, hml numeric(12,8) NOT NULL, rmw numeric(12,8) NOT NULL,
          cma numeric(12,8) NOT NULL, mom numeric(12,8) NOT NULL, source varchar(100) NOT NULL DEFAULT 'learning_sample'
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS factor_exposures (
          symbol varchar(20) NOT NULL, model varchar(20) NOT NULL, factor_name varchar(30) NOT NULL,
          start_date date NOT NULL, end_date date NOT NULL, loading numeric(18,8) NOT NULL,
          alpha_annual numeric(18,8), r_squared numeric(18,8), observations integer NOT NULL,
          calculated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(symbol,model,factor_name,start_date,end_date)
        )
    """))
    conn.execute(text("""
        INSERT INTO factor_returns(factor_date,risk_free,market_excess,smb,hml,rmw,cma,mom)
        SELECT factor_day, 0.00008, round((0.00035 + sin(n / 13.0) * 0.006)::numeric,8),
          round((cos(n / 9.0) * 0.0025)::numeric,8), round((sin(n / 17.0) * 0.0022)::numeric,8),
          round((cos(n / 21.0) * 0.0018)::numeric,8), round((sin(n / 25.0) * 0.0015)::numeric,8),
          round((sin(n / 11.0) * 0.0030)::numeric,8)
        FROM (SELECT DISTINCT trade_time::date AS factor_day, row_number() OVER (ORDER BY trade_time::date)-1 AS n FROM market_data) d
        ON CONFLICT(factor_date) DO NOTHING
    """))


_STRATEGY_NAMES = {
    "ma2050": "이동평균 교차 MA 20/50",
    "trend": "단기 추세추종 MA 5/20",
    "pullback": "상승추세 눌림목 MA 20/60",
    "rsi": "RSI 과매도 반등",
    "breakout": "거래량 20일 돌파",
    "momentum": "60일 모멘텀",
}


def _average(values):
    return sum(values) / len(values) if values else None


def _strategy_signals(prices, strategy):
    """Generate long-only signals without using any future candle data."""
    closes = [float(row["adjusted_close"]) for row in prices]
    volumes = [float(row["volume"]) for row in prices]
    ma5 = [_average(closes[max(0, index - 4):index + 1]) for index in range(len(closes))]
    ma10 = [_average(closes[max(0, index - 9):index + 1]) for index in range(len(closes))]
    ma20 = [_average(closes[max(0, index - 19):index + 1]) for index in range(len(closes))]
    ma50 = [_average(closes[max(0, index - 49):index + 1]) for index in range(len(closes))]
    ma60 = [_average(closes[max(0, index - 59):index + 1]) for index in range(len(closes))]
    gains, losses, rsi = [], [], []
    for index, close in enumerate(closes):
        change = close - closes[index - 1] if index else 0
        gains.append(max(change, 0)); losses.append(max(-change, 0))
        avg_gain = _average(gains[max(0, index - 13):index + 1])
        avg_loss = _average(losses[max(0, index - 13):index + 1])
        rsi.append(100 if not avg_loss else 100 - 100 / (1 + avg_gain / avg_loss))
    in_position = False
    signals = []
    for index, row in enumerate(prices):
        buy = sell = False
        if strategy == "ma2050":
            buy = index > 0 and ma20[index] > ma50[index] and ma20[index - 1] <= ma50[index - 1]
            sell = index > 0 and ma20[index] < ma50[index] and ma20[index - 1] >= ma50[index - 1]
        elif strategy == "trend":
            buy = index > 0 and ma5[index] > ma20[index] and ma5[index - 1] <= ma20[index - 1]
            sell = index > 0 and ma5[index] < ma20[index] and ma5[index - 1] >= ma20[index - 1]
        elif strategy == "pullback":
            buy = index > 60 and ma20[index] > ma60[index] and closes[index] > ma20[index] and closes[index - 1] <= ma20[index - 1]
            sell = index > 60 and ma20[index] < ma60[index]
        elif strategy == "rsi":
            buy = index > 14 and rsi[index] > 30 and rsi[index - 1] <= 30
            sell = index > 14 and rsi[index] < 70 and rsi[index - 1] >= 70
        elif strategy == "breakout":
            prior_high = max(closes[max(0, index - 20):index]) if index >= 20 else None
            prior_volume = _average(volumes[max(0, index - 20):index]) if index >= 20 else None
            buy = index >= 20 and closes[index] > prior_high and volumes[index] > prior_volume
            sell = index >= 20 and closes[index] < ma10[index]
        elif strategy == "momentum":
            buy = index >= 60 and closes[index] / closes[index - 60] - 1 > 0 and closes[index] > ma60[index]
            sell = index >= 60 and (closes[index] / closes[index - 60] - 1 <= 0 or closes[index] < ma60[index])
        if buy and not in_position:
            signals.append({**row, "signal": "BUY"}); in_position = True
        elif sell and in_position:
            signals.append({**row, "signal": "SELL"}); in_position = False
    # Close an open position on the final available price to make each run comparable.
    if in_position and prices:
        signals.append({**prices[-1], "signal": "SELL"})
    return signals


def _params():
    symbol = request.args.get("symbol", "005930").upper().strip()
    if not symbol or len(symbol) > 20 or not all(char.isalnum() or char in "-_" for char in symbol):
        raise ValueError("유효한 symbol을 입력하세요.")
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    return symbol, limit


@quant_bp.get("/overview")
def overview():
    try:
        with _db().connect() as conn:
            result = conn.execute(text("""
                SELECT (SELECT count(*) FROM market_data) AS market_rows,
                       (SELECT count(*) FROM strategies) AS strategy_count,
                       (SELECT count(*) FROM trade_logs) AS trade_count,
                       (SELECT array_agg(symbol ORDER BY symbol) FROM (SELECT DISTINCT symbol FROM market_data) s) AS symbols
            """))
            row = _rows(result)[0]
        return jsonify(row)
    except SQLAlchemyError as exc:
        return jsonify({"message": f"퀀트 PostgreSQL에 연결할 수 없습니다: {exc.__class__.__name__}"}), 503


@quant_bp.get("/market-data")
def market_data():
    try:
        symbol, limit = _params()
        with _db().connect() as conn:
            rows = _rows(conn.execute(text("""
                SELECT symbol, trade_time, open, high, low, close, volume, adjusted_close
                FROM market_data WHERE symbol = :symbol
                ORDER BY trade_time DESC LIMIT :limit
            """), {"symbol": symbol, "limit": limit}))
        return jsonify({"rows": rows, "source": "PostgreSQL market_data"})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"데이터 조회 실패: {exc.__class__.__name__}"}), 503


@quant_bp.get("/signals")
def signals():
    try:
        symbol, limit = _params()
        fast = max(2, min(int(request.args.get("fast", 20)), 100))
        slow = max(fast + 1, min(int(request.args.get("slow", 50)), 250))
        sql = text("""
            WITH ma AS (
                SELECT symbol, trade_time, adjusted_close,
                       avg(adjusted_close) OVER (PARTITION BY symbol ORDER BY trade_time ROWS BETWEEN :fast PRECEDING AND CURRENT ROW) AS fast_ma,
                       avg(adjusted_close) OVER (PARTITION BY symbol ORDER BY trade_time ROWS BETWEEN :slow PRECEDING AND CURRENT ROW) AS slow_ma
                FROM market_data WHERE symbol = :symbol
            ), crossed AS (
                SELECT *, lag(fast_ma > slow_ma) OVER (ORDER BY trade_time) AS prior_above FROM ma
            )
            SELECT symbol, trade_time, adjusted_close, fast_ma, slow_ma,
                   CASE WHEN fast_ma > slow_ma AND coalesce(prior_above, false) = false THEN 'BUY'
                        WHEN fast_ma < slow_ma AND coalesce(prior_above, true) = true THEN 'SELL'
                        ELSE 'HOLD' END AS signal
            FROM crossed ORDER BY trade_time DESC LIMIT :limit
        """)
        with _db().connect() as conn:
            rows = _rows(conn.execute(sql, {"symbol": symbol, "fast": fast - 1, "slow": slow - 1, "limit": limit}))
        return jsonify({"rows": list(reversed(rows)), "fast": fast, "slow": slow})
    except (ValueError, SQLAlchemyError) as exc:
        return jsonify({"message": f"시그널 조회 실패: {exc}"}), 400


@quant_bp.post("/backtests")
def run_backtest():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "005930")).upper().strip()
    fast, slow = int(data.get("fast", 20)), int(data.get("slow", 50))
    quantity = max(1, min(int(data.get("quantity", 10)), 100000))
    fee_rate = max(0.0, min(float(data.get("feeRate", 0.00015)), 0.02))
    slippage = max(0.0, min(float(data.get("slippage", 0.0005)), 0.02))
    strategy = str(data.get("strategy", "ma2050")).strip().lower()
    if not symbol or fast < 2 or slow <= fast or slow > 250 or strategy not in _STRATEGY_NAMES:
        return jsonify({"message": "symbol과 이동평균 기간을 확인하세요."}), 400
    try:
        with _db().begin() as conn:
            strategy_id = conn.execute(text("""
                INSERT INTO strategies(name, parameters) VALUES (:name, CAST(:params AS jsonb)) RETURNING strategy_id
            """), {"name": f"{_STRATEGY_NAMES[strategy]} {symbol}", "params": __import__("json").dumps(data)}).scalar_one()
            price_rows = _rows(conn.execute(text("""
                SELECT trade_time, adjusted_close, volume FROM market_data WHERE symbol=:symbol ORDER BY trade_time
            """), {"symbol": symbol}))
            signal_rows = _strategy_signals(price_rows, strategy)
            entry = None
            trades = []
            for row in signal_rows:
                if row["signal"] == "BUY" and entry is None:
                    entry = row
                    trades.append((row, "BUY", 0.0))
                elif row["signal"] == "SELL" and entry is not None:
                    buy_cost = entry["adjusted_close"] * quantity * (1 + fee_rate + slippage)
                    sell_value = row["adjusted_close"] * quantity * (1 - fee_rate - slippage)
                    trades.append((row, "SELL", sell_value - buy_cost))
                    entry = None
            for row, side, pnl in trades:
                price = row["adjusted_close"] * (1 + slippage if side == "BUY" else 1 - slippage)
                conn.execute(text("""
                    INSERT INTO trade_logs(strategy_id,symbol,trade_time,side,price,quantity,fee,slippage,pnl)
                    VALUES(:id,:symbol,:time,:side,:price,:quantity,:fee,:slippage,:pnl)
                """), {"id": strategy_id, "symbol": symbol, "time": row["trade_time"], "side": side, "price": price,
                       "quantity": quantity, "fee": row["adjusted_close"] * quantity * fee_rate, "slippage": row["adjusted_close"] * quantity * slippage, "pnl": pnl})
            realized = [pnl for _, side, pnl in trades if side == "SELL"]
            total_pnl = sum(realized)
            first = signal_rows[0]["trade_time"] if signal_rows else datetime.now(timezone.utc)
            last = signal_rows[-1]["trade_time"] if signal_rows else first
            capital = (signal_rows[0]["adjusted_close"] if signal_rows else 1) * quantity
            total_return = (total_pnl / capital * 100) if capital else 0
            equity = capital
            peak = capital
            max_drawdown = 0.0
            periodic_returns = []
            for pnl in realized:
                prior_equity = equity
                equity += pnl
                if prior_equity:
                    periodic_returns.append(pnl / prior_equity)
                peak = max(peak, equity)
                if peak:
                    max_drawdown = min(max_drawdown, (equity / peak - 1) * 100)
            if len(periodic_returns) > 1:
                mean_return = sum(periodic_returns) / len(periodic_returns)
                deviation = math.sqrt(sum((value - mean_return) ** 2 for value in periodic_returns) / (len(periodic_returns) - 1))
                sharpe = mean_return / deviation * math.sqrt(252) if deviation else None
            else:
                sharpe = None
            first_dt = datetime.fromisoformat(str(first))
            last_dt = datetime.fromisoformat(str(last))
            days = max((last_dt - first_dt).days, 1)
            annual_return = ((equity / capital) ** (365 / days) - 1) * 100 if capital > 0 and equity > 0 else total_return
            conn.execute(text("""
                INSERT INTO performance_metrics(strategy_id,start_date,end_date,sharpe_ratio,max_drawdown,annual_return,total_return,trade_count)
                VALUES(:id,:start,:end,:sharpe,:mdd,:annual,:total,:count)
            """), {"id": strategy_id, "start": first, "end": last, "sharpe": sharpe, "mdd": max_drawdown,
                   "annual": annual_return, "total": total_return, "count": len(trades)})
        return jsonify({"strategyId": strategy_id, "tradeCount": len(trades), "realizedPnl": round(total_pnl, 2),
                        "totalReturn": round(total_return, 4), "sharpeRatio": round(sharpe, 4) if sharpe is not None else None,
                        "maxDrawdown": round(max_drawdown, 4), "annualReturn": round(annual_return, 4),
                        "message": "백테스트 결과와 거래 로그를 PostgreSQL에 저장했습니다."}), 201
    except (ValueError, SQLAlchemyError) as exc:
        return jsonify({"message": f"백테스트 실행 실패: {exc}"}), 400


@quant_bp.get("/results")
def results():
    try:
        with _db().connect() as conn:
            strategies = _rows(conn.execute(text("""
                SELECT s.strategy_id, s.name, s.parameters, s.created_at, p.total_return, p.annual_return, p.trade_count
                FROM strategies s LEFT JOIN performance_metrics p ON p.strategy_id=s.strategy_id
                ORDER BY s.strategy_id DESC LIMIT 20
            """)))
            trades = _rows(conn.execute(text("""
                SELECT trade_id,strategy_id,symbol,trade_time,side,price,quantity,fee,slippage,pnl
                FROM trade_logs ORDER BY trade_id DESC LIMIT 50
            """)))
        return jsonify({"strategies": strategies, "trades": trades})
    except SQLAlchemyError as exc:
        return jsonify({"message": f"결과 조회 실패: {exc.__class__.__name__}"}), 503


@quant_bp.get("/factor-analysis")
def factor_analysis():
    """Calculate CAPM alpha/beta and Fama-French-style exposures from daily returns."""
    try:
        symbol, _ = _params()
        with _db().begin() as conn:
            _ensure_factor_schema(conn)
            rows = _rows(conn.execute(text("""
                WITH prices AS (
                  SELECT trade_time::date factor_date, adjusted_close,
                         lag(adjusted_close) OVER (ORDER BY trade_time) prior_close
                  FROM market_data WHERE symbol=:symbol
                )
                SELECT p.factor_date, (p.adjusted_close / p.prior_close - 1) AS asset_return,
                       f.risk_free, f.market_excess, f.smb, f.hml, f.rmw, f.cma, f.mom
                FROM prices p JOIN factor_returns f ON f.factor_date=p.factor_date
                WHERE p.prior_close IS NOT NULL ORDER BY p.factor_date
            """), {"symbol": symbol}))
            if len(rows) < 60:
                return jsonify({"message": "팩터 분석에는 최소 60개 이상의 가격 관측치가 필요합니다."}), 400
            capm = _regression(rows, ["market_excess"])
            multi_factors = ["market_excess", "smb", "hml", "rmw", "cma", "mom"]
            multi = _regression(rows, multi_factors)
            start, end = rows[0]["factor_date"], rows[-1]["factor_date"]
            alpha_annual = capm["alpha_daily"] * 252 * 100
            for name, loading in multi["loadings"].items():
                conn.execute(text("""
                    INSERT INTO factor_exposures(symbol,model,factor_name,start_date,end_date,loading,alpha_annual,r_squared,observations)
                    VALUES(:symbol,'FF6',:name,:start,:end,:loading,:alpha,:r2,:count)
                    ON CONFLICT(symbol,model,factor_name,start_date,end_date) DO UPDATE SET
                      loading=EXCLUDED.loading, alpha_annual=EXCLUDED.alpha_annual, r_squared=EXCLUDED.r_squared,
                      observations=EXCLUDED.observations, calculated_at=now()
                """), {"symbol": symbol, "name": name, "start": start, "end": end, "loading": loading,
                       "alpha": alpha_annual, "r2": multi["r_squared"], "count": multi["observations"]})
        labels = {"market_excess": "시장 베타", "smb": "규모(SMB)", "hml": "가치(HML)",
                  "rmw": "수익성(RMW)", "cma": "투자(CMA)", "mom": "모멘텀(MOM)"}
        return jsonify({"symbol": symbol, "period": {"start": start, "end": end},
                        "capm": {"alphaAnnual": round(alpha_annual, 4), "beta": round(capm["loadings"]["market_excess"], 4),
                                 "rSquared": round(capm["r_squared"], 4), "observations": capm["observations"]},
                        "multiFactor": {"rSquared": round(multi["r_squared"], 4), "observations": multi["observations"],
                                        "exposures": [{"name": name, "label": labels[name], "loading": round(value, 4)} for name, value in multi["loadings"].items()]},
                        "source": "learning_sample",
                        "notice": "팩터 수익률은 교육용 샘플입니다. 실제 운용에는 검증된 시장·팩터 데이터로 교체해야 합니다."})
    except (ValueError, SQLAlchemyError) as exc:
        return jsonify({"message": f"팩터 분석 실패: {exc}"}), 400


# --- Exit score: price trend + foreign/institution flows (CSV upload) -------------
_MAX_CSV_BYTES = 1_000_000


def _ensure_flow_schema(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS investor_flows (
          symbol varchar(20) NOT NULL, flow_date date NOT NULL,
          foreign_net numeric(24,2) NOT NULL, institution_net numeric(24,2) NOT NULL,
          individual_net numeric(24,2), source varchar(50) NOT NULL DEFAULT 'csv_upload',
          uploaded_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(symbol, flow_date)
        )
    """))
    conn.execute(text("ALTER TABLE investor_flows ADD COLUMN IF NOT EXISTS name varchar(100)"))


def _load_bars_and_flows(conn, symbol, max_bars=1000):
    # Adjust open/high/low by the same factor as adjusted_close so the series is consistent.
    rows = _rows(conn.execute(text("""
        SELECT to_char(trade_time AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD') AS date,
               open * adjusted_close / NULLIF(close, 0) AS open, high * adjusted_close / NULLIF(close, 0) AS high,
               low * adjusted_close / NULLIF(close, 0) AS low, adjusted_close AS close, volume
        FROM market_data WHERE symbol = :symbol AND close > 0
        ORDER BY trade_time DESC LIMIT :limit
    """), {"symbol": symbol, "limit": max_bars}))
    bars = [{**row, **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")}} for row in reversed(rows)]
    flows = []
    if bars:
        flows = [{"date": row["date"], "foreign": float(row["foreign"]), "institution": float(row["institution"])}
                 for row in _rows(conn.execute(text("""
            SELECT to_char(flow_date, 'YYYY-MM-DD') AS date, foreign_net AS foreign, institution_net AS institution
            FROM investor_flows WHERE symbol = :symbol AND flow_date >= :start ORDER BY flow_date
        """), {"symbol": symbol, "start": bars[0]["date"]}))]
    return bars, flows


def _exit_params(source):
    def number(key, default, low, high, cast=float):
        return max(low, min(cast(source.get(key, default)), high))
    return ExitParams(
        reduce_at=number("reduceAt", 4, 1, 9, int), exit_at=number("exitAt", 6, 2, 9, int),
        atr_mult=number("atrMult", 3.0, 0.5, 10.0), confirm_days=number("confirmDays", 3, 1, 20, int),
    ).validate()


@quant_bp.post("/investor-flows")
def upload_investor_flows():
    """Store daily foreign/institution net-buy amounts parsed from a CSV export."""
    try:
        symbol = request.args.get("symbol", "").upper().strip()
        if not symbol or len(symbol) > 20 or not all(char.isalnum() or char in "-_" for char in symbol):
            raise ValueError("유효한 symbol을 입력하세요.")
        upload = request.files.get("file")
        raw = upload.read(_MAX_CSV_BYTES + 1) if upload else request.get_data(cache=False)[:_MAX_CSV_BYTES + 1]
        if len(raw) > _MAX_CSV_BYTES:
            raise ValueError("CSV는 1MB 이하만 업로드할 수 있습니다.")
        try:
            text_body = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text_body = raw.decode("cp949", errors="strict")  # Korean HTS exports
        rows = parse_flows_csv(text_body)
        if not rows:
            raise ValueError("데이터 행이 없습니다.")
        with _db().begin() as conn:
            _ensure_flow_schema(conn)
            conn.execute(text("""
                INSERT INTO investor_flows(symbol, flow_date, foreign_net, institution_net, individual_net)
                VALUES(:symbol, :date, :foreign, :institution, :individual)
                ON CONFLICT(symbol, flow_date) DO UPDATE SET foreign_net=EXCLUDED.foreign_net,
                  institution_net=EXCLUDED.institution_net, individual_net=EXCLUDED.individual_net,
                  source='csv_upload', uploaded_at=now()
            """), [{"symbol": symbol, "date": r["date"], "foreign": r["foreign"], "institution": r["institution"],
                    "individual": r.get("individual")} for r in rows])
        return jsonify({"symbol": symbol, "rows": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"],
                        "message": "투자자별 수급 데이터를 저장했습니다. 금액 단위는 업로드한 CSV 그대로입니다."}), 201
    except (ValueError, UnicodeDecodeError) as exc:
        return jsonify({"message": f"업로드 실패: {exc}"}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"저장 실패: {exc.__class__.__name__}"}), 503


@quant_bp.get("/exit-score")
def exit_score():
    """Latest exit scores (HOLD / REDUCE / EXIT) with the rules that fired."""
    try:
        symbol, limit = _params()
        with _db().begin() as conn:
            _ensure_flow_schema(conn)
            bars, flows = _load_bars_and_flows(conn, symbol)
        if len(bars) < 61:
            return jsonify({"message": "매도 점수 계산에는 최소 61개 이상의 가격 데이터가 필요합니다."}), 400
        scores = compute_scores(bars, flows, _exit_params(request.args))
        closes = {bar["date"]: bar["close"] for bar in bars}
        rows = [{**item, "close": closes[item["date"]]} for item in scores[-limit:]]
        return jsonify({"symbol": symbol, "rows": rows, "latest": rows[-1], "flowDays": len(flows), "barDays": len(bars),
                        "notice": "수급 데이터가 없는 날짜는 수급 점수를 계산하지 않습니다. 신호는 다음 거래일 시가 체결을 가정합니다."})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"점수 조회 실패: {exc.__class__.__name__}"}), 503


@quant_bp.post("/exit-backtest")
def exit_score_backtest():
    data = request.get_json(silent=True) or {}
    try:
        symbol = str(data.get("symbol", "005930")).upper().strip()
        if not symbol or len(symbol) > 20 or not all(char.isalnum() or char in "-_" for char in symbol):
            raise ValueError("유효한 symbol을 입력하세요.")
        fee_rate = max(0.0, min(float(data.get("feeRate", 0.00015)), 0.02))
        slippage = max(0.0, min(float(data.get("slippage", 0.0005)), 0.02))
        with _db().begin() as conn:
            _ensure_flow_schema(conn)
            bars, flows = _load_bars_and_flows(conn, symbol)
        if len(bars) < 61:
            return jsonify({"message": "백테스트에는 최소 61개 이상의 가격 데이터가 필요합니다."}), 400
        result = exit_backtest(bars, flows, _exit_params(data), fee_rate=fee_rate, slippage=slippage)
        return jsonify({"symbol": symbol, "totalReturn": round(result["total_return"] * 100, 4),
                        "buyAndHoldReturn": round(result["buy_and_hold_return"] * 100, 4),
                        "maxDrawdown": round(result["max_drawdown"] * 100, 4), "tradeCount": result["trade_count"],
                        "trades": result["trades"][-50:], "latest": result["last_signal"], "flowDays": len(flows),
                        "barDays": len(bars), "params": result["params"]})
    except (ValueError, TypeError) as exc:
        return jsonify({"message": f"백테스트 실행 실패: {exc}"}), 400
    except SQLAlchemyError as exc:
        return jsonify({"message": f"백테스트 실행 실패: {exc.__class__.__name__}"}), 503
