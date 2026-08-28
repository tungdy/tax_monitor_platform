from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
import pytest

from tax_risk import main as main_module
from tax_risk.adapters.ingest.dgc_sap_profit import DgcClientConfig
from tax_risk.application.dgc_hesi_reimbursement import (
    DgcHesiReimbursementSource,
)
from tax_risk.application.external_fetch import CoordinatedDgcSource
from tax_risk.config import Settings


class _OwnedClient:
    instances: list[_OwnedClient] = []

    def __init__(self, config: DgcClientConfig) -> None:
        self.config = config
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


def test_create_app_owns_and_closes_enabled_hesi_reimbursement_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    settings = Settings(
        dgc_hesi_reimbursement_enabled=True,
        dgc_hesi_reimbursement_api_url="https://dgc.example.test/hesi-detail",
        dgc_hesi_reimbursement_app_key="hesi-key",
        dgc_hesi_reimbursement_app_secret="hesi-secret",
        dgc_hesi_reimbursement_page_size=25,
        dgc_tls_server_name=None,
        dgc_tls_pinned_certificate_sha256=None,
    )

    app = main_module.create_app(settings=settings)

    assert len(_OwnedClient.instances) == 1
    owned = _OwnedClient.instances[0]
    assert app.state.dgc_hesi_reimbursement_client is owned
    assert owned.config.api_url == "https://dgc.example.test/hesi-detail"
    assert owned.config.app_key == "hesi-key"
    assert owned.config.app_secret == "hesi-secret"
    assert owned.config.page_size == 25
    assert owned.config.strict_offset_pagination is True
    with TestClient(app):
        assert owned.closed is False
    assert owned.closed is True


def test_injected_hesi_source_uses_parallel_fetch_coordinator() -> None:
    source = cast(DgcHesiReimbursementSource, object())
    app = main_module.create_app(
        settings=Settings(
            external_fetch_enabled=True,
            external_fetch_cache_enabled=False,
        ),
        dgc_hesi_reimbursement_source=source,
    )

    assert isinstance(
        app.state.dgc_hesi_reimbursement_client,
        CoordinatedDgcSource,
    )
    assert app.state.external_fetch_coordinator is not None
