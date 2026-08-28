from __future__ import annotations

import pytest

from tax_risk.config import Settings


DGC_FIELD_NAMES = (
    "client",
    "company_code",
    "company_name",
    "fiscal_year",
    "fiscal_period",
    "ledger",
    "line_number",
    "line_item",
    "current_month_amount",
    "year_to_date_amount",
)
DGC_METRIC_NAMES = (
    "cumulative_profit",
    "fair_value_change",
    "cumulative_revenue",
)
INDEPENDENT_DGC_INTERFACES = (
    (
        "dgc_sap_trial_balance",
        "https://dgc.example.test/trial-balance",
        "trial-balance",
    ),
    (
        "dgc_sap_account_balance",
        "https://dgc.example.test/account-balance",
        "account-balance",
    ),
    (
        "dgc_hesi_reimbursement",
        "https://dgc.example.test/hesi-reimbursement",
        "hesi-reimbursement",
    ),
    (
        "dgc_hesi_invoice",
        "https://dgc.example.test/hesi-invoice",
        "hesi-invoice",
    ),
    (
        "dgc_hesi_application",
        "https://dgc.example.test/hesi-application",
        "hesi-application",
    ),
    (
        "dgc_sap_dividend_detail",
        "https://dgc.example.test/dividend-detail",
        "dividend-detail",
    ),
    (
        "dgc_invoice_detail",
        "https://dgc.example.test/invoice-detail",
        "invoice-detail",
    ),
)


def _enabled_dgc_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "dgc_sap_profit_enabled": True,
        "dgc_sap_profit_api_url": "https://dgc.example.test/sap-profit",
        "dgc_app_key": "test-app-key",
        "dgc_app_secret": "test-app-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _enabled_independent_dgc_interface_settings(
    prefix: str,
    api_url: str,
    credential_label: str,
    **overrides: object,
) -> Settings:
    values: dict[str, object] = {
        f"{prefix}_enabled": True,
        f"{prefix}_api_url": api_url,
        f"{prefix}_app_key": f"{credential_label}-app-key",
        f"{prefix}_app_secret": f"{credential_label}-app-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _enabled_lark_refund_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "lark_refund_writeback_enabled": True,
        "lark_refund_base_url": "https://feishu.example.test/base/refund-base",
        "lark_refund_api_base_url": "https://open.feishu.example.test",
        "lark_refund_base_token": "refund-base",
        "lark_refund_table_id": "refund-table",
        "lark_refund_company_code_field_id": "company-code-field",
        "lark_refund_status_field_id": "refund-status-field",
        "lark_refund_app_id": "test-lark-app-id",
        "lark_refund_app_secret": "test-lark-app-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    (
        {},
        {"export_download_secret": "development-export-download-secret"},
        {"worker_scope_secret": "development-worker-scope-secret-change-me"},
    ),
)
def test_production_rejects_default_runtime_signing_secrets(
    overrides: dict[str, str],
) -> None:
    values = {
        "environment": "production",
        "export_download_secret": "production-export-secret-at-least-32-chars",
        "worker_scope_secret": "production-worker-secret-at-least-32-chars",
    }
    values.update(overrides)
    if not overrides:
        values.pop("export_download_secret")
        values.pop("worker_scope_secret")

    with pytest.raises(ValueError, match="production runtime secrets"):
        Settings(**values)  # type: ignore[arg-type]


def test_production_accepts_independent_nondefault_runtime_signing_secrets() -> None:
    settings = Settings(
        environment="production",
        export_download_secret="production-export-secret-at-least-32-chars",
        worker_scope_secret="production-worker-secret-at-least-32-chars",
    )

    assert settings.export_download_secret != settings.worker_scope_secret


def test_parallel_external_fetch_defaults_are_local_safe() -> None:
    settings = Settings()

    assert settings.external_fetch_enabled is False
    assert settings.external_fetch_cache_enabled is False
    assert settings.external_fetch_max_workers == 12
    assert settings.external_fetch_source_concurrency["dgc_sap_profit"] == 4
    assert settings.external_fetch_source_concurrency["dgc_hesi_reimbursement"] == 4
    assert settings.external_fetch_source_concurrency["dgc_hesi_invoice"] == 4
    assert settings.external_fetch_source_concurrency["dgc_hesi_application"] == 4
    assert settings.external_fetch_source_concurrency["dgc_invoice_detail"] == 4
    assert settings.external_fetch_empty_cache_ttl_seconds < (
        settings.external_fetch_cache_ttl_seconds
    )


