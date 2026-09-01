from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, text

from tax_risk.application.quarterly_batches import QuarterlyBatchService
from tax_risk.application.quarterly_runs import QuarterlyRunService
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.quarterly_batch import (
    build_quarterly_batch_canvas,
    register_quarterly_tasks,
)


@dataclass
class _InjectedFailureState:
    failed_snapshot_ids: set[UUID]


class _InjectedCompanyFailure(RuntimeError):
    pass


class _QuarterlyBatchSeed(Protocol):
    snapshot_set_id: UUID
    rule_version_id: UUID
    snapshot_ids: tuple[UUID, ...]
    inactive_company_id: UUID
    failed_snapshot_id: UUID


class _CompanyRunner:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        failure_state: _InjectedFailureState,
    ) -> None:
        self._delegate = QuarterlyRunService(uow_factory)
        self._failure_state = failure_state

    def execute(self, *, run_id: UUID, snapshot_id: UUID) -> object:
        if snapshot_id in self._failure_state.failed_snapshot_ids:
            raise _InjectedCompanyFailure("injected worker failure")
        return self._delegate.execute(run_id=run_id, snapshot_id=snapshot_id)


def _status_counts(engine: Engine, run_id: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT status, count(*)
                FROM monitoring_run_company
                WHERE run_id = :run_id
                GROUP BY status
                """
            ),
            {"run_id": run_id},
        )
        return {str(status): count for status, count in rows}


def _run_row(engine: Engine, run_id: UUID) -> dict[str, object]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM monitoring_run WHERE id = :run_id"),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
        return dict(row)


def _evidence_counts(engine: Engine, run_id: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        detection = (
            connection.execute(
                text(
                    """
                SELECT count(*) AS total,
                       count(DISTINCT detection_key) AS unique_keys,
                       count(DISTINCT company_id) AS companies
                FROM detection_record
                WHERE run_id = :run_id
                """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
        cases = (
            connection.execute(
                text(
                    """
                SELECT count(*) AS total,
                       count(DISTINCT fingerprint) AS unique_fingerprints
                FROM risk_case
                WHERE company_id IN (
                    SELECT member.company_id
                    FROM monitoring_run_company AS company_run
                    JOIN snapshot_set_member AS member
                      ON member.id = company_run.snapshot_set_member_id
                    WHERE company_run.run_id = :run_id
                )
                """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
        return {
            "detections": detection["total"],
            "unique_detection_keys": detection["unique_keys"],
            "detected_companies": detection["companies"],
            "cases": cases["total"],
            "unique_case_fingerprints": cases["unique_fingerprints"],
        }


def _snapshot_source_counts(engine: Engine, snapshot_ids: tuple[UUID, ...]) -> dict[str, int]:
    with engine.connect() as connection:
        counts = (
            connection.execute(
                text(
                    """
                SELECT count(DISTINCT source.id) AS snapshot_sources,
                       count(DISTINCT batch.id) AS sap_batches,
                       count(record.id) AS source_records
                FROM snapshot_source AS source
                JOIN accounting_snapshot AS snapshot
                  ON snapshot.id = source.snapshot_id
                JOIN ingest_batch AS batch
                  ON batch.id = source.ingest_batch_id
                 AND batch.source = 'SAP'
                JOIN source_record AS record
                  ON record.batch_id = batch.id
                WHERE snapshot.id = ANY(:snapshot_ids)
                """
                ),
                {"snapshot_ids": list(snapshot_ids)},
            )
            .mappings()
            .one()
        )
        return {key: int(value) for key, value in counts.items()}


def test_105_company_batch_isolates_failures_and_retries_failed_only(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
    rls_database_url: str,
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    failure_state = _InjectedFailureState({seed.failed_snapshot_id})

    def batch_service_factory() -> QuarterlyBatchService:
        return QuarterlyBatchService(
            uow_factory,
            company_runner_factory=lambda: _CompanyRunner(uow_factory, failure_state),
        )

    app_engine, app_factory = create_session_factory(rls_database_url)
    app_uow_factory = partial(UnitOfWork, app_factory)

    def worker_batch_service_factory() -> QuarterlyBatchService:
        return QuarterlyBatchService(
            app_uow_factory,
            company_runner_factory=lambda: _CompanyRunner(
                app_uow_factory,
                failure_state,
            ),
        )

    service = batch_service_factory()
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )

    assert plan.run_key == (f"quarterly:2026:Q2:{seed.snapshot_set_id}:{seed.rule_version_id}")
    assert len(plan.run_company_ids) == 105
    assert len(set(plan.run_company_ids)) == 105
    assert _snapshot_source_counts(engine, seed.snapshot_ids) == {
        "snapshot_sources": 105,
        "sap_batches": 105,
        "source_records": 945,
    }

    # If the process dies after the database commit but before broker publish,
    # resubmitting the same immutable batch must return only its still-pending work.
    redispatch = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    assert redispatch.run_id == plan.run_id
    assert set(redispatch.run_company_ids) == set(plan.run_company_ids)

    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=0,
        quarterly_worker_concurrency=4,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=worker_batch_service_factory)
    first_company_ids = _company_ids_for_tasks(engine, plan.run_company_ids)

    first_summary = (
        build_quarterly_batch_canvas(
            app=app,
            run_id=plan.run_id,
            run_company_ids=plan.run_company_ids,
            company_ids=first_company_ids,
            scope_period=date(2026, 6, 30),
            worker_scope_secret=settings.worker_scope_secret,
        )
        .apply_async()
        .get(timeout=120)
    )

    assert first_summary["run_id"] == str(plan.run_id)
    assert first_summary["status"] == "PARTIAL_SUCCESS"
    assert first_summary["requested_company_count"] == 105
    assert first_summary["succeeded_company_count"] == 103
    assert first_summary["blocked_company_count"] == 1
    assert first_summary["failed_company_count"] == 1
    assert _status_counts(engine, plan.run_id) == {
        "BLOCKED": 1,
        "FAILED": 1,
        "SUCCEEDED": 103,
    }
    run = _run_row(engine, plan.run_id)
    assert run["status"] == "PARTIAL_SUCCESS"
    assert run["succeeded_company_count"] == 103
    assert run["blocked_company_count"] == 1
    assert run["failed_company_count"] == 1
    assert run["finished_at"] is not None
    first_finished_at = run["finished_at"]
    assert _evidence_counts(engine, plan.run_id) == {
        "detections": 309,
        "unique_detection_keys": 309,
        "detected_companies": 103,
        "cases": 309,
        "unique_case_fingerprints": 309,
    }
    with engine.connect() as connection:
        frozen = (
            connection.execute(
                text(
                    """
                SELECT snapshot.lineage AS snapshot_lineage,
                       detection.lineage AS detection_lineage
                FROM detection_record AS detection
                JOIN accounting_snapshot AS snapshot ON snapshot.id = detection.snapshot_id
                WHERE detection.run_id = :run_id
                  AND detection.company_id = :company_id
                ORDER BY detection.monitor_type
                LIMIT 1
                """
                ),
                {"run_id": plan.run_id, "company_id": seed.company_ids[0]},
            )
            .mappings()
            .one()
        )
    snapshot_lineage = frozen["snapshot_lineage"]
    detection_lineage = frozen["detection_lineage"]
    source_batch = snapshot_lineage["sources"][0]["batch"]
    assert source_batch["extraction_time"].endswith("Z")
    assert source_batch["payload_ref"].endswith(".csv")
    assert detection_lineage["sources"] == snapshot_lineage["sources"]
    assert (
        detection_lineage["tax_master_version"]["source_file_name"]
        == (snapshot_lineage["tax_master"]["source_file_name"])
    )
    assert (
        detection_lineage["tax_master_version"]["imported_at"]
        == (snapshot_lineage["tax_master"]["imported_at"])
    )
    assert detection_lineage["tax_master_version"]["imported_at"].endswith("Z")

    duplicate_summary = service.summarize(run_id=plan.run_id)
    assert duplicate_summary == first_summary
    assert _run_row(engine, plan.run_id)["finished_at"] == first_finished_at

    replay = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    assert replay.run_id == plan.run_id
    assert replay.run_company_ids == ()

    failure_state.failed_snapshot_ids.clear()
    retry_plan = service.retry_failed(run_id=plan.run_id)
    assert retry_plan.run_id == plan.run_id
    assert len(retry_plan.run_company_ids) == 1

    retry_summary = (
        build_quarterly_batch_canvas(
            app=app,
            run_id=retry_plan.run_id,
            run_company_ids=retry_plan.run_company_ids,
            company_ids=_company_ids_for_tasks(engine, retry_plan.run_company_ids),
            summary_company_ids=first_company_ids,
            scope_period=date(2026, 6, 30),
            worker_scope_secret=settings.worker_scope_secret,
        )
        .apply_async()
        .get(timeout=30)
    )

    assert retry_summary["status"] == "PARTIAL_SUCCESS"
    assert retry_summary["succeeded_company_count"] == 104
    assert retry_summary["blocked_company_count"] == 1
    assert retry_summary["failed_company_count"] == 0
    assert _status_counts(engine, plan.run_id) == {
        "BLOCKED": 1,
        "SUCCEEDED": 104,
    }
    assert _evidence_counts(engine, plan.run_id) == {
        "detections": 312,
        "unique_detection_keys": 312,
        "detected_companies": 104,
        "cases": 312,
        "unique_case_fingerprints": 312,
    }

    with engine.connect() as connection:
        attempts = connection.execute(
            text(
                """
                SELECT company_run.status, company_run.attempt_count
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.run_id = :run_id
                  AND (
                      member.company_id = :inactive_company_id
                      OR member.snapshot_id = :failed_snapshot_id
                  )
                ORDER BY member.company_id
                """
            ),
            {
                "run_id": plan.run_id,
                "inactive_company_id": seed.inactive_company_id,
                "failed_snapshot_id": seed.failed_snapshot_id,
            },
        ).all()
    assert sorted((str(status), count) for status, count in attempts) == [
        ("BLOCKED", 1),
        ("SUCCEEDED", 2),
    ]
    app_engine.dispose()


def _company_ids_for_tasks(
    engine: Engine,
    run_company_ids: tuple[UUID, ...],
) -> tuple[UUID, ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT company_run.id, member.company_id
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.id = ANY(:run_company_ids)
                """
            ),
            {"run_company_ids": list(run_company_ids)},
        ).all()
    company_by_task = {task_id: company_id for task_id, company_id in rows}
    return tuple(company_by_task[value] for value in run_company_ids)


