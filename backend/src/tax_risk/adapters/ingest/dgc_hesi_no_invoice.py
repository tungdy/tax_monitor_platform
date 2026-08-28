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
HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_PREFIXES: Final[tuple[str, ...]] = (
    "F39",
    "F72",
    "F73",
    "F35",
    "F64",
    "F07",
    "F74",
    "F44",
    "F40",
    "F55",
)
HESI_NO_INVOICE_CALCULATION_VERSION: Final[str] = "code-rollup-v3"


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
    invoice_id: str = "invoice_id"
    expense_type_id: str = "feetypeid"
    expense_line_amount: str = "amount_standard_dec"
    invoice_approved_amount: str = "approve_amount_dec"


@dataclass(frozen=True, slots=True)
class DgcHesiReimbursementRecord:
    company_code: str
    approval_completed_on: date | None
    expense_claim_code: str
    expense_type_code: str
    expense_type_amount: Decimal
    source_fingerprint: str
    source_row_number: int


@dataclass(frozen=True, slots=True)
class DgcHesiInvoiceRecord:
    company_code: str
    approval_completed_on: date
    expense_claim_code: str
    invoice_id: str
    expense_type_id: str
    expense_type_code: str
    expense_type_candidates: tuple[str, ...]
    excluded_expense_type: bool
    expense_line_amount: Decimal
    invoice_approved_amount: Decimal
    source_row_number: int


