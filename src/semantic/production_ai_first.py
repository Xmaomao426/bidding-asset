"""Fail-closed AI-first production adapter over the existing semantic provider."""

from __future__ import annotations

import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping

from src.semantic.ai_provider import (
    AISemanticProvider,
    BUSINESS_PROMPT_PATH,
    BUSINESS_PROMPT_VERSION,
    DOCUMENT_BUSINESS_PROMPT_VERSION,
    DOCUMENT_BUSINESS_V4_PROMPT_VERSION,
    DOCUMENT_BUSINESS_V5_PROMPT_VERSION,
)
from src.semantic.document_evidence import (
    assess_document_field_integrity,
)
from src.semantic.models import NormalizedDocument
from src.semantic.openrouter_transport import OpenRouterTransport


MODEL_ENV = "BIDDING_ASSET_AI_SEMANTIC_MODEL"
TIMEOUT_ENV = "BIDDING_ASSET_AI_SEMANTIC_TIMEOUT_SECONDS"
DEFAULT_SEMANTIC_WALL_CLOCK_SECONDS = 180.0
_FILE_PROCESSING_DEADLINE: ContextVar[float | None] = ContextVar(
    "bidding_asset_file_processing_deadline", default=None
)
FIELD_POLICIES = {
    "project_name": "text", "project_number": "project_number", "customer": "text",
    "winner": "text", "award_amount": "amount", "budget": "amount",
    "content": "text", "bid_open_time": "text",
}
CRITICAL_DOCUMENT_FIELDS = ("project_name", "customer")
DOCUMENT_INTEGRITY_FIELDS_V5 = (
    "project_name", "customer", "bid_open_time", "content"
)
_MAX_FIELD_INTEGRITY_ISSUES = 16


class FileProcessingDeadlineExceeded(RuntimeError):
    """The enclosing file attempt exhausted its shared wall-clock budget."""

    error_code = "FILE_PROCESSING_TIMEOUT"


@contextmanager
def file_processing_deadline(deadline: float) -> Iterator[None]:
    """Expose one absolute file deadline to the semantic transport in this worker."""

    token = _FILE_PROCESSING_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _FILE_PROCESSING_DEADLINE.reset(token)


def remaining_file_processing_seconds() -> float | None:
    deadline = _FILE_PROCESSING_DEADLINE.get()
    return None if deadline is None else deadline - perf_counter()


def validate_required_startup_configuration() -> None:
    """Fail closed locally before the production UI binds its port."""
    if not os.environ.get(MODEL_ENV, "").strip():
        raise RuntimeError(
            f"Required configuration is unavailable in {MODEL_ENV}; "
            "set it before starting the Operator UI"
        )
    OpenRouterTransport().authorization_headers()


def _failure_reason(exc: Exception) -> str:
    return str(getattr(exc, "error_code", "") or type(exc).__name__)


def _critical_target_fields(
    fields: Mapping[str, Any], integrity: Mapping[str, Any],
    critical_fields: tuple[str, ...] = CRITICAL_DOCUMENT_FIELDS,
) -> list[str]:
    suspect_fields = {
        str(field) for field in integrity.get("suspect_fields") or []
    }
    return [
        field for field in critical_fields
        if not str(fields.get(field) or "").strip() or field in suspect_fields
    ]