def test_same_celery_task_can_retry_a_persisted_failed_company_attempt(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    failure_state = _InjectedFailureState({seed.failed_snapshot_id})
    service = QuarterlyBatchService(
        uow_factory,
        company_runner_factory=lambda: _CompanyRunner(uow_factory, failure_state),
    )
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    with engine.connect() as connection:
        run_company_id = connection.execute(
            text(
                """
                SELECT company_run.id
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.run_id = :run_id
                  AND member.snapshot_id = :snapshot_id
                """
            ),
            {"run_id": plan.run_id, "snapshot_id": seed.failed_snapshot_id},
        ).scalar_one()

    first = service.run_company(
        run_company_id=run_company_id,
        task_id="celery-same-id",
        automatic_retry_pending=True,
    )
    assert first["status"] == "RETRY_PENDING"
    assert first["retryable"] is True
    failure_state.failed_snapshot_ids.clear()

    second = service.run_company(run_company_id=run_company_id, task_id="celery-same-id")

    assert second["status"] == "SUCCEEDED"
    assert second["retryable"] is False
    assert second["run_type"] == "QUARTERLY"
    assert second["monitor_type"] == "QUARTERLY_ALL"
    assert second["batch_id"] == str(plan.run_id)
    assert second["period"] == "2026-Q2"
    assert second["retry_count"] == 1
    assert second["company_output_ready_at"] is not None
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT status, attempt_count, celery_task_id
                FROM monitoring_run_company
                WHERE id = :run_company_id
                """
            ),
            {"run_company_id": run_company_id},
        ).one()
    assert tuple(row) == ("SUCCEEDED", 2, "celery-same-id")


def test_emergency_header_failure_is_reconciled_once_before_database_summary(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    service = QuarterlyBatchService(uow_factory)
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    run_company_id = plan.run_company_ids[0]
    emergency = {
        "run_company_id": str(run_company_id),
        "status": "FAILED",
        "retryable": False,
        "task_id": "celery-emergency-id",
        "error_code": "CELERY_TASK_EXECUTION_FAILED",
        "detection_ids": [],
        "case_ids": [],
    }

    service.reconcile_header_results(
        run_id=plan.run_id,
        header_results=[emergency],
    )
    service.reconcile_header_results(
        run_id=plan.run_id,
        header_results=[emergency],
    )
    summary = service.summarize(run_id=plan.run_id)

    assert summary["status"] == "RUNNING"
    assert summary["failed_company_count"] == 1
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT status, attempt_count, retryable, celery_task_id,
                       error_code, detection_ids, case_ids
                FROM monitoring_run_company
                WHERE id = :run_company_id
                """
            ),
            {"run_company_id": run_company_id},
        ).one()
    assert tuple(row) == (
        "FAILED",
        1,
        True,
        "celery-emergency-id",
        "CELERY_TASK_EXECUTION_FAILED",
        [],
        [],
    )


