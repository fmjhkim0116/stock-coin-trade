-- PostgreSQL quant research store.  This schema is intentionally separate
-- from the mock-investing account database so historical research data can
-- grow without affecting trading/account workloads.
CREATE TABLE IF NOT EXISTS market_data (
    symbol          varchar(20) NOT NULL,
    trade_time      timestamptz NOT NULL,
    open            numeric(18, 4) NOT NULL,
    high            numeric(18, 4) NOT NULL,
    low             numeric(18, 4) NOT NULL,
    close           numeric(18, 4) NOT NULL,
    volume          bigint NOT NULL,
    adjusted_close  numeric(18, 4) NOT NULL,
    PRIMARY KEY (symbol, trade_time)
) PARTITION BY RANGE (trade_time);

CREATE TABLE IF NOT EXISTS market_data_2025 PARTITION OF market_data
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE IF NOT EXISTS market_data_2026 PARTITION OF market_data
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE IF NOT EXISTS market_data_default PARTITION OF market_data DEFAULT;

CREATE INDEX IF NOT EXISTS idx_market_data_time_brin ON market_data USING brin (trade_time);
CREATE INDEX IF NOT EXISTS idx_market_data_symbol_time ON market_data (symbol, trade_time DESC);

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name varchar(100) NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_logs (
    trade_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id bigint NOT NULL REFERENCES strategies(strategy_id) ON DELETE CASCADE,
    symbol varchar(20) NOT NULL,
    trade_time timestamptz NOT NULL,
    side varchar(4) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price numeric(18, 4) NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    fee numeric(18, 4) NOT NULL DEFAULT 0,
    slippage numeric(18, 4) NOT NULL DEFAULT 0,
    pnl numeric(18, 4) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trade_logs_strategy_time ON trade_logs(strategy_id, trade_time);

CREATE TABLE IF NOT EXISTS performance_metrics (
    strategy_id bigint NOT NULL REFERENCES strategies(strategy_id) ON DELETE CASCADE,
    start_date timestamptz NOT NULL,
    end_date timestamptz NOT NULL,
    sharpe_ratio numeric(18, 6),
    max_drawdown numeric(18, 6),
    annual_return numeric(18, 6),
    total_return numeric(18, 6),
    trade_count integer NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy_id, start_date, end_date)
);

-- Daily factor returns for CAPM and Fama-French-style multi-factor analysis.
-- In production, load validated factor-provider data rather than these learning samples.
CREATE TABLE IF NOT EXISTS factor_returns (
    factor_date date PRIMARY KEY,
    risk_free numeric(12, 8) NOT NULL,
    market_excess numeric(12, 8) NOT NULL,
    smb numeric(12, 8) NOT NULL,
    hml numeric(12, 8) NOT NULL,
    rmw numeric(12, 8) NOT NULL,
    cma numeric(12, 8) NOT NULL,
    mom numeric(12, 8) NOT NULL,
    source varchar(100) NOT NULL DEFAULT 'learning_sample'
);

CREATE TABLE IF NOT EXISTS factor_exposures (
    symbol varchar(20) NOT NULL,
    model varchar(20) NOT NULL,
    factor_name varchar(30) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    loading numeric(18, 8) NOT NULL,
    alpha_annual numeric(18, 8),
    r_squared numeric(18, 8),
    observations integer NOT NULL,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, model, factor_name, start_date, end_date)
);
CREATE INDEX IF NOT EXISTS idx_factor_exposures_symbol ON factor_exposures(symbol, calculated_at DESC);

-- Deterministic, clearly labelled sample OHLCV. Replace via COPY/upsert in production.
INSERT INTO market_data (symbol, trade_time, open, high, low, close, volume, adjusted_close)
SELECT symbol, day::timestamptz,
       round((base + n * drift + sin(n / 7.0) * volatility)::numeric, 4),
       round((base + n * drift + sin(n / 7.0) * volatility + 2.2)::numeric, 4),
       round((base + n * drift + sin(n / 7.0) * volatility - 2.0)::numeric, 4),
       round((base + n * drift + sin(n / 7.0) * volatility + cos(n / 3.0))::numeric, 4),
       (1000000 + n * 1300)::bigint,
       round((base + n * drift + sin(n / 7.0) * volatility + cos(n / 3.0))::numeric, 4)
FROM (
    SELECT d AS day, row_number() OVER (ORDER BY d) - 1 AS n
    FROM generate_series('2025-01-02'::date, '2026-08-12'::date, interval '1 day') d
    WHERE extract(isodow FROM d) < 6
) days
CROSS JOIN (VALUES
    ('005930', 54000.0, 12.0, 420.0),
    ('000660', 145000.0, 31.0, 1050.0),
    ('KRW-BTC', 97000000.0, 42000.0, 1700000.0)
) AS assets(symbol, base, drift, volatility)
ON CONFLICT (symbol, trade_time) DO NOTHING;

INSERT INTO factor_returns (factor_date, risk_free, market_excess, smb, hml, rmw, cma, mom)
SELECT factor_day,
       0.00008,
       round((0.00035 + sin(n / 13.0) * 0.006)::numeric, 8),
       round((cos(n / 9.0) * 0.0025)::numeric, 8),
       round((sin(n / 17.0) * 0.0022)::numeric, 8),
       round((cos(n / 21.0) * 0.0018)::numeric, 8),
       round((sin(n / 25.0) * 0.0015)::numeric, 8),
       round((sin(n / 11.0) * 0.0030)::numeric, 8)
FROM (
    SELECT DISTINCT trade_time::date AS factor_day,
           row_number() OVER (ORDER BY trade_time::date) - 1 AS n
    FROM market_data
) dates
ON CONFLICT (factor_date) DO NOTHING;

-- Daily foreign/institution net-buy amounts uploaded from KRX/HTS CSV exports.
-- Amounts keep the unit of the uploaded file; the exit score only uses ratios and signs.
CREATE TABLE IF NOT EXISTS investor_flows (
    symbol          varchar(20)   NOT NULL,
    flow_date       date          NOT NULL,
    foreign_net     numeric(24,2) NOT NULL,
    institution_net numeric(24,2) NOT NULL,
    individual_net  numeric(24,2),
    source          varchar(50)   NOT NULL DEFAULT 'csv_upload',
    uploaded_at     timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, flow_date)
);
