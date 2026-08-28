import logging
import re
from calendar import monthrange
from collections.abc import Awaitable, Callable
from datetime import date
from functools import partial
from threading import BoundedSemaphore
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, Request, Response
from sqlalchemy import select

from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (
    DgcHesiInvoiceFieldMap,
    DgcHesiReimbursementFieldMap,
)
from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcClientConfig,
    DgcSapProfitClient,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
)
from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits
from tax_risk.api.business_entertainment_dependencies import (
    bind_structured_model_client,
)
from tax_risk.api.routes.audit import router as audit_router
from tax_risk.api.routes.auth import router as auth_router
from tax_risk.api.routes.business_entertainment import (
    router as business_entertainment_router,
)
from tax_risk.api.routes.cases import router as cases_router
from tax_risk.api.routes.dashboard import router as dashboard_router
from tax_risk.api.routes.exports import router as exports_router
from tax_risk.api.routes.health import DefaultReadinessProbe, ReadinessProbe
from tax_risk.api.routes.health import router as health_router
from tax_risk.api.routes.income_tax_refunds import router as income_tax_refunds_router
from tax_risk.api.routes.ingest import router as ingest_router
from tax_risk.api.routes.master_data import router as master_data_router
from tax_risk.api.routes.monthly_semantic import router as monthly_semantic_router
from tax_risk.api.routes.operations import router as operations_router
from tax_risk.api.routes.runs import router as runs_router
from tax_risk.api.routes.semantic_governance import router as semantic_governance_router
from tax_risk.api.routes.snapshots import router as snapshots_router
from tax_risk.application.audit import AuditEventDraft, AuditService, normalized_filter_hash
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.application.dgc_hesi_invoice import DgcHesiInvoiceSource
from tax_risk.application.dgc_hesi_reimbursement import DgcHesiReimbursementSource
from tax_risk.application.dgc_invoice_detail import DgcInvoiceDetailSource
from tax_risk.application.dgc_sap_account_balance import DgcSapAccountBalanceSource
from tax_risk.application.dgc_sap_dividend_detail import DgcSapDividendDetailSource
from tax_risk.application.dgc_sap_profit import DgcSapProfitSource
from tax_risk.application.dgc_sap_trial_balance import DgcSapTrialBalanceSource
from tax_risk.application.exports import (
    ExportObjectStore,
    ExportService,
    FileExportObjectStore,
)
from tax_risk.application.external_fetch import (
    CoordinatedDgcSource,
    DgcFetchSource,
    FetchCache,
)
from tax_risk.application.external_fetch_runtime import (
    MetricFetchObserver,
    build_external_fetch_coordinator,
)
from tax_risk.application.ingest import (
    AdapterFactory,
    UowFactory,
    create_csv_adapter,
)
from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.config import Settings
from tax_risk.observability.context import observability_context
from tax_risk.observability.metrics import DEFAULT_METRICS, MetricRegistry
from tax_risk.observability.tracing import configure_structured_logging, start_span
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import MonitoringRun, MonitoringRunCompany
from tax_risk.persistence.snapshot_models import SnapshotSetMember
from tax_risk.security.auth_configuration import (
    build_authentication_service,
    build_feishu_oauth_client,
)
from tax_risk.security.principal import PrincipalProvider
from tax_risk.security.service_scope import issue_service_scope_token

logger = logging.getLogger(__name__)


