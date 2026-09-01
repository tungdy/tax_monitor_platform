from __future__ import annotations

from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from hashlib import sha256
import inspect
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from starlette.requests import Request

from tax_risk.adapters.ingest.base import AdapterRow, CompanyMasterRow
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.api.routes.ingest import upload_ingest_file
from tax_risk.application.dgc_sap_dividend_detail import dgc_dividend_scope_sha256
from tax_risk.application.dgc_sap_profit import dgc_parameters_sha256
from tax_risk.application.dgc_sap_trial_balance import (
    dgc_trial_balance_scope_sha256,
)
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.ingest_models import SourceRecord
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import Principal


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_master_event_sequence = 0
GROUP_TAX_ADMIN = Principal(
    subject="group-tax-admin@example.com",
    roles=frozenset({"group-tax"}),
    allowed_company_ids=frozenset(),
    organization_path="/GROUP/TAX",
)


def _group_tax_admin(_request: Request) -> Principal:
    return GROUP_TAX_ADMIN


def _next_master_event_time() -> str:
    global _master_event_sequence
    _master_event_sequence += 1
    return (
        datetime(2200, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=_master_event_sequence)
    ).isoformat()


@pytest.fixture
def api_resources(
    isolated_database_url: str,
) -> Iterator[tuple[TestClient, Engine]]:
    database_engine, factory = create_session_factory(isolated_database_url)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
    )
    with TestClient(app) as client:
        yield client, database_engine
    database_engine.dispose()


def _metadata(
    dataset_code: str,
    *,
    source_batch_key: str | None = None,
    source: str = "TEST",
) -> dict[str, object]:
    return {
        "source": source,
        "source_batch_key": source_batch_key or f"{dataset_code}-{uuid4().hex}",
        "dataset_code": dataset_code,
        "extraction_time": "2026-04-01T08:00:00Z",
        "period": "2026-03-31",
        "mode": "FULL",
        "schema_version": "1",
        "currency": "CNY",
        "amount_scale": 2,
        "source_primary_key_definition": {"fields": ["source_record_key"]},
    }


