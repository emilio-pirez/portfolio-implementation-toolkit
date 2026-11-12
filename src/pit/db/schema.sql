-- Generated from src/pit/db/models.py by pit.db.session.render_schema_sql().
-- Do not edit by hand: run `pit db-schema > src/pit/db/schema.sql`.

CREATE TABLE audit_log (
	id INTEGER NOT NULL,
	timestamp DATETIME NOT NULL,
	event VARCHAR(64) NOT NULL,
	details_json TEXT NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_audit_log_event ON audit_log (event);
CREATE INDEX ix_audit_log_timestamp ON audit_log (timestamp);

CREATE TABLE backtest_daily (
	run_id VARCHAR(36) NOT NULL,
	scenario VARCHAR(32) NOT NULL,
	date DATE NOT NULL,
	nav FLOAT NOT NULL,
	flow FLOAT NOT NULL,
	fund_return FLOAT,
	benchmark_return FLOAT,
	cash_pct FLOAT NOT NULL,
	unequitized_pct FLOAT NOT NULL,
	exposure FLOAT NOT NULL,
	futures_notional FLOAT NOT NULL,
	cash_drag FLOAT,
	hard_breaches INTEGER NOT NULL,
	soft_breaches INTEGER NOT NULL,
	PRIMARY KEY (run_id, scenario, date)
);

CREATE TABLE benchmark_weights (
	date DATE NOT NULL,
	ticker VARCHAR(16) NOT NULL,
	weight FLOAT NOT NULL,
	market_cap FLOAT,
	source VARCHAR(16) NOT NULL,
	PRIMARY KEY (date, ticker),
	CONSTRAINT ck_source_valid CHECK (source IN ('FMP', 'FRED', 'SYNTHETIC', 'INTERNAL'))
);

CREATE TABLE cash_flows (
	id INTEGER NOT NULL,
	date DATE NOT NULL,
	amount FLOAT NOT NULL,
	type VARCHAR(16) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_amount_positive CHECK (amount > 0),
	CONSTRAINT ck_type_valid CHECK (type IN ('SUBSCRIPTION', 'REDEMPTION'))
);
CREATE INDEX ix_cash_flows_date ON cash_flows (date);

CREATE TABLE compliance_checks (
	id INTEGER NOT NULL,
	batch_id VARCHAR(36) NOT NULL,
	check_name VARCHAR(64) NOT NULL,
	value FLOAT,
	limit_value VARCHAR(64),
	passed BOOLEAN NOT NULL,
	severity VARCHAR(4) NOT NULL,
	message TEXT,
	PRIMARY KEY (id),
	CONSTRAINT ck_severity_valid CHECK (severity IN ('HARD', 'SOFT'))
);
CREATE INDEX ix_compliance_checks_batch_id ON compliance_checks (batch_id);

CREATE TABLE orders (
	id INTEGER NOT NULL,
	batch_id VARCHAR(36) NOT NULL,
	date DATE NOT NULL,
	ticker VARCHAR(16) NOT NULL,
	side VARCHAR(4) NOT NULL,
	quantity FLOAT NOT NULL,
	est_price FLOAT NOT NULL,
	est_notional FLOAT NOT NULL,
	est_cost FLOAT NOT NULL,
	status VARCHAR(10) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_quantity_positive CHECK (quantity > 0),
	CONSTRAINT ck_side_valid CHECK (side IN ('BUY', 'SELL')),
	CONSTRAINT ck_status_valid CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'FILLED'))
);
CREATE INDEX ix_orders_batch_id ON orders (batch_id);

CREATE TABLE positions (
	date DATE NOT NULL,
	ticker VARCHAR(16) NOT NULL,
	quantity FLOAT NOT NULL,
	price FLOAT NOT NULL,
	market_value FLOAT NOT NULL,
	PRIMARY KEY (date, ticker)
);

CREATE TABLE prices (
	ticker VARCHAR(16) NOT NULL,
	date DATE NOT NULL,
	close FLOAT NOT NULL,
	volume FLOAT,
	source VARCHAR(16) NOT NULL,
	PRIMARY KEY (ticker, date),
	CONSTRAINT ck_close_positive CHECK (close > 0),
	CONSTRAINT ck_source_valid CHECK (source IN ('FMP', 'FRED', 'SYNTHETIC', 'INTERNAL'))
);

CREATE TABLE rates (
	series VARCHAR(16) NOT NULL,
	date DATE NOT NULL,
	value FLOAT NOT NULL,
	source VARCHAR(16) NOT NULL,
	PRIMARY KEY (series, date),
	CONSTRAINT ck_source_valid CHECK (source IN ('FMP', 'FRED', 'SYNTHETIC', 'INTERNAL'))
);

CREATE TABLE securities (
	ticker VARCHAR(16) NOT NULL,
	name VARCHAR(128),
	sector VARCHAR(64),
	asset_type VARCHAR(8) NOT NULL,
	source VARCHAR(16) NOT NULL,
	PRIMARY KEY (ticker),
	CONSTRAINT ck_asset_type_valid CHECK (asset_type IN ('EQUITY', 'FUTURE', 'CASH', 'INDEX')),
	CONSTRAINT ck_source_valid CHECK (source IN ('FMP', 'FRED', 'SYNTHETIC', 'INTERNAL'))
);

CREATE TABLE target_weights (
	as_of DATE NOT NULL,
	ticker VARCHAR(16) NOT NULL,
	weight FLOAT NOT NULL,
	PRIMARY KEY (as_of, ticker)
);
