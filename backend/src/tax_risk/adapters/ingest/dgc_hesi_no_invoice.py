"""Calculate the quarterly Hesi no-invoice metric from two DGC sources."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from typing import Final, cast

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    fits_database_amount,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES: Final[frozenset[str]] = frozenset(
    {
        "CLF0101",
        "CLF0102",
        "CLF0103",
        "CLF0117",
        "CLF0118",
        "CLF0119",
        "CLF0120",
        "CLF0126",
        "CLF0130",
        "CLF0131",
        "F0507",
        "F0508",
        "F0605",
        "F5409",
        "F5724",
        "F5725",
        "F5809",
        "F5811",
        "F6309",
    }
)


class DgcHesiNoInvoiceError(ValueError):
    """Stable, row-aware rejection for unsafe Hesi source data."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        source: str | None = None,
        row_number: int | None = None,
        field: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.source = source
        self.row_number = row_number
        self.field = field
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DgcHesiReimbursementFieldMap:
    company_code: str = "company_code"
    approval_completed_at: str = "flow_end_date"
    expense_claim_code: str = "expense_code"
    expense_type_code: str = "fee_type_code"
    expense_type_amount: str = "fee_type_amount"


@dataclass(frozen=True, slots=True)
class DgcHesiInvoiceFieldMap:
    company_code: str = "company_code"
    expense_claim_code: str = "code"
    invoice_approved_amount: str = "approve_amount_dec"


@dataclass(frozen=True, slots=True)
class DgcHesiReimbursementRecord:
    company_code: str
    approval_completed_on: date | None
    expense_claim_code: str
    expense_type_code: str
    expense_type_amount: Decimal
    source_row_number: int


@dataclass(frozen=True, slots=True)
class DgcHesiInvoiceRecord:
    company_code: str
    expense_claim_code: str
    invoice_approved_amount: Decimal
    source_row_number: int


@dataclass(frozen=True, slots=True)
class DgcHesiNoInvoiceResult:
    reimbursement_records: tuple[DgcHesiReimbursementRecord, ...]
    invoice_records: tuple[DgcHesiInvoiceRecord, ...]
    reimbursement_expense_total: Decimal
    invoice_approved_total: Decimal
    hesi_no_invoice: Decimal
    excluded_reimbursement_count: int
    excluded_invoice_count: int
    source_checksum: str