def create_app(
    *,
    uow_factory: UowFactory | None = None,
    adapter_factory: AdapterFactory | None = None,
    dgc_sap_profit_source: DgcSapProfitSource | None = None,
    dgc_sap_trial_balance_source: DgcSapTrialBalanceSource | None = None,
    dgc_sap_account_balance_source: DgcSapAccountBalanceSource | None = None,
    dgc_sap_dividend_detail_source: DgcSapDividendDetailSource | None = None,
    dgc_hesi_reimbursement_source: DgcHesiReimbursementSource | None = None,
    dgc_hesi_invoice_source: DgcHesiInvoiceSource | None = None,
    dgc_invoice_detail_source: DgcInvoiceDetailSource | None = None,
    settings: Settings | None = None,
    principal_provider: PrincipalProvider | None = None,
    quarterly_dispatcher: Callable[..., None] | None = None,
    monthly_semantic_dispatcher: Callable[..., None] | None = None,
    income_tax_refund_writeback_dispatcher: Callable[[], object] | None = None,
    semantic_credential_resolver: Callable[[str], str] | None = None,
    export_dispatcher: Callable[..., None] | None = None,
    export_object_store: ExportObjectStore | None = None,
    readiness_probe: ReadinessProbe | None = None,
    metrics_registry: MetricRegistry | None = None,
    external_fetch_cache: FetchCache | None = None,
) -> FastAPI:
    """Create the tax risk monitoring API."""

    resolved_settings = settings or Settings()
    if resolved_settings.environment == "production":
        configure_structured_logging()
    app = FastAPI(title="Group Income Tax Risk Monitoring Platform")
    app.state.uow_factory = uow_factory or UnitOfWork
    app.state.metrics_registry = metrics_registry or DEFAULT_METRICS
    app.state.audit_service = AuditService(app.state.uow_factory)
    resolved_export_store = export_object_store or FileExportObjectStore(
        resolved_settings.export_storage_path
    )
    app.state.export_service = ExportService(
        app.state.uow_factory,
        BusinessEntertainmentReportingService(app.state.uow_factory),
        resolved_export_store,
        app.state.audit_service,
        resolved_settings,
    )
    app.state.export_dispatcher = export_dispatcher or partial(
        _dispatch_export_job,
        worker_scope_secret=resolved_settings.worker_scope_secret,
    )
    app.state.readiness_probe = readiness_probe or DefaultReadinessProbe(
        settings=resolved_settings,
        uow_factory=app.state.uow_factory,
        object_store=resolved_export_store,
    )
    app.state.settings = resolved_settings
    app.state.authentication_service = build_authentication_service(resolved_settings)
    app.state.feishu_oauth_client = build_feishu_oauth_client(resolved_settings)
    if income_tax_refund_writeback_dispatcher is not None:
        app.state.income_tax_refund_writeback_dispatcher = income_tax_refund_writeback_dispatcher
    elif resolved_settings.lark_refund_writeback_enabled:
        app.state.income_tax_refund_writeback_dispatcher = partial(
            _dispatch_income_tax_refund_writebacks,
            uow_factory=app.state.uow_factory,
            worker_scope_secret=resolved_settings.worker_scope_secret,
            max_retries=resolved_settings.lark_refund_max_retries,
        )
    else:
        app.state.income_tax_refund_writeback_dispatcher = lambda: ()
    app.state.principal_provider = principal_provider
    app.state.adapter_factory = adapter_factory or create_csv_adapter
    app.state.dgc_sap_profit_field_map = DgcSapProfitFieldMap(
        **resolved_settings.dgc_sap_profit_field_map
    )
    app.state.dgc_sap_profit_metric_map = DgcSapProfitMetricMap(
        **resolved_settings.dgc_sap_profit_metric_map
    )
    app.state.dgc_sap_profit_ledger = resolved_settings.dgc_sap_profit_ledger
    app.state.dgc_hesi_reimbursement_field_map = DgcHesiReimbursementFieldMap(
        **resolved_settings.dgc_hesi_reimbursement_field_map
    )
    app.state.dgc_hesi_invoice_field_map = DgcHesiInvoiceFieldMap(
        **resolved_settings.dgc_hesi_invoice_field_map
    )
    app.state.dgc_sap_profit_client = dgc_sap_profit_source
    owned_dgc_sap_profit_client: DgcSapProfitClient | None = None
    if app.state.dgc_sap_profit_client is None and resolved_settings.dgc_sap_profit_enabled:
        assert resolved_settings.dgc_sap_profit_api_url is not None
        if resolved_settings.dgc_app_key is not None:
            assert resolved_settings.dgc_app_secret is not None
            client_config = DgcClientConfig(
                api_url=resolved_settings.dgc_sap_profit_api_url,
                app_key=resolved_settings.dgc_app_key.get_secret_value(),
                app_secret=resolved_settings.dgc_app_secret.get_secret_value(),
                timeout=resolved_settings.dgc_timeout_seconds,
                page_size=resolved_settings.dgc_page_size,
                max_pages=resolved_settings.dgc_max_pages,
                max_records=resolved_settings.dgc_max_records,
                max_page_bytes=resolved_settings.dgc_max_page_bytes,
                max_total_bytes=resolved_settings.dgc_max_total_bytes,
                token_ttl=resolved_settings.dgc_token_ttl_seconds,
                tls_server_name=resolved_settings.dgc_tls_server_name,
                tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
            )
        else:
            assert resolved_settings.dgc_iam_username is not None
            assert resolved_settings.dgc_iam_password is not None
            client_config = DgcClientConfig(
                iam_url=resolved_settings.dgc_iam_url,
                api_url=resolved_settings.dgc_sap_profit_api_url,
                username=resolved_settings.dgc_iam_username,
                password=resolved_settings.dgc_iam_password.get_secret_value(),
                domain=resolved_settings.dgc_iam_domain,
                project=resolved_settings.dgc_iam_project,
                timeout=resolved_settings.dgc_timeout_seconds,
                page_size=resolved_settings.dgc_page_size,
                max_pages=resolved_settings.dgc_max_pages,
                max_records=resolved_settings.dgc_max_records,
                max_page_bytes=resolved_settings.dgc_max_page_bytes,
                max_total_bytes=resolved_settings.dgc_max_total_bytes,
                token_ttl=resolved_settings.dgc_token_ttl_seconds,
            )
        owned_dgc_sap_profit_client = DgcSapProfitClient(client_config)
        app.state.dgc_sap_profit_client = owned_dgc_sap_profit_client
        app.router.add_event_handler("shutdown", owned_dgc_sap_profit_client.close)
    app.state.dgc_sap_trial_balance_client = dgc_sap_trial_balance_source
    owned_dgc_sap_trial_balance_client: DgcSapProfitClient | None = None
    if (
        app.state.dgc_sap_trial_balance_client is None
        and resolved_settings.dgc_sap_trial_balance_enabled
    ):
        assert resolved_settings.dgc_sap_trial_balance_api_url is not None
        assert resolved_settings.dgc_sap_trial_balance_app_key is not None
        assert resolved_settings.dgc_sap_trial_balance_app_secret is not None
        trial_balance_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_sap_trial_balance_api_url,
            app_key=resolved_settings.dgc_sap_trial_balance_app_key.get_secret_value(),
            app_secret=(resolved_settings.dgc_sap_trial_balance_app_secret.get_secret_value()),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_sap_trial_balance_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_sap_trial_balance_client = DgcSapProfitClient(trial_balance_client_config)
        app.state.dgc_sap_trial_balance_client = owned_dgc_sap_trial_balance_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_sap_trial_balance_client.close,
        )
    app.state.dgc_sap_account_balance_client = dgc_sap_account_balance_source
    owned_dgc_sap_account_balance_client: DgcSapProfitClient | None = None
    if (
        app.state.dgc_sap_account_balance_client is None
        and resolved_settings.dgc_sap_account_balance_enabled
    ):
        assert resolved_settings.dgc_sap_account_balance_api_url is not None
        assert resolved_settings.dgc_sap_account_balance_app_key is not None
        assert resolved_settings.dgc_sap_account_balance_app_secret is not None
        account_balance_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_sap_account_balance_api_url,
            app_key=(resolved_settings.dgc_sap_account_balance_app_key.get_secret_value()),
            app_secret=(resolved_settings.dgc_sap_account_balance_app_secret.get_secret_value()),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_sap_account_balance_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_sap_account_balance_client = DgcSapProfitClient(account_balance_client_config)
        app.state.dgc_sap_account_balance_client = owned_dgc_sap_account_balance_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_sap_account_balance_client.close,
        )
    app.state.dgc_sap_dividend_detail_client = dgc_sap_dividend_detail_source
    owned_dgc_sap_dividend_detail_client: DgcSapProfitClient | None = None
    if (
        app.state.dgc_sap_dividend_detail_client is None
        and resolved_settings.dgc_sap_dividend_detail_enabled
    ):
        assert resolved_settings.dgc_sap_dividend_detail_api_url is not None
        assert resolved_settings.dgc_sap_dividend_detail_app_key is not None
        assert resolved_settings.dgc_sap_dividend_detail_app_secret is not None
        dividend_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_sap_dividend_detail_api_url,
            app_key=(resolved_settings.dgc_sap_dividend_detail_app_key.get_secret_value()),
            app_secret=(resolved_settings.dgc_sap_dividend_detail_app_secret.get_secret_value()),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_sap_dividend_detail_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_sap_dividend_detail_client = DgcSapProfitClient(dividend_client_config)
        app.state.dgc_sap_dividend_detail_client = owned_dgc_sap_dividend_detail_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_sap_dividend_detail_client.close,
        )
    app.state.dgc_hesi_reimbursement_client = dgc_hesi_reimbursement_source
    owned_dgc_hesi_reimbursement_client: DgcSapProfitClient | None = None
    if (
        app.state.dgc_hesi_reimbursement_client is None
        and resolved_settings.dgc_hesi_reimbursement_enabled
    ):
        assert resolved_settings.dgc_hesi_reimbursement_api_url is not None
        assert resolved_settings.dgc_hesi_reimbursement_app_key is not None
        assert resolved_settings.dgc_hesi_reimbursement_app_secret is not None
        hesi_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_hesi_reimbursement_api_url,
            app_key=(resolved_settings.dgc_hesi_reimbursement_app_key.get_secret_value()),
            app_secret=(resolved_settings.dgc_hesi_reimbursement_app_secret.get_secret_value()),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_hesi_reimbursement_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            strict_offset_pagination=True,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_hesi_reimbursement_client = DgcSapProfitClient(hesi_client_config)
        app.state.dgc_hesi_reimbursement_client = owned_dgc_hesi_reimbursement_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_hesi_reimbursement_client.close,
        )
    app.state.dgc_hesi_invoice_client = dgc_hesi_invoice_source
    owned_dgc_hesi_invoice_client: DgcSapProfitClient | None = None
    if app.state.dgc_hesi_invoice_client is None and resolved_settings.dgc_hesi_invoice_enabled:
        assert resolved_settings.dgc_hesi_invoice_api_url is not None
        assert resolved_settings.dgc_hesi_invoice_app_key is not None
        assert resolved_settings.dgc_hesi_invoice_app_secret is not None
        hesi_invoice_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_hesi_invoice_api_url,
            request_method="POST",
            app_key=resolved_settings.dgc_hesi_invoice_app_key.get_secret_value(),
            app_secret=resolved_settings.dgc_hesi_invoice_app_secret.get_secret_value(),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_hesi_invoice_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            strict_offset_pagination=True,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_hesi_invoice_client = DgcSapProfitClient(hesi_invoice_client_config)
        app.state.dgc_hesi_invoice_client = owned_dgc_hesi_invoice_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_hesi_invoice_client.close,
        )
    app.state.dgc_invoice_detail_client = dgc_invoice_detail_source
    owned_dgc_invoice_detail_client: DgcSapProfitClient | None = None
    if app.state.dgc_invoice_detail_client is None and resolved_settings.dgc_invoice_detail_enabled:
        assert resolved_settings.dgc_invoice_detail_api_url is not None
        assert resolved_settings.dgc_invoice_detail_app_key is not None
        assert resolved_settings.dgc_invoice_detail_app_secret is not None
        invoice_client_config = DgcClientConfig(
            api_url=resolved_settings.dgc_invoice_detail_api_url,
            app_key=(resolved_settings.dgc_invoice_detail_app_key.get_secret_value()),
            app_secret=(resolved_settings.dgc_invoice_detail_app_secret.get_secret_value()),
            timeout=resolved_settings.dgc_timeout_seconds,
            page_size=resolved_settings.dgc_invoice_detail_page_size,
            max_pages=resolved_settings.dgc_max_pages,
            max_records=resolved_settings.dgc_max_records,
            max_page_bytes=resolved_settings.dgc_max_page_bytes,
            max_total_bytes=resolved_settings.dgc_max_total_bytes,
            token_ttl=resolved_settings.dgc_token_ttl_seconds,
            tls_server_name=resolved_settings.dgc_tls_server_name,
            tls_pinned_certificate_sha256=(resolved_settings.dgc_tls_pinned_certificate_sha256),
        )
        owned_dgc_invoice_detail_client = DgcSapProfitClient(invoice_client_config)
        app.state.dgc_invoice_detail_client = owned_dgc_invoice_detail_client
        app.router.add_event_handler(
            "shutdown",
            owned_dgc_invoice_detail_client.close,
        )
    app.state.external_fetch_coordinator = None
    if resolved_settings.external_fetch_enabled:
        fetch_sources: dict[str, DgcFetchSource] = {}
        if app.state.dgc_sap_profit_client is not None:
            fetch_sources["dgc_sap_profit"] = app.state.dgc_sap_profit_client
        if app.state.dgc_sap_trial_balance_client is not None:
            fetch_sources["dgc_sap_trial_balance"] = app.state.dgc_sap_trial_balance_client
        if app.state.dgc_sap_account_balance_client is not None:
            fetch_sources["dgc_sap_account_balance"] = app.state.dgc_sap_account_balance_client
        if app.state.dgc_sap_dividend_detail_client is not None:
            fetch_sources["dgc_sap_dividend_detail"] = app.state.dgc_sap_dividend_detail_client
        if app.state.dgc_hesi_reimbursement_client is not None:
            fetch_sources["dgc_hesi_reimbursement"] = app.state.dgc_hesi_reimbursement_client
        if app.state.dgc_hesi_invoice_client is not None:
            fetch_sources["dgc_hesi_invoice"] = app.state.dgc_hesi_invoice_client
        if app.state.dgc_invoice_detail_client is not None:
            fetch_sources["dgc_invoice_detail"] = app.state.dgc_invoice_detail_client
        if fetch_sources:
            coordinator = build_external_fetch_coordinator(
                resolved_settings,
                fetch_sources,
                cache=external_fetch_cache,
                observer=MetricFetchObserver(app.state.metrics_registry),
            )
            app.state.external_fetch_coordinator = coordinator
            if "dgc_sap_profit" in fetch_sources:
                app.state.dgc_sap_profit_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_sap_profit",
                )
            if "dgc_sap_trial_balance" in fetch_sources:
                app.state.dgc_sap_trial_balance_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_sap_trial_balance",
                )
            if "dgc_sap_account_balance" in fetch_sources:
                app.state.dgc_sap_account_balance_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_sap_account_balance",
                )
            if "dgc_sap_dividend_detail" in fetch_sources:
                app.state.dgc_sap_dividend_detail_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_sap_dividend_detail",
                )
            if "dgc_hesi_reimbursement" in fetch_sources:
                app.state.dgc_hesi_reimbursement_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_hesi_reimbursement",
                )
            if "dgc_hesi_invoice" in fetch_sources:
                app.state.dgc_hesi_invoice_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_hesi_invoice",
                )
            if "dgc_invoice_detail" in fetch_sources:
                app.state.dgc_invoice_detail_client = CoordinatedDgcSource(
                    coordinator,
                    "dgc_invoice_detail",
                )
            app.router.add_event_handler("shutdown", coordinator.close)
    app.state.quarterly_batch_service_factory = lambda: QuarterlyBatchService(app.state.uow_factory)
    app.state.quarterly_dispatcher = quarterly_dispatcher or partial(
        _dispatch_quarterly_batch,
        uow_factory=app.state.uow_factory,
        worker_scope_secret=resolved_settings.worker_scope_secret,
    )
    app.state.monthly_semantic_dispatcher = monthly_semantic_dispatcher or partial(
        _dispatch_monthly_semantic_batch,
        uow_factory=app.state.uow_factory,
        worker_scope_secret=resolved_settings.worker_scope_secret,
    )
    app.state.structured_model_client = bind_structured_model_client(
        resolved_settings,
        credential_resolver=semantic_credential_resolver or (lambda _reference: ""),
        uow_factory=app.state.uow_factory,
    )
    app.state.ingest_max_upload_bytes = resolved_settings.ingest_max_upload_bytes
    app.state.ingest_upload_semaphore = BoundedSemaphore(
        resolved_settings.ingest_max_concurrent_uploads
    )
    app.state.tax_master_xlsx_limits = XlsxResourceLimits(
        max_zip_members=resolved_settings.tax_master_xlsx_max_zip_members,
        max_total_uncompressed_bytes=(
            resolved_settings.tax_master_xlsx_max_total_uncompressed_bytes
        ),
        max_member_uncompressed_bytes=(
            resolved_settings.tax_master_xlsx_max_member_uncompressed_bytes
        ),
        max_compression_ratio=resolved_settings.tax_master_xlsx_max_compression_ratio,
        max_worksheet_rows=resolved_settings.tax_master_xlsx_max_worksheet_rows,
        max_worksheet_cells=resolved_settings.tax_master_xlsx_max_worksheet_cells,
    )

    @app.middleware("http")
    async def immutable_audit_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        with observability_context(request_id=request_id):
            with start_span(f"HTTP {request.method}", logger):
                try:
                    response = await call_next(request)
                except Exception:
                    _append_http_audit(request, 500, app.state.audit_service, request_id)
                    _record_http_metrics(request, 500, app.state.metrics_registry)
                    raise
                _append_http_audit(
                    request,
                    response.status_code,
                    app.state.audit_service,
                    request_id,
                )
                _record_http_metrics(
                    request,
                    response.status_code,
                    app.state.metrics_registry,
                )
                response.headers["X-Request-ID"] = request_id
                return response

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(ingest_router)
    app.include_router(income_tax_refunds_router)
    app.include_router(cases_router)
    app.include_router(dashboard_router)
    app.include_router(master_data_router)
    app.include_router(snapshots_router)
    app.include_router(runs_router)
    app.include_router(semantic_governance_router)
    app.include_router(exports_router)
    app.include_router(business_entertainment_router)
    app.include_router(monthly_semantic_router)
    app.include_router(operations_router)
    return app


