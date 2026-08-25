from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import SecretStr

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcClientConfig,
    DgcFetchResult,
    DgcTransportError,
)
from tests.support.tiered_dgc import (
    DataStatus,
    DgcInterface,
    SourceMode,
    TieredConfigurationError,
    TieredDgcConfig,
    build_tiered_source,
    data_status,
    load_tiered_settings,
    tiered_config,
)
from tax_risk.config import Settings


class _RealSource:
    def __init__(self, result: DgcFetchResult) -> None:
        self.result = result
        self.closed = False

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        del parameters
        return self.result

    def close(self) -> None:
        self.closed = True


class _FailingRealSource:
    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        del parameters
        raise DgcTransportError("real interface is unavailable")

    def close(self) -> None:
        return None


def test_missing_credentials_selects_mock_even_when_default_url_exists() -> None:
    factory_called = False

    def factory(config: DgcClientConfig) -> _RealSource:
        nonlocal factory_called
        del config
        factory_called = True
        return _RealSource(_result("real"))

    source = build_tiered_source(_config(), _result("mock"), real_source_factory=factory)

    result = source.fetch({"company": "3000"})

    assert source.mode is SourceMode.MOCK
    assert result.records[0]["value"] == "mock"
    assert factory_called is False


def test_complete_credentials_select_real_independent_of_enabled_flag() -> None:
    settings = Settings(
        dgc_sap_trial_balance_enabled=False,
        dgc_sap_trial_balance_api_url="https://dgc.example.test/trial-balance",
        dgc_sap_trial_balance_app_key=SecretStr("key"),
        dgc_sap_trial_balance_app_secret=SecretStr("secret"),
    )
    real = _RealSource(_result("real"))
    source = build_tiered_source(
        tiered_config(settings, DgcInterface.SAP_TRIAL_BALANCE),
        _result("mock"),
        real_source_factory=lambda config: real,
    )

    result = source.fetch({})

    assert source.mode is SourceMode.REAL
    assert result.records[0]["value"] == "real"
    source.close()
    assert real.closed is True


def test_invoice_detail_tiered_config_uses_independent_settings() -> None:
    settings = Settings(
        dgc_invoice_detail_enabled=False,
        dgc_invoice_detail_api_url="https://dgc.example.test/invoice-detail",
        dgc_invoice_detail_app_key=SecretStr("invoice-key"),
        dgc_invoice_detail_app_secret=SecretStr("invoice-secret"),
        dgc_invoice_detail_page_size=654,
    )

    config = tiered_config(settings, DgcInterface.INVOICE_DETAIL)

    assert config.interface is DgcInterface.INVOICE_DETAIL
    assert config.api_url == "https://dgc.example.test/invoice-detail"
    assert config.app_key == "invoice-key"
    assert config.app_secret == "invoice-secret"
    assert config.page_size == 654


def test_hesi_invoice_tiered_config_uses_independent_settings() -> None:
    settings = Settings(
        dgc_hesi_invoice_enabled=False,
        dgc_hesi_invoice_api_url="https://dgc.example.test/hesi-invoice",
        dgc_hesi_invoice_app_key=SecretStr("hesi-invoice-key"),
        dgc_hesi_invoice_app_secret=SecretStr("hesi-invoice-secret"),
        dgc_hesi_invoice_page_size=100,
    )

    config = tiered_config(settings, DgcInterface.HESI_INVOICE)

    assert config.interface is DgcInterface.HESI_INVOICE
    assert config.api_url == "https://dgc.example.test/hesi-invoice"
    assert config.app_key == "hesi-invoice-key"
    assert config.app_secret == "hesi-invoice-secret"
    assert config.page_size == 100


def test_tiered_config_repr_does_not_disclose_credentials() -> None:
    rendered = repr(_config(app_key="key", app_secret="controlled-secret"))

    assert "controlled-secret" not in rendered
    assert "app_secret=" not in rendered


def test_process_environment_overrides_local_infra_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / ".env").write_text(
        "DGC_APP_KEY=file-key\nDGC_APP_SECRET=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DGC_APP_KEY", "environment-key")
    monkeypatch.setenv("DGC_APP_SECRET", "environment-secret")

    settings = load_tiered_settings(tmp_path)

    assert settings.dgc_app_key is not None
    assert settings.dgc_app_secret is not None
    assert settings.dgc_app_key.get_secret_value() == "environment-key"
    assert settings.dgc_app_secret.get_secret_value() == "environment-secret"


def test_real_failure_is_not_replaced_by_mock_data() -> None:
    config = _config(app_key="key", app_secret="secret")
    source = build_tiered_source(
        config,
        _result("mock"),
        real_source_factory=lambda client_config: _FailingRealSource(),
    )

    with pytest.raises(DgcTransportError, match="real interface"):
        source.fetch({})

    assert source.mode is SourceMode.REAL


def test_successful_empty_real_response_stays_real_and_is_no_data() -> None:
    empty = DgcFetchResult(records=(), checksum="0" * 64)
    source = build_tiered_source(
        _config(app_key="key", app_secret="secret"),
        _result("mock"),
        real_source_factory=lambda client_config: _RealSource(empty),
    )

    result = source.fetch({})

    assert source.mode is SourceMode.REAL
    assert result.records == ()
    assert data_status(result) is DataStatus.NO_DATA


@pytest.mark.parametrize(
    ("app_key", "app_secret", "api_url", "message"),
    [
        ("key", None, "https://dgc.example.test/profit", "AppKey and AppSecret"),
        (None, "secret", "https://dgc.example.test/profit", "AppKey and AppSecret"),
        ("key", "secret", None, "API URL"),
    ],
)
def test_partial_real_configuration_fails_instead_of_using_mock(
    app_key: str | None,
    app_secret: str | None,
    api_url: str | None,
    message: str,
) -> None:
    with pytest.raises(TieredConfigurationError, match=message):
        build_tiered_source(
            _config(app_key=app_key, app_secret=app_secret, api_url=api_url),
            _result("mock"),
        )


def _config(
    *,
    api_url: str | None = "https://dgc.example.test/profit",
    app_key: str | None = None,
    app_secret: str | None = None,
) -> TieredDgcConfig:
    return TieredDgcConfig(
        interface=DgcInterface.SAP_PROFIT,
        api_url=api_url,
        app_key=app_key,
        app_secret=app_secret,
        page_size=100,
        timeout=5,
        max_pages=10,
        max_records=1_000,
        max_page_bytes=1_000_000,
        max_total_bytes=2_000_000,
        token_ttl=60,
        tls_server_name=None,
        tls_pinned_certificate_sha256=None,
    )


def _result(value: str) -> DgcFetchResult:
    return DgcFetchResult(records=({"value": value},), checksum="a" * 64)
