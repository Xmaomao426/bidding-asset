"""Thin orchestration from manual acquisition inputs to the existing review queue."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from collections.abc import Mapping
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from src import parser as tender_parser
from src.acquisition.browser_capture import (
    capture_browser_structured_region,
)
from src.acquisition.attachment_downloader import (
    ALLOWED_ATTACHMENT_SUFFIXES,
    AttachmentDownloadRecord,
    fetch_attachment_bytes,
    safe_filename,
    suffix_for,
    unique_path,
)
from src.acquisition.headless_browser import (
    HeadlessBrowserCapture,
    HeadlessBrowserCaptureError,
    _capture_http_html_dom,
    capture_rendered_dom,
    validate_automatic_capture_url,
)
from src.acquisition.models import DocumentSource
from src.acquisition.xlsx_adapter import parse_xlsx_workbook
from src.discovery_integration.asset_candidate_importer import (
    AssetCandidate,
    DEFAULT_ASSET_CANDIDATES_OUTPUT,
    normalize_asset_candidate,
    write_asset_candidates,
)
from src.discovery_integration.candidate_deduplicator import (
    DEFAULT_DEDUPED_CANDIDATES_OUTPUT,
    DEFAULT_DEDUP_SUMMARY_OUTPUT,
    deduplicate_candidates,
    write_dedup_outputs,
)
from src.lifecycle.asset_lifecycle import (
    DEFAULT_ASSET_LIFECYCLE_OUTPUT,
    build_asset_lifecycles,
    load_json_array,
    write_asset_lifecycles,
)
from src.review.review_queue import (
    DEFAULT_REVIEW_QUEUE_OUTPUT,
    build_review_queue,
    write_review_queue,
)
from src.semantic.production_ai_first import (
    FileProcessingDeadlineExceeded,
    invoke_semantic_ai_first,
    remaining_file_processing_seconds,
)
from src.semantic.ai_provider import (
    DOCUMENT_BUSINESS_V5_PROMPT_PATH,
    DOCUMENT_BUSINESS_V5_PROMPT_VERSION,
)
from src.semantic.models import normalize_document
from src.semantic.document_evidence import build_document_evidence, reject_placeholder_fields
from src.diagnostics import (
    append_stage,
    attach_stage_trace,
    map_error_code,
    new_stage_trace,
    safe_counts,
)


_PUBLICATION_LOCK = threading.RLock()


SUPPORTED_UPLOAD_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}
ARCHIVE_ATTACHMENT_SUFFIXES = {".zip", ".rar", ".7z"}
VISIBLE_ATTACHMENT_SUFFIXES = ALLOWED_ATTACHMENT_SUFFIXES | ARCHIVE_ATTACHMENT_SUFFIXES
HTTP_BROWSER_FALLBACK_CODES = frozenset({
    "http_transport_failed",
    "http_status_error",
    "http_content_type",
    "http_body_too_large",
    "notice_content_frame_not_found",
    "notice_content_technical_error",
    "external_access_blocked",
    "http_region_timeout",
    "http_region_technical_error",
    "dom_too_large",
})
INDEPENDENT_CANDIDATE_SOURCE_TYPES = {"xlsx_file_upload"}
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class AcquisitionWorkflowPaths:
    asset_candidates: Path = DEFAULT_ASSET_CANDIDATES_OUTPUT
    deduped_candidates: Path = DEFAULT_DEDUPED_CANDIDATES_OUTPUT
    dedup_summary: Path = DEFAULT_DEDUP_SUMMARY_OUTPUT
    lifecycle: Path = DEFAULT_ASSET_LIFECYCLE_OUTPUT
    review_queue: Path = DEFAULT_REVIEW_QUEUE_OUTPUT


@dataclass
class NoticeAttachmentBundle:
    attachment_documents: list[Any]
    attachment_summary: dict[str, Any]


def acquire_headless_browser_dom_with_source(
    url: str,
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
    *,
    capture_root: Path | None = None,
    source_origin: str = "operator_headless_browser_capture",
    submission_method: str = "operator_browser_capture",
    max_browser_attempts: int = 1,
    automatic_public_url: bool = False,
) -> tuple[dict[str, Any], DocumentSource]:
    """Acquire one current Region, HTTP-first for automatic public URLs."""
    source_url = url.strip()
    http_elapsed = 0.0
    http_failure_code = ""
    if automatic_public_url:
        # Validate before any transport. The HTTP transport and every redirect also
        # revalidate immediately before use.
        validate_automatic_capture_url(source_url)
        started = perf_counter()
        try:
            captured = _capture_http_html_dom(source_url)
        except HeadlessBrowserCaptureError as exc:
            http_elapsed = perf_counter() - started
            if exc.code not in HTTP_BROWSER_FALLBACK_CODES:
                exc.acquisition_route = {
                    "selected_method": "none", "http_attempted": 1,
                    "http_result": exc.code, "chrome_attempted": 0,
                    "chrome_result": "not_attempted",
                }
                exc.stage_timings_ms = {"http_capture": round(http_elapsed * 1000, 3)}
                raise
            http_failure_code = exc.code
            started = perf_counter()
            try:
                browser_capture = capture_rendered_dom(
                    source_url, max_attempts=max_browser_attempts
                )
            except Exception as browser_exc:
                chrome_elapsed = perf_counter() - started
                browser_code = str(getattr(browser_exc, "code", "") or "browser_failed")
                browser_exc.acquisition_route = {
                    "selected_method": "none", "http_attempted": 1,
                    "http_result": http_failure_code, "chrome_attempted": 1,
                    "chrome_result": browser_code,
                }
                browser_exc.stage_timings_ms = {
                    "http_capture": round(http_elapsed * 1000, 3),
                    "chrome_capture": round(chrome_elapsed * 1000, 3),
                }
                raise
            captured = HeadlessBrowserCapture(
                dom=browser_capture.dom,
                metadata={
                    **browser_capture.metadata,
                    "http_attempt_count": 1,
                    "http_failure_code": http_failure_code,
                    "target_site_browser_navigation_count": 1,
                    "acquisition_route": {
                        "selected_method": "chrome", "http_attempted": 1,
                        "http_result": http_failure_code, "chrome_attempted": 1,
                        "chrome_result": "success",
                    },
                },
            )
            chrome_elapsed = perf_counter() - started
        else:
            http_elapsed = perf_counter() - started
            chrome_elapsed = 0.0
    else:
        started = perf_counter()
        captured = capture_rendered_dom(source_url, max_attempts=max_browser_attempts)
        chrome_elapsed = perf_counter() - started
    started = perf_counter()
    try:
        region_payload = json.loads(captured.dom)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("notice_content_dom_invalid") from exc
    if not isinstance(region_payload, dict):
        raise ValueError("notice_content_dom_invalid")
    http_selected = str(captured.metadata.get("capture_mode") or "").startswith("http_html_")
    capture_method = (
        "http_html_current_playwright_region_script"
        if http_selected
        else "playwright_system_chrome_notice_content_dom"
    )
    source = capture_browser_structured_region(
        source_url=source_url,
        region_payload=region_payload,
        capture_root=capture_root,
        acquisition_method=capture_method,
        capture_method=capture_method,
        submission_method=submission_method,
        source_origin=source_origin,
        capture_metadata=captured.metadata,
    )
    persist_elapsed = perf_counter() - started
    result = acquire_url_source(source, paths, capture_root=capture_root)
    if automatic_public_url:
        record_processing_timing(result, "http_capture", http_elapsed)
    if chrome_elapsed:
        record_processing_timing(result, "chrome_capture", chrome_elapsed)
    record_processing_timing(result, "rendered_dom_persist", persist_elapsed)
    return result, source


def record_processing_timing(result: dict[str, Any], stage: str, elapsed_seconds: float) -> None:
    processing = result.get("processing")
    if not isinstance(processing, dict):
        return
    timings = processing.setdefault("stage_timings_ms", {})
    if isinstance(timings, dict):
        timings[stage] = round(elapsed_seconds * 1000, 3)


def acquire_url_source(
    source: DocumentSource,
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
    *,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Classify and process a captured URL source without changing its URL identity."""
    return acquire_notice_content_source(source, paths, capture_root=capture_root)


