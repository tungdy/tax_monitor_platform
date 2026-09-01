from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB, NUMERIC, TIMESTAMP, UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from tax_risk.config import Settings


EXPECTED_TABLES = {
    "business_entertainment_evaluation",
    "business_entertainment_scope_company",
    "business_entertainment_scope_version",
    "business_entertainment_source_observation",
    "company",
    "ingest_batch",
    "ingest_error",
    "income_tax_refund_scan_result",
    "income_tax_refund_target",
    "income_tax_refund_writeback",
    "source_record",
    "tax_master_version",
    "accounting_snapshot",
    "snapshot_source",
    "snapshot_set",
    "snapshot_set_member",
    "rule_version",
    "monitoring_run",
    "monitoring_run_company",
    "detection_record",
    "evidence_link",
    "risk_case",
    "review_action",
    "sap_expense_voucher_observation",
    "sap_expense_voucher_snapshot_projection",
    "sap_gl_line_observation",
    "sap_link_coverage",
    "sap_refund_evidence_batch",
    "suggested_account_dictionary_version",
    "suggested_account_entry",
    "semantic_artifact_version",
    "semantic_model_call_audit",
    "semantic_detection_record",
    "semantic_evidence_task",
    "semantic_version_set",
    "business_entertainment_case_detail",
    "audit_event",
    "export_job",
    "release_manifest",
    "release_event",
}
BACKEND_ROOT = Path(__file__).resolve().parents[3]
PYTEST_SCHEMA_PATTERN = re.compile(r"tax_risk_pytest_[0-9a-f]{32}")
PYTEST_SCHEMA_MARKER = "tax_risk_pytest_owned_v1"


def test_persistence_engine_uses_marker_owned_random_pytest_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
        schema_marker = connection.execute(
            text(
                """
                SELECT obj_description(namespace.oid, 'pg_namespace')
                FROM pg_namespace AS namespace
                WHERE namespace.nspname = current_schema()
                """
            )
        ).scalar_one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert PYTEST_SCHEMA_PATTERN.fullmatch(schema_name), schema_name
    assert schema_marker == PYTEST_SCHEMA_MARKER
    assert revision == "0023_refund_ambiguous_match_alert"
    version_column = _column(engine, "alembic_version", "version_num")
    assert version_column["type"].length == 64


def _column(engine: Engine, table_name: str, column_name: str) -> dict[str, object]:
    columns = inspect(engine).get_columns(table_name)
    return next(column for column in columns if column["name"] == column_name)


def _enum_labels(engine: Engine, enum_name: str) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    """
                    SELECT enum_value.enumlabel
                    FROM pg_enum AS enum_value
                    JOIN pg_type AS enum_type ON enum_type.oid = enum_value.enumtypid
                    JOIN pg_namespace AS enum_namespace
                      ON enum_namespace.oid = enum_type.typnamespace
                    WHERE enum_type.typname = :enum_name
                      AND enum_namespace.nspname = current_schema()
                    ORDER BY enum_value.enumsortorder
                    """
                ),
                {"enum_name": enum_name},
            ).scalars()
        )


def test_control_plane_has_every_required_table_and_uuid_primary_key(engine: Engine) -> None:
    inspector = inspect(engine)

    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    for table_name in EXPECTED_TABLES:
        primary_key = inspector.get_pk_constraint(table_name)
        assert primary_key["constrained_columns"] == ["id"]
        assert isinstance(_column(engine, table_name, "id")["type"], UUID)


def test_schema_uses_postgresql_enums_and_timezone_aware_audit_fields(engine: Engine) -> None:
    enum_names = {enum["name"] for enum in inspect(engine).get_enums()}

    assert {
        "company_lifecycle",
        "ingest_batch_status",
        "ingest_mode",
        "version_status",
        "snapshot_status",
        "snapshot_set_status",
        "monitoring_run_status",
        "monitoring_run_company_status",
        "calculation_status",
        "risk_case_status",
    } <= enum_names

    for table_name in EXPECTED_TABLES - {"audit_event", "release_event"}:
        created_at = _column(engine, table_name, "created_at")
        assert isinstance(created_at["type"], TIMESTAMP)
        assert created_at["type"].timezone is True

    occurred_at = _column(engine, "audit_event", "occurred_at")
    assert isinstance(occurred_at["type"], TIMESTAMP)
    assert occurred_at["type"].timezone is True

    release_occurred_at = _column(engine, "release_event", "occurred_at")
    assert isinstance(release_occurred_at["type"], TIMESTAMP)
    assert release_occurred_at["type"].timezone is True

    master_updated_at = _column(engine, "company", "master_data_updated_at")
    assert isinstance(master_updated_at["type"], TIMESTAMP)
    assert master_updated_at["type"].timezone is True
    assert master_updated_at["nullable"] is False


@pytest.mark.parametrize(
    ("enum_name", "expected_labels"),
    [
        (
            "ingest_batch_status",
            ["RECEIVED", "VALIDATING", "SUCCEEDED", "PARTIAL", "FAILED"],
        ),
        ("snapshot_status", ["DRAFT", "VALIDATED", "PUBLISHED"]),
        (
            "monitor_type",
            [
                "ACCRUAL_ACCURACY",
                "TAX_BURDEN",
                "POTENTIAL_TAX_COST",
                "BUSINESS_ENTERTAINMENT",
                "WELFARE",
                "DONATION",
                "DEFERRED_TAX_ACCURACY",
                "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
            ],
        ),
        (
            "monitoring_run_company_status",
            [
                "PENDING",
                "RUNNING",
                "RETRY_PENDING",
                "SUCCEEDED",
                "BLOCKED",
                "FAILED",
                "NOT_RUN",
            ],
        ),
        (
            "risk_case_status",
            [
                "NEW",
                "ASSIGNED",
                "PENDING_COMPANY_CONFIRMATION",
                "PENDING_ADJUSTMENT",
                "ADJUSTED_PENDING_REVIEW",
                "CLOSED",
                "GROUP_REVIEW",
                "EVIDENCE_REQUIRED",
            ],
        ),
    ],
)
def test_control_plane_enums_match_approved_phase_one_state_machines(
    engine: Engine,
    enum_name: str,
    expected_labels: list[str],
) -> None:
    assert _enum_labels(engine, enum_name) == expected_labels


