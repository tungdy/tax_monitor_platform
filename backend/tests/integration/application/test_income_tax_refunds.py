from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from functools import partial
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

import tax_risk.application.income_tax_refunds as refund_application
from tax_risk.application.income_tax_refunds import (
    IncomeTaxRefundService,
    IncomeTaxRefundServiceError,
    IncomeTaxRefundTargetDraft,
    SapRefundEvidenceDraft,
    SapRefundLineDraft,
)
from tax_risk.domain.income_tax_refund import (
    IncomeTaxRefundInputs,
    IncomeTaxRefundResult,
    evaluate_income_tax_refund,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


REFUND_TAX_YEAR = 2035
SCAN_YEAR = REFUND_TAX_YEAR + 1


def _service(isolated_database_url: str) -> tuple[IncomeTaxRefundService, Engine]:
    engine, factory = create_session_factory(isolated_database_url)
    return IncomeTaxRefundService(partial(UnitOfWork, factory)), engine


def _seed_company(engine: Engine, label: str) -> tuple[str, UUID]:
    company_code = f"REFUND-{label}-{uuid4().hex}"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                "INSERT INTO company (company_code, company_name, lifecycle) "
                "VALUES (:company_code, :company_name, 'ACTIVE') RETURNING id"
            ),
            {"company_code": company_code, "company_name": f"Refund {label}"},
        ).scalar_one()
    return company_code, company_id


def _target(
    company_code: str,
    amount: str,
    *,
    source_record_key: str | None = None,
    received_in_source: bool = False,
) -> IncomeTaxRefundTargetDraft:
    return IncomeTaxRefundTargetDraft(
        company_code=company_code,
        source_record_key=source_record_key or f"row:{company_code}",
        expected_refund_amount=Decimal(amount),
        raw_expected_refund_amount=Decimal(amount),
        currency="CNY",
        amount_scale=2,
        received_in_source=received_in_source,
    )


def _line(
    company_code: str,
    amount: str,
    *,
    document_number: str,
    line_item: str = "001",
    account_category: str = "INCOME_TAX_EXPENSE",
    debit_credit: str = "CREDIT",
    period: int = 3,
    is_reversed: bool = False,
) -> SapRefundLineDraft:
    return SapRefundLineDraft(
        company_code=company_code,
        client="800",
        ledger="0L",
        fiscal_year=SCAN_YEAR,
        fiscal_period=period,
        posting_date=date(SCAN_YEAR, period, 15),
        document_number=document_number,
        line_item=line_item,
        gl_account_code={
            "INCOME_TAX_EXPENSE": "6801010000",
            "OTHER_INCOME": "6112010000",
            "TAXES_PAYABLE": "2221130000",
        }[account_category],
        gl_account_name={
            "INCOME_TAX_EXPENSE": "所得税费用",
            "OTHER_INCOME": "其他收益",
            "TAXES_PAYABLE": "应交税费-企业所得税",
        }[account_category],
        account_category=account_category,
        debit_credit=debit_credit,
        amount=Decimal(amount),
        currency="CNY",
        amount_scale=2,
        is_reversed=is_reversed,
    )


def _evidence(
    source_batch_key: str,
    company_codes: tuple[str, ...],
    lines: tuple[SapRefundLineDraft, ...],
    *,
    through_period: int = 3,
) -> SapRefundEvidenceDraft:
    return SapRefundEvidenceDraft(
        source_batch_key=source_batch_key,
        fiscal_year=SCAN_YEAR,
        through_period=through_period,
        company_codes=company_codes,
        lines=lines,
    )


