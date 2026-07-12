"""Smoke tests: every route mounts, health probe answers, gate behaves."""
from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_openapi_lists_all_v1_routes(client):
    """If the router split broke an import, OpenAPI won't include the path."""
    paths = client.get("/openapi.json").json()["paths"].keys()
    expected = {
        "/api/v1/validate-invoice",
        "/api/v1/health-check/batch",
        "/api/v1/health-check/batch/async",
        "/api/v1/health-check/batch/template.csv",
        "/api/v1/audit/progress/{batch_id}",
        "/api/v1/enrich-audit",
        "/api/v1/suggest-fix",
    }
    missing = expected - set(paths)
    assert not missing, f"missing routes: {missing}"


def test_batch_template_csv_downloads(client):
    """The Batch Inspector 'Download template' endpoint returns a CSV attachment
    whose header carries the required columns."""
    r = client.get("/api/v1/health-check/batch/template.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    header = r.text.splitlines()[0]
    for col in ("transaction_id", "date", "description", "vendor_name", "amount"):
        assert col in header


def test_enrich_audit_disabled_returns_disabled(client):
    """Fail-open contract: gate OFF → status='disabled', queued_rows=0."""
    r = client.post(
        "/api/v1/enrich-audit",
        json={
            "batch_id": "t1", "company_id": "c1", "total_documents": 0,
            "trapped_rows": [],
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "disabled"
    assert body["queued_rows"] == 0


def test_suggest_fix_disabled_returns_503(client):
    """Fail-open contract: gate OFF → 503, never calls Groq."""
    r = client.post(
        "/api/v1/suggest-fix",
        json={
            "rule_id": "future_dated",
            "transaction": {"transaction_id": "t1"},
        },
    )
    assert r.status_code == 503


def test_validate_invoice_rejects_bad_body(client):
    """Pydantic validation: missing required fields → 422, not 500."""
    r = client.post("/api/v1/validate-invoice", json={})
    assert r.status_code == 422


def test_health_check_batch_runs_deterministic_rules(client):
    """End-to-end: deterministic rules fire without any LLM call."""
    r = client.post(
        "/api/v1/health-check/batch",
        json={
            "transactions": [{
                "transaction_id": "tx1",
                "date": "2026-01-01",
                "description": "Test bill",
                "amount": "100.00",
                "vendor_name": "Acme",
                # tax_code intentionally missing → expect missing_tax flag
                "type": "ACCPAY",
            }],
        },
    )
    assert r.status_code == 200
    issue_types = {f["issue_type"] for f in r.json()["flagged"]}
    assert "missing_tax" in issue_types
