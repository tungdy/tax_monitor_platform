"""Align persisted check-constraint names with ORM metadata.

Revision ID: 0024_align_check_constraint_names
Revises: 0023_refund_ambiguous_match_alert
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_align_check_constraint_names"
down_revision: str | None = "0023_refund_ambiguous_match_alert"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ConstraintRename = tuple[str, str, str]

RENAMES: tuple[ConstraintRename, ...] = (
    (
        "accounting_snapshot",
        "ck_accounting_snapshot_checksum",
        "ck_accounting_snapshot_checksum_length",
    ),
    (
        "accounting_snapshot",
        "ck_accounting_snapshot_source_hash",
        "ck_accounting_snapshot_source_version_hash_length",
    ),
    (
        "business_entertainment_scope_version",
        "ck_business_entertainment_scope_version_ck_business_ent_3062",
        "ck_business_entertainment_scope_version_file_checksum_length",
    ),
    (
        "business_entertainment_scope_version",
        "ck_business_entertainment_scope_version_ck_business_ent_c386",
        "ck_business_entertainment_scope_version_status_audit",
    ),
    (
        "business_entertainment_scope_version",
        "ck_business_entertainment_scope_version_ck_business_ent_c62a",
        "ck_business_entertainment_scope_version_effective_period",
    ),
    (
        "business_entertainment_source_observation",
        "ck_business_entertainment_source_observation_amount_currency_pa",
        "ck_business_entertainment_source_observation_amount_currency",
    ),
    (
        "detection_record",
        "ck_detection_record_rate",
        "ck_detection_record_rate_value",
    ),
    (
        "monitoring_run",
        "ck_monitoring_run_ck_monitoring_run_batch_finished_terminal",
        "ck_monitoring_run_batch_finished_terminal",
    ),
    (
        "monitoring_run",
        "ck_monitoring_run_ck_monitoring_run_output_ready_success",
        "ck_monitoring_run_output_ready_success",
    ),
    (
        "monitoring_run_company",
        "ck_monitoring_run_company_ck_monitoring_run_company_att_5cb5",
        "ck_monitoring_run_company_attempt_time_order",
    ),
    (
        "monitoring_run_company",
        "ck_monitoring_run_company_ck_monitoring_run_company_non_4792",
        "ck_monitoring_run_company_nonnegative_attempt_count",
    ),
    (
        "monitoring_run_company",
        "ck_monitoring_run_company_ck_monitoring_run_company_out_7a7a",
        "ck_monitoring_run_company_output_ready_success",
    ),
    (
        "monitoring_run_company",
        "ck_monitoring_run_company_ck_monitoring_run_company_res_7e2e",
        "ck_monitoring_run_company_result_ids_are_arrays",
    ),
    (
        "release_event",
        "ck_release_event_ck_release_event_action",
        "ck_release_event_action",
    ),
    (
        "release_event",
        "ck_release_event_ck_release_event_manifest_sha256_length",
        "ck_release_event_manifest_sha256_length",
    ),
    (
        "release_event",
        "ck_release_event_ck_release_event_report_sha256_length",
        "ck_release_event_report_sha256_length",
    ),
    (
        "release_manifest",
        "ck_release_manifest_ck_release_manifest_manifest_sha256_length",
        "ck_release_manifest_manifest_sha256_length",
    ),
    (
        "release_manifest",
        "ck_release_manifest_ck_release_manifest_replay_report_s_bc8c",
        "ck_release_manifest_replay_report_sha256_length",
    ),
    (
        "release_manifest",
        "ck_release_manifest_ck_release_manifest_status",
        "ck_release_manifest_status",
    ),
    (
        "semantic_version_set",
        "ck_semantic_version_set_ck_semantic_version_set_effecti_a7d8",
        "ck_semantic_version_set_effective_period",
    ),
    (
        "semantic_version_set",
        "ck_semantic_version_set_ck_semantic_version_set_set_key_length",
        "ck_semantic_version_set_set_key_length",
    ),
    (
        "semantic_version_set",
        "ck_semantic_version_set_ck_semantic_version_set_status",
        "ck_semantic_version_set_status",
    ),
    (
        "tax_master_version",
        "ck_tax_master_amount_scale",
        "ck_tax_master_version_amount_scale",
    ),
    (
        "tax_master_version",
        "ck_tax_master_average_rate",
        "ck_tax_master_version_average_tax_burden_rate",
    ),
    (
        "tax_master_version",
        "ck_tax_master_currency",
        "ck_tax_master_version_currency",
    ),
    (
        "tax_master_version",
        "ck_tax_master_tax_rate",
        "ck_tax_master_version_tax_rate",
    ),
    (
        "tax_master_version",
        "ck_tax_master_valid_period",
        "ck_tax_master_version_valid_period",
    ),
)


def upgrade() -> None:
    for table_name, legacy_name, metadata_name in RENAMES:
        _rename_constraint(table_name, legacy_name, metadata_name)


def downgrade() -> None:
    for table_name, legacy_name, metadata_name in reversed(RENAMES):
        _rename_constraint(table_name, metadata_name, legacy_name)


def _rename_constraint(table_name: str, current_name: str, target_name: str) -> None:
    bind = op.get_bind()
    names = set(
        bind.execute(
            sa.text(
                "SELECT constraint_record.conname "
                "FROM pg_constraint AS constraint_record "
                "WHERE constraint_record.contype = 'c' "
                "AND constraint_record.conrelid = to_regclass(:table_name)"
            ),
            {"table_name": table_name},
        ).scalars()
    )
    if target_name in names and current_name not in names:
        return
    if current_name not in names:
        raise RuntimeError(
            f"CHECK_CONSTRAINT_RENAME_SOURCE_MISSING: {table_name}.{current_name}"
        )
    if target_name in names:
        raise RuntimeError(
            f"CHECK_CONSTRAINT_RENAME_TARGET_EXISTS: {table_name}.{target_name}"
        )

    preparer = bind.dialect.identifier_preparer
    op.execute(
        sa.text(
            f"ALTER TABLE {preparer.quote(table_name)} "
            f"RENAME CONSTRAINT {preparer.quote(current_name)} "
            f"TO {preparer.quote(target_name)}"
        )
    )
