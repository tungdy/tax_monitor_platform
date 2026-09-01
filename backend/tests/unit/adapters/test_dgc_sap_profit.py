from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import json
import ssl
from threading import Event

import httpx
import pytest

from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.adapters.ingest.csv_adapter import HeaderValidationError
from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcApiError,
    DgcCertificateError,
    DgcClientConfig,
    DgcFetchResult,
    DgcHttpError,
    DgcJsonError,
    DgcPaginationError,
    DgcResourceLimitError,
    DgcSapProfitAdapter,
    DgcSapProfitClient,
    DgcSapProfitError,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
    DgcSchemaError,
)


IAM_URL = "https://iam.example.test/v3/auth/tokens"
API_URL = "https://dgc.example.test/profit"


def _config(
    *,
    page_size: int = 2,
    max_pages: int = 10,
    max_records: int = 10_000,
    max_page_bytes: int = 10 * 1024 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
    token_ttl: float = 86_400,
) -> DgcClientConfig:
    return DgcClientConfig(
        iam_url=IAM_URL,
        api_url=API_URL,
        username="iam-user",
        password="controlled-secret",
        domain="hljtzb",
        project="cn-east-3",
        timeout=5,
        page_size=page_size,
        max_pages=max_pages,
        max_records=max_records,
        max_page_bytes=max_page_bytes,
        max_total_bytes=max_total_bytes,
        token_ttl=token_ttl,
    )


def _iam_response(token: str) -> httpx.Response:
    return httpx.Response(201, headers={"X-Subject-Token": token}, json={"token": {}})


def test_client_config_repr_does_not_disclose_password() -> None:
    rendered = repr(_config())

    assert "controlled-secret" not in rendered
    assert "password=" not in rendered


def test_client_config_normalizes_pinned_tls_identity() -> None:
    config = DgcClientConfig(
        api_url=API_URL,
        app_key="test-app-key",
        app_secret="test-app-secret",
        tls_server_name=" DGC.Example.Test ",
        tls_pinned_certificate_sha256=":".join(["ab"] * 32),
    )

    assert config.tls_server_name == "dgc.example.test"
    assert config.tls_pinned_certificate_sha256 == "AB" * 32


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("tls_server_name", "not-a-host"),
        ("tls_server_name", "https://dgc.example.test"),
        ("tls_pinned_certificate_sha256", "not-a-fingerprint"),
        ("tls_pinned_certificate_sha256", "00" * 31),
    ],
)
def test_client_config_rejects_invalid_pinned_tls_identity(
    setting: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        DgcClientConfig(
            api_url=API_URL,
            app_key="test-app-key",
            app_secret="test-app-secret",
            **{setting: value},
        )


def test_client_passes_configured_sni_without_changing_signed_host() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    config = DgcClientConfig(
        api_url="https://116.63.221.181/post/sapincome",
        app_key="test-app-key",
        app_secret="test-app-secret",
        tls_server_name="dgc.huaweicloud.com",
    )
    DgcSapProfitClient(
        config,
        httpx.Client(transport=httpx.MockTransport(handler)),
    ).fetch({})

    assert len(requests) == 1
    assert requests[0].extensions["sni_hostname"] == "dgc.huaweicloud.com"
    assert requests[0].headers["Host"] == "116.63.221.181"


def test_pinned_client_rejects_a_certificate_change_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certificate = ssl.DER_cert_to_PEM_cert(b"not-the-pinned-certificate")
    monkeypatch.setattr(ssl, "get_server_certificate", lambda *_args, **_kwargs: certificate)
    config = DgcClientConfig(
        api_url="https://116.63.221.181/post/sapincome",
        app_key="test-app-key",
        app_secret="test-app-secret",
        tls_pinned_certificate_sha256="00" * 32,
    )

    with pytest.raises(DgcCertificateError, match="fingerprint"):
        DgcSapProfitClient(config)


def test_app_secret_auth_uses_stable_apig_signature_without_iam_request() -> None:
    api_url = "https://dgc.example.test/profit?name=a%20b&empty="
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    config = DgcClientConfig(
        api_url=api_url,
        app_key="test-app-key",
        app_secret="test-app-secret",
        page_size=15_000,
        max_records=100_000,
    )
    client = DgcSapProfitClient(
        config,
        httpx.Client(transport=httpx.MockTransport(handler)),
        signing_clock=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
    )

    result = client.fetch({"gjahr": "2026", "monat": "03"})

    assert result.records == ()
    assert len(requests) == 1
    request = requests[0]
    assert request.content == (b'{"gjahr":"2026","monat":"03","limitValue":15000,"offsetValue":0}')
    assert request.headers["Host"] == "dgc.example.test"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-Sdk-Date"] == "20260715T010203Z"
    expected_authorization = (
        "SDK-HMAC-SHA256 Access=test-app-key, "
        "SignedHeaders=content-type;host;x-sdk-date, "
        "Signature=ba6b2e1dbed5311ac79d96a66155dce7c528b8db2b114c732fcc55b452cebdeb"
    )
    assert request.headers["Authorization"] == expected_authorization
    assert request.headers["x-Authorization"] == expected_authorization
    rendered = repr(config)
    assert "test-app-key" not in rendered
    assert "test-app-secret" not in rendered


def test_app_secret_get_mode_signs_paginated_query_without_a_request_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errCode": "DLM.0", "data": {"data": []}})

    client = DgcSapProfitClient(
        DgcClientConfig(
            api_url="https://dgc.example.test/post/hesiinvoice",
            request_method="GET",
            app_key="test-app-key",
            app_secret="test-app-secret",
            page_size=15_000,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
        signing_clock=lambda: datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc),
    )

    result = client.fetch({"company_code": "3000"})

    assert result.records == ()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.content == b""
    assert dict(request.url.params) == {
        "company_code": "3000",
        "limitValue": "15000",
        "offsetValue": "0",
    }
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"].startswith(
        "SDK-HMAC-SHA256 Access=test-app-key,"
    )