class DgcHesiNoInvoiceAdapter:
    """Validate, filter, and aggregate Hesi reimbursement and invoice rows."""

    def __init__(
        self,
        reimbursement_result: DgcFetchResult,
        invoice_result: DgcFetchResult,
        *,
        reimbursement_field_map: DgcHesiReimbursementFieldMap,
        invoice_field_map: DgcHesiInvoiceFieldMap,
        expected_company_code: str,
        fiscal_year: int | str,
        through_period: int | str,
    ) -> None:
        if not isinstance(reimbursement_result, DgcFetchResult):
            raise TypeError("reimbursement_result must be a DgcFetchResult")
        if not isinstance(invoice_result, DgcFetchResult):
            raise TypeError("invoice_result must be a DgcFetchResult")
        if not isinstance(reimbursement_field_map, DgcHesiReimbursementFieldMap):
            raise TypeError("reimbursement_field_map must be a DgcHesiReimbursementFieldMap")
        if not isinstance(invoice_field_map, DgcHesiInvoiceFieldMap):
            raise TypeError("invoice_field_map must be a DgcHesiInvoiceFieldMap")
        self._reimbursement_result = reimbursement_result
        self._invoice_result = invoice_result
        self._reimbursement_field_map = reimbursement_field_map
        self._invoice_field_map = invoice_field_map
        self._company_code = _required_text(expected_company_code, "expected_company_code")
        self._fiscal_year = _expected_year(fiscal_year)
        self._through_period = _quarter_end(through_period)

    def adapt(self) -> DgcHesiNoInvoiceResult:
        reimbursements = tuple(
            self._parse_reimbursement(raw, row_number)
            for row_number, raw in enumerate(self._reimbursement_result.records, start=1)
        )
        invoices = tuple(
            self._parse_invoice(raw, row_number)
            for row_number, raw in enumerate(self._invoice_result.records, start=1)
        )
        period_start = date(self._fiscal_year, 1, 1)
        period_end = date(
            self._fiscal_year,
            self._through_period,
            monthrange(self._fiscal_year, self._through_period)[1],
        )
        scoped_reimbursements = tuple(
            record
            for record in reimbursements
            if record.approval_completed_on is not None
            and period_start <= record.approval_completed_on <= period_end
        )
        included_reimbursements = tuple(
            record
            for record in scoped_reimbursements
            if record.expense_type_code not in HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES
        )
        included_claim_codes = frozenset(
            record.expense_claim_code for record in included_reimbursements
        )
        included_invoices = tuple(
            record for record in invoices if record.expense_claim_code in included_claim_codes
        )
        reimbursement_total = _exact_sum(
            tuple(record.expense_type_amount for record in included_reimbursements)
        )
        invoice_total = _exact_sum(
            tuple(record.invoice_approved_amount for record in included_invoices)
        )
        difference = reimbursement_total - invoice_total
        no_invoice_amount = difference if difference > 0 else Decimal(0)
        for field, amount in (
            ("reimbursement_expense_total", reimbursement_total),
            ("invoice_approved_total", invoice_total),
            ("hesi_no_invoice", no_invoice_amount),
        ):
            if not fits_database_amount(amount):
                raise DgcHesiNoInvoiceError(
                    "AGGREGATE_AMOUNT_OUT_OF_RANGE",
                    f"{field} must fit NUMERIC(38,12)",
                    field=field,
                )
        source_checksum = _combined_checksum(
            self._reimbursement_result.checksum,
            self._invoice_result.checksum,
            self._company_code,
            self._fiscal_year,
            self._through_period,
        )
        return DgcHesiNoInvoiceResult(
            reimbursement_records=scoped_reimbursements,
            invoice_records=included_invoices,
            reimbursement_expense_total=reimbursement_total,
            invoice_approved_total=invoice_total,
            hesi_no_invoice=no_invoice_amount,
            excluded_reimbursement_count=(
                len(scoped_reimbursements) - len(included_reimbursements)
            ),
            excluded_invoice_count=len(invoices) - len(included_invoices),
            source_checksum=source_checksum,
        )

    def _parse_reimbursement(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> DgcHesiReimbursementRecord:
        source = "hesi_reimbursement"
        mapping = self._reimbursement_field_map
        _validate_mapped_fields(
            raw,
            (
                mapping.company_code,
                mapping.approval_completed_at,
                mapping.expense_claim_code,
                mapping.expense_type_code,
                mapping.expense_type_amount,
            ),
            source,
            row_number,
        )
        company_code = _text(raw[mapping.company_code], mapping.company_code, source, row_number)
        _validate_company(company_code, self._company_code, source, row_number)
        return DgcHesiReimbursementRecord(
            company_code=company_code,
            approval_completed_on=_optional_approval_date(
                raw[mapping.approval_completed_at],
                mapping.approval_completed_at,
                source,
                row_number,
            ),
            expense_claim_code=_text(
                raw[mapping.expense_claim_code],
                mapping.expense_claim_code,
                source,
                row_number,
            ),
            expense_type_code=_expense_type_code(
                raw[mapping.expense_type_code],
                mapping.expense_type_code,
                source,
                row_number,
            ),
            expense_type_amount=_amount(
                raw[mapping.expense_type_amount],
                mapping.expense_type_amount,
                source,
                row_number,
            ),
            source_row_number=row_number,
        )

    def _parse_invoice(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> DgcHesiInvoiceRecord:
        source = "hesi_invoice"
        mapping = self._invoice_field_map
        _validate_mapped_fields(
            raw,
            (
                mapping.company_code,
                mapping.expense_claim_code,
                mapping.invoice_approved_amount,
            ),
            source,
            row_number,
        )
        company_code = _text(raw[mapping.company_code], mapping.company_code, source, row_number)
        _validate_company(company_code, self._company_code, source, row_number)
        return DgcHesiInvoiceRecord(
            company_code=company_code,
            expense_claim_code=_text(
                raw[mapping.expense_claim_code],
                mapping.expense_claim_code,
                source,
                row_number,
            ),
            invoice_approved_amount=_amount(
                raw[mapping.invoice_approved_amount],
                mapping.invoice_approved_amount,
                source,
                row_number,
            ),
            source_row_number=row_number,
        )


class DgcHesiNoInvoiceMetricAdapter:
    """Materialize the calculated Hesi amount as one quarterly metric."""

    def __init__(
        self,
        result: DgcHesiNoInvoiceResult,
        *,
        company_code: str,
        fiscal_year: int,
        fiscal_period: int,
        currency: str,
        amount_scale: int,
        extracted_at: datetime,
    ) -> None:
        if not isinstance(result, DgcHesiNoInvoiceResult):
            raise TypeError("result must be a DgcHesiNoInvoiceResult")
        company = _required_text(company_code, "company_code")
        year = _expected_year(fiscal_year)
        period_number = _quarter_end(fiscal_period)
        period = date(year, period_number, monthrange(year, period_number)[1])
        identity = json.dumps(
            (company, str(year), f"{period_number:03d}", "hesi_no_invoice"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._checksum = result.source_checksum
        self._row = CanonicalFinancialRow(
            source_record_key=f"dgc-hesi-no-invoice:{sha256(identity).hexdigest()}",
            company_code=company,
            fiscal_year=year,
            period=period,
            currency=currency,
            amount_scale=amount_scale,
            metric_code="hesi_no_invoice",
            amount=result.hesi_no_invoice,
            extracted_at=extracted_at,
        )

    @property
    def checksum(self) -> str:
        return self._checksum

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        yield AdapterRow(row_number=1, value=self._row, error=None)


def _validate_mapped_fields(
    raw: Mapping[str, object],
    required_fields: tuple[str, ...],
    source: str,
    row_number: int,
) -> None:
    if any(not isinstance(key, str) for key in raw):
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_FIELD",
            f"{source} response field names must be strings",
            source=source,
            row_number=row_number,
        )
    missing = tuple(field for field in required_fields if field not in raw)
    if missing:
        raise DgcHesiNoInvoiceError(
            "MISSING_RESPONSE_FIELD",
            f"{source} response row is missing mapped fields: " + ", ".join(missing),
            source=source,
            row_number=row_number,
            field=missing[0],
        )


def _validate_company(
    actual: str,
    expected: str,
    source: str,
    row_number: int,
) -> None:
    if actual != expected:
        raise DgcHesiNoInvoiceError(
            "DGC_RESPONSE_SCOPE_MISMATCH",
            f"{source} response contained a company outside the requested scope",
            source=source,
            row_number=row_number,
            field="company_code",
        )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _text(value: object, field: str, source: str, row_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be a non-empty string",
            source=source,
            row_number=row_number,
            field=field,
        )
    return value.strip()


def _expense_type_code(value: object, field: str, source: str, row_number: int) -> str:
    return _text(value, field, source, row_number).upper()


def _optional_approval_date(
    value: object,
    field: str,
    source: str,
    row_number: int,
) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    parsed: date
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00")).date()
            except ValueError as error:
                raise DgcHesiNoInvoiceError(
                    "INVALID_RESPONSE_VALUE",
                    f"{field} must be an ISO date or datetime",
                    source=source,
                    row_number=row_number,
                    field=field,
                ) from error
    else:
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an ISO date or datetime",
            source=source,
            row_number=row_number,
            field=field,
        )
    return parsed


def _amount(value: object, field: str, source: str, row_number: int) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an exact decimal, integer, or decimal string",
            source=source,
            row_number=row_number,
            field=field,
        )
    try:
        if isinstance(value, Decimal):
            parsed = value
        elif type(value) is int:
            parsed = Decimal(value)
        elif isinstance(value, str) and value.strip():
            parsed = Decimal(value.strip())
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be an exact decimal, integer, or decimal string",
            source=source,
            row_number=row_number,
            field=field,
        ) from error
    if not parsed.is_finite() or not fits_database_amount(parsed):
        raise DgcHesiNoInvoiceError(
            "INVALID_RESPONSE_VALUE",
            f"{field} must be finite and fit NUMERIC(38,12)",
            source=source,
            row_number=row_number,
            field=field,
        )
    return parsed


