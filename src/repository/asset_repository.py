from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.intake.asset_intake import DEFAULT_ASSET_INTAKE_OUTPUT
from src.award_detail import (
    award_detail_id,
    build_award_detail,
    business_group_id,
    business_project_id,
    business_sequence,
    first_value,
    merge_award_details,
    publish_date,
)


DEFAULT_DOCUMENTS_REPOSITORY = Path("data/repository/documents.json")
DEFAULT_PROJECTS_REPOSITORY = Path("data/repository/projects.json")
DEFAULT_REPOSITORY_AUDIT = Path("data/repository/repository_audit.json")

CANDIDATE = "candidate"
CONFIRMED = "confirmed"
SUPPORTED_STATUSES = {CANDIDATE, CONFIRMED}


def build_asset_repository(
    intake_items: list[dict[str, Any]],
    *,
    created_time: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build simplified asset repository entities from intake items."""
    timestamp = created_time or utc_now()
    documents: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for item in intake_items:
        if "document_candidate" in item:
            document = document_entity_from_intake(item, timestamp)
            documents.append(document)
            audit.append(audit_item(item, "document", document["document_id"], timestamp))
        elif "project_candidate" in item:
            project = project_entity_from_intake(item, timestamp)
            projects.append(project)
            audit.append(audit_item(item, "project", project["project_id"], timestamp))
    return documents, projects, audit


def document_entity_from_intake(item: dict[str, Any], created_time: str) -> dict[str, Any]:
    candidate = dict(item.get("document_candidate") or {})
    metadata = dict(candidate.get("metadata") or {})
    decision = decision_snapshot(item)
    lifecycle = lifecycle_snapshot(decision)
    trace = source_trace(item, metadata)
    detail_id = award_detail_id(trace)
    return {
        "document_id": build_award_detail_document_id(detail_id) if detail_id else str(candidate.get("document_id") or build_document_id_from_item(item)),
        "asset_id": str(item.get("asset_id") or ""),
        "project_id": str(item.get("project_id") or metadata.get("project_id") or ""),
        "document_metadata": metadata,
        "source_trace": trace,
        "lifecycle": lifecycle,
        "review_decision_snapshot": decision,
        "status": CANDIDATE,
        "created_time": created_time,
    }


def build_award_detail_document_id(detail_id: str) -> str:
    digest = hashlib.sha256(f"xlsx_award_detail_document\n{detail_id}".encode("utf-8")).hexdigest()[:16]
    return f"document_{digest}"


def project_entity_from_intake(item: dict[str, Any], created_time: str) -> dict[str, Any]:
    candidate = dict(item.get("project_candidate") or {})
    metadata = dict(candidate.get("metadata") or {})
    decision = decision_snapshot(item)
    lifecycle = lifecycle_snapshot(decision)
    project_name = str(candidate.get("project_name") or metadata.get("project_name") or "")
    trace = source_trace(item, metadata)
    detail = build_award_detail(trace)
    return {
        "project_id": business_project_id(trace) or str(candidate.get("project_candidate_id") or build_project_id(project_name, str(item.get("asset_id") or ""))),
        "asset_id": str(item.get("asset_id") or ""),
        "project_name": project_name,
        "customer": first_value(trace, "customer"),
        "business_sequence": business_sequence(trace),
        "business_group_id": business_group_id(trace),
        "publish_date": publish_date(trace),
        "award_details": [detail] if detail else [],
        "source_trace": trace,
        "lifecycle": lifecycle,
        "review_decision_snapshot": decision,
        "status": CANDIDATE,
        "created_time": created_time,
    }


def project_business_group_id(project: dict[str, Any]) -> str:
    """Return the persisted or backward-compatible Tianyancha group identity."""
    return business_group_id(project)


def merge_project_award_detail(
    project: dict[str, Any],
    extracted: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Merge one physical XLSX row as an idempotent award detail."""
    detail = build_award_detail(extracted)
    if not detail:
        return dict(project), None
    merged, added = merge_award_details(project.get("award_details"), [detail])
    updated = copy.deepcopy(project)
    updated["award_details"] = merged
    for key, value in (
        ("business_sequence", business_sequence(extracted)),
        ("business_group_id", business_group_id(extracted)),
        ("publish_date", publish_date(extracted)),
        ("customer", first_value(extracted, "customer")),
    ):
        if value and not str(updated.get(key) or "").strip():
            updated[key] = value
    return updated, (added[0] if added else None)


def audit_item(item: dict[str, Any], entity_type: str, entity_id: str, created_time: str) -> dict[str, str]:
    decision = decision_snapshot(item)
    return {
        "repository_id": build_repository_id(entity_type, entity_id, str(item.get("intake_id") or "")),
        "asset_id": str(item.get("asset_id") or ""),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "created_time": created_time,
        "source_decision_id": str(decision.get("decision_id") or ""),
        "intake_id": str(item.get("intake_id") or ""),
    }


def update_entity_status(entity: dict[str, Any], status: str, *, updated_time: str = "") -> dict[str, Any]:
    normalized = status.strip().lower()
    if normalized not in SUPPORTED_STATUSES:
        raise ValueError(f"Unsupported status: {status}. Supported statuses: {', '.join(sorted(SUPPORTED_STATUSES))}")
    updated = dict(entity)
    updated["status"] = normalized
    updated["status_updated_time"] = updated_time or utc_now()
    return updated


def decision_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    decision = item.get("decision_snapshot")
    return copy.deepcopy(decision) if isinstance(decision, dict) else {}


def lifecycle_snapshot(decision: dict[str, Any]) -> dict[str, Any]:
    snapshot = decision.get("asset_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = decision.get("snapshot")
    if not isinstance(snapshot, dict):
        return {}
    lifecycle = snapshot.get("lifecycle")
    if isinstance(lifecycle, dict):
        return copy.deepcopy(lifecycle)
    lifecycle_status = str(snapshot.get("lifecycle_status") or "")
    return {"status": lifecycle_status} if lifecycle_status else {}


def source_trace(item: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    trace = item.get("source_trace")
    if isinstance(trace, dict) and trace:
        return copy.deepcopy(trace)
    trace = metadata.get("source_trace")
    if isinstance(trace, dict):
        return copy.deepcopy(trace)
    return {}


def build_document_id_from_item(item: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        "\n".join([str(item.get("asset_id") or ""), str(item.get("intake_id") or ""), "document"]).encode("utf-8")
    ).hexdigest()[:16]
    return f"document_{digest}"


def build_project_id(project_name: str, asset_id: str) -> str:
    digest = hashlib.sha256(f"{project_name}\n{asset_id}".encode("utf-8")).hexdigest()[:16]
    return f"project_{digest}"


def build_repository_id(entity_type: str, entity_id: str, intake_id: str) -> str:
    digest = hashlib.sha256(f"{entity_type}\n{entity_id}\n{intake_id}".encode("utf-8")).hexdigest()[:16]
    return f"repository_{digest}"


def write_repository_outputs(
    documents: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    *,
    documents_path: Path = DEFAULT_DOCUMENTS_REPOSITORY,
    projects_path: Path = DEFAULT_PROJECTS_REPOSITORY,
    audit_path: Path = DEFAULT_REPOSITORY_AUDIT,
) -> None:
    write_json(documents_path, documents)
    write_json(projects_path, projects)
    write_json(audit_path, audit)


def merge_repository_rows(
    existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]], id_key: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Append only stable-ID entities not already present in the JSON repository."""
    merged = [dict(row) for row in existing_rows]
    existing_ids = {str(row.get(id_key) or "") for row in existing_rows if str(row.get(id_key) or "")}
    added: list[dict[str, Any]] = []
    for row in new_rows:
        identifier = str(row.get(id_key) or "")
        if not identifier or identifier in existing_ids:
            continue
        copied = dict(row)
        merged.append(copied)
        added.append(copied)
        existing_ids.add(identifier)
    return merged, added


def load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected {label} JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build simplified diagnostics Asset Repository from intake candidates.")
    parser.add_argument("--input", default=str(DEFAULT_ASSET_INTAKE_OUTPUT))
    parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    parser.add_argument("--audit", default=str(DEFAULT_REPOSITORY_AUDIT))
    args = parser.parse_args()

    documents, projects, audit = build_asset_repository(load_json_array(Path(args.input), "asset intake candidates"))
    write_repository_outputs(
        documents,
        projects,
        audit,
        documents_path=Path(args.documents),
        projects_path=Path(args.projects),
        audit_path=Path(args.audit),
    )
    print(f"wrote repository documents={len(documents)} projects={len(projects)} audit={len(audit)}")


if __name__ == "__main__":
    main()