def test_numeric_and_json_lineage_contracts_are_exact(engine: Engine) -> None:
    for table_name, column_name in {
        ("ingest_batch", "control_total"),
        ("source_record", "amount"),
        ("tax_master_version", "loss_carryforward"),
        ("accounting_snapshot", "control_total"),
        ("snapshot_source", "control_total"),
        ("detection_record", "input_amount"),
        ("detection_record", "result_amount"),
        ("detection_record", "difference_amount"),
        ("risk_case", "risk_amount"),
    }:
        numeric_type = _column(engine, table_name, column_name)["type"]
        assert isinstance(numeric_type, NUMERIC)
        assert (numeric_type.precision, numeric_type.scale) == (38, 12)

    for table_name, column_name in {
        ("tax_master_version", "tax_rate"),
        ("tax_master_version", "deferred_tax_rate"),
        ("tax_master_version", "average_tax_burden_rate_3y"),
        ("detection_record", "rate_value"),
    }:
        rate_type = _column(engine, table_name, column_name)["type"]
        assert isinstance(rate_type, NUMERIC)
        assert (rate_type.precision, rate_type.scale) == (20, 12)

    for table_name in {
        "ingest_batch",
        "source_record",
        "tax_master_version",
        "accounting_snapshot",
        "snapshot_source",
        "detection_record",
        "risk_case",
    }:
        columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
        assert {"currency", "amount_scale"} <= columns

    for table_name, column_name in {
        ("source_record", "lineage"),
        ("snapshot_source", "lineage"),
        ("detection_record", "lineage"),
        ("detection_record", "formula_substitution"),
    }:
        assert isinstance(_column(engine, table_name, column_name)["type"], JSONB)


def test_only_material_requisition_source_rows_may_omit_amount(engine: Engine) -> None:
    constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints("source_record")
    }

    assert "ck_source_record_amount_required_by_dataset" in constraints
    assert "oa_material_requisition" in constraints["ck_source_record_amount_required_by_dataset"]


def test_tax_master_governance_has_strict_state_loss_and_no_legacy_defaults(
    engine: Engine,
) -> None:
    source_row = _column(engine, "tax_master_version", "source_row_number")
    uploaded_by = _column(engine, "tax_master_version", "uploaded_by")
    deferred_tax_rate = _column(engine, "tax_master_version", "deferred_tax_rate")
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints("tax_master_version")
    }

    assert source_row["nullable"] is False
    assert source_row["default"] is None
    assert uploaded_by["nullable"] is False
    assert uploaded_by["default"] is None
    assert deferred_tax_rate["nullable"] is True
    assert deferred_tax_rate["default"] is None
    assert any("loss_carryforward >= 0" in sql for sql in checks.values())
    deferred_rate_sql = checks["ck_tax_master_version_deferred_tax_rate"]
    assert "deferred_tax_rate IS NULL" in deferred_rate_sql
    assert "deferred_tax_rate >= 0" in deferred_rate_sql
    assert "deferred_tax_rate <= 1" in deferred_rate_sql
    state_sql = next(sql for name, sql in checks.items() if "published_at_state" in name)
    assert "approved_by IS NOT NULL" in state_sql
    assert "approved_by IS NULL" in state_sql


