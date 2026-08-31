"""Controlled real-material verification over existing V3 modules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.acquisition.inbox.acquisition_inbox import (
    AcquisitionInboxPaths,
    COMPLETED,
    create_file_item,
    process_item,
)
from src.matcher.record_matcher import project_numbers
from src.operator.acquisition_workflow import AcquisitionWorkflowPaths
from src.operator.operator_workflow import apply_reviewed
from src.project_relation.project_document_relation import (
    DEFAULT_PROJECT_DOCUMENT_LINKS,
    create_link,
    find_existing_link,
    load_json_array as load_links,
    write_links,
)
from src.query.repository_query import RepositoryQueryService
from src.repository.asset_repository import (
    DEFAULT_DOCUMENTS_REPOSITORY,
    DEFAULT_PROJECTS_REPOSITORY,
    DEFAULT_REPOSITORY_AUDIT,
    load_json_array as load_repository_rows,
)
from src.review.review_decision import (
    DEFAULT_REVIEW_DECISIONS_OUTPUT,
    DEFAULT_REVIEW_QUEUE_INPUT,
    append_review_decision,
    create_review_decision,
    load_review_queue,
)


FORMAL_DATA_PATHS = [
    Path("招投标.xlsx"),
    Path("data/cache/final_records.json"),
    Path("data/overrides/manual_overrides.json"),
]
DEFAULT_REPORT = Path("data/diagnostics/phase34_business_flow_report.json")
DEFAULT_SUMMARY = Path("data/diagnostics/phase34_business_flow_summary.json")


@dataclass(frozen=True)
class BusinessFlowSample:
    path: Path
    group_key: str
    relation_type: str
    association: str = "confirmed"


@dataclass(frozen=True)
class BusinessFlowPaths:
    inbox: AcquisitionInboxPaths = AcquisitionInboxPaths()
    acquisition: AcquisitionWorkflowPaths = AcquisitionWorkflowPaths()
    review_queue: Path = DEFAULT_REVIEW_QUEUE_INPUT
    review_decisions: Path = DEFAULT_REVIEW_DECISIONS_OUTPUT
    projects: Path = DEFAULT_PROJECTS_REPOSITORY
    documents: Path = DEFAULT_DOCUMENTS_REPOSITORY
    repository_audit: Path = DEFAULT_REPOSITORY_AUDIT
    links: Path = DEFAULT_PROJECT_DOCUMENT_LINKS
    report: Path = DEFAULT_REPORT
    summary: Path = DEFAULT_SUMMARY


def run_business_flow(
    samples: list[BusinessFlowSample],
    *,
    paths: BusinessFlowPaths = BusinessFlowPaths(),
    reviewer: str = "phase34_business_flow",
) -> dict[str, Any]:
    """Verify a small, explicitly selected sample set without scanning its directory."""
    before_formal = hashes(FORMAL_DATA_PATHS)
    rows = [initial_row(sample) for sample in samples]

    for sample, row in zip(samples, rows):
        try:
            inbox_item = create_file_item(sample.path, paths.inbox)
            row["acquisition_task_id"] = inbox_item["inbox_id"]
            processed = process_item(inbox_item["inbox_id"], paths.acquisition, paths.inbox)
            item = dict(processed.get("item") or {})
            workflow = dict(processed.get("workflow") or {})
            row.update(processing_fields(workflow))
            row["inbox_status"] = str(item.get("status") or "")
            row["generated_asset_ids"] = list(item.get("generated_asset_ids") or [])
            if item.get("status") != COMPLETED or not row["generated_asset_ids"]:
                row["failed_stage"] = "acquisition"
                row["failed_reason"] = str(item.get("error_message") or "No Asset Candidate generated")
        except Exception as exc:  # A single source must not stop the selected batch.
            row["failed_stage"] = "acquisition"
            row["failed_reason"] = str(exc)

    for group_key in sorted({sample.group_key for sample in samples}):
        group_rows = [row for sample, row in zip(samples, rows) if sample.group_key == group_key]
        if any(sample.association == "ambiguous" for sample in samples if sample.group_key == group_key):
            for row in group_rows:
                row["association_status"] = "ambiguous"
            continue
        ready_rows = [row for row in group_rows if row.get("generated_asset_ids") and not row.get("failed_stage")]
        if not ready_rows:
            continue
        process_group(ready_rows, paths, reviewer)

    after_formal = hashes(FORMAL_DATA_PATHS)
    report = {
        "phase": "Phase34",
        "generated_time": utc_now(),
        "sample_count": len(rows),
        "samples": rows,
        "formal_data_hashes_before": before_formal,
        "formal_data_hashes_after": after_formal,
        "formal_data_unchanged": before_formal == after_formal,
        "current_flow_breakpoints": [
            "XLS/XLSX upload types are accepted by the UI but not supported by the existing Parser; Inbox records FAILED diagnostics.",
            "Repository Query needed a minimal project-name lookup for this verification.",
        ],
        "minimal_fixes": [
            "Single-file Acquisition now calls Parser on the selected file instead of scanning its parent directory.",
            "Repository writes append only stable-ID entities, preserving existing assets during controlled apply.",
        ],
        "unresolved_issues": unresolved(rows),
    }
    summary = build_summary(rows, report)
    write_json(paths.report, report)
    write_json(paths.summary, summary)
    return report


def process_group(rows: list[dict[str, Any]], paths: BusinessFlowPaths, reviewer: str) -> None:
    canonical = rows[0]
    canonical_asset_id = str(canonical["generated_asset_ids"][0])
    try:
        append_accept(canonical_asset_id, "", paths, reviewer, "Controlled Phase34 project confirmation.")
        apply_reviewed(
            operator=reviewer,
            review_decisions_path=paths.review_decisions,
            review_queue_path=paths.review_queue,
            documents_path=paths.documents,
            projects_path=paths.projects,
            repository_audit_path=paths.repository_audit,
            result_path=Path("data/diagnostics/phase34_apply_project.json"),
        )
        project = find_project_for_asset(paths.projects, canonical_asset_id)
        if project is None:
            raise ValueError("ProjectEntity was not created or reused")
        project_id = str(project["project_id"])
        for row in rows:
            row["project_entity_id"] = project_id
            row["association_status"] = "confirmed"
        for row in rows:
            append_accept(
                str(row["generated_asset_ids"][0]),
                project_id,
                paths,
                reviewer,
                "Controlled Phase34 document confirmation to an explicitly selected project.",
            )
        apply_reviewed(
            operator=reviewer,
            review_decisions_path=paths.review_decisions,
            review_queue_path=paths.review_queue,
            documents_path=paths.documents,
            projects_path=paths.projects,
            repository_audit_path=paths.repository_audit,
            result_path=Path("data/diagnostics/phase34_apply_documents.json"),
        )
        documents = load_repository_rows(paths.documents, "documents")
        for row in rows:
            document = find_document_for_asset(documents, str(row["generated_asset_ids"][0]))
            if document is None:
                row["failed_stage"] = "repository"
                row["failed_reason"] = "DocumentEntity was not created"
                continue
            row["document_entity_id"] = str(document["document_id"])
        establish_links(rows, project_id, paths)
        verify_query(rows, project_id, paths)
    except Exception as exc:
        for row in rows:
            if not row.get("failed_stage"):
                row["failed_stage"] = "controlled_apply"
                row["failed_reason"] = str(exc)


def append_accept(asset_id: str, related_project_id: str, paths: BusinessFlowPaths, reviewer: str, note: str) -> None:
    decision = create_review_decision(
        load_review_queue(paths.review_queue),
        asset_id,
        "ACCEPT",
        "Phase34 controlled real-material verification.",
        reviewer=reviewer,
        review_time=decision_time(),
        reviewer_note=note,
        related_project_id=related_project_id,
    )
    append_review_decision(decision, paths.review_decisions)


def establish_links(rows: list[dict[str, Any]], project_id: str, paths: BusinessFlowPaths) -> None:
    projects = load_repository_rows(paths.projects, "projects")
    documents = load_repository_rows(paths.documents, "documents")
    links = load_links(paths.links, "project document links")
    changed = False
    for row in rows:
        document_id = str(row.get("document_entity_id") or "")
        if not document_id:
            continue
        try:
            existing = find_existing_link(links, project_id, document_id, str(row["relation_type"]))
            if existing is None:
                links.append(
                    create_link(
                        links,
                        projects,
                        documents,
                        project_id=project_id,
                        document_id=document_id,
                        relation_type=str(row["relation_type"]),
                        source="phase34_business_flow",
                    )
                )
                changed = True
                row["relation_status"] = "created"
            else:
                row["relation_status"] = "existing"
        except Exception as exc:
            row["relation_status"] = "failed"
            row["failed_stage"] = "relation"
            row["failed_reason"] = str(exc)
    if changed:
        write_links(links, paths.links)


def verify_query(rows: list[dict[str, Any]], project_id: str, paths: BusinessFlowPaths) -> None:
    service = RepositoryQueryService(paths.projects, paths.documents, paths.links)
    project_asset = service.get_project_asset(project_id)
    search_matches = service.search_projects_by_name(str((project_asset.get("project") or {}).get("project_name") or ""))
    query_ok = bool(project_asset.get("project")) and bool(search_matches)
    for row in rows:
        row["query_status"] = "success" if query_ok else "failed"
        row["query_document_count"] = len(project_asset.get("documents") or [])
        if not query_ok and not row.get("failed_stage"):
            row["failed_stage"] = "query"
            row["failed_reason"] = "Project query or name lookup returned no result"


def initial_row(sample: BusinessFlowSample) -> dict[str, Any]:
    return {
        "source_relative_path": str(sample.path),
        "file_name": sample.path.name,
        "file_type": sample.path.suffix.lower(),
        "source_sha256": sha256(sample.path),
        "group_key": sample.group_key,
        "relation_type": sample.relation_type,
        "association_status": sample.association,
        "acquisition_task_id": "",
        "inbox_status": "",
        "parse_status": "",
        "extract_status": "",
        "project_name": "",
        "customer_name": "",
        "project_number": "",
        "generated_asset_ids": [],
        "project_entity_id": "",
        "document_entity_id": "",
        "relation_status": "",
        "query_status": "",
        "failed_stage": "",
        "failed_reason": "",
    }


def processing_fields(workflow: dict[str, Any]) -> dict[str, str]:
    processing = dict(workflow.get("processing") or {})
    project_name = str(processing.get("project_name") or "")
    customer = str(processing.get("customer") or "")
    numbers = sorted(project_numbers({"project_name": project_name, "source_file": "", "content": "", "note": ""}))
    return {
        "parse_status": str(processing.get("parse_status") or ""),
        "extract_status": str(processing.get("extract_status") or ""),
        "project_name": project_name,
        "customer_name": customer,
        "project_number": numbers[0] if numbers else "",
    }


def find_project_for_asset(path: Path, asset_id: str) -> dict[str, Any] | None:
    for project in load_repository_rows(path, "projects"):
        if str(project.get("asset_id") or "") == asset_id:
            return project
    return None


def find_document_for_asset(documents: list[dict[str, Any]], asset_id: str) -> dict[str, Any] | None:
    for document in documents:
        if str(document.get("asset_id") or "") == asset_id:
            return document
    return None


def build_summary(rows: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    def count(predicate: Any) -> int:
        return sum(1 for row in rows if predicate(row))

    return {
        "sample_count": len(rows),
        "acquisition_success_count": count(lambda row: bool(row["acquisition_task_id"]) and not row["failed_stage"]),
        "parser_success_count": count(lambda row: row["parse_status"] == "success"),
        "extractor_success_count": count(lambda row: row["extract_status"] == "success"),
        "project_entity_success_count": len({row["project_entity_id"] for row in rows if row["project_entity_id"]}),
        "document_entity_success_count": count(lambda row: bool(row["document_entity_id"])),
        "relation_success_count": count(lambda row: row["relation_status"] in {"created", "existing"}),
        "query_success_count": len({row["project_entity_id"] for row in rows if row["query_status"] == "success"}),
        "ambiguous_count": count(lambda row: row["association_status"] == "ambiguous"),
        "failed_count": count(lambda row: bool(row["failed_stage"])),
        "failures": [
            {"file_name": row["file_name"], "stage": row["failed_stage"], "reason": row["failed_reason"]}
            for row in rows
            if row["failed_stage"]
        ],
        "current_flow_breakpoints": report["current_flow_breakpoints"],
        "minimal_fixes": report["minimal_fixes"],
        "unresolved_issues": report["unresolved_issues"],
    }


def unresolved(rows: list[dict[str, Any]]) -> list[str]:
    issues = sorted({str(row["failed_reason"]) for row in rows if row["failed_reason"]})
    return issues


def hashes(paths: list[Path]) -> dict[str, str]:
    return {str(path): sha256(path) for path in paths}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def decision_time() -> str:
    """Ensure append-only controlled decisions have a deterministic latest ordering."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