def test_target_and_sap_imports_are_idempotent_and_reject_batch_duplicates(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code, _ = _seed_company(engine, "IDEMPOTENCY")
    target = _target(company_code, "100.005")
    first_line = _line(company_code, "100.00", document_number="910001")
    first_batch = _evidence("refund-idempotency-1", (company_code,), (first_line,))
    second_line = _line(company_code, "99.00", document_number="910001")
    second_batch = _evidence("refund-idempotency-2", (company_code,), (second_line,))
    duplicate_batch = _evidence(
        "refund-duplicate-lines",
        (company_code,),
        (
            _line(company_code, "100.00", document_number="910002"),
            _line(company_code, "101.00", document_number="910002"),
        ),
    )
    try:
        imported = service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-v1",
            drafts=(target,),
        )
        replayed = service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-v1",
            drafts=(target,),
        )
        first = service.import_sap_evidence(first_batch)
        first_replay = service.import_sap_evidence(first_batch)
        second = service.import_sap_evidence(second_batch)

        assert (imported.accepted_count, imported.replayed_count) == (1, 0)
        assert (replayed.accepted_count, replayed.replayed_count) == (0, 1)
        assert (first.accepted_count, first.replayed_count) == (1, 0)
        assert (first_replay.accepted_count, first_replay.replayed_count) == (0, 1)
        assert (second.accepted_count, second.replayed_count) == (1, 0)
        with pytest.raises(IncomeTaxRefundServiceError) as captured:
            service.import_sap_evidence(duplicate_batch)
        assert captured.value.error_code == "DUPLICATE_SAP_LINE_IN_BATCH"

        with engine.connect() as connection:
            target_rows = (
                connection.execute(
                    text(
                        "SELECT expected_amount FROM income_tax_refund_target "
                        "WHERE company_id = (SELECT id FROM company WHERE company_code = :code)"
                    ),
                    {"code": company_code},
                )
                .scalars()
                .all()
            )
            evidence_count = connection.execute(
                text(
                    "SELECT count(*) FROM sap_refund_evidence_batch "
                    "WHERE source_batch_key IN "
                    "('refund-idempotency-1', 'refund-idempotency-2', "
                    "'refund-duplicate-lines')"
                ),
            ).scalar_one()
            line_count = connection.execute(
                text(
                    "SELECT count(*) FROM sap_gl_line_observation "
                    "WHERE company_id = (SELECT id FROM company WHERE company_code = :code)"
                ),
                {"code": company_code},
            ).scalar_one()
        assert target_rows == [Decimal("100.010000000000")]
        assert evidence_count == 2
        assert line_count == 2
    finally:
        engine.dispose()