def test_production_parallel_external_fetch_requires_redis_cache() -> None:
    with pytest.raises(ValueError, match="requires Redis cache"):
        Settings(
            environment="production",
            external_fetch_enabled=True,
            external_fetch_cache_enabled=False,
            export_download_secret="production-export-secret-at-least-32-chars",
            worker_scope_secret="production-worker-secret-at-least-32-chars",
        )


def test_production_parallel_external_fetch_accepts_bounded_cache_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EXTERNAL_FETCH_SOURCE_CONCURRENCY__DGC_SAP_PROFIT",
        "3",
    )
    settings = Settings(
        environment="production",
        external_fetch_enabled=True,
        external_fetch_cache_enabled=True,
        redis_url="rediss://redis.example.test:6380/0",
        external_fetch_max_workers=8,
        export_download_secret="production-export-secret-at-least-32-chars",
        worker_scope_secret="production-worker-secret-at-least-32-chars",
    )

    assert settings.external_fetch_source_concurrency["dgc_sap_profit"] == 3


@pytest.mark.parametrize(
    "overrides",
    (
        {"external_fetch_empty_cache_ttl_seconds": 901},
        {"external_fetch_lock_wait_seconds": 299},
        {"external_fetch_source_concurrency": {"dgc_sap_profit": 13}},
        {
            "external_fetch_retry_base_delay_seconds": 6,
            "external_fetch_retry_max_delay_seconds": 5,
        },
    ),
)
def test_external_fetch_rejects_inconsistent_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Settings(**overrides)  # type: ignore[arg-type]


