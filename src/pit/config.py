"""Configuration loading.

Two sources of configuration are kept deliberately separate:

* ``Settings`` - secrets and environment-specific values (API keys) read from
  environment variables or a local ``.env`` file. Never committed.
* ``AppConfig`` - business parameters (limits, contract multipliers, cost
  assumptions) read from ``config.yaml``. Committed and reviewable, which is
  part of the governance story: every limit the toolkit enforces is visible in
  one file.
"""

from __future__ import annotations

import os
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_ENV_VAR = "PIT_CONFIG_PATH"


# --------------------------------------------------------------------------- secrets


class Settings(BaseSettings):
    """Secrets and environment-specific values (from env vars or ``.env``)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    fmp_api_key: str | None = None
    fred_api_key: str | None = None

    @property
    def has_market_data_keys(self) -> bool:
        """True when both API keys are present, i.e. online mode is possible."""
        return bool(self.fmp_api_key and self.fred_api_key)


# --------------------------------------------------------------------------- business config


class _StrictModel(BaseModel):
    """Base model that rejects unknown keys, so typos in config.yaml fail loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(StrEnum):
    """HARD breaches block a basket; SOFT breaches raise an alert for manual review."""

    HARD = "HARD"
    SOFT = "SOFT"


class FundConfig(_StrictModel):
    name: str
    base_currency: str = "USD"
    initial_nav: float = Field(gt=0)
    benchmark: str = "SPX"


class DataConfig(_StrictModel):
    db_url: str
    target_weights_file: str
    offline_universe_file: str
    cash_flows_file: str
    restricted_list_file: str
    history_start: date
    index_symbol: str = "^GSPC"
    fmp_base_url: str
    fred_base_url: str
    risk_free_series: str = "DTB3"
    synthetic_seed: int = 42
    synthetic_risk_free_rate: float = Field(ge=0, lt=1)


class ContractSpec(_StrictModel):
    description: str
    multiplier: float = Field(gt=0, description="USD per index point")


class FuturesConfig(_StrictModel):
    index_dividend_yield: float = Field(ge=0, lt=1)
    contracts: dict[str, ContractSpec]
    roll_window_business_days: int = Field(ge=0)
    target_beta: float = Field(gt=0)

    @model_validator(mode="after")
    def _require_es_and_mes(self) -> FuturesConfig:
        missing = {"ES", "MES"} - self.contracts.keys()
        if missing:
            raise ValueError(f"futures.contracts must define {sorted(missing)}")
        return self


class EquitizationConfig(_StrictModel):
    cash_buffer_pct: float = Field(ge=0, lt=1)


class OrdersConfig(_StrictModel):
    round_lot: int = Field(ge=1)
    no_trade_threshold_bps: float = Field(ge=0)


class CostConfig(_StrictModel):
    commission_bps: float = Field(ge=0)
    half_spread_bps: float = Field(ge=0)

    @property
    def total_bps(self) -> float:
        """All-in estimated one-way cost in basis points."""
        return self.commission_bps + self.half_spread_bps


class LimitRule(_StrictModel):
    """A single upper bound, e.g. max issuer weight <= 8 %."""

    limit: float = Field(ge=0)
    severity: Severity


class RangeRule(_StrictModel):
    """A two-sided bound, e.g. 0.25 % <= cash <= 3 %."""

    min: float
    max: float
    severity: Severity

    @model_validator(mode="after")
    def _min_le_max(self) -> RangeRule:
        if self.min > self.max:
            raise ValueError(f"range min ({self.min}) must be <= max ({self.max})")
        return self


class FlagRule(_StrictModel):
    """A check with no numeric limit (e.g. restricted list)."""

    severity: Severity


class ComplianceConfig(_StrictModel):
    max_issuer_weight: LimitRule
    max_sector_weight: LimitRule
    sector_active_weight: LimitRule
    cash_range: RangeRule
    total_exposure_range: RangeRule
    max_tracking_error: LimitRule
    beta_range: RangeRule
    holdings_count_range: RangeRule
    restricted_list: FlagRule
    max_order_pct_adv: LimitRule


class RiskConfig(_StrictModel):
    lookback_days: int = Field(gt=1)
    annualization_factor: int = Field(gt=0)
    covariance_shrinkage: str = Field(pattern="^(none|ledoit_wolf)$")


class AppConfig(_StrictModel):
    fund: FundConfig
    data: DataConfig
    futures: FuturesConfig
    equitization: EquitizationConfig
    orders: OrdersConfig
    costs: CostConfig
    compliance: ComplianceConfig
    risk: RiskConfig

    # Folder that relative paths in the config are resolved against (the
    # folder holding config.yaml), so commands work from any working directory.
    _base_dir: Path = PrivateAttr(default=PROJECT_ROOT)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve_path(self, path: str | Path) -> Path:
        """Absolute path for a file referenced in the config."""
        p = Path(path)
        return p if p.is_absolute() else self._base_dir / p

    @property
    def database_url(self) -> str:
        """``data.db_url`` with a relative SQLite file path made absolute."""
        url = self.data.db_url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith("sqlite:////"):
            rel = url[len(prefix) :]
            if rel and rel != ":memory:":
                return prefix + str(self.resolve_path(rel))
        return url


# --------------------------------------------------------------------------- loaders


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Pick the config file: explicit argument > ``PIT_CONFIG_PATH`` > repo default."""
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> AppConfig:
    """Read and validate ``config.yaml``. Raises ``FileNotFoundError`` or a
    pydantic ``ValidationError`` with a precise message if anything is wrong."""
    config_path = resolve_config_path(path)
    with config_path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    cfg = AppConfig.model_validate(raw)
    cfg._base_dir = config_path.resolve().parent
    return cfg


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Cached default configuration for application code."""
    return load_config()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached secrets/settings for application code."""
    return Settings()
