"""Small, serializable stage diagnostics for the current acquisition mainline.

The module intentionally contains no I/O, model, browser, or persistence code.
It only normalizes bounded values so callers can attach a trace to an existing
processing result without changing the business payload contract.
"""

from __future__ import annotations

import math
import re
import uuid
from pathlib import PurePath
from typing import Any, Mapping


STAGE_TRACE_SCHEMA_VERSION = "stage-trace/v1"
STAGE_ORDER = (
    "acquisition",
    "parse",
    "ocr",
    "evidence",
    "semantic",
    "integrity",
    "readiness",
    "candidate",
    "persistence",
    "ui",
)
STAGE_STATUSES = frozenset({"success", "failed", "blocked", "skipped"})
MAX_COUNT_VALUE = 1_000_000_000
MAX_SOURCE_SIZE_BYTES = 10_000_000_000_000
MAX_ERROR_SUMMARY_CHARS = 240
MAX_ROUTE_CHARS = 120
MAX_SOURCE_NAME_CHARS = 255
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s,;]+")
_POSIX_PATH_PATTERN = re.compile(r"(?<!\w)/[^\s,;]+")
_URL_PATTERN = re.compile(r"(?i)https?://[^\s,;]+")
_AUTH_HEADER_PATTERN = re.compile(r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+)[^\s,;]+")
_KEY_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
)
_OPENROUTER_KEY_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")


def _bounded_text(value: Any, limit: int) -> str:
    text = _CONTROL_PATTERN.sub(" ", str(value or "")).strip()
    return text[:limit]


def _basename(value: Any) -> str:
    text = str(value or "").replace("\\", "/")
    if not text:
        return ""
    return _bounded_text(PurePath(text).name, MAX_SOURCE_NAME_CHARS)


def _bounded_number(value: Any, *, field: str, maximum: int | float = MAX_COUNT_VALUE) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ValueError(f"{field} must be a bounded non-negative number")
    if number.is_integer():
        return int(number)
    return number


def _safe_counts(counts: Mapping[str, Any] | None) -> dict[str, int | float]:
    if counts is None:
        return {}
    if not isinstance(counts, Mapping):
        raise ValueError("counts must be an object")
    result: dict[str, int | float] = {}
    for key, value in counts.items():
        name = _bounded_text(key, 80)
        if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("count names must be short identifiers")
        result[name] = _bounded_number(value, field=f"counts.{name}")
    return result


