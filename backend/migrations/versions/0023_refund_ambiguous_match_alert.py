"""Classify multiple equal refund candidates as an alert.

Revision ID: 0023_refund_ambiguous_match_alert
Revises: 0022_refund_taxes_payable_priority
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0023_refund_ambiguous_match_alert"
down_revision: str | None = "0022_refund_taxes_payable_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT_NAME = "ck_income_tax_refund_scan_result_classification_state"


def upgrade() -> None:
    op.drop_constraint(
        op.f(CONSTRAINT_NAME),
        "income_tax_refund_scan_result",
        type_="check",
    )
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "income_tax_refund_scan_result",
        _classification_constraint("alert_code = 'AMBIGUOUS_REFUND_MATCH'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("LOCK TABLE income_tax_refund_scan_result IN ACCESS EXCLUSIVE MODE")
    )
    alerted_ambiguous_result = bind.execute(
        sa.text(
            "SELECT id FROM income_tax_refund_scan_result "
            "WHERE receipt_status = 'AMBIGUOUS' "
            "AND alert_code = 'AMBIGUOUS_REFUND_MATCH' LIMIT 1"
        )
    ).scalar_one_or_none()
    if alerted_ambiguous_result is not None:
        raise RuntimeError(
            "REFUND_AMBIGUOUS_ALERT_DOWNGRADE_BLOCKED: an alerted ambiguous "
            f"scan result exists ({alerted_ambiguous_result})"
        )
    op.drop_constraint(
        op.f(CONSTRAINT_NAME),
        "income_tax_refund_scan_result",
        type_="check",
    )
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "income_tax_refund_scan_result",
        _classification_constraint("alert_code IS NULL"),
    )


def _classification_constraint(ambiguous_alert_clause: str) -> str:
    return (
        "(receipt_status = 'NOT_RECEIVED' AND account_status = 'NOT_APPLICABLE' "
        "AND matched_line_id IS NULL AND matched_amount IS NULL "
        "AND gl_account_code IS NULL AND gl_account_name IS NULL AND alert_code IS NULL) OR "
        "(receipt_status = 'RECEIVED' AND account_status = 'CORRECT' "
        "AND matched_line_id IS NOT NULL AND matched_amount = expected_amount "
        "AND gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL "
        "AND alert_code IS NULL) OR "
        "(receipt_status = 'RECEIVED' AND account_status = 'WRONG_ACCOUNT' "
        "AND matched_line_id IS NOT NULL AND matched_amount = expected_amount "
        "AND gl_account_code IS NOT NULL AND gl_account_name IS NOT NULL "
        "AND alert_code = 'REFUND_BOOKED_TO_WRONG_ACCOUNT') OR "
        "(receipt_status = 'AMBIGUOUS' AND account_status = 'AMBIGUOUS' "
        "AND matched_line_id IS NULL AND matched_amount IS NULL "
        "AND gl_account_code IS NULL AND gl_account_name IS NULL "
        f"AND {ambiguous_alert_clause})"
    )
