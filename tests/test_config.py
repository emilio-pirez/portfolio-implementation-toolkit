from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pit.config import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    AppConfig,
    Settings,
    Severity,
    load_config,
    resolve_config_path,
)


@pytest.fixture
def raw_config() -> dict:
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_default_config_loads() -> None:
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.fund.initial_nav == 500_000_000


def test_futures_multipliers() -> None:
    cfg = load_config()
    assert cfg.futures.contracts["ES"].multiplier == 50
    assert cfg.futures.contracts["MES"].multiplier == 5
    assert cfg.futures.index_dividend_yield == pytest.approx(0.013)


def test_compliance_limits_match_spec() -> None:
    c = load_config().compliance
    assert c.max_issuer_weight.limit == pytest.approx(0.08)
    assert c.max_issuer_weight.severity is Severity.HARD
    assert c.max_sector_weight.limit == pytest.approx(0.30)
    assert c.sector_active_weight.severity is Severity.SOFT
    assert (c.cash_range.min, c.cash_range.max) == pytest.approx((0.0025, 0.03))
    assert (c.total_exposure_range.min, c.total_exposure_range.max) == pytest.approx((0.98, 1.02))
    assert c.max_tracking_error.limit == pytest.approx(0.06)
    assert (c.beta_range.min, c.beta_range.max) == pytest.approx((0.85, 1.15))
    assert (c.holdings_count_range.min, c.holdings_count_range.max) == (30, 80)
    assert c.restricted_list.severity is Severity.HARD
    assert c.max_order_pct_adv.limit == pytest.approx(0.10)


def test_cost_total_bps() -> None:
    costs = load_config().costs
    assert costs.total_bps == pytest.approx(costs.commission_bps + costs.half_spread_bps)


def test_unknown_key_is_rejected(tmp_path: Path, raw_config: dict) -> None:
    raw_config["fund"]["typo_field"] = 1
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw_config))


def test_inverted_range_is_rejected(tmp_path: Path, raw_config: dict) -> None:
    raw_config["compliance"]["cash_range"] = {"min": 0.05, "max": 0.01, "severity": "HARD"}
    with pytest.raises(ValidationError, match="must be <= max"):
        load_config(_write(tmp_path, raw_config))


def test_missing_mes_contract_is_rejected(tmp_path: Path, raw_config: dict) -> None:
    del raw_config["futures"]["contracts"]["MES"]
    with pytest.raises(ValidationError, match="MES"):
        load_config(_write(tmp_path, raw_config))


def test_invalid_severity_is_rejected(tmp_path: Path, raw_config: dict) -> None:
    raw_config["compliance"]["max_issuer_weight"]["severity"] = "MEDIUM"
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw_config))


def test_config_path_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "other.yaml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(target))
    assert resolve_config_path() == target
    assert resolve_config_path("explicit.yaml") == Path("explicit.yaml")


def test_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert resolve_config_path() == DEFAULT_CONFIG_PATH


def test_settings_detect_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMP_API_KEY", "dummy")
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    assert Settings(_env_file=None).has_market_data_keys  # type: ignore[call-arg]


def test_settings_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert not Settings(_env_file=None).has_market_data_keys  # type: ignore[call-arg]