def _create_batch(client: TestClient, metadata: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/v1/ingest-batches", json=metadata)
    assert response.status_code == 201, response.text
    return response.json()


def _upload_csv(
    client: TestClient,
    batch_id: str,
    payload: bytes,
    *,
    filename: str = "source.csv",
):
    return client.post(
        f"/api/v1/ingest-batches/{batch_id}/files",
        files={"file": (filename, payload, "text/csv")},
    )


def _company_master_payload(
    *,
    c001_lifecycle: str = "ACTIVE",
    c002_lifecycle: str = "ACTIVE",
    extracted_at: str = "2026-04-01T08:00:00+00:00",
) -> bytes:
    return (
        "source_record_key,company_code,company_name,lifecycle,extracted_at\n"
        f"company-C001,C001,Company One,{c001_lifecycle},{extracted_at}\n"
        f"company-C002,C002,Company Two,{c002_lifecycle},{extracted_at}\n"
    ).encode()


def _single_company_master_payload(
    company_code: str,
    company_name: str,
    lifecycle: str,
    extracted_at: str,
) -> bytes:
    return (
        "source_record_key,company_code,company_name,lifecycle,extracted_at\n"
        f"event-{uuid4().hex},{company_code},{company_name},{lifecycle},{extracted_at}\n"
    ).encode()


class _DgcSapProfitSource:
    def __init__(self) -> None:
        self.parameters: list[Mapping[str, object]] = []

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.parameters.append(dict(parameters))
        return DgcFetchResult(
            records=(
                {
                    "mandt": "100",
                    "bukrs": "C001",
                    "companyname": "Company One",
                    "gjahr": "2026",
                    "monat": "03",
                    "rldnr": "0L",
                    "hs": "10",
                    "ztext": "利润总额",
                    "nmhsl": "999.00",
                    "nyhsl": "100.00",
                },
                {
                    "mandt": "100",
                    "bukrs": "C001",
                    "companyname": "Company One",
                    "gjahr": "2026",
                    "monat": "03",
                    "rldnr": "0L",
                    "hs": "20",
                    "ztext": "公允价值变动收益",
                    "nmhsl": "999.00",
                    "nyhsl": "-2.50",
                },
                {
                    "mandt": "100",
                    "bukrs": "C001",
                    "companyname": "Company One",
                    "gjahr": "2026",
                    "monat": "03",
                    "rldnr": "0L",
                    "hs": "30",
                    "ztext": "营业收入",
                    "nmhsl": "999.00",
                    "nyhsl": "500.00",
                },
                {
                    "rldnr": "2L",
                    "ztext": "利润总额",
                    "nyhsl": "999999.00",
                },
                {
                    "rldnr": "0L",
                    "ztext": "管理费用",
                    "nyhsl": "888888.00",
                },
            ),
            checksum="d" * 64,
        )


class _DgcSapDividendDetailSource:
    def __init__(self) -> None:
        self.parameters: list[Mapping[str, object]] = []

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.parameters.append(dict(parameters))
        return DgcFetchResult(
            records=(
                _dgc_dividend_row(
                    voucher_no="q2-dividend",
                    fiscal_period="006",
                    header_text="收到子公司分红",
                    amount_ksl="-12.50",
                ),
                _dgc_dividend_row(
                    voucher_no="future-q3-dividend",
                    fiscal_period="007",
                    detail_text="收到股利",
                    amount_ksl="-20.00",
                ),
                _dgc_dividend_row(
                    voucher_no="not-a-dividend",
                    fiscal_period="006",
                    header_text="投资收益",
                    amount_ksl="-1000.00",
                ),
            ),
            checksum="e" * 64,
        )


class _DgcSapTrialBalanceSource:
    def __init__(self) -> None:
        self.parameters: list[Mapping[str, object]] = []

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.parameters.append(dict(parameters))
        return DgcFetchResult(
            records=(
                _dgc_trial_balance_row(
                    fiscal_period="003",
                    total_debit_amount="900000",
                ),
                _dgc_trial_balance_row(
                    fiscal_period="006",
                    total_debit_amount="750000",
                    total_credit_amount="-50000",
                ),
            ),
            checksum="f" * 64,
        )


def _dgc_trial_balance_row(**overrides: object) -> dict[str, object]:
    return {
        "company_code": "C001",
        "company_name": "Company One",
        "fiscal_year": "2026",
        "fiscal_period": "006",
        "gl_account_code": "6801010000",
        "gl_account_name": "所得税费用-当期所得税费用",
        "bank_center_code": "",
        "bank_account_number": "",
        "cost_center_code": "",
        "cost_center_name": "",
        "profit_center_code": "",
        "profit_center_name": "",
        "internal_order_code": "",
        "internal_order_name": "",
        "business_area_code": "",
        "business_area_name": "",
        "customer_code": "",
        "customer_name": "",
        "vendor_code": "",
        "vendor_name": "",
        "asset_code": "",
        "asset_name": "",
        "rstgr": "",
        "rstgr_name": "",
        "input_tax_process_method": "",
        "sfkf": "",
        "total_debit_amount": "0",
        "total_credit_amount": "0",
    } | overrides


def _dgc_dividend_row(**overrides: object) -> dict[str, object]:
    return {
        "fiscal_year": "2026",
        "fiscal_period": "006",
        "voucher_no": "default-voucher",
        "header_text": "",
        "detail_text": "",
        "amount_ksl": "0",
        "gl_account": "6111010000",
        "account_name": "投资收益",
        "project_code": "",
        "project_name": "",
        "debit_credit_flag": "H",
        "group_currency": "CNY",
        "original_system_doc_no": "",
    } | overrides


class _BarrierCompanyAdapter:
    def __init__(
        self,
        payload: bytes,
        barrier: Barrier,
        *,
        company_code: str,
        company_name: str,
        extracted_at: datetime,
    ) -> None:
        self._payload = payload
        self._barrier = barrier
        self._company_code = company_code
        self._company_name = company_name
        self._extracted_at = extracted_at

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        self._barrier.wait(timeout=5)
        yield AdapterRow(
            row_number=2,
            value=CompanyMasterRow(
                source_record_key=f"event-{self._company_name}",
                company_code=self._company_code,
                company_name=self._company_name,
                lifecycle="ACTIVE",
                extracted_at=self._extracted_at,
            ),
            error=None,
        )


class _ReverseTwoCompanyAdapter:
    def __init__(
        self,
        payload: bytes,
        barrier: Barrier,
        company_codes: tuple[str, str],
    ) -> None:
        self._payload = payload
        self._barrier = barrier
        self._company_codes = company_codes

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        for index, company_code in enumerate(self._company_codes, start=2):
            if index == 3:
                self._barrier.wait(timeout=5)
            yield AdapterRow(
                row_number=index,
                value=CompanyMasterRow(
                    source_record_key=f"{self._payload.decode()}-{company_code}",
                    company_code=company_code,
                    company_name=f"Company {company_code}",
                    lifecycle="ACTIVE",
                    extracted_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
                ),
                error=None,
            )


class _ExplodingAdapter:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        raise RuntimeError("secret database details must not escape")
        yield  # pragma: no cover


class _HoldingEmptyAdapter:
    def __init__(self, payload: bytes, entered: Event, release: Event) -> None:
        self._payload = payload
        self._entered = entered
        self._release = release

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        if self._payload == b"hold":
            self._entered.set()
            assert self._release.wait(timeout=10)
        return
        yield  # pragma: no cover


class _FailOnEnterUow:
    def __enter__(self) -> None:
        raise RuntimeError("secret audit storage failure")

    def __exit__(self, *args: object) -> None:
        return None


class _AuditFailureUowFactory:
    def __init__(self, factory: object) -> None:
        self._factory = factory
        self._calls = 0

    def __call__(self) -> object:
        self._calls += 1
        # Batch creation and its HTTP audit run before the upload. The upload
        # then resolves the batch and enters its processing transaction.
        if self._calls == 5:
            return _FailOnEnterUow()
        return UnitOfWork(self._factory)  # type: ignore[arg-type]


class _PausingFinancialUow(UnitOfWork):
    def __init__(self, factory: object, locked: Event, release: Event) -> None:
        super().__init__(factory)  # type: ignore[arg-type]
        self._locked = locked
        self._release = release

    def commit(self) -> None:
        if any(isinstance(instance, SourceRecord) for instance in self.session.new):
            self._locked.set()
            assert self._release.wait(timeout=10)
        super().commit()


class _SignalingCompanyAdapter:
    def __init__(
        self,
        payload: bytes,
        started: Event,
        *,
        company_code: str,
        extracted_at: datetime,
    ) -> None:
        self._payload = payload
        self._started = started
        self._company_code = company_code
        self._extracted_at = extracted_at

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    def validate_header(self) -> None:
        return None

    def iter_rows(self) -> Iterator[AdapterRow]:
        self._started.set()
        yield AdapterRow(
            row_number=2,
            value=CompanyMasterRow(
                source_record_key=f"deactivate-{self._company_code}",
                company_code=self._company_code,
                company_name=f"Company {self._company_code}",
                lifecycle="INACTIVE",
                extracted_at=self._extracted_at,
            ),
            error=None,
        )


def _seed_active_companies(client: TestClient) -> dict[str, object]:
    batch = _create_batch(client, _metadata("company_master", source="COMPANY_REGISTRY"))
    response = _upload_csv(
        client,
        str(batch["id"]),
        _company_master_payload(extracted_at=_next_master_event_time()),
        filename="company-master.csv",
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUCCEEDED"
    return response.json()


def test_company_master_is_the_controlled_company_create_update_and_deactivate_path(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    first = _seed_active_companies(client)

    deactivation_time = _next_master_event_time()
    second_batch = _create_batch(
        client,
        _metadata("company_master", source="COMPANY_REGISTRY"),
    )
    response = _upload_csv(
        client,
        str(second_batch["id"]),
        _company_master_payload(
            c002_lifecycle="INACTIVE",
            extracted_at=deactivation_time,
        ),
        filename="company-master-deactivate.csv",
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUCCEEDED"
    with database_engine.connect() as connection:
        companies = (
            connection.execute(
                text(
                    """
                SELECT company_code, company_name, lifecycle, lifecycle_changed_at,
                       deactivated_at, lifecycle_reason, lifecycle_changed_by
                FROM company
                WHERE company_code IN ('C001', 'C002')
                ORDER BY company_code
                """
                )
            )
            .mappings()
            .all()
        )
        company_master_source_records = connection.execute(
            text(
                """
                SELECT count(*)
                FROM source_record
                WHERE batch_id IN (:first_batch_id, :second_batch_id)
                """
            ),
            {
                "first_batch_id": first["id"],
                "second_batch_id": response.json()["id"],
            },
        ).scalar_one()

    assert len(companies) == 2
    assert companies[0]["lifecycle"] == "ACTIVE"
    assert companies[0]["deactivated_at"] is None
    assert companies[1]["lifecycle"] == "INACTIVE"
    expected_change = datetime.fromisoformat(deactivation_time)
    assert companies[1]["lifecycle_changed_at"] == expected_change
    assert companies[1]["deactivated_at"] == expected_change
    assert companies[1]["lifecycle_reason"] == "company_master_import"
    assert companies[1]["lifecycle_changed_by"] == "COMPANY_REGISTRY"
    assert company_master_source_records == 0


def test_batch_metadata_creation_is_idempotent_and_conflicts_are_stable(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    key = f"idempotent-{uuid4().hex}"
    metadata = _metadata("quarterly_metric", source_batch_key=key, source="SAP")

    first = client.post("/api/v1/ingest-batches", json=metadata)
    replay = client.post("/api/v1/ingest-batches", json=metadata)
    conflicting = client.post(
        "/api/v1/ingest-batches",
        json=metadata | {"schema_version": "2"},
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "IDEMPOTENCY_METADATA_CONFLICT"
    with database_engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM ingest_batch WHERE source = 'SAP' AND source_batch_key = :key"
            ),
            {"key": key},
        ).scalar_one()
    assert count == 1


def test_valid_financial_file_succeeds_and_persists_canonical_controls(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    _seed_active_companies(client)
    payload = (FIXTURES / "sap_quarterly_valid.csv").read_bytes()
    batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))

    uploaded = _upload_csv(
        client,
        str(batch["id"]),
        payload,
        filename="sap-quarterly.csv",
    )
    fetched = client.get(f"/api/v1/ingest-batches/{batch['id']}")

    assert uploaded.status_code == 200, uploaded.text
    assert fetched.status_code == 200
    result = fetched.json()
    assert result["status"] == "SUCCEEDED"
    assert result["record_count"] == 3
    assert result["accepted_count"] == 3
    assert result["rejected_count"] == 0
    assert Decimal(result["control_total"]) == Decimal("99750.25")
    assert result["checksum"] == sha256(payload).hexdigest()
    assert result["schema_version"] == "1"
    assert result["errors"] == []
    with database_engine.connect() as connection:
        records = (
            connection.execute(
                text(
                    """
                SELECT source_record_key, amount, payload, lineage
                FROM source_record
                WHERE batch_id = :batch_id
                ORDER BY source_record_key
                """
                ),
                {"batch_id": batch["id"]},
            )
            .mappings()
            .all()
        )
    assert len(records) == 3
    assert records[1]["amount"] == Decimal("-500.000000000000")
    assert records[1]["payload"]["metric_code"] == "received_dividends"
    assert records[1]["lineage"]["row_number"] == 3


def test_dgc_sap_profit_endpoint_reuses_controlled_financial_ingest(
    isolated_database_url: str,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    source = _DgcSapProfitSource()
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
        dgc_sap_profit_source=source,
    )
    request_body = {
        "source_batch_key": f"sap-profit-{uuid4().hex}",
        "extraction_time": "2026-04-01T08:00:00Z",
        "gjahr": "2026",
        "monat": "03",
        "bukrs": "C001",
        "currency": "CNY",
        "amount_scale": 2,
    }

    try:
        with TestClient(app) as client:
            _seed_active_companies(client)
            response = client.post(
                "/api/v1/ingest-batches/dgc-sap-profit",
                json=request_body,
            )
            replay = client.post(
                "/api/v1/ingest-batches/dgc-sap-profit",
                json=request_body,
            )
            conflicting_query = client.post(
                "/api/v1/ingest-batches/dgc-sap-profit",
                json=request_body | {"bukrs": "C002"},
            )

        assert response.status_code == 201, response.text
        assert replay.status_code == 200, replay.text
        assert conflicting_query.status_code == 409, conflicting_query.text
        assert conflicting_query.json()["detail"]["code"] == ("IDEMPOTENCY_METADATA_CONFLICT")
        result = response.json()
        assert replay.json()["id"] == result["id"]
        assert result["source"] == "DGC_SAP"
        assert result["dataset_code"] == "quarterly_metric"
        assert result["period"] == "2026-03-31"
        assert result["status"] == "SUCCEEDED"
        assert result["record_count"] == 3
        assert result["accepted_count"] == 3
        assert result["rejected_count"] == 0
        assert result["checksum"] == "d" * 64
        parameters_sha256 = dgc_parameters_sha256({"gjahr": "2026", "monat": "03", "bukrs": "C001"})
        assert result["source_primary_key_definition"] == {
            "fields": ["source_record_key"],
            "dgc_parameters_sha256": parameters_sha256,
        }
        assert result["payload_ref"] == (f"dgc://sap-income?query_sha256={parameters_sha256}")
        assert source.parameters == [
            {"gjahr": "2026", "monat": "03", "bukrs": "C001"},
            {"gjahr": "2026", "monat": "03", "bukrs": "C001"},
            {"gjahr": "2026", "monat": "03", "bukrs": "C002"},
        ]

        with database_engine.connect() as connection:
            records = (
                connection.execute(
                    text(
                        """
                        SELECT source_record_key, amount, payload ->> 'metric_code' AS metric_code
                        FROM source_record
                        WHERE batch_id = :batch_id
                        ORDER BY metric_code
                        """
                    ),
                    {"batch_id": result["id"]},
                )
                .mappings()
                .all()
            )

        assert [(row["metric_code"], row["amount"]) for row in records] == [
            ("cumulative_profit", Decimal("100.00")),
            ("cumulative_revenue", Decimal("500.00")),
            ("fair_value_change", Decimal("-2.50")),
        ]
        assert all(row["source_record_key"].startswith("dgc-sap-profit:") for row in records)
    finally:
        database_engine.dispose()


def test_dgc_sap_dividend_endpoint_reuses_controlled_quarterly_ingest(
    isolated_database_url: str,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    source = _DgcSapDividendDetailSource()
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
        dgc_sap_dividend_detail_source=source,
    )
    request_body = {
        "source_batch_key": f"sap-dividend-{uuid4().hex}",
        "extraction_time": "2026-07-01T08:00:00Z",
        "company": "C001",
        "fiscal_year": "2026",
        "through_period": "006",
        "currency": "CNY",
        "amount_scale": 2,
    }

    try:
        with TestClient(app) as client:
            _seed_active_companies(client)
            response = client.post(
                "/api/v1/ingest-batches/dgc-sap-dividend-detail",
                json=request_body,
            )
            replay = client.post(
                "/api/v1/ingest-batches/dgc-sap-dividend-detail",
                json=request_body,
            )
            conflicting_scope = client.post(
                "/api/v1/ingest-batches/dgc-sap-dividend-detail",
                json=request_body | {"through_period": 9},
            )

        assert response.status_code == 201, response.text
        assert replay.status_code == 200, replay.text
        assert conflicting_scope.status_code == 409, conflicting_scope.text
        assert conflicting_scope.json()["detail"]["code"] == (
            "IDEMPOTENCY_METADATA_CONFLICT"
        )
        result = response.json()
        assert replay.json()["id"] == result["id"]
        assert result["source"] == "DGC_SAP_DIVIDEND"
        assert result["dataset_code"] == "quarterly_metric"
        assert result["period"] == "2026-06-30"
        assert result["status"] == "SUCCEEDED"
        assert result["record_count"] == 1
        assert result["accepted_count"] == 1
        assert result["rejected_count"] == 0
        assert result["checksum"] == "e" * 64
        scope = {"company": "C001", "fiscal_year": "2026", "through_period": 6}
        scope_sha256 = dgc_dividend_scope_sha256(scope)
        assert result["source_primary_key_definition"] == {
            "fields": ["source_record_key"],
            "dgc_dividend_scope_sha256": scope_sha256,
        }
        assert result["payload_ref"] == (
            f"dgc://sap-dividend-detail?scope_sha256={scope_sha256}"
        )
        assert source.parameters == [
            {"company": "C001", "fiscal_year": "2026"},
            {"company": "C001", "fiscal_year": "2026"},
            {"company": "C001", "fiscal_year": "2026"},
        ]

        with database_engine.connect() as connection:
            records = (
                connection.execute(
                    text(
                        """
                        SELECT source_record_key, amount,
                               payload ->> 'metric_code' AS metric_code
                        FROM source_record
                        WHERE batch_id = :batch_id
                        """
                    ),
                    {"batch_id": result["id"]},
                )
                .mappings()
                .all()
            )

        assert len(records) == 1
        assert records[0]["metric_code"] == "received_dividends"
        assert records[0]["amount"] == Decimal("12.500000000000")
        assert records[0]["source_record_key"].startswith(
            "dgc-sap-dividend-detail:"
        )
    finally:
        database_engine.dispose()


def test_dgc_sap_trial_balance_endpoint_reuses_controlled_quarterly_ingest(
    isolated_database_url: str,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    source = _DgcSapTrialBalanceSource()
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
        dgc_sap_trial_balance_source=source,
    )
    request_body = {
        "source_batch_key": f"sap-trial-balance-{uuid4().hex}",
        "extraction_time": "2026-07-01T08:00:00Z",
        "company_code": "C001",
        "fiscal_year": "2026",
        "through_period": "006",
        "currency": "CNY",
        "amount_scale": 2,
    }

    try:
        with TestClient(app) as client:
            _seed_active_companies(client)
            response = client.post(
                "/api/v1/ingest-batches/dgc-sap-trial-balance",
                json=request_body,
            )
            replay = client.post(
                "/api/v1/ingest-batches/dgc-sap-trial-balance",
                json=request_body,
            )
            conflicting_scope = client.post(
                "/api/v1/ingest-batches/dgc-sap-trial-balance",
                json=request_body | {"through_period": 9},
            )

        assert response.status_code == 201, response.text
        assert replay.status_code == 200, replay.text
        assert conflicting_scope.status_code == 409, conflicting_scope.text
        assert conflicting_scope.json()["detail"]["code"] == (
            "IDEMPOTENCY_METADATA_CONFLICT"
        )
        result = response.json()
        assert replay.json()["id"] == result["id"]
        assert result["source"] == "DGC_SAP_TRIAL_BALANCE"
        assert result["dataset_code"] == "quarterly_metric"
        assert result["period"] == "2026-06-30"
        assert result["status"] == "SUCCEEDED"
        assert result["record_count"] == 2
        assert result["accepted_count"] == 2
        assert result["rejected_count"] == 0
        assert result["checksum"] == "f" * 64
        parameters = {
            "company_code": "C001",
            "fiscal_year": "2026",
            "gl_account_code": "6801010000",
        }
        scope_sha256 = dgc_trial_balance_scope_sha256(
            {**parameters, "through_period": 6}
        )
        assert result["source_primary_key_definition"] == {
            "fields": ["source_record_key"],
            "dgc_trial_balance_scope_sha256": scope_sha256,
        }
        assert result["payload_ref"] == (
            f"dgc://sap-trial-balance?scope_sha256={scope_sha256}"
        )
        assert source.parameters == [parameters, parameters, parameters]

        with database_engine.connect() as connection:
            records = (
                connection.execute(
                    text(
                        """
                        SELECT source_record_key, amount,
                               payload ->> 'metric_code' AS metric_code
                        FROM source_record
                        WHERE batch_id = :batch_id
                        ORDER BY metric_code
                        """
                    ),
                    {"batch_id": result["id"]},
                )
                .mappings()
                .all()
            )

        assert [(row["metric_code"], row["amount"]) for row in records] == [
            ("current_quarter_current_tax", Decimal("700000.000000000000")),
            ("prior_quarter_current_tax", Decimal("900000.000000000000")),
        ]
        assert all(
            row["source_record_key"].startswith("dgc-sap-trial-balance:")
            for row in records
        )
    finally:
        database_engine.dispose()


def test_dgc_sap_trial_balance_endpoint_is_unavailable_when_not_configured(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _database_engine = api_resources

    response = client.post(
        "/api/v1/ingest-batches/dgc-sap-trial-balance",
        json={
            "source_batch_key": "disabled-trial-balance-source",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company_code": "C001",
            "fiscal_year": "2026",
            "through_period": 6,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DGC_SAP_TRIAL_BALANCE_DISABLED"


def test_dgc_sap_dividend_endpoint_is_unavailable_when_not_configured(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _database_engine = api_resources

    response = client.post(
        "/api/v1/ingest-batches/dgc-sap-dividend-detail",
        json={
            "source_batch_key": "disabled-dividend-source",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company": "C001",
            "fiscal_year": "2026",
            "through_period": 6,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "DGC_SAP_DIVIDEND_DETAIL_DISABLED"
    )


def test_partial_file_reports_exact_rows_and_excludes_invalid_amount_from_total(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    _seed_active_companies(client)
    payload = (FIXTURES / "sap_quarterly_invalid.csv").read_bytes()
    batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))

    response = _upload_csv(client, str(batch["id"]), payload)
    replay = _upload_csv(client, str(batch["id"]), payload)

    assert response.status_code == 200, response.text
    assert replay.status_code == 200
    assert replay.json() == response.json()
    result = response.json()
    assert result["status"] == "PARTIAL"
    assert result["record_count"] == 4
    assert result["accepted_count"] == 2
    assert result["rejected_count"] == 2
    assert Decimal(result["control_total"]) == Decimal("74.75")
    assert [error["row_number"] for error in result["errors"]] == [3, 4]
    assert [error["error_code"] for error in result["errors"]] == [
        "UNKNOWN_COMPANY",
        "INVALID_DECIMAL",
    ]
    assert [error["details"] for error in result["errors"]] == [
        {
            "company_code": "C999",
            "metric_code": "cumulative_revenue",
            "field": "company_code",
            "rejected_value": "C999",
        },
        {
            "company_code": "C001",
            "metric_code": "fair_value_change",
            "field": "amount",
            "rejected_value": "not-a-decimal",
        },
    ]
    with database_engine.connect() as connection:
        source_keys = (
            connection.execute(
                text(
                    "SELECT source_record_key FROM source_record "
                    "WHERE batch_id = :batch_id ORDER BY source_record_key"
                ),
                {"batch_id": batch["id"]},
            )
            .scalars()
            .all()
        )
        error_count = connection.execute(
            text("SELECT count(*) FROM ingest_error WHERE batch_id = :batch_id"),
            {"batch_id": batch["id"]},
        ).scalar_one()
    assert source_keys == ["sap-bad-001", "sap-bad-004"]
    assert error_count == 2


def test_inactive_company_financial_row_is_rejected_without_auto_reactivation(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    _seed_active_companies(client)
    master_batch = _create_batch(
        client,
        _metadata("company_master", source="COMPANY_REGISTRY"),
    )
    deactivated = _upload_csv(
        client,
        str(master_batch["id"]),
        _company_master_payload(
            c002_lifecycle="INACTIVE",
            extracted_at=_next_master_event_time(),
        ),
    )
    assert deactivated.status_code == 200
    financial_batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))

    response = _upload_csv(
        client,
        str(financial_batch["id"]),
        (FIXTURES / "sap_quarterly_valid.csv").read_bytes(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "PARTIAL"
    assert result["accepted_count"] == 2
    assert result["rejected_count"] == 1
    assert result["errors"][0]["row_number"] == 4
    assert result["errors"][0]["error_code"] == "INACTIVE_COMPANY"
    with database_engine.connect() as connection:
        lifecycle = connection.execute(
            text("SELECT lifecycle FROM company WHERE company_code = 'C002'")
        ).scalar_one()
    assert lifecycle == "INACTIVE"


def test_identical_file_retry_returns_original_and_different_terminal_file_conflicts(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    _seed_active_companies(client)
    payload = (FIXTURES / "sap_quarterly_valid.csv").read_bytes()
    batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))

    first = _upload_csv(client, str(batch["id"]), payload)
    replay = _upload_csv(client, str(batch["id"]), payload)
    conflict = _upload_csv(client, str(batch["id"]), payload + b"\n")

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "TERMINAL_BATCH_FILE_CONFLICT"
    with database_engine.connect() as connection:
        record_count = connection.execute(
            text("SELECT count(*) FROM source_record WHERE batch_id = :batch_id"),
            {"batch_id": batch["id"]},
        ).scalar_one()
        error_count = connection.execute(
            text("SELECT count(*) FROM ingest_error WHERE batch_id = :batch_id"),
            {"batch_id": batch["id"]},
        ).scalar_one()
    assert record_count == 3
    assert error_count == 0


def test_zero_accepted_rows_fail_and_header_and_not_found_errors_are_stable(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _ = api_resources
    _seed_active_companies(client)
    failed_payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "none-1,UNKNOWN,2026,2026-03-31,CNY,2,cumulative_profit,10.00,"
        "2026-04-01T08:00:00+00:00\n"
        "none-2,C001,2026,2026-03-31,CNY,2,cumulative_profit,bad,"
        "2026-04-01T08:00:00+00:00\n"
    ).encode()
    failed_batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))
    failed = _upload_csv(client, str(failed_batch["id"]), failed_payload)
    header_batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))
    invalid_header = _upload_csv(
        client,
        str(header_batch["id"]),
        b"company_code,amount\nC001,10.00\n",
    )
    missing_id = str(uuid4())

    assert failed.status_code == 200
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["accepted_count"] == 0
    assert Decimal(failed.json()["control_total"]) == Decimal("0")
    assert invalid_header.status_code == 422
    assert invalid_header.json()["detail"]["code"] == "INVALID_HEADER"
    header_status = client.get(f"/api/v1/ingest-batches/{header_batch['id']}")
    assert header_status.status_code == 200
    assert header_status.json()["status"] == "FAILED"
    assert client.get(f"/api/v1/ingest-batches/{missing_id}").status_code == 404
    missing_upload = _upload_csv(client, missing_id, b"anything")
    assert missing_upload.status_code == 404


def test_invalid_csv_encoding_is_a_stable_422_and_failed_batch(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _ = api_resources
    batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))

    response = _upload_csv(client, str(batch["id"]), b"\xff\xfe\x00")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_ENCODING"
    fetched = client.get(f"/api/v1/ingest-batches/{batch['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "FAILED"
    assert fetched.json()["checksum"] == sha256(b"\xff\xfe\x00").hexdigest()


def test_control_total_preserves_all_38_database_digits_without_context_rounding(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _ = api_resources
    _seed_active_companies(client)
    metadata = _metadata("quarterly_metric", source="SAP") | {"amount_scale": 12}
    batch = _create_batch(client, metadata)
    payload = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        "precise-1,C001,2026,2026-03-31,CNY,12,cumulative_profit,"
        "12345678901234567890123456.123456789012,2026-04-01T08:00:00+00:00\n"
        "precise-2,C001,2026,2026-03-31,CNY,12,received_dividends,"
        "0.000000000001,2026-04-01T08:00:00+00:00\n"
    ).encode()

    response = _upload_csv(client, str(batch["id"]), payload)

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert Decimal(response.json()["control_total"]) == Decimal(
        "12345678901234567890123456.123456789013"
    )


@pytest.mark.parametrize(
    ("dataset_code", "payload"),
    [
        (
            "quarterly_metric",
            (
                "source_record_key,company_code,fiscal_year,period,currency,"
                "amount_scale,metric_code,amount,extracted_at\n"
            ).encode(),
        ),
        (
            "company_master",
            ("source_record_key,company_code,company_name,lifecycle,extracted_at\n").encode(),
        ),
    ],
)
def test_header_only_file_fails_without_creating_canonical_side_effects(
    api_resources: tuple[TestClient, Engine],
    dataset_code: str,
    payload: bytes,
) -> None:
    client, database_engine = api_resources
    with database_engine.connect() as connection:
        companies_before = connection.execute(text("SELECT count(*) FROM company")).scalar_one()
    batch = _create_batch(client, _metadata(dataset_code, source="EMPTY_FILE_TEST"))

    response = _upload_csv(client, str(batch["id"]), payload, filename="empty.csv")

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "FAILED"
    assert result["record_count"] == 0
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 0
    assert Decimal(result["control_total"]) == Decimal("0")
    assert result["checksum"] == sha256(payload).hexdigest()
    assert [error["error_code"] for error in result["errors"]] == ["EMPTY_FILE"]
    with database_engine.connect() as connection:
        canonical_counts = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM source_record WHERE batch_id = :batch_id),
                    (SELECT count(*) FROM ingest_error WHERE batch_id = :batch_id),
                    (SELECT count(*) FROM company)
                """
            ),
            {"batch_id": batch["id"]},
        ).one()
    assert canonical_counts == (0, 1, companies_before)


def test_newer_company_master_event_wins_and_stale_event_cannot_roll_state_back(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    company_code = f"EVENT-{uuid4().hex}"

    initial_batch = _create_batch(
        client,
        _metadata("company_master", source="COMPANY_REGISTRY"),
    )
    initial = _upload_csv(
        client,
        str(initial_batch["id"]),
        _single_company_master_payload(
            company_code,
            "Initial Name",
            "ACTIVE",
            "2026-04-01T08:00:00+00:00",
        ),
    )
    newer_batch = _create_batch(
        client,
        _metadata("company_master", source="COMPANY_REGISTRY"),
    )
    newer = _upload_csv(
        client,
        str(newer_batch["id"]),
        _single_company_master_payload(
            company_code,
            "New Name",
            "ACTIVE",
            "2026-04-03T08:00:00+00:00",
        ),
    )
    stale_batch = _create_batch(
        client,
        _metadata("company_master", source="COMPANY_REGISTRY"),
    )
    stale = _upload_csv(
        client,
        str(stale_batch["id"]),
        _single_company_master_payload(
            company_code,
            "Old Name",
            "INACTIVE",
            "2026-04-02T08:00:00+00:00",
        ),
    )

    assert initial.status_code == 200
    assert newer.status_code == 200
    assert newer.json()["status"] == "SUCCEEDED"
    assert stale.status_code == 200
    assert stale.json()["status"] == "FAILED"
    assert stale.json()["errors"][0]["error_code"] == "STALE_COMPANY_MASTER_EVENT"
    with database_engine.connect() as connection:
        company = (
            connection.execute(
                text(
                    """
                SELECT company_name, lifecycle, master_data_updated_at,
                       lifecycle_changed_at, deactivated_at
                FROM company WHERE company_code = :company_code
                """
                ),
                {"company_code": company_code},
            )
            .mappings()
            .one()
        )
    assert company["company_name"] == "New Name"
    assert company["lifecycle"] == "ACTIVE"
    assert company["master_data_updated_at"] == datetime(2026, 4, 3, 8, tzinfo=timezone.utc)
    assert company["lifecycle_changed_at"] == datetime(2026, 4, 1, 8, tzinfo=timezone.utc)
    assert company["deactivated_at"] is None


def test_equal_company_master_event_is_idempotent_but_conflicting_payload_fails(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    company_code = f"EQUAL-{uuid4().hex}"
    event_time = "2026-04-05T08:00:00+00:00"

    responses = []
    for company_name in ("Stable Name", "Stable Name", "Conflicting Name"):
        batch = _create_batch(
            client,
            _metadata("company_master", source="COMPANY_REGISTRY"),
        )
        responses.append(
            _upload_csv(
                client,
                str(batch["id"]),
                _single_company_master_payload(
                    company_code,
                    company_name,
                    "ACTIVE",
                    event_time,
                ),
            )
        )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.json()["status"] for response in responses] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]
    assert responses[2].json()["errors"][0]["error_code"] == ("COMPANY_MASTER_EVENT_CONFLICT")
    with database_engine.connect() as connection:
        persisted = connection.execute(
            text(
                "SELECT company_name, master_data_updated_at FROM company "
                "WHERE company_code = :company_code"
            ),
            {"company_code": company_code},
        ).one()
    assert persisted == (
        "Stable Name",
        datetime(2026, 4, 5, 8, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("conflicting_names", [False, True])
def test_concurrent_first_company_imports_serialize_without_duplicate_or_500(
    isolated_database_url: str,
    conflicting_names: bool,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    barrier = Barrier(2)
    company_code = f"RACE-{uuid4().hex}"
    event_time = datetime(2026, 4, 6, 8, tzinfo=timezone.utc)

    def adapter_factory(payload: bytes, dataset_code: str) -> _BarrierCompanyAdapter:
        assert dataset_code == "company_master"
        marker = payload.decode()
        company_name = marker if conflicting_names else "Same Name"
        return _BarrierCompanyAdapter(
            payload,
            barrier,
            company_code=company_code,
            company_name=company_name,
            extracted_at=event_time,
        )

    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        adapter_factory=adapter_factory,
        principal_provider=_group_tax_admin,
    )
    try:
        with TestClient(app) as first_client, TestClient(app) as second_client:
            first_batch = _create_batch(
                first_client,
                _metadata("company_master", source="RACE_SOURCE_A"),
            )
            second_batch = _create_batch(
                second_client,
                _metadata("company_master", source="RACE_SOURCE_B"),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _upload_csv,
                        first_client,
                        str(first_batch["id"]),
                        b"First Name",
                    ),
                    executor.submit(
                        _upload_csv,
                        second_client,
                        str(second_batch["id"]),
                        b"Second Name",
                    ),
                ]
                responses = [future.result(timeout=10) for future in futures]
        with database_engine.connect() as connection:
            company_count = connection.execute(
                text("SELECT count(*) FROM company WHERE company_code = :company_code"),
                {"company_code": company_code},
            ).scalar_one()
    finally:
        database_engine.dispose()

    assert [response.status_code for response in responses] == [200, 200]
    statuses = [response.json()["status"] for response in responses]
    if conflicting_names:
        assert sorted(statuses) == ["FAILED", "SUCCEEDED"]
        failed = next(response for response in responses if response.json()["status"] == "FAILED")
        assert failed.json()["errors"][0]["error_code"] == ("COMPANY_MASTER_EVENT_CONFLICT")
    else:
        assert statuses == ["SUCCEEDED", "SUCCEEDED"]
    assert company_count == 1


@pytest.mark.parametrize("round_number", range(2))
def test_reverse_multi_company_imports_use_one_lock_order_without_deadlock(
    isolated_database_url: str,
    round_number: int,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    barrier = Barrier(2)
    codes = (
        f"LOCK-{round_number}-A-{uuid4().hex}",
        f"LOCK-{round_number}-B-{uuid4().hex}",
    )

    def adapter_factory(payload: bytes, dataset_code: str) -> _ReverseTwoCompanyAdapter:
        assert dataset_code == "company_master"
        order = codes if payload == b"AB" else tuple(reversed(codes))
        return _ReverseTwoCompanyAdapter(payload, barrier, order)  # type: ignore[arg-type]

    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        adapter_factory=adapter_factory,
        principal_provider=_group_tax_admin,
    )
    try:
        with TestClient(app) as first_client, TestClient(app) as second_client:
            first_batch = _create_batch(first_client, _metadata("company_master"))
            second_batch = _create_batch(second_client, _metadata("company_master"))
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _upload_csv,
                        first_client,
                        str(first_batch["id"]),
                        b"AB",
                    ),
                    executor.submit(
                        _upload_csv,
                        second_client,
                        str(second_batch["id"]),
                        b"BA",
                    ),
                ]
                responses = [future.result(timeout=15) for future in futures]
        with database_engine.connect() as connection:
            company_count = connection.execute(
                text(
                    "SELECT count(*) FROM company "
                    "WHERE company_code = :first_code OR company_code = :second_code"
                ),
                {"first_code": codes[0], "second_code": codes[1]},
            ).scalar_one()
    finally:
        database_engine.dispose()

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["status"] for response in responses] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    assert company_count == 2


def test_upload_endpoint_is_sync_and_oversize_file_leaves_batch_retryable_received(
    isolated_database_url: str,
) -> None:
    assert inspect.iscoroutinefunction(upload_ingest_file) is False
    database_engine, factory = create_session_factory(isolated_database_url)
    settings = Settings(ingest_max_upload_bytes=32)
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=settings,
        principal_provider=_group_tax_admin,
    )
    try:
        with TestClient(app) as client:
            batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))
            response = _upload_csv(client, str(batch["id"]), b"x" * 33)
            fetched = client.get(f"/api/v1/ingest-batches/{batch['id']}")
    finally:
        database_engine.dispose()

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "RECEIVED"
    assert fetched.json()["checksum"] == "0" * 64
    assert fetched.json()["payload_ref"] is None
    assert fetched.json()["errors"] == []


def test_unexpected_adapter_failure_is_audited_without_leaking_internal_details(
    isolated_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)

    def adapter_factory(payload: bytes, dataset_code: str) -> _ExplodingAdapter:
        assert dataset_code == "quarterly_metric"
        return _ExplodingAdapter(payload)

    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        adapter_factory=adapter_factory,
        principal_provider=_group_tax_admin,
    )
    payload = b"exploding-adapter-payload"
    try:
        with TestClient(app) as client:
            batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))
            with caplog.at_level("ERROR", logger="tax_risk.application.ingest"):
                response = _upload_csv(
                    client,
                    str(batch["id"]),
                    payload,
                    filename="exploding.csv",
                )
            fetched = client.get(f"/api/v1/ingest-batches/{batch['id']}")
        with database_engine.connect() as connection:
            source_count = connection.execute(
                text("SELECT count(*) FROM source_record WHERE batch_id = :batch_id"),
                {"batch_id": batch["id"]},
            ).scalar_one()
    finally:
        database_engine.dispose()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "INGEST_PROCESSING_FAILED"
    assert "secret database" not in response.text
    assert fetched.status_code == 200
    result = fetched.json()
    assert result["status"] == "FAILED"
    assert result["checksum"] == sha256(payload).hexdigest()
    assert result["payload_ref"] == "exploding.csv"
    assert result["record_count"] == 0
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["row_number"] == 1
    assert result["errors"][0]["error_code"] == "INGEST_PROCESSING_FAILED"
    assert result["errors"][0]["retryable"] is False
    assert source_count == 0
    processing_logs = [
        record for record in caplog.records if record.message == "ingest_processing_failed"
    ]
    assert len(processing_logs) == 1
    assert processing_logs[0].batch_id == str(batch["id"])
    assert "exploding-adapter-payload" not in processing_logs[0].getMessage()


def test_audit_write_failure_is_logged_but_client_error_remains_generic(
    isolated_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)

    def adapter_factory(payload: bytes, dataset_code: str) -> _ExplodingAdapter:
        assert dataset_code == "quarterly_metric"
        return _ExplodingAdapter(payload)

    app = create_app(
        uow_factory=_AuditFailureUowFactory(factory),  # type: ignore[arg-type]
        adapter_factory=adapter_factory,
        principal_provider=_group_tax_admin,
    )
    try:
        with TestClient(app) as client:
            batch = _create_batch(client, _metadata("quarterly_metric", source="SAP"))
            with caplog.at_level("ERROR", logger="tax_risk.application.ingest"):
                response = _upload_csv(client, str(batch["id"]), b"private-financial-data")
    finally:
        database_engine.dispose()

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "INGEST_PROCESSING_FAILED",
        "message": "ingest file processing failed",
    }
    messages = [record.message for record in caplog.records]
    assert messages.count("ingest_processing_failed") == 1
    assert messages.count("ingest_processing_audit_failed") == 1
    assert "private-financial-data" not in caplog.text
    assert "secret audit storage failure" in caplog.text


def test_upload_capacity_rejection_is_503_and_does_not_touch_batch(
    isolated_database_url: str,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    entered = Event()
    release = Event()

    def adapter_factory(payload: bytes, dataset_code: str) -> _HoldingEmptyAdapter:
        assert dataset_code == "quarterly_metric"
        return _HoldingEmptyAdapter(payload, entered, release)

    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        adapter_factory=adapter_factory,
        settings=Settings(ingest_max_concurrent_uploads=1),
        principal_provider=_group_tax_admin,
    )
    try:
        with TestClient(app) as first_client, TestClient(app) as second_client:
            first_batch = _create_batch(first_client, _metadata("quarterly_metric"))
            second_batch = _create_batch(second_client, _metadata("quarterly_metric"))
            with ThreadPoolExecutor(max_workers=1) as executor:
                first_future = executor.submit(
                    _upload_csv,
                    first_client,
                    str(first_batch["id"]),
                    b"hold",
                )
                assert entered.wait(timeout=5)
                second = _upload_csv(
                    second_client,
                    str(second_batch["id"]),
                    b"second",
                )
                second_state = second_client.get(f"/api/v1/ingest-batches/{second_batch['id']}")
                release.set()
                first = first_future.result(timeout=10)
    finally:
        release.set()
        database_engine.dispose()

    assert second.status_code == 503
    assert second.json()["detail"]["code"] == "INGEST_CAPACITY_EXCEEDED"
    assert second_state.status_code == 200
    assert second_state.json()["status"] == "RECEIVED"
    assert second_state.json()["checksum"] == "0" * 64
    assert first.status_code == 200


def test_financial_shared_lock_serializes_with_deactivation_and_reverse_reads_inactive(
    isolated_database_url: str,
) -> None:
    database_engine, factory = create_session_factory(isolated_database_url)
    financial_locked = Event()
    release_financial = Event()
    master_started = Event()
    company_code = f"SHARED-{uuid4().hex}"

    def pausing_uow_factory() -> _PausingFinancialUow:
        return _PausingFinancialUow(factory, financial_locked, release_financial)

    def master_adapter_factory(
        payload: bytes,
        dataset_code: str,
    ) -> _SignalingCompanyAdapter:
        assert dataset_code == "company_master"
        return _SignalingCompanyAdapter(
            payload,
            master_started,
            company_code=company_code,
            extracted_at=datetime(2026, 7, 2, 8, tzinfo=timezone.utc),
        )

    setup_app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        principal_provider=_group_tax_admin,
    )
    financial_app = create_app(
        uow_factory=pausing_uow_factory,
        principal_provider=_group_tax_admin,
    )
    master_app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        adapter_factory=master_adapter_factory,
        principal_provider=_group_tax_admin,
    )
    try:
        with (
            TestClient(setup_app) as setup_client,
            TestClient(financial_app) as financial_client,
            TestClient(master_app) as master_client,
        ):
            master_seed = _create_batch(setup_client, _metadata("company_master"))
            seeded = _upload_csv(
                setup_client,
                str(master_seed["id"]),
                _single_company_master_payload(
                    company_code,
                    f"Company {company_code}",
                    "ACTIVE",
                    "2026-07-01T08:00:00+00:00",
                ),
            )
            assert seeded.status_code == 200
            financial_batch = _create_batch(
                financial_client,
                _metadata("quarterly_metric", source="SAP"),
            )
            deactivate_batch = _create_batch(
                master_client,
                _metadata("company_master", source="MASTER"),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                financial_future = executor.submit(
                    _upload_csv,
                    financial_client,
                    str(financial_batch["id"]),
                    _single_financial_company_payload(company_code),
                )
                assert financial_locked.wait(timeout=5)
                master_future = executor.submit(
                    _upload_csv,
                    master_client,
                    str(deactivate_batch["id"]),
                    b"deactivate",
                )
                assert master_started.wait(timeout=5)
                with pytest.raises(FutureTimeoutError):
                    master_future.result(timeout=0.25)
                release_financial.set()
                financial_response = financial_future.result(timeout=10)
                master_response = master_future.result(timeout=10)

            reverse_batch = _create_batch(
                financial_client,
                _metadata("quarterly_metric", source="SAP"),
            )
            reverse = _upload_csv(
                financial_client,
                str(reverse_batch["id"]),
                _single_financial_company_payload(company_code),
            )
    finally:
        release_financial.set()
        database_engine.dispose()

    assert financial_response.status_code == 200
    assert financial_response.json()["status"] == "SUCCEEDED"
    assert master_response.status_code == 200
    assert master_response.json()["status"] == "SUCCEEDED"
    assert reverse.status_code == 200
    assert reverse.json()["status"] == "FAILED"
    assert reverse.json()["errors"][0]["error_code"] == "INACTIVE_COMPANY"


def _financial_payload_for_amounts(amounts: list[str]) -> bytes:
    header = (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
    )
    rows = [
        (
            f"ordered-{index}-{uuid4().hex},C001,2026,2026-03-31,CNY,12,"
            f"cumulative_profit,{amount},2026-04-01T08:00:00+00:00\n"
        )
        for index, amount in enumerate(amounts, start=1)
    ]
    return (header + "".join(rows)).encode()


def _single_financial_company_payload(company_code: str) -> bytes:
    return (
        "source_record_key,company_code,fiscal_year,period,currency,amount_scale,"
        "metric_code,amount,extracted_at\n"
        f"financial-{uuid4().hex},{company_code},2026,2026-03-31,CNY,2,"
        "cumulative_profit,10.00,2026-04-01T08:00:00+00:00\n"
    ).encode()


def test_control_total_validity_depends_only_on_final_sum_not_row_order(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, _ = api_resources
    _seed_active_companies(client)
    maximum = "99999999999999999999999999.999999999999"
    orders = [
        [maximum, "1.000000000000", "-1.000000000000"],
        ["1.000000000000", "-1.000000000000", maximum],
    ]

    results = []
    for amounts in orders:
        batch = _create_batch(
            client,
            _metadata("quarterly_metric", source="SAP") | {"amount_scale": 12},
        )
        response = _upload_csv(
            client,
            str(batch["id"]),
            _financial_payload_for_amounts(amounts),
        )
        assert response.status_code == 200
        results.append(response.json())

    for result in results:
        assert result["status"] == "SUCCEEDED"
        assert result["record_count"] == 3
        assert result["accepted_count"] == 3
        assert result["rejected_count"] == 0
        assert Decimal(result["control_total"]) == Decimal(maximum)
        assert result["errors"] == []


def test_final_out_of_range_control_total_fails_whole_file_without_source_records(
    api_resources: tuple[TestClient, Engine],
) -> None:
    client, database_engine = api_resources
    _seed_active_companies(client)
    maximum = "99999999999999999999999999.999999999999"
    batch = _create_batch(
        client,
        _metadata("quarterly_metric", source="SAP") | {"amount_scale": 12},
    )

    response = _upload_csv(
        client,
        str(batch["id"]),
        _financial_payload_for_amounts([maximum, "1.000000000000"]),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "FAILED"
    assert result["record_count"] == 2
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 2
    assert Decimal(result["control_total"]) == Decimal("0")
    assert [error["error_code"] for error in result["errors"]] == ["CONTROL_TOTAL_OUT_OF_RANGE"]
    assert result["errors"][0]["row_number"] == 1
    with database_engine.connect() as connection:
        source_count = connection.execute(
            text("SELECT count(*) FROM source_record WHERE batch_id = :batch_id"),
            {"batch_id": batch["id"]},
        ).scalar_one()
    assert source_count == 0
