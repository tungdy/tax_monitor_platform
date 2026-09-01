"""Static contract tests for the production-shaped Compose topology."""

from __future__ import annotations

import json
from hashlib import sha256
import hmac
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit

import pytest


INFRA_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = INFRA_DIR / "docker-compose.yml"
ENV_EXAMPLE = INFRA_DIR / "env.example"
OPERATIONS_GUIDE = INFRA_DIR / "README.md"
ROLE_BOOTSTRAP = INFRA_DIR / "postgres" / "configure_database_roles.sh"
EXPECTED_SERVICES = {
    "postgres",
    "database-roles",
    "redis",
    "migrate",
    "api",
    "worker-quarterly",
    "worker-business-entertainment",
    "worker-monthly-semantic",
    "worker-exports",
    "worker-income-tax-refund-writeback",
    "beat-income-tax-refund-writeback",
    "web",
}
WORKER_SERVICES = {
    "worker-quarterly",
    "worker-business-entertainment",
    "worker-monthly-semantic",
    "worker-exports",
    "worker-income-tax-refund-writeback",
}
BEAT_SERVICE = "beat-income-tax-refund-writeback"
BACKEND_RUNTIME_SERVICES = {"api", *WORKER_SERVICES, BEAT_SERVICE}
LONG_LIVED_SERVICES = {
    "postgres",
    "redis",
    *BACKEND_RUNTIME_SERVICES,
    "web",
}
DGC_ENVIRONMENT = {
    "DGC_SAP_PROFIT_ENABLED": "true",
    "DGC_IAM_URL": "https://iam.example.test/v3/auth/tokens",
    "DGC_SAP_PROFIT_API_URL": "https://dgc.example.test/sap-profit",
    "DGC_APP_KEY": "static-test-app-key",
    "DGC_APP_SECRET": "static-test-app-secret",
    "DGC_IAM_USERNAME": "",
    "DGC_IAM_PASSWORD": "",
    "DGC_IAM_DOMAIN": "static-domain",
    "DGC_IAM_PROJECT": "static-project",
    "DGC_TIMEOUT_SECONDS": "45",
    "DGC_PAGE_SIZE": "15000",
    "DGC_MAX_PAGES": "250",
    "DGC_MAX_RECORDS": "50000",
    "DGC_MAX_PAGE_BYTES": "5242880",
    "DGC_MAX_TOTAL_BYTES": "33554432",
    "DGC_TOKEN_TTL_SECONDS": "7200",
    "DGC_TLS_SERVER_NAME": "dgc.example.test",
    "DGC_TLS_PINNED_CERTIFICATE_SHA256": "AB" * 32,
    "DGC_SAP_PROFIT_FIELD_MAP": json.dumps(
        {
            "client": "client",
            "company_code": "company",
            "company_name": "company_name",
            "fiscal_year": "year",
            "fiscal_period": "period",
            "ledger": "ledger",
            "line_number": "line_number",
            "line_item": "line_item",
            "current_month_amount": "month_amount",
            "year_to_date_amount": "ytd_amount",
        },
        separators=(",", ":"),
    ),
    "DGC_SAP_PROFIT_METRIC_MAP": json.dumps(
        {
            "cumulative_profit": ["profit"],
            "fair_value_change": ["fair_value"],
            "cumulative_revenue": ["revenue"],
        },
        separators=(",", ":"),
    ),
    "DGC_SAP_PROFIT_LEDGER": "0L",
    "DGC_SAP_TRIAL_BALANCE_ENABLED": "true",
    "DGC_SAP_TRIAL_BALANCE_API_URL": "https://dgc.example.test/trial-balance",
    "DGC_SAP_TRIAL_BALANCE_APP_KEY": "static-trial-balance-key",
    "DGC_SAP_TRIAL_BALANCE_APP_SECRET": "static-trial-balance-secret",
    "DGC_SAP_TRIAL_BALANCE_PAGE_SIZE": "1000",
    "DGC_SAP_ACCOUNT_BALANCE_ENABLED": "false",
    "DGC_SAP_ACCOUNT_BALANCE_API_URL": "https://dgc.example.test/account-balance",
    "DGC_SAP_ACCOUNT_BALANCE_APP_KEY": "",
    "DGC_SAP_ACCOUNT_BALANCE_APP_SECRET": "",
    "DGC_SAP_ACCOUNT_BALANCE_PAGE_SIZE": "15000",
    "DGC_HESI_REIMBURSEMENT_ENABLED": "false",
    "DGC_HESI_REIMBURSEMENT_API_URL": "https://dgc.example.test/hesi-reimbursement",
    "DGC_HESI_REIMBURSEMENT_APP_KEY": "",
    "DGC_HESI_REIMBURSEMENT_APP_SECRET": "",
    "DGC_HESI_REIMBURSEMENT_PAGE_SIZE": "25",
    "DGC_HESI_REIMBURSEMENT_FIELD_MAP": (
        '{"company_code":"company_code","approval_completed_at":"flow_end_date",'
        '"expense_claim_code":"expense_code","expense_type_code":"fee_type_code",'
        '"expense_type_amount":"fee_type_amount"}'
    ),
    "DGC_HESI_INVOICE_ENABLED": "false",
    "DGC_HESI_INVOICE_API_URL": "https://dgc.example.test/hesi-invoice",
    "DGC_HESI_INVOICE_APP_KEY": "",
    "DGC_HESI_INVOICE_APP_SECRET": "",
    "DGC_HESI_INVOICE_PAGE_SIZE": "25",
    "DGC_HESI_INVOICE_FIELD_MAP": (
        '{"company_code":"company_code","expense_claim_code":"code",'
        '"invoice_id":"invoice_id","expense_type_id":"feetypeid",'
        '"expense_line_amount":"amount_standard_dec",'
        '"invoice_approved_amount":"approve_amount_dec"}'
    ),
    "DGC_SAP_DIVIDEND_DETAIL_ENABLED": "true",
    "DGC_SAP_DIVIDEND_DETAIL_API_URL": "https://dgc.example.test/dividend-detail",
    "DGC_SAP_DIVIDEND_DETAIL_APP_KEY": "static-dividend-detail-key",
    "DGC_SAP_DIVIDEND_DETAIL_APP_SECRET": "static-dividend-detail-secret",
    "DGC_SAP_DIVIDEND_DETAIL_PAGE_SIZE": "15000",
    "DGC_INVOICE_DETAIL_ENABLED": "true",
    "DGC_INVOICE_DETAIL_API_URL": "https://dgc.example.test/invoice-detail",
    "DGC_INVOICE_DETAIL_APP_KEY": "static-invoice-detail-key",
    "DGC_INVOICE_DETAIL_APP_SECRET": "static-invoice-detail-secret",
    "DGC_INVOICE_DETAIL_PAGE_SIZE": "15000",
}
LARK_REFUND_RESOURCE_ENVIRONMENT = {
    "LARK_REFUND_WRITEBACK_ENABLED": "true",
    "LARK_REFUND_BASE_URL": (
        "https://hailiang.feishu.cn/base/A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
    ),
    "LARK_REFUND_API_BASE_URL": "https://open.feishu.cn",
    "LARK_REFUND_BASE_TOKEN": "A1Kwb4tkZaZdE2s3C2dcG49Fn2d",
    "LARK_REFUND_TABLE_ID": "tbl4PCNdcl4BYzgZ",
    "LARK_REFUND_COMPANY_CODE_FIELD_ID": "fld5uBjB9R",
    "LARK_REFUND_STATUS_FIELD_ID": "fld4HLnqDk",
    "LARK_REFUND_TIMEOUT_SECONDS": "30",
    "LARK_REFUND_PAGE_SIZE": "100",
    "LARK_REFUND_MAX_RETRIES": "3",
}
LARK_REFUND_CREDENTIAL_ENVIRONMENT = {
    "LARK_REFUND_APP_ID": "static-lark-app-id",
    "LARK_REFUND_APP_SECRET": "static-lark-app-secret",
}


