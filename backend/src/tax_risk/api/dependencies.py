"""FastAPI dependencies for identity, roles, and SQL company scopes."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from hashlib import sha256
from typing import Annotated, Any, Never, cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from tax_risk.config import Settings
from tax_risk.security.authentication import AuthenticationService
from tax_risk.security.context import bind_principal, reset_principal
from tax_risk.security.policies import (
    DEFAULT_POLICY,
    Action,
    AuthorizationDenied,
    ResourceNotFound,
)
from tax_risk.security.principal import (
    API_ROLES,
    AUDIT_ROLE,
    COMPANY_FINANCE_ROLE,
    GROUP_TAX_ROLE,
    Principal,
    PrincipalProvider,
)


async def get_principal(
    request: Request,
    development_principal: Annotated[
        str | None,
        Header(alias="X-Development-Principal"),
    ] = None,
    development_signature: Annotated[
        str | None,
        Header(alias="X-Development-Principal-Signature"),
    ] = None,
) -> AsyncIterator[Principal]:
    """Resolve an IdP principal, or a signed principal in development only."""

    authentication_service = cast(
        AuthenticationService | None,
        getattr(request.app.state, "authentication_service", None),
    )
    settings = cast(Settings | None, getattr(request.app.state, "settings", None))
    identity = (
        authentication_service.authenticate_session(
            request.cookies.get(settings.auth_session_cookie_name)
        )
        if authentication_service is not None and settings is not None
        else None
    )
    provider = cast(PrincipalProvider | None, request.app.state.principal_provider)
    if identity is not None:
        principal = identity.principal
        request.state.auth_identity = identity
    elif provider is not None:
        principal = provider(request)
    else:
        if settings is None:
            _unauthenticated()
        if (
            settings.environment != "development"
            or not settings.development_principal_enabled
        ):
            _unauthenticated()
        secret = settings.development_principal_secret
        if not secret or development_principal is None or development_signature is None:
            _unauthenticated()
        expected = hmac.new(
            secret.encode("utf-8"),
            development_principal.encode("utf-8"),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, development_signature):
            _unauthenticated()
        try:
            payload = json.loads(development_principal)
            principal = _parse_principal_payload(payload)
        except (TypeError, ValueError, KeyError):
            _unauthenticated()
    request.state.principal = principal
    token = bind_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


def require_reader(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    try:
        DEFAULT_POLICY.require(principal, Action.READ_RISK)
    except AuthorizationDenied:
        _forbidden()
    return principal


def require_group_tax(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if principal.has_role(AUDIT_ROLE) or not principal.has_role(GROUP_TAX_ROLE):
        _forbidden()
    return principal


def require_case_writer(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    try:
        DEFAULT_POLICY.require(principal, Action.PROCESS_COMPANY_RISK)
    except AuthorizationDenied:
        _forbidden()
    return principal


def require_audit_reader(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    try:
        DEFAULT_POLICY.require(principal, Action.READ_AUDIT)
    except AuthorizationDenied:
        _forbidden()
    return principal


def require_exporter(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    try:
        DEFAULT_POLICY.require(principal, Action.EXPORT_RISK)
    except AuthorizationDenied:
        _forbidden()
    return principal


def require_monitor_runner(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    try:
        DEFAULT_POLICY.require(principal, Action.RUN_MONITOR)
    except AuthorizationDenied:
        _forbidden()
    return principal


def company_scope(
    principal: Principal,
    *,
    requested_company_id: UUID | None = None,
) -> frozenset[UUID] | None:
    """Return the SQL company filter; ``None`` means unrestricted group scope."""

    try:
        return DEFAULT_POLICY.company_scope(
            principal,
            Action.READ_RISK,
            requested_company_id=requested_company_id,
        )
    except ResourceNotFound:
        _not_found()
    except AuthorizationDenied:
        _forbidden()


def actor_role(principal: Principal) -> str:
    if principal.has_role(GROUP_TAX_ROLE):
        return GROUP_TAX_ROLE
    if principal.has_role(COMPANY_FINANCE_ROLE):
        return COMPANY_FINANCE_ROLE
    _forbidden()


def _parse_principal_payload(payload: Any) -> Principal:
    if not isinstance(payload, dict):
        raise TypeError("principal payload must be an object")
    subject = payload["subject"]
    raw_roles = payload["roles"]
    raw_company_ids = payload["allowed_company_ids"]
    organization_path = payload["organization_path"]
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or not isinstance(raw_roles, list)
        or not all(isinstance(role, str) for role in raw_roles)
        or not isinstance(raw_company_ids, list)
        or not all(isinstance(company_id, str) for company_id in raw_company_ids)
        or not isinstance(organization_path, str)
        or not organization_path.strip()
    ):
        raise TypeError("principal payload has invalid fields")
    roles = frozenset(cast(list[str], raw_roles))
    if not roles or not roles <= API_ROLES:
        raise ValueError("principal payload has unsupported roles")
    return Principal(
        subject=subject.strip(),
        roles=roles,
        allowed_company_ids=frozenset(UUID(value) for value in raw_company_ids),
        organization_path=organization_path.strip(),
    )


def _unauthenticated() -> Never:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthenticated")


def _forbidden() -> Never:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _not_found() -> Never:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


__all__ = [
    "actor_role",
    "company_scope",
    "get_principal",
    "require_audit_reader",
    "require_case_writer",
    "require_exporter",
    "require_group_tax",
    "require_monitor_runner",
    "require_reader",
]
