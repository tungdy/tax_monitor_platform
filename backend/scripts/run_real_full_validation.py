"""Run read-only real-source validation and publish a local web artifact."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic
from typing import Final, Literal


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
BACKEND_SCRIPTS = REPO_ROOT / "backend" / "scripts"
for import_path in (BACKEND_ROOT, BACKEND_SRC, BACKEND_SCRIPTS):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from scripts.archive_feishu_alert_results import archive_report  # noqa: E402
from scripts.enqueue_feishu_alert_notifications import enqueue_report  # noqa: E402
from tax_risk.adapters.cache.memory_fetch_cache import MemoryFetchCache  # noqa: E402
from tax_risk.adapters.ingest.base import CanonicalFinancialRow  # noqa: E402
from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (  # noqa: E402
    DgcHesiInvoiceFieldMap,
    DgcHesiNoInvoiceAdapter,
    DgcHesiReimbursementFieldMap,
)
from tax_risk.adapters.ingest.dgc_sap_account_balance import (  # noqa: E402
    DgcSapAccountBalanceAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_dividend_detail import (  # noqa: E402
    DgcSapDividendDetailAdapter,
    DgcSettlementIncomeTaxExpenseAdapter,
    DgcSettlementOtherIncomeAdapter,
    DgcSettlementTaxesPayableAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_profit import (  # noqa: E402
    DgcClientConfig,
    DgcSapProfitAdapter,
    DgcSapProfitClient,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
)
from tax_risk.adapters.ingest.dgc_sap_trial_balance import (  # noqa: E402
    CURRENT_INCOME_TAX_GL_ACCOUNT,
    DgcSapTrialBalanceAdapter,
)
from tax_risk.application.external_fetch import (  # noqa: E402
    FetchCoordinatorConfig,
    FetchOutcome,
    FetchRequest,
    ParallelFetchCoordinator,
)
from tax_risk.application.tax_adjustment_accounts.web_report import (  # noqa: E402
    merge_tax_adjustment_report,
)
from tax_risk.config import Settings  # noqa: E402
from tax_risk.domain.money import Money, Rate  # noqa: E402
from tax_risk.domain.quarterly import (  # noqa: E402
    DeferredTaxInputs,
    QuarterlyInputs,
    calculate_deferred_tax,
    calculate_quarterly,
)


BASE_TOKEN: Final[str] = "A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
TABLE_ID: Final[str] = "tbl4PCNdcl4BYzgZ"
FIELD_IDS: Final[tuple[str, ...]] = (
    "fld5uBjB9R",
    "fld65JDObx",
    "fldgeRGkKv",
    "fld3zvDri3",
    "fld70tcRFh",
    "fld5c2IX6N",
    "fld6bBYJeP",
    "fld5KnsfqZ",
    "fld4HLnqDk",
)
SOURCE_NAMES: Final[tuple[str, ...]] = (
    "dgc_sap_profit",
    "dgc_sap_trial_balance",
    "dgc_sap_account_balance",
    "dgc_sap_dividend_detail",
    "dgc_hesi_reimbursement",
    "dgc_hesi_invoice",
)
MONITORS: Final[tuple[tuple[str, str], ...]] = (
    ("current_tax_accrual", "季度应计提所得税准确性检查"),
    ("deferred_tax", "递延所得税计提/转回准确性检查"),
    ("refund", "所得税退税进度监控及入账科目准确性检查"),
    ("tax_burden", "当年累计税负率异常监测"),
    ("potential_tax_cost", "潜在纳税调增税务成本"),
)
CURRENCY: Final[str] = "CNY"
AMOUNT_SCALE: Final[int] = 2


@dataclass(frozen=True, slots=True)
class CompanyMaster:
    code: str
    name: str
    tax_rate: Decimal | None
    deferred_tax_rate: Decimal | None
    loss_carryforward: Decimal | None
    historical_tax_burden: Decimal | None
    refund_involved: bool | None
    refund_amount: Decimal | None
    refund_status: str | None
    errors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class FetchKey:
    company_code: str
    source_name: str


def _decimal(value: object, *, rate: bool = False) -> Decimal | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = Decimal(str(value))
        elif isinstance(value, str) and value.strip():
            parsed = Decimal(value.strip().replace(",", ""))
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (rate and not Decimal(0) <= parsed <= Decimal(1)):
        return None
    return parsed


def _selection(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0].strip() or None
    return None


def _company_master(row: list[object]) -> CompanyMaster | None:
    code = str(row[0]).strip() if row[0] is not None else ""
    if not code:
        return None
    name = str(row[1]).strip() if row[1] is not None else ""
    tax_rate = _decimal(row[2], rate=True)
    deferred_rate = _decimal(row[3], rate=True)
    loss = _decimal(row[4])
    burden = _decimal(row[5], rate=True)
    involved_raw = _selection(row[6])
    refund_amount = _decimal(row[7])
    refund_status = str(row[8]).strip() if row[8] is not None else None
    errors: dict[str, str] = {}
    if not name:
        errors["company_name"] = "公司名称为空"
        name = code
    if tax_rate is None:
        errors["tax_rate"] = "所得税税率缺失或无效"
    if deferred_rate is None:
        errors["deferred_tax_rate"] = "递延所得税税率缺失或无效"
    if loss is None or loss < 0:
        errors["loss_carryforward"] = "可弥补亏损额合计缺失或无效"
        loss = None
    if burden is None:
        errors["historical_tax_burden"] = "3年平均税负率缺失或无效"
    refund_involved = {"是": True, "否": False}.get(involved_raw or "")
    if refund_involved is None:
        errors["refund_involved"] = "2025年是否涉及退税未明确"
    if refund_involved is True and (refund_amount is None or refund_amount <= 0):
        errors["refund_amount"] = "2025年应退税金额缺失或不大于0"
    return CompanyMaster(
        code=code,
        name=name,
        tax_rate=tax_rate,
        deferred_tax_rate=deferred_rate,
        loss_carryforward=loss,
        historical_tax_burden=burden,
        refund_involved=refund_involved,
        refund_amount=refund_amount,
        refund_status=refund_status or None,
        errors=errors,
    )


def _load_base_records() -> tuple[list[CompanyMaster], int, int]:
    cli = shutil.which("lark-cli")
    if cli is None:
        raise RuntimeError("lark-cli is not installed")
    offset = 0
    source_count = 0
    excluded = 0
    companies: list[CompanyMaster] = []
    while True:
        command = [
            cli,
            "base",
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
        ]
        for field_id in FIELD_IDS:
            command.extend(("--field-id", field_id))
        command.extend(
            (
                "--offset",
                str(offset),
                "--limit",
                "200",
                "--format",
                "json",
                "--as",
                "user",
            )
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError("Lark Base record read failed")
        envelope = json.loads(completed.stdout)
        if envelope.get("ok") is not True:
            raise RuntimeError("Lark Base record read returned ok=false")
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("field_id_list") != list(FIELD_IDS):
            raise RuntimeError("Lark Base projection contract changed")
        rows = data.get("data")
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or len(row) != len(FIELD_IDS) for row in rows
        ):
            raise RuntimeError("Lark Base records have an unexpected shape")
        source_count += len(rows)
        for row in rows:
            company = _company_master(row)
            if company is None:
                excluded += 1
            else:
                companies.append(company)
        if data.get("has_more") is not True:
            break
        if not rows:
            raise RuntimeError("Lark Base pagination did not advance")
        offset += len(rows)
    codes = [company.code for company in companies]
    if len(set(codes)) != len(codes):
        raise RuntimeError("Lark Base contains duplicate nonblank company codes")
    return companies, source_count, excluded


def _required_secret(settings: Settings, name: str) -> str:
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(f"{name} is not configured")
    secret = str(value.get_secret_value()).strip()
    if not secret:
        raise RuntimeError(f"{name} is blank")
    return secret


def _client_config(
    settings: Settings,
    *,
    url_name: str,
    key_name: str,
    secret_name: str,
    page_size: int,
    request_method: Literal["GET", "POST"] = "POST",
    strict_offset_pagination: bool = False,
) -> DgcClientConfig:
    api_url = getattr(settings, url_name)
    if not isinstance(api_url, str) or not api_url.strip():
        raise RuntimeError(f"{url_name} is not configured")
    return DgcClientConfig(
        api_url=api_url,
        request_method=request_method,
        app_key=_required_secret(settings, key_name),
        app_secret=_required_secret(settings, secret_name),
        timeout=settings.dgc_timeout_seconds,
        page_size=page_size,
        max_pages=settings.dgc_max_pages,
        max_records=settings.dgc_max_records,
        max_page_bytes=settings.dgc_max_page_bytes,
        max_total_bytes=settings.dgc_max_total_bytes,
        token_ttl=settings.dgc_token_ttl_seconds,
        strict_offset_pagination=strict_offset_pagination,
        tls_server_name=settings.dgc_tls_server_name,
        tls_pinned_certificate_sha256=settings.dgc_tls_pinned_certificate_sha256,
    )


def _build_sources(settings: Settings) -> dict[str, DgcSapProfitClient]:
    return {
        "dgc_sap_profit": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_sap_profit_api_url",
                key_name="dgc_app_key",
                secret_name="dgc_app_secret",
                page_size=settings.dgc_page_size,
            )
        ),
        "dgc_sap_trial_balance": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_sap_trial_balance_api_url",
                key_name="dgc_sap_trial_balance_app_key",
                secret_name="dgc_sap_trial_balance_app_secret",
                page_size=settings.dgc_sap_trial_balance_page_size,
            )
        ),
        "dgc_sap_account_balance": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_sap_account_balance_api_url",
                key_name="dgc_sap_account_balance_app_key",
                secret_name="dgc_sap_account_balance_app_secret",
                page_size=settings.dgc_sap_account_balance_page_size,
            )
        ),
        "dgc_sap_dividend_detail": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_sap_dividend_detail_api_url",
                key_name="dgc_sap_dividend_detail_app_key",
                secret_name="dgc_sap_dividend_detail_app_secret",
                page_size=settings.dgc_sap_dividend_detail_page_size,
            )
        ),
        "dgc_hesi_reimbursement": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_hesi_reimbursement_api_url",
                key_name="dgc_hesi_reimbursement_app_key",
                secret_name="dgc_hesi_reimbursement_app_secret",
                page_size=settings.dgc_hesi_reimbursement_page_size,
                strict_offset_pagination=True,
            )
        ),
        "dgc_hesi_invoice": DgcSapProfitClient(
            _client_config(
                settings,
                url_name="dgc_hesi_invoice_api_url",
                key_name="dgc_hesi_invoice_app_key",
                secret_name="dgc_hesi_invoice_app_secret",
                page_size=settings.dgc_hesi_invoice_page_size,
                request_method="POST",
                strict_offset_pagination=True,
            )
        ),
    }


def _parameters(company: str, source: str, year: int, period: int) -> dict[str, object]:
    if source == "dgc_sap_profit":
        return {"bukrs": company, "gjahr": str(year), "monat": f"{period:02d}"}
    if source == "dgc_sap_trial_balance":
        return {
            "company_code": company,
            "fiscal_year": str(year),
            "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
        }
    if source == "dgc_sap_account_balance":
        return {
            "company_code": company,
            "fiscal_year": str(year),
            "fiscal_period": f"{period:03d}",
        }
    if source == "dgc_sap_dividend_detail":
        return {"company": company, "fiscal_year": str(year)}
    if source in {"dgc_hesi_reimbursement", "dgc_hesi_invoice"}:
        return {"company_code": company}
    raise ValueError(f"unsupported source {source}")


def _fetch_all(
    companies: list[CompanyMaster],
    settings: Settings,
    *,
    year: int,
    period: int,
) -> tuple[dict[FetchKey, FetchOutcome], dict[FetchKey, str], float]:
    sources = _build_sources(settings)
    max_workers = settings.external_fetch_max_workers
    config = FetchCoordinatorConfig(
        max_workers=max_workers,
        source_concurrency=settings.external_fetch_source_concurrency,
        cache_ttl_seconds=settings.external_fetch_cache_ttl_seconds,
        empty_cache_ttl_seconds=settings.external_fetch_empty_cache_ttl_seconds,
        lock_ttl_seconds=settings.external_fetch_lock_ttl_seconds,
        lock_wait_seconds=settings.external_fetch_lock_wait_seconds,
        lock_poll_seconds=settings.external_fetch_lock_poll_seconds,
        retry_max_attempts=settings.external_fetch_retry_max_attempts,
        retry_base_delay_seconds=settings.external_fetch_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.external_fetch_retry_max_delay_seconds,
        retry_jitter_ratio=settings.external_fetch_retry_jitter_ratio,
    )
    coordinator = ParallelFetchCoordinator(
        sources,
        MemoryFetchCache(),
        config,
    )
    outcomes: dict[FetchKey, FetchOutcome] = {}
    errors: dict[FetchKey, str] = {}
    started = monotonic()
    total = len(companies) * len(SOURCE_NAMES)
    completed_count = 0
    try:
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="full-validation"
        ) as pool:
            futures = {}
            for company in companies:
                for source_name in SOURCE_NAMES:
                    key = FetchKey(company.code, source_name)
                    request = FetchRequest(
                        source_name=source_name,
                        parameters=_parameters(company.code, source_name, year, period),
                        schema_version="full-validation-v1",
                    )
                    futures[pool.submit(coordinator.fetch_one, request)] = key
            for future in as_completed(futures):
                key = futures[future]
                try:
                    outcomes[key] = future.result()
                except Exception as error:
                    error_code = getattr(error, "error_code", type(error).__name__)
                    errors[key] = str(error_code)[:128]
                completed_count += 1
                if completed_count % 100 == 0 or completed_count == total:
                    print(f"external fetch progress: {completed_count}/{total}", flush=True)
    finally:
        coordinator.close()
        for source in sources.values():
            source.close()
    return outcomes, errors, monotonic() - started


def _blocked(reason: str) -> dict[str, object]:
    return {"status": "BLOCKED", "outcome": "无法计算", "reason": reason, "values": {}}


def _money(value: Decimal) -> Money:
    return Money.unrounded(value, currency=CURRENCY, scale=AMOUNT_SCALE)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _profit_metrics(
    outcome: FetchOutcome,
    company: str,
    settings: Settings,
    extracted_at: datetime,
) -> tuple[dict[str, Decimal], str | None]:
    adapter = DgcSapProfitAdapter(
        outcome.result,
        field_map=DgcSapProfitFieldMap(**settings.dgc_sap_profit_field_map),
        metric_map=DgcSapProfitMetricMap(**settings.dgc_sap_profit_metric_map),
        ledger=settings.dgc_sap_profit_ledger,
        expected_company_code=company,
        currency=CURRENCY,
        amount_scale=AMOUNT_SCALE,
        extracted_at=extracted_at,
    )
    values: dict[str, Decimal] = {}
    errors: list[str] = []
    for row in adapter.iter_rows():
        if row.error is not None:
            errors.append(row.error.error_code)
        elif isinstance(row.value, CanonicalFinancialRow):
            values[row.value.metric_code] = row.value.amount
    return values, ",".join(sorted(set(errors))) or None


def _source_status(
    company: str,
    outcomes: Mapping[FetchKey, FetchOutcome],
    errors: Mapping[FetchKey, str],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for source_name in SOURCE_NAMES:
        key = FetchKey(company, source_name)
        if key in errors:
            result[source_name] = {"status": "ERROR", "error_code": errors[key]}
        else:
            outcome = outcomes[key]
            result[source_name] = {
                "status": "NO_DATA" if outcome.record_count == 0 else "DATA",
                "record_count": outcome.record_count,
                "provenance": outcome.provenance.value,
            }
    return result


def _evaluate_company(
    master: CompanyMaster,
    outcomes: Mapping[FetchKey, FetchOutcome],
    errors: Mapping[FetchKey, str],
    settings: Settings,
    *,
    year: int,
    period: int,
    extracted_at: datetime,
) -> dict[str, object]:
    adapted_errors: dict[str, str] = {}
    profit: dict[str, Decimal] = {}
    dividends: Decimal | None = None
    trial = None
    account = None
    hesi_no_invoice = None
    income_tax_lines = None
    other_income = None
    taxes_payable = None

    def outcome(source: str) -> FetchOutcome | None:
        return outcomes.get(FetchKey(master.code, source))

    profit_outcome = outcome("dgc_sap_profit")
    if profit_outcome is not None:
        try:
            profit, row_error = _profit_metrics(profit_outcome, master.code, settings, extracted_at)
            if row_error is not None:
                adapted_errors["dgc_sap_profit"] = row_error
        except Exception as error:
            adapted_errors["dgc_sap_profit"] = str(
                getattr(error, "error_code", type(error).__name__)
            )

    detail_outcome = outcome("dgc_sap_dividend_detail")
    if detail_outcome is not None:
        try:
            dividends = (
                DgcSapDividendDetailAdapter(
                    detail_outcome.result,
                    expected_company=master.code,
                    expected_fiscal_year=year,
                    through_period=period,
                )
                .adapt()
                .cumulative_dividend_amount
            )
            income_tax_lines = DgcSettlementIncomeTaxExpenseAdapter(
                detail_outcome.result,
                expected_company=master.code,
                expected_fiscal_year=year,
                through_period=period,
            ).adapt()
            other_income = DgcSettlementOtherIncomeAdapter(
                detail_outcome.result,
                expected_company=master.code,
                expected_fiscal_year=year,
                through_period=period,
            ).adapt()
            taxes_payable = DgcSettlementTaxesPayableAdapter(
                detail_outcome.result,
                expected_company=master.code,
                expected_fiscal_year=year,
                through_period=period,
            ).adapt()
        except Exception as error:
            adapted_errors["dgc_sap_dividend_detail"] = str(
                getattr(error, "error_code", type(error).__name__)
            )

    trial_outcome = outcome("dgc_sap_trial_balance")
    if trial_outcome is not None:
        try:
            trial = DgcSapTrialBalanceAdapter(
                trial_outcome.result,
                expected_company_code=master.code,
                expected_fiscal_year=year,
                through_period=period,
            ).adapt()
        except Exception as error:
            adapted_errors["dgc_sap_trial_balance"] = str(
                getattr(error, "error_code", type(error).__name__)
            )

    account_outcome = outcome("dgc_sap_account_balance")
    if account_outcome is not None:
        try:
            account = DgcSapAccountBalanceAdapter(
                account_outcome.result,
                expected_company_code=master.code,
                expected_fiscal_year=year,
                expected_fiscal_period=period,
            ).adapt()
        except Exception as error:
            adapted_errors["dgc_sap_account_balance"] = str(
                getattr(error, "error_code", type(error).__name__)
            )

    reimbursement_outcome = outcome("dgc_hesi_reimbursement")
    invoice_outcome = outcome("dgc_hesi_invoice")
    if reimbursement_outcome is not None and invoice_outcome is not None:
        try:
            hesi_no_invoice = DgcHesiNoInvoiceAdapter(
                reimbursement_outcome.result,
                invoice_outcome.result,
                reimbursement_field_map=DgcHesiReimbursementFieldMap(
                    **settings.dgc_hesi_reimbursement_field_map
                ),
                invoice_field_map=DgcHesiInvoiceFieldMap(**settings.dgc_hesi_invoice_field_map),
                expected_company_code=master.code,
                fiscal_year=year,
                through_period=period,
            ).adapt()
        except Exception as error:
            adapted_errors["dgc_hesi_no_invoice"] = str(
                getattr(error, "error_code", type(error).__name__)
            )

    fetch_errors = {
        key.source_name: code for key, code in errors.items() if key.company_code == master.code
    }

    common_reasons: list[str] = []
    for field in ("tax_rate", "loss_carryforward"):
        if field in master.errors:
            common_reasons.append(master.errors[field])
    for metric in ("cumulative_profit", "fair_value_change"):
        if metric not in profit:
            common_reasons.append(f"利润表缺少{metric}")
    if dividends is None:
        common_reasons.append("累计分红明细不可用")

    current_reasons = list(common_reasons)
    if trial is None:
        current_reasons.append("所得税费用科目发生额不可用")
    if current_reasons:
        current_result = _blocked("；".join(dict.fromkeys(current_reasons)))
    else:
        assert master.tax_rate is not None
        assert master.loss_carryforward is not None
        assert dividends is not None
        assert trial is not None
        current = calculate_quarterly(
            QuarterlyInputs(
                cumulative_profit=_money(profit["cumulative_profit"]),
                received_dividends=_money(dividends),
                fair_value_change=_money(profit["fair_value_change"]),
                loss_carryforward=_money(master.loss_carryforward),
                tax_rate=Rate.from_fraction(master.tax_rate),
                prior_quarter_current_tax=_money(trial.prior_quarter_current_tax),
                current_quarter_current_tax=_money(trial.current_quarter_current_tax),
                cumulative_revenue=_money(Decimal(0)),
                historical_average_tax_burden=Rate.from_fraction(Decimal(0)),
                other_payables_accrual=_money(Decimal(0)),
                hesi_no_invoice=_money(Decimal(0)),
            )
        )
        current_result = {
            "status": "ALERT" if current.accrual_alert_flag else "CLEAR",
            "outcome": {
                "UNDER_ACCRUED": "少计提",
                "OVER_ACCRUED": "多计提",
            }.get(current.accrual_alert_code or "", "计提一致"),
            "reason": None,
            "alert_code": current.accrual_alert_code,
            "values": {
                "cumulative_profit": _decimal_text(profit["cumulative_profit"]),
                "received_dividends": _decimal_text(dividends),
                "fair_value_change": _decimal_text(profit["fair_value_change"]),
                "loss_carryforward": _decimal_text(master.loss_carryforward),
                "tax_rate": _decimal_text(master.tax_rate),
                "prior_quarter_current_tax": _decimal_text(trial.prior_quarter_current_tax),
                "current_quarter_should_accrue": _decimal_text(
                    current.current_quarter_should_accrue.amount
                    if current.current_quarter_should_accrue is not None
                    else None
                ),
                "current_quarter_current_tax": _decimal_text(trial.current_quarter_current_tax),
                "difference": _decimal_text(
                    current.current_quarter_difference.amount
                    if current.current_quarter_difference is not None
                    else None
                ),
            },
        }

    deferred_reasons: list[str] = []
    for field in ("deferred_tax_rate", "loss_carryforward"):
        if field in master.errors:
            deferred_reasons.append(master.errors[field])
    if "cumulative_profit" not in profit:
        deferred_reasons.append("利润表缺少cumulative_profit")
    if account is None:
        deferred_reasons.append("科目余额表不可用")
    if deferred_reasons:
        deferred_result = _blocked("；".join(dict.fromkeys(deferred_reasons)))
    else:
        assert master.deferred_tax_rate is not None
        assert master.loss_carryforward is not None
        assert account is not None
        deferred = calculate_deferred_tax(
            DeferredTaxInputs(
                cumulative_profit=_money(profit["cumulative_profit"]),
                loss_carryforward=_money(master.loss_carryforward),
                deferred_tax_rate=Rate.from_fraction(master.deferred_tax_rate),
                sap_cumulative_deferred_tax_expense=_money(
                    account.sap_cumulative_deferred_tax_expense
                ),
            )
        )
        deferred_result = {
            "status": "ALERT" if deferred.alert_flag else "CLEAR",
            "outcome": {
                "DEFERRED_TAX_TO_ACCRUE": "应计提",
                "DEFERRED_TAX_TO_REVERSE": "应转回",
            }.get(deferred.alert_code or "", "无需调整"),
            "reason": deferred.not_calculated_reason,
            "alert_code": deferred.alert_code,
            "values": {
                "cumulative_profit": _decimal_text(profit["cumulative_profit"]),
                "loss_carryforward": _decimal_text(master.loss_carryforward),
                "deferred_tax_rate": _decimal_text(master.deferred_tax_rate),
                "deferred_tax_base": _decimal_text(
                    deferred.deferred_tax_base.amount
                    if deferred.deferred_tax_base is not None
                    else None
                ),
                "sap_cumulative_deferred_tax_expense": _decimal_text(
                    account.sap_cumulative_deferred_tax_expense
                ),
                "system_cumulative_deferred_tax": _decimal_text(
                    deferred.system_cumulative_deferred_tax.amount
                    if deferred.system_cumulative_deferred_tax is not None
                    else None
                ),
                "adjustment": _decimal_text(
                    deferred.current_year_deferred_tax_adjustment.amount
                    if deferred.current_year_deferred_tax_adjustment is not None
                    else None
                ),
            },
        }

    burden_reasons = list(common_reasons)
    if "cumulative_revenue" not in profit:
        burden_reasons.append("利润表缺少cumulative_revenue")
    if "historical_tax_burden" in master.errors:
        burden_reasons.append(master.errors["historical_tax_burden"])
    if burden_reasons:
        burden_result = _blocked("；".join(dict.fromkeys(burden_reasons)))
    else:
        assert master.tax_rate is not None
        assert master.loss_carryforward is not None
        assert master.historical_tax_burden is not None
        assert dividends is not None
        burden = calculate_quarterly(
            QuarterlyInputs(
                cumulative_profit=_money(profit["cumulative_profit"]),
                received_dividends=_money(dividends),
                fair_value_change=_money(profit["fair_value_change"]),
                loss_carryforward=_money(master.loss_carryforward),
                tax_rate=Rate.from_fraction(master.tax_rate),
                prior_quarter_current_tax=_money(Decimal(0)),
                current_quarter_current_tax=_money(Decimal(0)),
                cumulative_revenue=_money(profit["cumulative_revenue"]),
                historical_average_tax_burden=Rate.from_fraction(master.historical_tax_burden),
                other_payables_accrual=_money(Decimal(0)),
                hesi_no_invoice=_money(Decimal(0)),
            )
        )
        burden_result = {
            "status": "ALERT" if burden.tax_burden_alert_flag else "CLEAR",
            "outcome": {
                "TAX_BURDEN_HIGH": "税负率偏高",
                "TAX_BURDEN_LOW": "税负率偏低",
            }.get(burden.tax_burden_alert_code or "", "税负率正常"),
            "reason": burden.tax_burden_not_calculated_reason,
            "alert_code": burden.tax_burden_alert_code,
            "values": {
                "cumulative_tax_payable": _decimal_text(
                    burden.cumulative_tax_payable.amount
                    if burden.cumulative_tax_payable is not None
                    else None
                ),
                "cumulative_revenue": _decimal_text(profit["cumulative_revenue"]),
                "current_tax_burden": _decimal_text(burden.current_tax_burden),
                "historical_tax_burden": _decimal_text(master.historical_tax_burden),
                "deviation": _decimal_text(burden.tax_burden_deviation),
            },
        }

    potential_reasons = list(common_reasons)
    if account is None:
        potential_reasons.append("科目余额表不可用")
    elif account.other_payables_accrual is None:
        potential_reasons.append("科目余额表未返回其他应付款暂估科目")
    if hesi_no_invoice is None:
        potential_reasons.append("合思无票报销数据不可用")
    potential_result: dict[str, object]
    if potential_reasons:
        potential_result = _blocked("；".join(dict.fromkeys(potential_reasons)))
    else:
        assert master.tax_rate is not None
        assert master.loss_carryforward is not None
        assert dividends is not None
        assert account is not None
        assert account.other_payables_accrual is not None
        assert hesi_no_invoice is not None
        potential = calculate_quarterly(
            QuarterlyInputs(
                cumulative_profit=_money(profit["cumulative_profit"]),
                received_dividends=_money(dividends),
                fair_value_change=_money(profit["fair_value_change"]),
                loss_carryforward=_money(master.loss_carryforward),
                tax_rate=Rate.from_fraction(master.tax_rate),
                prior_quarter_current_tax=_money(Decimal(0)),
                current_quarter_current_tax=_money(Decimal(0)),
                cumulative_revenue=_money(Decimal(0)),
                historical_average_tax_burden=Rate.from_fraction(Decimal(0)),
                other_payables_accrual=_money(account.other_payables_accrual),
                hesi_no_invoice=_money(hesi_no_invoice.hesi_no_invoice),
            )
        )
        if (
            potential.potential_adjustment is None
            or potential.cumulative_tax_payable is None
            or potential.potential_tax_payable is None
            or potential.potential_tax_cost is None
        ):
            potential_result = _blocked(potential.not_calculated_reason or "潜在税务成本计算失败")
        else:
            potential_result = {
                "status": ("ALERT" if potential.potential_tax_cost_alert_flag else "CLEAR"),
                "outcome": (
                    "存在潜在纳税调增税务成本"
                    if potential.potential_tax_cost_alert_flag
                    else "无潜在纳税调增税务成本"
                ),
                "reason": potential.not_calculated_reason,
                "alert_code": potential.potential_tax_cost_alert_code,
                "values": {
                    "other_payables_accrual": _decimal_text(account.other_payables_accrual),
                    "reimbursement_expense_total": _decimal_text(
                        hesi_no_invoice.reimbursement_expense_total
                    ),
                    "invoice_approved_total": _decimal_text(hesi_no_invoice.invoice_approved_total),
                    "hesi_no_invoice": _decimal_text(hesi_no_invoice.hesi_no_invoice),
                    "potential_adjustment": _decimal_text(potential.potential_adjustment.amount),
                    "cumulative_tax_payable": _decimal_text(
                        potential.cumulative_tax_payable.amount
                    ),
                    "potential_tax_payable": _decimal_text(potential.potential_tax_payable.amount),
                    "potential_tax_cost": _decimal_text(potential.potential_tax_cost.amount),
                },
            }

    refund_result: dict[str, object]
    if master.refund_involved is False:
        refund_result = {
            "status": "NOT_APPLICABLE",
            "outcome": "不涉及退税",
            "reason": None,
            "values": {},
        }
    elif master.refund_involved is None:
        refund_result = _blocked(master.errors["refund_involved"])
    elif master.refund_status == "已退税":
        refund_result = {
            "status": "CLEAR",
            "outcome": "已退税（飞书已登记，停止扫描）",
            "reason": None,
            "evidence_limited": False,
            "values": {
                "refund_amount": _decimal_text(master.refund_amount),
                "match_count": "0",
                "booking_account": None,
                "booking_account_family": None,
                "receipt_source": "飞书手工登记",
            },
        }
    elif master.refund_amount is None or master.refund_amount <= 0:
        refund_result = _blocked(master.errors["refund_amount"])
    elif income_tax_lines is None or other_income is None or taxes_payable is None:
        refund_result = _blocked("汇算清缴相关科目明细不可用")
    else:
        expected = _money(master.refund_amount).quantized().amount
        primary_candidates: list[dict[str, str]] = []
        for line in income_tax_lines.lines:
            record = line.source_record
            amount = _money(line.income_tax_expense_amount).quantized().amount
            if record.group_currency == CURRENCY and amount == expected:
                primary_candidates.append(
                    {
                        "family": "所得税费用",
                        "account_code": record.gl_account,
                        "account_name": record.account_name,
                        "voucher_no": record.voucher_no,
                        "amount": _decimal_text(amount) or "0",
                    }
                )
        for record in other_income.records:
            raw_amount = (
                Decimal(0) if record.amount_ksl.is_zero() else record.amount_ksl.copy_negate()
            )
            amount = _money(raw_amount).quantized().amount
            if record.group_currency == CURRENCY and amount == expected:
                primary_candidates.append(
                    {
                        "family": "其他收益",
                        "account_code": record.gl_account,
                        "account_name": record.account_name,
                        "voucher_no": record.voucher_no,
                        "amount": _decimal_text(amount) or "0",
                    }
                )
        if primary_candidates:
            candidates = primary_candidates
            match_stage = "所得税费用及其他收益"
        else:
            candidates = []
            match_stage = "应交税费"
            for tax_line in taxes_payable.lines:
                record = tax_line.source_record
                amount = _money(tax_line.taxes_payable_amount).quantized().amount
                if record.group_currency == CURRENCY and amount == expected:
                    candidates.append(
                        {
                            "family": "应交税费",
                            "account_code": record.gl_account,
                            "account_name": record.account_name,
                            "voucher_no": record.voucher_no,
                            "amount": _decimal_text(amount) or "0",
                        }
                    )
        if not candidates:
            status = "CLEAR"
            outcome_text = "未取得退税"
            alert_code = None
        elif len(candidates) > 1:
            status = "ALERT"
            outcome_text = "存在多个等额候选"
            alert_code = "AMBIGUOUS_REFUND_MATCH"
        elif candidates[0]["family"] == "所得税费用":
            status = "CLEAR"
            outcome_text = "已退税且入账至所得税费用"
            alert_code = None
        elif candidates[0]["family"] == "其他收益":
            status = "ALERT"
            outcome_text = "已退税但入账至其他收益"
            alert_code = "REFUND_BOOKED_TO_WRONG_ACCOUNT"
        else:
            status = "ALERT"
            outcome_text = "已退税但入账至应交税费"
            alert_code = "REFUND_BOOKED_TO_WRONG_ACCOUNT"
        refund_result = {
            "status": status,
            "outcome": outcome_text,
            "reason": (
                "当前接口缺少行项目唯一标识、过账日期和冲销标志，结果仅作接口验证"
                if candidates
                else None
            ),
            "alert_code": alert_code,
            "evidence_limited": bool(candidates),
            "values": {
                "refund_amount": _decimal_text(master.refund_amount),
                "match_count": str(len(candidates)),
                "booking_account": (
                    candidates[0]["account_code"] if len(candidates) == 1 else None
                ),
                "booking_account_family": (
                    candidates[0]["family"] if len(candidates) == 1 else None
                ),
                "match_stage": match_stage,
                "receipt_source": "SAP等额匹配" if candidates else None,
            },
            "candidates": candidates,
        }

    return {
        "company_code": master.code,
        "company_name": master.name,
        "master_data_issues": list(master.errors.values()),
        "source_status": _source_status(master.code, outcomes, errors),
        "adapter_errors": adapted_errors,
        "fetch_errors": fetch_errors,
        "monitor_results": {
            "current_tax_accrual": current_result,
            "deferred_tax": deferred_result,
            "refund": refund_result,
            "tax_burden": burden_result,
            "potential_tax_cost": potential_result,
        },
    }


def _summary(companies: list[dict[str, object]]) -> dict[str, object]:
    monitors: dict[str, object] = {}
    for code, name in MONITORS:
        counts = {key: 0 for key in ("ALERT", "CLEAR", "BLOCKED", "NOT_APPLICABLE")}
        for company in companies:
            results = company["monitor_results"]
            assert isinstance(results, dict)
            result = results[code]
            assert isinstance(result, dict)
            counts[str(result["status"])] += 1
        monitors[code] = {"name": name, "total": len(companies), **counts}
    return monitors


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fiscal-year", type=int, default=2026)
    parser.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--max-companies", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "web" / "public" / "real-validation-latest.json",
    )
    parser.add_argument(
        "--tax-adjustment-results",
        type=Path,
        help="full tax-adjustment account check JSON; defaults to the acceptance artifact",
    )
    parser.add_argument(
        "--tax-adjustment-candidates",
        type=Path,
        help="tax-adjustment candidate detail JSON; defaults to the acceptance artifact",
    )
    parser.add_argument("--skip-alert-archive", action="store_true")
    parser.add_argument("--archive-base-as", choices=("user", "bot"), default="user")
    parser.add_argument("--archive-base-profile", default="tax-risk-notifier")
    parser.add_argument("--skip-alert-queue", action="store_true")
    parser.add_argument("--queue-base-as", choices=("user", "bot"), default="bot")
    parser.add_argument("--queue-base-profile", default="tax-risk-notifier")
    args = parser.parse_args()
    period = args.quarter * 3
    generated_at = datetime.now(UTC)
    companies, base_count, excluded = _load_base_records()
    companies.sort(key=lambda company: company.code)
    if args.max_companies is not None:
        companies = companies[: args.max_companies]
    print(
        f"company scope: {len(companies)} included, {excluded} blank codes excluded",
        flush=True,
    )
    settings = Settings(_env_file=REPO_ROOT / "infra" / ".env")  # type: ignore[call-arg]
    outcomes, fetch_errors, fetch_seconds = _fetch_all(
        companies,
        settings,
        year=args.fiscal_year,
        period=period,
    )
    evaluated = [
        _evaluate_company(
            company,
            outcomes,
            fetch_errors,
            settings,
            year=args.fiscal_year,
            period=period,
            extracted_at=generated_at,
        )
        for company in companies
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "fiscal_year": args.fiscal_year,
        "quarter": args.quarter,
        "through_period": period,
        "currency": CURRENCY,
        "amount_scale": AMOUNT_SCALE,
        "source_mode": "REAL",
        "company_scope": {
            "base_record_count": base_count,
            "excluded_blank_company_count": excluded,
            "included_company_count": len(companies),
        },
        "runtime": {
            "parallelism": settings.external_fetch_max_workers,
            "source_concurrency": settings.external_fetch_source_concurrency,
            "cache": "MEMORY",
            "external_fetch_seconds": round(fetch_seconds, 3),
            "request_count": len(companies) * len(SOURCE_NAMES),
            "request_error_count": len(fetch_errors),
        },
        "refund_evidence_notice": (
            "退税结果先逐条匹配所得税费用及其他收益，零命中时再匹配应交税费；"
            "接口缺少行项目唯一标识、过账日期和冲销标志，命中结果仅作接口验证且未回写飞书。"
        ),
        "monitor_summary": _summary(evaluated),
        "companies": evaluated,
    }
    tax_adjustment_results = args.tax_adjustment_results or (
        REPO_ROOT
        / "artifacts"
        / "acceptance"
        / f"tax_adjustment_accounts_full_{args.fiscal_year}_{period:02d}.json"
    )
    tax_adjustment_candidates = args.tax_adjustment_candidates or (
        REPO_ROOT
        / "artifacts"
        / "acceptance"
        / f"tax_adjustment_accounts_candidates_{args.fiscal_year}_{period:02d}.json"
    )
    payload = merge_tax_adjustment_report(
        payload,
        result_path=tax_adjustment_results.resolve(),
        candidate_path=tax_adjustment_candidates.resolve(),
    )
    _write_json(args.output.resolve(), payload)
    print(f"result written: {args.output.resolve()}", flush=True)
    if args.max_companies is not None:
        print("alert archive skipped: partial --max-companies run", flush=True)
    elif args.skip_alert_archive:
        print("alert archive skipped by explicit option", flush=True)
    else:
        _, archive_results = archive_report(
            payload,
            base_identity=args.archive_base_as,
            base_profile=args.archive_base_profile,
        )
        for archive_result in archive_results:
            print(
                f"alert archive {archive_result.table_name}: "
                f"created={archive_result.created_rows}, "
                f"restored={archive_result.restored_rows}, "
                f"retired={archive_result.retired_rows}",
                flush=True,
            )
    if args.max_companies is not None:
        print("alert queue skipped: partial --max-companies run", flush=True)
    elif args.skip_alert_queue:
        print("alert queue skipped by explicit option", flush=True)
    else:
        queue_plan, queue_result = enqueue_report(
            payload,
            monitor_codes={"tax_adjustment_account_accuracy"},
            base_identity=args.queue_base_as,
            base_profile=args.queue_base_profile,
        )
        assert queue_result is not None
        print(
            "alert queue tax_adjustment_account_accuracy: "
            f"planned={len(queue_plan.items)}, "
            f"created={queue_result.created_rows}, "
            f"existing={queue_result.existing_rows}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