def _record_http_metrics(
    request: Request,
    status_code: int,
    registry: MetricRegistry,
) -> None:
    route = request.scope.get("route")
    raw_path = getattr(route, "path", request.url.path)
    metric_path = re.sub(r"\{[^}]+\}", ":param", str(raw_path))
    registry.metric("tax_risk_http_request_total").inc(
        {
            "method": request.method,
            "path": metric_path,
            "status": str(status_code),
        }
    )
    if status_code in {401, 403}:
        action = f"HTTP_{request.method}_{metric_path}".replace("/", "_").replace(":", "_")[:128]
        registry.metric("tax_risk_authorization_failure_total").inc(
            {"action": action, "reason_code": f"HTTP_{status_code}"}
        )


def _append_http_audit(
    request: Request,
    status_code: int,
    service: AuditService,
    request_id: str,
) -> None:
    action = _http_audit_action(request.method, request.url.path)
    if action is None:
        return
    principal = getattr(request.state, "principal", None)
    company_ids = frozenset(
        getattr(
            request.state,
            "audit_company_ids",
            principal.allowed_company_ids if principal is not None else (),
        )
    )
    filters: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        filters.setdefault(key, []).append(value)
    target_id = _target_id(request.url.path, request.method)
    result = (
        "SUCCEEDED"
        if status_code < 400
        else "DENIED"
        if status_code in {401, 403, 404}
        else "FAILED"
    )
    try:
        service.append(
            AuditEventDraft(
                action=action,
                entity_type=_entity_type(request.url.path),
                entity_id=target_id,
                principal=principal,
                company_ids=company_ids,
                result=result,
                request_id=request_id,
                filters_hash=normalized_filter_hash(filters),
                row_count=getattr(request.state, "audit_row_count", None),
                reason_code=None if result == "SUCCEEDED" else f"HTTP_{status_code}",
                payload={"method": request.method, "path": request.url.path},
            )
        )
    except Exception:
        logger.exception("security_audit_append_failed", extra={"request_id": request_id})
        if status_code < 500:
            raise


