from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import partial
from threading import Event, Lock
from typing import Protocol, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError

from tax_risk.application.refund_writebacks import IncomeTaxRefundWritebackService
from tax_risk.persistence.income_tax_refund_models import IncomeTaxRefundWriteback
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def write_status(self, company_code: str, desired_value: str) -> object:
        self.calls.append((company_code, desired_value))
        return object()


class _GenerationRaceSender:
    def __init__(self) -> None:
        self.first_entered = Event()
        self.release_first = Event()
        self._lock = Lock()
        self.calls = 0

    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=10):
                raise RuntimeError("timed out waiting for the competing claim")
        return object()


class _PsycopgDiagnostic(Protocol):
    constraint_name: str


class _PsycopgError(Protocol):
    diag: _PsycopgDiagnostic


def _constraint_name(error: IntegrityError) -> str:
    assert error.orig is not None
    return cast(_PsycopgError, error.orig).diag.constraint_name


@pytest.mark.parametrize(
    ("status", "attempt_count", "last_error", "processed_at_sql"),
    [
        pytest.param("PENDING", 0, None, "now()", id="pending-cannot-be-processed"),
        pytest.param("PROCESSING", 0, None, "NULL", id="processing-needs-attempt"),
        pytest.param("SUCCEEDED", 1, None, "NULL", id="success-needs-timestamp"),
        pytest.param(
            "SUCCEEDED",
            1,
            "must-be-null",
            "now()",
            id="success-cannot-have-error",
        ),
        pytest.param("FAILED", 1, None, "NULL", id="failure-needs-error"),
    ],
)
def test_live_database_rejects_invalid_writeback_delivery_states(
    isolated_database_url: str,
    status: str,
    attempt_count: int,
    last_error: str | None,
    processed_at_sql: str,
) -> None:
    engine, _factory = create_session_factory(isolated_database_url)
    company_id, target_id = _seed_target(engine, f"STATE-{uuid4().hex}")
    statement = text(
        "INSERT INTO income_tax_refund_writeback ("
        "id, target_id, company_id, idempotency_key, desired_value, status, "
        f"attempt_count, last_error, processed_at) VALUES ("
        ":id, :target_id, :company_id, :idempotency_key, '已退税', :status, "
        f":attempt_count, :last_error, {processed_at_sql})"
    )
    try:
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                connection.execute(
                    statement,
                    {
                        "id": uuid4(),
                        "target_id": target_id,
                        "company_id": company_id,
                        "idempotency_key": f"invalid-state:{uuid4()}",
                        "status": status,
                        "attempt_count": attempt_count,
                        "last_error": last_error,
                    },
                )
        assert _constraint_name(raised.value) == ("ck_income_tax_refund_writeback_delivery_state")
    finally:
        engine.dispose()


def test_live_database_enforces_writeback_idempotency_and_target_uniqueness(
    isolated_database_url: str,
) -> None:
    engine, _factory = create_session_factory(isolated_database_url)
    company_a, target_a = _seed_target(engine, "UNIQUE-A")
    company_b, target_b = _seed_target(engine, "UNIQUE-B")
    shared_key = f"refund-received:{uuid4()}"
    try:
        _insert_writeback(engine, target_a, company_a, idempotency_key=shared_key)
        with pytest.raises(IntegrityError) as duplicate_key:
            _insert_writeback(engine, target_b, company_b, idempotency_key=shared_key)
        assert _constraint_name(duplicate_key.value) == (
            "uq_income_tax_refund_writeback_idempotency_key"
        )

        with pytest.raises(IntegrityError) as duplicate_target:
            _insert_writeback(
                engine,
                target_a,
                company_a,
                idempotency_key=f"other-key:{uuid4()}",
            )
        assert _constraint_name(duplicate_target.value) == ("uq_income_tax_refund_writeback_target")
    finally:
        engine.dispose()


def test_live_database_rejects_cross_company_target_reference(
    isolated_database_url: str,
) -> None:
    engine, _factory = create_session_factory(isolated_database_url)
    _company_a, target_a = _seed_target(engine, "FK-A")
    company_b, _target_b = _seed_target(engine, "FK-B")
    try:
        with pytest.raises(IntegrityError) as raised:
            _insert_writeback(
                engine,
                target_a,
                company_b,
                idempotency_key=f"cross-company:{uuid4()}",
            )
        assert _constraint_name(raised.value) == "fk_refund_writeback_target_company"
    finally:
        engine.dispose()