@pytest.mark.parametrize("request_method", ("GET", "POST"))
def test_hesi_app_secret_modes_fetch_every_25_row_page(request_method: str) -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request_method == "GET":
            offset = int(request.url.params["offsetValue"])
            assert request.url.params["limitValue"] == "25"
        else:
            body = json.loads(request.content)
            assert isinstance(body, dict)
            offset = int(body["offsetValue"])
            assert body["limitValue"] == 25
        offsets.append(offset)
        row_count = min(25, 250 - offset)
        rows = [{"id": offset + index} for index in range(row_count)]
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "totalSize": 250,
                    "rowSize": row_count,
                    "data": rows,
                },
            },
        )

    client = DgcSapProfitClient(
        DgcClientConfig(
            api_url="https://dgc.example.test/post/hesi",
            request_method=request_method,
            app_key="test-app-key",
            app_secret="test-app-secret",
            page_size=25,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch({"company_code": "3KF0"})

    assert offsets == [0, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250]
    assert len(result.records) == 250
    assert result.records[-1]["id"] == 249


def test_client_does_not_trust_an_undercounted_total_size() -> None:
    api_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        assert isinstance(body, dict)
        api_bodies.append(body)
        offset = body["offsetValue"]
        if offset == 0:
            rows = [{"id": 1}, {"id": 2}]
        elif offset == 2:
            rows = [{"id": 3}]
        else:
            rows = []
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "totalSize": 1,
                    "rowSize": len(rows),
                    "data": rows,
                },
            },
        )

    client = DgcSapProfitClient(
        _config(page_size=2),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch({})

    assert [body["offsetValue"] for body in api_bodies] == [0, 2]
    assert tuple(row["id"] for row in result.records) == (1, 2, 3)


def test_strict_offset_pagination_continues_after_short_nonempty_page() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        offsets.append(body["offsetValue"])
        rows = [{"id": body["offsetValue"]}]
        if body["offsetValue"] == 50:
            rows = []
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {"rowSize": len(rows), "data": rows},
            },
        )

    client = DgcSapProfitClient(
        DgcClientConfig(
            api_url="https://dgc.example.test/hesimingxi",
            app_key="test-app-key",
            app_secret="test-app-secret",
            page_size=25,
            strict_offset_pagination=True,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch({"company_code": "3KF0"})

    assert offsets == [0, 25, 50]
    assert tuple(row["id"] for row in result.records) == (0, 25)


def test_strict_offset_pagination_rejects_large_page_size() -> None:
    with pytest.raises(ValueError, match="must not exceed 25"):
        DgcClientConfig(
            api_url="https://dgc.example.test/hesiinvoice",
            app_key="test-app-key",
            app_secret="test-app-secret",
            page_size=26,
            strict_offset_pagination=True,
        )


def test_get_mode_rejects_null_or_structured_query_parameters() -> None:
    client = DgcSapProfitClient(
        DgcClientConfig(
            api_url="https://dgc.example.test/post/hesiinvoice",
            request_method="GET",
            app_key="test-app-key",
            app_secret="test-app-secret",
        ),
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    )

    with pytest.raises(DgcSchemaError, match="non-null scalar"):
        client.fetch({"company_code": None})
    with pytest.raises(DgcSchemaError, match="non-null scalar"):
        client.fetch({"company_code": ["3000"]})


def test_client_authenticates_paginates_and_preserves_json_decimal_precision() -> None:
    iam_bodies: list[object] = []
    api_bodies: list[dict[str, object]] = []
    api_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            iam_bodies.append(json.loads(request.content))
            return _iam_response("token-1")
        body = json.loads(request.content)
        assert isinstance(body, dict)
        api_bodies.append(body)
        api_tokens.append(request.headers["X-Auth-Token"])
        if body["offsetValue"] == 0:
            return httpx.Response(
                200,
                content=(
                    b'[{"company_code":"C001","cumulative_profit":123.4500},'
                    b'{"company_code":"C002","cumulative_profit":2.10}]'
                ),
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200,
            content=b'[{"company_code":"C003","cumulative_profit":0.01}]',
            headers={"Content-Type": "application/json"},
        )

    client = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch({"fiscalPeriod": "2026-Q1", "limitValue": 999})

    assert iam_bodies == [
        {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": "iam-user",
                            "password": "controlled-secret",
                            "domain": {"name": "hljtzb"},
                        }
                    },
                },
                "scope": {"project": {"name": "cn-east-3"}},
            }
        }
    ]
    assert api_bodies == [
        {"fiscalPeriod": "2026-Q1", "limitValue": 2, "offsetValue": 0},
        {"fiscalPeriod": "2026-Q1", "limitValue": 2, "offsetValue": 2},
    ]
    assert api_tokens == ["token-1", "token-1"]
    assert len(result.records) == 3
    assert result.records[0]["cumulative_profit"] == Decimal("123.4500")
    assert isinstance(result.records[0]["cumulative_profit"], Decimal)
    assert len(result.checksum) == 64


