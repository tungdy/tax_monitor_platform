from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tax_risk.domain.business_entertainment.company_scope import ScopeVersionStatus
from tax_risk.persistence.models import AuditTimestampMixin, Base, UUIDPrimaryKeyMixin


class BusinessEntertainmentScopeVersion(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_scope_version"
    __table_args__ = (
        CheckConstraint("effective_to >= effective_from", name="effective_period"),
        CheckConstraint("length(file_checksum) = 64", name="file_checksum_length"),
        CheckConstraint(
            "(status = 'DRAFT' AND reviewer_id IS NULL AND approved_at IS NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status = 'APPROVED' AND reviewer_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND published_at IS NULL AND published_by IS NULL) OR "
            "(status IN ('PUBLISHED', 'RETIRED') AND reviewer_id IS NOT NULL "
            "AND approved_at IS NOT NULL AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL)",
            name="status_audit",
        ),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "ingest_batch.id",
            name="fk_be_scope_version_batch",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date] = mapped_column(Date, nullable=False)
    source_file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[ScopeVersionStatus] = mapped_column(
        Enum(ScopeVersionStatus, name="business_entertainment_scope_status"),
        nullable=False,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(256))


class BusinessEntertainmentScopeCompany(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_scope_company"
    __table_args__ = (
        UniqueConstraint("version_id", "company_id", name="uq_be_scope_version_company"),
        UniqueConstraint("version_id", "source_record_id", name="uq_be_scope_version_source"),
    )

    version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "business_entertainment_scope_version.id",
            name="fk_be_scope_company_version",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "company.id",
            name="fk_be_scope_company_company",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "source_record.id",
            name="fk_be_scope_company_source",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )


class BusinessEntertainmentSourceObservation(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_source_observation"
    __table_args__ = (
        UniqueConstraint("ingest_batch_id", "source_record_key", name="uq_be_source_batch_key"),
        CheckConstraint("period BETWEEN 1 AND 12", name="period"),
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="currency",
        ),
        CheckConstraint(
            "(amount IS NULL AND currency IS NULL) OR "
            "(amount IS NOT NULL AND currency IS NOT NULL)",
            name="amount_currency",
        ),
        Index("ix_be_source_obs_batch", "ingest_batch_id"),
        Index("ix_be_source_obs_company", "company_code"),
    )

    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_be_source_obs_source", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    ingest_batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingest_batch.id", name="fk_be_source_obs_batch", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_code: Mapped[str] = mapped_column(String(128), nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str | None] = mapped_column(String(3))
    parent_oa_id: Mapped[str | None] = mapped_column(String(128))
    parent_hesi_id: Mapped[str | None] = mapped_column(String(128))


class EvidenceLink(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "evidence_link"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "source_record_id",
            "target_record_id",
            "relation_kind",
            name="uq_evidence_link_snapshot_relation",
        ),
        Index("ix_evidence_link_company", "company_code"),
        Index("ix_evidence_link_snapshot", "snapshot_id"),
    )

    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_evidence_source", ondelete="RESTRICT"),
        nullable=False,
    )
    target_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_evidence_target", ondelete="RESTRICT"),
        nullable=False,
    )
    relation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_quality: Mapped[str] = mapped_column(String(32), nullable=False)
    matched_field: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounting_snapshot.id", name="fk_evidence_snapshot", ondelete="RESTRICT"),
        nullable=False,
    )


class BusinessEntertainmentEvaluation(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_evaluation"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "candidate_key", name="uq_be_evaluation_snapshot_key"),
        CheckConstraint("period BETWEEN 1 AND 12", name="period"),
        Index("ix_be_evaluation_company", "company_code"),
        Index("ix_be_evaluation_snapshot", "snapshot_id"),
    )

    candidate_key: Mapped[str] = mapped_column(String(512), nullable=False)
    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_be_eval_source", ondelete="RESTRICT"),
        nullable=False,
    )
    sap_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "sap_expense_voucher_observation.id",
            name="fk_be_eval_sap_obs",
            ondelete="RESTRICT",
        )
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    amount_source: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounting_snapshot.id", name="fk_be_eval_snapshot", ondelete="RESTRICT"),
        nullable=False,
    )


class SapLinkCoverage(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "sap_link_coverage"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "sap_observation_id", name="uq_sap_coverage_snapshot_obs"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        Index("ix_sap_coverage_company", "company_code"),
        Index("ix_sap_coverage_snapshot", "snapshot_id"),
    )

    company_code: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    sap_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "sap_expense_voucher_observation.id",
            name="fk_sap_coverage_obs",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    document_number: Mapped[str] = mapped_column(String(64), nullable=False)
    line_item: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    link_status: Mapped[str] = mapped_column(String(64), nullable=False)
    exact_evidence_link_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evidence_link.id", name="fk_sap_coverage_link", ondelete="RESTRICT")
    )
    evaluated_via_business_document: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "accounting_snapshot.id",
            name="fk_sap_coverage_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


class BusinessEntertainmentCaseDetail(UUIDPrimaryKeyMixin, AuditTimestampMixin, Base):
    __tablename__ = "business_entertainment_case_detail"
    __table_args__ = (
        UniqueConstraint("risk_case_id", name="uq_be_case_detail_case"),
        CheckConstraint(
            "source_mode IN ('SAP_LINKED', 'BUSINESS_DOCUMENT_UNLINKED')",
            name="source_mode",
        ),
        CheckConstraint(
            "sap_link_status IN ('LINKED', 'PENDING_LOCATION')",
            name="sap_link_status",
        ),
        Index("ix_be_case_detail_detection", "semantic_detection_id"),
    )

    risk_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_case.id", name="fk_be_case_detail_case", ondelete="RESTRICT"),
        nullable=False,
    )
    semantic_detection_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "semantic_detection_record.id",
            name="fk_be_case_detail_detection",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    candidate_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_record.id", name="fk_be_case_detail_source", ondelete="RESTRICT"),
        nullable=False,
    )
    source_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    sap_link_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sap_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "sap_expense_voucher_observation.id",
            name="fk_be_case_detail_sap",
            ondelete="RESTRICT",
        )
    )
    risk_amount_source: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    account_dictionary_version: Mapped[str] = mapped_column(String(128), nullable=False)
    exact_evidence_link_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evidence_link.id", name="fk_be_case_detail_link", ondelete="RESTRICT")
    )
    workflow_note: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "BusinessEntertainmentEvaluation",
    "BusinessEntertainmentCaseDetail",
    "BusinessEntertainmentScopeCompany",
    "BusinessEntertainmentScopeVersion",
    "BusinessEntertainmentSourceObservation",
    "EvidenceLink",
    "SapLinkCoverage",
]