def test_scan_classifies_all_outcomes_stops_received_and_enqueues_writebacks_and_case(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    seeded = {
        label: _seed_company(engine, label)
        for label in ("CORRECT", "WRONG", "MISSING", "AMBIGUOUS")
    }
    codes = tuple(value[0] for value in seeded.values())
    drafts = (
        _target(seeded["CORRECT"][0], "100.00"),
        _target(seeded["WRONG"][0], "200.00"),
        _target(seeded["MISSING"][0], "300.00"),
        _target(seeded["AMBIGUOUS"][0], "400.00"),
    )
    march_lines = (
        _line(seeded["CORRECT"][0], "100.00", document_number="920001"),
        _line(
            seeded["WRONG"][0],
            "200.00",
            document_number="920002",
            account_category="OTHER_INCOME",
        ),
        _line(seeded["MISSING"][0], "300.00", document_number="920003", debit_credit="DEBIT"),
        _line(seeded["AMBIGUOUS"][0], "400.00", document_number="920004"),
        _line(
            seeded["AMBIGUOUS"][0],
            "400.00",
            document_number="920005",
            account_category="OTHER_INCOME",
        ),
    )
    march_batch = _evidence("refund-march-all-outcomes", codes, march_lines)
    april_batch = _evidence(
        "refund-april-pending-only",
        codes,
        tuple(
            _line(
                line.company_code,
                format(line.amount, "f"),
                document_number=line.document_number,
                line_item=line.line_item,
                account_category=line.account_category,
                debit_credit=line.debit_credit,
                period=line.fiscal_period,
                is_reversed=line.is_reversed,
            )
            for line in march_lines
        ),
        through_period=4,
    )
    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-all-outcomes",
            drafts=drafts,
        )
        service.import_sap_evidence(march_batch)
        march = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=march_batch.source_batch_key,
            allowed_company_ids=frozenset(value[1] for value in seeded.values()),
        )
        replay = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=march_batch.source_batch_key,
            allowed_company_ids=frozenset(value[1] for value in seeded.values()),
        )

        assert (
            march.received_count,
            march.not_received_count,
            march.wrong_account_count,
            march.ambiguous_count,
        ) == (2, 1, 1, 1)
        assert replay == march
        received_by_code = {item.company_code: item for item in march.received}
        assert received_by_code[seeded["CORRECT"][0]].booking_status == "CORRECT"
        wrong = received_by_code[seeded["WRONG"][0]]
        assert wrong.booking_status == "WRONG_ACCOUNT"
        assert wrong.alert_code == "REFUND_BOOKED_TO_WRONG_ACCOUNT"
        assert wrong.writeback_status == "PENDING"
        assert march.not_received[0].company_code == seeded["MISSING"][0]
        assert march.ambiguous[0].company_code == seeded["AMBIGUOUS"][0]
        assert march.ambiguous[0].alert_code == "AMBIGUOUS_REFUND_MATCH"

        service.import_sap_evidence(april_batch)
        april = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=4,
            source_batch_key=april_batch.source_batch_key,
            allowed_company_ids=frozenset(value[1] for value in seeded.values()),
        )
        assert (
            april.received_count,
            april.not_received_count,
            april.wrong_account_count,
            april.ambiguous_count,
        ) == (2, 1, 1, 1)

        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM income_tax_refund_scan_result r "
                    " JOIN income_tax_refund_target t ON t.id = r.target_id "
                    " WHERE t.refund_tax_year = :year AND t.company_id = ANY(:company_ids)), "
                    "(SELECT count(*) FROM income_tax_refund_writeback w "
                    " WHERE w.company_id = ANY(:company_ids)), "
                    "(SELECT count(*) FROM risk_case c "
                    " WHERE c.company_id = ANY(:company_ids) "
                    " AND c.monitor_type = 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY')"
                ),
                {
                    "year": REFUND_TAX_YEAR,
                    "company_ids": [value[1] for value in seeded.values()],
                },
            ).one()
            writebacks = [
                (row.desired_value, row.status)
                for row in connection.execute(
                    text(
                        "SELECT desired_value, status FROM income_tax_refund_writeback "
                        "WHERE company_id = ANY(:company_ids) ORDER BY company_id"
                    ),
                    {"company_ids": [value[1] for value in seeded.values()]},
                )
            ]
            risk_case = (
                connection.execute(
                    text(
                        "SELECT risk_amount, currency, risk_direction, fingerprint, lineage "
                        "FROM risk_case WHERE company_id = :company_id "
                        "AND monitor_type = 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY'"
                    ),
                    {"company_id": seeded["WRONG"][1]},
                )
                .mappings()
                .one()
            )
            ambiguous_case = (
                connection.execute(
                    text(
                        "SELECT risk_amount, currency, risk_direction, fingerprint, lineage "
                        "FROM risk_case WHERE company_id = :company_id "
                        "AND monitor_type = 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY'"
                    ),
                    {"company_id": seeded["AMBIGUOUS"][1]},
                )
                .mappings()
                .one()
            )
        assert counts == (6, 2, 2)
        assert writebacks == [("已退税", "PENDING"), ("已退税", "PENDING")]
        assert risk_case["risk_amount"] == Decimal("200.000000000000")
        assert risk_case["currency"] == "CNY"
        assert risk_case["risk_direction"] == "REFUND_BOOKED_TO_WRONG_ACCOUNT"
        assert len(risk_case["fingerprint"]) == 64
        assert risk_case["lineage"]["refund_tax_year"] == REFUND_TAX_YEAR
        assert risk_case["lineage"]["scan_period"] == f"{SCAN_YEAR}-03"
        assert risk_case["lineage"]["source_batch_key"] == march_batch.source_batch_key
        for key in ("target_id", "scan_result_id", "matched_line_id"):
            assert UUID(risk_case["lineage"][key])
        assert ambiguous_case["risk_amount"] == Decimal("400.000000000000")
        assert ambiguous_case["currency"] == "CNY"
        assert ambiguous_case["risk_direction"] == "AMBIGUOUS_REFUND_MATCH"
        assert len(ambiguous_case["fingerprint"]) == 64
        assert len(ambiguous_case["lineage"]["matched_candidates"]) == 2
        assert "matched_line_id" not in ambiguous_case["lineage"]
    finally:
        engine.dispose()


def test_scan_uses_only_the_requested_complete_sap_batch(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code, company_id = _seed_company(engine, "BATCH-SCOPE")
    old_batch = _evidence(
        "refund-old-snapshot",
        (company_code,),
        (_line(company_code, "500.00", document_number="930001"),),
    )
    current_batch = _evidence(
        "refund-current-snapshot",
        (company_code,),
        (_line(company_code, "499.99", document_number="930001"),),
    )
    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-batch-scope",
            drafts=(_target(company_code, "500.00"),),
        )
        service.import_sap_evidence(old_batch)
        service.import_sap_evidence(current_batch)

        result = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=current_batch.source_batch_key,
            allowed_company_ids=frozenset({company_id}),
        )

        assert result.received_count == 0
        assert result.not_received_count == 1
        assert result.not_received[0].receipt_status == "NOT_RECEIVED"
        with engine.connect() as connection:
            stored_batches = (
                connection.execute(
                    text(
                        "SELECT source_batch_key FROM sap_gl_line_observation "
                        "WHERE company_id = (SELECT id FROM company WHERE company_code = :code) "
                        "ORDER BY source_batch_key"
                    ),
                    {"code": company_code},
                )
                .scalars()
                .all()
            )
        assert stored_batches == ["refund-current-snapshot", "refund-old-snapshot"]
    finally:
        engine.dispose()