@dataclass(frozen=True, slots=True)
class _DgcHesiInvoiceSourceRecord:
    company_code: str
    expense_claim_code: str
    invoice_id: str
    expense_type_id: str
    expense_line_amount: Decimal
    invoice_approved_amount: Decimal
    source_fingerprint: str
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
    reimbursement_duplicate_count: int = 0
    invoice_duplicate_count: int = 0
    claim_level_aggregation_codes: tuple[str, ...] = ()


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
        parsed_reimbursements = tuple(
            self._parse_reimbursement(raw, row_number)
            for row_number, raw in enumerate(self._reimbursement_result.records, start=1)
        )
        reimbursements, reimbursement_duplicate_count = _deduplicate_reimbursements(
            parsed_reimbursements
        )
        parsed_invoice_sources = tuple(
            self._parse_invoice_source(raw, row_number)
            for row_number, raw in enumerate(self._invoice_result.records, start=1)
        )
        invoice_sources, invoice_duplicate_count = _deduplicate_invoice_sources(
            parsed_invoice_sources
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
            if not _is_excluded_expense_type_code(record.expense_type_code)
        )
        included_claim_codes = frozenset(
            record.expense_claim_code for record in included_reimbursements
        )
        reimbursements_by_claim: dict[str, list[DgcHesiReimbursementRecord]] = {}
        for record in scoped_reimbursements:
            if record.expense_claim_code in included_claim_codes:
                reimbursements_by_claim.setdefault(record.expense_claim_code, []).append(record)
        relevant_invoice_sources = tuple(
            record
            for record in invoice_sources
            if record.expense_claim_code in included_claim_codes
        )
        expense_type_by_id = self._infer_invoice_expense_types(
            relevant_invoice_sources,
            reimbursements_by_claim,
        )
        scoped_invoices_list: list[DgcHesiInvoiceRecord] = []
        for source_record in relevant_invoice_sources:
            resolved = self._resolve_invoice(
                source_record,
                reimbursements_by_claim,
                expense_type_by_id,
                period_start,
                period_end,
            )
            if resolved is not None:
                scoped_invoices_list.append(resolved)
        scoped_invoices = tuple(scoped_invoices_list)
        included_invoices = tuple(
            record
            for record in scoped_invoices
            if not record.excluded_expense_type
        )
        expense_types_by_claim_invoice: dict[tuple[str, str], set[str]] = {}
        for record in scoped_invoices:
            key = (record.expense_claim_code, record.invoice_id)
            expense_types_by_claim_invoice.setdefault(key, set()).add(
                record.expense_type_id
            )
        claim_level_aggregation_codes = tuple(
            sorted(
                {
                    claim_code
                    for (claim_code, _invoice_id), expense_type_ids in (
                        expense_types_by_claim_invoice.items()
                    )
                    if len(expense_type_ids) > 1
                }
            )
        )
        claim_level_aggregation_set = frozenset(claim_level_aggregation_codes)
        reimbursement_total = _exact_sum(
            tuple(record.expense_type_amount for record in included_reimbursements)
        )
        invoice_total = _exact_sum(
            tuple(record.invoice_approved_amount for record in included_invoices)
        )
        reimbursement_totals_by_group: dict[tuple[str, str | None], Decimal] = {}
        for record in included_reimbursements:
            key = (
                record.expense_claim_code,
                None
                if record.expense_claim_code in claim_level_aggregation_set
                else record.expense_type_code,
            )
            reimbursement_totals_by_group[key] = (
                reimbursement_totals_by_group.get(key, Decimal(0))
                + record.expense_type_amount
            )
        invoice_totals_by_group: dict[tuple[str, str | None], Decimal] = {}
        for invoice_record in included_invoices:
            key = (
                invoice_record.expense_claim_code,
                None
                if invoice_record.expense_claim_code in claim_level_aggregation_set
                else invoice_record.expense_type_code,
            )
            invoice_totals_by_group[key] = (
                invoice_totals_by_group.get(key, Decimal(0))
                + invoice_record.invoice_approved_amount
            )
        no_invoice_amount = _exact_sum(
            tuple(
                max(
                    reimbursement_amount - invoice_totals_by_group.get(key, Decimal(0)),
                    Decimal(0),
                )
                for key, reimbursement_amount in reimbursement_totals_by_group.items()
            )
        )
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
            invoice_records=scoped_invoices,
            reimbursement_expense_total=reimbursement_total,
            invoice_approved_total=invoice_total,
            hesi_no_invoice=no_invoice_amount,
            excluded_reimbursement_count=(
                len(scoped_reimbursements) - len(included_reimbursements)
            ),
            excluded_invoice_count=len(scoped_invoices) - len(included_invoices),
            source_checksum=source_checksum,
            reimbursement_duplicate_count=reimbursement_duplicate_count,
            invoice_duplicate_count=invoice_duplicate_count,
            claim_level_aggregation_codes=claim_level_aggregation_codes,
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
            source_fingerprint=_record_fingerprint(raw),
            source_row_number=row_number,
        )

    def _parse_invoice_source(
        self,
        raw: Mapping[str, object],
        row_number: int,
    ) -> _DgcHesiInvoiceSourceRecord:
        source = "hesi_invoice"
        mapping = self._invoice_field_map
        _validate_mapped_fields(
            raw,
            (
                mapping.company_code,
                mapping.expense_claim_code,
                mapping.invoice_id,
                mapping.expense_type_id,
                mapping.expense_line_amount,
                mapping.invoice_approved_amount,
            ),
            source,
            row_number,
        )
        company_code = _text(raw[mapping.company_code], mapping.company_code, source, row_number)
        _validate_company(company_code, self._company_code, source, row_number)
        return _DgcHesiInvoiceSourceRecord(
            company_code=company_code,
            expense_claim_code=_text(
                raw[mapping.expense_claim_code],
                mapping.expense_claim_code,
                source,
                row_number,
            ),
            invoice_id=_text(
                raw[mapping.invoice_id],
                mapping.invoice_id,
                source,
                row_number,
            ),
            expense_type_id=_text(
                raw[mapping.expense_type_id],
                mapping.expense_type_id,
                source,
                row_number,
            ),
            expense_line_amount=_amount(
                raw[mapping.expense_line_amount],
                mapping.expense_line_amount,
                source,
                row_number,
            ),
            invoice_approved_amount=_amount(
                raw[mapping.invoice_approved_amount],
                mapping.invoice_approved_amount,
                source,
                row_number,
            ),
            source_fingerprint=_record_fingerprint(raw),
            source_row_number=row_number,
        )

    def _infer_invoice_expense_types(
        self,
        invoices: tuple[_DgcHesiInvoiceSourceRecord, ...],
        reimbursements_by_claim: Mapping[str, list[DgcHesiReimbursementRecord]],
    ) -> dict[str, str]:
        expense_type_by_id: dict[str, str] = {}
        for invoice in invoices:
            reimbursements = self._matched_reimbursements(invoice, reimbursements_by_claim)
            matching_codes = {
                record.expense_type_code
                for record in reimbursements
                if record.expense_type_amount == invoice.expense_line_amount
            }
            if len(matching_codes) != 1:
                continue
            expense_type_code = next(iter(matching_codes))
            existing = expense_type_by_id.get(invoice.expense_type_id)
            if existing is not None and existing != expense_type_code:
                raise DgcHesiNoInvoiceError(
                    "CONFLICTING_INVOICE_EXPENSE_TYPE",
                    "invoice expense type id mapped to conflicting reimbursement fee type codes",
                    source="hesi_invoice",
                    row_number=invoice.source_row_number,
                    field=self._invoice_field_map.expense_type_id,
                )
            expense_type_by_id[invoice.expense_type_id] = expense_type_code
        return expense_type_by_id

    def _resolve_invoice(
        self,
        invoice: _DgcHesiInvoiceSourceRecord,
        reimbursements_by_claim: Mapping[str, list[DgcHesiReimbursementRecord]],
        expense_type_by_id: Mapping[str, str],
        period_start: date,
        period_end: date,
    ) -> DgcHesiInvoiceRecord | None:
        reimbursements = self._matched_reimbursements(invoice, reimbursements_by_claim)
        approval_dates = {
            record.approval_completed_on
            for record in reimbursements
            if record.approval_completed_on is not None
        }
        if not approval_dates:
            return None
        if len(approval_dates) != 1:
            raise DgcHesiNoInvoiceError(
                "AMBIGUOUS_INVOICE_APPROVAL_DATE",
                "invoice claim matched reimbursement rows with conflicting completion dates",
                source="hesi_invoice",
                row_number=invoice.source_row_number,
                field=self._reimbursement_field_map.approval_completed_at,
            )
        approval_date = next(iter(approval_dates))
        if not period_start <= approval_date <= period_end:
            return None

        matching_codes = {
            record.expense_type_code
            for record in reimbursements
            if record.expense_type_amount == invoice.expense_line_amount
        }
        expense_type_codes: tuple[str, ...]
        if len(matching_codes) == 1:
            expense_type_codes = (next(iter(matching_codes)),)
        else:
            inferred_code = expense_type_by_id.get(invoice.expense_type_id)
            if inferred_code is not None and (
                not matching_codes or inferred_code in matching_codes
            ):
                expense_type_codes = (inferred_code,)
            else:
                raise DgcHesiNoInvoiceError(
                    "UNRESOLVED_INVOICE_EXPENSE_TYPE",
                    "invoice fee type could not be matched safely to a reimbursement fee type code",
                    source="hesi_invoice",
                    row_number=invoice.source_row_number,
                    field=self._invoice_field_map.expense_type_id,
                )

        return DgcHesiInvoiceRecord(
            company_code=invoice.company_code,
            approval_completed_on=approval_date,
            expense_claim_code=invoice.expense_claim_code,
            invoice_id=invoice.invoice_id,
            expense_type_id=invoice.expense_type_id,
            expense_type_code=expense_type_codes[0],
            expense_type_candidates=expense_type_codes,
            excluded_expense_type=all(
                _is_excluded_expense_type_code(code)
                for code in expense_type_codes
            ),
            expense_line_amount=invoice.expense_line_amount,
            invoice_approved_amount=invoice.invoice_approved_amount,
            source_row_number=invoice.source_row_number,
        )

    def _matched_reimbursements(
        self,
        invoice: _DgcHesiInvoiceSourceRecord,
        reimbursements_by_claim: Mapping[str, list[DgcHesiReimbursementRecord]],
    ) -> list[DgcHesiReimbursementRecord]:
        matches = reimbursements_by_claim.get(invoice.expense_claim_code)
        if not matches:
            raise DgcHesiNoInvoiceError(
                "UNMATCHED_INVOICE_CLAIM",
                "invoice claim code did not match any reimbursement detail row",
                source="hesi_invoice",
                row_number=invoice.source_row_number,
                field=self._invoice_field_map.expense_claim_code,
            )
        return matches


