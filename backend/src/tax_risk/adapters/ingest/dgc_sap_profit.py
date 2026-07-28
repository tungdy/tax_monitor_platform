from __future__ import annotations

from calendar import monthrange
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import hmac
import json
from math import isfinite
import re
import ssl
from threading import Lock
from time import monotonic
from types import MappingProxyType
from typing import Literal, Never
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    CanonicalRowValidationError,
    RowError,
)
from tax_risk.adapters.ingest.csv_adapter import HeaderValidationError


_DLM_STATUS_KEYS = (
    "code",
    "errCode",
    "errorCode",
    "error_code",
    "resultCode",
    "result_code",
    "resCode",
)
_DLM_CODE_PATTERN = re.compile(r"DLM\.\d+")
_METRICS = ("cumulative_profit", "fair_value_change", "cumulative_revenue")


class DgcSapProfitError(RuntimeError):
    """Base class for stable, non-secret-bearing DGC client failures."""

    error_code = "DGC_SAP_PROFIT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DgcAuthenticationError(DgcSapProfitError):
    error_code = "DGC_AUTHENTICATION_FAILED"


class DgcTransportError(DgcSapProfitError):
    error_code = "DGC_TRANSPORT_FAILED"


class DgcCertificateError(DgcTransportError):
    error_code = "DGC_TLS_CERTIFICATE_FAILED"


class DgcHttpError(DgcSapProfitError):
    error_code = "DGC_HTTP_ERROR"

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class DgcResponseError(DgcSapProfitError):
    error_code = "DGC_RESPONSE_ERROR"


class DgcJsonError(DgcResponseError):
    error_code = "DGC_INVALID_JSON"


class DgcSchemaError(DgcResponseError):
    error_code = "DGC_INVALID_RESPONSE_SCHEMA"


class DgcApiError(DgcSapProfitError):
    error_code = "DGC_API_ERROR"

    def __init__(self, dlm_code: str) -> None:
        super().__init__(f"DGC data service returned {dlm_code}")
        self.dlm_code = dlm_code


class DgcPaginationError(DgcSapProfitError):
    error_code = "DGC_PAGINATION_ERROR"


class DgcResourceLimitError(DgcSapProfitError):
    error_code = "DGC_RESOURCE_LIMIT_EXCEEDED"