def test_client_continues_when_service_clamps_page_below_requested_limit() -> None:
    api_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        assert isinstance(body, dict)
        api_bodies.append(body)
        offset = body["offsetValue"]
        rows = [{"id": 5}]
        if offset == 0:
            rows = [{"id": 1}, {"id": 2}]
        elif offset == 2:
            rows = [{"id": 3}, {"id": 4}]
        return httpx.Response(
            200,
            json={
                "errCode": "DLM.0",
                "data": {
                    "rowSize": len(rows),
                    "columnSize": 1,
                    "success": True,
                    "data": rows,
                    "columnNames": ["id"],
                },
            },
        )

    client = DgcSapProfitClient(
        _config(page_size=5),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.fetch({})

    assert [body["offsetValue"] for body in api_bodies] == [0, 2, 4]
    assert tuple(record["id"] for record in result.records) == (1, 2, 3, 4, 5)


def test_client_caches_token_until_monotonic_ttl_expires() -> None:
    now = [100.0]
    token_calls = 0
    data_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            return _iam_response(f"token-{token_calls}")
        data_tokens.append(request.headers["X-Auth-Token"])
        return httpx.Response(200, json=[])

    client = DgcSapProfitClient(
        _config(page_size=10, token_ttl=10),
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )

    client.fetch({})
    now[0] = 109.9
    client.fetch({})
    now[0] = 110.0
    client.fetch({})

    assert token_calls == 2
    assert data_tokens == ["token-1", "token-1", "token-2"]


def test_concurrent_fetches_share_one_iam_token_refresh() -> None:
    token_request_started = Event()
    release_token_response = Event()
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            token_request_started.set()
            assert release_token_response.wait(timeout=2)
            return _iam_response("shared-token")
        assert request.headers["X-Auth-Token"] == "shared-token"
        return httpx.Response(200, json=[])

    client = DgcSapProfitClient(
        _config(page_size=10),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(client.fetch, {}) for _ in range(8)]
        assert token_request_started.wait(timeout=2)
        release_token_response.set()
        results = [future.result(timeout=2) for future in futures]

    assert token_calls == 1
    assert all(result.records == () for result in results)


def test_client_closes_only_an_internally_owned_http_client() -> None:
    injected = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    with DgcSapProfitClient(_config(), injected):
        pass
    assert injected.is_closed is False

    owned = DgcSapProfitClient(_config())
    owned.close()
    assert owned._client.is_closed is True
    injected.close()


@pytest.mark.parametrize("failure_kind", ["http", "dlm"])
def test_client_refreshes_token_once_after_authentication_failure(failure_kind: str) -> None:
    token_calls = 0
    data_calls = 0
    used_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, data_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            return _iam_response(f"token-{token_calls}")
        data_calls += 1
        used_tokens.append(request.headers["X-Auth-Token"])
        if data_calls == 1:
            if failure_kind == "http":
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(
                200,
                json={"code": "DLM.4211", "message": "expired", "data": []},
            )
        return httpx.Response(200, json={"code": "DLM.0", "data": []})

    result = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    ).fetch({})

    assert result.records == ()
    assert token_calls == 2
    assert data_calls == 2
    assert used_tokens == ["token-1", "token-2"]