def _deduplicate_reimbursements(
    records: tuple[DgcHesiReimbursementRecord, ...],
) -> tuple[tuple[DgcHesiReimbursementRecord, ...], int]:
    """Drop exact repeated reimbursement lines emitted by overlapping pages."""

    seen: set[tuple[object, ...]] = set()
    unique: list[DgcHesiReimbursementRecord] = []
    duplicate_count = 0
    for record in records:
        identity = record.source_fingerprint
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        unique.append(record)
    return tuple(unique), duplicate_count


def _deduplicate_invoice_sources(
    records: tuple[_DgcHesiInvoiceSourceRecord, ...],
) -> tuple[tuple[_DgcHesiInvoiceSourceRecord, ...], int]:
    """Drop only exact repeated invoice allocations from overlapping pages."""

    seen: set[str] = set()
    unique: list[_DgcHesiInvoiceSourceRecord] = []
    duplicate_count = 0
    for record in records:
        if record.source_fingerprint in seen:
            duplicate_count += 1
            continue
        seen.add(record.source_fingerprint)
        unique.append(record)
    return tuple(unique), duplicate_count


def _record_fingerprint(raw: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_fingerprint_default,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _is_excluded_expense_type_code(code: str) -> bool:
    return (
        code in HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES
        or code.startswith(HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_PREFIXES)
    )


def _fingerprint_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported fingerprint value {type(value).__name__}")


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
            "calculation_version": HESI_NO_INVOICE_CALCULATION_VERSION,
            "company_code": company_code,
            "excluded_expense_type_codes": sorted(
                HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES
            ),
            "excluded_expense_type_prefixes": sorted(
                HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_PREFIXES
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
    "HESI_NO_INVOICE_CALCULATION_VERSION",
    "HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_CODES",
    "HESI_NO_INVOICE_EXCLUDED_EXPENSE_TYPE_PREFIXES",
]