@dataclass(frozen=True, slots=True)
class DgcClientConfig:
    api_url: str
    request_method: Literal["GET", "POST"] = "POST"
    iam_url: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    domain: str | None = None
    project: str | None = None
    app_key: str | None = field(default=None, repr=False)
    app_secret: str | None = field(default=None, repr=False)
    timeout: float = 30.0
    page_size: int = 15_000
    max_pages: int = 1_000
    max_records: int = 100_000
    max_page_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    token_ttl: float = 24 * 60 * 60
    tls_server_name: str | None = None
    tls_pinned_certificate_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.api_url, str) or not self.api_url.strip():
            raise ValueError("api_url must be a non-empty string")
        parsed_api_url = urlsplit(self.api_url)
        if (
            parsed_api_url.scheme.lower() != "https"
            or parsed_api_url.hostname is None
            or parsed_api_url.username is not None
            or parsed_api_url.password is not None
            or parsed_api_url.fragment
        ):
            raise ValueError("api_url must be an HTTPS URL without credentials or a fragment")
        if self.request_method not in {"GET", "POST"}:
            raise ValueError("request_method must be GET or POST")

        app_values = (self.app_key, self.app_secret)
        iam_values = (self.iam_url, self.username, self.password, self.domain, self.project)
        app_configured = any(value is not None for value in app_values)
        iam_configured = any(value is not None for value in iam_values)
        if app_configured and not all(_nonempty(value) for value in app_values):
            raise ValueError("app_key and app_secret must both be non-empty strings")
        if iam_configured and not all(_nonempty(value) for value in iam_values):
            raise ValueError("all IAM authentication settings must be non-empty strings")
        if app_configured == iam_configured:
            raise ValueError("configure exactly one DGC authentication method")
        if app_configured:
            assert self.app_key is not None
            if any(character.isspace() or character in ",=" for character in self.app_key):
                raise ValueError("app_key contains characters that cannot be signed safely")
        else:
            assert self.iam_url is not None
            parsed_iam_url = urlsplit(self.iam_url)
            if (
                parsed_iam_url.scheme.lower() != "https"
                or parsed_iam_url.hostname is None
                or parsed_iam_url.username is not None
                or parsed_iam_url.password is not None
                or parsed_iam_url.fragment
            ):
                raise ValueError("iam_url must be an HTTPS URL without credentials or a fragment")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if type(self.page_size) is not int:
            raise TypeError("page_size must be an integer")
        if self.page_size <= 0:
            raise ValueError("page_size must be greater than zero")
        if type(self.max_pages) is not int:
            raise TypeError("max_pages must be an integer")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be greater than zero")
        if type(self.max_records) is not int:
            raise TypeError("max_records must be an integer")
        if self.max_records <= 0:
            raise ValueError("max_records must be greater than zero")
        if type(self.max_page_bytes) is not int:
            raise TypeError("max_page_bytes must be an integer")
        if self.max_page_bytes <= 0:
            raise ValueError("max_page_bytes must be greater than zero")
        if type(self.max_total_bytes) is not int:
            raise TypeError("max_total_bytes must be an integer")
        if self.max_total_bytes < self.max_page_bytes:
            raise ValueError("max_total_bytes must be at least max_page_bytes")
        if self.max_records < self.page_size:
            raise ValueError("max_records must be at least page_size")
        if isinstance(self.token_ttl, bool) or not isinstance(self.token_ttl, (int, float)):
            raise TypeError("token_ttl must be a number")
        if self.token_ttl <= 0:
            raise ValueError("token_ttl must be greater than zero")

        if self.tls_server_name is not None:
            if not isinstance(self.tls_server_name, str):
                raise TypeError("tls_server_name must be a string")
            normalized_server_name = self.tls_server_name.strip().lower()
            try:
                ascii_server_name = normalized_server_name.encode("idna").decode("ascii")
            except UnicodeError as error:
                raise ValueError("tls_server_name must be a valid DNS name") from error
            parsed_server_name = urlsplit(f"https://{ascii_server_name}")
            if (
                not ascii_server_name
                or len(ascii_server_name) > 253
                or parsed_server_name.hostname != ascii_server_name
                or parsed_server_name.netloc != ascii_server_name
                or "." not in ascii_server_name
            ):
                raise ValueError("tls_server_name must be a valid DNS name")
            object.__setattr__(self, "tls_server_name", ascii_server_name)

        if self.tls_pinned_certificate_sha256 is not None:
            if not isinstance(self.tls_pinned_certificate_sha256, str):
                raise TypeError("tls_pinned_certificate_sha256 must be a string")
            normalized_fingerprint = self.tls_pinned_certificate_sha256.replace(":", "").upper()
            if re.fullmatch(r"[0-9A-F]{64}", normalized_fingerprint) is None:
                raise ValueError(
                    "tls_pinned_certificate_sha256 must be a 64-character SHA-256 fingerprint"
                )
            object.__setattr__(
                self,
                "tls_pinned_certificate_sha256",
                normalized_fingerprint,
            )


@dataclass(frozen=True, slots=True)
class DgcSapProfitFieldMap:
    client: str = "mandt"
    company_code: str = "bukrs"
    company_name: str = "companyname"
    fiscal_year: str = "gjahr"
    fiscal_period: str = "monat"
    ledger: str = "rldnr"
    line_number: str = "hs"
    line_item: str = "ztext"
    current_month_amount: str = "nmhsl"
    year_to_date_amount: str = "nyhsl"


@dataclass(frozen=True, slots=True)
class DgcSapProfitMetricMap:
    cumulative_profit: tuple[str, ...] = (
        "利润总额",
        "四、利润总额",
        "四、利润总额（损失以“－”号填列）",
        '四、利润总额(损失以"-"号填列)',
    )
    fair_value_change: tuple[str, ...] = (
        "公允价值变动收益",
        "公允价值变动损益",
        "公允价值变动收益（损失以“－”号填列）",
        '公允价值变动收益(损失以"-"号填列)',
    )
    cumulative_revenue: tuple[str, ...] = ("一、营业总收入", "营业收入")