def _integrity_with_missing_fields(
    fields: Mapping[str, Any],
    field_evidence: Mapping[str, Any] | None,
    structured_dom: Mapping[str, Any] | None,
    critical_fields: tuple[str, ...] = CRITICAL_DOCUMENT_FIELDS,
    required_fields: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    integrity = assess_document_field_integrity(
        fields,
        field_evidence,
        structured_dom,
        critical_fields=critical_fields,
    )
    missing = [
        field for field in (required_fields or critical_fields)
        if not str(fields.get(field) or "").strip()
    ]
    if not missing:
        return integrity
    issues = [dict(issue) for issue in integrity.get("quality_issues") or []]
    for field in missing:
        if not any(
            issue.get("code") == "field_missing" and issue.get("field") == field
            for issue in issues
        ):
            issues.append({"code": "field_missing", "field": field})
    integrity["quality_issues"] = issues[:_MAX_FIELD_INTEGRITY_ISSUES]
    integrity["status"] = "suspect"
    return integrity


def invoke_semantic_ai_first(
    document: NormalizedDocument,
    *,
    provider_factory: Callable[[], AISemanticProvider] | None = None,
    prompt_path: Path = BUSINESS_PROMPT_PATH,
    prompt_version: str = BUSINESS_PROMPT_VERSION,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invoke the active business contract once and preserve field-level audit."""
    started = perf_counter()
    ai_fields: dict[str, Any] = {}
    model = os.environ.get(MODEL_ENV, "").strip()
    audit: dict[str, Any] = {
        "mode": "notice_content_dom_ai",
        "invoked": False,
        "status": "pending",
        "provider": "openrouter",
        "model": model,
        "prompt_version": prompt_version,
        "failure_reason": "",
        "applied_fields": [],
        "field_sources": {},
        "call_count": 0,
        "repair": {
            "invoked": False,
            "status": "not_applicable",
            "target_fields": [],
            "applied_fields": [],
            "failure_reason": "",
            "repair_evidence_audit": {},
            "provider_diagnostics": {},
            "stage_timings_ms": {},
        },
    }
    if provider_factory is None and not model:
        audit.update(status="unavailable", failure_reason="model_not_configured")
        audit["stage_timings_ms"] = {"ai": round((perf_counter() - started) * 1000, 3)}
        return ai_fields, audit

    try:
        document_metadata = getattr(document, "metadata", {})
        structured_dom = (
            document_metadata.get("notice_content_dom")
            if isinstance(document_metadata, Mapping)
            else None
        )
        if not isinstance(structured_dom, Mapping) or not structured_dom:
            raise ValueError("notice_content_dom is required for business semantic extraction")
        provider = (
            provider_factory()
            if provider_factory is not None
            else configured_provider(
                model,
                prompt_path=prompt_path,
                prompt_version=prompt_version,
            )
        )
        audit["model"] = provider.model
        audit["prompt_version"] = provider.prompt_version
        audit["invoked"] = True
        audit["call_count"] = 1
        result, provider_diagnostics = provider.extract_business(document)
        page_fields = dict(result.get("fields") or {})
        correction_fields = dict(result.get("corrections") or {})
        for field_name in (*FIELD_POLICIES.keys(),):
            page_value = str(page_fields.get(field_name) or "").strip()
            correction_value = str(correction_fields.get(field_name) or "").strip()
            value = correction_value or page_value
            if value:
                ai_fields[field_name] = value
                audit["field_sources"][field_name] = "correction" if correction_value else "page"
        ai_fields["award_details"] = list(result.get("packages") or [])
        field_evidence = result.get("field_evidence")
        if str(audit.get("prompt_version") or "") in {
            DOCUMENT_BUSINESS_PROMPT_VERSION,
            DOCUMENT_BUSINESS_V4_PROMPT_VERSION,
            DOCUMENT_BUSINESS_V5_PROMPT_VERSION,
        }:
            critical_fields = (
                DOCUMENT_INTEGRITY_FIELDS_V5
                if str(audit.get("prompt_version") or "")
                == DOCUMENT_BUSINESS_V5_PROMPT_VERSION
                else CRITICAL_DOCUMENT_FIELDS
            )
            audit["field_integrity"] = _integrity_with_missing_fields(
                ai_fields,
                field_evidence if isinstance(field_evidence, Mapping) else {},
                structured_dom if isinstance(structured_dom, Mapping) else None,
                critical_fields,
            )
            target_fields = _critical_target_fields(
                ai_fields, audit["field_integrity"], CRITICAL_DOCUMENT_FIELDS
            )
            repair = audit["repair"]
            repair["target_fields"] = target_fields
            if target_fields:
                repair["status"] = "not_used"
                repair["failure_reason"] = "normal_repair_removed"
            else:
                repair["status"] = "not_needed"
        audit["status"] = "available" if ai_fields else "abstained"
        audit["applied_fields"] = sorted(
            field_name for field_name in ai_fields if field_name != "award_details"
        )
        audit["provider_diagnostics"] = provider_diagnostics
    except Exception as exc:  # No provider/transport failure may interrupt URL acquisition.
        audit.update(
            status="failed",
            failure_reason=_failure_reason(exc),
        )
        audit["stage_timings_ms"] = {"ai": round((perf_counter() - started) * 1000, 3)}
        return {}, audit
    stage_timings = {"ai": round((perf_counter() - started) * 1000, 3)}
    repair_timing = audit["repair"].get("stage_timings_ms", {}).get("repair")
    if repair_timing is not None:
        stage_timings["repair"] = repair_timing
    audit["stage_timings_ms"] = stage_timings
    return ai_fields, audit


def configured_provider(
    model: str,
    *,
    prompt_path: Path = BUSINESS_PROMPT_PATH,
    prompt_version: str = BUSINESS_PROMPT_VERSION,
) -> AISemanticProvider:
    timeout_text = os.environ.get(TIMEOUT_ENV, "").strip()
    timeout = float(timeout_text) if timeout_text else DEFAULT_SEMANTIC_WALL_CLOCK_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_semantic_timeout")
    remaining = remaining_file_processing_seconds()
    if remaining is not None:
        if remaining <= 0:
            raise FileProcessingDeadlineExceeded("file_processing_wall_clock_timeout")
        timeout = min(timeout, remaining)
    return AISemanticProvider(
        transport=OpenRouterTransport(timeout_seconds=timeout),
        model=model,
        prompt_path=prompt_path,
        prompt_version=prompt_version,
        parameters={"temperature": 0},
    )
