from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DGC_SAP_PROFIT_FIELD_NAMES = (
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
_DGC_SAP_PROFIT_METRIC_NAMES = (
    "cumulative_profit",
    "fair_value_change",
    "cumulative_revenue",
)
_DGC_HESI_REIMBURSEMENT_FIELD_NAMES = (
    "company_code",
    "approval_completed_at",
    "expense_claim_code",
    "expense_type_code",
    "expense_type_amount",
)
_DGC_HESI_INVOICE_FIELD_NAMES = (
    "company_code",
    "expense_claim_code",
    "invoice_id",
    "expense_type_id",
    "expense_line_amount",
    "invoice_approved_amount",
)


def _default_dgc_sap_profit_field_map() -> dict[str, str]:
    return {
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


def _default_dgc_sap_profit_metric_map() -> dict[str, tuple[str, ...]]:
    return {
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


def _default_dgc_hesi_reimbursement_field_map() -> dict[str, str]:
    return {
        "company_code": "company_code",
        "approval_completed_at": "flow_end_date",
        "expense_claim_code": "expense_code",
        "expense_type_code": "fee_type_code",
        "expense_type_amount": "fee_type_amount",
    }


def _default_dgc_hesi_invoice_field_map() -> dict[str, str]:
    return {
        "company_code": "company_code",
        "expense_claim_code": "code",
        "invoice_id": "invoice_id",
        "expense_type_id": "feetypeid",
        "expense_line_amount": "amount_standard_dec",
        "invoice_approved_amount": "approve_amount_dec",
    }


def _is_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _normalize_dgc_app_interface(
    *,
    prefix: str,
    enabled: bool,
    api_url: str | None,
    app_key: SecretStr | None,
    app_secret: SecretStr | None,
) -> tuple[str | None, SecretStr | None, SecretStr | None]:
    normalized_url = api_url.strip() or None if api_url is not None else None
    url_name = f"{prefix}_api_url"
    if normalized_url is not None and not _is_https_url(normalized_url):
        raise ValueError(f"{url_name} must be an HTTPS URL")

    normalized_key = app_key.get_secret_value().strip() if app_key is not None else ""
    normalized_secret = app_secret.get_secret_value().strip() if app_secret is not None else ""
    key_name = f"{prefix}_app_key"
    secret_name = f"{prefix}_app_secret"
    if (normalized_key or normalized_secret) and (not normalized_key or not normalized_secret):
        raise ValueError(f"{key_name} and {secret_name} must both be nonempty")

    if enabled:
        required_values = {
            url_name: normalized_url or "",
            key_name: normalized_key,
            secret_name: normalized_secret,
        }
        missing = sorted(name for name, value in required_values.items() if not value)
        if missing:
            raise ValueError(
                f"enabled {prefix} interface requires nonempty settings: " + ", ".join(missing)
            )

    return (
        normalized_url,
        SecretStr(normalized_key) if normalized_key else None,
        SecretStr(normalized_secret) if normalized_secret else None,
    )


class Settings(BaseSettings):
    """Environment-backed application settings."""

    database_url: str = "postgresql+psycopg://tax_risk:tax_risk@localhost:5432/tax_risk"
    redis_url: str = "redis://localhost:6379/0"
    external_fetch_enabled: bool = False
    external_fetch_cache_enabled: bool = False
    external_fetch_max_workers: int = Field(default=12, gt=0, le=64)
    external_fetch_source_concurrency: dict[str, int] = Field(
        default_factory=lambda: {
            "dgc_sap_profit": 4,
            "dgc_sap_trial_balance": 4,
            "dgc_sap_account_balance": 4,
            "dgc_hesi_reimbursement": 4,
            "dgc_hesi_invoice": 4,
            "dgc_hesi_application": 4,
            "dgc_sap_dividend_detail": 4,
            "dgc_invoice_detail": 4,
        }
    )
    external_fetch_cache_prefix: str = "tax-risk:external-fetch"
    external_fetch_cache_ttl_seconds: int = Field(default=900, gt=0, le=86_400)
    external_fetch_empty_cache_ttl_seconds: int = Field(default=60, gt=0, le=3_600)
    external_fetch_lock_ttl_seconds: int = Field(default=300, gt=0, le=3_600)
    external_fetch_lock_wait_seconds: float = Field(default=305, gt=0, le=3_900)
    external_fetch_lock_poll_seconds: float = Field(default=0.1, gt=0, le=5)
    external_fetch_retry_max_attempts: int = Field(default=3, gt=0, le=10)
    external_fetch_retry_base_delay_seconds: float = Field(default=0.25, gt=0, le=60)
    external_fetch_retry_max_delay_seconds: float = Field(default=5, gt=0, le=300)
    external_fetch_retry_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    environment: Literal["development", "test", "production"] = "development"
    development_principal_enabled: bool = False
    development_principal_secret: str | None = None
    auth_session_secret: SecretStr | None = None
    auth_session_cookie_name: str = "tax_risk_session"
    auth_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    auth_oauth_state_ttl_seconds: int = Field(default=10 * 60, ge=60, le=30 * 60)
    auth_login_max_failures: int = Field(default=5, ge=1, le=20)
    auth_login_window_seconds: int = Field(default=5 * 60, ge=60, le=60 * 60)
    auth_local_accounts: dict[str, dict[str, object]] = Field(default_factory=dict)
    auth_feishu_enabled: bool = False
    auth_feishu_client_id: str | None = None
    auth_feishu_client_secret: SecretStr | None = None
    auth_feishu_redirect_uri: str | None = None
    auth_feishu_tenant_key: str | None = None
    auth_feishu_principals: dict[str, dict[str, object]] = Field(default_factory=dict)
    auth_feishu_timeout_seconds: float = Field(default=15, ge=1, le=60)
    ingest_max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    ingest_max_concurrent_uploads: int = Field(default=4, gt=0)
    tax_master_xlsx_max_zip_members: int = Field(default=128, gt=0)
    tax_master_xlsx_max_total_uncompressed_bytes: int = Field(
        default=64 * 1024 * 1024,
        gt=0,
    )
    tax_master_xlsx_max_member_uncompressed_bytes: int = Field(
        default=32 * 1024 * 1024,
        gt=0,
    )
    tax_master_xlsx_max_compression_ratio: int = Field(default=200, gt=0)
    tax_master_xlsx_max_worksheet_rows: int = Field(default=20_000, gt=0)
    tax_master_xlsx_max_worksheet_cells: int = Field(default=200_000, gt=0)
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = False
    celery_task_store_eager_result: bool = False
    celery_visibility_timeout_seconds: int = Field(default=3_600, gt=0)
    celery_result_expires_seconds: int = Field(default=86_400, gt=0)
    quarterly_worker_concurrency: int = Field(default=4, gt=0)
    quarterly_task_soft_time_limit_seconds: int = Field(default=300, gt=0)
    quarterly_task_time_limit_seconds: int = Field(default=330, gt=0)
    quarterly_task_max_retries: int = Field(default=3, ge=0)
    quarterly_task_retry_backoff_seconds: int = Field(default=5, gt=0)
    semantic_model_provider: Literal["enterprise", "fake"] = "enterprise"
    semantic_model_endpoint: str | None = None
    semantic_model_deployment: str | None = None
    semantic_model_timeout_seconds: float = Field(default=30, gt=0, le=300)
    semantic_model_credential_ref: str | None = None
    semantic_model_zero_retention_required: bool = True
    semantic_model_no_public_training: bool = True
    semantic_model_retention_mode: Literal["zero", "approved"] = "zero"
    dgc_sap_profit_enabled: bool = False
    dgc_iam_url: str = "https://iam.cn-east-3.myhuaweicloud.com/v3/auth/tokens"
    dgc_sap_profit_api_url: str | None = "https://116.63.221.181/post/sapincome"
    dgc_iam_username: str | None = None
    dgc_iam_password: SecretStr | None = None
    dgc_iam_domain: str = "hljtzb"
    dgc_iam_project: str = "cn-east-3"
    dgc_app_key: SecretStr | None = None
    dgc_app_secret: SecretStr | None = None
    dgc_timeout_seconds: float = Field(default=30, gt=0, le=300)
    dgc_page_size: int = Field(default=15_000, gt=0, le=50_000)
    dgc_max_pages: int = Field(default=1_000, gt=0, le=10_000)
    dgc_max_records: int = Field(default=100_000, gt=0, le=1_000_000)
    dgc_max_page_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)
    dgc_max_total_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)
    dgc_token_ttl_seconds: int = Field(default=82_800, gt=0, le=86_400)
    dgc_tls_server_name: str | None = "dgc.huaweicloud.com"
    dgc_tls_pinned_certificate_sha256: str | None = (
        "AF3850E5ACC206D12082BDD32E94AD4675F3AD7AB0AE23A247053DE9ED2883BF"
    )
    dgc_sap_profit_field_map: dict[str, str] = Field(
        default_factory=_default_dgc_sap_profit_field_map
    )
    dgc_sap_profit_metric_map: dict[str, tuple[str, ...]] = Field(
        default_factory=_default_dgc_sap_profit_metric_map
    )
    dgc_sap_profit_ledger: str = "0L"
    dgc_sap_trial_balance_enabled: bool = False
    dgc_sap_trial_balance_api_url: str | None = "https://116.63.221.181/fin/trial_balance"
    dgc_sap_trial_balance_app_key: SecretStr | None = None
    dgc_sap_trial_balance_app_secret: SecretStr | None = None
    dgc_sap_trial_balance_page_size: int = Field(default=1_000, gt=0, le=50_000)
    dgc_sap_account_balance_enabled: bool = False
    dgc_sap_account_balance_api_url: str | None = "https://116.63.221.181/post/sapaccountbalance"
    dgc_sap_account_balance_app_key: SecretStr | None = None
    dgc_sap_account_balance_app_secret: SecretStr | None = None
    dgc_sap_account_balance_page_size: int = Field(default=15_000, gt=0, le=50_000)
    dgc_hesi_reimbursement_enabled: bool = False
    dgc_hesi_reimbursement_api_url: str | None = "https://116.63.221.181/post/hesimingxi"
    dgc_hesi_reimbursement_app_key: SecretStr | None = None
    dgc_hesi_reimbursement_app_secret: SecretStr | None = None
    dgc_hesi_reimbursement_page_size: int = Field(default=25, gt=0, le=25)
    dgc_hesi_reimbursement_field_map: dict[str, str] = Field(
        default_factory=_default_dgc_hesi_reimbursement_field_map
    )
    dgc_hesi_invoice_enabled: bool = False
    dgc_hesi_invoice_api_url: str | None = "https://116.63.221.181/post/hesiinvoice"
    dgc_hesi_invoice_app_key: SecretStr | None = None
    dgc_hesi_invoice_app_secret: SecretStr | None = None
    dgc_hesi_invoice_page_size: int = Field(default=25, gt=0, le=25)
    dgc_hesi_invoice_field_map: dict[str, str] = Field(
        default_factory=_default_dgc_hesi_invoice_field_map
    )
    dgc_hesi_application_enabled: bool = False
    dgc_hesi_application_api_url: str | None = "https://116.63.221.181/post/apply"
    dgc_hesi_application_app_key: SecretStr | None = None
    dgc_hesi_application_app_secret: SecretStr | None = None
    dgc_hesi_application_page_size: int = Field(default=5_000, gt=0, le=50_000)
    dgc_sap_dividend_detail_enabled: bool = False
    dgc_sap_dividend_detail_api_url: str | None = (
        "https://116.63.221.181/post/settlement_adjustment"
    )
    dgc_sap_dividend_detail_app_key: SecretStr | None = None
    dgc_sap_dividend_detail_app_secret: SecretStr | None = None
    dgc_sap_dividend_detail_page_size: int = Field(default=15_000, gt=0, le=50_000)
    dgc_invoice_detail_enabled: bool = False
    dgc_invoice_detail_api_url: str | None = "https://116.63.221.181/post/writeoff"
    dgc_invoice_detail_app_key: SecretStr | None = None
    dgc_invoice_detail_app_secret: SecretStr | None = None
    dgc_invoice_detail_page_size: int = Field(default=15_000, gt=0, le=50_000)
    lark_refund_writeback_enabled: bool = False
    lark_refund_base_url: str | None = "https://hailiang.feishu.cn/base/A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
    lark_refund_api_base_url: str = "https://open.feishu.cn"
    lark_refund_base_token: str | None = "A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
    lark_refund_table_id: str | None = "tbl4PCNdcl4BYzgZ"
    lark_refund_company_code_field_id: str | None = "fld5uBjB9R"
    lark_refund_status_field_id: str | None = "fld4HLnqDk"
    lark_refund_app_id: SecretStr | None = None
    lark_refund_app_secret: SecretStr | None = None
    lark_refund_timeout_seconds: float = Field(default=30, ge=1, le=300)
    lark_refund_page_size: int = Field(default=100, ge=1, le=200)
    lark_refund_max_retries: int = Field(default=3, ge=0, le=10)
    export_storage_path: str = "./var/exports"
    export_retention_hours: int = Field(default=24, gt=0, le=24 * 30)
    export_download_ttl_seconds: int = Field(default=300, gt=0, le=3_600)
    export_download_secret: str = "development-export-download-secret"
    worker_scope_secret: str = "development-worker-scope-secret-change-me"
    expected_migration_head: str = "0023_refund_ambiguous_match_alert"

    @model_validator(mode="after")
    def validate_browser_authentication(self) -> Self:
        if not self.auth_session_cookie_name or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in self.auth_session_cookie_name
        ):
            raise ValueError("authentication cookie name contains unsupported characters")
        authentication_enabled = bool(
            self.auth_local_accounts or self.auth_feishu_enabled or self.auth_feishu_principals
        )
        session_secret = (
            self.auth_session_secret.get_secret_value()
            if self.auth_session_secret is not None
            else ""
        )
        if authentication_enabled and len(session_secret) < 32:
            raise ValueError("configured authentication requires a 32-character session secret")
        if self.auth_feishu_enabled:
            required_text = {
                "auth_feishu_client_id": self.auth_feishu_client_id,
                "auth_feishu_redirect_uri": self.auth_feishu_redirect_uri,
                "auth_feishu_tenant_key": self.auth_feishu_tenant_key,
            }
            missing = [name for name, value in required_text.items() if not value or not value.strip()]
            if (
                self.auth_feishu_client_secret is None
                or not self.auth_feishu_client_secret.get_secret_value().strip()
            ):
                missing.append("auth_feishu_client_secret")
            if not self.auth_feishu_principals:
                missing.append("auth_feishu_principals")
            if missing:
                raise ValueError(
                    "Feishu authentication requires configured values: " + ", ".join(missing)
                )
            assert self.auth_feishu_redirect_uri is not None
            redirect = urlsplit(self.auth_feishu_redirect_uri)
            if (
                redirect.scheme not in {"http", "https"}
                or not redirect.netloc
                or redirect.fragment
            ):
                raise ValueError("Feishu redirect URI must be an absolute HTTP(S) URL")
            if self.environment == "production" and redirect.scheme != "https":
                raise ValueError("production Feishu redirect URI must use HTTPS")
        return self

    @model_validator(mode="after")
    def validate_dgc_sap_profit(self) -> Self:
        if self.dgc_max_total_bytes < self.dgc_max_page_bytes:
            raise ValueError("DGC total byte limit must be at least the page byte limit")
        if self.dgc_max_records < self.dgc_page_size:
            raise ValueError("DGC record limit must be at least the page size")
        interface_page_sizes = {
            "dgc_sap_trial_balance_page_size": self.dgc_sap_trial_balance_page_size,
            "dgc_sap_account_balance_page_size": self.dgc_sap_account_balance_page_size,
            "dgc_hesi_reimbursement_page_size": self.dgc_hesi_reimbursement_page_size,
            "dgc_hesi_invoice_page_size": self.dgc_hesi_invoice_page_size,
            "dgc_hesi_application_page_size": self.dgc_hesi_application_page_size,
            "dgc_sap_dividend_detail_page_size": self.dgc_sap_dividend_detail_page_size,
            "dgc_invoice_detail_page_size": self.dgc_invoice_detail_page_size,
        }
        oversized_page_setting = next(
            (
                name
                for name, page_size in interface_page_sizes.items()
                if page_size > self.dgc_max_records
            ),
            None,
        )
        if oversized_page_setting is not None:
            raise ValueError(f"DGC record limit must be at least {oversized_page_setting}")

        if self.dgc_tls_server_name is not None:
            self.dgc_tls_server_name = self.dgc_tls_server_name.strip().lower() or None
        if self.dgc_tls_pinned_certificate_sha256 is not None:
            self.dgc_tls_pinned_certificate_sha256 = (
                self.dgc_tls_pinned_certificate_sha256.replace(":", "").strip().upper() or None
            )
        if bool(self.dgc_tls_server_name) != bool(self.dgc_tls_pinned_certificate_sha256):
            raise ValueError(
                "dgc_tls_server_name and dgc_tls_pinned_certificate_sha256 "
                "must both be configured or both be empty"
            )
        if self.dgc_tls_server_name is not None:
            parsed_tls_name = urlsplit(f"https://{self.dgc_tls_server_name}")
            if (
                parsed_tls_name.hostname != self.dgc_tls_server_name
                or parsed_tls_name.netloc != self.dgc_tls_server_name
                or "." not in self.dgc_tls_server_name
            ):
                raise ValueError("dgc_tls_server_name must be a valid DNS name")
        if self.dgc_tls_pinned_certificate_sha256 is not None and (
            len(self.dgc_tls_pinned_certificate_sha256) != 64
            or any(
                character not in "0123456789ABCDEF"
                for character in self.dgc_tls_pinned_certificate_sha256
            )
        ):
            raise ValueError("dgc_tls_pinned_certificate_sha256 must be a SHA-256 fingerprint")
        expected_fields = set(_DGC_SAP_PROFIT_FIELD_NAMES)
        actual_fields = set(self.dgc_sap_profit_field_map)
        if actual_fields != expected_fields:
            raise ValueError(
                "DGC SAP profit field map must contain exactly the supported logical fields"
            )

        normalized_field_map = {
            logical_name: source_name.strip()
            for logical_name, source_name in self.dgc_sap_profit_field_map.items()
        }
        if any(not source_name for source_name in normalized_field_map.values()):
            raise ValueError("DGC SAP profit source field names must be nonempty")
        if len(set(normalized_field_map.values())) != len(normalized_field_map):
            raise ValueError("DGC SAP profit source field names must be unique")
        self.dgc_sap_profit_field_map = normalized_field_map

        expected_metrics = set(_DGC_SAP_PROFIT_METRIC_NAMES)
        if set(self.dgc_sap_profit_metric_map) != expected_metrics:
            raise ValueError("DGC SAP profit metric map must contain exactly the supported metrics")
        normalized_metric_map: dict[str, tuple[str, ...]] = {}
        all_labels: list[str] = []
        for metric_code, labels in self.dgc_sap_profit_metric_map.items():
            normalized_labels = tuple(label.strip() for label in labels)
            if not normalized_labels or any(not label for label in normalized_labels):
                raise ValueError("DGC SAP profit metric labels must be nonempty")
            normalized_metric_map[metric_code] = normalized_labels
            all_labels.extend(normalized_labels)
        if len(set(all_labels)) != len(all_labels):
            raise ValueError("DGC SAP profit metric labels must be unique")
        self.dgc_sap_profit_metric_map = normalized_metric_map

        for setting_name, expected_names in (
            (
                "dgc_hesi_reimbursement_field_map",
                set(_DGC_HESI_REIMBURSEMENT_FIELD_NAMES),
            ),
            ("dgc_hesi_invoice_field_map", set(_DGC_HESI_INVOICE_FIELD_NAMES)),
        ):
            field_map = getattr(self, setting_name)
            if set(field_map) != expected_names:
                raise ValueError(
                    f"{setting_name} must contain exactly the supported logical fields"
                )
            normalized_hesi_field_map = {
                logical_name: source_name.strip()
                for logical_name, source_name in field_map.items()
            }
            if any(not source_name for source_name in normalized_hesi_field_map.values()):
                raise ValueError(f"{setting_name} source field names must be nonempty")
            if len(set(normalized_hesi_field_map.values())) != len(normalized_hesi_field_map):
                raise ValueError(f"{setting_name} source field names must be unique")
            setattr(self, setting_name, normalized_hesi_field_map)

        self.dgc_sap_profit_ledger = self.dgc_sap_profit_ledger.strip()
        if not self.dgc_sap_profit_ledger:
            raise ValueError("dgc_sap_profit_ledger must be nonempty")

        self.dgc_iam_url = self.dgc_iam_url.strip()
        if not _is_https_url(self.dgc_iam_url):
            raise ValueError("DGC IAM URL must be an HTTPS URL")

        if self.dgc_sap_profit_api_url is not None:
            self.dgc_sap_profit_api_url = self.dgc_sap_profit_api_url.strip() or None
        if self.dgc_sap_profit_api_url is not None and not _is_https_url(
            self.dgc_sap_profit_api_url
        ):
            raise ValueError("DGC SAP profit API URL must be an HTTPS URL")

        if self.dgc_iam_username is not None:
            self.dgc_iam_username = self.dgc_iam_username.strip() or None
        self.dgc_iam_domain = self.dgc_iam_domain.strip()
        self.dgc_iam_project = self.dgc_iam_project.strip()

        app_key = (
            self.dgc_app_key.get_secret_value().strip() if self.dgc_app_key is not None else ""
        )
        app_secret = (
            self.dgc_app_secret.get_secret_value().strip()
            if self.dgc_app_secret is not None
            else ""
        )
        app_supplied = bool(app_key or app_secret)
        if app_supplied:
            if not app_key or not app_secret:
                raise ValueError("dgc_app_key and dgc_app_secret must both be nonempty")
            self.dgc_app_key = SecretStr(app_key)
            self.dgc_app_secret = SecretStr(app_secret)
        else:
            self.dgc_app_key = None
            self.dgc_app_secret = None

        iam_password = (
            self.dgc_iam_password.get_secret_value().strip()
            if self.dgc_iam_password is not None
            else ""
        )
        iam_supplied = bool(self.dgc_iam_username or iam_password)
        if iam_supplied and (not self.dgc_iam_username or not iam_password):
            raise ValueError("dgc_iam_username and dgc_iam_password must both be nonempty")
        if iam_supplied:
            self.dgc_iam_password = SecretStr(iam_password)
        else:
            self.dgc_iam_password = None
        iam_configured = bool(iam_supplied)
        app_configured = bool(app_key and app_secret)
        if iam_configured and app_configured:
            raise ValueError("configure exactly one DGC authentication method")

        if self.dgc_sap_profit_enabled:
            required_values = {
                "dgc_sap_profit_api_url": self.dgc_sap_profit_api_url or "",
            }
            if not app_configured and not iam_configured:
                required_values["dgc_authentication"] = ""
            if iam_configured:
                required_values.update(
                    {
                        "dgc_iam_domain": self.dgc_iam_domain,
                        "dgc_iam_project": self.dgc_iam_project,
                    }
                )
            missing = sorted(name for name, value in required_values.items() if not value.strip())
            if missing:
                raise ValueError(
                    "enabled DGC SAP profit ingestion requires nonempty settings: "
                    + ", ".join(missing)
                )

        (
            self.dgc_sap_trial_balance_api_url,
            self.dgc_sap_trial_balance_app_key,
            self.dgc_sap_trial_balance_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_sap_trial_balance",
            enabled=self.dgc_sap_trial_balance_enabled,
            api_url=self.dgc_sap_trial_balance_api_url,
            app_key=self.dgc_sap_trial_balance_app_key,
            app_secret=self.dgc_sap_trial_balance_app_secret,
        )
        (
            self.dgc_sap_account_balance_api_url,
            self.dgc_sap_account_balance_app_key,
            self.dgc_sap_account_balance_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_sap_account_balance",
            enabled=self.dgc_sap_account_balance_enabled,
            api_url=self.dgc_sap_account_balance_api_url,
            app_key=self.dgc_sap_account_balance_app_key,
            app_secret=self.dgc_sap_account_balance_app_secret,
        )
        (
            self.dgc_hesi_reimbursement_api_url,
            self.dgc_hesi_reimbursement_app_key,
            self.dgc_hesi_reimbursement_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_hesi_reimbursement",
            enabled=self.dgc_hesi_reimbursement_enabled,
            api_url=self.dgc_hesi_reimbursement_api_url,
            app_key=self.dgc_hesi_reimbursement_app_key,
            app_secret=self.dgc_hesi_reimbursement_app_secret,
        )
        (
            self.dgc_hesi_invoice_api_url,
            self.dgc_hesi_invoice_app_key,
            self.dgc_hesi_invoice_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_hesi_invoice",
            enabled=self.dgc_hesi_invoice_enabled,
            api_url=self.dgc_hesi_invoice_api_url,
            app_key=self.dgc_hesi_invoice_app_key,
            app_secret=self.dgc_hesi_invoice_app_secret,
        )
        (
            self.dgc_hesi_application_api_url,
            self.dgc_hesi_application_app_key,
            self.dgc_hesi_application_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_hesi_application",
            enabled=self.dgc_hesi_application_enabled,
            api_url=self.dgc_hesi_application_api_url,
            app_key=self.dgc_hesi_application_app_key,
            app_secret=self.dgc_hesi_application_app_secret,
        )
        (
            self.dgc_sap_dividend_detail_api_url,
            self.dgc_sap_dividend_detail_app_key,
            self.dgc_sap_dividend_detail_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_sap_dividend_detail",
            enabled=self.dgc_sap_dividend_detail_enabled,
            api_url=self.dgc_sap_dividend_detail_api_url,
            app_key=self.dgc_sap_dividend_detail_app_key,
            app_secret=self.dgc_sap_dividend_detail_app_secret,
        )
        (
            self.dgc_invoice_detail_api_url,
            self.dgc_invoice_detail_app_key,
            self.dgc_invoice_detail_app_secret,
        ) = _normalize_dgc_app_interface(
            prefix="dgc_invoice_detail",
            enabled=self.dgc_invoice_detail_enabled,
            api_url=self.dgc_invoice_detail_api_url,
            app_key=self.dgc_invoice_detail_app_key,
            app_secret=self.dgc_invoice_detail_app_secret,
        )
        return self

    @model_validator(mode="after")
    def validate_lark_refund_writeback(self) -> Self:
        optional_text_settings = (
            "lark_refund_base_url",
            "lark_refund_base_token",
            "lark_refund_table_id",
            "lark_refund_company_code_field_id",
            "lark_refund_status_field_id",
        )
        for name in optional_text_settings:
            value = getattr(self, name)
            setattr(self, name, value.strip() or None if value is not None else None)
        self.lark_refund_api_base_url = self.lark_refund_api_base_url.strip()

        app_id = (
            self.lark_refund_app_id.get_secret_value().strip()
            if self.lark_refund_app_id is not None
            else ""
        )
        app_secret = (
            self.lark_refund_app_secret.get_secret_value().strip()
            if self.lark_refund_app_secret is not None
            else ""
        )
        if bool(app_id) != bool(app_secret):
            raise ValueError("lark_refund_app_id and lark_refund_app_secret must both be nonempty")
        self.lark_refund_app_id = SecretStr(app_id) if app_id else None
        self.lark_refund_app_secret = SecretStr(app_secret) if app_secret else None

        if self.lark_refund_writeback_enabled:
            required_values = {
                "lark_refund_base_url": self.lark_refund_base_url or "",
                "lark_refund_api_base_url": self.lark_refund_api_base_url,
                "lark_refund_base_token": self.lark_refund_base_token or "",
                "lark_refund_table_id": self.lark_refund_table_id or "",
                "lark_refund_company_code_field_id": (self.lark_refund_company_code_field_id or ""),
                "lark_refund_status_field_id": self.lark_refund_status_field_id or "",
            }
            missing = sorted(name for name, value in required_values.items() if not value)
            if missing:
                raise ValueError(
                    "enabled Lark refund writeback requires nonempty settings: "
                    + ", ".join(missing)
                )
            assert self.lark_refund_base_url is not None
            if not _is_https_url(self.lark_refund_base_url):
                raise ValueError("lark_refund_base_url must be an HTTPS URL")
            parsed_base_url = urlsplit(self.lark_refund_base_url)
            if parsed_base_url.query:
                raise ValueError("lark_refund_base_url must not contain a query")
            if not _is_https_url(self.lark_refund_api_base_url):
                raise ValueError("lark_refund_api_base_url must be an HTTPS URL")
            api_origin = urlsplit(self.lark_refund_api_base_url)
            if api_origin.path not in {"", "/"} or api_origin.query:
                raise ValueError("lark_refund_api_base_url must be an HTTPS origin")
            normalized_api_origin = self.lark_refund_api_base_url.rstrip("/").lower()
            if (
                self.environment == "production"
                and normalized_api_origin != "https://open.feishu.cn"
            ):
                raise ValueError(
                    "production lark_refund_api_base_url must be https://open.feishu.cn"
                )
            assert self.lark_refund_base_token is not None
            base_path = parsed_base_url.path.rstrip("/")
            if not base_path.endswith(f"/base/{self.lark_refund_base_token}"):
                raise ValueError("lark_refund_base_url must identify lark_refund_base_token")
            identifiers = {
                "lark_refund_base_token": self.lark_refund_base_token,
                "lark_refund_table_id": self.lark_refund_table_id,
                "lark_refund_company_code_field_id": (self.lark_refund_company_code_field_id),
                "lark_refund_status_field_id": self.lark_refund_status_field_id,
            }
            for name, value in identifiers.items():
                assert value is not None
                if any(character in value for character in "/?#"):
                    raise ValueError(f"{name} contains unsupported characters")
        return self

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> Self:
        if self.quarterly_task_time_limit_seconds <= (self.quarterly_task_soft_time_limit_seconds):
            raise ValueError("quarterly hard time limit must exceed its soft time limit")
        if self.celery_visibility_timeout_seconds <= self.quarterly_task_time_limit_seconds:
            raise ValueError("Celery visibility timeout must exceed the quarterly hard time limit")
        self.external_fetch_cache_prefix = self.external_fetch_cache_prefix.strip().rstrip(":")
        if not self.external_fetch_cache_prefix:
            raise ValueError("external fetch cache prefix must be nonempty")
        normalized_source_concurrency: dict[str, int] = {}
        for source_name, limit in self.external_fetch_source_concurrency.items():
            normalized_name = source_name.strip()
            if not normalized_name:
                raise ValueError("external fetch source names must be nonempty")
            if type(limit) is not int or not 1 <= limit <= self.external_fetch_max_workers:
                raise ValueError(
                    "external fetch source concurrency must be between 1 and max workers"
                )
            normalized_source_concurrency[normalized_name] = limit
        self.external_fetch_source_concurrency = normalized_source_concurrency
        if self.external_fetch_empty_cache_ttl_seconds > self.external_fetch_cache_ttl_seconds:
            raise ValueError("external fetch empty cache TTL must not exceed the normal TTL")
        if self.external_fetch_lock_wait_seconds < self.external_fetch_lock_ttl_seconds:
            raise ValueError("external fetch lock wait must be at least the lock TTL")
        if (
            self.external_fetch_retry_max_delay_seconds
            < self.external_fetch_retry_base_delay_seconds
        ):
            raise ValueError("external fetch retry maximum delay must be at least the base delay")
        if self.external_fetch_cache_enabled:
            parsed_redis_url = urlsplit(self.redis_url)
            if parsed_redis_url.scheme not in {"redis", "rediss"} or not parsed_redis_url.hostname:
                raise ValueError("external fetch cache requires a valid Redis URL")
        if (
            self.environment == "production"
            and self.external_fetch_enabled
            and not self.external_fetch_cache_enabled
        ):
            raise ValueError("production parallel external fetch requires Redis cache")
        if self.environment == "production" and (
            len(self.export_download_secret) < 32
            or len(self.worker_scope_secret) < 32
            or self.export_download_secret == "development-export-download-secret"
            or self.worker_scope_secret == "development-worker-scope-secret-change-me"
            or self.export_download_secret == self.worker_scope_secret
        ):
            raise ValueError(
                "production runtime secrets must be independent, nondefault, "
                "and at least 32 characters"
            )
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )
