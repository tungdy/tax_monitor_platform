from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (
    DgcHesiInvoiceFieldMap,
    DgcHesiReimbursementFieldMap,
    HESI_NO_INVOICE_CALCULATION_VERSION,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.api.schemas import DgcHesiNoInvoiceImportRequest
from tax_risk.application.dgc_hesi_no_invoice import (
    DgcHesiNoInvoiceImportCommand,
    DgcHesiNoInvoiceImportService,
    dgc_hesi_no_invoice_scope_sha256,
)
from tax_risk.application.ingest import (
    BatchMetadata,
    BatchView,
    CreateBatchResult,
    IngestService,
)
from tax_risk.persistence.ingest_models import IngestMode


class _Source:
    def __init__(self, result: DgcFetchResult) -> None:
        self.result = result
        self.parameters: Mapping[str, object] | None = None

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.parameters = parameters
        return self.result


class _Ingest:
    def __init__(self) -> None:
        self.batch = cast(BatchView, SimpleNamespace(id=uuid4()))
        self.metadata: BatchMetadata | None = None
        self.ingested: tuple[object, str, object] | None = None

    def create_batch(self, metadata: BatchMetadata) -> CreateBatchResult:
        self.metadata = metadata
        return CreateBatchResult(batch=self.batch, created=True)

    def ingest_adapter(self, batch_id: object, payload_ref: str, adapter: object) -> BatchView:
        self.ingested = (batch_id, payload_ref, adapter)
        return self.batch


def test_import_fetches_both_sources_and_persists_one_metric() -> None:
    extracted_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    reimbursements = _Source(
        DgcFetchResult(records=(_reimbursement("100"),), checksum="a" * 64)
    )
    invoices = _Source(DgcFetchResult(records=(_invoice("25"),), checksum="b" * 64))
    ingest = _Ingest()
    service = DgcHesiNoInvoiceImportService(
        cast(IngestService, ingest),
        reimbursements,
        invoices,
        DgcHesiReimbursementFieldMap(),
        DgcHesiInvoiceFieldMap(),
    )
    command = DgcHesiNoInvoiceImportCommand(
        source_batch_key="hesi-no-invoice-3000-2026-q2",
        extraction_time=extracted_at,
        company_code=" 3000 ",
        fiscal_year=" 2026 ",
        fiscal_period=6,
        mode=IngestMode.FULL,
        schema_version=" 1 ",
        currency="cny",
        amount_scale=2,
    )

    result = service.import_quarterly_metric(command)

    assert result.created is True
    assert reimbursements.parameters == {"company_code": "3000"}
    assert invoices.parameters == {"company_code": "3000"}
    scope = {
        "calculation_version": HESI_NO_INVOICE_CALCULATION_VERSION,
        "company_code": "3000",
        "fiscal_year": "2026",
        "through_period": 6,
        "reimbursement_parameters": {"company_code": "3000"},
        "invoice_parameters": {"company_code": "3000"},
    }
    scope_sha256 = dgc_hesi_no_invoice_scope_sha256(scope)
    assert ingest.metadata == BatchMetadata(
        source="DGC_HESI_NO_INVOICE",
        source_batch_key="hesi-no-invoice-3000-2026-q2",
        dataset_code="quarterly_metric",
        extraction_time=extracted_at,
        period=date(2026, 6, 30),
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={
            "fields": ["source_record_key"],
            "dgc_hesi_no_invoice_scope_sha256": scope_sha256,
            "reimbursement_checksum": "a" * 64,
            "invoice_checksum": "b" * 64,
            "reimbursement_source_record_count": 1,
            "invoice_source_record_count": 1,
            "reimbursement_duplicate_count": 0,
            "invoice_duplicate_count": 0,
            "invoice_duplicate_key": ["full_payload_sha256"],
            "claim_level_aggregation_count": 0,
        },
    )
    assert ingest.ingested is not None
    assert ingest.ingested[1] == f"dgc://hesi-no-invoice?scope_sha256={scope_sha256}"
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    rows = tuple(adapter.iter_rows())
    assert len(rows) == 1
    assert isinstance(rows[0].value, CanonicalFinancialRow)
    assert rows[0].value.metric_code == "hesi_no_invoice"
    assert rows[0].value.amount == Decimal("75")


def test_request_normalizes_company_year_and_quarter_end() -> None:
    request = DgcHesiNoInvoiceImportRequest.model_validate(
        {
            "source_batch_key": " hesi-3000-q2 ",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company_code": " 3000 ",
            "fiscal_year": " 2026 ",
            "fiscal_period": "006",
        }
    )

    assert request.source_batch_key == "hesi-3000-q2"
    assert request.company_code == "3000"
    assert request.fiscal_year == "2026"
    assert request.fiscal_period == 6


def _reimbursement(amount: str) -> dict[str, object]:
    return {
        "company_code": "3000",
        "flow_end_date": "2026-06-30",
        "expense_code": "C-1",
        "fee_type_code": "F1000",
        "fee_type_amount": amount,
    }


def _invoice(amount: str) -> dict[str, object]:
    return {
        "company_code": "3000",
        "code": "C-1",
        "invoice_id": "INVOICE-C-1",
        "feetypeid": "TYPE-F1000",
        "amount_standard_dec": "100",
        "approve_amount_dec": amount,
    }