def test_scan_uses_taxes_payable_only_after_primary_accounts_do_not_match(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    primary_code, primary_id = _seed_company(engine, "PRIMARY-PRIORITY")
    fallback_code, fallback_id = _seed_company(engine, "TAXES-PAYABLE-FALLBACK")
    batch = _evidence(
        "refund-ordered-account-stages",
        (primary_code, fallback_code),
        (
            _line(
                primary_code,
                "100.00",
                document_number="931001",
                account_category="OTHER_INCOME",
            ),
            _line(
                primary_code,
                "100.00",
                document_number="931002",
                account_category="TAXES_PAYABLE",
            ),
            _line(
                fallback_code,
                "200.00",
                document_number="931003",
                account_category="TAXES_PAYABLE",
            ),
        ),
    )
    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-ordered-stages",
            drafts=(
                _target(primary_code, "100.00"),
                _target(fallback_code, "200.00"),
            ),
        )
        service.import_sap_evidence(batch)

        result = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=batch.source_batch_key,
            allowed_company_ids=frozenset({primary_id, fallback_id}),
        )

        assert (result.received_count, result.wrong_account_count) == (2, 2)
        received = {item.company_code: item for item in result.received}
        assert received[primary_code].account_family == "OTHER_INCOME"
        assert received[primary_code].gl_account_code == "6112010000"
        assert received[fallback_code].account_family == "TAXES_PAYABLE"
        assert received[fallback_code].gl_account_code == "2221130000"
        with engine.connect() as connection:
            stages = {
                row.company_id: row.match_stage
                for row in connection.execute(
                    text(
                        "SELECT company_id, structured_output ->> 'match_stage' AS match_stage "
                        "FROM income_tax_refund_scan_result "
                        "WHERE company_id = ANY(:company_ids)"
                    ),
                    {"company_ids": [primary_id, fallback_id]},
                )
            }
        assert stages == {
            primary_id: "PRIMARY_ACCOUNTS",
            fallback_id: "TAXES_PAYABLE",
        }
    finally:
        engine.dispose()


def test_manual_feishu_received_status_stops_future_scans_without_writeback(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code, company_id = _seed_company(engine, "MANUAL-RECEIVED")
    march_batch = _evidence("refund-manual-march", (company_code,), ())
    april_batch = _evidence(
        "refund-manual-april",
        (company_code,),
        (
            _line(
                company_code,
                "500.00",
                document_number="932001",
                period=4,
            ),
        ),
        through_period=4,
    )
    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-before-manual-receipt",
            drafts=(_target(company_code, "500.00"),),
        )
        service.import_sap_evidence(march_batch)
        march = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=march_batch.source_batch_key,
            allowed_company_ids=frozenset({company_id}),
        )
        assert march.not_received_count == 1

        imported = service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-manual-receipt",
            drafts=(_target(company_code, "500.00", received_in_source=True),),
        )
        service.import_sap_evidence(april_batch)
        april = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=4,
            source_batch_key=april_batch.source_batch_key,
            allowed_company_ids=frozenset({company_id}),
        )

        assert (imported.accepted_count, imported.replayed_count) == (1, 0)
        assert april.received_count == 1
        assert april.not_received_count == 0
        assert april.received[0].company_code == company_code
        assert april.received[0].receipt_source == "LARK_MANUAL"
        assert april.received[0].booking_status == "NOT_APPLICABLE"
        assert april.received[0].writeback_status is None
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM income_tax_refund_scan_result "
                    " WHERE company_id = :company_id), "
                    "(SELECT count(*) FROM income_tax_refund_writeback "
                    " WHERE company_id = :company_id)"
                ),
                {"company_id": company_id},
            ).one()
        assert counts == (1, 0)
    finally:
        engine.dispose()


