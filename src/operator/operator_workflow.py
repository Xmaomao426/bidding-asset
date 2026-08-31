from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.intake.asset_intake import (  # noqa: E402
    DEFAULT_ASSET_INTAKE_OUTPUT,
    DEFAULT_INTAKE_AUDIT_OUTPUT,
    build_asset_intake,
    latest_decision_by_asset,
    load_json_array as load_intake_json_array,
    write_intake_outputs,
)
from src.project_relation.project_document_relation import (  # noqa: E402
    DEFAULT_PROJECT_DOCUMENT_LINKS,
    create_link,
    load_json_array as load_relation_json_array,
    write_links,
)
from src.query.repository_query import RepositoryQueryService  # noqa: E402
from src.repository.asset_repository import (  # noqa: E402
    DEFAULT_DOCUMENTS_REPOSITORY,
    DEFAULT_PROJECTS_REPOSITORY,
    DEFAULT_REPOSITORY_AUDIT,
    build_asset_repository,
    load_json_array as load_repository_json_array,
    merge_repository_rows,
    write_repository_outputs,
)
from src.review.review_decision import (  # noqa: E402
    DEFAULT_REVIEW_DECISIONS_OUTPUT,
    DEFAULT_REVIEW_QUEUE_INPUT,
    append_review_decision,
    create_review_decision,
    load_review_queue,
)


DEFAULT_OPERATOR_WORKFLOW_RESULT = Path("data/diagnostics/operator_workflow_result.json")


def review_batch(
    *,
    project_asset_id: str,
    document_asset_ids: list[str],
    project_id: str,
    reviewer: str,
    note: str = "",
    review_queue_path: Path = DEFAULT_REVIEW_QUEUE_INPUT,
    review_decisions_path: Path = DEFAULT_REVIEW_DECISIONS_OUTPUT,
    result_path: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT,
    executed_time: str = "",
) -> dict[str, Any]:
    review_queue = load_review_queue(review_queue_path)
    timestamp = executed_time or utc_now()
    result = base_result(
        "review-batch",
        reviewer,
        {
            "project_asset_id": project_asset_id,
            "document_asset_ids": list(document_asset_ids),
            "project_id": project_id,
        },
        executed_time=timestamp,
    )

    review_targets = [(project_asset_id, "")] + [(asset_id, project_id) for asset_id in document_asset_ids]
    for asset_id, related_project_id in review_targets:
        try:
            decision = create_review_decision(
                review_queue,
                asset_id,
                "ACCEPT",
                "Batch ACCEPT from operator workflow.",
                reviewer=reviewer,
                review_time=timestamp,
                reviewer_note=note,
                related_project_id=related_project_id,
            )
            append_review_decision(decision, review_decisions_path)
            result["created_decision_ids"].append(str(decision.get("decision_id") or ""))
        except Exception as exc:  # noqa: BLE001 - operator workflow records per-item failures.
            result["errors"].append({"asset_id": asset_id, "error": str(exc)})

    write_operator_result(result, result_path)
    return result


def apply_reviewed(
    *,
    operator: str = "operator",
    review_decisions_path: Path = DEFAULT_REVIEW_DECISIONS_OUTPUT,
    review_queue_path: Path = DEFAULT_REVIEW_QUEUE_INPUT,
    intake_output_path: Path = DEFAULT_ASSET_INTAKE_OUTPUT,
    intake_audit_path: Path = DEFAULT_INTAKE_AUDIT_OUTPUT,
    documents_path: Path = DEFAULT_DOCUMENTS_REPOSITORY,
    projects_path: Path = DEFAULT_PROJECTS_REPOSITORY,
    repository_audit_path: Path = DEFAULT_REPOSITORY_AUDIT,
    result_path: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT,
    executed_time: str = "",
) -> dict[str, Any]:
    timestamp = executed_time or utc_now()
    result = base_result(
        "apply-reviewed",
        operator,
        {"review_decisions": str(review_decisions_path), "review_queue": str(review_queue_path)},
        executed_time=timestamp,
    )
    try:
        decisions = load_intake_json_array(review_decisions_path, "review decisions")
        review_queue = load_intake_json_array(review_queue_path, "review queue")
        latest_decisions = latest_decision_by_asset(decisions)
        accepted_asset_count = sum(1 for decision in latest_decisions.values() if str(decision.get("decision") or "") == "ACCEPT")
        intake_items, audit_items = build_asset_intake(
            decisions,
            review_queue,
            operator=operator,
            intake_time=timestamp,
        )
        write_intake_outputs(intake_items, audit_items, intake_output_path, intake_audit_path)

        documents, projects, repository_audit = build_asset_repository(intake_items, created_time=timestamp)
        existing_documents = load_repository_json_array(documents_path, "documents")
        existing_projects = load_repository_json_array(projects_path, "projects")
        existing_audit = load_repository_json_array(repository_audit_path, "repository audit")
        documents, added_documents = merge_repository_rows(existing_documents, documents, "document_id")
        projects, added_projects = merge_repository_rows(existing_projects, projects, "project_id")
        repository_audit, _added_audit = merge_repository_rows(existing_audit, repository_audit, "repository_id")
        write_repository_outputs(
            documents,
            projects,
            repository_audit,
            documents_path=documents_path,
            projects_path=projects_path,
            audit_path=repository_audit_path,
        )

        result["summary"] = {
            "accepted_asset_count": accepted_asset_count,
            "intake_count": len(intake_items),
            "project_count": len(added_projects),
            "document_count": len(added_documents),
            "skipped_count": max(len(latest_decisions) - accepted_asset_count, 0),
            "error_count": 0,
        }
        result["created_entity_ids"].extend([str(project.get("project_id") or "") for project in added_projects])
        result["created_entity_ids"].extend([str(document.get("document_id") or "") for document in added_documents])
    except Exception as exc:  # noqa: BLE001 - command result must report workflow errors.
        result["errors"].append({"error": str(exc)})
        result["summary"] = {
            "accepted_asset_count": 0,
            "intake_count": 0,
            "project_count": 0,
            "document_count": 0,
            "skipped_count": 0,
            "error_count": 1,
        }

    result["summary"]["error_count"] = len(result["errors"])
    write_operator_result(result, result_path)
    return result