def safe_counts(counts: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Keep only legal finite counts when an existing audit is best-effort."""
    if not isinstance(counts, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for key, value in counts.items():
        try:
            result.update(_safe_counts({key: value}))
        except ValueError:
            continue
    return result


def safe_error_summary(error: Any) -> str:
    """Return a bounded diagnostic summary without paths, URLs, or credentials."""
    if isinstance(error, BaseException):
        raw = str(error) or type(error).__name__
    else:
        raw = str(error or "")
    summary = _CONTROL_PATTERN.sub(" ", raw).strip()
    summary = _URL_PATTERN.sub("<url>", summary)
    summary = _AUTH_HEADER_PATTERN.sub("<credential>", summary)
    def redact_assignment(match: re.Match[str]) -> str:
        token = match.group(0)
        separator = "=" if "=" in token else ":"
        return token.split(separator, 1)[0] + "=<redacted>"

    summary = _KEY_ASSIGNMENT_PATTERN.sub(redact_assignment, summary)
    summary = _OPENROUTER_KEY_PATTERN.sub("<credential>", summary)
    summary = _WINDOWS_PATH_PATTERN.sub("<path>", summary)
    summary = _POSIX_PATH_PATTERN.sub("<path>", summary)
    return _bounded_text(summary, MAX_ERROR_SUMMARY_CHARS)


def map_error_code(stage: str, error: Any = "") -> str:
    """Map current exception/audit wording to a stable stage-prefixed code."""
    normalized_stage = _bounded_text(stage, 40).lower()
    text = safe_error_summary(error).lower()
    if normalized_stage == "parse":
        if any(token in text for token in ("unsupported_or_corrupt", "corrupt_zip", "unsupported upload", "不支持")):
            return "parse.unsupported_or_corrupt_document"
        if "text_pdf_has_no_extractable_text" in text or "text layer" in text:
            return "parse.text_layer_empty"
        if "document_has_no_usable_text" in text:
            return "parse.document_empty"
        if "password" in text or "encrypted" in text:
            return "parse.password_protected"
    elif normalized_stage == "evidence":
        if "budget" in text:
            return "evidence.budget_exhausted"
        if "empty" in text:
            return "evidence.empty"
        if "structure" in text or "schema" in text:
            return "evidence.invalid_structure"
    elif normalized_stage == "semantic":
        if "model_not_configured" in text or "model not configured" in text:
            return "semantic.model_not_configured"
        if "schema" in text or "json" in text:
            return "semantic.schema_invalid"
        if "no_fields" in text or "no fields" in text or "abstained" in text:
            return "semantic.no_fields"
        if any(token in text for token in ("transport", "timeout", "openrouter", "connection")):
            return "semantic.transport_failed"
    elif normalized_stage == "integrity":
        if "missing" in text:
            return "integrity.field_missing"
        if any(token in text for token in ("evidence", "quote", "truncated", "role")):
            return "integrity.field_evidence_invalid"
    elif normalized_stage == "readiness":
        if "integrity" in text or "evidence" in text:
            return "readiness.confirmation_blocked_integrity"
        if "missing" in text:
            return "readiness.confirmation_blocked_missing"
    elif normalized_stage == "candidate":
        for token, code in (
            ("dedup", "candidate.dedup_failed"),
            ("lifecycle", "candidate.lifecycle_failed"),
            ("review", "candidate.review_failed"),
        ):
            if token in text:
                return code
    elif normalized_stage == "persistence":
        if "excel" in text:
            return "persistence.excel_write_failed"
        if "relation" in text or "link" in text:
            return "persistence.relation_write_failed"
        if "partial" in text:
            return "persistence.partial_commit"
        if "repository" in text or "asset" in text:
            return "persistence.repository_write_failed"
    elif normalized_stage == "ui":
        if "render" in text:
            return "ui.render_error"
        if "route" in text or "http" in text:
            return "ui.route_error"
    if normalized_stage in STAGE_ORDER:
        return f"{normalized_stage}.unknown"
    return "stage.unknown"


def new_stage_trace(
    *,
    source_type: str = "",
    source_name: str = "",
    file_type: str = "",
    sha256: str = "",
    size_bytes: int | float = 0,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Create a fresh trace with a fixed, fully serializable stage list."""
    normalized_size = _bounded_number(size_bytes, field="size_bytes", maximum=MAX_SOURCE_SIZE_BYTES)
    normalized_hash = _bounded_text(sha256, 64)
    if normalized_hash and not _SHA256_PATTERN.fullmatch(normalized_hash):
        normalized_hash = ""
    stages = [
        {
            "stage": stage,
            "status": "skipped",
            "error_code": "",
            "error_summary": "",
            "duration_ms": 0,
            "counts": {},
            "route": "",
        }
        for stage in STAGE_ORDER
    ]
    trace = {
        "schema_version": STAGE_TRACE_SCHEMA_VERSION,
        "trace_id": _bounded_text(trace_id or f"trace_{uuid.uuid4().hex}", 80),
        "source": {
            "source_type": _bounded_text(source_type, 80),
            "source_name": _basename(source_name),
            "file_type": _bounded_text(file_type, 20),
            "sha256": normalized_hash,
            "size_bytes": normalized_size,
        },
        "route": {},
        "stages": stages,
        "summary": {},
    }
    trace["summary"] = summarize_stage_trace(trace)
    return trace


def append_stage(
    trace: dict[str, Any],
    stage: str,
    status: str,
    *,
    error: Any = "",
    error_code: str = "",
    duration_ms: int | float = 0,
    counts: Mapping[str, Any] | None = None,
    route: str = "",
) -> dict[str, Any]:
    """Replace one fixed stage and refresh the bounded summary."""
    if stage not in STAGE_ORDER:
        raise ValueError(f"Unknown stage: {stage}")
    if status not in STAGE_STATUSES:
        raise ValueError(f"Unknown stage status: {status}")
    if not isinstance(trace, dict):
        raise ValueError("trace must be an object")
    duration = _bounded_number(duration_ms, field="duration_ms")
    normalized_code = _bounded_text(error_code, 120) or (
        map_error_code(stage, error) if status == "failed" or status == "blocked" else ""
    )
    record = {
        "stage": stage,
        "status": status,
        "error_code": normalized_code,
        "error_summary": safe_error_summary(error) if status in {"failed", "blocked"} else "",
        "duration_ms": duration,
        "counts": _safe_counts(counts),
        "route": _bounded_text(route, MAX_ROUTE_CHARS),
    }
    stages = trace.get("stages")
    if not isinstance(stages, list):
        stages = []
    by_stage = {str(item.get("stage")): dict(item) for item in stages if isinstance(item, Mapping)}
    by_stage[stage] = record
    trace["stages"] = [by_stage.get(name, {
        "stage": name, "status": "skipped", "error_code": "",
        "error_summary": "",
        "duration_ms": 0, "counts": {}, "route": "",
    }) for name in STAGE_ORDER]
    trace["summary"] = summarize_stage_trace(trace)
    return trace


def summarize_stage_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    stages = [item for item in trace.get("stages", []) if isinstance(item, Mapping)]
    ordered = {stage: index for index, stage in enumerate(STAGE_ORDER)}
    stages.sort(key=lambda item: ordered.get(str(item.get("stage")), len(STAGE_ORDER)))
    failed = next((item for item in stages if item.get("status") == "failed"), None)
    blocked = next((item for item in stages if item.get("status") == "blocked"), None)
    executed = [item for item in stages if item.get("status") != "skipped"]
    if failed is not None:
        terminal_status = "failed"
        terminal = failed
    elif blocked is not None:
        terminal_status = "blocked"
        terminal = blocked
    elif executed:
        terminal_status = "success"
        terminal = executed[-1]
    else:
        terminal_status = "pending"
        terminal = {}
    stage_name = str(terminal.get("stage") or "")
    error_code = str(terminal.get("error_code") or "")
    return {
        "terminal_status": terminal_status,
        "failed_stage": stage_name if terminal_status in {"failed", "blocked"} else "",
        "error_code": error_code,
        "next_action": (
            "manual_review" if terminal_status == "blocked" else
            "inspect_or_retry" if terminal_status == "failed" else
            "none" if terminal_status == "success" else "not_run"
        ),
    }


def normalize_stage_trace(trace: Any) -> dict[str, Any] | None:
    """Accept only our bounded trace shape when reading a legacy result."""
    if not isinstance(trace, Mapping):
        return None
    source = trace.get("source") if isinstance(trace.get("source"), Mapping) else {}
    try:
        source_size = _bounded_number(
            source.get("size_bytes", 0),
            field="size_bytes",
            maximum=MAX_SOURCE_SIZE_BYTES,
        )
    except ValueError:
        source_size = 0
    normalized = new_stage_trace(
        source_type=source.get("source_type", ""),
        source_name=source.get("source_name", ""),
        file_type=source.get("file_type", ""),
        sha256=source.get("sha256", ""),
        size_bytes=source_size,
        trace_id=str(trace.get("trace_id") or "") or None,
    )
    route = trace.get("route")
    if isinstance(route, Mapping):
        normalized["route"] = {
            _bounded_text(key, 60): _bounded_text(value, MAX_ROUTE_CHARS)
            for key, value in route.items()
            if _bounded_text(key, 60) and isinstance(value, (str, int, float, bool))
        }
    for item in trace.get("stages", []) if isinstance(trace.get("stages"), list) else []:
        if not isinstance(item, Mapping):
            continue
        stage = str(item.get("stage") or "")
        if stage not in STAGE_ORDER:
            continue
        status = str(item.get("status") or "skipped")
        status = status if status in STAGE_STATUSES else "skipped"
        try:
            duration = _bounded_number(item.get("duration_ms", 0), field="duration_ms")
        except ValueError:
            duration = 0
        append_stage(
            normalized,
            stage,
            status,
            error=str(item.get("error_summary") or "") if status in {"failed", "blocked"} else "",
            error_code=str(item.get("error_code") or ""),
            duration_ms=duration,
            counts=safe_counts(item.get("counts") if isinstance(item.get("counts"), Mapping) else {}),
            route=str(item.get("route") or ""),
        )
    normalized["summary"] = summarize_stage_trace(normalized)
    return normalized


def attach_stage_trace(processing_result: Mapping[str, Any] | None, trace: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow-compatible processing result with a normalized trace."""
    result = dict(processing_result or {})
    normalized = normalize_stage_trace(trace)
    if normalized is not None:
        result["stage_trace"] = normalized
    return result