def test_dgc_sap_profit_defaults_are_disabled_and_safe() -> None:
    settings = Settings()

    assert settings.dgc_sap_profit_enabled is False
    assert settings.dgc_sap_profit_api_url == "https://116.63.221.181/post/sapincome"
    assert settings.dgc_app_key is None
    assert settings.dgc_app_secret is None
    assert settings.dgc_iam_username is None
    assert settings.dgc_iam_password is None
    assert settings.dgc_iam_url == ("https://iam.cn-east-3.myhuaweicloud.com/v3/auth/tokens")
    assert settings.dgc_iam_domain == "hljtzb"
    assert settings.dgc_iam_project == "cn-east-3"
    assert settings.dgc_timeout_seconds == 30
    assert settings.dgc_page_size == 15_000
    assert settings.dgc_max_pages == 1_000
    assert settings.dgc_max_records == 100_000
    assert settings.dgc_max_page_bytes == 10 * 1024 * 1024
    assert settings.dgc_max_total_bytes == 64 * 1024 * 1024
    assert settings.dgc_token_ttl_seconds == 82_800
    assert settings.dgc_tls_server_name == "dgc.huaweicloud.com"
    assert settings.dgc_tls_pinned_certificate_sha256 == (
        "AF3850E5ACC206D12082BDD32E94AD4675F3AD7AB0AE23A247053DE9ED2883BF"
    )
    assert settings.dgc_sap_profit_field_map == {
        "client": "mandt",
        "company_code": "bukrs",
        "company_name": "companyname",
        "fiscal_year": "gjahr",
        "fiscal_period": "monat",
        "ledger": "rldnr",
        "line_number": "hs",
        "line_item": "ztext",
        "current_month_amount": "nmhsl",
        "year_to_date_amount": "nyhsl",
    }
    assert settings.dgc_sap_profit_metric_map == {
        "cumulative_profit": (
            "利润总额",
            "四、利润总额",
            "四、利润总额（损失以“－”号填列）",
            '四、利润总额(损失以"-"号填列)',
        ),
        "fair_value_change": (
            "公允价值变动收益",
            "公允价值变动损益",
            "公允价值变动收益（损失以“－”号填列）",
            '公允价值变动收益(损失以"-"号填列)',
        ),
        "cumulative_revenue": ("一、营业总收入", "营业收入"),
    }
    assert settings.dgc_sap_profit_ledger == "0L"
    assert settings.dgc_sap_trial_balance_enabled is False
    assert settings.dgc_sap_trial_balance_api_url == ("https://116.63.221.181/fin/trial_balance")
    assert settings.dgc_sap_trial_balance_app_key is None
    assert settings.dgc_sap_trial_balance_app_secret is None
    assert settings.dgc_sap_trial_balance_page_size == 1_000
    assert settings.dgc_sap_account_balance_enabled is False
    assert settings.dgc_sap_account_balance_api_url == (
        "https://116.63.221.181/post/sapaccountbalance"
    )
    assert settings.dgc_sap_account_balance_app_key is None
    assert settings.dgc_sap_account_balance_app_secret is None
    assert settings.dgc_sap_account_balance_page_size == 15_000
    assert settings.dgc_hesi_reimbursement_enabled is False
    assert settings.dgc_hesi_reimbursement_api_url == ("https://116.63.221.181/post/hesimingxi")
    assert settings.dgc_hesi_reimbursement_app_key is None
    assert settings.dgc_hesi_reimbursement_app_secret is None
    assert settings.dgc_hesi_reimbursement_page_size == 25
    assert settings.dgc_hesi_reimbursement_field_map == {
        "company_code": "company_code",
        "approval_completed_at": "flow_end_date",
        "expense_claim_code": "expense_code",
        "expense_type_code": "fee_type_code",
        "expense_type_amount": "fee_type_amount",
    }
    assert settings.dgc_hesi_invoice_enabled is False
    assert settings.dgc_hesi_invoice_api_url == "https://116.63.221.181/post/hesiinvoice"
    assert settings.dgc_hesi_invoice_app_key is None
    assert settings.dgc_hesi_invoice_app_secret is None
    assert settings.dgc_hesi_invoice_page_size == 25
    assert settings.dgc_hesi_invoice_field_map == {
        "company_code": "company_code",
        "expense_claim_code": "code",
        "invoice_id": "invoice_id",
        "expense_type_id": "feetypeid",
        "expense_line_amount": "amount_standard_dec",
        "invoice_approved_amount": "approve_amount_dec",
    }
    assert settings.dgc_hesi_application_enabled is False
    assert settings.dgc_hesi_application_api_url == "https://116.63.221.181/post/apply"
    assert settings.dgc_hesi_application_app_key is None
    assert settings.dgc_hesi_application_app_secret is None
    assert settings.dgc_hesi_application_page_size == 5_000
    assert settings.dgc_sap_dividend_detail_enabled is False
    assert settings.dgc_sap_dividend_detail_api_url == (
        "https://116.63.221.181/post/settlement_adjustment"
    )
    assert settings.dgc_sap_dividend_detail_app_key is None
    assert settings.dgc_sap_dividend_detail_app_secret is None
    assert settings.dgc_sap_dividend_detail_page_size == 15_000
    assert settings.dgc_invoice_detail_enabled is False
    assert settings.dgc_invoice_detail_api_url == ("https://116.63.221.181/post/writeoff")
    assert settings.dgc_invoice_detail_app_key is None
    assert settings.dgc_invoice_detail_app_secret is None
    assert settings.dgc_invoice_detail_page_size == 15_000
    assert settings.expected_migration_head == "0023_refund_ambiguous_match_alert"


def test_lark_refund_writeback_defaults_are_disabled_and_safe() -> None:
    settings = Settings()

    assert settings.lark_refund_writeback_enabled is False
    assert settings.lark_refund_base_url == (
        "https://hailiang.feishu.cn/base/A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
    )
    assert settings.lark_refund_api_base_url == "https://open.feishu.cn"
    assert settings.lark_refund_base_token == "A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
    assert settings.lark_refund_table_id == "tbl4PCNdcl4BYzgZ"
    assert settings.lark_refund_company_code_field_id == "fld5uBjB9R"
    assert settings.lark_refund_status_field_id == "fld4HLnqDk"
    assert settings.lark_refund_app_id is None
    assert settings.lark_refund_app_secret is None
    assert settings.lark_refund_timeout_seconds == 30
    assert settings.lark_refund_page_size == 100
    assert settings.lark_refund_max_retries == 3