def _expected_year(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            raise ValueError("fiscal_year must be an integer from 2000 to 9999")
        parsed = int(normalized)
    else:
        raise TypeError("fiscal_year must be an integer or decimal string")
    if not 2000 <= parsed <= 9999:
        raise ValueError("fiscal_year must be an integer from 2000 to 9999")
    return parsed


def _quarter_end(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdecimal():
            raise ValueError("through_period must be a quarter-end month")
        parsed = int(normalized)
    else:
        raise TypeError("through_period must be an integer or decimal string")
    if parsed not in {3, 6, 9, 12}:
        raise ValueError("through_period must be one of 3, 6, 9, or 12")
    return parsed


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal(0)
    minimum_exponent = min(cast(int, value.as_tuple().exponent) for value in values)
    maximum_adjusted = max(value.adjusted() for value in values)
    aligned_digits = max(1, maximum_adjusted - minimum_exponent + 1)
    with localcontext() as context:
        context.prec = aligned_digits + len(str(len(values))) + 1
        return sum(values, start=Decimal(0))


def _combined_checksum(
    reimbursement_checksum: str,
    invoice_checksum: str,
    company_code: str,
    fiscal_year: int,
    through_period: int,
) -> str:
    payload = json.dumps(
        {
            "company_code": company_code,
            "excluded_expense_type_codes": sorted(
                HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES
            ),
            "fiscal_year": fiscal_year,
            "invoice_checksum": invoice_checksum,
            "reimbursement_checksum": reimbursement_checksum,
            "through_period": through_period,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


__all__ = [
    "DgcHesiInvoiceFieldMap",
    "DgcHesiInvoiceRecord",
    "DgcHesiNoInvoiceAdapter",
    "DgcHesiNoInvoiceError",
    "DgcHesiNoInvoiceMetricAdapter",
    "DgcHesiNoInvoiceResult",
    "DgcHesiReimbursementFieldMap",
    "DgcHesiReimbursementRecord",
    "HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES",
]