def test_duplicate_canvas_cannot_finalize_while_owner_retry_is_pending(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    failure_state = _InjectedFailureState({seed.failed_snapshot_id})
    service = QuarterlyBatchService(
        uow_factory,
        company_runner_factory=lambda: _CompanyRunner(uow_factory, failure_state),
    )
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    with engine.connect() as connection:
        owner_run_company_id = connection.execute(
            text(
                """
                SELECT company_run.id
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.run_id = :run_id
                  AND member.snapshot_id = :snapshot_id
                """
            ),
            {"run_id": plan.run_id, "snapshot_id": seed.failed_snapshot_id},
        ).scalar_one()

    retry_pending = service.run_company(
        run_company_id=owner_run_company_id,
        task_id="owner-task-a",
        automatic_retry_pending=True,
    )
    assert retry_pending["status"] == "RETRY_PENDING"
    with engine.connect() as connection:
        atomic_state = connection.execute(
            text(
                "SELECT status, attempt_count, celery_task_id "
                "FROM monitoring_run_company WHERE id = :run_company_id"
            ),
            {"run_company_id": owner_run_company_id},
        ).one()
    assert tuple(atomic_state) == ("RETRY_PENDING", 1, "owner-task-a")

    settings = Settings(
        redis_url="redis://localhost:6379/15",
        environment="test",
        celery_task_always_eager=True,
        celery_task_eager_propagates=False,
        celery_task_store_eager_result=True,
        quarterly_task_max_retries=0,
    )
    app = create_celery_app(settings)
    register_quarterly_tasks(app=app, service_factory=lambda: service)
    duplicate_summary = (
        build_quarterly_batch_canvas(
            app=app,
            run_id=plan.run_id,
            run_company_ids=(owner_run_company_id,),
        )
        .apply_async()
        .get(timeout=10)
    )

    assert duplicate_summary["status"] == "RUNNING"
    with engine.connect() as connection:
        owner_state = connection.execute(
            text(
                """
                SELECT status, attempt_count, celery_task_id
                FROM monitoring_run_company
                WHERE id = :run_company_id
                """
            ),
            {"run_company_id": owner_run_company_id},
        ).one()
    assert tuple(owner_state) == ("RETRY_PENDING", 1, "owner-task-a")

    failure_state.failed_snapshot_ids.clear()
    for run_company_id in plan.run_company_ids:
        if run_company_id != owner_run_company_id:
            service.run_company(
                run_company_id=run_company_id,
                task_id=f"remaining-{run_company_id}",
            )
    owner_success = service.run_company(
        run_company_id=owner_run_company_id,
        task_id="owner-task-a",
    )
    final_summary = service.summarize(run_id=plan.run_id)

    assert owner_success["status"] == "SUCCEEDED"
    assert final_summary["status"] == "PARTIAL_SUCCESS"
    assert final_summary["succeeded_company_count"] == 104
    assert final_summary["blocked_company_count"] == 1
    assert final_summary["failed_company_count"] == 0


def test_emergency_failure_exhausted_during_retry_pending_becomes_failed(
    quarterly_batch_resources: tuple[
        Callable[[], UnitOfWork],
        Engine,
        _QuarterlyBatchSeed,
    ],
) -> None:
    uow_factory, engine, seed = quarterly_batch_resources
    failure_state = _InjectedFailureState({seed.failed_snapshot_id})
    service = QuarterlyBatchService(
        uow_factory,
        company_runner_factory=lambda: _CompanyRunner(uow_factory, failure_state),
    )
    plan = service.start_batch(
        fiscal_year=2026,
        quarter=2,
        snapshot_set_id=seed.snapshot_set_id,
        rule_version_id=seed.rule_version_id,
    )
    with engine.connect() as connection:
        run_company_id = connection.execute(
            text(
                """
                SELECT company_run.id
                FROM monitoring_run_company AS company_run
                JOIN snapshot_set_member AS member
                  ON member.id = company_run.snapshot_set_member_id
                WHERE company_run.run_id = :run_id
                  AND member.snapshot_id = :snapshot_id
                """
            ),
            {"run_id": plan.run_id, "snapshot_id": seed.failed_snapshot_id},
        ).scalar_one()
    first = service.run_company(
        run_company_id=run_company_id,
        task_id="retry-owner",
        automatic_retry_pending=True,
    )
    assert first["status"] == "RETRY_PENDING"
    foreign_emergency = {
        "run_company_id": str(run_company_id),
        "status": "FAILED",
        "retryable": False,
        "task_id": "duplicate-task-without-ownership",
        "error_code": "CELERY_TASK_EXECUTION_FAILED",
        "detection_ids": [],
        "case_ids": [],
    }
    service.reconcile_header_results(
        run_id=plan.run_id,
        header_results=[foreign_emergency],
    )
    with engine.connect() as connection:
        still_owned = connection.execute(
            text(
                "SELECT status, attempt_count, celery_task_id "
                "FROM monitoring_run_company WHERE id = :run_company_id"
            ),
            {"run_company_id": run_company_id},
        ).one()
    assert tuple(still_owned) == ("RETRY_PENDING", 1, "retry-owner")

    emergency = {
        "run_company_id": str(run_company_id),
        "status": "FAILED",
        "retryable": False,
        "task_id": "retry-owner",
        "error_code": "CELERY_TASK_EXECUTION_FAILED",
        "detection_ids": [],
        "case_ids": [],
    }

    service.reconcile_header_results(run_id=plan.run_id, header_results=[emergency])
    service.reconcile_header_results(run_id=plan.run_id, header_results=[emergency])

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT status, attempt_count, celery_task_id, error_code
                FROM monitoring_run_company
                WHERE id = :run_company_id
                """
            ),
            {"run_company_id": run_company_id},
        ).one()
    assert tuple(row) == (
        "FAILED",
        2,
        "retry-owner",
        "CELERY_TASK_EXECUTION_FAILED",
    )