def test_skip_locked_claim_returns_without_waiting_or_calling_sender(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    company_id, target_id = _seed_target(engine, "LOCKED")
    writeback_id = _insert_writeback(
        engine,
        target_id,
        company_id,
        idempotency_key=f"locked:{uuid4()}",
    )
    sender = _RecordingSender()
    service = IncomeTaxRefundWritebackService(
        partial(UnitOfWork, factory),
        sender,
        max_retries=3,
    )
    try:
        with factory() as locking_session:
            locked = locking_session.scalar(
                select(IncomeTaxRefundWriteback)
                .where(IncomeTaxRefundWriteback.id == writeback_id)
                .with_for_update()
            )
            assert locked is not None

            outcome = service.deliver(writeback_id)

            assert outcome.status == "PENDING"
            assert outcome.claimed is False
            assert sender.calls == []
    finally:
        engine.dispose()


def test_newer_processing_generation_wins_a_real_database_race(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    company_id, target_id = _seed_target(engine, "RACE")
    writeback_id = _insert_writeback(
        engine,
        target_id,
        company_id,
        idempotency_key=f"generation-race:{uuid4()}",
    )
    sender = _GenerationRaceSender()
    service = IncomeTaxRefundWritebackService(
        partial(UnitOfWork, factory),
        sender,
        max_retries=3,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(service.deliver, writeback_id)
            assert sender.first_entered.wait(timeout=10)

            second = service.deliver(writeback_id)
            sender.release_first.set()
            first = first_future.result(timeout=10)

        assert second.status == "SUCCEEDED"
        assert second.attempt_count == 2
        assert first.status == "PROCESSING"
        assert first.error_code == "WRITEBACK_STATE_CHANGED"
        assert sender.calls == 2
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT status, attempt_count, last_error, processed_at "
                    "FROM income_tax_refund_writeback WHERE id = :id"
                ),
                {"id": writeback_id},
            ).one()
        assert stored.status == "SUCCEEDED"
        assert stored.attempt_count == 2
        assert stored.last_error is None
        assert stored.processed_at is not None
    finally:
        sender.release_first.set()
        engine.dispose()


def test_processing_row_recovers_after_retry_limit_in_live_database(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    company_id, target_id = _seed_target(engine, "RECOVERY")
    writeback_id = _insert_writeback(
        engine,
        target_id,
        company_id,
        idempotency_key=f"processing-recovery:{uuid4()}",
        status="PROCESSING",
        attempt_count=4,
    )
    sender = _RecordingSender()
    service = IncomeTaxRefundWritebackService(
        partial(UnitOfWork, factory),
        sender,
        max_retries=3,
    )
    try:
        outcome = service.deliver(writeback_id)

        assert outcome.status == "SUCCEEDED"
        assert outcome.attempt_count == 5
        assert sender.calls == [("REFUND-RECOVERY", "已退税")]
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT status, attempt_count FROM income_tax_refund_writeback WHERE id = :id"
                ),
                {"id": writeback_id},
            ).one()
        assert tuple(stored) == ("SUCCEEDED", 5)
    finally:
        engine.dispose()


def _seed_target(engine: Engine, suffix: str) -> tuple[UUID, UUID]:
    company_id = uuid4()
    target_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company (id, company_code, company_name, lifecycle) "
                "VALUES (:id, :code, :name, 'ACTIVE')"
            ),
            {
                "id": company_id,
                "code": f"REFUND-{suffix}",
                "name": f"Refund {suffix}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO income_tax_refund_target ("
                "id, company_id, refund_tax_year, source_record_key, expected_amount, "
                "currency, amount_scale, source_version, receipt_status, received_at, "
                "latest_scan_period) VALUES ("
                ":id, :company_id, 2025, :source_key, 100.00, 'CNY', 2, 'test-v1', "
                "'RECEIVED', now(), :scan_period)"
            ),
            {
                "id": target_id,
                "company_id": company_id,
                "source_key": f"source-{target_id}",
                "scan_period": date(2026, 3, 31),
            },
        )
    return company_id, target_id


def _insert_writeback(
    engine: Engine,
    target_id: UUID,
    company_id: UUID,
    *,
    idempotency_key: str,
    status: str = "PENDING",
    attempt_count: int = 0,
) -> UUID:
    writeback_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO income_tax_refund_writeback ("
                "id, target_id, company_id, idempotency_key, desired_value, status, "
                "attempt_count, last_error, processed_at) VALUES ("
                ":id, :target_id, :company_id, :idempotency_key, '已退税', :status, "
                ":attempt_count, NULL, NULL)"
            ),
            {
                "id": writeback_id,
                "target_id": target_id,
                "company_id": company_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "attempt_count": attempt_count,
            },
        )
    return writeback_id