def link_documents(
    *,
    project_id: str,
    bid_notice: str = "",
    award_notice: str = "",
    contract: str = "",
    source: str = "operator_workflow",
    operator: str = "operator",
    projects_path: Path = DEFAULT_PROJECTS_REPOSITORY,
    documents_path: Path = DEFAULT_DOCUMENTS_REPOSITORY,
    links_path: Path = DEFAULT_PROJECT_DOCUMENT_LINKS,
    result_path: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT,
    executed_time: str = "",
) -> dict[str, Any]:
    timestamp = executed_time or utc_now()
    relation_inputs = [
        ("bid_notice", bid_notice),
        ("award_notice", award_notice),
        ("contract", contract),
    ]
    result = base_result(
        "link-documents",
        operator,
        {
            "project_id": project_id,
            "bid_notice": bid_notice,
            "award_notice": award_notice,
            "contract": contract,
        },
        executed_time=timestamp,
    )

    projects = load_relation_json_array(projects_path, "projects")
    documents = load_relation_json_array(documents_path, "documents")
    links = load_relation_json_array(links_path, "project document links")
    changed = False

    for index, (relation_type, document_id) in enumerate(relation_inputs):
        if not document_id:
            result["skipped_items"].append({"relation_type": relation_type, "reason": "document_id omitted"})
            continue
        try:
            before_count = len(links)
            link = create_link(
                links,
                projects,
                documents,
                project_id=project_id,
                document_id=document_id,
                relation_type=relation_type,
                source=source,
                created_time=offset_timestamp(timestamp, index),
            )
            if any(str(existing.get("link_id") or "") == str(link.get("link_id") or "") for existing in links):
                result["skipped_items"].append(
                    {"relation_type": relation_type, "document_id": document_id, "reason": "duplicate_link"}
                )
                continue
            links.append(link)
            changed = len(links) != before_count
            result["created_link_ids"].append(str(link.get("link_id") or ""))
        except Exception as exc:  # noqa: BLE001 - keep batch linking going after one bad document.
            result["errors"].append({"relation_type": relation_type, "document_id": document_id, "error": str(exc)})

    if changed:
        write_links(links, links_path)
    write_operator_result(result, result_path)
    return result


def show_project(
    *,
    project_id: str,
    operator: str = "operator",
    projects_path: Path = DEFAULT_PROJECTS_REPOSITORY,
    documents_path: Path = DEFAULT_DOCUMENTS_REPOSITORY,
    links_path: Path = DEFAULT_PROJECT_DOCUMENT_LINKS,
    result_path: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT,
    executed_time: str = "",
) -> dict[str, Any]:
    timestamp = executed_time or utc_now()
    result = base_result(
        "show-project",
        operator,
        {"project_id": project_id},
        executed_time=timestamp,
    )
    try:
        service = RepositoryQueryService(projects_path, documents_path, links_path)
        project_asset = service.get_project_asset(project_id)
        timeline = service.get_project_timeline(project_id)
        result["project_asset"] = project_asset
        result["timeline"] = timeline
    except Exception as exc:  # noqa: BLE001 - CLI should return structured errors.
        result["errors"].append({"project_id": project_id, "error": str(exc)})

    write_operator_result(result, result_path)
    return result