@pytest.mark.parametrize("failure_kind", ["http", "dlm"])
def test_client_does_not_retry_authentication_more_than_once(failure_kind: str) -> None:
    token_calls = 0
    data_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, data_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            return _iam_response(f"token-{token_calls}")
        data_calls += 1
        if failure_kind == "http":
            return httpx.Response(403, json={"message": "rejected"})
        return httpx.Response(
            200,
            json={"code": "DLM.4211", "message": "rejected", "data": []},
        )

    client = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    expected_error = DgcHttpError if failure_kind == "http" else DgcApiError
    with pytest.raises(expected_error):
        client.fetch({})

    assert token_calls == 2
    assert data_calls == 2


@pytest.mark.parametrize("status_key", ("errorCode", "errCode"))
def test_non_success_dlm_code_fails_closed_without_retry(status_key: str) -> None:
    token_calls = 0
    data_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, data_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            return _iam_response("token-1")
        data_calls += 1
        return httpx.Response(
            200,
            json={status_key: "DLM.4018", "message": "API does not exist"},
        )

    client = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcApiError) as raised:
        client.fetch({})

    assert raised.value.dlm_code == "DLM.4018"
    assert raised.value.error_code == "DGC_API_ERROR"
    assert "API does not exist" not in str(raised.value)
    assert token_calls == 1
    assert data_calls == 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": "DLM.0", "rows": [{"id": 1}]}, ({"id": 1},)),
        ({"code": "DLM.0", "data": [{"id": 2}]}, ({"id": 2},)),
        ({"code": "DLM.0", "data": {"rows": [{"id": 3}]}}, ({"id": 3},)),
        (
            {
                "errCode": "DLM.0",
                "data": {
                    "totalSize": "1",
                    "rowSize": 1,
                    "columnSize": 1,
                    "success": True,
                    "data": [{"id": 4}],
                    "columnNames": ["id"],
                },
            },
            ({"id": 4},),
        ),
    ],
)
def test_client_accepts_supported_wrapped_success_shapes(
    payload: dict[str, object],
    expected: tuple[dict[str, int], ...],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        assert isinstance(body, dict)
        if body["offsetValue"] > 0:
            return httpx.Response(
                200,
                json={"errCode": "DLM.0", "data": {"rowSize": 0, "data": []}},
            )
        return httpx.Response(200, json=payload)

    result = DgcSapProfitClient(
        _config(page_size=10),
        httpx.Client(transport=httpx.MockTransport(handler)),
    ).fetch({})

    assert result.records == expected


@pytest.mark.parametrize(
    ("content", "error_type"),
    [
        (b"not-json", DgcJsonError),
        (b'{"code":"DLM.0","data":{}}', DgcSchemaError),
        (b'{"code":"DLM.0","rows":[1]}', DgcSchemaError),
        (b'{"code":0,"rows":[]}', DgcSchemaError),
        (b'{"code":"DLM.internal-url","rows":[]}', DgcSchemaError),
        (
            b'{"errCode":"DLM.0","data":{"success":false,"data":[]}}',
            DgcSchemaError,
        ),
        (
            b'{"errCode":"DLM.0","data":{"success":"true","data":[]}}',
            DgcSchemaError,
        ),
    ],
)
def test_client_fails_closed_for_invalid_json_or_schema(
    content: bytes,
    error_type: type[DgcSapProfitError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        return httpx.Response(200, content=content)

    client = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(error_type):
        client.fetch({})


def test_client_detects_repeated_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    client = DgcSapProfitClient(
        _config(page_size=2),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcPaginationError, match="repeated"):
        client.fetch({})


def test_client_fails_when_a_full_last_allowed_page_cannot_prove_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        return httpx.Response(200, json=[{"id": body["offsetValue"]}])

    client = DgcSapProfitClient(
        _config(page_size=1, max_pages=2),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcPaginationError, match="maximum of 2 pages"):
        client.fetch({})


def test_client_rejects_page_larger_than_requested_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}, {"id": 3}])

    client = DgcSapProfitClient(
        _config(page_size=2),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="more rows"):
        client.fetch({})