@dataclass(frozen=True, slots=True)
class DgcFetchResult:
    records: tuple[Mapping[str, object], ...]
    checksum: str

    def __post_init__(self) -> None:
        immutable_records = tuple(MappingProxyType(dict(record)) for record in self.records)
        object.__setattr__(self, "records", immutable_records)


class DgcSapProfitClient:
    """Synchronous APIG-signed or IAM-authenticated paginated DGC reader."""

    def __init__(
        self,
        config: DgcClientConfig,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = monotonic,
        signing_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        if client is None:
            tls_context = _pinned_tls_context(config)
            if tls_context is None:
                self._client = httpx.Client(
                    timeout=config.timeout,
                    trust_env=False,
                    follow_redirects=False,
                )
            else:
                self._client = httpx.Client(
                    timeout=config.timeout,
                    verify=tls_context,
                    trust_env=False,
                    follow_redirects=False,
                )
        else:
            self._client = client
        self._clock = clock
        self._signing_clock = signing_clock or _utc_now
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = Lock()

    def __enter__(self) -> DgcSapProfitClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close only the HTTP client created and owned by this instance."""

        if self._owns_client:
            self._client.close()

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        if not isinstance(parameters, Mapping) or any(
            not isinstance(key, str) for key in parameters
        ):
            raise DgcSchemaError("DGC request parameters must be an object with string keys")

        records: list[Mapping[str, object]] = []
        page_checksums: set[str] = set()
        offset = 0
        total_response_bytes = 0
        effective_page_size = self._config.page_size
        for _page_number in range(1, self._config.max_pages + 1):
            body = dict(parameters)
            body["limitValue"] = self._config.page_size
            body["offsetValue"] = offset
            page, response_bytes, total_size, row_size = self._fetch_page(body)
            total_response_bytes += response_bytes
            if total_response_bytes > self._config.max_total_bytes:
                raise DgcResourceLimitError("DGC result exceeded the configured total byte limit")
            if len(page) > self._config.page_size:
                raise DgcResourceLimitError(
                    "DGC data service returned more rows than the requested page size"
                )
            if len(records) + len(page) > self._config.max_records:
                raise DgcResourceLimitError("DGC result exceeded the configured record limit")
            page_checksum = _checksum(page)
            if page and page_checksum in page_checksums:
                raise DgcPaginationError("DGC pagination returned a repeated non-empty page")
            if page:
                page_checksums.add(page_checksum)
            records.extend(page)
            if total_size is not None and len(records) >= total_size:
                frozen_records = tuple(records)
                return DgcFetchResult(
                    records=frozen_records,
                    checksum=_checksum(frozen_records),
                )
            if row_size is None:
                if len(page) < self._config.page_size:
                    frozen_records = tuple(records)
                    return DgcFetchResult(
                        records=frozen_records,
                        checksum=_checksum(frozen_records),
                    )
                offset += self._config.page_size
                continue
            if not page or (offset > 0 and len(page) < effective_page_size):
                frozen_records = tuple(records)
                return DgcFetchResult(
                    records=frozen_records,
                    checksum=_checksum(frozen_records),
                )
            effective_page_size = min(effective_page_size, len(page))
            offset += len(page)

        raise DgcPaginationError(
            f"DGC pagination exceeded the configured maximum of {self._config.max_pages} pages"
        )

    def _fetch_page(
        self,
        body: Mapping[str, object],
    ) -> tuple[tuple[Mapping[str, object], ...], int, int | None, int | None]:
        response_bytes = 0
        attempts = 1 if self._config.app_key is not None else 2
        for attempt in range(attempts):
            token = None if self._config.app_key is not None else self._get_token()
            response = self._request_data(body, token)
            response_bytes += len(response.content)
            if response.status_code in {401, 403}:
                if token is not None and attempt == 0:
                    self._invalidate_token(token)
                    continue
                raise DgcHttpError(
                    response.status_code,
                    "DGC data service rejected the configured authentication credentials",
                )
            if not 200 <= response.status_code < 300:
                raise DgcHttpError(
                    response.status_code,
                    f"DGC data service returned HTTP {response.status_code}",
                )

            payload = _decode_json(response)
            dlm_code = _extract_dlm_code(payload)
            if dlm_code == "DLM.4211":
                if token is not None and attempt == 0:
                    self._invalidate_token(token)
                    continue
                raise DgcApiError(dlm_code)
            if dlm_code is not None and dlm_code != "DLM.0":
                raise DgcApiError(dlm_code)
            total_size, row_size = _extract_pagination_metadata(payload)
            return _extract_records(payload), response_bytes, total_size, row_size

        raise AssertionError("DGC token retry loop exhausted unexpectedly")

    def _get_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token_expires_at:
            return self._token

        with self._token_lock:
            now = self._clock()
            if self._token is not None and now < self._token_expires_at:
                return self._token
            return self._request_token()

    def _request_token(self) -> str:
        """Request a token while the caller holds the dedicated token lock."""

        assert self._config.iam_url is not None
        assert self._config.username is not None
        assert self._config.password is not None
        assert self._config.domain is not None
        assert self._config.project is not None
        request_body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self._config.username,
                            "password": self._config.password,
                            "domain": {"name": self._config.domain},
                        }
                    },
                },
                "scope": {"project": {"name": self._config.project}},
            }
        }
        try:
            with self._client.stream(
                "POST",
                self._config.iam_url,
                json=request_body,
                timeout=self._config.timeout,
            ) as response:
                status_code = response.status_code
                token = response.headers.get("X-Subject-Token")
        except httpx.HTTPError as error:
            raise DgcTransportError("IAM token request failed at the transport layer") from error
        except (TypeError, ValueError) as error:
            raise DgcSchemaError("IAM token request could not be encoded as JSON") from error

        if not 200 <= status_code < 300:
            raise DgcAuthenticationError(f"IAM token endpoint returned HTTP {status_code}")
        if token is None or not token.strip():
            raise DgcAuthenticationError("IAM token response did not include X-Subject-Token")

        self._token = token.strip()
        self._token_expires_at = self._clock() + self._config.token_ttl
        return self._token

    def _request_data(self, body: Mapping[str, object], token: str | None) -> httpx.Response:
        method = self._config.request_method
        if method == "POST":
            try:
                encoded_body = json.dumps(
                    dict(body),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise DgcSchemaError("DGC data request could not be encoded as JSON") from error
            request_url = self._config.api_url
        else:
            encoded_body = b""
            request_url = _query_request_url(self._config.api_url, body)

        headers = {
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        if self._config.app_key is not None:
            assert self._config.app_secret is not None
            headers.update(
                _app_secret_headers(
                    method=method,
                    url=request_url,
                    body=encoded_body,
                    app_key=self._config.app_key,
                    app_secret=self._config.app_secret,
                    now=self._signing_clock(),
                )
            )
        else:
            assert token is not None
            headers["X-Auth-Token"] = token
        try:
            with self._client.stream(
                method,
                request_url,
                content=encoded_body,
                headers=headers,
                timeout=self._config.timeout,
                extensions=(
                    {"sni_hostname": self._config.tls_server_name}
                    if self._config.tls_server_name is not None
                    else None
                ),
            ) as response:
                content = bytearray()
                if 200 <= response.status_code < 300:
                    content_encoding = response.headers.get("Content-Encoding", "identity")
                    if content_encoding.strip().lower() not in {"", "identity"}:
                        raise DgcResourceLimitError("DGC response compression is not allowed")
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > self._config.max_page_bytes:
                            raise DgcResourceLimitError(
                                "DGC response page exceeded the configured byte limit"
                            )
                        content.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    request=response.request,
                )
        except httpx.HTTPError as error:
            raise DgcTransportError("DGC data request failed at the transport layer") from error

    def _invalidate_token(self, rejected_token: str) -> None:
        with self._token_lock:
            if self._token == rejected_token:
                self._token = None
                self._token_expires_at = 0.0


def _query_request_url(url: str, parameters: Mapping[str, object]) -> str:
    parsed = urlsplit(url)
    query_items: list[tuple[str, str]] = list(parse_qsl(parsed.query, keep_blank_values=True))
    for name, value in parameters.items():
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise DgcSchemaError("DGC GET query parameters must be non-null scalar values")
        if isinstance(value, float) and not isfinite(value):
            raise DgcSchemaError("DGC GET query parameters must be finite")
        if isinstance(value, bool):
            normalized = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            normalized = str(value)
        else:
            raise DgcSchemaError("DGC GET query parameters must be scalar JSON values")
        query_items.append((name, normalized))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_items),
            "",
        )
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pinned_tls_context(config: DgcClientConfig) -> ssl.SSLContext | None:
    expected_fingerprint = config.tls_pinned_certificate_sha256
    if expected_fingerprint is None:
        return None

    parsed = urlsplit(config.api_url)
    assert parsed.hostname is not None
    try:
        pem_certificate = ssl.get_server_certificate(
            (parsed.hostname, parsed.port or 443),
            timeout=config.timeout,
        )
        der_certificate = ssl.PEM_cert_to_DER_cert(pem_certificate)
    except (OSError, ValueError, ssl.SSLError) as error:
        raise DgcCertificateError(
            "DGC TLS certificate preflight failed before authentication"
        ) from error

    actual_fingerprint = sha256(der_certificate).hexdigest().upper()
    if not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        raise DgcCertificateError(
            "DGC TLS certificate fingerprint did not match the configured pin"
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = config.tls_server_name is not None
    try:
        context.load_verify_locations(cadata=pem_certificate)
    except ssl.SSLError as error:
        raise DgcCertificateError("DGC pinned TLS certificate could not be trusted") from error
    return context


def _app_secret_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    app_key: str,
    app_secret: str,
    now: datetime,
) -> dict[str, str]:
    parsed = urlsplit(url)
    sdk_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = parsed.netloc
    signed = {
        "content-type": "application/json",
        "host": host,
        "x-sdk-date": sdk_date,
    }
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(
        f"{name}:{' '.join(signed[name].split())}\n" for name in sorted(signed)
    )
    canonical_uri = quote(unquote(parsed.path or "/"), safe="/-_.~")
    # Huawei APIG signs a canonical path ending in a slash, even when the URL does not.
    if not canonical_uri.endswith("/"):
        canonical_uri += "/"
    canonical_query = "&".join(
        f"{name}={value}"
        for name, value in sorted(
            (
                quote(name, safe="-_.~"),
                quote(value, safe="-_.~"),
            )
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    )
    canonical_request = "\n".join(
        (
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            sha256(body).hexdigest(),
        )
    )
    string_to_sign = "\n".join(
        (
            "SDK-HMAC-SHA256",
            sdk_date,
            sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    signature = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        sha256,
    ).hexdigest()
    authorization = (
        f"SDK-HMAC-SHA256 Access={app_key}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": host,
        "X-Sdk-Date": sdk_date,
        "Authorization": authorization,
        "x-Authorization": authorization,
    }


class DgcSapProfitAdapter:
    """Map the real SAP long-form profit statement into canonical YTD metrics."""

    def __init__(
        self,
        result: DgcFetchResult,
        *,
        field_map: DgcSapProfitFieldMap,
        metric_map: DgcSapProfitMetricMap,
        ledger: str,
        expected_company_code: str | None = None,
        currency: str,
        amount_scale: int,
        extracted_at: datetime,
    ) -> None:
        self._result = result
        self._field_map = field_map
        self._metric_map = metric_map
        self._ledger = ledger
        self._expected_company_code = expected_company_code
        self._currency = currency
        self._amount_scale = amount_scale
        self._extracted_at = extracted_at

    @property
    def checksum(self) -> str:
        return self._result.checksum

    def validate_header(self) -> None:
        invalid_mappings = tuple(
            item.name
            for item in fields(self._field_map)
            if not isinstance(getattr(self._field_map, item.name), str)
            or not getattr(self._field_map, item.name).strip()
        )
        if invalid_mappings:
            raise HeaderValidationError(
                "INVALID_FIELD_MAP",
                "DGC SAP profit field mappings must be non-empty strings",
                missing_columns=invalid_mappings,
            )

        source_fields = tuple(
            getattr(self._field_map, item.name) for item in fields(self._field_map)
        )
        if len(set(source_fields)) != len(source_fields):
            raise HeaderValidationError(
                "INVALID_FIELD_MAP",
                "DGC SAP profit field mappings must be distinct",
            )

        if not isinstance(self._ledger, str) or not self._ledger.strip():
            raise HeaderValidationError(
                "INVALID_LEDGER",
                "DGC SAP profit ledger must be a non-empty string",
            )
        if self._expected_company_code is not None and (
            not isinstance(self._expected_company_code, str)
            or not self._expected_company_code.strip()
        ):
            raise HeaderValidationError(
                "INVALID_COMPANY_SCOPE",
                "DGC SAP profit expected company code must be a non-empty string",
            )

        invalid_metrics: list[str] = []
        normalized_labels: list[str] = []
        for item in fields(self._metric_map):
            labels = getattr(self._metric_map, item.name)
            if (
                not isinstance(labels, tuple)
                or not labels
                or any(not isinstance(label, str) or not label.strip() for label in labels)
            ):
                invalid_metrics.append(item.name)
                continue
            normalized_labels.extend(label.strip() for label in labels)
        if invalid_metrics:
            raise HeaderValidationError(
                "INVALID_METRIC_MAP",
                "DGC SAP profit metric labels must be non-empty string tuples",
                missing_columns=tuple(invalid_metrics),
            )
        if len(set(normalized_labels)) != len(normalized_labels):
            raise HeaderValidationError(
                "INVALID_METRIC_MAP",
                "DGC SAP profit metric labels must be unique",
            )

        if not isinstance(self._result.records, tuple):
            raise HeaderValidationError(
                "INVALID_DGC_RESPONSE",
                "DGC SAP profit records must be an immutable sequence",
            )
        if any(
            not isinstance(record, Mapping) or any(not isinstance(key, str) for key in record)
            for record in self._result.records
        ):
            raise HeaderValidationError(
                "INVALID_DGC_RESPONSE",
                "every DGC SAP profit record must be an object with string keys",
            )

    def iter_rows(self) -> Iterator[AdapterRow]:
        self.validate_header()
        labels = self._label_to_metric()
        duplicate_counts: Counter[tuple[str, int, int, str, str]] = Counter()
        for raw in self._result.records:
            try:
                candidate_metric = self._classify(raw, labels)
                if candidate_metric is None:
                    continue
                duplicate_counts[self._logical_identity(raw, candidate_metric)] += 1
            except _FieldError:
                continue

        for row_number, raw in enumerate(self._result.records, start=1):
            metric_code: str | None = None
            try:
                metric_code = self._classify(raw, labels)
                if metric_code is None:
                    continue
                identity = self._logical_identity(raw, metric_code)
                if duplicate_counts[identity] > 1:
                    _fail(
                        "DUPLICATE_FINANCIAL_METRIC",
                        "multiple SAP rows map to the same company, period, ledger, and metric",
                        "metric_code",
                        metric_code,
                    )
                value = self._parse_metric(raw, metric_code)
            except _FieldError as error:
                yield AdapterRow(
                    row_number=row_number,
                    value=None,
                    error=RowError(
                        row_number=row_number,
                        error_code=error.error_code,
                        message=error.message,
                        field=error.field,
                        rejected_value=error.rejected_value,
                        context=_safe_error_context(raw, self._field_map, metric_code),
                    ),
                )
            except CanonicalRowValidationError as error:
                field = metric_code if error.field == "amount" else error.field
                yield AdapterRow(
                    row_number=row_number,
                    value=None,
                    error=RowError(
                        row_number=row_number,
                        error_code=error.error_code,
                        message=error.message,
                        field=field,
                        rejected_value=error.rejected_value,
                        context=_safe_error_context(raw, self._field_map, metric_code),
                    ),
                )
            else:
                yield AdapterRow(row_number=row_number, value=value, error=None)

    def _label_to_metric(self) -> dict[str, str]:
        return {
            label.strip(): item.name
            for item in fields(self._metric_map)
            for label in getattr(self._metric_map, item.name)
        }

    def _classify(
        self,
        raw: Mapping[str, object],
        labels: Mapping[str, str],
    ) -> str | None:
        ledger = _text(
            _required(raw, self._field_map.ledger, "ledger"),
            "ledger",
        )
        if ledger != self._ledger.strip():
            return None
        line_item = _text(
            _required(raw, self._field_map.line_item, "line_item"),
            "line_item",
        )
        return labels.get(line_item)

    def _logical_identity(
        self,
        raw: Mapping[str, object],
        metric_code: str,
    ) -> tuple[str, int, int, str, str]:
        company_code = _text(
            _required(raw, self._field_map.company_code, "company_code"),
            "company_code",
        )
        if (
            self._expected_company_code is not None
            and company_code != self._expected_company_code.strip()
        ):
            _fail(
                "DGC_RESPONSE_SCOPE_MISMATCH",
                "DGC SAP profit response contained a company outside the requested scope",
                "company_code",
                company_code,
            )
        fiscal_year = _integer(
            _required(raw, self._field_map.fiscal_year, "fiscal_year"),
            "fiscal_year",
            minimum=2000,
            maximum=9999,
        )
        fiscal_period = _integer(
            _required(raw, self._field_map.fiscal_period, "fiscal_period"),
            "fiscal_period",
            minimum=1,
            maximum=12,
        )
        ledger = _text(
            _required(raw, self._field_map.ledger, "ledger"),
            "ledger",
        )
        return company_code, fiscal_year, fiscal_period, ledger, metric_code

    def _parse_metric(
        self,
        raw: Mapping[str, object],
        metric_code: str,
    ) -> CanonicalFinancialRow:
        company_code, fiscal_year, fiscal_period, ledger, _ = self._logical_identity(
            raw,
            metric_code,
        )
        period = date(
            fiscal_year,
            fiscal_period,
            monthrange(fiscal_year, fiscal_period)[1],
        )
        client = _identity_text(
            _required(raw, self._field_map.client, "client"),
            "client",
        )
        line_number = _identity_text(
            _required(raw, self._field_map.line_number, "line_number"),
            "line_number",
        )
        amount = _decimal(
            _required(raw, self._field_map.year_to_date_amount, "year_to_date_amount"),
            "year_to_date_amount",
        )
        source_identity = json.dumps(
            (
                client,
                company_code,
                str(fiscal_year),
                f"{fiscal_period:02d}",
                ledger,
                line_number,
                metric_code,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return CanonicalFinancialRow(
            source_record_key=f"dgc-sap-profit:{sha256(source_identity).hexdigest()}",
            company_code=company_code,
            fiscal_year=fiscal_year,
            period=period,
            currency=self._currency,
            amount_scale=self._amount_scale,
            metric_code=metric_code,
            amount=amount,
            extracted_at=self._extracted_at,
        )


@dataclass(frozen=True, slots=True)
class _FieldError(Exception):
    error_code: str
    message: str
    field: str
    rejected_value: str | None


def _decode_json(response: httpx.Response) -> object:
    try:
        return json.loads(
            response.content.decode("utf-8-sig"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DgcJsonError("DGC data service returned invalid JSON") from error


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant {value!r} is not allowed")


def _extract_dlm_code(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    codes: list[str] = []
    for key in _DLM_STATUS_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or _DLM_CODE_PATTERN.fullmatch(value) is None:
            raise DgcSchemaError("DGC response status code must use the DLM.<number> format")
        codes.append(value)
    if len(set(codes)) > 1:
        raise DgcSchemaError("DGC response contains conflicting status codes")
    return codes[0] if codes else None


def _extract_pagination_metadata(payload: object) -> tuple[int | None, int | None]:
    if not isinstance(payload, Mapping):
        return None, None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None, None
    return (
        _optional_nonnegative_int(data.get("totalSize"), "totalSize"),
        _optional_nonnegative_int(data.get("rowSize"), "rowSize"),
    )


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DgcSchemaError(f"DGC response {field} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise DgcSchemaError(f"DGC response {field} must be a non-negative integer")
    if parsed < 0:
        raise DgcSchemaError(f"DGC response {field} must be a non-negative integer")
    return parsed


def _extract_records(payload: object) -> tuple[Mapping[str, object], ...]:
    candidate: object
    if isinstance(payload, list):
        candidate = payload
    elif isinstance(payload, Mapping):
        if "rows" in payload:
            candidate = payload["rows"]
        elif "data" in payload:
            data = payload["data"]
            if isinstance(data, Mapping):
                if "success" in data and data["success"] is not True:
                    raise DgcSchemaError("DGC response data success must be true")
                if "rows" in data:
                    candidate = data["rows"]
                else:
                    candidate = data.get("data")
            else:
                candidate = data
        else:
            raise DgcSchemaError("DGC response object must contain data or rows")
    else:
        raise DgcSchemaError("DGC response must be a record list or wrapper object")

    if not isinstance(candidate, list):
        raise DgcSchemaError("DGC response rows must be a list")
    records: list[Mapping[str, object]] = []
    for item in candidate:
        if not isinstance(item, Mapping) or any(not isinstance(key, str) for key in item):
            raise DgcSchemaError("every DGC response row must be an object with string keys")
        records.append(dict(item))
    return tuple(records)


def _checksum(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value {type(value).__name__}")


def _required(raw: Mapping[str, object], source_field: str, logical_field: str) -> object:
    if source_field not in raw or raw[source_field] is None:
        _fail("MISSING_VALUE", f"{logical_field} is required", logical_field, None)
    return raw[source_field]


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TEXT", f"{field} must be a string", field, _rejected(value))
    parsed = value.strip()
    if not parsed:
        _fail("MISSING_VALUE", f"{field} is required", field, parsed)
    return parsed


def _identity_text(value: object, field: str) -> str:
    if isinstance(value, str):
        parsed = value.strip()
    elif type(value) is int:
        parsed = str(value)
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        parsed = format(value, "f")
    else:
        _fail("INVALID_TEXT", f"{field} must be a string or integer", field, _rejected(value))
    if not parsed:
        _fail("MISSING_VALUE", f"{field} is required", field, parsed)
    return parsed


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    parsed: int
    if type(value) is int:
        parsed = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            parsed = int(stripped)
        except ValueError:
            _fail("INVALID_INTEGER", f"{field} must be an integer", field, stripped)
        if not stripped or any(character not in "+-0123456789" for character in stripped):
            _fail("INVALID_INTEGER", f"{field} must be an integer", field, stripped)
    else:
        _fail("INVALID_INTEGER", f"{field} must be an integer", field, _rejected(value))
    if parsed < minimum or parsed > maximum:
        _fail(
            "INTEGER_OUT_OF_RANGE",
            f"{field} must be between {minimum} and {maximum}",
            field,
            _rejected(value),
        )
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        _fail("INVALID_DECIMAL", f"{field} must be an exact decimal", field, _rejected(value))
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is int:
        parsed = Decimal(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            _fail("MISSING_VALUE", f"{field} is required", field, stripped)
        try:
            parsed = Decimal(stripped)
        except InvalidOperation:
            _fail(
                "INVALID_DECIMAL",
                f"{field} must be an exact decimal",
                field,
                stripped,
            )
    else:
        _fail("INVALID_DECIMAL", f"{field} must be an exact decimal", field, _rejected(value))
    if not parsed.is_finite():
        _fail(
            "INVALID_DECIMAL",
            f"{field} must be a finite decimal",
            field,
            _rejected(value),
        )
    return parsed


def _safe_error_context(
    raw: Mapping[str, object],
    field_map: DgcSapProfitFieldMap,
    metric_code: str | None,
) -> tuple[tuple[str, str], ...]:
    context: list[tuple[str, str]] = []
    company = raw.get(field_map.company_code)
    if isinstance(company, str):
        normalized = company.strip()
        if normalized and len(normalized) <= 64:
            context.append(("company_code", normalized))
    if metric_code is not None:
        context.append(("metric_code", metric_code))
    return tuple(context)


def _rejected(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, Decimal, float, bool)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return type(value).__name__


def _fail(error_code: str, message: str, field: str, value: str | None) -> Never:
    raise _FieldError(error_code, message, field, value)


__all__ = [
    "DgcApiError",
    "DgcAuthenticationError",
    "DgcCertificateError",
    "DgcClientConfig",
    "DgcFetchResult",
    "DgcHttpError",
    "DgcJsonError",
    "DgcPaginationError",
    "DgcResponseError",
    "DgcResourceLimitError",
    "DgcSapProfitAdapter",
    "DgcSapProfitClient",
    "DgcSapProfitError",
    "DgcSapProfitFieldMap",
    "DgcSapProfitMetricMap",
    "DgcSchemaError",
    "DgcTransportError",
]