def _http_audit_action(method: str, path: str) -> str | None:
    if not path.startswith("/api/v1/") or path.startswith("/api/v1/audit-events"):
        return None
    if path == "/api/v1/risk-cases" and method == "GET":
        return "HTTP_RISK_CASE_LIST"
    if path.startswith("/api/v1/risk-cases/"):
        return "HTTP_RISK_CASE_ACTION" if method != "GET" else "HTTP_RISK_CASE_DETAIL"
    normalized = path.removeprefix("/api/v1/").replace("/", "_").replace("-", "_")
    return f"HTTP_{method}_{normalized}".upper()[:128]


def _entity_type(path: str) -> str:
    segment = path.removeprefix("/api/v1/").split("/", 1)[0]
    return segment.replace("-", "_").upper()[:128]


def _target_id(path: str, method: str) -> UUID:
    for segment in reversed(path.rstrip("/").split("/")):
        try:
            return UUID(segment)
        except ValueError:
            continue
    return uuid5(NAMESPACE_URL, f"{method}:{path}")


def _dispatch_quarterly_batch(
    *,
    run_id: UUID,
    run_company_ids: tuple[UUID, ...],
    uow_factory: Callable[[], UnitOfWork],
    worker_scope_secret: str,
) -> None:
    """Send the durable ID-only quarterly canvas through the production broker."""

    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.quarterly_batch import build_quarterly_batch_canvas

    company_ids, summary_company_ids, period = _worker_dispatch_scope(
        uow_factory,
        run_id=run_id,
        run_company_ids=run_company_ids,
    )
    with observability_context(run_id=run_id):
        build_quarterly_batch_canvas(
            app=celery_app,
            run_id=run_id,
            run_company_ids=run_company_ids,
            company_ids=company_ids,
            summary_company_ids=summary_company_ids,
            scope_period=period,
            worker_scope_secret=worker_scope_secret,
        ).apply_async()


