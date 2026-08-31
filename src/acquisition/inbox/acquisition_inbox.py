"""Synchronous, append-preserving inbox records over the existing acquisition workflow."""

from __future__ import annotations

import json
import hashlib
import math
import uuid
import copy
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.acquisition.browser_capture import load_saved_browser_source
from src.operator.acquisition_workflow import (
    AcquisitionWorkflowPaths,
    SUPPORTED_UPLOAD_SUFFIXES,
    acquire_file,
    acquire_headless_browser_dom_with_source,
    acquire_notice_content_attachments,
    acquire_xlsx_row,
    retry_candidate_refresh,
)
from src.diagnostics import normalize_stage_trace, summarize_stage_trace
from src.semantic.production_ai_first import file_processing_deadline


DEFAULT_ACQUISITION_INBOX = Path("data/diagnostics/acquisition_inbox.json")
DEFAULT_ACQUISITION_INBOX_SUMMARY = Path("data/diagnostics/acquisition_inbox_summary.json")
DEFAULT_MANUAL_REMEDIATION_BACKUP_ROOT = Path("data/backups/manual_remediation")
RECEIVED = "RECEIVED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
PROCESSABLE_STATUSES = {RECEIVED, FAILED}
REPOSITORY_PENDING = "PENDING"
EXCEL_NOT_WRITTEN = "NOT_WRITTEN"
HIDDEN_OPERATOR_ORIGINS = {"browser_acceptance_harness", "automation_fixture"}
HEADLESS_CAPTURE_ORIGIN = "operator_headless_browser_capture"
HEADLESS_CAPTURE_PROVENANCE = {
    "source_origin": HEADLESS_CAPTURE_ORIGIN,
    "capture_method": "http_first_current_region_with_browser_fallback",
    "submission_method": "operator_browser_capture",
    "acquisition_method": "http_first_current_region_with_browser_fallback",
}

_INBOX_IO_LOCK = threading.RLock()
FILE_PROCESSING_TIMEOUT_ENV = "BIDDING_ASSET_FILE_PROCESSING_TIMEOUT_SECONDS"
DEFAULT_FILE_PROCESSING_TIMEOUT_SECONDS = 300.0
MAX_FILE_PROCESSING_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class AcquisitionInboxPaths:
    inbox: Path = DEFAULT_ACQUISITION_INBOX
    summary: Path = DEFAULT_ACQUISITION_INBOX_SUMMARY
    manual_remediation_backup_root: Path = DEFAULT_MANUAL_REMEDIATION_BACKUP_ROOT


def create_url_item(
    url: str,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    batch_id: str = "",
) -> dict[str, Any]:
    return create_url_items([url], paths, batch_id=batch_id)[0]


def create_url_items(
    urls: list[str],
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    batch_id: str = "",
) -> list[dict[str, Any]]:
    """Create a URL batch with one whole-Inbox read and at most one write."""
    items = load_inbox_items(paths.inbox)
    results: list[dict[str, Any]] = []
    created_any = False
    for url in urls:
        normalized = normalize_url(url)
        if not normalized:
            raise ValueError("URL is required")
        source_key = url_source_key(normalized)
        existing = preferred_url_item(items, source_key)
        if existing is not None:
            result = dict(existing)
            result["_created"] = False
            results.append(result)
            continue
        created_item = new_item(
            "url",
            source_url=normalized,
            batch_id=batch_id,
            normalized_url=normalized,
            source_key=source_key,
            source_origin="operator",
        )
        items.append(created_item)
        result = dict(created_item)
        result["_created"] = True
        results.append(result)
        created_any = True
    if created_any:
        write_inbox(items, paths)
    return results


def create_file_item(
    source_file: Path | str,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    batch_id: str = "",
) -> dict[str, Any]:
    path = Path(source_file)
    if path.suffix.lower() not in SUPPORTED_UPLOAD_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_UPLOAD_SUFFIXES))
        raise ValueError(f"Unsupported upload type: {path.suffix}. Allowed: {allowed}")
    return append_item(new_item("file", source_file=str(path), batch_id=batch_id), paths)


