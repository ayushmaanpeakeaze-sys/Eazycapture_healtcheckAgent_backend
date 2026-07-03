"""Pydantic v2 schemas for the healthcheck HTTP surface."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field  # noqa: F401  (Field used by responses below)

# Status / stage / kind labels used across the audit pipeline. Kept as
# Literals so any drift between Celery task + service + frontend lights
# up in pydantic validation rather than silently miscompare-ing.
AuditStatus = Literal["in_progress", "completed", "failed"]


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class DispatchAuditResponse(_StrictBase):
    """Returned 202 from POST /sync-xero-history/{company_id}/.

    The frontend uses ``batch_id`` to subscribe to the status endpoint
    immediately. ``status`` is always ``in_progress`` on dispatch.
    """
    batch_id: UUID
    status: AuditStatus = "in_progress"


class AuditStatusResponse(_StrictBase):
    """Polled by the frontend on the status endpoint. Every field is
    sourced from the ``xero_historical_audit_batch:{batch_id}`` Redis
    hash so a single read is enough — no DB hit per poll."""
    batch_id: UUID
    status: AuditStatus
    stage: Optional[str] = None
    stage_label: Optional[str] = None
    total: int = 0
    trapped: int = 0
    new_trapped: int = 0
    started_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    # AI enrichment, populated by the existing /api/v1/enrich-audit
    # service writing back to the same Redis hash.
    audit_summary: Optional[dict[str, Any]] = None
    ai_summary_ready: bool = False
    ai_enriched_count: Optional[int] = None
    ai_enrichment_complete: bool = False


# ---------- Trapped-invoices feed ----------

class TrappedInvoiceAI(BaseModel):
    """AI enrichment spliced from Redis. Fields mirror the
    ``health_check_ai:{tx_id}`` record written by the existing
    ``/api/v1/enrich-audit`` endpoint. ``extra='allow'`` so any new
    fields added by the rules engine flow through without a schema
    bump here."""
    model_config = ConfigDict(extra="allow")

    explanation: Optional[str] = None
    severity_ai: Optional[str] = None
    confidence: Optional[float] = None
    regulatory_ref: Optional[str] = None


class TrappedInvoiceItem(BaseModel):
    """One row of the trapped-invoices feed.

    ``user_id`` and ``target_ledger`` aren't real columns on
    ``health_check_result``; they live inside the ``result`` JSONB and
    are projected up here so the frontend gets a flat shape.
    """
    model_config = ConfigDict(extra="ignore")

    id: UUID
    document_id: UUID
    document_type: str
    company_id: UUID
    user_id: Optional[UUID] = None
    kind: str
    target_ledger: str = "xero"
    status: str
    title: Optional[str] = None
    error_msgs: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    ran_at: datetime
    xero_url: Optional[str] = None
    ai: Optional[TrappedInvoiceAI] = None


class TrappedInvoicesResponse(BaseModel):
    results: list[TrappedInvoiceItem] = Field(default_factory=list)
    total: int = 0
    # "Total Potential Errors" — £ sum of outstanding (amount_due / RemainingCredit)
    # else transaction value, across ALL matching rows (not just this page).
    total_value: Decimal = Decimal("0")
    limit: int
    offset: int


# ---------- Audit log (all health-check events) ----------

class HealthCheckResultItem(BaseModel):
    """One health-check verdict row for the Audit-log feed — every event,
    any kind/status (not just blocked post-ledger like trapped-invoices)."""
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    document_id: UUID
    document_type: str
    company_id: UUID
    kind: str                  # preview | pre_ledger | post_ledger
    status: str                # passed | blocked | unavailable | skipped
    error_msgs: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    ran_at: datetime
    xero_url: Optional[str] = None


class HealthCheckStatusCounts(BaseModel):
    all: int = 0
    blocked: int = 0
    passed: int = 0
    unavailable: int = 0
    skipped: int = 0


class HealthCheckResultsResponse(BaseModel):
    results: list[HealthCheckResultItem] = Field(default_factory=list)
    counts: HealthCheckStatusCounts = Field(default_factory=HealthCheckStatusCounts)
    total: int = 0             # rows matching the current filter
    limit: int
    offset: int


# ---------- Resolution flow ----------

class ResolveRequest(BaseModel):
    field_updates: dict[str, str] = Field(default_factory=dict)
    resolution_notes: Optional[str] = None


class ResolveResponse(BaseModel):
    row_id: UUID
    document_id: UUID
    resolved: bool
    applied_updates: dict[str, str] = Field(default_factory=dict)
    skipped_fields: list[str] = Field(default_factory=list)
    xero_response: Optional[dict[str, Any]] = None
    ai_applied: bool = False
    ai_fix_strategy: Optional[str] = None
    xero_url: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


class DismissRequest(BaseModel):
    dismissal_reason: Optional[str] = None


class DismissResponse(BaseModel):
    row_id: UUID
    dismissed: bool


class CreditNoteRequest(BaseModel):
    """POST body for the 'Credit Note' button on an old unpaid invoice."""
    reason: Optional[str] = None


class ContactDefaultValues(BaseModel):
    """The four per-contact defaults (Xero Contact fields). All optional — a
    partial set only writes the fields provided."""
    sales_account: Optional[str] = None        # SalesDefaultAccountCode
    sales_tax: Optional[str] = None            # AccountsReceivableTaxType
    purchases_account: Optional[str] = None    # PurchasesDefaultAccountCode
    purchases_tax: Optional[str] = None        # AccountsPayableTaxType


class ConfirmContactDefaultsRequest(ContactDefaultValues):
    """PUT/POST body for the 'Confirm' button on one contact."""


class BulkConfirmItem(BaseModel):
    contact_id: str
    defaults: ContactDefaultValues = Field(default_factory=ContactDefaultValues)


class BulkConfirmContactDefaultsRequest(BaseModel):
    items: list[BulkConfirmItem] = Field(..., min_length=1, max_length=500)


class SnoozeRequest(BaseModel):
    """'Ignore for N days' — hide the row until it ages back in."""
    days: int = Field(default=30, ge=1, le=3650)
    reason: Optional[str] = None


class SnoozeResponse(BaseModel):
    row_id: UUID
    snoozed: bool
    snoozed_until: Optional[str] = None  # ISO-8601 UTC


class MarkOkRequest(BaseModel):
    """Accept a real flag as a legit/acceptable difference (not a false positive)."""
    reason: Optional[str] = None


class MarkOkResponse(BaseModel):
    row_id: UUID
    marked_ok: bool


class RestoreResponse(BaseModel):
    row_id: UUID
    restored: bool


class RecheckAttachmentResponse(BaseModel):
    row_id: UUID
    attached: bool          # does the doc now have an attachment in Xero?
    resolved: bool          # did we drop it from the issue list?
    stub: bool = False      # True when Xero isn't connected (no live check)


class UploadAttachmentRequest(BaseModel):
    """Upload a file to a Xero document as base64 (avoids a multipart dependency)."""
    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default="application/pdf", max_length=128)
    content_base64: str = Field(..., min_length=1)


class UploadAttachmentResponse(BaseModel):
    row_id: UUID
    uploaded: bool
    resolved: bool
    filename: str
    stub: bool = False


class BulkActionRequest(BaseModel):
    """Apply one local-state action to many rows at once."""
    row_ids: list[UUID] = Field(..., min_length=1, max_length=500)
    action: Literal["dismiss", "snooze", "mark_ok", "restore"]
    # snooze only
    days: int = Field(default=30, ge=1, le=3650)
    reason: Optional[str] = None


class BulkActionItemResult(BaseModel):
    row_id: UUID
    ok: bool
    error: Optional[str] = None


class BulkActionResponse(BaseModel):
    action: str
    requested: int
    succeeded: int
    failed: int
    results: list[BulkActionItemResult] = Field(default_factory=list)


class SuggestFixSuggestion(BaseModel):
    """Normalised AI suggestion. ``extra="allow"`` so any new field the
    rules engine starts emitting flows through to the frontend without
    a schema bump here."""
    model_config = ConfigDict(extra="allow")

    fix_strategy: str = "manual_review"
    xero_action: str = ""
    human_steps: list[str] = Field(default_factory=list)
    rationale: str = ""
    estimated_minutes: int = 0
    field_updates: Optional[dict[str, str]] = None
    line_item_updates: Optional[dict[str, str]] = None
    target_transaction_id: Optional[str] = None


class SuggestFixResponse(BaseModel):
    row_id: UUID
    document_id: UUID
    document_type: str
    xero_url: Optional[str] = None
    available: bool
    reason: Optional[str] = None
    suggestion: SuggestFixSuggestion = Field(default_factory=SuggestFixSuggestion)


class AuditConfigUpdate(BaseModel):
    """PUT body for the Audit Configuration screen."""
    disabled_rules: list[str] = Field(default_factory=list)
    # ISO date string "YYYY-MM-DD" — transactions before this are skipped.
    ignore_before: Optional[str] = None
    # Per-client tunable thresholds. Keys map to ``AuditSettings`` fields;
    # unknown keys / bad values are dropped on save, missing keys keep defaults.
    settings: Optional[dict] = None


class AuditRuleItem(BaseModel):
    key: str
    label: str
    built: bool
    enabled: bool


class AuditRuleGroup(BaseModel):
    group: str
    rules: list[AuditRuleItem]


class AuditConfigResponse(BaseModel):
    company_id: UUID
    total_checks: int
    enabled_checks: int
    disabled_rules: list[str] = Field(default_factory=list)
    ignore_before: Optional[str] = None
    # Current per-client threshold overrides, and the full default set so the
    # frontend can render each input with its value/placeholder.
    settings: dict = Field(default_factory=dict)
    settings_defaults: dict = Field(default_factory=dict)
    groups: list[AuditRuleGroup] = Field(default_factory=list)


class IssueTypeCount(BaseModel):
    issue_type: str
    count: int
    severity: str


class SeverityCount(BaseModel):
    severity: str
    count: int


class HealthStatsResponse(BaseModel):
    """Aggregated data for charts and graphs."""
    company_id: UUID
    health_score: Optional[int] = None
    total_issues: int = 0
    open_issues: int = 0
    # Split so the UI never shows "53 documents" when 29 are contacts.
    open_document_issues: int = 0   # invoices/bills trapped
    open_contact_issues: int = 0    # contact-hygiene issues
    audited_documents: int = 0      # document denominator (62) → "24 of 62"
    audited_contacts: int = 0       # contact denominator (51) → "29 of 51"
    resolved_issues: int = 0
    dismissed_issues: int = 0
    by_issue_type: list[IssueTypeCount] = Field(default_factory=list)
    by_severity: list[SeverityCount] = Field(default_factory=list)
    generated_at: datetime


class ApplyAiFixRequest(BaseModel):
    """Body for /apply-ai-fix. ``suggestion`` is optional — frontend
    can hand back the one it already showed in the modal to skip a
    second LLM call."""
    suggestion: Optional[SuggestFixSuggestion] = None


# ---------- Panorama + Summary + Re-enrich ----------

class CompanyHealthRow(BaseModel):
    """One row of the multi-tenant panorama dashboard."""
    company_id: UUID
    name: str
    is_active: bool
    needs_reconnect: bool = False
    nango_connection_id: Optional[str] = None
    xero_tenant_id: Optional[str] = None
    health_score: Optional[int] = None      # 0..100, None when never audited
    trapped_count: int = 0                   # total trapped = docs + contacts
    post_audited_total: int = 0              # document denominator (62)
    audited_contacts: int = 0                # contact denominator (51); AUDITED column = post_audited_total + this
    open_document_issues: int = 0            # trapped invoices/bills/credit notes (24)
    open_contact_issues: int = 0             # trapped contacts — hygiene (29)
    last_audit_at: Optional[datetime] = None
    top_issue: Optional[str] = None         # most common rule_id in trapped set


class CompaniesPanoramaResponse(BaseModel):
    results: list[CompanyHealthRow] = Field(default_factory=list)
    total: int = 0
    window_days: int = 30
    generated_at: datetime


class HealthSummaryIssue(BaseModel):
    issue_type: str
    count: int
    sample_msg: Optional[str] = None


class HealthSummaryResponse(BaseModel):
    company_id: UUID
    health_score: Optional[int] = None
    window_days: int = 30
    post_audited_total: int = 0
    trapped_count: int = 0
    resolved_count: int = 0
    dismissed_count: int = 0
    pre_ledger_blocked: int = 0     # reserved for v2 (outbound flow)
    pre_ledger_passed: int = 0      # reserved for v2 (outbound flow)
    last_audit_at: Optional[datetime] = None
    top_issues: list[HealthSummaryIssue] = Field(default_factory=list)


class ReenrichDispatchResponse(BaseModel):
    company_id: UUID
    task_id: str
    eligible_rows: int


# ---------- Opening Balance Differences ----------

class RegistrationNumberRequest(BaseModel):
    """Set the company's Companies House registration number (for auto-fetch)."""
    registration_number: str = Field(..., min_length=1, max_length=16)


class FiledNetAssetsRequest(BaseModel):
    """Manually enter the filed Net Assets for a period end (used when
    Companies House isn't connected)."""
    period_end: date
    net_assets: Decimal = Field(..., max_digits=14, decimal_places=2)


# ---------- Bank Balance Check ----------

class StatementBalanceRequest(BaseModel):
    """User-entered 'Per Bank Statement' balance for an account at a period end."""
    account_code: str = Field(..., min_length=1, max_length=32)
    period_end: date
    balance: Decimal = Field(..., max_digits=14, decimal_places=2)


class ExcludeAccountRequest(BaseModel):
    excluded: bool = True


class BankBalanceMarkOkRequest(BaseModel):
    account_code: str = Field(..., min_length=1, max_length=32)
    period_end: date
    ok: bool = True