def _require_docker_compose() -> None:
    if shutil.which("docker") is not None:
        return
    if os.getenv("CI"):
        pytest.fail("CI Compose topology gate requires the docker CLI")
    pytest.skip("Compose topology validation requires the docker CLI")


def _compose_config() -> dict[str, Any]:
    _require_docker_compose()
    environment = os.environ | {
        "POSTGRES_DB": "tax_risk",
        "POSTGRES_USER": "tax_risk",
        "POSTGRES_PASSWORD": "static-test-only",
        "POSTGRES_APP_USER": "tax_risk_app",
        "POSTGRES_APP_PASSWORD": "static-app-test-only",
        "MIGRATION_DATABASE_URL": (
            "postgresql+psycopg://tax_risk:static-test-only@postgres:5432/tax_risk"
        ),
        "DATABASE_URL": (
            "postgresql+psycopg://tax_risk_app:static-app-test-only@postgres:5432/tax_risk"
        ),
        "REDIS_URL": "redis://redis:6379/0",
        "EXPORT_DOWNLOAD_SECRET": "static-export-signing-secret-at-least-32-chars",
        "WORKER_SCOPE_SECRET": "static-worker-signing-secret-at-least-32-chars",
        **DGC_ENVIRONMENT,
        **LARK_REFUND_RESOURCE_ENVIRONMENT,
        **LARK_REFUND_CREDENTIAL_ENVIRONMENT,
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=INFRA_DIR.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _local_acceptance_config() -> dict[str, Any]:
    _require_docker_compose()
    environment = os.environ.copy()
    environment.pop("ENVIRONMENT", None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_EXAMPLE),
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=INFRA_DIR.parent,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _render_services_for_forbidden_content_scan(services: dict[str, Any]) -> str:
    portable_services = json.loads(json.dumps(services))
    for service in portable_services.values():
        build = service.get("build")
        if isinstance(build, dict) and "context" in build:
            build["context"] = "<build-context>"
    return json.dumps(portable_services).lower()


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid dotenv line for {key!r}"
        values[key] = value
    return values


def test_forbidden_content_scan_normalizes_only_the_resolved_build_context() -> None:
    services = {
        "api": {
            "build": {
                "context": "/Users/example/Agents-Repo/tax-monitor/backend",
                "dockerfile": "Dockerfile",
            },
            "command": ["agent-runtime"],
        }
    }

    rendered = _render_services_for_forbidden_content_scan(services)

    assert "agents-repo" not in rendered
    assert "agent-runtime" in rendered
    assert "dockerfile" in rendered


def test_compose_has_only_the_required_service_topology() -> None:
    services = _compose_config()["services"]

    assert set(services) == EXPECTED_SERVICES
    rendered = _render_services_for_forbidden_content_scan(services)
    for forbidden in ("agent", "llm", "model-server", "ollama", "openai"):
        assert forbidden not in rendered


def test_application_services_use_immutable_images_without_source_mounts() -> None:
    services = _compose_config()["services"]

    backend_images = {
        services[name]["image"] for name in ("migrate", *BACKEND_RUNTIME_SERVICES)
    }
    assert len(backend_images) == 1
    for name in ("migrate", *BACKEND_RUNTIME_SERVICES, "web"):
        assert not services[name].get("volumes")
    for service in services.values():
        assert all(mount["type"] != "bind" for mount in service.get("volumes", []))

    assert services["postgres"]["volumes"][0]["target"] == "/var/lib/postgresql/data"
    assert services["redis"]["volumes"][0]["target"] == "/data"


def test_long_lived_services_are_healthy_and_start_in_dependency_order() -> None:
    services = _compose_config()["services"]

    for name in LONG_LIVED_SERVICES:
        assert services[name].get("healthcheck"), name

    assert (
        services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    )
    assert services["migrate"]["depends_on"]["database-roles"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["database-roles"]["depends_on"]["postgres"]["condition"] == (
        "service_healthy"
    )
    assert services["api"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["depends_on"]["redis"]["condition"] == "service_healthy"
    for worker in WORKER_SERVICES:
        assert services[worker]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )
        assert services[worker]["depends_on"]["redis"]["condition"] == (
            "service_healthy"
        )
    assert services[BEAT_SERVICE]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services[BEAT_SERVICE]["depends_on"]["redis"]["condition"] == (
        "service_healthy"
    )
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"

    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert "--queues=quarterly" in services["worker-quarterly"]["command"]
    assert (
        "--queues=business-entertainment"
        in services["worker-business-entertainment"]["command"]
    )
    assert "--queues=monthly-semantic" in services["worker-monthly-semantic"]["command"]
    assert "--queues=exports" in services["worker-exports"]["command"]
    assert (
        "--queues=income-tax-refund-writeback"
        in services["worker-income-tax-refund-writeback"]["command"]
    )
    assert "beat" in services[BEAT_SERVICE]["command"]
    assert "--pidfile=/tmp/celerybeat.pid" in services[BEAT_SERVICE]["command"]
    assert "--schedule=/tmp/celerybeat-schedule" in services[BEAT_SERVICE]["command"]
    assert all(
        "sleep" not in json.dumps(service.get("command", ""))
        for service in services.values()
    )


def test_migrations_and_runtime_use_separate_database_roles() -> None:
    services = _compose_config()["services"]

    migration_url = services["migrate"]["environment"]["DATABASE_URL"]
    api_url = services["api"]["environment"]["DATABASE_URL"]
    runtime_urls = {
        services[name]["environment"]["DATABASE_URL"]
        for name in {*WORKER_SERVICES, BEAT_SERVICE}
    }
    role_environment = services["database-roles"]["environment"]

    assert urlsplit(migration_url).username == "tax_risk"
    assert urlsplit(api_url).username == "tax_risk_app"
    assert runtime_urls == {api_url}
    assert role_environment["POSTGRES_APP_USER"] == "tax_risk_app"
    assert role_environment["PGUSER"] == "tax_risk"


def test_runtime_signing_secrets_are_injected_only_into_backend_services() -> None:
    services = _compose_config()["services"]
    backend_names = BACKEND_RUNTIME_SERVICES

    for name in backend_names:
        environment = services[name]["environment"]
        assert environment["EXPORT_DOWNLOAD_SECRET"].startswith("static-export")
        assert environment["WORKER_SCOPE_SECRET"].startswith("static-worker")
    assert "EXPORT_DOWNLOAD_SECRET" not in services["web"]["environment"]
    assert "WORKER_SCOPE_SECRET" not in services["web"]["environment"]


def test_dgc_settings_are_forwarded_only_to_api() -> None:
    services = _compose_config()["services"]

    api_environment = services["api"]["environment"]
    assert {key: api_environment[key] for key in DGC_ENVIRONMENT} == DGC_ENVIRONMENT
    for name in set(services) - {"api"}:
        assert not set(DGC_ENVIRONMENT) & set(services[name].get("environment", {}))


def test_lark_credentials_are_forwarded_only_to_refund_writeback_worker() -> None:
    services = _compose_config()["services"]
    worker_name = "worker-income-tax-refund-writeback"
    worker_environment = services[worker_name]["environment"]
    beat_environment = services[BEAT_SERVICE]["environment"]
    api_environment = services["api"]["environment"]

    assert {
        key: worker_environment[key] for key in LARK_REFUND_RESOURCE_ENVIRONMENT
    } == LARK_REFUND_RESOURCE_ENVIRONMENT
    assert {
        key: api_environment[key] for key in LARK_REFUND_RESOURCE_ENVIRONMENT
    } == LARK_REFUND_RESOURCE_ENVIRONMENT
    assert {
        key: beat_environment[key] for key in LARK_REFUND_RESOURCE_ENVIRONMENT
    } == LARK_REFUND_RESOURCE_ENVIRONMENT
    assert {
        key: worker_environment[key] for key in LARK_REFUND_CREDENTIAL_ENVIRONMENT
    } == LARK_REFUND_CREDENTIAL_ENVIRONMENT
    for name in set(services) - {worker_name, BEAT_SERVICE, "api"}:
        assert not set(LARK_REFUND_RESOURCE_ENVIRONMENT) & set(
            services[name].get("environment", {})
        )
    for name in set(services) - {worker_name}:
        assert not set(LARK_REFUND_CREDENTIAL_ENVIRONMENT) & set(
            services[name].get("environment", {})
        )
    assert not set(LARK_REFUND_CREDENTIAL_ENVIRONMENT) & set(beat_environment)


def test_lark_refund_env_example_uses_base_contract_and_blank_credentials() -> None:
    values = _dotenv_values(ENV_EXAMPLE)

    assert {
        key: values[key] for key in LARK_REFUND_RESOURCE_ENVIRONMENT
    } == LARK_REFUND_RESOURCE_ENVIRONMENT | {"LARK_REFUND_WRITEBACK_ENABLED": "false"}
    assert values["LARK_REFUND_APP_ID"] == ""
    assert values["LARK_REFUND_APP_SECRET"] == ""
    assert values["LARK_REFUND_WORKER_CONCURRENCY"] == "2"
    assert not {
        "LARK_REFUND_LEDGER_URL",
        "LARK_REFUND_LEDGER_EXPECTED_TYPE",
        "LARK_REFUND_LEDGER_SHEET_ID",
    } & set(values)


def test_invoice_detail_env_example_uses_safe_defaults() -> None:
    values = _dotenv_values(ENV_EXAMPLE)

    assert values["DGC_INVOICE_DETAIL_ENABLED"] == "false"
    assert values["DGC_INVOICE_DETAIL_API_URL"] == (
        "https://116.63.221.181/post/writeoff"
    )
    assert values["DGC_INVOICE_DETAIL_APP_KEY"] == ""
    assert values["DGC_INVOICE_DETAIL_APP_SECRET"] == ""
    assert values["DGC_INVOICE_DETAIL_PAGE_SIZE"] == "15000"


def test_hesi_invoice_env_example_uses_safe_defaults() -> None:
    values = _dotenv_values(ENV_EXAMPLE)

    assert values["DGC_HESI_INVOICE_ENABLED"] == "false"
    assert values["DGC_HESI_INVOICE_API_URL"] == (
        "https://116.63.221.181/post/hesiinvoice"
    )
    assert values["DGC_HESI_INVOICE_APP_KEY"] == ""
    assert values["DGC_HESI_INVOICE_APP_SECRET"] == ""
    assert values["DGC_HESI_INVOICE_PAGE_SIZE"] == "25"
    assert values["DGC_HESI_INVOICE_FIELD_MAP"] == (
        '{"company_code":"company_code","expense_claim_code":"code",'
        '"invoice_id":"invoice_id","expense_type_id":"feetypeid",'
        '"expense_line_amount":"amount_standard_dec",'
        '"invoice_approved_amount":"approve_amount_dec"}'
    )


def test_hesi_detail_env_example_uses_new_contract() -> None:
    values = _dotenv_values(ENV_EXAMPLE)

    assert values["DGC_HESI_REIMBURSEMENT_ENABLED"] == "false"
    assert values["DGC_HESI_REIMBURSEMENT_API_URL"] == (
        "https://116.63.221.181/post/hesimingxi"
    )
    assert values["DGC_HESI_REIMBURSEMENT_APP_KEY"] == ""
    assert values["DGC_HESI_REIMBURSEMENT_APP_SECRET"] == ""
    assert values["DGC_HESI_REIMBURSEMENT_PAGE_SIZE"] == "25"
    assert values["DGC_HESI_REIMBURSEMENT_FIELD_MAP"] == (
        '{"company_code":"company_code","approval_completed_at":"flow_end_date",'
        '"expense_claim_code":"expense_code","expense_type_code":"fee_type_code",'
        '"expense_type_amount":"fee_type_amount"}'
    )


def test_database_role_bootstrap_cannot_create_a_bypass_runtime_identity() -> None:
    script = ROLE_BOOTSTRAP.read_text(encoding="utf-8")

    assert '"${PGUSER}" == "${POSTGRES_APP_USER}"' in script
    assert "NOSUPERUSER" in script
    assert "NOBYPASSRLS" in script
    assert "NOINHERIT" in script
    assert "ALTER DEFAULT PRIVILEGES" in script


def test_host_ports_are_loopback_only() -> None:
    services = _compose_config()["services"]

    for name in ("postgres", "redis", "api", "web"):
        assert services[name]["ports"]
        assert all(port["host_ip"] == "127.0.0.1" for port in services[name]["ports"])

    assert services["web"]["ports"][0]["target"] == 8080


def test_base_datastores_can_be_configured_without_application_environment() -> None:
    _require_docker_compose()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
            "REDIS_URL",
            "DEVELOPMENT_PRINCIPAL_SECRET",
        }
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--quiet",
            "postgres",
            "redis",
        ],
        cwd=INFRA_DIR.parent,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_local_acceptance_proxy_injects_a_valid_signed_group_tax_principal() -> None:
    production_web_environment = _compose_config()["services"]["web"]["environment"]
    services = _local_acceptance_config()["services"]
    backend_environment = services["api"]["environment"]
    web_environment = services["web"]["environment"]
    escaped_principal = web_environment["DEVELOPMENT_PRINCIPAL"]
    raw_principal = escaped_principal.replace(r"\"", '"')
    signature = web_environment["DEVELOPMENT_PRINCIPAL_SIGNATURE"]

    assert backend_environment["ENVIRONMENT"] == "development"
    assert backend_environment["DEVELOPMENT_PRINCIPAL_ENABLED"] == "true"
    assert json.loads(raw_principal)["roles"] == ["group-tax"]
    expected = hmac.new(
        backend_environment["DEVELOPMENT_PRINCIPAL_SECRET"].encode(),
        raw_principal.encode(),
        sha256,
    ).hexdigest()
    assert signature == expected
    assert production_web_environment["DEVELOPMENT_PRINCIPAL"] == ""
    assert production_web_environment["DEVELOPMENT_PRINCIPAL_SIGNATURE"] == ""
    assert "DEVELOPMENT_PRINCIPAL_SECRET" not in web_environment
    assert web_environment["NGINX_ENVSUBST_FILTER"].startswith(
        "^DEVELOPMENT_PRINCIPAL(_SIGNATURE)?$"
    )
    assert set(services["web"]["build"]["args"]) == {"VITE_API_BASE_URL"}
    assert services["web"]["build"]["args"]["VITE_API_BASE_URL"] == ""
    assert "http://127.0.0.1:8080/healthz" in services["web"]["healthcheck"]["test"]