def test_foreign_keys_cover_lineage_and_control_plane_relationships(engine: Engine) -> None:
    expected_foreign_keys = {
        "ingest_error": {("batch_id", "ingest_batch")},
        "source_record": {("batch_id", "ingest_batch"), ("company_id", "company")},
        "tax_master_version": {("company_id", "company"), ("source_batch_id", "ingest_batch")},
        "accounting_snapshot": {
            ("company_id", "company"),
            ("tax_master_version_id", "tax_master_version"),
        },
        "snapshot_source": {
            ("snapshot_id", "accounting_snapshot"),
            ("ingest_batch_id", "ingest_batch"),
        },
        "snapshot_set_member": {
            ("snapshot_set_id", "snapshot_set"),
            ("company_id", "company"),
            ("snapshot_id", "accounting_snapshot"),
        },
        "monitoring_run": {
            ("snapshot_set_id", "snapshot_set"),
            ("rule_version_id", "rule_version"),
        },
        "monitoring_run_company": {
            ("run_id", "monitoring_run"),
            ("snapshot_set_member_id", "snapshot_set_member"),
        },
        "detection_record": {
            ("run_id", "monitoring_run"),
            ("company_id", "company"),
            ("snapshot_id", "accounting_snapshot"),
            ("rule_version_id", "rule_version"),
            ("tax_master_version_id", "tax_master_version"),
        },
        "risk_case": {("company_id", "company"), ("latest_detection_id", "detection_record")},
        "review_action": {("risk_case_id", "risk_case")},
    }

    inspector = inspect(engine)
    for table_name, expected in expected_foreign_keys.items():
        actual = {
            (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        assert expected <= actual


def test_company_consistent_lineage_uses_composite_foreign_keys(engine: Engine) -> None:
    inspector = inspect(engine)
    accounting_snapshot_foreign_keys = inspector.get_foreign_keys("accounting_snapshot")
    detection_foreign_keys = inspector.get_foreign_keys("detection_record")

    assert any(
        foreign_key["constrained_columns"] == ["tax_master_version_id", "company_id"]
        and foreign_key["referred_table"] == "tax_master_version"
        and foreign_key["referred_columns"] == ["id", "company_id"]
        for foreign_key in accounting_snapshot_foreign_keys
    )
    assert any(
        foreign_key["constrained_columns"] == ["snapshot_id", "company_id", "tax_master_version_id"]
        and foreign_key["referred_table"] == "accounting_snapshot"
        and foreign_key["referred_columns"] == ["id", "company_id", "tax_master_version_id"]
        for foreign_key in detection_foreign_keys
    )


def test_quarterly_detection_schema_freezes_master_and_outcome_fields(engine: Engine) -> None:
    accounting_snapshot_columns = {
        column["name"]: column for column in inspect(engine).get_columns("accounting_snapshot")
    }
    detection_columns = {
        column["name"]: column for column in inspect(engine).get_columns("detection_record")
    }

    assert accounting_snapshot_columns["tax_master_version_id"]["nullable"] is False
    assert detection_columns["tax_master_version_id"]["nullable"] is False
    assert detection_columns["alert_code"]["nullable"] is True
    assert detection_columns["direction"]["nullable"] is True

    review_status_type = _column(engine, "review_action", "from_status")["type"]
    assert getattr(review_status_type, "name", None) == "risk_case_status"

    run_foreign_key = next(
        foreign_key
        for foreign_key in inspect(engine).get_foreign_keys("detection_record")
        if foreign_key["constrained_columns"] == ["run_id"]
    )
    assert run_foreign_key["options"].get("ondelete") == "RESTRICT"


def test_review_action_assignment_owner_is_nullable_and_action_scoped(
    engine: Engine,
) -> None:
    assignee = _column(engine, "review_action", "assignee")
    constraints = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints("review_action")
    }

    assert assignee["nullable"] is True
    assert getattr(assignee["type"], "length", None) == 256
    contract = constraints["ck_review_action_assignee_action"]
    assert "assignee IS NULL" in contract
    assert "action" in contract
    assert "ASSIGN" in contract
    assert "btrim" in contract


def test_quarterly_batch_company_state_has_retry_and_result_contracts(engine: Engine) -> None:
    columns = {
        column["name"]: column for column in inspect(engine).get_columns("monitoring_run_company")
    }
    assert {
        "run_id",
        "snapshot_set_id",
        "snapshot_set_member_id",
        "status",
        "attempt_count",
        "retryable",
        "celery_task_id",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
        "detection_ids",
        "case_ids",
    } <= columns.keys()
    assert isinstance(columns["detection_ids"]["type"], JSONB)
    assert isinstance(columns["case_ids"]["type"], JSONB)

    uniques = inspect(engine).get_unique_constraints("monitoring_run_company")
    assert any(unique["column_names"] == ["run_id", "snapshot_set_member_id"] for unique in uniques)
    index_columns = {
        tuple(index["column_names"])
        for index in inspect(engine).get_indexes("monitoring_run_company")
    }
    assert ("run_id", "status") in index_columns

    run_company_foreign_keys = inspect(engine).get_foreign_keys("monitoring_run_company")
    run_foreign_key = next(
        foreign_key
        for foreign_key in run_company_foreign_keys
        if foreign_key["constrained_columns"] == ["run_id", "snapshot_set_id"]
    )
    assert run_foreign_key["referred_table"] == "monitoring_run"
    assert run_foreign_key["referred_columns"] == ["id", "snapshot_set_id"]
    assert run_foreign_key["options"].get("ondelete") == "RESTRICT"

    member_foreign_key = next(
        foreign_key
        for foreign_key in run_company_foreign_keys
        if foreign_key["constrained_columns"] == ["snapshot_set_member_id", "snapshot_set_id"]
    )
    assert member_foreign_key["referred_table"] == "snapshot_set_member"
    assert member_foreign_key["referred_columns"] == ["id", "snapshot_set_id"]
    assert member_foreign_key["options"].get("ondelete") == "RESTRICT"

    run_uniques = inspect(engine).get_unique_constraints("monitoring_run")
    assert any(unique["column_names"] == ["id", "snapshot_set_id"] for unique in run_uniques)
    member_uniques = inspect(engine).get_unique_constraints("snapshot_set_member")
    assert any(unique["column_names"] == ["id", "snapshot_set_id"] for unique in member_uniques)


def test_quarterly_rule_seed_records_0004_migration_provenance(engine: Engine) -> None:
    with engine.connect() as connection:
        definition = connection.execute(
            text(
                """
                SELECT definition FROM rule_version
                WHERE rule_code = 'QUARTERLY_V1' AND version = 'phase-1-reviewed'
                """
            )
        ).scalar_one()

    assert definition["migration_provenance"] == {
        "revision": "0004_quarterly_detection",
        "seed": "QUARTERLY_V1:phase-1-reviewed",
    }


def test_deferred_tax_migration_seeds_reviewed_v2_and_preserves_v1(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        v2 = (
            connection.execute(
                text(
                    """
                SELECT status, effective_from, effective_to, definition,
                       change_reason, published_at, approved_by
                FROM rule_version
                WHERE rule_code = 'QUARTERLY_V2'
                  AND version = 'deferred-tax-reviewed'
                """
                )
            )
            .mappings()
            .one()
        )
        v1_count = connection.execute(
            text(
                "SELECT count(*) FROM rule_version "
                "WHERE rule_code = 'QUARTERLY_V1' "
                "AND version = 'phase-1-reviewed'"
            )
        ).scalar_one()

    definition = v2["definition"]
    manifest = definition["formula_manifest"]
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert v1_count == 1
    assert v2["status"] == "PUBLISHED"
    assert v2["effective_from"] == date(2000, 1, 1)
    assert v2["effective_to"] is None
    assert v2["published_at"] is not None
    assert v2["approved_by"] == "deferred-tax-business-rule-confirmation-2026-07-16"
    assert "deferred income tax accuracy" in v2["change_reason"]
    assert definition["review_status"] == "REVIEWED"
    assert definition["formula_manifest_sha256"] == sha256(canonical).hexdigest()
    assert definition["migration_provenance"] == {
        "revision": "0019_deferred_tax_accuracy",
        "seed": "QUARTERLY_V2:deferred-tax-reviewed",
    }
    assert manifest["schema_version"] == "QUARTERLY_V2"
    assert manifest["rounding_mode"] == "ROUND_HALF_UP"
    assert manifest["formulas"]["deferred_tax_base"] == ("loss_carryforward+cumulative_profit")
    assert manifest["formulas"]["current_year_deferred_tax_adjustment"] == (
        "system_cumulative_deferred_tax-sap_cumulative_deferred_tax_expense"
    )
    assert manifest["alert_boundaries"]["deferred_tax_accuracy"] == ("adjustment != 0")


def test_deferred_tax_formula_migration_seeds_reviewed_v3_and_preserves_v2(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        v3 = (
            connection.execute(
                text(
                    """
                SELECT status, effective_from, effective_to, definition,
                       change_reason, published_at, approved_by
                FROM rule_version
                WHERE rule_code = 'QUARTERLY_V3'
                  AND version = 'deferred-tax-loss-less-profit-reviewed'
                """
                )
            )
            .mappings()
            .one()
        )
        v2_count = connection.execute(
            text(
                "SELECT count(*) FROM rule_version "
                "WHERE rule_code = 'QUARTERLY_V2' "
                "AND version = 'deferred-tax-reviewed'"
            )
        ).scalar_one()

    definition = v3["definition"]
    manifest = definition["formula_manifest"]
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert v2_count == 1
    assert v3["status"] == "PUBLISHED"
    assert v3["effective_from"] == date(2000, 1, 1)
    assert v3["effective_to"] is None
    assert v3["published_at"] is not None
    assert v3["approved_by"] == "deferred-tax-business-rule-confirmation-2026-07-22"
    assert "loss carryforward less cumulative profit" in v3["change_reason"]
    assert definition["review_status"] == "REVIEWED"
    assert definition["formula_manifest_sha256"] == sha256(canonical).hexdigest()
    assert definition["migration_provenance"] == {
        "revision": "0021_deferred_tax_loss_less_profit",
        "seed": "QUARTERLY_V3:deferred-tax-loss-less-profit-reviewed",
    }
    assert manifest["schema_version"] == "QUARTERLY_V3"
    assert manifest["formulas"]["deferred_tax_base"] == ("loss_carryforward-cumulative_profit")


def test_repositories_import_cleanly_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tax_risk.persistence.repositories import UnitOfWork; "
            "assert UnitOfWork.__name__ == 'UnitOfWork'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_models_import_registers_every_table_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tax_risk.persistence.models import Base; "
            f"expected = {EXPECTED_TABLES!r}; "
            "actual = set(Base.metadata.tables); "
            "assert actual == expected, f'expected {expected!r}, got {actual!r}'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "direct_import",
    [
        "from tax_risk.persistence.ingest_models import Company; "
        "from tax_risk.persistence.models import Base",
        "from tax_risk.persistence.repositories import UnitOfWork; "
        "from tax_risk.persistence.models import Base",
        "from tax_risk.db import Base",
    ],
    ids=["focused-model", "repositories", "db"],
)
def test_every_persistence_import_path_registers_complete_metadata(
    direct_import: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{direct_import}; "
            f"expected = {EXPECTED_TABLES!r}; "
            "actual = set(Base.metadata.tables); "
            "assert actual == expected, f'expected {expected!r}, got {actual!r}'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND_ROOT / "alembic.ini"),
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        env=os.environ | {"DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )


@contextmanager
def _owned_migration_schema() -> Iterator[tuple[str, Engine]]:
    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_pytest_{uuid4().hex}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    isolated_engine: Engine | None = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
            quoted = connection.dialect.identifier_preparer.quote(schema_name)
            connection.exec_driver_sql(f"COMMENT ON SCHEMA {quoted} IS '{PYTEST_SCHEMA_MARKER}'")
        database_url = base_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        ).render_as_string(hide_password=False)
        isolated_engine = create_engine(database_url, poolclass=NullPool)
        yield database_url, isolated_engine
    finally:
        if isolated_engine is not None:
            isolated_engine.dispose()
        with admin_engine.begin() as connection:
            marker = connection.execute(
                text(
                    """
                    SELECT obj_description(oid, 'pg_namespace')
                    FROM pg_namespace WHERE nspname = :schema_name
                    """
                ),
                {"schema_name": schema_name},
            ).scalar_one_or_none()
            assert marker == PYTEST_SCHEMA_MARKER
            connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


def _insert_pre_0003_tax_master(
    engine: Engine,
    *,
    loss: str,
    status: str,
    published_at_sql: str,
) -> None:
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:code, 'Legacy Company') RETURNING id
                """
            ),
            {"code": f"LEGACY-{uuid4().hex}"},
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'TAX_MASTER', :key, 'tax_master', 'SUCCEEDED', now(),
                    DATE '2026-03-31', 'FULL', '1', 'CNY', 2,
                    1, 1, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"legacy-{uuid4().hex}"},
        ).scalar_one()
        connection.execute(
            text(
                f"""
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, data, published_at
                ) VALUES (
                    :company_id, :batch_id, DATE '2026-01-01', 'legacy-v1', :status,
                    0.25, :loss, 0.10, 'CNY', 2, '{{}}'::jsonb, {published_at_sql}
                )
                """
            ),
            {
                "company_id": company_id,
                "batch_id": batch_id,
                "status": status,
                "loss": loss,
            },
        )


def _insert_pre_0004_tax_burden_detections(engine: Engine) -> tuple[object, object, object]:
    """Seed the legacy 0003 shape, including a historically incomplete burden row."""

    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                """
                INSERT INTO company (company_code, company_name)
                VALUES (:code, 'Legacy Quarterly Company') RETURNING id
                """
            ),
            {"code": f"LEGACY-Q-{uuid4().hex}"},
        ).scalar_one()
        batch_id = connection.execute(
            text(
                """
                INSERT INTO ingest_batch (
                    source, source_batch_key, dataset_code, status, extraction_time,
                    period, mode, schema_version, currency, amount_scale,
                    record_count, accepted_count, rejected_count, control_total, checksum
                ) VALUES (
                    'TAX_MASTER', :key, 'tax_master', 'SUCCEEDED', now(),
                    DATE '2026-03-31', 'FULL', '1', 'CNY', 2,
                    1, 1, 0, 0, repeat('a', 64)
                ) RETURNING id
                """
            ),
            {"key": f"legacy-quarterly-{uuid4().hex}"},
        ).scalar_one()
        master_id = connection.execute(
            text(
                """
                INSERT INTO tax_master_version (
                    company_id, source_batch_id, valid_from, version, status,
                    tax_rate, loss_carryforward, average_tax_burden_rate_3y,
                    currency, amount_scale, data, published_at, approved_by,
                    source_row_number, uploaded_by
                ) VALUES (
                    :company_id, :batch_id, DATE '2026-01-01', 'legacy-quarterly-v1',
                    'PUBLISHED', 0.25, 0, 0.10, 'CNY', 2, '{}'::jsonb, now(),
                    'legacy-reviewer', 2, 'legacy-uploader'
                ) RETURNING id
                """
            ),
            {"company_id": company_id, "batch_id": batch_id},
        ).scalar_one()
        snapshot_id = connection.execute(
            text(
                """
                INSERT INTO accounting_snapshot (
                    company_id, tax_master_version_id, period, source_version_set_hash,
                    status, currency, amount_scale, record_count, control_total,
                    checksum, published_at
                ) VALUES (
                    :company_id, :master_id, DATE '2026-03-31', repeat('b', 64),
                    'PUBLISHED', 'CNY', 2, 1, 0, repeat('c', 64), now()
                ) RETURNING id
                """
            ),
            {"company_id": company_id, "master_id": master_id},
        ).scalar_one()
        snapshot_set_id = connection.execute(
            text(
                """
                INSERT INTO snapshot_set (
                    set_key, period, status, expected_member_count
                ) VALUES (
                    :key, DATE '2026-03-31', 'DRAFT', 100
                ) RETURNING id
                """
            ),
            {"key": f"legacy-set-{uuid4().hex}"},
        ).scalar_one()
        rule_id = connection.execute(
            text(
                """
                INSERT INTO rule_version (
                    rule_code, version, status, effective_from, definition,
                    change_reason, published_at
                ) VALUES (
                    'LEGACY_QUARTERLY', 'v1', 'PUBLISHED', DATE '2026-01-01',
                    '{}'::jsonb, 'legacy migration fixture', now()
                ) RETURNING id
                """
            )
        ).scalar_one()
        run_id = connection.execute(
            text(
                """
                INSERT INTO monitoring_run (
                    run_key, run_type, snapshot_set_id, rule_version_id, status,
                    fiscal_year, quarter, requested_company_count
                ) VALUES (
                    :key, 'QUARTERLY', :snapshot_set_id, :rule_id, 'SUCCEEDED',
                    2026, 1, 1
                ) RETURNING id
                """
            ),
            {
                "key": f"legacy-run-{uuid4().hex}",
                "snapshot_set_id": snapshot_set_id,
                "rule_id": rule_id,
            },
        ).scalar_one()

        detection_ids: list[object] = []
        for key_suffix, result_amount, difference_amount, direction in (
            ("complete", "0.120000000000", "0.060000000000", "HIGH"),
            ("missing-deviation", "0.110000000000", None, "HIGH"),
        ):
            detection_ids.append(
                connection.execute(
                    text(
                        """
                        INSERT INTO detection_record (
                            detection_key, run_id, company_id, snapshot_id,
                            rule_version_id, tax_master_version_id, monitor_type,
                            calculation_status, input_amount, result_amount,
                            difference_amount, rate_value, currency, amount_scale,
                            formula_substitution, lineage, structured_output,
                            alert_code, direction
                        ) VALUES (
                            :key, :run_id, :company_id, :snapshot_id, :rule_id,
                            :master_id, 'TAX_BURDEN', 'CALCULATED', 100,
                            :result_amount, :difference_amount, 0.25, 'CNY', 2,
                            '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                            'TAX_BURDEN_DEVIATION', :direction
                        ) RETURNING id
                        """
                    ),
                    {
                        "key": f"legacy-burden-{key_suffix}-{uuid4().hex}",
                        "run_id": run_id,
                        "company_id": company_id,
                        "snapshot_id": snapshot_id,
                        "rule_id": rule_id,
                        "master_id": master_id,
                        "result_amount": result_amount,
                        "difference_amount": difference_amount,
                        "direction": direction,
                    },
                ).scalar_one()
            )

    return run_id, detection_ids[0], detection_ids[1]


def _reference_pre_0004_detection_from_risk_case(
    engine: Engine,
    detection_id: object,
) -> object:
    with engine.begin() as connection:
        company_id = connection.execute(
            text("SELECT company_id FROM detection_record WHERE id = :detection_id"),
            {"detection_id": detection_id},
        ).scalar_one()
        return connection.execute(
            text(
                """
                INSERT INTO risk_case (
                    fingerprint, company_id, latest_detection_id, monitor_type,
                    status, risk_amount, currency, amount_scale, risk_direction,
                    priority, lineage
                ) VALUES (
                    :fingerprint, :company_id, :detection_id, 'TAX_BURDEN',
                    'NEW', 0.05, 'CNY', 2, 'HIGH', 3, '{}'::jsonb
                ) RETURNING id
                """
            ),
            {
                "fingerprint": f"legacy-burden-case-{uuid4().hex}",
                "company_id": company_id,
                "detection_id": detection_id,
            },
        ).scalar_one()


def test_alembic_current_accepts_a_percent_encoded_database_url(
    isolated_database_url: str,
) -> None:
    encoded_url = (
        make_url(isolated_database_url)
        .update_query_dict({"application_name": "tax risk"})
        .render_as_string(hide_password=False)
        .replace("application_name=tax+risk", "application_name=tax%20risk")
    )
    assert "application_name=tax%20risk" in encoded_url
    completed = _run_alembic(encoded_url, "current")

    assert completed.returncode == 0, completed.stderr
    assert "0023_refund_ambiguous_match_alert (head)" in completed.stdout


def test_alembic_check_and_round_trip_stay_in_the_isolated_schema() -> None:
    with _owned_migration_schema() as (database_url, _engine):
        upgrade_first = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_first.returncode == 0, upgrade_first.stderr

        before = _run_alembic(database_url, "check")
        assert before.returncode == 0, before.stderr

        downgrade = _run_alembic(database_url, "downgrade", "base")
        assert downgrade.returncode == 0, downgrade.stderr

        upgrade = _run_alembic(database_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr

        after = _run_alembic(database_url, "check")
        assert after.returncode == 0, after.stderr


def test_database_is_at_current_schema_revision(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0023_refund_ambiguous_match_alert"


def test_0022_accepts_taxes_payable_evidence_and_blocks_unsafe_downgrade() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade = _run_alembic(database_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        source_batch_key = f"refund-taxes-payable-{uuid4().hex}"
        with isolated_engine.begin() as connection:
            company_id = connection.execute(
                text(
                    "INSERT INTO company (company_code, company_name, lifecycle) "
                    "VALUES (:code, 'Taxes Payable Migration', 'ACTIVE') RETURNING id"
                ),
                {"code": f"REFUND-MIGRATION-{uuid4().hex}"},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO sap_refund_evidence_batch ("
                    "source_batch_key, fiscal_year, through_period, company_ids, "
                    "status, record_count, checksum"
                    ") VALUES ("
                    ":source_batch_key, 2041, DATE '2041-03-31', "
                    "CAST(:company_ids AS jsonb), 'COMPLETE', 1, :checksum"
                    ")"
                ),
                {
                    "source_batch_key": source_batch_key,
                    "company_ids": json.dumps([str(company_id)]),
                    "checksum": "a" * 64,
                },
            )
            taxes_payable_line_id = connection.execute(
                text(
                    "INSERT INTO sap_gl_line_observation ("
                    "company_id, source_batch_key, client, ledger, fiscal_year, "
                    "fiscal_period, posting_date, document_number, line_item, "
                    "gl_account_code, gl_account_name, account_category, debit_credit, "
                    "amount, currency, amount_scale, is_reversed, source_hash"
                    ") VALUES ("
                    ":company_id, :source_batch_key, '800', '0L', 2041, 3, "
                    "DATE '2041-03-15', '990001', '001', '2221130000', "
                    "'应交税费-企业所得税', 'TAXES_PAYABLE', 'CREDIT', "
                    "100.00, 'CNY', 2, false, :source_hash"
                    ") RETURNING id"
                ),
                {
                    "company_id": company_id,
                    "source_batch_key": source_batch_key,
                    "source_hash": "b" * 64,
                },
            ).scalar_one()

        downgrade = _run_alembic(
            database_url,
            "downgrade",
            "0021_deferred_tax_loss_less_profit",
        )

        assert downgrade.returncode != 0
        assert "REFUND_TAXES_PAYABLE_DOWNGRADE_BLOCKED" in downgrade.stderr
        with isolated_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            retained_line = connection.execute(
                text("SELECT id FROM sap_gl_line_observation WHERE id = :id"),
                {"id": taxes_payable_line_id},
            ).scalar_one()
        assert revision == "0023_refund_ambiguous_match_alert"
        assert retained_line == taxes_payable_line_id


def test_0019_refuses_conflicting_v2_rule_seed_without_overwriting_it() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0018 = _run_alembic(
            database_url,
            "upgrade",
            "0018_nonpositive_revenue_tax_burden",
        )
        assert upgrade_0018.returncode == 0, upgrade_0018.stderr
        forged_definition = {"owner": "preexisting", "formula": "forged"}
        with isolated_engine.begin() as connection:
            forged_id = connection.execute(
                text(
                    """
                    INSERT INTO rule_version (
                        rule_code, version, status, effective_from, definition,
                        change_reason, published_at, approved_by
                    ) VALUES (
                        'QUARTERLY_V2', 'deferred-tax-reviewed', 'PUBLISHED',
                        DATE '1999-01-01', CAST(:definition AS jsonb),
                        'preexisting forged row', now(), 'preexisting-owner'
                    ) RETURNING id
                    """
                ),
                {"definition": json.dumps(forged_definition)},
            ).scalar_one()

        upgrade_0019 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0019.returncode != 0
        with isolated_engine.connect() as connection:
            preserved = (
                connection.execute(
                    text(
                        """
                    SELECT id, definition, change_reason FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V2'
                      AND version = 'deferred-tax-reviewed'
                    """
                    )
                )
                .mappings()
                .one()
            )
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        columns = {
            column["name"] for column in inspect(isolated_engine).get_columns("tax_master_version")
        }
        assert preserved == {
            "id": forged_id,
            "definition": forged_definition,
            "change_reason": "preexisting forged row",
        }
        assert revision == "0018_nonpositive_revenue_tax_burden"
        assert "deferred_tax_rate" not in columns
        assert "DEFERRED_TAX_ACCURACY" not in _enum_labels(
            isolated_engine,
            "monitor_type",
        )


def test_0019_and_0020_downgrade_reupgrade_keep_additive_enums_idempotent() -> None:
    refund_tables = {
        "income_tax_refund_scan_result",
        "income_tax_refund_target",
        "income_tax_refund_writeback",
        "sap_gl_line_observation",
        "sap_refund_evidence_batch",
    }
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade = _run_alembic(database_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr

        downgrade = _run_alembic(
            database_url,
            "downgrade",
            "0018_nonpositive_revenue_tax_burden",
        )
        assert downgrade.returncode == 0, downgrade.stderr
        with isolated_engine.connect() as connection:
            remaining_v2 = connection.execute(
                text(
                    "SELECT count(*) FROM rule_version "
                    "WHERE rule_code = 'QUARTERLY_V2' "
                    "AND version = 'deferred-tax-reviewed'"
                )
            ).scalar_one()
        downgraded_columns = {
            column["name"] for column in inspect(isolated_engine).get_columns("tax_master_version")
        }
        assert remaining_v2 == 0
        assert "deferred_tax_rate" not in downgraded_columns
        assert _enum_labels(isolated_engine, "monitor_type").count("DEFERRED_TAX_ACCURACY") == 1
        assert (
            _enum_labels(isolated_engine, "monitor_type").count(
                "INCOME_TAX_REFUND_ACCOUNT_ACCURACY"
            )
            == 1
        )
        assert not refund_tables.intersection(inspect(isolated_engine).get_table_names())

        reupgrade = _run_alembic(database_url, "upgrade", "head")
        assert reupgrade.returncode == 0, reupgrade.stderr
        with isolated_engine.connect() as connection:
            restored_v2 = connection.execute(
                text(
                    "SELECT count(*) FROM rule_version "
                    "WHERE rule_code = 'QUARTERLY_V2' "
                    "AND version = 'deferred-tax-reviewed'"
                )
            ).scalar_one()
        restored_columns = {
            column["name"] for column in inspect(isolated_engine).get_columns("tax_master_version")
        }
        assert restored_v2 == 1
        assert "deferred_tax_rate" in restored_columns
        assert _enum_labels(isolated_engine, "monitor_type").count("DEFERRED_TAX_ACCURACY") == 1
        assert (
            _enum_labels(isolated_engine, "monitor_type").count(
                "INCOME_TAX_REFUND_ACCOUNT_ACCURACY"
            )
            == 1
        )
        assert refund_tables <= set(inspect(isolated_engine).get_table_names())


def test_0019_downgrade_refuses_a_referenced_v2_rule() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade = _run_alembic(database_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        with isolated_engine.begin() as connection:
            rule_id = connection.execute(
                text(
                    "SELECT id FROM rule_version "
                    "WHERE rule_code = 'QUARTERLY_V2' "
                    "AND version = 'deferred-tax-reviewed'"
                )
            ).scalar_one()
            snapshot_set_id = connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set (
                        set_key, period, status, expected_member_count
                    ) VALUES (
                        :key, DATE '2026-06-30', 'DRAFT', 100
                    ) RETURNING id
                    """
                ),
                {"key": f"deferred-tax-downgrade-{uuid4().hex}"},
            ).scalar_one()
            run_id = connection.execute(
                text(
                    """
                    INSERT INTO monitoring_run (
                        run_key, run_type, snapshot_set_id, rule_version_id, status,
                        fiscal_year, quarter, requested_company_count
                    ) VALUES (
                        :key, 'QUARTERLY', :snapshot_set_id, :rule_id, 'PENDING',
                        2026, 2, 0
                    ) RETURNING id
                    """
                ),
                {
                    "key": f"deferred-tax-downgrade-run-{uuid4().hex}",
                    "snapshot_set_id": snapshot_set_id,
                    "rule_id": rule_id,
                },
            ).scalar_one()

        downgrade = _run_alembic(
            database_url,
            "downgrade",
            "0018_nonpositive_revenue_tax_burden",
        )

        assert downgrade.returncode != 0
        assert "DEFERRED_TAX_DOWNGRADE_BLOCKED" in downgrade.stderr
        with isolated_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            retained_run = connection.execute(
                text("SELECT id FROM monitoring_run WHERE id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
        assert revision == "0023_refund_ambiguous_match_alert"
        assert retained_run == run_id
        assert "deferred_tax_rate" in {
            column["name"] for column in inspect(isolated_engine).get_columns("tax_master_version")
        }


def test_0004_migrates_dataful_legacy_tax_burden_rows_safely() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0003 = _run_alembic(
            database_url,
            "upgrade",
            "0003_tax_master_governance",
        )
        assert upgrade_0003.returncode == 0, upgrade_0003.stderr
        run_id, complete_id, missing_deviation_id = _insert_pre_0004_tax_burden_detections(
            isolated_engine
        )

        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0004.returncode == 0, upgrade_0004.stderr
        with isolated_engine.connect() as connection:
            migrated = {
                row["id"]: row
                for row in connection.execute(
                    text(
                        """
                        SELECT id, calculation_status, result_amount, difference_amount,
                               tax_burden_rate, tax_burden_deviation,
                               not_calculated_reason, alert_code, direction
                        FROM detection_record
                        WHERE id IN (:complete_id, :missing_deviation_id)
                        """
                    ),
                    {
                        "complete_id": complete_id,
                        "missing_deviation_id": missing_deviation_id,
                    },
                ).mappings()
            }

        assert migrated[complete_id] == {
            "id": complete_id,
            "calculation_status": "CALCULATED",
            "result_amount": None,
            "difference_amount": None,
            "tax_burden_rate": Decimal("0.120000000000"),
            "tax_burden_deviation": Decimal("0.060000000000"),
            "not_calculated_reason": None,
            "alert_code": "TAX_BURDEN_DEVIATION",
            "direction": "HIGH",
        }
        assert migrated[missing_deviation_id] == {
            "id": missing_deviation_id,
            "calculation_status": "FAILED",
            "result_amount": None,
            "difference_amount": None,
            "tax_burden_rate": None,
            "tax_burden_deviation": None,
            "not_calculated_reason": "LEGACY_TAX_BURDEN_DEVIATION_MISSING",
            "alert_code": None,
            "direction": None,
        }

        upgraded_run_foreign_key = next(
            foreign_key
            for foreign_key in inspect(isolated_engine).get_foreign_keys("detection_record")
            if foreign_key["constrained_columns"] == ["run_id"]
        )
        assert upgraded_run_foreign_key["options"].get("ondelete") == "RESTRICT"

        with isolated_engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(IntegrityError, match="detection_record_run_id_fkey"):
                connection.execute(
                    text("DELETE FROM monitoring_run WHERE id = :run_id"),
                    {"run_id": run_id},
                )
            transaction.rollback()

        downgrade_0003 = _run_alembic(
            database_url,
            "downgrade",
            "0003_tax_master_governance",
        )
        assert downgrade_0003.returncode == 0, downgrade_0003.stderr
        downgraded_run_foreign_key = next(
            foreign_key
            for foreign_key in inspect(isolated_engine).get_foreign_keys("detection_record")
            if foreign_key["constrained_columns"] == ["run_id"]
        )
        assert downgraded_run_foreign_key["options"].get("ondelete") == "CASCADE"
        with isolated_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM monitoring_run WHERE id = :run_id"),
                {"run_id": run_id},
            )
            remaining = connection.execute(
                text(
                    "SELECT count(*) FROM detection_record "
                    "WHERE id IN (:complete_id, :missing_deviation_id)"
                ),
                {
                    "complete_id": complete_id,
                    "missing_deviation_id": missing_deviation_id,
                },
            ).scalar_one()
        assert remaining == 0


def test_0004_refuses_incomplete_legacy_burden_evidence_referenced_by_case() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0003 = _run_alembic(
            database_url,
            "upgrade",
            "0003_tax_master_governance",
        )
        assert upgrade_0003.returncode == 0, upgrade_0003.stderr
        _, _, incomplete_detection_id = _insert_pre_0004_tax_burden_detections(isolated_engine)
        case_id = _reference_pre_0004_detection_from_risk_case(
            isolated_engine,
            incomplete_detection_id,
        )
        with isolated_engine.connect() as connection:
            before_detection = (
                connection.execute(
                    text(
                        """
                    SELECT calculation_status, result_amount, difference_amount,
                           not_calculated_reason, alert_code, direction
                    FROM detection_record WHERE id = :detection_id
                    """
                    ),
                    {"detection_id": incomplete_detection_id},
                )
                .mappings()
                .one()
            )
            before_case = (
                connection.execute(
                    text(
                        """
                    SELECT latest_detection_id, status, risk_amount, risk_direction
                    FROM risk_case WHERE id = :case_id
                    """
                    ),
                    {"case_id": case_id},
                )
                .mappings()
                .one()
            )

        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0004.returncode != 0
        assert "LEGACY_TAX_BURDEN_CASE_EVIDENCE_INCOMPLETE" in upgrade_0004.stderr
        with isolated_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            after_detection = (
                connection.execute(
                    text(
                        """
                    SELECT calculation_status, result_amount, difference_amount,
                           not_calculated_reason, alert_code, direction
                    FROM detection_record WHERE id = :detection_id
                    """
                    ),
                    {"detection_id": incomplete_detection_id},
                )
                .mappings()
                .one()
            )
            after_case = (
                connection.execute(
                    text(
                        """
                    SELECT latest_detection_id, status, risk_amount, risk_direction
                    FROM risk_case WHERE id = :case_id
                    """
                    ),
                    {"case_id": case_id},
                )
                .mappings()
                .one()
            )

        assert revision == "0003_tax_master_governance"
        assert (
            after_detection
            == before_detection
            == {
                "calculation_status": "CALCULATED",
                "result_amount": Decimal("0.110000000000"),
                "difference_amount": None,
                "not_calculated_reason": None,
                "alert_code": "TAX_BURDEN_DEVIATION",
                "direction": "HIGH",
            }
        )
        assert (
            after_case
            == before_case
            == {
                "latest_detection_id": incomplete_detection_id,
                "status": "NEW",
                "risk_amount": Decimal("0.050000000000"),
                "risk_direction": "HIGH",
            }
        )


def test_0004_refuses_conflicting_fixed_rule_seed_without_overwriting_it() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0003 = _run_alembic(
            database_url,
            "upgrade",
            "0003_tax_master_governance",
        )
        assert upgrade_0003.returncode == 0, upgrade_0003.stderr
        forged_definition = {"owner": "preexisting", "formula": "forged"}
        with isolated_engine.begin() as connection:
            forged_id = connection.execute(
                text(
                    """
                    INSERT INTO rule_version (
                        rule_code, version, status, effective_from, definition,
                        change_reason, published_at, approved_by
                    ) VALUES (
                        'QUARTERLY_V1', 'phase-1-reviewed', 'PUBLISHED',
                        DATE '1999-01-01', CAST(:definition AS jsonb),
                        'preexisting forged row', now(), 'preexisting-owner'
                    ) RETURNING id
                    """
                ),
                {"definition": json.dumps(forged_definition)},
            ).scalar_one()

        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0004.returncode != 0
        with isolated_engine.connect() as connection:
            preserved = (
                connection.execute(
                    text(
                        """
                    SELECT id, definition, change_reason FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    """
                    )
                )
                .mappings()
                .one()
            )
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert preserved == {
            "id": forged_id,
            "definition": forged_definition,
            "change_reason": "preexisting forged row",
        }
        assert revision == "0003_tax_master_governance"


def test_0004_downgrade_preserves_same_key_row_without_its_provenance() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_0004.returncode == 0, upgrade_0004.stderr
        downgrade_0017 = _run_alembic(
            database_url,
            "downgrade",
            "0017_strict_rls_runtime",
        )
        assert downgrade_0017.returncode == 0, downgrade_0017.stderr
        preexisting_definition = {"owner": "preexisting-after-upgrade"}
        with isolated_engine.begin() as connection:
            fixed_rule_id = connection.execute(
                text(
                    """
                    UPDATE rule_version
                    SET definition = CAST(:definition AS jsonb),
                        change_reason = 'preexisting replacement'
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    RETURNING id
                    """
                ),
                {"definition": json.dumps(preexisting_definition)},
            ).scalar_one()

        downgrade_0003 = _run_alembic(
            database_url,
            "downgrade",
            "0003_tax_master_governance",
        )

        assert downgrade_0003.returncode == 0, downgrade_0003.stderr
        with isolated_engine.connect() as connection:
            preserved = (
                connection.execute(
                    text(
                        """
                    SELECT id, definition, change_reason FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    """
                    )
                )
                .mappings()
                .one()
            )
        assert preserved == {
            "id": fixed_rule_id,
            "definition": preexisting_definition,
            "change_reason": "preexisting replacement",
        }


def test_0004_downgrade_deletes_its_unreferenced_rule_seed() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_0004.returncode == 0, upgrade_0004.stderr

        downgrade_0003 = _run_alembic(
            database_url,
            "downgrade",
            "0003_tax_master_governance",
        )

        assert downgrade_0003.returncode == 0, downgrade_0003.stderr
        with isolated_engine.connect() as connection:
            remaining = connection.execute(
                text(
                    """
                    SELECT count(*) FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    """
                )
            ).scalar_one()
        assert remaining == 0


def test_0004_reuses_its_referenced_seed_after_downgrade_and_reupgrade() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0004 = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_0004.returncode == 0, upgrade_0004.stderr
        with isolated_engine.begin() as connection:
            fixed_rule_id = connection.execute(
                text(
                    """
                    SELECT id FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    """
                )
            ).scalar_one()
            snapshot_set_id = connection.execute(
                text(
                    """
                    INSERT INTO snapshot_set (
                        set_key, period, status, expected_member_count
                    ) VALUES (
                        :key, DATE '2026-03-31', 'DRAFT', 100
                    ) RETURNING id
                    """
                ),
                {"key": f"referenced-seed-{uuid4().hex}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO monitoring_run (
                        run_key, run_type, snapshot_set_id, rule_version_id, status,
                        fiscal_year, quarter, requested_company_count
                    ) VALUES (
                        :key, 'QUARTERLY', :snapshot_set_id, :rule_id, 'PENDING',
                        2026, 1, 0
                    )
                    """
                ),
                {
                    "key": f"referenced-seed-run-{uuid4().hex}",
                    "snapshot_set_id": snapshot_set_id,
                    "rule_id": fixed_rule_id,
                },
            )

        downgrade_0003 = _run_alembic(
            database_url,
            "downgrade",
            "0003_tax_master_governance",
        )
        assert downgrade_0003.returncode == 0, downgrade_0003.stderr
        reupgrade_0004 = _run_alembic(database_url, "upgrade", "head")

        assert reupgrade_0004.returncode == 0, reupgrade_0004.stderr
        with isolated_engine.connect() as connection:
            retained_ids = (
                connection.execute(
                    text(
                        """
                    SELECT id FROM rule_version
                    WHERE rule_code = 'QUARTERLY_V1'
                      AND version = 'phase-1-reviewed'
                    """
                    )
                )
                .scalars()
                .all()
            )
        assert retained_ids == [fixed_rule_id]


def test_0003_backfills_legacy_approval_audit_then_removes_insert_defaults() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0002 = _run_alembic(
            database_url,
            "upgrade",
            "0002_company_master_freshness",
        )
        assert upgrade_0002.returncode == 0, upgrade_0002.stderr
        _insert_pre_0003_tax_master(
            isolated_engine,
            loss="0",
            status="PUBLISHED",
            published_at_sql="now()",
        )

        upgrade_0003 = _run_alembic(database_url, "upgrade", "head")
        assert upgrade_0003.returncode == 0, upgrade_0003.stderr
        with isolated_engine.connect() as connection:
            governed = (
                connection.execute(
                    text(
                        """
                    SELECT approved_by, uploaded_by, source_row_number
                    FROM tax_master_version WHERE version = 'legacy-v1'
                    """
                    )
                )
                .mappings()
                .one()
            )
        columns = {
            column["name"]: column
            for column in inspect(isolated_engine).get_columns("tax_master_version")
        }

        assert governed == {
            "approved_by": "legacy-migration",
            "uploaded_by": "legacy-migration",
            "source_row_number": 2,
        }
        assert columns["uploaded_by"]["default"] is None
        assert columns["source_row_number"]["default"] is None

        downgrade = _run_alembic(
            database_url,
            "downgrade",
            "0002_company_master_freshness",
        )
        assert downgrade.returncode == 0, downgrade.stderr
        reupgrade = _run_alembic(database_url, "upgrade", "head")
        assert reupgrade.returncode == 0, reupgrade.stderr


def test_0003_refuses_historical_negative_loss_without_modifying_it() -> None:
    with _owned_migration_schema() as (database_url, isolated_engine):
        upgrade_0002 = _run_alembic(
            database_url,
            "upgrade",
            "0002_company_master_freshness",
        )
        assert upgrade_0002.returncode == 0, upgrade_0002.stderr
        _insert_pre_0003_tax_master(
            isolated_engine,
            loss="-0.01",
            status="DRAFT",
            published_at_sql="NULL",
        )

        upgrade_0003 = _run_alembic(database_url, "upgrade", "head")

        assert upgrade_0003.returncode != 0
        with isolated_engine.connect() as connection:
            loss = connection.execute(
                text("SELECT loss_carryforward FROM tax_master_version WHERE version = 'legacy-v1'")
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert loss == Decimal("-0.010000000000")
        assert revision == "0002_company_master_freshness"


def test_0002_backfills_existing_company_from_historical_lifecycle_timestamp() -> None:
    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_pytest_{uuid4().hex}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
            quoted = connection.dialect.identifier_preparer.quote(schema_name)
            connection.exec_driver_sql(f"COMMENT ON SCHEMA {quoted} IS '{PYTEST_SCHEMA_MARKER}'")
        database_url = base_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        ).render_as_string(hide_password=False)
        first_upgrade = _run_alembic(database_url, "upgrade", "0001_control_plane")
        assert first_upgrade.returncode == 0, first_upgrade.stderr
        isolated_engine = create_engine(database_url, poolclass=NullPool)
        try:
            with isolated_engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO company (
                            company_code, company_name, lifecycle_changed_at,
                            created_at, updated_at
                        ) VALUES (
                            'HISTORICAL', 'Historical Company',
                            TIMESTAMPTZ '2020-02-03 04:05:06+00',
                            TIMESTAMPTZ '2019-01-01 00:00:00+00',
                            TIMESTAMPTZ '2021-01-01 00:00:00+00'
                        )
                        """
                    )
                )
            second_upgrade = _run_alembic(database_url, "upgrade", "head")
            assert second_upgrade.returncode == 0, second_upgrade.stderr
            with isolated_engine.connect() as connection:
                backfilled = connection.execute(
                    text(
                        "SELECT master_data_updated_at FROM company "
                        "WHERE company_code = 'HISTORICAL'"
                    )
                ).scalar_one()
            assert backfilled.isoformat() == "2020-02-03T04:05:06+00:00"

            downgrade = _run_alembic(database_url, "downgrade", "0001_control_plane")
            assert downgrade.returncode == 0, downgrade.stderr
            assert "master_data_updated_at" not in {
                column["name"] for column in inspect(isolated_engine).get_columns("company")
            }
            final_upgrade = _run_alembic(database_url, "upgrade", "head")
            assert final_upgrade.returncode == 0, final_upgrade.stderr
        finally:
            isolated_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            marker = connection.execute(
                text(
                    """
                    SELECT obj_description(oid, 'pg_namespace')
                    FROM pg_namespace WHERE nspname = :schema_name
                    """
                ),
                {"schema_name": schema_name},
            ).scalar_one_or_none()
            assert marker == PYTEST_SCHEMA_MARKER
            connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
