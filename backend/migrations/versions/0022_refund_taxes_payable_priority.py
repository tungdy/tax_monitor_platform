"""Add taxes-payable evidence to the ordered refund matcher.

Revision ID: 0022_refund_taxes_payable_priority
Revises: 0021_deferred_tax_loss_less_profit
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_refund_taxes_payable_priority"
down_revision: str | None = "0021_deferred_tax_loss_less_profit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT_NAME = "ck_sap_gl_line_observation_account_category"


def upgrade() -> None:
    op.drop_constraint(
        op.f(CONSTRAINT_NAME),
        "sap_gl_line_observation",
        type_="check",
    )
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "sap_gl_line_observation",
        "account_category IN ('INCOME_TAX_EXPENSE', 'OTHER_INCOME', 'TAXES_PAYABLE')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE sap_gl_line_observation IN ACCESS EXCLUSIVE MODE"))
    taxes_payable_line = bind.execute(
        sa.text(
            "SELECT id FROM sap_gl_line_observation "
            "WHERE account_category = 'TAXES_PAYABLE' LIMIT 1"
        )
    ).scalar_one_or_none()
    if taxes_payable_line is not None:
        raise RuntimeError(
            "REFUND_TAXES_PAYABLE_DOWNGRADE_BLOCKED: taxes-payable evidence exists "
            f"({taxes_payable_line})"
        )
    op.drop_constraint(
        op.f(CONSTRAINT_NAME),
        "sap_gl_line_observation",
        type_="check",
    )
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "sap_gl_line_observation",
        "account_category IN ('INCOME_TAX_EXPENSE', 'OTHER_INCOME')",
    )