def _legacy_process_item(
    inbox_id: str,
    workflow_paths: AcquisitionWorkflowPaths,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    force: bool = False,
    attempt_origin: str = "operator",
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Process supported non-URL inputs without changing their established workflow."""
    timeout_seconds = configured_file_processing_timeout_seconds()
    claim = _claim_file_attempt(
        inbox_id,
        paths,
        force=force,
        attempt_origin=attempt_origin,
        timeout_seconds=timeout_seconds,
    )
    if claim.get("skipped"):
        return claim
    item = dict(claim["item"])
    attempt_id = str(claim["attempt_id"])
    deadline = perf_counter() + timeout_seconds
    timer = threading.Timer(
        timeout_seconds,
        _expire_file_attempt,
        kwargs={
            "inbox_id": inbox_id,
            "attempt_id": attempt_id,
            "paths": paths,
            "timeout_seconds": timeout_seconds,
        },
    )
    timer.daemon = True
    timer.start()
    try:
        effective_capture_root = (
            Path(capture_root) if capture_root is not None else Path("data/web_capture")
        )
        with file_processing_deadline(deadline):
            workflow_result = run_workflow(
                item, workflow_paths, capture_root=effective_capture_root
            )
        processing_result = dict(workflow_result.get("processing") or {})
        processing_result["automatic_chrome_fallback"] = {
            "attempted": False,
            "skipped_reason": "not_url",
            "max_chrome_attempts": 1,
        }
        stored, committed = _commit_file_attempt_success(
            inbox_id,
            attempt_id,
            paths,
            workflow_result,
            processing_result,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
        )
        if not committed:
            return {
                "item": stored,
                "workflow": {},
                "skipped": True,
                "reason": "attempt_no_longer_active",
            }
        return {"item": stored, "workflow": workflow_result, "skipped": False}
    except Exception as exc:
        stored, committed = _commit_file_attempt_failure(
            inbox_id,
            attempt_id,
            paths,
            exc,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
        )
        return {
            "item": stored,
            "workflow": {},
            "skipped": not committed,
            **({"reason": "attempt_no_longer_active"} if not committed else {}),
        }
    finally:
        timer.cancel()


def process_item(
    inbox_id: str,
    workflow_paths: AcquisitionWorkflowPaths,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    force: bool = False,
    attempt_origin: str = "operator",
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Process URL items from one guarded rendered DOM; retain the file workflow unchanged."""
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    if str(item.get("source_type") or "") != "url":
        return _legacy_process_item(
            inbox_id,
            workflow_paths,
            paths,
            force=force,
            attempt_origin=attempt_origin,
            capture_root=capture_root,
        )
    if str(item.get("status") or "") == COMPLETED and not force:
        return {"item": dict(item), "skipped": True, "reason": "already_completed"}
    if str(item.get("status") or "") not in PROCESSABLE_STATUSES and not force:
        return {"item": dict(item), "skipped": True, "reason": f"status={item.get('status', '')}"}

    attempt = begin_attempt(item, attempt_origin)
    attempt["provenance"] = dict(HEADLESS_CAPTURE_PROVENANCE)
    item["status"] = PROCESSING
    item["error_type"] = ""
    item["error_code"] = ""
    item["error_message"] = ""
    item["failed_time"] = ""
    item["failed_stage"] = ""
    write_inbox(items, paths)

    try:
        effective_capture_root = Path(capture_root) if capture_root is not None else Path("data/web_capture")
        workflow_result, page_source = acquire_headless_browser_dom_with_source(
            str(item.get("source_url") or ""),
            workflow_paths,
            capture_root=effective_capture_root,
            source_origin=HEADLESS_CAPTURE_ORIGIN,
            submission_method="operator_browser_capture",
            max_browser_attempts=1,
            automatic_public_url=True,
        )
        source_metadata = dict(page_source.metadata or {})
        selected_provenance = dict(source_metadata.get("provenance") or {})
        selected_access = dict(source_metadata.get("access") or {})
        attempt["provenance"] = {
            **dict(HEADLESS_CAPTURE_PROVENANCE),
            "capture_method": str(
                selected_provenance.get("capture_method")
                or HEADLESS_CAPTURE_PROVENANCE["capture_method"]
            ),
            "acquisition_method": str(
                selected_access.get("acquisition_method")
                or HEADLESS_CAPTURE_PROVENANCE["acquisition_method"]
            ),
        }
        processing_result = dict(workflow_result.get("processing") or {})
        item["status"] = COMPLETED
        item["completed_time"] = utc_now()
        item["generated_asset_ids"] = candidate_ids(workflow_result)
        item["processing_result"] = processing_result
        item["sources"] = append_source_snapshots(
            item.get("sources", []), workflow_result.get("sources", [])
        )
        snapshot_attempt(attempt, processing_result, item["generated_asset_ids"])
        finish_attempt(attempt, COMPLETED)
        write_inbox(items, paths)
        return {"item": dict(item), "workflow": workflow_result, "skipped": False}
    except Exception as exc:  # DOM capture/processing failure is terminal and retryable by explicit action.
        item["status"] = FAILED
        item["error_type"] = type(exc).__name__
        item["error_code"] = str(getattr(exc, "code", ""))
        item["error_message"] = str(exc)
        item["failed_time"] = utc_now()
        item["failed_stage"] = "rendered_dom_acquisition"
        route_evidence = getattr(exc, "acquisition_route", None)
        timing_evidence = getattr(exc, "stage_timings_ms", None)
        if isinstance(route_evidence, dict):
            attempt["provenance"] = {
                **dict(HEADLESS_CAPTURE_PROVENANCE),
                "acquisition_route": dict(route_evidence),
            }
            item["processing_result"] = {
                **dict(item.get("processing_result") or {}),
                "acquisition_route": dict(route_evidence),
                "stage_timings_ms": (
                    dict(timing_evidence) if isinstance(timing_evidence, dict) else {}
                ),
            }
        diagnostic = getattr(exc, "processing_result", None)
        if isinstance(diagnostic, dict):
            item["processing_result"] = dict(diagnostic)
        item["generated_asset_ids"] = []
        item["sources"] = []
        snapshot_attempt(attempt, dict(item.get("processing_result") or {}), [])
        finish_attempt(attempt, FAILED, type(exc).__name__, str(exc))
        write_inbox(items, paths)
        return {"item": dict(item), "workflow": {}, "skipped": False}


def parse_item_attachments(
    inbox_id: str,
    workflow_paths: AcquisitionWorkflowPaths,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Run the explicit URL attachment pass without repeating page acquisition or page AI."""
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    if str(item.get("source_type") or "") != "url":
        raise ValueError("附件二次解析仅适用于 URL 页面关联附件。")
    if str(item.get("status") or "") != COMPLETED:
        raise ValueError("仅技术处理完成的 URL 任务可解析关联附件。")
    processing = dict(item.get("processing_result") or {})
    attachments = dict(processing.get("attachments") or {})
    if not attachments.get("total_count"):
        raise ValueError("当前 URL 页面没有发现可解析附件。")
    active_mainline = str(processing.get("mainline") or "") == "notice_content_dom_qwen/v1"
    if not active_mainline:
        raise ValueError("legacy_url_attachment_disabled: 旧 URL 任务不支持附件二次解析。")
    effective_capture_root = Path(capture_root) if capture_root is not None else Path("data/web_capture")
    eligibility = {
        "status": str(processing.get("confirmation_eligibility") or "blocked"),
        "content_status": str(processing.get("content_status") or "content_ready"),
    }
    completeness = dict(processing.get("field_completeness") or {})
    missing_fields = [str(value) for value in completeness.get("missing_fields") or [] if str(value)]
    attachment_items = [row for row in attachments.get("items") or [] if isinstance(row, dict)]
    pending_count = (
        int(attachments.get("pending_count") or 0) if "pending_count" in attachments else
        sum(1 for row in attachment_items if row.get("download_status") == "not_requested")
    )
    retryable_count = (
        int(attachments.get("retryable_count") or 0) if "retryable_count" in attachments else
        sum(
            1 for row in attachment_items
            if row.get("download_status") == "failed" or row.get("parse_status") == "failed"
        )
    )
    if str(eligibility.get("content_status") or "") != "content_ready":
        return {"item": dict(item), "workflow": {}, "skipped": True, "reason": "content_not_ready"}
    if not missing_fields or not attachments.get("requires_explicit_parse"):
        return {"item": dict(item), "workflow": {}, "skipped": True, "reason": "not_required"}
    if pending_count <= 0 and retryable_count <= 0:
        return {
            "item": dict(item), "workflow": {}, "skipped": True,
            "reason": "no_pending_or_retryable_attachments",
        }

    source = load_saved_browser_source(
        str(processing.get("source_id") or ""), effective_capture_root
    )
    if source is None:
        raise ValueError("缺少已保存的网页采集，不能在不重新抓取网页的前提下解析附件。")
    attempt = begin_attempt(item, "operator_explicit_attachment_parse")
    attempt["provenance"] = {
        "source_origin": "operator_explicit_attachment_parse",
        "capture_method": "saved_page_attachment_links",
        "submission_method": "operator_explicit_action",
        "page_reacquired": False,
        "page_ai_reinvoked": False,
    }
    item["status"] = PROCESSING
    write_inbox(items, paths)
    try:
        workflow_result = acquire_notice_content_attachments(
            source,
            processing,
            workflow_paths,
            capture_root=effective_capture_root,
        )
        updated = dict(workflow_result.get("processing") or processing)
        item["status"] = COMPLETED
        item["completed_time"] = utc_now()
        item["processing_result"] = updated
        new_ids = candidate_ids(workflow_result)
        if new_ids:
            item["generated_asset_ids"] = new_ids
        item["sources"] = append_source_snapshots(item.get("sources", []), workflow_result.get("sources", []))
        snapshot_attempt(attempt, updated, item.get("generated_asset_ids", []))
        finish_attempt(attempt, COMPLETED)
        write_inbox(items, paths)
        return {"item": dict(item), "workflow": workflow_result, "skipped": False}
    except Exception as exc:
        item["status"] = COMPLETED
        item["processing_result"] = processing
        attempt["attachment_failure_isolated"] = True
        finish_attempt(attempt, FAILED, type(exc).__name__, str(exc))
        write_inbox(items, paths)
        return {"item": dict(item), "workflow": {}, "skipped": False, "error": str(exc)}


def retry_item_downstream_refresh(
    inbox_id: str,
    workflow_paths: AcquisitionWorkflowPaths,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
) -> dict[str, Any]:
    """Retry only stale derived views while preserving the captured source and Candidate."""
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    if str(item.get("status") or "") != COMPLETED:
        raise ValueError("仅技术采集完成的任务可重试后处理。")
    processing = dict(item.get("processing_result") or {})
    downstream = dict(processing.get("downstream_refresh") or {})
    if downstream.get("status") != "failed" or not downstream.get("retryable"):
        return {"item": dict(item), "skipped": True, "reason": "downstream_refresh_not_required"}
    changed_ids = {
        str(value) for value in (
            downstream.get("changed_candidate_ids") or item.get("generated_asset_ids") or []
        ) if str(value)
    }
    attempt = begin_attempt(item, "operator_retry_downstream_refresh")
    attempt["provenance"] = {
        "source_origin": "operator_retry_downstream_refresh",
        "capture_method": "saved_candidate_refresh",
        "submission_method": "operator_explicit_action",
        "page_reacquired": False,
        "page_ai_reinvoked": False,
        "candidate_rewritten": False,
    }
    refresh = retry_candidate_refresh(changed_ids, workflow_paths)
    processing["downstream_refresh"] = {
        key: value for key, value in refresh.items() if key not in {"timings", "performance"}
    }
    if refresh.get("status") == "success":
        prior = dict(processing.get("pre_refresh_confirmation_state") or {})
        processing["post_processing_status"] = "success"
        processing["confirmation_eligibility"] = str(
            prior.get("confirmation_eligibility") or processing.get("confirmation_eligibility") or "blocked"
        )
        processing["block_reason"] = str(prior.get("block_reason") or "")
        processing["next_action"] = str(prior.get("next_action") or "none")
        finish_attempt(attempt, COMPLETED)
    else:
        processing["post_processing_status"] = "failed"
        processing["confirmation_eligibility"] = "blocked"
        processing["block_reason"] = (
            f"网页采集和候选保存成功，但后处理阶段 {refresh.get('failed_stage') or 'unknown'} 失败；"
            "请重试后处理后再确认。"
        )
        processing["next_action"] = "retry_downstream_refresh"
        finish_attempt(
            attempt, FAILED, str(refresh.get("error_type") or "DownstreamRefreshError"),
            str(refresh.get("error_message") or "downstream refresh failed"),
        )
    item["processing_result"] = processing
    snapshot_attempt(attempt, processing, item.get("generated_asset_ids", []))
    write_inbox(items, paths)
    return {"item": dict(item), "refresh": refresh, "skipped": False}




def capture_headless_browser_dom(
    inbox_id: str,
    workflow_paths: AcquisitionWorkflowPaths,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Append one operator-triggered headless capture to an existing URL item."""
    return process_item(
        inbox_id,
        workflow_paths,
        paths,
        force=True,
        attempt_origin=HEADLESS_CAPTURE_ORIGIN,
        capture_root=capture_root,
    )


def append_source_snapshots(existing: Any, new_values: Any) -> list[dict[str, Any]]:
    """Preserve prior HTTP evidence while appending the rendered browser capture."""
    result = [dict(value) for value in (existing or []) if isinstance(value, dict)]
    seen = {
        (
            str(value.get("source_id") or ""),
            str(value.get("source_url") or ""),
            str(value.get("created_time") or ""),
        )
        for value in result
    }
    for value in (new_values or []):
        if not isinstance(value, dict):
            continue
        snapshot = dict(value)
        identity = (
            str(snapshot.get("source_id") or ""),
            str(snapshot.get("source_url") or ""),
            str(snapshot.get("created_time") or ""),
        )
        if identity not in seen:
            result.append(snapshot)
            seen.add(identity)
    return result


def run_workflow(
    item: dict[str, Any], workflow_paths: AcquisitionWorkflowPaths, *, capture_root: Path | None = None
) -> dict[str, Any]:
    source_type = str(item.get("source_type") or "")
    if source_type == "file":
        return acquire_file(Path(str(item.get("source_file") or "")), workflow_paths)
    if source_type == "xlsx_row":
        return acquire_xlsx_row(dict(item.get("xlsx_row_payload") or {}), workflow_paths)
    raise ValueError(f"Legacy workflow does not support source_type: {source_type}")


def fail_item(
    inbox_id: str,
    message: str,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
    *,
    stage: str = "acquisition",
) -> dict[str, Any]:
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    attempt = begin_attempt(item, "operator_validation")
    item["status"] = FAILED
    item["error_type"] = "ValueError"
    item["error_message"] = message
    item["failed_time"] = utc_now()
    item["failed_stage"] = stage
    finish_attempt(attempt, FAILED, "ValueError", message)
    write_inbox(items, paths)
    return dict(item)


def new_item(
    source_type: str,
    *,
    source_url: str = "",
    source_file: str = "",
    batch_id: str = "",
    normalized_url: str = "",
    source_key: str = "",
    source_origin: str = "operator",
) -> dict[str, Any]:
    return {
        "inbox_id": f"acquisition_inbox_{uuid.uuid4().hex}",
        "batch_id": batch_id,
        "source_type": source_type,
        "source_url": source_url,
        "normalized_url": normalized_url,
        "source_key": source_key,
        "source_origin": source_origin,
        "duplicate_of": "",
        "hidden_from_operator": False,
        "attempt_history": [],
        "source_file": source_file,
        "upload_status": "SUCCESS" if source_type == "file" else "NOT_APPLICABLE",
        "created_time": utc_now(),
        "status": RECEIVED,
        "error_message": "",
        "error_type": "",
        "failed_time": "",
        "generated_asset_ids": [],
        "processing_result": {},
        "sources": [],
        "failed_stage": "",
        "confirm_status": "",
        "confirm_failure_stage": "",
        "confirm_failure_reason": "",
        "confirm_time": "",
        "repository_status": REPOSITORY_PENDING,
        "repository_time": "",
        "repository_error": "",
        "excel_status": EXCEL_NOT_WRITTEN,
        "excel_time": "",
        "excel_error": "",
        "excel_action": "",
        "excel_conflicts": [],
    }


def update_confirmation_result(
    inbox_id: str,
    status: str,
    reason: str = "",
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
) -> dict[str, Any]:
    """Persist the per-item confirmation outcome without introducing a batch state model."""
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    item["confirm_status"] = status
    item["confirm_failure_stage"] = "confirm" if status == "FAILED" else ""
    item["confirm_failure_reason"] = reason
    item["confirm_time"] = utc_now()
    write_inbox(items, paths)
    return dict(item)


def update_business_sync_result(
    inbox_id: str,
    *,
    repository_status: str | None = None,
    repository_error: str | None = None,
    excel_status: str | None = None,
    excel_error: str | None = None,
    excel_action: str | None = None,
    excel_conflicts: list[dict[str, Any]] | None = None,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
) -> dict[str, Any]:
    """Persist Repository and Excel outcomes independently for retryable confirmation."""
    items = load_inbox_items(paths.inbox)
    item = find_item(items, inbox_id)
    if item is None:
        raise ValueError(f"Inbox item not found: {inbox_id}")
    timestamp = utc_now()
    if repository_status is not None:
        item["repository_status"] = repository_status
        item["repository_time"] = timestamp
    if repository_error is not None:
        item["repository_error"] = repository_error
    if excel_status is not None:
        item["excel_status"] = excel_status
        item["excel_time"] = timestamp
    if excel_error is not None:
        item["excel_error"] = excel_error
    if excel_action is not None:
        item["excel_action"] = excel_action
    if excel_conflicts is not None:
        item["excel_conflicts"] = [dict(row) for row in excel_conflicts]
    write_inbox(items, paths)
    return dict(item)


def append_item(item: dict[str, Any], paths: AcquisitionInboxPaths) -> dict[str, Any]:
    items = load_inbox_items(paths.inbox)
    items.append(item)
    write_inbox(items, paths)
    return dict(item)


def reconcile_xlsx_row_items(
    items: list[dict[str, Any]],
    parent: dict[str, Any],
    row_tasks: list[Any],
) -> dict[str, int]:
    """Upsert row-level Inbox items by file hash, sheet, and original row identity."""
    created_count = 0
    reused_completed_count = 0
    failed_count = 0
    processed_count = 0
    for raw_task in row_tasks:
        if not isinstance(raw_task, dict):
            continue
        source_key = str(raw_task.get("source_key") or "")
        if not source_key:
            continue
        existing = next(
            (
                row for row in items
                if str(row.get("source_type") or "") == "xlsx_row"
                and str(row.get("source_key") or "") == source_key
            ),
            None,
        )
        target_status = str(raw_task.get("status") or FAILED)
        if existing is not None and str(existing.get("status") or "") == COMPLETED:
            reused_completed_count += 1
            processed_count += 1
            continue
        if existing is None:
            processing = dict(raw_task.get("processing") or {})
            existing = new_item(
                "xlsx_row",
                source_url=str(processing.get("source_url") or ""),
                source_file=str(parent.get("source_file") or ""),
                batch_id=str(parent.get("batch_id") or "") or str(parent.get("inbox_id") or ""),
                source_key=source_key,
                source_origin="xlsx_ingestion",
            )
            existing["task_role"] = "xlsx_business_row"
            existing["parent_inbox_id"] = str(parent.get("inbox_id") or "")
            existing["upload_status"] = "SUCCESS"
            items.append(existing)
            created_count += 1

        attempt = begin_attempt(existing, "xlsx_workbook_processing")
        processing = dict(raw_task.get("processing") or {})
        existing["processing_result"] = processing
        existing["sources"] = [dict(row) for row in raw_task.get("sources", []) if isinstance(row, dict)]
        existing["source_url"] = str(processing.get("source_url") or "")
        existing["xlsx_row_payload"] = dict(raw_task.get("row_payload") or {})
        existing["status"] = target_status
        existing["generated_asset_ids"] = [str(raw_task.get("candidate_id") or "")] if raw_task.get("candidate_id") else []
        existing["error_type"] = str(raw_task.get("error_type") or "")
        existing["error_message"] = str(raw_task.get("error_message") or "")
        existing["failed_stage"] = "xlsx_row" if target_status == FAILED else ""
        if target_status == COMPLETED:
            existing["completed_time"] = utc_now()
            existing["failed_time"] = ""
            finish_attempt(attempt, COMPLETED)
        else:
            existing["failed_time"] = utc_now()
            finish_attempt(
                attempt,
                FAILED,
                str(raw_task.get("error_type") or "XlsxRowError"),
                str(raw_task.get("error_message") or "该 Excel 行无法转换为业务候选。"),
            )
            failed_count += 1
        processed_count += 1
    return {
        "row_task_count": processed_count,
        "created_row_task_count": created_count,
        "reused_completed_row_task_count": reused_completed_count,
        "failed_row_task_count": failed_count,
    }


def configured_file_processing_timeout_seconds() -> float:
    raw = os.environ.get(FILE_PROCESSING_TIMEOUT_ENV, "").strip()
    timeout = float(raw) if raw else DEFAULT_FILE_PROCESSING_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_file_processing_timeout")
    return min(timeout, MAX_FILE_PROCESSING_TIMEOUT_SECONDS)


def _claim_file_attempt(
    inbox_id: str,
    paths: AcquisitionInboxPaths,
    *,
    force: bool,
    attempt_origin: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Atomically claim one file item without regressing another concurrent item."""

    with _INBOX_IO_LOCK:
        items = _load_inbox_items_unlocked(paths.inbox)
        item = find_item(items, inbox_id)
        if item is None:
            raise ValueError(f"Inbox item not found: {inbox_id}")
        if str(item.get("source_type") or "") == "url":
            raise ValueError("URL items must use the Playwright notice-content mainline")
        status = str(item.get("status") or "")
        if status == COMPLETED and not force:
            return {"item": copy.deepcopy(item), "skipped": True, "reason": "already_completed"}
        if status not in PROCESSABLE_STATUSES and not force:
            return {"item": copy.deepcopy(item), "skipped": True, "reason": f"status={status}"}
        if (
            str(item.get("source_type") or "") == "file"
            and Path(str(item.get("source_file") or "")).suffix.lower() == ".xlsx"
        ):
            item["task_role"] = "xlsx_workbook_summary"
        attempt = begin_attempt(item, attempt_origin)
        deadline_time = (
            datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        ).isoformat()
        attempt["processing_timeout_seconds"] = timeout_seconds
        attempt["deadline_time"] = deadline_time
        item.update({
            "status": PROCESSING,
            "active_attempt_id": str(attempt["attempt_id"]),
            "processing_timeout_seconds": timeout_seconds,
            "processing_deadline_time": deadline_time,
            "error_message": "",
            "error_type": "",
            "error_code": "",
            "failed_time": "",
            "failed_stage": "",
            "completed_time": "",
        })
        _write_inbox_unlocked(items, paths)
        return {
            "item": copy.deepcopy(item),
            "attempt_id": str(attempt["attempt_id"]),
            "skipped": False,
        }


def _active_file_attempt(
    item: dict[str, Any], attempt_id: str
) -> dict[str, Any] | None:
    if (
        str(item.get("status") or "") != PROCESSING
        or str(item.get("active_attempt_id") or "") != attempt_id
    ):
        return None
    history = item.get("attempt_history")
    if not isinstance(history, list):
        return None
    return next(
        (
            attempt for attempt in history
            if isinstance(attempt, dict)
            and str(attempt.get("attempt_id") or "") == attempt_id
        ),
        None,
    )


def _commit_file_attempt_success(
    inbox_id: str,
    attempt_id: str,
    paths: AcquisitionInboxPaths,
    workflow_result: dict[str, Any],
    processing_result: dict[str, Any],
    *,
    deadline: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], bool]:
    with _INBOX_IO_LOCK:
        items = _load_inbox_items_unlocked(paths.inbox)
        item = find_item(items, inbox_id)
        if item is None:
            raise ValueError(f"Inbox item not found: {inbox_id}")
        attempt = _active_file_attempt(item, attempt_id)
        if attempt is None:
            return copy.deepcopy(item), False
        if perf_counter() >= deadline:
            _mark_file_attempt_timeout(item, attempt, timeout_seconds)
            _write_inbox_unlocked(items, paths)
            return copy.deepcopy(item), False
        item["status"] = COMPLETED
        item["completed_time"] = utc_now()
        row_tasks = workflow_result.get("row_tasks")
        if isinstance(row_tasks, list):
            processing_result.update(reconcile_xlsx_row_items(items, item, row_tasks))
            item["task_role"] = "xlsx_workbook_summary"
            item["generated_asset_ids"] = []
            file_hash_value = str(processing_result.get("file_hash") or "")
            if file_hash_value:
                item["source_key"] = f"xlsx_workbook_sha256:{file_hash_value}"
        else:
            item["generated_asset_ids"] = candidate_ids(workflow_result)
        item["processing_result"] = processing_result
        item["sources"] = append_source_snapshots(
            item.get("sources", []), workflow_result.get("sources", [])
        )
        item["active_attempt_id"] = ""
        snapshot_attempt(attempt, processing_result, item.get("generated_asset_ids", []))
        finish_attempt(attempt, COMPLETED)
        _write_inbox_unlocked(items, paths)
        return copy.deepcopy(item), True


def _commit_file_attempt_failure(
    inbox_id: str,
    attempt_id: str,
    paths: AcquisitionInboxPaths,
    exc: Exception,
    *,
    deadline: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], bool]:
    with _INBOX_IO_LOCK:
        items = _load_inbox_items_unlocked(paths.inbox)
        item = find_item(items, inbox_id)
        if item is None:
            raise ValueError(f"Inbox item not found: {inbox_id}")
        attempt = _active_file_attempt(item, attempt_id)
        if attempt is None:
            return copy.deepcopy(item), False
        if perf_counter() >= deadline:
            _mark_file_attempt_timeout(item, attempt, timeout_seconds)
            _write_inbox_unlocked(items, paths)
            return copy.deepcopy(item), False
        item.update({
            "status": FAILED,
            "active_attempt_id": "",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "failed_time": utc_now(),
            "failed_stage": "acquisition",
        })
        diagnostic = getattr(exc, "processing_result", None)
        if isinstance(diagnostic, dict):
            item["processing_result"] = dict(diagnostic)
            trace = normalize_stage_trace(diagnostic.get("stage_trace"))
            if trace is not None:
                summary = summarize_stage_trace(trace)
                item["failed_stage"] = str(summary.get("failed_stage") or "acquisition")
                item["error_code"] = str(summary.get("error_code") or "")
            semantic_reason = str(
                dict(diagnostic.get("semantic") or {}).get("failure_reason") or ""
            )
            if semantic_reason in {"OPENROUTER_WALL_CLOCK_TIMEOUT", "FILE_PROCESSING_TIMEOUT"}:
                item["failed_stage"] = "semantic"
                item["error_code"] = semantic_reason
        snapshot_attempt(
            attempt,
            dict(item.get("processing_result") or {}),
            item.get("generated_asset_ids", []),
        )
        finish_attempt(attempt, FAILED, type(exc).__name__, str(exc))
        _write_inbox_unlocked(items, paths)
        return copy.deepcopy(item), True


def _expire_file_attempt(
    *,
    inbox_id: str,
    attempt_id: str,
    paths: AcquisitionInboxPaths,
    timeout_seconds: float,
) -> None:
    """Persist the deadline terminal state; a late worker loses its attempt lease."""

    with _INBOX_IO_LOCK:
        items = _load_inbox_items_unlocked(paths.inbox)
        item = find_item(items, inbox_id)
        if item is None:
            return
        attempt = _active_file_attempt(item, attempt_id)
        if attempt is None:
            return
        _mark_file_attempt_timeout(item, attempt, timeout_seconds)
        _write_inbox_unlocked(items, paths)


def _mark_file_attempt_timeout(
    item: dict[str, Any],
    attempt: dict[str, Any],
    timeout_seconds: float,
) -> None:
    timeout_result = {
        **dict(item.get("processing_result") or {}),
        "timeout": {
            "status": "failed",
            "error_code": "FILE_PROCESSING_TIMEOUT",
            "budget_seconds": timeout_seconds,
        },
    }
    item.update({
        "status": FAILED,
        "active_attempt_id": "",
        "error_type": "TimeoutError",
        "error_code": "FILE_PROCESSING_TIMEOUT",
        "error_message": "file_processing_wall_clock_timeout",
        "failed_time": utc_now(),
        "failed_stage": "file_processing",
        "processing_result": timeout_result,
    })
    snapshot_attempt(attempt, timeout_result, item.get("generated_asset_ids", []))
    finish_attempt(
        attempt,
        FAILED,
        "TimeoutError",
        "file_processing_wall_clock_timeout",
    )


def load_inbox_items(path: Path = DEFAULT_ACQUISITION_INBOX) -> list[dict[str, Any]]:
    with _INBOX_IO_LOCK:
        return _load_inbox_items_unlocked(path)


def is_manual_remediation_target(item: dict[str, Any]) -> bool:
    processing = item.get("processing_result")
    processing = processing if isinstance(processing, dict) else {}
    source_type = str(item.get("source_type") or "")
    mainline = str(processing.get("mainline") or "")
    active_file = (
        source_type in {"file", "file_upload"}
        and mainline == "unstructured_document_qwen/v1"
    )
    active_url = (
        source_type in {"url", "url_acquisition"}
        and mainline == "notice_content_dom_qwen/v1"
        and str(processing.get("content_status") or "") == "content_ready"
    )
    return bool(
        str(item.get("status") or "") == COMPLETED
        and str(item.get("repository_status") or "PENDING").upper() != "WRITTEN"
        and (active_file or active_url)
        and any(str(value) for value in item.get("generated_asset_ids") or [])
    )


def apply_manual_remediation(
    inbox_id: str,
    *,
    field: str,
    action_type: str,
    new_value: str,
    operator: str,
    expected_revision: int,
    action_id: str,
    paths: AcquisitionInboxPaths = AcquisitionInboxPaths(),
) -> dict[str, Any]:
    """Atomically append one governed manual field remediation to an Inbox item."""

    normalized_field = str(field or "").strip()
    if normalized_field not in {
        "customer", "project_number", "project_name", "content", "budget",
        "bid_open_time", "winner", "award_amount",
    }:
        raise ValueError("manual_remediation_invalid_field")
    normalized_action_type = str(action_type or "").strip()
    if normalized_action_type not in {"verify_current_value", "correct_effective_value"}:
        raise ValueError("manual_remediation_invalid_action_type")
    normalized_operator = str(operator or "").strip()
    if not normalized_operator or normalized_operator == "operator_ui":
        raise ValueError("manual_remediation_operator_required")
    normalized_action_id = str(action_id or "").strip()
    if (
        not normalized_action_id
        or len(normalized_action_id) > 128
        or any(not (character.isalnum() or character in {"-", "_"}) for character in normalized_action_id)
    ):
        raise ValueError("manual_remediation_invalid_action_id")
    try:
        requested_revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("manual_remediation_invalid_expected_revision") from exc

    with _INBOX_IO_LOCK:
        inbox_bytes = paths.inbox.read_bytes() if paths.inbox.exists() else b"[]"
        try:
            payload = json.loads(inbox_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Expected acquisition inbox JSON array: {paths.inbox}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"Expected acquisition inbox JSON array: {paths.inbox}")
        items = [dict(row) for row in payload if isinstance(row, dict)]
        item = find_item(items, inbox_id)
        if item is None:
            raise ValueError(f"Inbox item not found: {inbox_id}")
        if not is_manual_remediation_target(item):
            raise ValueError("manual_remediation_target_not_allowed")

        raw_remediation = item.get("manual_remediation")
        remediation = dict(raw_remediation) if isinstance(raw_remediation, dict) else {}
        history = [dict(row) for row in remediation.get("history") or [] if isinstance(row, dict)]
        replayed = next(
            (row for row in history if str(row.get("action_id") or "") == normalized_action_id),
            None,
        )
        if replayed is not None:
            return {
                "item": copy.deepcopy(item),
                "action": copy.deepcopy(replayed),
                "replayed": True,
                "backup_path": str(
                    paths.manual_remediation_backup_root
                    / normalized_action_id
                    / "acquisition_inbox_before.json"
                ),
            }
        if any(
            str(action.get("action_id") or "") == normalized_action_id
            for other_item in items if other_item is not item
            for action in (
                dict(other_item.get("manual_remediation") or {}).get("history") or []
                if isinstance(other_item.get("manual_remediation"), dict) else []
            )
            if isinstance(action, dict)
        ):
            raise ValueError("manual_remediation_action_id_conflict")

        current_revision = int(remediation.get("revision") or 0)
        if requested_revision != current_revision:
            raise ValueError(
                f"manual_remediation_stale_revision:expected={requested_revision}:current={current_revision}"
            )

        processing = dict(item.get("processing_result") or {})
        confirmable = processing.get("confirmable_fields")
        original_fields = confirmable if isinstance(confirmable, dict) else processing
        original_value = str(original_fields.get(normalized_field) or "").strip()
        completeness = dict(processing.get("field_completeness") or {})
        integrity = dict(processing.get("field_integrity") or {})
        missing_fields = {
            str(value) for value in completeness.get("required_missing_fields") or [] if str(value)
        }
        suspect_fields = {
            str(value) for value in completeness.get("suspect_fields") or [] if str(value)
        } | {
            str(value) for value in completeness.get("integrity_blocked_fields") or [] if str(value)
        } | {
            str(value) for value in integrity.get("suspect_fields") or [] if str(value)
        }
        is_missing = normalized_field in missing_fields or not original_value
        is_suspect = normalized_field in suspect_fields
        stored_effective_fields = remediation.get("effective_fields")
        stored_effective_fields = (
            dict(stored_effective_fields) if isinstance(stored_effective_fields, dict) else {}
        )
        previous_effective_value = str(
            stored_effective_fields.get(normalized_field) or original_value
        ).strip()
        if normalized_action_type == "verify_current_value":
            effective_value = original_value
            if not effective_value:
                raise ValueError("manual_remediation_value_required")
            system_reason = "人工确认原始提取值"
        else:
            effective_value = str(new_value or "").strip()
            if not effective_value:
                raise ValueError("manual_remediation_value_required")
            system_reason = "人工修正有效值"

        next_revision = current_revision + 1
        timestamp = utc_now()
        resolved_issues = []
        if is_missing:
            resolved_issues.append("required_missing_field")
        if is_suspect:
            resolved_issues.append("suspect_field")
        action = {
            "action_id": normalized_action_id,
            "revision": next_revision,
            "action_type": normalized_action_type,
            "operator": normalized_operator,
            "timestamp": timestamp,
            "inbox_id": str(item.get("inbox_id") or ""),
            "candidate_id": next(
                (str(value) for value in item.get("generated_asset_ids") or [] if str(value)),
                "",
            ),
            "field": normalized_field,
            "original_value": original_value,
            "previous_effective_value": previous_effective_value,
            "new_value": effective_value,
            "reason": system_reason,
            "resolved_issues": resolved_issues,
        }
        effective_fields = dict(stored_effective_fields)
        effective_fields[normalized_field] = effective_value
        history.append(action)
        updated_remediation = {
            "schema_version": "inbox-manual-remediation/v1",
            "revision": next_revision,
            "effective_fields": effective_fields,
            "history": history,
        }
        if isinstance(remediation.get("verified_evidence"), dict):
            updated_remediation["verified_evidence"] = dict(remediation["verified_evidence"])
        item["manual_remediation"] = updated_remediation

        backup_path = (
            paths.manual_remediation_backup_root
            / normalized_action_id
            / "acquisition_inbox_before.json"
        )
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            if backup_path.read_bytes() != inbox_bytes:
                raise ValueError("manual_remediation_backup_conflict")
        else:
            try:
                with backup_path.open("xb") as handle:
                    handle.write(inbox_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if backup_path.read_bytes() != inbox_bytes:
                    raise ValueError("manual_remediation_backup_conflict")

        _write_json_atomic(paths.inbox, items)
        return {
            "item": copy.deepcopy(item),
            "action": copy.deepcopy(action),
            "replayed": False,
            "backup_path": str(backup_path),
        }


def write_inbox(items: list[dict[str, Any]], paths: AcquisitionInboxPaths) -> None:
    """Merge one caller snapshot and replace Inbox artifacts atomically.

    Callers intentionally keep their existing read/modify/write shape.  The
    locked merge makes stale snapshots additive by stable ``inbox_id`` so a
    concurrent URL/file writer cannot replace the other task's record.
    """
    with _INBOX_IO_LOCK:
        current = _load_inbox_items_unlocked(paths.inbox)
        merged = _merge_inbox_items(current, items)
        _write_inbox_unlocked(merged, paths)


def _write_inbox_unlocked(items: list[dict[str, Any]], paths: AcquisitionInboxPaths) -> None:
    _write_json_atomic(paths.inbox, items)
    _write_json_atomic(paths.summary, build_summary(items))


def _load_inbox_items_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected acquisition inbox JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _merge_inbox_items(
    current: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge snapshots by stable ID while preserving append order and history."""
    merged = [copy.deepcopy(item) for item in current if isinstance(item, dict)]
    positions = {
        str(item.get("inbox_id") or ""): index
        for index, item in enumerate(merged)
        if str(item.get("inbox_id") or "")
    }
    for item in incoming:
        if not isinstance(item, dict):
            continue
        inbox_id = str(item.get("inbox_id") or "")
        if not inbox_id or inbox_id not in positions:
            merged.append(copy.deepcopy(item))
            if inbox_id:
                positions[inbox_id] = len(merged) - 1
            continue
        target = merged[positions[inbox_id]]
        prior_history = target.get("attempt_history")
        incoming_history = item.get("attempt_history")
        target.update(copy.deepcopy(item))
        if isinstance(prior_history, list) or isinstance(incoming_history, list):
            history: list[dict[str, Any]] = []
            history_positions: dict[str, int] = {}
            for attempt in [*(prior_history or []), *(incoming_history or [])]:
                if not isinstance(attempt, dict):
                    continue
                attempt_id = str(attempt.get("attempt_id") or "")
                if attempt_id and attempt_id in history_positions:
                    history[history_positions[attempt_id]].update(copy.deepcopy(attempt))
                    continue
                history.append(copy.deepcopy(attempt))
                if attempt_id:
                    history_positions[attempt_id] = len(history) - 1
            target["attempt_history"] = history
    return merged


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in (RECEIVED, PROCESSING, COMPLETED, FAILED)}
    business_items = [
        item for item in items
        if str(item.get("task_role") or "") != "xlsx_workbook_summary"
    ]
    for item in business_items:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(business_items),
        "status_counts": counts,
        "workbook_summary_count": len(items) - len(business_items),
        "updated_time": utc_now(),
    }


def find_item(items: list[dict[str, Any]], inbox_id: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("inbox_id") or "") == inbox_id:
            return item
    return None


def normalize_url(value: str) -> str:
    """Normalize only URL identity details that are safe for Inbox deduplication."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    display_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = f"{userinfo}{display_host}"
    if port is not None and not default_port:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def url_source_key(value: str) -> str:
    normalized = normalize_url(value)
    return f"url_sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}" if normalized else ""


def item_source_key(item: dict[str, Any]) -> str:
    return str(item.get("source_key") or "") or url_source_key(
        str(item.get("normalized_url") or item.get("source_url") or "")
    )


def preferred_url_item(items: list[dict[str, Any]], source_key: str) -> dict[str, Any] | None:
    matches = [item for item in items if source_key and item_source_key(item) == source_key]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: (
            str(item.get("status") or "") != COMPLETED,
            bool(item.get("duplicate_of")),
            bool(item.get("hidden_from_operator")),
            str(item.get("created_time") or ""),
        ),
    )[0]


def is_operator_visible(item: dict[str, Any]) -> bool:
    if bool(item.get("hidden_from_operator")) or bool(str(item.get("duplicate_of") or "")):
        return False
    return str(item.get("source_origin") or "") not in HIDDEN_OPERATOR_ORIGINS


def begin_attempt(item: dict[str, Any], origin: str) -> dict[str, Any]:
    history = item.setdefault("attempt_history", [])
    if not isinstance(history, list):
        history = []
        item["attempt_history"] = history
    attempt = {
        "attempt_id": f"attempt_{uuid.uuid4().hex}",
        "source_origin": origin,
        "started_time": utc_now(),
        "finished_time": "",
        "status": PROCESSING,
        "error_type": "",
        "error_message": "",
    }
    history.append(attempt)
    return attempt


def finish_attempt(attempt: dict[str, Any], status: str, error_type: str = "", error_message: str = "") -> None:
    attempt.update({
        "finished_time": utc_now(),
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
    })


def snapshot_attempt(
    attempt: dict[str, Any], processing: dict[str, Any], candidate_id_values: Any
) -> None:
    """Persist the independent URL-quality outcome of this attempt."""
    has_stage_trace = isinstance(processing.get("stage_trace"), dict)
    if not any(
        key in processing
        for key in ("access_status", "content_status", "content_quality", "confirmation_eligibility")
    ) and not has_stage_trace:
        return
    attempt.update({
        "access_status": str(processing.get("access_status") or ""),
        "access": dict(processing.get("access") or {}),
        "source_id": str(processing.get("source_id") or ""),
        "source_url": str(processing.get("source_url") or ""),
        "provenance": dict(processing.get("provenance") or {}),
        "content_status": str(processing.get("content_status") or "unknown"),
        "content_quality": dict(processing.get("content_quality") or {}),
        "extract_status": str(processing.get("extract_status") or "not_run"),
        "candidate_ids": [str(value) for value in (candidate_id_values or []) if str(value)],
        "confirmation_eligibility": str(processing.get("confirmation_eligibility") or "blocked"),
        "block_reason": str(processing.get("block_reason") or ""),
        "next_action": str(processing.get("next_action") or "manual_review"),
        "semantic": dict(processing.get("semantic") or {}),
        "attachments": dict(processing.get("attachments") or {}),
        "supporting_sources": [
            dict(row) for row in processing.get("supporting_sources", []) if isinstance(row, dict)
        ],
    })
    if has_stage_trace:
        attempt["stage_trace"] = copy.deepcopy(processing["stage_trace"])


def candidate_ids(workflow_result: dict[str, Any]) -> list[str]:
    return [
        str(candidate.get("candidate_id") or "")
        for candidate in workflow_result.get("candidates", [])
        if isinstance(candidate, dict) and str(candidate.get("candidate_id") or "")
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