def base_result(action: str, operator: str, input_ids: dict[str, Any], *, executed_time: str) -> dict[str, Any]:
    return {
        "workflow_id": f"operator_workflow_{uuid.uuid4().hex}",
        "action": action,
        "operator": operator.strip() or "operator",
        "executed_time": executed_time,
        "input_ids": input_ids,
        "created_decision_ids": [],
        "created_entity_ids": [],
        "created_link_ids": [],
        "skipped_items": [],
        "errors": [],
    }


def write_operator_result(result: dict[str, Any], result_path: Path = DEFAULT_OPERATOR_WORKFLOW_RESULT) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def offset_timestamp(value: str, seconds: int) -> str:
    if not value or seconds == 0:
        return value
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Minimal operator workflow over existing V3 asset modules.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    review_parser = subparsers.add_parser("review-batch", help="Batch ACCEPT one project asset and related document assets.")
    review_parser.add_argument("--project-asset-id", required=True)
    review_parser.add_argument("--document-asset-ids", nargs="*", default=[])
    review_parser.add_argument("--project-id", required=True)
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument("--note", default="")
    review_parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE_INPUT))
    review_parser.add_argument("--review-decisions", default=str(DEFAULT_REVIEW_DECISIONS_OUTPUT))

    apply_parser = subparsers.add_parser("apply-reviewed", help="Build intake and repository outputs from ACCEPT decisions.")
    apply_parser.add_argument("--operator", default="operator")
    apply_parser.add_argument("--review-decisions", default=str(DEFAULT_REVIEW_DECISIONS_OUTPUT))
    apply_parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE_INPUT))
    apply_parser.add_argument("--intake-output", default=str(DEFAULT_ASSET_INTAKE_OUTPUT))
    apply_parser.add_argument("--intake-audit", default=str(DEFAULT_INTAKE_AUDIT_OUTPUT))
    apply_parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    apply_parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    apply_parser.add_argument("--repository-audit", default=str(DEFAULT_REPOSITORY_AUDIT))

    link_parser = subparsers.add_parser("link-documents", help="Explicitly link known documents to one project.")
    link_parser.add_argument("--project-id", required=True)
    link_parser.add_argument("--bid-notice", default="")
    link_parser.add_argument("--award-notice", default="")
    link_parser.add_argument("--contract", default="")
    link_parser.add_argument("--source", default="operator_workflow")
    link_parser.add_argument("--operator", default="operator")
    link_parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    link_parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    link_parser.add_argument("--links", default=str(DEFAULT_PROJECT_DOCUMENT_LINKS))

    show_parser = subparsers.add_parser("show-project", help="Show project asset view and timeline.")
    show_parser.add_argument("--project-id", required=True)
    show_parser.add_argument("--operator", default="operator")
    show_parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    show_parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    show_parser.add_argument("--links", default=str(DEFAULT_PROJECT_DOCUMENT_LINKS))

    for command_parser in (review_parser, apply_parser, link_parser, show_parser):
        command_parser.add_argument("--result", default=str(DEFAULT_OPERATOR_WORKFLOW_RESULT))

    args = parser.parse_args()
    if args.action == "review-batch":
        result = review_batch(
            project_asset_id=args.project_asset_id,
            document_asset_ids=args.document_asset_ids,
            project_id=args.project_id,
            reviewer=args.reviewer,
            note=args.note,
            review_queue_path=Path(args.review_queue),
            review_decisions_path=Path(args.review_decisions),
            result_path=Path(args.result),
        )
    elif args.action == "apply-reviewed":
        result = apply_reviewed(
            operator=args.operator,
            review_decisions_path=Path(args.review_decisions),
            review_queue_path=Path(args.review_queue),
            intake_output_path=Path(args.intake_output),
            intake_audit_path=Path(args.intake_audit),
            documents_path=Path(args.documents),
            projects_path=Path(args.projects),
            repository_audit_path=Path(args.repository_audit),
            result_path=Path(args.result),
        )
    elif args.action == "link-documents":
        result = link_documents(
            project_id=args.project_id,
            bid_notice=args.bid_notice,
            award_notice=args.award_notice,
            contract=args.contract,
            source=args.source,
            operator=args.operator,
            projects_path=Path(args.projects),
            documents_path=Path(args.documents),
            links_path=Path(args.links),
            result_path=Path(args.result),
        )
    else:
        result = show_project(
            project_id=args.project_id,
            operator=args.operator,
            projects_path=Path(args.projects),
            documents_path=Path(args.documents),
            links_path=Path(args.links),
            result_path=Path(args.result),
        )
    print_json(result)


if __name__ == "__main__":
    main()