def test_client_enforces_total_record_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        offset = body["offsetValue"]
        return httpx.Response(200, json=[{"id": offset}, {"id": offset + 1}])

    client = DgcSapProfitClient(
        _config(page_size=2, max_records=3),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="record limit"):
        client.fetch({})


def test_client_streams_and_enforces_page_byte_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        return httpx.Response(200, content=b'[{"id":123456789}]')

    client = DgcSapProfitClient(
        _config(max_page_bytes=8),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="byte limit"):
        client.fetch({})


def test_client_requests_identity_encoding_and_rejects_compressed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            content=gzip.compress(b"[]"),
            headers={"Content-Encoding": "gzip"},
        )

    client = DgcSapProfitClient(
        _config(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="compression"):
        client.fetch({})


def test_client_enforces_total_response_byte_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == IAM_URL:
            return _iam_response("token")
        body = json.loads(request.content)
        offset = body["offsetValue"]
        return httpx.Response(200, content=f'[{{"id":{offset}}}]'.encode())

    client = DgcSapProfitClient(
        _config(
            page_size=1,
            max_page_bytes=16,
            max_total_bytes=16,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="total byte limit"):
        client.fetch({})


def test_total_byte_limit_includes_dlm_token_refresh_response() -> None:
    token_calls = 0
    data_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, data_calls
        if str(request.url) == IAM_URL:
            token_calls += 1
            return _iam_response(f"token-{token_calls}")
        data_calls += 1
        if data_calls == 1:
            return httpx.Response(200, content=b'{"code":"DLM.4211"}')
        return httpx.Response(200, content=b'[{"id":0}]')

    client = DgcSapProfitClient(
        _config(
            page_size=1,
            max_page_bytes=24,
            max_total_bytes=24,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DgcResourceLimitError, match="total byte limit"):
        client.fetch({})

    assert token_calls == 2
    assert data_calls == 2


def test_fetch_result_and_checksum_are_stable_for_equivalent_key_order() -> None:
    def fetch(content: bytes) -> DgcFetchResult:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == IAM_URL:
                return _iam_response("token")
            return httpx.Response(200, content=content)

        return DgcSapProfitClient(
            _config(page_size=10),
            httpx.Client(transport=httpx.MockTransport(handler)),
        ).fetch({})

    first = fetch(b'[{"company_code":"C001","amount":1.20}]')
    second = fetch(b'[{"amount":1.20,"company_code":"C001"}]')

    assert first.checksum == second.checksum
    with pytest.raises(FrozenInstanceError):
        first.checksum = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.records[0]["company_code"] = "changed"  # type: ignore[index]


def _adapter(
    records: tuple[dict[str, object], ...],
    *,
    field_map: DgcSapProfitFieldMap | None = None,
    metric_map: DgcSapProfitMetricMap | None = None,
    ledger: str = "0L",
    expected_company_code: str | None = None,
) -> DgcSapProfitAdapter:
    return DgcSapProfitAdapter(
        DgcFetchResult(records=records, checksum="a" * 64),
        field_map=field_map or DgcSapProfitFieldMap(),
        metric_map=metric_map or DgcSapProfitMetricMap(),
        ledger=ledger,
        expected_company_code=expected_company_code,
        currency="CNY",
        amount_scale=2,
        extracted_at=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
    )


def _sap_row(
    ztext: str,
    nyhsl: object,
    **overrides: object,
) -> dict[str, object]:
    return {
        "mandt": "100",
        "bukrs": "C001",
        "companyname": "Company One",
        "gjahr": "2026",
        "monat": "03",
        "rldnr": "0L",
        "hs": "10",
        "ztext": ztext,
        "nmhsl": "999999.99",
        "nyhsl": nyhsl,
    } | overrides


def test_adapter_maps_real_long_rows_and_uses_year_to_date_amount() -> None:
    adapter: BulkFileAdapter = _adapter(
        (
            _sap_row(
                "四、利润总额（损失以“－”号填列）",
                Decimal("100.00"),
                hs="10",
            ),
            _sap_row(
                "公允价值变动收益（损失以“－”号填列）",
                Decimal("-2.50"),
                hs="20",
            ),
            _sap_row("一、营业总收入", Decimal("500.00"), hs="30"),
        )
    )

    adapter.validate_header()
    parsed = list(adapter.iter_rows())

    assert adapter.checksum == "a" * 64
    assert [item.row_number for item in parsed] == [1, 2, 3]
    assert [item.error for item in parsed] == [None, None, None]
    assert [cast_row(item.value).metric_code for item in parsed] == [
        "cumulative_profit",
        "fair_value_change",
        "cumulative_revenue",
    ]
    rows = [cast_row(item.value) for item in parsed]
    assert [row.amount for row in rows] == [
        Decimal("100.00"),
        Decimal("-2.50"),
        Decimal("500.00"),
    ]
    assert all(row.period == date(2026, 3, 31) for row in rows)
    assert all(row.source_record_key.startswith("dgc-sap-profit:") for row in rows)
    assert len({row.source_record_key for row in rows}) == 3


def test_adapter_applies_custom_sap_field_map_and_row_values() -> None:
    field_map = DgcSapProfitFieldMap(
        client="CLIENT",
        company_code="BUKRS",
        company_name="NAME",
        fiscal_year="GJAHR",
        fiscal_period="MONAT",
        ledger="LEDGER",
        line_number="LINE",
        line_item="ITEM",
        current_month_amount="MONTH_AMOUNT",
        year_to_date_amount="YTD_AMOUNT",
    )
    adapter = _adapter(
        (
            {
                "CLIENT": 100,
                "BUKRS": " 1000 ",
                "NAME": "Custom Company",
                "GJAHR": "2026",
                "MONAT": "06",
                "LEDGER": "0L",
                "LINE": 42,
                "ITEM": "Custom Profit",
                "MONTH_AMOUNT": "1.00",
                "YTD_AMOUNT": "10.20",
            },
        ),
        field_map=field_map,
        metric_map=DgcSapProfitMetricMap(
            cumulative_profit=("Custom Profit",),
            fair_value_change=("Custom Fair Value",),
            cumulative_revenue=("Custom Revenue",),
        ),
    )

    rows = [cast_row(item.value) for item in adapter.iter_rows()]

    assert len(rows) == 1
    assert all(row.company_code == "1000" for row in rows)
    assert all(row.period == date(2026, 6, 30) for row in rows)
    assert all(row.currency == "CNY" for row in rows)
    assert rows[0].metric_code == "cumulative_profit"
    assert rows[0].amount == Decimal("10.20")


def test_adapter_filters_ledger_and_ignores_nonexact_or_unmapped_line_items() -> None:
    parsed = list(
        _adapter(
            (
                _sap_row("利润总额", "1.00", rldnr="2L", hs="1"),
                _sap_row("其中：利润总额", "2.00", hs="2"),
                _sap_row("管理费用", "3.00", hs="3"),
                _sap_row("利润总额", "4.00", hs="4"),
            )
        ).iter_rows()
    )

    assert len(parsed) == 1
    assert parsed[0].row_number == 4
    assert cast_row(parsed[0].value).amount == Decimal("4.00")


def test_adapter_rejects_every_duplicate_metric_row_without_aggregation() -> None:
    parsed = list(
        _adapter(
            (
                _sap_row("营业收入", "100.00", hs="30"),
                _sap_row("营业收入", "200.00", hs="31"),
            )
        ).iter_rows()
    )

    assert len(parsed) == 2
    assert all(item.value is None for item in parsed)
    assert all(item.error is not None for item in parsed)
    assert {item.error.error_code for item in parsed if item.error is not None} == {
        "DUPLICATE_FINANCIAL_METRIC"
    }


def test_adapter_does_not_synthesize_a_missing_metric_or_zero() -> None:
    parsed = list(_adapter((_sap_row("利润总额", "10.00"),)).iter_rows())

    assert len(parsed) == 1
    assert cast_row(parsed[0].value).metric_code == "cumulative_profit"
    assert cast_row(parsed[0].value).amount == Decimal("10.00")


def test_adapter_accepts_explicit_zero_and_rejects_missing_ytd_amount() -> None:
    parsed = list(
        _adapter(
            (
                _sap_row("利润总额", "0", hs="10"),
                _sap_row("公允价值变动损益", None, hs="20"),
            )
        ).iter_rows()
    )

    assert cast_row(parsed[0].value).amount == Decimal("0")
    assert parsed[1].value is None
    assert parsed[1].error is not None
    assert parsed[1].error.error_code == "MISSING_VALUE"
    assert parsed[1].error.field == "year_to_date_amount"


def test_adapter_derives_leap_year_month_end_and_rejects_special_periods() -> None:
    parsed = list(
        _adapter(
            (
                _sap_row("利润总额", "1.00", gjahr="2024", monat="02", hs="10"),
                _sap_row("营业收入", "2.00", gjahr="2024", monat="13", hs="20"),
            )
        ).iter_rows()
    )

    assert cast_row(parsed[0].value).period == date(2024, 2, 29)
    assert parsed[1].error is not None
    assert parsed[1].error.error_code == "INTEGER_OUT_OF_RANGE"
    assert parsed[1].error.field == "fiscal_period"


def test_adapter_rejects_company_outside_requested_scope() -> None:
    parsed = list(
        _adapter(
            (_sap_row("利润总额", "1.00", bukrs="C002"),),
            expected_company_code="C001",
        ).iter_rows()
    )

    assert parsed[0].value is None
    assert parsed[0].error is not None
    assert parsed[0].error.error_code == "DGC_RESPONSE_SCOPE_MISMATCH"


def test_adapter_source_key_is_stable_and_changes_with_sap_line_identity() -> None:
    first = cast_row(list(_adapter((_sap_row("利润总额", "1.00", hs="10"),)).iter_rows())[0].value)
    replay = cast_row(list(_adapter((_sap_row("利润总额", "9.00", hs="10"),)).iter_rows())[0].value)
    other_line = cast_row(
        list(_adapter((_sap_row("利润总额", "1.00", hs="11"),)).iter_rows())[0].value
    )

    assert first.source_record_key == replay.source_record_key
    assert first.source_record_key != other_line.source_record_key


def test_header_validation_rejects_invalid_required_mapping_configuration() -> None:
    adapter = _adapter(
        (),
        field_map=DgcSapProfitFieldMap(year_to_date_amount=""),
    )

    with pytest.raises(HeaderValidationError) as raised:
        adapter.validate_header()

    assert raised.value.error_code == "INVALID_FIELD_MAP"
    assert raised.value.missing_columns == ("year_to_date_amount",)


def test_header_validation_rejects_duplicate_metric_labels() -> None:
    adapter = _adapter(
        (),
        metric_map=DgcSapProfitMetricMap(
            cumulative_profit=("Same label",),
            fair_value_change=("Same label",),
            cumulative_revenue=("Revenue",),
        ),
    )

    with pytest.raises(HeaderValidationError) as raised:
        adapter.validate_header()

    assert raised.value.error_code == "INVALID_METRIC_MAP"


def cast_row(value: object) -> CanonicalFinancialRow:
    assert isinstance(value, CanonicalFinancialRow)
    return value