def test_container_files_enforce_locked_dependencies_and_server_side_auth_headers() -> (
    None
):
    backend_dockerfile = (INFRA_DIR.parent / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    backend_dockerignore = (INFRA_DIR.parent / "backend" / ".dockerignore").read_text(
        encoding="utf-8"
    )
    web_dockerfile = (INFRA_DIR.parent / "web" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    web_dockerignore = (INFRA_DIR.parent / "web" / ".dockerignore").read_text(
        encoding="utf-8"
    )
    nginx_template = (INFRA_DIR.parent / "web" / "nginx.conf").read_text(
        encoding="utf-8"
    )

    assert "COPY requirements.lock" in backend_dockerfile
    assert "--constraint requirements.lock ." in backend_dockerfile
    assert ".env\n" in backend_dockerignore
    assert ".env.*" in backend_dockerignore
    assert ".env\n" in web_dockerignore
    assert ".env.*" in web_dockerignore
    assert "nginxinc/nginx-unprivileged:1.27-alpine" in web_dockerfile
    assert "USER 101" in web_dockerfile
    assert "listen 8080" in nginx_template
    assert "client_max_body_size 50m" in nginx_template
    assert "X-Development-Principal" in nginx_template
    assert "X-Development-Principal-Signature" in nginx_template


def test_operations_guide_has_an_explicit_production_go_no_go_gate() -> None:
    guide = OPERATIONS_GUIDE.read_text(encoding="utf-8")

    assert "## 生产上线准入" in guide
    for required_evidence in (
        "字段映射签字确认",
        "金额精度签字确认",
        "E2E_SEED_TOKEN",
        "E2E_STANDARD_COMPANY_CODE",
        "105 requested / 103 succeeded / 2 blocked / 0 failed",
        "Playwright 结果",
        "业务批准人",
        "严禁部署至生产环境",
    ):
        assert required_evidence in guide
