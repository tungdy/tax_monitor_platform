from __future__ import annotations

from sqlalchemy import Engine, inspect, text


def _enum_labels(engine: Engine, enum_name: str) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    """
                    SELECT enum_value.enumlabel
                    FROM pg_enum AS enum_value
                    JOIN pg_type AS enum_type ON enum_type.oid = enum_value.enumtypid
                    JOIN pg_namespace AS namespace ON namespace.oid = enum_type.typnamespace
                    WHERE enum_type.typname = :enum_name
                      AND namespace.nspname = current_schema()
                    ORDER BY enum_value.enumsortorder
                    """
                ),
                {"enum_name": enum_name},
            ).scalars()
        )


def test_phase_3_extends_the_existing_control_plane(engine: Engine) -> None:
    inspector = inspect(engine)
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0024_align_check_constraint_names"
    assert "semantic_version_set" in inspector.get_table_names()
    assert {
        "WELFARE",
        "DONATION",
        "DEFERRED_TAX_ACCURACY",
        "INCOME_TAX_REFUND_ACCOUNT_ACCURACY",
    } <= set(
        _enum_labels(engine, "monitor_type")
    )
    assert "MONTHLY_SEMANTIC" in _enum_labels(engine, "monitoring_run_type")
    assert "NOT_RUN" in _enum_labels(engine, "monitoring_run_company_status")

    run_columns = {column["name"] for column in inspector.get_columns("monitoring_run")}
    assert {"period", "monitoring_type", "semantic_version_set_id"} <= run_columns
    company_columns = {
        column["name"] for column in inspector.get_columns("monitoring_run_company")
    }
    assert {
        "selected",
        "adjustment_amount",
        "processed_line_count",
        "risk_case_count",
        "issue_code",
    } <= company_columns

    run_checks = " ".join(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("monitoring_run")
    )
    assert "MONTHLY_SEMANTIC" in run_checks
    assert "semantic_version_set_id IS NOT NULL" in run_checks

    foreign_keys = inspector.get_foreign_keys("monitoring_run")
    assert any(
        key["referred_table"] == "semantic_version_set"
        and key["constrained_columns"] == ["semantic_version_set_id"]
        for key in foreign_keys
    )


def test_existing_quarterly_run_contract_remains_insertable(engine: Engine) -> None:
    with engine.begin() as connection:
        snapshot_set_id = connection.execute(
            text(
                "INSERT INTO snapshot_set "
                "(set_key, period, status, expected_member_count) "
                "VALUES (:key, '2026-06-30', 'DRAFT', 100) RETURNING id"
            ),
            {"key": "phase-3-quarterly-compatibility"},
        ).scalar_one()
        rule_id = connection.execute(
            text(
                "INSERT INTO rule_version "
                "(rule_code, version, status, effective_from, effective_to, definition, "
                "change_reason, published_at, approved_by) "
                "VALUES ('PHASE-3-COMPAT', 'v1', 'PUBLISHED', '2026-01-01', "
                "'2026-12-31', '{}'::jsonb, 'compatibility test', now(), 'tester') "
                "RETURNING id"
            )
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO monitoring_run "
                "(run_key, run_type, snapshot_set_id, rule_version_id, status, "
                "fiscal_year, quarter, requested_company_count) "
                "VALUES ('phase-3-quarterly-compatible', 'QUARTERLY', :set_id, :rule_id, "
                "'PENDING', 2026, 2, 100)"
            ),
            {"set_id": snapshot_set_id, "rule_id": rule_id},
        )


def test_phase_3_monitoring_type_is_persisted_with_semantic_detection(engine: Engine) -> None:
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("semantic_detection_record")
    }
    assert "monitoring_type" in columns
