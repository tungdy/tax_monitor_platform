"""Controlled ingest for the quarterly Hesi no-invoice metric."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json

from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (
    DgcHesiInvoiceFieldMap,
    DgcHesiNoInvoiceAdapter,
    DgcHesiNoInvoiceMetricAdapter,
    DgcHesiReimbursementFieldMap,
    HESI_NO_INVOICE_CALCULATION_VERSION,
)
from tax_risk.application.dgc_hesi_invoice import DgcHesiInvoiceSource
from tax_risk.application.dgc_hesi_reimbursement import DgcHesiReimbursementSource
from tax_risk.application.ingest import BatchMetadata, BatchView, IngestService
from tax_risk.persistence.ingest_models import IngestMode


@dataclass(frozen=True, slots=True)
class DgcHesiNoInvoiceImportCommand:
    source_batch_key: str
    extraction_time: datetime
    company_code: str
    fiscal_year: str
    fiscal_period: int
    mode: IngestMode
    schema_version: str
    currency: str
    amount_scale: int


@dataclass(frozen=True, slots=True)
class DgcHesiNoInvoiceImportResult:
    batch: BatchView
    created: bool


class DgcHesiNoInvoiceImportService:
    """Pull both Hesi sources and persist one evidenced quarterly metric."""

    SOURCE = "DGC_HESI_NO_INVOICE"
    DATASET_CODE = "quarterly_metric"
    PAYLOAD_REF = "dgc://hesi-no-invoice"

    def __init__(
        self,
        ingest_service: IngestService,
        reimbursement_source: DgcHesiReimbursementSource,
        invoice_source: DgcHesiInvoiceSource,
        reimbursement_field_map: DgcHesiReimbursementFieldMap,
        invoice_field_map: DgcHesiInvoiceFieldMap,
    ) -> None:
        self._ingest_service = ingest_service
        self._reimbursement_source = reimbursement_source
        self._invoice_source = invoice_source
        self._reimbursement_field_map = reimbursement_field_map
        self._invoice_field_map = invoice_field_map

    def import_quarterly_metric(
        self,
        command: DgcHesiNoInvoiceImportCommand,
    ) -> DgcHesiNoInvoiceImportResult:
        source_batch_key = _nonempty(command.source_batch_key, "source_batch_key")
        schema_version = _nonempty(command.schema_version, "schema_version")
        company_code = _nonempty(command.company_code, "company_code")
        fiscal_year = _fiscal_year(command.fiscal_year)
        fiscal_period = _quarter_end(command.fiscal_period)
        currency = _currency(command.currency)
        amount_scale = _amount_scale(command.amount_scale)
        extraction_time = _extraction_time(command.extraction_time)
        if not isinstance(command.mode, IngestMode):
            raise TypeError("mode must be an IngestMode")

        reimbursement_parameters = {"company_code": company_code}
        invoice_parameters = {"company_code": company_code}
        scope = {
            "calculation_version": HESI_NO_INVOICE_CALCULATION_VERSION,
            "company_code": company_code,
            "fiscal_year": fiscal_year,
            "through_period": fiscal_period,
            "reimbursement_parameters": reimbursement_parameters,
            "invoice_parameters": invoice_parameters,
        }
        scope_sha256 = dgc_hesi_no_invoice_scope_sha256(scope)
        reimbursement_result = self._reimbursement_source.fetch(reimbursement_parameters)
        invoice_result = self._invoice_source.fetch(invoice_parameters)
        result = DgcHesiNoInvoiceAdapter(
            reimbursement_result,
            invoice_result,
            reimbursement_field_map=self._reimbursement_field_map,
            invoice_field_map=self._invoice_field_map,
            expected_company_code=company_code,
            fiscal_year=fiscal_year,
            through_period=fiscal_period,
        ).adapt()
        adapter = DgcHesiNoInvoiceMetricAdapter(
            result,
            company_code=company_code,
            fiscal_year=int(fiscal_year),
            fiscal_period=fiscal_period,
            currency=currency,
            amount_scale=amount_scale,
            extracted_at=extraction_time,
        )
        period = date(
            int(fiscal_year),
            fiscal_period,
            monthrange(int(fiscal_year), fiscal_period)[1],
        )
        created = self._ingest_service.create_batch(
            BatchMetadata(
                source=self.SOURCE,
                source_batch_key=source_batch_key,
                dataset_code=self.DATASET_CODE,
                extraction_time=extraction_time,
                period=period,
                mode=command.mode,
                schema_version=schema_version,
                currency=currency,
                amount_scale=amount_scale,
                source_primary_key_definition={
                    "fields": ["source_record_key"],
                    "dgc_hesi_no_invoice_scope_sha256": scope_sha256,
                    "reimbursement_checksum": reimbursement_result.checksum,
                    "invoice_checksum": invoice_result.checksum,
                    "reimbursement_source_record_count": len(reimbursement_result.records),
                    "invoice_source_record_count": len(invoice_result.records),
                    "reimbursement_duplicate_count": result.reimbursement_duplicate_count,
                    "invoice_duplicate_count": result.invoice_duplicate_count,
                    "invoice_duplicate_key": ["full_payload_sha256"],
                    "claim_level_aggregation_count": len(
                        result.claim_level_aggregation_codes
                    ),
                },
            )
        )
        batch = self._ingest_service.ingest_adapter(
            created.batch.id,
            f"{self.PAYLOAD_REF}?scope_sha256={scope_sha256}",
            adapter,
        )
        return DgcHesiNoInvoiceImportResult(batch=batch, created=created.created)


def dgc_hesi_no_invoice_scope_sha256(scope: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _fiscal_year(value: object) -> str:
    normalized = _nonempty(value, "fiscal_year")
    if (
        len(normalized) != 4
        or not normalized.isascii()
        or not normalized.isdecimal()
        or not 2000 <= int(normalized) <= 9999
    ):
        raise ValueError("fiscal_year must be a four-digit year from 2000 to 9999")
    return normalized


def _quarter_end(value: object) -> int:
    if type(value) is not int or value not in {3, 6, 9, 12}:
        raise ValueError("fiscal_period must be one of 3, 6, 9, or 12")
    return value


def _currency(value: object) -> str:
    normalized = _nonempty(value, "currency").upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be three ASCII letters")
    return normalized


def _amount_scale(value: object) -> int:
    if type(value) is not int:
        raise TypeError("amount_scale must be an integer")
    if not 0 <= value <= 12:
        raise ValueError("amount_scale must be between 0 and 12")
    return value


def _extraction_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("extraction_time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("extraction_time must include a UTC offset")
    return value


__all__ = [
    "DgcHesiNoInvoiceImportCommand",
    "DgcHesiNoInvoiceImportResult",
    "DgcHesiNoInvoiceImportService",
    "dgc_hesi_no_invoice_scope_sha256",
]