def acquire_notice_content_source(
    source: DocumentSource,
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
    *,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Active URL mainline: selected structured DOM -> one semantic call -> readiness."""
    structured_dom = source.metadata.get("notice_content_dom")
    if not isinstance(structured_dom, dict) or not structured_dom:
        raise ValueError("notice_content_dom_missing: page semantic extraction was not invoked")
    normalized = normalize_document(
        {
            "document_id": source.source_id,
            "source_name": source.title,
            "source_url": source.source_url,
            "title": source.title,
            "text": "",
            "tables": [],
        },
        source_type="url",
        source_url=source.source_url,
        title=source.title,
        metadata={
            "capture_time": source.capture_time,
            "notice_content_dom": structured_dom,
        },
    )
    semantic_fields, semantic_audit = invoke_semantic_ai_first(normalized)
    if (
        str(semantic_audit.get("status") or "") == "unavailable"
        and str(semantic_audit.get("failure_reason") or "") == "model_not_configured"
    ):
        raise RuntimeError(
            "semantic_model_not_configured: 请配置 BIDDING_ASSET_AI_SEMANTIC_MODEL 后重新采集。"
        )
    fields = {
        key: str(semantic_fields.get(key) or "").strip()
        for key in (
            "project_name", "customer", "project_number", "content", "budget",
            "bid_open_time", "winner", "award_amount",
        )
    }
    packages = [
        dict(item) for item in semantic_fields.get("award_details") or []
        if isinstance(item, dict)
    ]
    award_details = semantic_packages_to_award_details(
        source, fields.get("project_number", ""), packages
    )
    if len(award_details) > 1:
        fields["winner"] = "；".join(
            str(detail.get("winner") or "") for detail in award_details
        )
        fields["award_amount"] = "；".join(
            str(detail.get("award_amount") or "") for detail in award_details
        )
    elif award_details:
        first_award = award_details[0]
        fields["winner"] = fields["winner"] or str(first_award.get("winner") or "")
        fields["award_amount"] = fields["award_amount"] or str(first_award.get("award_amount") or "")
    _normalize_award_fields(fields, award_details)
    readiness = notice_business_readiness(
        fields,
        notice_title=source.title,
        doc_type=str(semantic_fields.get("doc_type") or ""),
        award_details=award_details,
    )
    fields = _exclude_unverified_optional_fields(fields, readiness)
    attachments = notice_attachment_summary(source, readiness["missing_fields"])
    row = url_candidate_row(source, fields)
    row["source_trace"] = {
        "semantic": semantic_audit,
        "award_details": award_details,
        "confirmation_eligibility": readiness["status"],
        "block_reason": readiness["block_reason"],
        "attachments": attachments,
        "field_completeness": readiness,
    }
    result = publish_candidates([row], "url_acquisition", paths)
    downstream = dict(result.get("downstream_refresh") or {})
    downstream_failed = downstream.get("status") == "failed"
    eligibility = "blocked" if downstream_failed else readiness["status"]
    block_reason = (
        f"网页语义提取成功，但后处理阶段 {downstream.get('failed_stage') or 'unknown'} 失败。"
        if downstream_failed else readiness["block_reason"]
    )
    result["sources"] = [document_source_view(source)]
    source_access = dict(source.metadata.get("access") or {})
    source_provenance = dict(source.metadata.get("provenance") or {})
    browser_capture_metadata = dict(source_provenance.get("browser_capture") or {})
    acquisition_route = dict(browser_capture_metadata.get("acquisition_route") or {})
    if source_access.get("acquisition_method"):
        source_provenance["acquisition_method"] = str(
            source_access["acquisition_method"]
        )
    result["processing"] = {
        **fields,
        "mainline": "notice_content_dom_qwen/v1",
        "file_name": "",
        "file_type": ".html",
        "source_id": source.source_id,
        "source_path": source.source_url,
        "source_url": source.source_url,
        "source_title": source.title,
        "access_status": str(source_access.get("status") or "success"),
        "access": source_access,
        "provenance": source_provenance,
        "acquisition_route": acquisition_route,
        "content_status": "content_ready",
        "parse_status": "success",
        "parse_error": "",
        "extract_status": extracted_fields_status(fields),
        "confirmation_eligibility": eligibility,
        "block_reason": block_reason,
        "next_action": "retry_downstream_refresh" if downstream_failed else (
            "parse_attachments" if attachments["requires_explicit_parse"] else
            ("none" if readiness["status"] == "eligible" else "manual_review")
        ),
        "technical_capture_status": "success",
        "post_processing_status": "failed" if downstream_failed else "success",
        "downstream_refresh": downstream,
        "pre_refresh_confirmation_state": {
            "confirmation_eligibility": readiness["status"],
            "block_reason": readiness["block_reason"],
        },
        "adapter": "notice_content_dom",
        "semantic": semantic_audit,
        "award_details": award_details,
        "attachments": attachments,
        "field_completeness": readiness,
        "stage_counters": {
            "page_ai_calls": int(bool(semantic_audit.get("invoked"))),
            "attachment_downloads": 0,
            "attachment_parses": 0,
            "attachment_ai_calls": 0,
        },
        "ai": {
            "invoked": bool(semantic_audit.get("invoked")),
            "status": str(semantic_audit.get("status") or "unknown"),
            "skip_reason": str(semantic_audit.get("failure_reason") or ""),
        },
    }
    return result


def _is_result_notice(*, notice_title: str = "", doc_type: str = "") -> bool:
    marker = re.sub(r"[\W_]+", "", f"{doc_type}{notice_title}", flags=re.UNICODE)
    if "候选" in marker:
        return False
    return bool(re.search(
        r"(?:中标|成交)(?:结果)?(?:公告|公示)|采购结果(?:公告|公示)|(?:中标|成交|采购)结果",
        marker,
    ))


def _is_not_applicable(value: Any) -> bool:
    marker = re.sub(r"[\W_]+", "", str(value or "").strip().lower(), flags=re.UNICODE)
    return marker in {"notapplicable", "na", "不适用", "不涉及", "无"}


_BASE_CONFIRMATION_FIELDS = frozenset({"project_name", "customer"})
_OPTIONAL_CONFIRMATION_FIELDS = frozenset({
    "project_number", "content", "budget", "bid_open_time", "winner", "award_amount",
})


def _valid_award_pair(winner: Any, amount: Any) -> bool:
    winner_text = str(winner or "").strip()
    amount_text = str(amount or "").strip()
    return bool(winner_text and amount_text and re.search(r"\d", amount_text))


def _normalize_award_fields(
    fields: dict[str, Any], award_details: list[dict[str, Any]] | None = None,
) -> None:
    if _valid_award_pair(fields.get("winner"), fields.get("award_amount")):
        return
    valid_details = [
        detail for detail in award_details or []
        if isinstance(detail, dict)
        and _valid_award_pair(detail.get("winner"), detail.get("award_amount"))
    ]
    if valid_details:
        fields["winner"] = "；".join(str(detail.get("winner") or "").strip() for detail in valid_details)
        fields["award_amount"] = "；".join(str(detail.get("award_amount") or "").strip() for detail in valid_details)
        return
    fields["winner"] = ""
    fields["award_amount"] = ""


def _exclude_unverified_optional_fields(
    fields: dict[str, Any], readiness: dict[str, Any],
) -> dict[str, Any]:
    sanitized = dict(fields)
    unverified_values: dict[str, str] = {}
    for field in readiness.get("optional_unverified_fields") or []:
        if field not in _OPTIONAL_CONFIRMATION_FIELDS:
            continue
        value = str(sanitized.get(field) or "").strip()
        if value:
            unverified_values[field] = value
            sanitized[field] = ""
    if unverified_values:
        readiness["unverified_values"] = unverified_values
    return sanitized


def notice_business_readiness(
    fields: dict[str, Any],
    *,
    notice_title: str = "",
    doc_type: str = "",
    result_notice: bool | None = None,
    award_details: list[dict[str, Any]] | None = None,
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical active-mainline readiness calculation; it never changes AI values."""
    business_fields = (
        "project_name", "project_number", "customer", "content", "budget",
        "bid_open_time", "winner", "award_amount",
    )
    missing = [
        field for field in business_fields
        if not str(fields.get(field) or "").strip()
        and not _is_not_applicable(fields.get(field))
    ]
    is_result = (
        _is_result_notice(notice_title=notice_title, doc_type=doc_type)
        or bool(award_details)
    ) if result_notice is None else bool(result_notice)
    required_fields = ("project_name", "customer")
    required_missing = [field for field in required_fields if field in missing]
    integrity_payload = integrity if isinstance(integrity, dict) else {}
    suspect_fields = sorted({
        str(field)
        for field in integrity_payload.get("suspect_fields") or []
        if str(field)
    })
    quality_issues = [
        dict(issue) for issue in integrity_payload.get("quality_issues") or []
        if isinstance(issue, dict)
    ]
    integrity_blocked_fields = set(
        field for field in suspect_fields if field in _BASE_CONFIRMATION_FIELDS
    )
    for issue in quality_issues:
        field = str(issue.get("field") or "")
        code = str(issue.get("code") or "").strip().lower()
        if (
            field in _BASE_CONFIRMATION_FIELDS
            and not (_is_not_applicable(fields.get(field)) and "missing" in code)
        ):
            integrity_blocked_fields.add(field)
    critical_suspect = sorted(integrity_blocked_fields)
    blocked_for_integrity = bool(integrity_blocked_fields)
    recorded_optional_fields = {
        str(field)
        for field in integrity_payload.get("optional_unverified_fields") or []
        if str(field)
    }
    quality_issue_fields = {
        str(issue.get("field") or "")
        for issue in quality_issues
        if str(issue.get("field") or "")
        and (
            str(fields.get(str(issue.get("field") or "")) or "").strip()
            or "missing" not in str(issue.get("code") or "").lower()
        )
    }
    optional_unverified_fields = sorted(
        (set(suspect_fields) | quality_issue_fields | recorded_optional_fields)
        & _OPTIONAL_CONFIRMATION_FIELDS
    )
    block_reasons: list[str] = []
    if required_missing:
        block_reasons.append("缺少人工确认所需字段：" + "、".join(required_missing))
    if blocked_for_integrity:
        block_reasons.append("字段证据/完整性异常：" + "、".join(critical_suspect))
    return {
        "status": "blocked" if required_missing or blocked_for_integrity else "eligible",
        "eligible": not required_missing and not blocked_for_integrity,
        "critical_fields_complete": not required_missing and not blocked_for_integrity,
        "missing_fields": missing,
        "required_missing_fields": required_missing,
        "suspect_fields": suspect_fields,
        "quality_issues": quality_issues,
        "integrity_blocked_fields": critical_suspect,
        "optional_unverified_fields": optional_unverified_fields,
        "result_notice": is_result,
        "block_reason": "；".join(block_reasons),
    }


def notice_attachment_summary(source: DocumentSource, missing_fields: list[str]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in source.attachments:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        raw_href = str(item.get("raw_href") or url)
        if not raw_href:
            continue
        declared_values = (
            str(item.get("filename") or ""),
            str(item.get("download") or ""),
            Path(urlsplit(url).path).name,
            Path(urlsplit(raw_href).path).name,
        )
        suffixes = [
            Path(value.split("?", 1)[0]).suffix.lower()
            for value in declared_values if value
        ]
        file_suffix = next(
            (suffix for suffix in suffixes if suffix in VISIBLE_ATTACHMENT_SUFFIXES), ""
        )
        if not str(item.get("download") or "") and not file_suffix:
            continue
        downloadable = bool(item.get("downloadable")) if "downloadable" in item else url.startswith(("http://", "https://"))
        parser_pending = downloadable and file_suffix not in ARCHIVE_ATTACHMENT_SUFFIXES
        items.append({
            "filename": str(item.get("filename") or ""),
            "url": url,
            "raw_href": raw_href,
            "download": str(item.get("download") or ""),
            "downloadable": downloadable,
            "download_status": (
                "not_requested" if parser_pending else
                ("not_applicable_archive" if file_suffix in ARCHIVE_ATTACHMENT_SUFFIXES else "unavailable_non_executable")
            ),
            "parse_status": "not_run",
            "ai_status": "not_run",
        })
    pending_count = sum(row["download_status"] == "not_requested" for row in items)
    return {
        "parent_source_id": source.source_id,
        "parent_source_url": source.source_url,
        "total_count": len(items),
        "success_count": 0,
        "failed_count": 0,
        "pending_count": pending_count,
        "retryable_count": 0,
        "has_failure": False,
        "has_blocking_failure": False,
        "processing_state": "not_requested" if items else "processed",
        "requires_explicit_parse": bool(pending_count and missing_fields),
        "missing_fields": list(missing_fields),
        "items": items,
        "supporting_sources": [],
    }


def semantic_packages_to_award_details(
    source: DocumentSource,
    project_number: str,
    packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for package_index, package in enumerate(packages):
        package_identifier = str(package.get("package_identifier") or "").strip()
        package_name = str(package.get("package_name") or "").strip()
        for award_index, award in enumerate(package.get("awards") or []):
            if not isinstance(award, dict):
                continue
            winner = str(award.get("winner") or "").strip()
            amount = str(award.get("award_amount") or "").strip()
            if not _valid_award_pair(winner, amount):
                continue
            identity = json.dumps(
                [source.source_url, project_number, package_identifier, package_name, winner, amount],
                ensure_ascii=False,
            )
            details.append({
                "award_detail_id": "award_detail_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
                "winner": winner,
                "award_amount": amount,
                "package_detail": {
                    **({"package_number": package_identifier} if package_identifier else {}),
                    **({"package_name": package_name} if package_name else {}),
                },
                "source_type": "url",
                "source_trace": {
                    "source_url": source.source_url,
                    "source_id": source.source_id,
                    "package_index": package_index,
                    "award_index": award_index,
                },
            })
    return details




def acquire_notice_content_attachments(
    source: DocumentSource,
    existing_processing: dict[str, Any],
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
    *,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Explicit attachment AI completion; every retained link is considered equally."""
    prior = copy.deepcopy(existing_processing)
    prior_summary = dict(prior.get("attachments") or {})
    items = [row for row in prior_summary.get("items") or [] if isinstance(row, dict)]
    selected_urls = {
        str(row.get("url") or "") for row in items
        if str(row.get("url") or "")
        and bool(row.get("downloadable", True))
        and (row.get("download_status") in {"not_requested", "failed"} or row.get("parse_status") == "failed")
    }
    if not selected_urls:
        prior_summary.update(processing_state="processed", requires_explicit_parse=False)
        prior["attachments"] = prior_summary
        return {
            "processing": prior,
            "candidates": [],
            "status": "attachment_second_pass_already_complete",
            "idempotent": True,
        }
    bundle = prepare_notice_attachment_bundle(
        source,
        capture_root=capture_root,
        selected_attachment_urls=selected_urls,
    )
    summary = merge_attachment_progress(prior_summary, bundle.attachment_summary, selected_urls)
    fields = {
        key: str(prior.get(key) or "").strip()
        for key in (
            "project_name", "customer", "project_number", "content", "budget",
            "bid_open_time", "winner", "award_amount",
        )
    }
    semantic = copy.deepcopy(dict(prior.get("semantic") or {}))
    attachment_audits: list[dict[str, Any]] = []
    attachment_award_details: list[dict[str, Any]] = []
    attachment_doc_type = ""
    for document in bundle.attachment_documents:
        normalized = normalize_document(
            document,
            source_type="attachment",
            source_url=source.source_url,
            metadata={"notice_content_dom": parsed_attachment_structured_dom(document)},
        )
        extracted, audit = invoke_semantic_ai_first(normalized)
        attachment_audits.append(audit)
        attachment_doc_type = attachment_doc_type or str(extracted.get("doc_type") or "").strip()
        for field_name in fields:
            if not fields[field_name] and str(extracted.get(field_name) or "").strip():
                fields[field_name] = str(extracted[field_name]).strip()
        packages = [
            dict(item) for item in extracted.get("award_details") or [] if isinstance(item, dict)
        ]
        attachment_award_details.extend(
            semantic_packages_to_award_details(source, fields.get("project_number", ""), packages)
        )
    merged_details = merge_attachment_award_details(
        prior.get("award_details") or [], attachment_award_details
    )
    prior_completeness = prior.get("field_completeness")
    prior_result_notice = (
        bool(prior_completeness.get("result_notice"))
        if isinstance(prior_completeness, dict) else None
    )
    _normalize_award_fields(fields, merged_details)
    readiness = notice_business_readiness(
        fields,
        notice_title=source.title,
        doc_type=attachment_doc_type,
        result_notice=prior_result_notice,
    )
    fields = _exclude_unverified_optional_fields(fields, readiness)
    summary["requires_explicit_parse"] = False
    summary["processing_state"] = "processed"
    semantic["attachment_passes"] = attachment_audits
    semantic["attachment_ai_calls"] = sum(bool(row.get("invoked")) for row in attachment_audits)
    updated = {
        **prior,
        **fields,
        "award_details": merged_details,
        "semantic": semantic,
        "attachments": summary,
        "field_completeness": readiness,
        "confirmation_eligibility": readiness["status"],
        "block_reason": readiness["block_reason"],
        "next_action": "none" if readiness["status"] == "eligible" else "manual_review",
        "extract_status": extracted_fields_status(fields),
    }
    counters = dict(updated.get("stage_counters") or {})
    counters["attachment_downloads"] = len(selected_urls)
    counters["attachment_parses"] = len(bundle.attachment_documents)
    counters["attachment_ai_calls"] = semantic["attachment_ai_calls"]
    updated["stage_counters"] = counters
    row = url_candidate_row(source, fields)
    row["source_trace"] = {
        "semantic": semantic,
        "award_details": merged_details,
        "confirmation_eligibility": readiness["status"],
        "block_reason": readiness["block_reason"],
        "attachments": summary,
        "field_completeness": readiness,
    }
    result = publish_candidates([row], "url_acquisition", paths)
    result["processing"] = updated
    result["sources"] = [document_source_view(source)]
    return result


def parsed_attachment_structured_dom(document: Any) -> dict[str, Any]:
    """Represent an already parsed attachment for the same semantic business contract."""
    children: list[dict[str, Any]] = []
    text_value = str(getattr(document, "text", "") or "").strip()
    if text_value:
        children.append({"type": "element", "tag": "p", "children": [{"type": "text", "text": text_value}]})
    for table in getattr(document, "tables", ()) or ():
        rows: list[dict[str, Any]] = []
        for row in table:
            rows.append({
                "type": "element",
                "tag": "tr",
                "children": [
                    {"type": "element", "tag": "td", "children": [{"type": "text", "text": str(cell or "")}]}
                    for cell in row
                ],
            })
        children.append({"type": "element", "tag": "table", "children": rows})
    return {"type": "element", "tag": "document", "children": children}


def attachment_suffix_from_content(content: bytes) -> str:
    """Identify parser-supported attachment types without relying on URL names."""
    if content.startswith(b"%PDF-"):
        return ".pdf"
    if content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return ".doc"
    if content.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
        except (BadZipFile, OSError):
            return ""
        if any(name.startswith("word/") for name in names):
            return ".docx"
        if any(name.startswith("xl/") for name in names):
            return ".xlsx"
    return ""


def download_attachment_records(
    document: DocumentSource,
    *,
    capture_root: Path | None,
    max_attachments: int,
) -> list[AttachmentDownloadRecord]:
    """Download explicit attachments into the isolated capture tree.

    The selected notice-content DOM is the only source of candidate URLs. URL
    suffixes are optional; supported file types can be identified from bytes.
    """
    output_dir = (
        Path(capture_root) if capture_root is not None else Path("data/web_capture")
    ) / document.source_id / "attachments"
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[AttachmentDownloadRecord] = []
    for index, attachment in enumerate(document.attachments):
        url = str(attachment.get("url") or "").strip()
        filename = safe_filename(str(attachment.get("filename") or ""), url)
        declared_suffix = suffix_for(filename, url)
        if index >= max(0, max_attachments):
            records.append(AttachmentDownloadRecord(
                filename=filename,
                url=url,
                local_path="",
                status="skipped",
                error_type="AttachmentLimitExceeded",
                error_message=f"Attachment skipped because the limit is {max_attachments}",
            ))
            continue
        if declared_suffix and declared_suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
            records.append(AttachmentDownloadRecord(
                filename=filename,
                url=url,
                local_path="",
                status="skipped",
                error_type="UnsupportedFileType",
                error_message=f"Unsupported attachment suffix: {declared_suffix}",
            ))
            continue
        try:
            validate_automatic_capture_url(url)
            content = fetch_attachment_bytes(url)
            detected_suffix = attachment_suffix_from_content(content)
            suffix = declared_suffix or detected_suffix
            if suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
                raise ValueError("Attachment content type is not parser-supported")
            if not declared_suffix:
                filename = f"{filename}{suffix}"
            local_path = unique_path(output_dir, filename)
            local_path.write_bytes(content)
        except Exception as exc:
            records.append(AttachmentDownloadRecord(
                filename=filename,
                url=url,
                local_path="",
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            ))
            continue
        records.append(AttachmentDownloadRecord(
            filename=filename,
            url=url,
            local_path=str(local_path),
            status="downloaded",
        ))
    (output_dir / "download_summary.json").write_text(
        json.dumps({
            "source_id": document.source_id,
            "total_attachments": len(records),
            "downloaded_count": sum(row.status == "downloaded" for row in records),
            "failed_count": sum(row.status == "failed" for row in records),
            "skipped_count": sum(row.status == "skipped" for row in records),
            "records": [asdict(row) for row in records],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return records


def prepare_notice_attachment_bundle(
    source: DocumentSource,
    *,
    capture_root: Path | None,
    selected_attachment_urls: set[str],
) -> NoticeAttachmentBundle:
    """Download and parse explicit attachments without page-quality or rule-semantic gates."""
    selected = [
        dict(item) for item in source.attachments
        if str(item.get("url") or "") in selected_attachment_urls
    ]
    attachment_source = DocumentSource(
        source_id=source.source_id,
        source_type=source.source_type,
        source_url=source.source_url,
        capture_time=source.capture_time,
        title=source.title,
        html_content="",
        text_content="",
        attachments=selected,
        metadata=dict(source.metadata),
    )
    records = download_attachment_records(
        attachment_source,
        capture_root=capture_root,
        max_attachments=max(len(selected), 1),
    ) if selected else []
    documents: list[Any] = []
    rows: list[dict[str, Any]] = []
    for record in records:
        row = {
            **asdict(record),
            "download_status": record.status,
            "parse_status": "not_run",
            "parse_error": "",
            "ai_status": "not_run",
        }
        if record.status == "downloaded":
            try:
                parsed = tender_parser.parse_document(Path(record.local_path), Path(record.local_path).parent)
                row["parse_status"] = parsed.parse_status
                row["parse_error"] = parsed.parse_error
                row["document_id"] = parsed.document_id
                row["sha256"] = parsed.file_hash or str(getattr(record, "sha256", "") or "")
                if parsed.parse_status == "success":
                    documents.append(parsed)
            except Exception as exc:
                row["parse_status"] = "failed"
                row["parse_error"] = str(exc)
        rows.append(row)
    return NoticeAttachmentBundle(
        attachment_documents=documents,
        attachment_summary={
            "items": rows,
            "success_count": sum(row.get("parse_status") == "success" for row in rows),
            "failed_count": sum(
                row.get("download_status") == "failed" or row.get("parse_status") == "failed"
                for row in rows
            ),
            "supporting_sources": [],
        },
    )


def merge_attachment_award_details(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    """Attachments may fill explicit empty page award slots, never create page conflicts."""
    merged = [
        copy.deepcopy(item) for item in existing
        if isinstance(item, dict)
        and _valid_award_pair(item.get("winner"), item.get("award_amount"))
    ] if isinstance(existing, list) else []
    additions = [
        copy.deepcopy(item) for item in incoming
        if isinstance(item, dict)
        and _valid_award_pair(item.get("winner"), item.get("award_amount"))
    ] if isinstance(incoming, list) else []
    if not merged:
        return additions
    for incoming_detail in additions:
        incoming_package = dict(incoming_detail.get("package_detail") or {})
        matching = [
            detail for detail in merged
            if dict(detail.get("package_detail") or {}) == incoming_package
        ]
        for detail in matching:
            if not str(detail.get("winner") or "").strip() and str(incoming_detail.get("winner") or "").strip():
                detail["winner"] = incoming_detail["winner"]
            if not str(detail.get("award_amount") or "").strip() and str(incoming_detail.get("award_amount") or "").strip():
                detail["award_amount"] = incoming_detail["award_amount"]
            if str(detail.get("winner") or "").strip() and str(detail.get("award_amount") or "").strip():
                break
    return merged


def merge_attachment_progress(
    prior_summary: dict[str, Any], current_summary: dict[str, Any], selected_urls: set[str]
) -> dict[str, Any]:
    """Merge one explicit attempt without erasing pending, successful, or failed history."""
    prior_rows = {
        str(row.get("url") or ""): copy.deepcopy(row)
        for row in prior_summary.get("items") or []
        if isinstance(row, dict) and str(row.get("url") or "")
    }
    current_rows = {
        str(row.get("url") or ""): copy.deepcopy(row)
        for row in current_summary.get("items") or []
        if isinstance(row, dict) and str(row.get("url") or "")
    }
    merged_rows: list[dict[str, Any]] = []
    for url, prior_row in prior_rows.items():
        if url in selected_urls:
            updated = current_rows.get(url, prior_row)
            updated["attempt_count"] = int(prior_row.get("attempt_count") or 0) + 1
            merged_rows.append(updated)
        else:
            merged_rows.append(prior_row)
    for url, current_row in current_rows.items():
        if url not in prior_rows:
            current_row["attempt_count"] = 1 if url in selected_urls else 0
            merged_rows.append(current_row)
    merged = {**copy.deepcopy(prior_summary), **{
        key: copy.deepcopy(value) for key, value in current_summary.items() if key != "items"
    }}
    merged["items"] = merged_rows
    refresh_attachment_progress(merged)
    return merged


def refresh_attachment_progress(summary: dict[str, Any]) -> None:
    rows = [row for row in summary.get("items") or [] if isinstance(row, dict)]
    failures = [
        row for row in rows
        if row.get("download_status") == "failed" or row.get("parse_status") == "failed"
    ]
    successful = [row for row in rows if row.get("parse_status") == "success"]
    summary.update({
        "total_count": len(rows),
        "success_count": len(successful),
        "failed_count": len(failures),
        "skipped_count": sum(
            1 for row in rows
            if str(row.get("download_status") or "").startswith(("skipped", "deferred"))
        ),
        "pending_count": sum(1 for row in rows if row.get("download_status") == "not_requested"),
        "retryable_count": len(failures),
        "has_failure": bool(failures),
        "has_blocking_failure": bool(failures),
        "supporting_sources": [dict(row.get("provenance") or {}) for row in successful],
    })


def _file_trace(source_path: Path) -> dict[str, Any]:
    try:
        size_bytes = source_path.stat().st_size
    except OSError:
        size_bytes = 0
    trace = new_stage_trace(
        source_type="file_upload",
        source_name=source_path.name,
        file_type=source_path.suffix.lower(),
        size_bytes=size_bytes,
    )
    append_stage(trace, "acquisition", "success", route="file_upload", counts={"attempts": 1})
    return trace


def _safe_duration_ms(value: Any) -> float:
    try:
        duration = float(value)
    except Exception:
        return 0.0
    return duration if math.isfinite(duration) and duration >= 0 else 0.0


def _semantic_call_count(audit: Any) -> int:
    if not isinstance(audit, dict):
        return 0
    count = safe_counts({"call_count": audit.get("call_count")}).get("call_count")
    return int(count) if count is not None else int(bool(audit.get("invoked")))


def _parser_audit(document: Any) -> Mapping[str, Any]:
    try:
        audit = getattr(document, "parser_audit", {})
    except Exception:
        return {}
    return audit if isinstance(audit, Mapping) else {}


def _audit_value(audit: Mapping[str, Any], key: str) -> Any:
    try:
        return audit.get(key)
    except Exception:
        return None


def _safe_parser_counts(audit: Mapping[str, Any]) -> dict[str, int | float]:
    counts: dict[str, int | float] = {}
    for output_name, audit_name in (("elements", "element_count"), ("source_characters", "source_characters")):
        try:
            counts.update(safe_counts({output_name: _audit_value(audit, audit_name)}))
        except Exception:
            continue
    return counts


def _file_parser_route(document: Any) -> str:
    audit = _parser_audit(document)
    try:
        detected_type = str(_audit_value(audit, "detected_type") or "")
    except Exception:
        detected_type = ""
    if detected_type == "pdf" and _audit_value(audit, "ocr_status") == "success":
        return "ocrmypdf_pdfminer_unstructured_text"
    return {
        "pdf": "pdfminer_unstructured_text",
        "docx": "unstructured_docx",
        "doc": "libreoffice_unstructured_docx",
        "zip": "zip_bounded",
    }.get(detected_type, "document_detector")


def _record_file_parser_trace(trace: dict[str, Any], document: Any, elapsed_seconds: float) -> None:
    source = trace.setdefault("source", {})
    source["sha256"] = str(getattr(document, "file_hash", "") or "")
    source["size_bytes"] = max(int(getattr(document, "file_size", 0) or 0), 0)
    audit = _parser_audit(document)
    detected_type = str(_audit_value(audit, "detected_type") or "")
    if detected_type == "pdf" or str(getattr(document, "file_type", "")) == ".pdf":
        ocr_status = str(_audit_value(audit, "ocr_status") or "")
        ocr_error_code = str(_audit_value(audit, "ocr_error_code") or "")
        ocr_duration = _safe_duration_ms(_audit_value(audit, "ocr_duration_ms"))
        if ocr_status == "success":
            append_stage(trace, "ocr", "success", duration_ms=ocr_duration, route="ocrmypdf")
        elif ocr_status == "failed":
            error_code = ocr_error_code if ocr_error_code.startswith("ocr.") else "ocr.execution_failed"
            append_stage(trace, "ocr", "failed", error_code=error_code, error=error_code, duration_ms=ocr_duration, route="ocrmypdf")
        elif ocr_error_code == "ocr.not_needed":
            append_stage(trace, "ocr", "skipped", error_code="ocr.not_needed", route="pdfminer_text_layer")
        else:
            # Preserve the P0-1A compatibility result for synthetic/legacy
            # ParsedDocument objects that predate the OCR audit fields.
            append_stage(trace, "ocr", "skipped", error_code="ocr.not_enabled", route="none")
    else:
        append_stage(trace, "ocr", "skipped", error_code="ocr.not_applicable", route="none")
    counts = _safe_parser_counts(audit)
    status = str(getattr(document, "parse_status", "") or "")
    if status == "success":
        append_stage(trace, "parse", "success", duration_ms=_safe_duration_ms(elapsed_seconds * 1000), counts=counts, route=_file_parser_route(document))
    else:
        parse_error = str(getattr(document, "parse_error", "") or "")
        stable_error_code = parse_error if parse_error in {
            "pdf.password_required",
            "libreoffice_doc_conversion_timeout",
        } else ""
        append_stage(
            trace,
            "parse",
            "failed",
            error=parse_error,
            error_code=stable_error_code,
            duration_ms=_safe_duration_ms(elapsed_seconds * 1000),
            counts=counts,
            route=_file_parser_route(document),
        )


def _file_processing_failure(
    source_path: Path,
    trace: dict[str, Any],
    *,
    document: Any | None = None,
    parse_elapsed: float = 0.0,
    parser: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    semantic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return attach_stage_trace(
        {
            "mainline": "unstructured_document_qwen/v1",
            "parse_status": str(getattr(document, "parse_status", "success") if document is not None else "failed"),
            "parse_error": str(getattr(document, "parse_error", "") if document is not None else ""),
            "file_name": source_path.name,
            "file_type": source_path.suffix.lower(),
            "file_hash": str(getattr(document, "file_hash", "") if document is not None else ""),
            "parser": dict(parser or {}),
            "evidence": dict(evidence or {}),
            "semantic": dict(semantic or {"invoked": False, "status": "not_run"}),
            "stage_counters": {
                "document_ai_calls": _semantic_call_count(semantic),
            },
            "stage_timings_ms": {
                "partition": round(parse_elapsed * 1000, 3),
                **dict((semantic or {}).get("stage_timings_ms") or {}),
            },
        },
        trace,
    )


def _attach_file_failure(exc: Exception, processing: dict[str, Any]) -> Exception:
    exc.processing_result = processing  # type: ignore[attr-defined]
    return exc


def acquire_file(upload_path: Path, paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths()) -> dict[str, Any]:
    """Partition one upload and run the bounded semantic-business mainline once."""
    source_path = Path(upload_path)
    if source_path.suffix.lower() == ".xlsx":
        return acquire_xlsx_file(source_path, paths)
    trace = _file_trace(source_path)
    try:
        if source_path.suffix.lower() == ".xls":
            raise ValueError("当前支持 XLSX，不支持旧版 XLS。")
        if source_path.suffix.lower() not in tender_parser.SUPPORTED_EXTENSIONS:
            allowed = ", ".join(sorted(tender_parser.SUPPORTED_EXTENSIONS))
            raise ValueError(f"Unsupported upload type: {source_path.suffix}. Allowed: {allowed}")
        parse_started = perf_counter()
        document = tender_parser.parse_document(source_path, source_path.parent)
        parse_elapsed = perf_counter() - parse_started
        _record_file_parser_trace(trace, document, parse_elapsed)
        if document.parse_status != "success":
            error = RuntimeError(f"document_partition_failed:{document.parse_error or 'unknown'}")
            raise _attach_file_failure(error, _file_processing_failure(
                source_path, trace, document=document, parse_elapsed=parse_elapsed,
                parser=dict(document.parser_audit),
            ))
    except Exception as exc:
        if not hasattr(exc, "processing_result"):
            append_stage(trace, "parse", "failed", error=exc, error_code=map_error_code("parse", exc), route="file_upload")
            raise _attach_file_failure(exc, _file_processing_failure(source_path, trace))
        raise

    evidence_started = perf_counter()
    try:
        evidence = build_document_evidence(document)
    except Exception as exc:
        append_stage(trace, "evidence", "failed", error=exc, duration_ms=(perf_counter() - evidence_started) * 1000, route="document_evidence")
        raise _attach_file_failure(exc, _file_processing_failure(
            source_path, trace, document=document, parse_elapsed=parse_elapsed,
            parser=dict(document.parser_audit), evidence={"status": "failed", "error_code": map_error_code("evidence", exc)},
        ))
    append_stage(trace, "evidence", "success", duration_ms=_safe_duration_ms((perf_counter() - evidence_started) * 1000), counts=safe_counts({
        "source_sections": evidence.audit.get("source_section_count"),
        "selected_sections": evidence.audit.get("selected_section_count"),
        "selected_tables": evidence.audit.get("selected_table_count"),
        "payload_bytes": evidence.audit.get("payload_bytes"),
    }), route="document_evidence")
    normalized = normalize_document(
        document,
        source_type="file",
        title=document.source_name,
        metadata={"notice_content_dom": evidence.structured_dom},
    )
    try:
        semantic_fields, semantic_audit = invoke_semantic_ai_first(
            normalized,
            prompt_path=DOCUMENT_BUSINESS_V5_PROMPT_PATH,
            prompt_version=DOCUMENT_BUSINESS_V5_PROMPT_VERSION,
        )
    except Exception as exc:
        semantic_audit = dict(getattr(exc, "semantic_audit", {}) or {})
        if not semantic_audit:
            semantic_audit = {
                "invoked": True,
                "status": "failed",
                "failure_reason": str(getattr(exc, "error_code", "") or type(exc).__name__),
                "call_count": safe_counts({"call_count": getattr(exc, "call_count", 0)}).get("call_count", 0),
                "stage_timings_ms": {},
            }
        semantic_audit.setdefault("status", "failed")
        semantic_audit.setdefault("invoked", True)
        semantic_audit.setdefault("call_count", 0)
        append_stage(
            trace,
            "semantic",
            "failed",
            error=exc,
            counts=safe_counts({"ai_calls": semantic_audit.get("call_count")}),
            route="openrouter.extract_business",
        )
        raise _attach_file_failure(exc, _file_processing_failure(
            source_path, trace, document=document, parse_elapsed=parse_elapsed,
            parser=dict(document.parser_audit), evidence=dict(evidence.audit), semantic=semantic_audit,
        ))
    semantic_status = str(semantic_audit.get("status") or "")
    semantic_duration = _safe_duration_ms(
        dict(semantic_audit.get("stage_timings_ms") or {}).get("ai")
    )
    if semantic_status not in {"available", "abstained"} or not semantic_fields:
        reason = str(semantic_audit.get("failure_reason") or semantic_status or "no_fields")
        append_stage(trace, "semantic", "failed", error=reason, duration_ms=semantic_duration, counts=safe_counts({
            "ai_calls": semantic_audit.get("call_count"),
        }), route="openrouter.extract_business")
        error = RuntimeError("document_semantic_unavailable:" + reason)
        raise _attach_file_failure(error, _file_processing_failure(
            source_path, trace, document=document, parse_elapsed=parse_elapsed,
            parser=dict(document.parser_audit), evidence=dict(evidence.audit), semantic=semantic_audit,
        ))
    append_stage(trace, "semantic", "success", duration_ms=semantic_duration, counts=safe_counts({
        "ai_calls": semantic_audit.get("call_count"),
        "fields": len(semantic_fields),
    }), route="openrouter.extract_business")
    fields = reject_placeholder_fields({
        key: str(semantic_fields.get(key) or "").strip()
        for key in (
            "project_name", "customer", "project_number", "content", "budget",
            "bid_open_time", "winner", "award_amount",
        )
    })
    packages = [
        dict(item) for item in semantic_fields.get("award_details") or []
        if isinstance(item, dict)
    ]
    award_details = document_packages_to_award_details(
        document.document_id, fields.get("project_number", ""), packages
    )
    if len(award_details) > 1:
        fields["winner"] = "；".join(str(row.get("winner") or "") for row in award_details)
        fields["award_amount"] = "；".join(str(row.get("award_amount") or "") for row in award_details)
    elif award_details:
        fields["winner"] = fields["winner"] or str(award_details[0].get("winner") or "")
        fields["award_amount"] = fields["award_amount"] or str(award_details[0].get("award_amount") or "")
    _normalize_award_fields(fields, award_details)
    integrity = dict(semantic_audit.get("field_integrity") or {})
    readiness = notice_business_readiness(
        fields,
        notice_title=document.source_name or source_path.name,
        doc_type=str(semantic_fields.get("doc_type") or ""),
        award_details=award_details,
        integrity=semantic_audit.get("field_integrity"),
    )
    fields = _exclude_unverified_optional_fields(fields, readiness)
    if integrity:
        integrity_status = "blocked" if readiness.get("integrity_blocked_fields") else "success"
        append_stage(trace, "integrity", integrity_status, error=integrity.get("quality_issues") or "field_integrity", counts=safe_counts({
            "suspect_fields": len(integrity.get("suspect_fields") or []),
            "quality_issues": len(integrity.get("quality_issues") or []),
        }), route="document_field_integrity")
    else:
        append_stage(trace, "integrity", "skipped", error_code="integrity.not_applicable", route="none")
    readiness_status = "success" if readiness["eligible"] else "blocked"
    append_stage(trace, "readiness", readiness_status, error=readiness.get("block_reason") or "", counts=safe_counts({
        "missing_fields": len(readiness.get("missing_fields") or []),
        "suspect_fields": len(readiness.get("suspect_fields") or []),
    }), route="notice_business_readiness")
    timestamp = utc_now()
    row = {
        "candidate_id": "asset_candidate_" + hashlib.sha256(
            f"file_upload\n{document.document_id}".encode("utf-8")
        ).hexdigest()[:16],
        "source_title": fields.get("project_name") or document.source_name,
        "source_url": "", "source_file": str(source_path),
        "discovered_time": timestamp, "confidence": 0.0, **fields,
        "doc_type": str(semantic_fields.get("doc_type") or "").strip(),
        "source_trace": {
            "source_id": document.document_id,
            "semantic": semantic_audit, "document_evidence": dict(evidence.audit),
            "field_integrity": integrity,
            "award_details": award_details,
            "confirmation_eligibility": readiness["status"],
            "field_completeness": readiness,
        },
    }
    try:
        result = publish_candidates([row], "file_upload", paths)
    except Exception as exc:
        append_stage(trace, "candidate", "failed", error=exc, route="candidate_publication")
        raise _attach_file_failure(exc, _file_processing_failure(
            source_path, trace, document=document, parse_elapsed=parse_elapsed,
            parser=dict(document.parser_audit), evidence=dict(evidence.audit), semantic=semantic_audit,
        ))
    downstream = dict(result.get("downstream_refresh") or {})
    if downstream.get("status") == "failed":
        failed_stage = str(downstream.get("failed_stage") or "candidate")
        append_stage(trace, "candidate", "failed", error=failed_stage, error_code=map_error_code("candidate", failed_stage), route="candidate_publication", counts=safe_counts({"candidates": 1}))
    else:
        append_stage(trace, "candidate", "success", route="candidate_publication", counts=safe_counts({"candidates": len(result.get("candidates") or [])}))
    result["sources"] = [
        {
            "source_type": "file_upload",
            "source_file": str(source_path),
            "created_time": timestamp,
            "status": document.parse_status,
            "source_id": document.document_id,
        }
    ]
    result["processing"] = {
        **fields,
        "mainline": "unstructured_document_qwen/v1",
        "parse_status": document.parse_status, "parse_error": document.parse_error,
        "extract_status": extracted_fields_status(fields),
        "doc_type": str(semantic_fields.get("doc_type") or "").strip(),
        "file_name": source_path.name,
        "file_type": source_path.suffix.lower(),
        "source_path": str(source_path),
        "file_hash": document.file_hash,
        "content_status": "content_ready",
        "confirmation_eligibility": readiness["status"],
        "block_reason": readiness["block_reason"],
        "next_action": "none" if readiness["eligible"] else "manual_review",
        "parser": dict(document.parser_audit),
        "evidence": dict(evidence.audit),
        "field_integrity": integrity,
        "semantic": semantic_audit,
        "ai": {
            "invoked": bool(semantic_audit.get("invoked")),
            "status": semantic_status,
            "skip_reason": str(semantic_audit.get("failure_reason") or ""),
        },
        "award_details": award_details,
        "field_completeness": readiness,
        "stage_counters": {
            "document_ai_calls": _semantic_call_count(semantic_audit),
        },
        "stage_timings_ms": {
            "partition": round(parse_elapsed * 1000, 3),
            "chunking": evidence.audit["elapsed_chunking_ms"],
            **dict(semantic_audit.get("stage_timings_ms") or {}),
        },
        "stage_trace": trace,
    }
    return result


def document_packages_to_award_details(
    document_id: str, project_number: str, packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for package_index, package in enumerate(packages):
        package_identifier = str(package.get("package_identifier") or "").strip()
        package_name = str(package.get("package_name") or "").strip()
        for award_index, award in enumerate(package.get("awards") or []):
            if not isinstance(award, dict):
                continue
            winner = str(award.get("winner") or "").strip()
            amount = str(award.get("award_amount") or "").strip()
            if not _valid_award_pair(winner, amount):
                continue
            identity = json.dumps(
                [document_id, project_number, package_identifier, package_name, winner, amount],
                ensure_ascii=False,
            )
            details.append({
                "award_detail_id": "award_detail_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
                "winner": winner, "award_amount": amount,
                "package_detail": {
                    **({"package_number": package_identifier} if package_identifier else {}),
                    **({"package_name": package_name} if package_name else {}),
                },
                "source_type": "file_upload",
                "source_trace": {
                    "source_id": document_id, "package_index": package_index,
                    "award_index": award_index,
                },
            })
    return details


def acquire_xlsx_file(
    source_path: Path,
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
) -> dict[str, Any]:
    """Convert XLSX rows into the existing Candidate publication pipeline."""
    parsed = parse_xlsx_workbook(source_path)
    row_payloads = [dict(row) for row in parsed.get("rows", []) if isinstance(row, dict)]
    candidate_rows = [dict(row.get("candidate_row") or {}) for row in row_payloads]
    result = publish_candidates(candidate_rows, "xlsx_file_upload", paths)
    candidates_by_id = {
        str(candidate.get("candidate_id") or ""): dict(candidate)
        for candidate in result.get("candidates", [])
        if isinstance(candidate, dict)
    }
    row_tasks: list[dict[str, Any]] = []
    for payload in row_payloads:
        candidate_id = str((payload.get("candidate_row") or {}).get("candidate_id") or "")
        row_tasks.append({
            "status": "COMPLETED",
            "source_key": str(payload.get("source_key") or ""),
            "candidate_id": candidate_id,
            "candidate": candidates_by_id.get(candidate_id, {}),
            "processing": dict(payload.get("processing") or {}),
            "sources": [dict(row) for row in payload.get("sources", []) if isinstance(row, dict)],
            "row_payload": payload,
            "error_type": "",
            "error_message": "",
        })
    for payload in parsed.get("failed_rows", []):
        if not isinstance(payload, dict):
            continue
        row_tasks.append({
            "status": "FAILED",
            "source_key": str(payload.get("source_key") or ""),
            "candidate_id": "",
            "candidate": {},
            "processing": dict(payload.get("processing") or {}),
            "sources": [dict(row) for row in payload.get("sources", []) if isinstance(row, dict)],
            "row_payload": payload,
            "error_type": str(payload.get("error_type") or "XlsxRowError"),
            "error_message": str(payload.get("error_message") or "该 Excel 行无法转换为业务候选。"),
        })
    summary = dict(parsed.get("summary") or {})
    result.update({
        "processing": summary,
        "sources": [{
            "source_type": "xlsx_workbook",
            "source_file": source_path.name,
            "source_path": str(source_path),
            "file_sha256": str(summary.get("file_hash") or ""),
            "created_time": utc_now(),
            "status": "parsed",
        }],
        "row_tasks": row_tasks,
    })
    return result


def acquire_xlsx_row(
    row_payload: dict[str, Any],
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
) -> dict[str, Any]:
    """Retry one prepared XLSX row without creating a parallel confirmation path."""
    candidate_row = dict(row_payload.get("candidate_row") or {})
    if not candidate_row:
        raise ValueError("该 Excel 行缺少可重试的结构化候选。")
    result = publish_candidates([candidate_row], "xlsx_file_upload", paths)
    result["processing"] = dict(row_payload.get("processing") or {})
    result["sources"] = [dict(row) for row in row_payload.get("sources", []) if isinstance(row, dict)]
    return result


def publish_candidates(
    rows: list[dict[str, Any]], source_type: str, paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths()
) -> dict[str, Any]:
    """Persist Candidates first, then refresh derived views with isolated failures."""
    with _PUBLICATION_LOCK:
        remaining = remaining_file_processing_seconds()
        if remaining is not None and remaining <= 0:
            raise FileProcessingDeadlineExceeded(
                "file processing wall-clock budget exhausted before publication"
            )
        return _publish_candidates_locked(rows, source_type, paths)


def _publish_candidates_locked(
    rows: list[dict[str, Any]], source_type: str, paths: AcquisitionWorkflowPaths
) -> dict[str, Any]:
    """Run one complete shared publication transaction under the process lock."""
    timings: dict[str, float] = {}
    stage_started = perf_counter()
    existing_payloads = load_json_array(paths.asset_candidates)
    previous_deduped = load_json_array(paths.deduped_candidates)
    previous_lifecycles = load_json_array(paths.lifecycle)
    previous_queue = load_json_array(paths.review_queue)
    timings["read"] = perf_counter() - stage_started
    existing = [asset_candidate_from_payload(item) for item in existing_payloads]
    existing_ids = {item.candidate_id for item in existing}
    new_candidates = [normalize_asset_candidate(row, source_type, []) for row in rows]
    candidate_models = unique_candidates(existing + new_candidates)
    changed_candidate_ids = {candidate.candidate_id for candidate in new_candidates}

    stage_started = perf_counter()
    write_asset_candidates(candidate_models, paths.asset_candidates)
    candidates = [asdict(candidate) for candidate in candidate_models]
    timings["candidate_write"] = perf_counter() - stage_started

    refresh = refresh_candidate_views(
        candidates,
        changed_candidate_ids,
        paths,
        previous_deduped=previous_deduped,
        previous_lifecycles=previous_lifecycles,
        previous_queue=previous_queue,
    )
    timings.update(dict(refresh.get("timings") or {}))
    failed = str(refresh.get("status") or "") == "failed"
    return {
        "created_candidate_count": sum(1 for candidate in new_candidates if candidate.candidate_id not in existing_ids),
        "asset_candidate_count": len(candidates),
        "review_queue_count": int(refresh.get("review_queue_count") or 0),
        "candidates": [asdict(candidate) for candidate in new_candidates],
        "status": "candidate_persisted_post_processing_failed" if failed else "review_queue_ready",
        "downstream_refresh": {
            key: value for key, value in refresh.items() if key not in {"timings", "performance"}
        },
        "performance": {
            **dict(refresh.get("performance") or {}),
            "stage_timings_ms": {key: round(value * 1000, 3) for key, value in timings.items()},
        },
    }


def retry_candidate_refresh(
    changed_candidate_ids: set[str],
    paths: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths(),
) -> dict[str, Any]:
    """Retry only derived Candidate views; never reacquire or rewrite a Candidate."""
    candidates = load_json_array(paths.asset_candidates)
    return refresh_candidate_views(
        candidates,
        set(changed_candidate_ids),
        paths,
        previous_deduped=load_json_array(paths.deduped_candidates),
        previous_lifecycles=load_json_array(paths.lifecycle),
        previous_queue=load_json_array(paths.review_queue),
    )


def refresh_candidate_views(
    candidates: list[dict[str, Any]],
    changed_candidate_ids: set[str],
    paths: AcquisitionWorkflowPaths,
    *,
    previous_deduped: list[dict[str, Any]],
    previous_lifecycles: list[dict[str, Any]],
    previous_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    """Refresh derived views with the tracked baseline module interfaces."""
    timings: dict[str, float] = {}

    def failure(stage: str, exc: Exception) -> dict[str, Any]:
        return {
            "status": "failed",
            "failed_stage": stage,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "retryable": True,
            "changed_candidate_ids": sorted(changed_candidate_ids),
            "next_action": "retry_downstream_refresh",
            "review_queue_count": len(previous_queue),
            "timings": timings,
            "performance": {"counts": {}, "dedup": {}, "lifecycle": {}, "review": {}},
        }

    try:
        stage_started = perf_counter()
        deduped, dedup_summary = deduplicate_candidates(
            candidates,
            independent_source_types=INDEPENDENT_CANDIDATE_SOURCE_TYPES,
        )
        timings["dedup"] = perf_counter() - stage_started
        stage_started = perf_counter()
        write_dedup_outputs(deduped, dedup_summary, paths.deduped_candidates, paths.dedup_summary)
        timings["dedup_write"] = perf_counter() - stage_started
    except Exception as exc:
        return failure("dedup", exc)

    try:
        stage_started = perf_counter()
        lifecycles = build_asset_lifecycles(asset_candidates=candidates, deduped_candidates=deduped)
        timings["lifecycle"] = perf_counter() - stage_started
        stage_started = perf_counter()
        write_asset_lifecycles(lifecycles, paths.lifecycle)
        timings["lifecycle_write"] = perf_counter() - stage_started
    except Exception as exc:
        return failure("lifecycle", exc)

    lifecycle_payloads = [asdict(item) for item in lifecycles]
    try:
        stage_started = perf_counter()
        queue = build_review_queue(deduped, lifecycle_payloads, [])
        timings["review"] = perf_counter() - stage_started
        stage_started = perf_counter()
        write_review_queue(queue, paths.review_queue)
        timings["review_write"] = perf_counter() - stage_started
    except Exception as exc:
        return failure("review", exc)

    return {
        "status": "success",
        "failed_stage": "",
        "error_type": "",
        "error_message": "",
        "retryable": False,
        "changed_candidate_ids": sorted(changed_candidate_ids),
        "next_action": "none",
        "review_queue_count": len(queue),
        "timings": timings,
        "performance": {
            "counts": {
                "dedup_full_rebuilds": 1,
                "dedup_incremental_rebuilds": 0,
                "lifecycle_full_rebuilds": 1,
                "lifecycle_incremental_rebuilds": 0,
                "review_full_rebuilds": 1,
                "review_incremental_rebuilds": 0,
            },
            "dedup": {"mode": "full"},
            "lifecycle": {"mode": "full"},
            "review": {"mode": "full"},
        },
    }


def url_candidate_row(source: DocumentSource, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "source_title": source.title or fields.get("project_name") or source.source_url,
        "source_url": source.source_url,
        "source_file": "",
        "discovered_time": source.capture_time,
        "confidence": 0.0,
        **fields,
    }


def document_source_view(source: DocumentSource) -> dict[str, Any]:
    provenance = dict(source.metadata.get("provenance") or {})
    view = {
        "source_type": source.source_type,
        "source_url": source.source_url,
        "source_file": "",
        "created_time": source.capture_time,
        "status": "captured",
        "source_id": source.source_id,
        "access": dict(source.metadata.get("access") or {}),
        "provenance": provenance,
        "html_sha256": str(source.metadata.get("html_sha256") or ""),
    }
    if str(provenance.get("source_origin") or ""):
        view["source_origin"] = str(provenance["source_origin"])
    return view


def extracted_fields_status(fields: dict[str, Any]) -> str:
    values = [
        str(fields.get(key) or "").strip()
        for key in (
            "project_name", "customer", "project_number", "content", "budget",
            "bid_open_time", "winner", "award_amount",
        )
    ]
    populated = sum(bool(value) for value in values)
    if populated == 0:
        return "empty"
    if populated == 1:
        return "partial"
    return "success"


def asset_candidate_from_payload(payload: dict[str, Any]) -> AssetCandidate:
    return AssetCandidate(
        candidate_id=str(payload.get("candidate_id") or ""),
        source_type=str(payload.get("source_type") or ""),
        source_title=str(payload.get("source_title") or ""),
        source_url=str(payload.get("source_url") or ""),
        matched_project_id=str(payload.get("matched_project_id") or ""),
        confidence=float(payload.get("confidence") or 0),
        status=str(payload.get("status") or "new_project_candidate"),
        source_trace=dict(payload.get("source_trace") or {}),
    )


def unique_candidates(candidates: list[AssetCandidate]) -> list[AssetCandidate]:
    """Keep stable IDs while refreshing an existing candidate with the latest extraction snapshot."""
    unique: list[AssetCandidate] = []
    positions: dict[str, int] = {}
    for candidate in candidates:
        if not candidate.candidate_id:
            continue
        if candidate.candidate_id in positions:
            unique[positions[candidate.candidate_id]] = candidate
        else:
            positions[candidate.candidate_id] = len(unique)
            unique.append(candidate)
    return unique