def test_lark_refund_writeback_can_be_enabled_with_complete_safe_settings() -> None:
    settings = _enabled_lark_refund_settings(
        lark_refund_base_url=" https://feishu.example.test/base/refund-base ",
        lark_refund_api_base_url=" https://open.feishu.example.test ",
        lark_refund_base_token=" refund-base ",
        lark_refund_table_id=" refund-table ",
        lark_refund_company_code_field_id=" company-code-field ",
        lark_refund_status_field_id=" refund-status-field ",
        lark_refund_app_id=" test-lark-app-id ",
        lark_refund_app_secret=" test-lark-app-secret ",
    )

    assert settings.lark_refund_base_url == "https://feishu.example.test/base/refund-base"
    assert settings.lark_refund_api_base_url == "https://open.feishu.example.test"
    assert settings.lark_refund_base_token == "refund-base"
    assert settings.lark_refund_table_id == "refund-table"
    assert settings.lark_refund_company_code_field_id == "company-code-field"
    assert settings.lark_refund_status_field_id == "refund-status-field"
    assert settings.lark_refund_app_id is not None
    assert settings.lark_refund_app_secret is not None
    assert settings.lark_refund_app_id.get_secret_value() == "test-lark-app-id"
    assert settings.lark_refund_app_secret.get_secret_value() == "test-lark-app-secret"
    assert "test-lark-app-id" not in repr(settings)
    assert "test-lark-app-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("lark_refund_base_url", ""),
        ("lark_refund_api_base_url", " "),
        ("lark_refund_base_token", " "),
        ("lark_refund_table_id", " "),
        ("lark_refund_company_code_field_id", " "),
        ("lark_refund_status_field_id", " "),
    ),
)
def test_enabled_lark_refund_writeback_rejects_missing_required_settings(
    setting_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=setting_name):
        _enabled_lark_refund_settings(**{setting_name: value})


def test_enabled_lark_refund_dispatch_can_run_without_worker_credentials() -> None:
    settings = _enabled_lark_refund_settings(
        lark_refund_app_id=None,
        lark_refund_app_secret=None,
    )

    assert settings.lark_refund_writeback_enabled is True
    assert settings.lark_refund_app_id is None
    assert settings.lark_refund_app_secret is None


@pytest.mark.parametrize(
    ("app_id", "app_secret"),
    (("only-app-id", ""), ("", "only-app-secret")),
)
def test_lark_refund_credentials_are_atomic_when_disabled(
    app_id: str,
    app_secret: str,
) -> None:
    values: dict[str, object] = {
        "lark_refund_app_id": app_id,
        "lark_refund_app_secret": app_secret,
    }
    with pytest.raises(ValueError, match="lark_refund_app_id"):
        Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("app_id", "app_secret"),
    (
        ("only-app-id", None),
        (None, "only-app-secret"),
        ("   ", "only-app-secret"),
        ("only-app-id", "   "),
    ),
)
def test_lark_refund_credentials_remain_atomic_for_none_and_whitespace(
    app_id: str | None,
    app_secret: str | None,
) -> None:
    values: dict[str, object] = {
        "lark_refund_app_id": app_id,
        "lark_refund_app_secret": app_secret,
    }
    with pytest.raises(ValueError, match="lark_refund_app_id"):
        Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    (
        "http://feishu.example.test/base/refund-base",
        "not-a-url",
        "https://user:secret@feishu.example.test/base/refund-base",
        "https://feishu.example.test/base/refund-base#fragment",
    ),
)
def test_enabled_lark_refund_writeback_requires_safe_https_url(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        _enabled_lark_refund_settings(lark_refund_base_url=url)


def test_enabled_lark_refund_writeback_requires_matching_base_token() -> None:
    with pytest.raises(ValueError, match="must identify lark_refund_base_token"):
        _enabled_lark_refund_settings(lark_refund_base_token="another-base")


def test_enabled_lark_refund_writeback_rejects_base_url_query() -> None:
    with pytest.raises(ValueError, match="lark_refund_base_url"):
        _enabled_lark_refund_settings(
            lark_refund_base_url=(
                "https://feishu.example.test/base/refund-base?tenant=do-not-accept"
            )
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://open.feishu.example.test",
        "https://open.feishu.example.test/open-apis",
        "https://open.feishu.example.test?tenant=secret",
    ),
)
def test_enabled_lark_refund_writeback_requires_safe_api_origin(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS (URL|origin)"):
        _enabled_lark_refund_settings(lark_refund_api_base_url=url)


def test_production_lark_refund_writeback_requires_the_official_api_origin() -> None:
    with pytest.raises(ValueError, match="must be https://open.feishu.cn"):
        _enabled_lark_refund_settings(
            environment="production",
            export_download_secret="production-export-secret-at-least-32-chars",
            worker_scope_secret="production-worker-secret-at-least-32-chars",
            lark_refund_api_base_url="https://lark-proxy.example.test",
        )


def test_production_lark_refund_writeback_accepts_the_canonical_official_origin() -> None:
    settings = _enabled_lark_refund_settings(
        environment="production",
        export_download_secret="production-export-secret-at-least-32-chars",
        worker_scope_secret="production-worker-secret-at-least-32-chars",
        lark_refund_api_base_url="https://OPEN.FEISHU.CN/",
    )

    assert settings.lark_refund_api_base_url == "https://OPEN.FEISHU.CN/"


def test_test_environment_can_inject_an_https_mock_lark_origin() -> None:
    settings = _enabled_lark_refund_settings(
        environment="test",
        lark_refund_api_base_url="https://open.feishu.mock.test",
    )

    assert settings.lark_refund_api_base_url == "https://open.feishu.mock.test"


@pytest.mark.parametrize(
    "setting_name",
    (
        "lark_refund_table_id",
        "lark_refund_company_code_field_id",
        "lark_refund_status_field_id",
    ),
)
def test_enabled_lark_refund_writeback_rejects_path_like_identifiers(
    setting_name: str,
) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        _enabled_lark_refund_settings(**{setting_name: "unsafe/id"})


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("lark_refund_timeout_seconds", 0.99),
        ("lark_refund_timeout_seconds", 301),
        ("lark_refund_page_size", 0),
        ("lark_refund_page_size", 201),
        ("lark_refund_max_retries", -1),
        ("lark_refund_max_retries", 11),
    ),
)
def test_lark_refund_numeric_settings_enforce_bounds(
    setting_name: str,
    value: float | int,
) -> None:
    with pytest.raises(ValueError):
        Settings(**{setting_name: value})  # type: ignore[arg-type]


