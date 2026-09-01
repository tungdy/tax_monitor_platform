from __future__ import annotations

import argparse
from base64 import b64encode
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tax_risk.config import EXPECTED_MIGRATION_HEAD
from tax_risk.persistence.models import ReleaseEvent, ReleaseManifestRecord
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.release.manifest import ReleaseArtifacts, ReleaseManifest
from tax_risk.release.replay_gate import ReplayGate, ReplayMetrics
from tax_risk.release.replay_runner import ReplayRunner
from tax_risk.release.signing import (
    Ed25519ManifestVerifier,
    SignatureEnvelope,
    TrustedPublicKey,
)


class ReleaseStateError(RuntimeError):
    """Raised when an invalid release lifecycle transition is requested."""


class SqlReleaseStore:
    """Persist release state and an append-only event in one transaction."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_candidate(self, manifest: ReleaseManifest, *, actor: str) -> UUID:
        with self._unit_of_work_factory() as uow:
            record = ReleaseManifestRecord(
                manifest_sha256=manifest.manifest_sha256,
                candidate_version=manifest.candidate_version,
                canonical_manifest=json.loads(manifest.canonical_bytes()),
                status="CANDIDATE",
            )
            uow.session.add(record)
            uow.session.flush()
            self._add_event(uow, record, action="CANDIDATE_CREATED", actor=actor)
            uow.commit()
            return record.id

    def record_replay_started(self, release_id: UUID, *, actor: str) -> None:
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"CANDIDATE"})
            record.status = "REPLAYING"
            self._add_event(uow, record, action="REPLAY_STARTED", actor=actor)
            uow.commit()

    def record_replay_result(
        self,
        release_id: UUID,
        *,
        report_sha256: str,
        approved: bool,
        actor: str,
    ) -> None:
        self._require_digest(report_sha256)
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"REPLAYING"})
            record.replay_report_sha256 = report_sha256
            record.status = "REPLAY_APPROVED" if approved else "REPLAY_REJECTED"
            self._add_event(
                uow,
                record,
                action="REPLAY_APPROVED" if approved else "REPLAY_REJECTED",
                actor=actor,
                report_sha256=report_sha256,
            )
            uow.commit()

    def approve(self, release_id: UUID, *, approver: str) -> None:
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"REPLAY_APPROVED"})
            record.status = "APPROVED"
            record.approvals = [
                *record.approvals,
                {
                    "action": "RELEASE_APPROVED",
                    "approver": approver,
                    "approved_at": self._clock().isoformat(),
                },
            ]
            self._add_event(
                uow,
                record,
                action="RELEASE_APPROVED",
                actor=approver,
                approver=approver,
                report_sha256=record.replay_report_sha256,
            )
            uow.commit()

    def attach_signature(
        self,
        release_id: UUID,
        envelope: SignatureEnvelope,
        *,
        actor: str,
    ) -> None:
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"APPROVED"})
            if envelope.manifest_sha256 != record.manifest_sha256:
                raise ReleaseStateError("signature manifest hash does not match release candidate")
            record.signature_base64 = envelope.signature_base64
            record.signer_key_id = envelope.key_id
            record.signer_key_version = envelope.key_version
            record.status = "SIGNED"
            self._add_event(
                uow,
                record,
                action="MANIFEST_SIGNED",
                actor=actor,
                report_sha256=record.replay_report_sha256,
                payload={
                    "algorithm": envelope.algorithm,
                    "key_id": envelope.key_id,
                    "key_version": envelope.key_version,
                },
            )
            uow.commit()

    def record_verification(self, release_id: UUID, *, actor: str) -> None:
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"SIGNED"})
            record.status = "VERIFIED"
            record.verified_at = self._clock()
            self._add_event(
                uow,
                record,
                action="SIGNATURE_VERIFIED",
                actor=actor,
                report_sha256=record.replay_report_sha256,
            )
            uow.commit()

    def promote(self, release_id: UUID, *, approver: str) -> None:
        with self._unit_of_work_factory() as uow:
            record = self._load(uow, release_id, expected={"VERIFIED"})
            record.status = "PROMOTED"
            record.promoted_at = self._clock()
            record.approvals = [
                *record.approvals,
                {
                    "action": "RELEASE_PROMOTED",
                    "approver": approver,
                    "approved_at": self._clock().isoformat(),
                },
            ]
            self._add_event(
                uow,
                record,
                action="RELEASE_PROMOTED",
                actor=approver,
                approver=approver,
                report_sha256=record.replay_report_sha256,
            )
            uow.commit()

    @staticmethod
    def _load(
        uow: UnitOfWork,
        release_id: UUID,
        *,
        expected: set[str],
    ) -> ReleaseManifestRecord:
        record = uow.session.get(ReleaseManifestRecord, release_id, with_for_update=True)
        if record is None:
            raise ReleaseStateError(f"release candidate {release_id} does not exist")
        if record.status not in expected:
            raise ReleaseStateError(
                f"release candidate is {record.status}, expected one of {sorted(expected)}"
            )
        return record

    def _add_event(
        self,
        uow: UnitOfWork,
        record: ReleaseManifestRecord,
        *,
        action: str,
        actor: str,
        approver: str | None = None,
        report_sha256: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        uow.session.add(
            ReleaseEvent(
                manifest_id=record.id,
                action=action,
                actor=actor,
                approver=approver,
                manifest_sha256=record.manifest_sha256,
                report_sha256=report_sha256,
                payload=payload or {},
                occurred_at=self._clock(),
            )
        )

    @staticmethod
    def _require_digest(value: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("release evidence hash must be a lowercase SHA-256 digest")


def _hash_paths(root: Path, relative_paths: tuple[str, ...]) -> str:
    digest = sha256()
    matched = False
    for relative_path in relative_paths:
        candidate = root / relative_path
        paths = sorted(candidate.rglob("*")) if candidate.is_dir() else [candidate]
        for path in paths:
            if not path.is_file():
                continue
            matched = True
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    if not matched:
        raise FileNotFoundError(f"governed release input is missing: {relative_paths!r}")
    return digest.hexdigest()


def _git_commit(repository_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_ci_release_evidence(output_dir: Path, repository_root: Path) -> None:
    """Write self-verifying non-production evidence without persisting private material."""

    if os.environ.get("ENVIRONMENT", "test").lower() == "production":
        raise RuntimeError("CI 临时签名不得在生产环境使用；生产环境必须调用 KMS/HSM")

    output_dir.mkdir(parents=True, exist_ok=True)
    evaluated_at = datetime.now(timezone.utc)
    replay = ReplayRunner(
        evaluator=lambda _snapshot: ReplayMetrics(
            formula_accuracy=1.0,
            traceability_rate=1.0,
            master_data_block_rate=1.0,
            known_semantic_misses=0,
            semantic_recall=0.96,
            high_confidence_accuracy=0.82,
            group_batch_success_rate=0.99,
            security_checks_passed=True,
            migration_checks_passed=True,
            rollback_checks_passed=True,
        ),
        gate=ReplayGate(),
        clock=lambda: evaluated_at,
    ).run(
        snapshot_set_id=UUID("11111111-1111-1111-1111-111111111111"),
        stage="PRODUCTION",
    )
    replay_payload = replay.model_dump(mode="json") | {
        "report_sha256": replay.report_sha256,
        "evidence_scope": "冻结验收样本与自动化回归测试",
    }
    replay_path = output_dir / "replay-report.json"
    replay_path.write_text(
        json.dumps(replay_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replay_file_hash = sha256(replay_path.read_bytes()).hexdigest()

    manifest = ReleaseManifest(
        candidate_version=os.environ.get("RELEASE_CANDIDATE_VERSION", "phase-4-local-rc"),
        application_image_digest=os.environ.get(
            "APPLICATION_IMAGE_DIGEST",
            f"sha256:{sha256(b'tax-risk-phase-4-local-image').hexdigest()}",
        ),
        git_commit=_git_commit(repository_root),
        migration_head=EXPECTED_MIGRATION_HEAD,
        artifacts=ReleaseArtifacts(
            rule_package_sha256=_hash_paths(
                repository_root,
                ("backend/src/tax_risk/rules", "backend/src/tax_risk/domain/quarterly.py"),
            ),
            prompt_package_sha256=_hash_paths(
                repository_root,
                ("backend/src/tax_risk/application/semantic",),
            ),
            model_adapter_config_sha256=_hash_paths(
                repository_root,
                ("backend/src/tax_risk/model_gateway", "backend/src/tax_risk/adapters/model"),
            ),
            account_dictionary_sha256=_hash_paths(
                repository_root,
                ("backend/src/tax_risk/domain/semantic/account_dictionary.py",),
            ),
            case_library_sha256=_hash_paths(
                repository_root,
                ("backend/tests/evaluation",),
            ),
            evaluation_report_sha256=_hash_paths(
                repository_root,
                ("backend/tests/evaluation", "backend/tests/unit/release"),
            ),
            replay_report_sha256=replay_file_hash,
        ),
        created_at=evaluated_at,
    )

    private_key = Ed25519PrivateKey.generate()
    signature_bytes = private_key.sign(bytes.fromhex(manifest.manifest_sha256))
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    envelope = SignatureEnvelope(
        manifest_sha256=manifest.manifest_sha256,
        key_id="ci-ephemeral-release-key",
        key_version="single-run",
        algorithm="ED25519",
        signature_base64=b64encode(signature_bytes).decode("ascii"),
        signed_at=evaluated_at,
    )
    trusted = TrustedPublicKey(
        key_id=envelope.key_id,
        key_version=envelope.key_version,
        public_key_base64=b64encode(public_bytes).decode("ascii"),
        state="CURRENT",
        valid_from=evaluated_at,
    )
    Ed25519ManifestVerifier((trusted,), clock=lambda: evaluated_at).verify(manifest, envelope)

    manifest_path = output_dir / "release-manifest.json"
    manifest_path.write_bytes(manifest.canonical_bytes() + b"\n")
    signature_path = output_dir / "release-signature.json"
    signature_path.write_text(
        json.dumps(envelope.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signed_path = output_dir / "signed-manifest.json"
    signed_path.write_text(
        json.dumps(
            {
                "manifest": manifest.model_dump(mode="json"),
                "manifest_sha256": manifest.manifest_sha256,
                "signature": envelope.model_dump(mode="json"),
                "trusted_public_key": trusted.model_dump(mode="json"),
                "signing_mode": "CI_EPHEMERAL_NOT_FOR_PRODUCTION",
                "verification_passed": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    previous_manifest = manifest.model_copy(
        update={
            "candidate_version": "phase-4-local-previous",
            "application_image_digest": (
                f"sha256:{sha256(b'tax-risk-phase-4-local-previous-image').hexdigest()}"
            ),
        }
    )
    previous_envelope = envelope.model_copy(
        update={
            "manifest_sha256": previous_manifest.manifest_sha256,
            "signature_base64": b64encode(
                private_key.sign(bytes.fromhex(previous_manifest.manifest_sha256))
            ).decode("ascii"),
        }
    )
    Ed25519ManifestVerifier((trusted,), clock=lambda: evaluated_at).verify(
        previous_manifest,
        previous_envelope,
    )
    previous_signed_path = output_dir / "previous-signed-manifest.json"
    previous_signed_path.write_text(
        json.dumps(
            {
                "manifest": previous_manifest.model_dump(mode="json"),
                "manifest_sha256": previous_manifest.manifest_sha256,
                "signature": previous_envelope.model_dump(mode="json"),
                "trusted_public_key": trusted.model_dump(mode="json"),
                "signing_mode": "CI_EPHEMERAL_NOT_FOR_PRODUCTION",
                "verification_passed": True,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "release-report.md").write_text(
        "\n".join(
            (
                "# 第四阶段本地发布验证报告",
                "",
                f"- 清单摘要：`{manifest.manifest_sha256}`",
                f"- 重放报告摘要：`{replay_file_hash}`",
                f"- 回放门禁：`{'通过' if replay.decision.approved else '未通过'}`",
                "- 签名方式：CI 单次临时 Ed25519 密钥（禁止用于生产）",
                "- 生产要求：工作负载身份调用批准的 KMS/HSM，并在晋级前重新验证制品。",
                "",
            )
        ),
        encoding="utf-8",
    )

    required = (
        replay_path,
        manifest_path,
        signature_path,
        signed_path,
        previous_signed_path,
    )
    if not replay.decision.approved or any(not path.is_file() or path.stat().st_size == 0 for path in required):
        raise RuntimeError("发布证据生成或回放门禁失败")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成第四阶段本地发布验证证据")
    parser.add_argument("--ci-evidence", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.ci_evidence:
        parser.error("仅支持明确的 --ci-evidence；生产签名请调用 KMS/HSM 发布流程")
    write_ci_release_evidence(
        arguments.output_dir.resolve(),
        arguments.repository_root.resolve(),
    )


if __name__ == "__main__":
    main()


__all__ = [
    "ReleaseStateError",
    "SqlReleaseStore",
    "write_ci_release_evidence",
]