def _dispatch_monthly_semantic_batch(
    *,
    run_id: UUID,
    run_company_ids: tuple[UUID, ...],
    uow_factory: Callable[[], UnitOfWork],
    worker_scope_secret: str,
) -> None:
    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.monthly_semantic import build_monthly_semantic_canvas

    company_ids, summary_company_ids, period = _worker_dispatch_scope(
        uow_factory,
        run_id=run_id,
        run_company_ids=run_company_ids,
    )
    with observability_context(run_id=run_id):
        build_monthly_semantic_canvas(
            app=celery_app,
            run_id=run_id,
            run_company_ids=run_company_ids,
            company_ids=company_ids,
            summary_company_ids=summary_company_ids,
            scope_period=period,
            worker_scope_secret=worker_scope_secret,
        ).apply_async()


def _dispatch_export_job(
    *,
    job_id: UUID,
    company_ids: tuple[str, ...],
    authorization_version: str,
    worker_scope_secret: str,
) -> None:
    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.exports import RENDER_EXPORT_TASK

    scope_token = issue_service_scope_token(
        secret=worker_scope_secret,
        queue="exports",
        run_type="EXPORT",
        batch_id=str(job_id),
        company_ids=frozenset(UUID(value) for value in company_ids),
        period=date.today(),
    )
    celery_app.send_task(
        RENDER_EXPORT_TASK,
        args=(str(job_id), authorization_version, scope_token),
    )