def test_dgc_sap_profit_can_be_enabled_with_https_endpoints_and_credentials() -> None:
    settings = _enabled_dgc_settings()

    assert settings.dgc_sap_profit_enabled is True
    assert settings.dgc_app_key is not None
    assert settings.dgc_app_secret is not None
    assert settings.dgc_app_key.get_secret_value() == "test-app-key"
    assert settings.dgc_app_secret.get_secret_value() == "test-app-secret"
    assert "test-app-key" not in repr(settings)
    assert "test-app-secret" not in repr(settings)


def test_dgc_sap_profit_retains_explicit_iam_compatibility() -> None:
    settings = _enabled_dgc_settings(
        dgc_app_key=None,
        dgc_app_secret=None,
        dgc_iam_username="service-user",
        dgc_iam_password="iam-secret",
    )

    assert settings.dgc_iam_username == "service-user"
    assert settings.dgc_iam_password is not None
    assert settings.dgc_iam_password.get_secret_value() == "iam-secret"
    assert "iam-secret" not in repr(settings)


def test_dgc_sap_profit_rejects_ambiguous_authentication_methods() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _enabled_dgc_settings(
            dgc_iam_username="service-user",
            dgc_iam_password="iam-secret",
        )


@pytest.mark.parametrize(
    ("prefix", "api_url", "credential_label"),
    INDEPENDENT_DGC_INTERFACES,
)
def test_dgc_app_interfaces_can_be_enabled_with_independent_credentials(
    prefix: str,
    api_url: str,
    credential_label: str,
) -> None:
    settings = _enabled_independent_dgc_interface_settings(
        prefix,
        api_url,
        credential_label,
    )

    app_key = getattr(settings, f"{prefix}_app_key")
    app_secret = getattr(settings, f"{prefix}_app_secret")
    assert getattr(settings, f"{prefix}_enabled") is True
    assert app_key is not None
    assert app_secret is not None
    assert app_key.get_secret_value() == f"{credential_label}-app-key"
    assert app_secret.get_secret_value() == f"{credential_label}-app-secret"
    assert f"{credential_label}-app-key" not in repr(settings)
    assert f"{credential_label}-app-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("prefix", "api_url", "credential_label"),
    INDEPENDENT_DGC_INTERFACES,
)
@pytest.mark.parametrize(
    ("suffix", "value"),
    (
        ("api_url", ""),
        ("app_key", ""),
        ("app_secret", "  "),
    ),
)
def test_enabled_dgc_app_interfaces_reject_missing_required_settings(
    prefix: str,
    api_url: str,
    credential_label: str,
    suffix: str,
    value: str,
) -> None:
    setting_name = f"{prefix}_{suffix}"
    with pytest.raises(ValueError, match=setting_name):
        _enabled_independent_dgc_interface_settings(
            prefix,
            api_url,
            credential_label,
            **{setting_name: value},
        )