def test_scan_rejects_out_of_window_and_non_n_plus_one_periods(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    try:
        with pytest.raises(IncomeTaxRefundServiceError) as outside_window:
            service.scan(
                refund_tax_year=REFUND_TAX_YEAR,
                scan_year=SCAN_YEAR,
                scan_month=2,
                source_batch_key="unused",
            )
        with pytest.raises(IncomeTaxRefundServiceError) as wrong_year:
            service.scan(
                refund_tax_year=REFUND_TAX_YEAR,
                scan_year=SCAN_YEAR + 1,
                scan_month=3,
                source_batch_key="unused",
            )
        assert outside_window.value.error_code == "INVALID_REFUND_SCAN_PERIOD"
        assert wrong_year.value.error_code == "INVALID_REFUND_SCAN_YEAR"
    finally:
        engine.dispose()


def test_scan_rolls_back_every_company_when_evaluation_fails_mid_transaction(
    isolated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine = _service(isolated_database_url)
    first_code, first_id = _seed_company(engine, "ROLLBACK-1")
    second_code, second_id = _seed_company(engine, "ROLLBACK-2")
    batch = _evidence("refund-rollback", (first_code, second_code), ())
    original_evaluate = evaluate_income_tax_refund
    call_count = 0

    def fail_second(inputs: IncomeTaxRefundInputs) -> IncomeTaxRefundResult:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("forced second-company failure")
        return original_evaluate(inputs)

    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-rollback",
            drafts=(_target(first_code, "10.00"), _target(second_code, "20.00")),
        )
        service.import_sap_evidence(batch)
        monkeypatch.setattr(refund_application, "evaluate_income_tax_refund", fail_second)

        with pytest.raises(RuntimeError, match="forced second-company failure"):
            service.scan(
                refund_tax_year=REFUND_TAX_YEAR,
                scan_year=SCAN_YEAR,
                scan_month=3,
                source_batch_key=batch.source_batch_key,
                allowed_company_ids=frozenset({first_id, second_id}),
            )

        with engine.connect() as connection:
            scan_count = connection.execute(
                text(
                    "SELECT count(*) FROM income_tax_refund_scan_result "
                    "WHERE company_id = ANY(:company_ids)"
                ),
                {"company_ids": [first_id, second_id]},
            ).scalar_one()
            target_states = [
                (row.receipt_status, row.latest_scan_period)
                for row in connection.execute(
                    text(
                        "SELECT receipt_status, latest_scan_period "
                        "FROM income_tax_refund_target WHERE company_id = ANY(:company_ids)"
                    ),
                    {"company_ids": [first_id, second_id]},
                )
            ]
        assert call_count == 2
        assert scan_count == 0
        assert target_states == [("PENDING", None), ("PENDING", None)]
    finally:
        engine.dispose()


def test_concurrent_same_month_replay_creates_one_result_case_and_writeback(
    isolated_database_url: str,
) -> None:
    service, engine = _service(isolated_database_url)
    company_code, company_id = _seed_company(engine, "CONCURRENT")
    batch = _evidence(
        "refund-concurrent-replay",
        (company_code,),
        (
            _line(
                company_code,
                "888.88",
                document_number="950001",
                account_category="OTHER_INCOME",
            ),
        ),
    )
    start = Barrier(2)

    def run_scan() -> tuple[int, int]:
        start.wait(timeout=10)
        result = service.scan(
            refund_tax_year=REFUND_TAX_YEAR,
            scan_year=SCAN_YEAR,
            scan_month=3,
            source_batch_key=batch.source_batch_key,
            allowed_company_ids=frozenset({company_id}),
        )
        return result.received_count, result.wrong_account_count

    try:
        service.import_targets(
            refund_tax_year=REFUND_TAX_YEAR,
            source_version="feishu-concurrent",
            drafts=(_target(company_code, "888.88"),),
        )
        service.import_sap_evidence(batch)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(lambda _index: run_scan(), range(2)))

        assert outcomes == ((1, 1), (1, 1))
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM income_tax_refund_scan_result "
                    " WHERE company_id = :company_id), "
                    "(SELECT count(*) FROM income_tax_refund_writeback "
                    " WHERE company_id = :company_id), "
                    "(SELECT count(*) FROM risk_case "
                    " WHERE company_id = :company_id "
                    " AND monitor_type = 'INCOME_TAX_REFUND_ACCOUNT_ACCURACY')"
                ),
                {"company_id": company_id},
            ).one()
        assert counts == (1, 1, 1)
    finally:
        engine.dispose()