def _dispatch_income_tax_refund_writebacks(
    *,
    uow_factory: Callable[[], UnitOfWork],
    worker_scope_secret: str,
    max_retries: int,
) -> tuple[str, ...]:
    """Publish bounded, signed outbox tasks after the scan transaction commits."""

    from tax_risk.application.refund_writebacks import (
        IncomeTaxRefundWritebackService,
    )
    from tax_risk.workers.celery_app import celery_app
    from tax_risk.workers.income_tax_refund_writebacks import (
        dispatch_refund_writebacks,
    )

    class DispatchOnlySender:
        def write_status(self, company_code: str, desired_value: str) -> object:
            del company_code, desired_value
            raise RuntimeError("dispatch-only service cannot deliver writebacks")

    service = IncomeTaxRefundWritebackService(
        uow_factory,
        DispatchOnlySender(),
        max_retries=max_retries,
    )
    items = service.list_dispatchable(limit=1_000)
    return dispatch_refund_writebacks(
        app=celery_app,
        items=items,
        worker_scope_secret=worker_scope_secret,
    )


def _worker_dispatch_scope(
    uow_factory: Callable[[], UnitOfWork],
    *,
    run_id: UUID,
    run_company_ids: tuple[UUID, ...],
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], date]:
    with uow_factory() as uow:
        run = uow.session.get(MonitoringRun, run_id)
        if run is None:
            raise RuntimeError("worker dispatch run was not found")
        rows = uow.session.execute(
            select(MonitoringRunCompany.id, SnapshotSetMember.company_id)
            .join(
                SnapshotSetMember,
                SnapshotSetMember.id == MonitoringRunCompany.snapshot_set_member_id,
            )
            .where(
                MonitoringRunCompany.run_id == run_id,
            )
        ).all()
        company_by_task = {task_id: company_id for task_id, company_id in rows}
        if not set(run_company_ids) <= set(company_by_task):
            raise RuntimeError("worker dispatch scope is incomplete")
        company_ids = tuple(company_by_task[value] for value in run_company_ids)
        summary_company_ids = tuple(sorted(set(company_by_task.values()), key=str))
        if run.period is not None:
            period = run.period
        else:
            if run.quarter is None:
                raise RuntimeError("worker dispatch period is unavailable")
            month = run.quarter * 3
            period = date(run.fiscal_year, month, monthrange(run.fiscal_year, month)[1])
    return company_ids, summary_company_ids, period