@pytest.mark.parametrize(
    ("prefix", "_api_url", "_credential_label"),
    INDEPENDENT_DGC_INTERFACES,
)
@pytest.mark.parametrize(
    ("app_key", "app_secret"),
    (("only-key", ""), ("", "only-secret")),
)
def test_dgc_app_interface_credentials_are_atomic_when_disabled(
    prefix: str,
    _api_url: str,
    _credential_label: str,
    app_key: str,
    app_secret: str,
) -> None:
    values: dict[str, object] = {
        f"{prefix}_app_key": app_key,
        f"{prefix}_app_secret": app_secret,
    }
    with pytest.raises(ValueError, match=f"{prefix}_app_key"):
        Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("dgc_sap_profit_api_url", ""),
        ("dgc_app_key", "  "),
        ("dgc_app_secret", ""),
    ),
)
def test_enabled_dgc_sap_profit_rejects_missing_required_settings(
    setting_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=setting_name):
        _enabled_dgc_settings(**{setting_name: value})


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("dgc_iam_url", "http://iam.example.test/v3/auth/tokens"),
        ("dgc_iam_url", "not-a-url"),
        ("dgc_sap_profit_api_url", "http://dgc.example.test/sap-profit"),
        ("dgc_sap_profit_api_url", "https://user:secret@dgc.example.test/sap-profit"),
        ("dgc_sap_profit_api_url", "https://dgc.example.test/sap-profit#fragment"),
        ("dgc_sap_trial_balance_api_url", "http://dgc.example.test/trial-balance"),
        ("dgc_sap_account_balance_api_url", "http://dgc.example.test/account-balance"),
        ("dgc_hesi_reimbursement_api_url", "http://dgc.example.test/hesi-reimbursement"),
        ("dgc_hesi_invoice_api_url", "http://dgc.example.test/hesi-invoice"),
        ("dgc_hesi_application_api_url", "http://dgc.example.test/hesi-application"),
        ("dgc_sap_dividend_detail_api_url", "http://dgc.example.test/dividend-detail"),
        ("dgc_invoice_detail_api_url", "http://dgc.example.test/invoice-detail"),
    ),
)
def test_dgc_endpoints_must_use_https(setting_name: str, value: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        Settings(**{setting_name: value})  # type: ignore[arg-type]


def test_dgc_field_map_accepts_unique_custom_source_fields() -> None:
    field_map = {name: f"sap_{name}" for name in DGC_FIELD_NAMES}

    settings = Settings(dgc_sap_profit_field_map=field_map)

    assert settings.dgc_sap_profit_field_map == field_map


@pytest.mark.parametrize(
    ("field_map", "message"),
    (
        (
            {name: name for name in DGC_FIELD_NAMES if name != "fiscal_period"},
            "exactly the supported logical fields",
        ),
        (
            {**{name: name for name in DGC_FIELD_NAMES}, "unexpected": "extra"},
            "exactly the supported logical fields",
        ),
        (
            {**{name: name for name in DGC_FIELD_NAMES}, "fiscal_period": " "},
            "must be nonempty",
        ),
        (
            {
                **{name: name for name in DGC_FIELD_NAMES},
                "fiscal_period": "company_code",
            },
            "must be unique",
        ),
    ),
)
def test_dgc_field_map_rejects_invalid_contracts(
    field_map: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(dgc_sap_profit_field_map=field_map)


@pytest.mark.parametrize(
    ("setting_name", "field_map"),
    (
        (
            "dgc_hesi_reimbursement_field_map",
            {"company_code": "company_code"},
        ),
        (
            "dgc_hesi_invoice_field_map",
            {
                "company_code": "same",
                "expense_claim_code": "same",
                "invoice_id": "invoice_id",
                "expense_type_id": "feetypeid",
                "expense_line_amount": "amount_standard_dec",
                "invoice_approved_amount": "invoice_approved_amount",
            },
        ),
    ),
)
def test_dgc_hesi_field_maps_reject_incomplete_or_duplicate_contracts(
    setting_name: str,
    field_map: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match=setting_name):
        Settings(**{setting_name: field_map})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("metric_map", "message"),
    (
        (
            {name: (name,) for name in DGC_METRIC_NAMES if name != "cumulative_revenue"},
            "exactly the supported metrics",
        ),
        (
            {**{name: (name,) for name in DGC_METRIC_NAMES}, "cumulative_revenue": ()},
            "must be nonempty",
        ),
        (
            {
                "cumulative_profit": ("same",),
                "fair_value_change": ("same",),
                "cumulative_revenue": ("revenue",),
            },
            "must be unique",
        ),
    ),
)
def test_dgc_metric_map_rejects_invalid_contracts(
    metric_map: dict[str, tuple[str, ...]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(dgc_sap_profit_metric_map=metric_map)


def test_dgc_ledger_must_be_nonempty() -> None:
    with pytest.raises(ValueError, match="dgc_sap_profit_ledger"):
        Settings(dgc_sap_profit_ledger=" ")


@pytest.mark.parametrize(
    ("setting_name", "value"),
    (
        ("dgc_timeout_seconds", 0),
        ("dgc_timeout_seconds", 301),
        ("dgc_page_size", 0),
        ("dgc_page_size", 50_001),
        ("dgc_max_pages", 0),
        ("dgc_max_pages", 10_001),
        ("dgc_max_records", 0),
        ("dgc_max_records", 1_000_001),
        ("dgc_max_page_bytes", 0),
        ("dgc_max_page_bytes", 64 * 1024 * 1024 + 1),
        ("dgc_max_total_bytes", 0),
        ("dgc_max_total_bytes", 1024 * 1024 * 1024 + 1),
        ("dgc_token_ttl_seconds", 0),
        ("dgc_token_ttl_seconds", 86_401),
        ("dgc_sap_trial_balance_page_size", 0),
        ("dgc_sap_trial_balance_page_size", 50_001),
        ("dgc_sap_account_balance_page_size", 0),
        ("dgc_sap_account_balance_page_size", 50_001),
        ("dgc_hesi_reimbursement_page_size", 0),
        ("dgc_hesi_reimbursement_page_size", 26),
        ("dgc_hesi_invoice_page_size", 0),
        ("dgc_hesi_invoice_page_size", 26),
        ("dgc_hesi_application_page_size", 0),
        ("dgc_hesi_application_page_size", 50_001),
        ("dgc_sap_dividend_detail_page_size", 0),
        ("dgc_sap_dividend_detail_page_size", 50_001),
        ("dgc_invoice_detail_page_size", 0),
        ("dgc_invoice_detail_page_size", 50_001),
    ),
)
def test_dgc_numeric_settings_enforce_bounds(setting_name: str, value: int) -> None:
    with pytest.raises(ValueError):
        Settings(**{setting_name: value})  # type: ignore[arg-type]


def test_dgc_total_byte_limit_cannot_be_smaller_than_page_limit() -> None:
    with pytest.raises(ValueError, match="total byte limit"):
        Settings(
            dgc_max_page_bytes=1024,
            dgc_max_total_bytes=1023,
        )


@pytest.mark.parametrize(
    ("server_name", "fingerprint"),
    [
        ("", "AA" * 32),
        ("dgc.example.test", ""),
        ("not-a-host", "AA" * 32),
        ("dgc.example.test", "not-a-fingerprint"),
    ],
)
def test_dgc_pinned_tls_identity_is_atomic_and_valid(
    server_name: str,
    fingerprint: str,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            dgc_tls_server_name=server_name,
            dgc_tls_pinned_certificate_sha256=fingerprint,
        )


def test_dgc_pinned_tls_identity_is_normalized() -> None:
    settings = Settings(
        dgc_tls_server_name=" DGC.Example.Test ",
        dgc_tls_pinned_certificate_sha256=":".join(["ab"] * 32),
    )

    assert settings.dgc_tls_server_name == "dgc.example.test"
    assert settings.dgc_tls_pinned_certificate_sha256 == "AB" * 32


def test_dgc_record_limit_cannot_be_smaller_than_page_size() -> None:
    with pytest.raises(ValueError, match="record limit"):
        Settings(dgc_page_size=15_000, dgc_max_records=14_999)
