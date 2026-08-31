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

from src.repository.asset_repository import DEFAULT_DOCUMENTS_REPOSITORY, DEFAULT_PROJECTS_REPOSITORY


DEFAULT_PROJECT_DOCUMENT_LINKS = Path("data/repository/project_document_links.json")
RELATION_TYPES = {"bid_notice", "award_notice", "contract", "attachment", "other"}


def create_link(
    links: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    *,
    project_id: str,
    document_id: str,
    relation_type: str,
    source: str = "manual",
    created_time: str = "",
) -> dict[str, Any]:
    """Explicitly create one project-document relation without mutating entities."""
    normalized_relation = normalize_relation_type(relation_type)
    project = find_by_id(projects, "project_id", project_id)
    document = find_by_id(documents, "document_id", document_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")
    if document is None:
        raise ValueError(f"Document not found: {document_id}")
    existing = find_existing_link(links, project_id, document_id, normalized_relation)
    if existing is not None:
        return copy.deepcopy(existing)
    return {
        "link_id": build_link_id(project_id, document_id, normalized_relation),
        "project_id": project_id,
        "document_id": document_id,
        "relation_type": normalized_relation,
        "created_time": created_time or utc_now(),
        "source": source.strip() or "manual",
        "source_trace": source_trace_from_entities(project, document),
        "audit": {
            "action": "create_link",
            "project_id": project_id,
            "document_id": document_id,
            "relation_type": normalized_relation,
            "source": source.strip() or "manual",
        },
    }


def get_project_documents(
    links: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    project_id: str,
) -> list[dict[str, Any]]:
    document_by_id = {str(document.get("document_id") or ""): dict(document) for document in documents}
    rows: list[dict[str, Any]] = []
    for link in links:
        if str(link.get("project_id") or "") != project_id:
            continue
        document = document_by_id.get(str(link.get("document_id") or ""))
        if document is None:
            continue
        rows.append({"link": dict(link), "document": document})
    return sorted(rows, key=lambda item: (str(item["link"].get("relation_type") or ""), str(item["link"].get("document_id") or "")))


def get_document_project(
    links: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    document_id: str,
) -> dict[str, Any] | None:
    project_by_id = {str(project.get("project_id") or ""): dict(project) for project in projects}
    matching = [link for link in links if str(link.get("document_id") or "") == document_id]
    if not matching:
        return None
    link = sorted(matching, key=lambda item: str(item.get("created_time") or ""))[0]
    project = project_by_id.get(str(link.get("project_id") or ""))
    if project is None:
        return None
    return {"link": dict(link), "project": project}


def normalize_relation_type(relation_type: str) -> str:
    normalized = relation_type.strip().lower()
    if normalized not in RELATION_TYPES:
        raise ValueError(f"Unsupported relation_type: {relation_type}. Supported: {', '.join(sorted(RELATION_TYPES))}")
    return normalized


def find_by_id(rows: list[dict[str, Any]], id_key: str, value: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get(id_key) or "") == value:
            return dict(row)
    return None


def find_existing_link(
    links: list[dict[str, Any]],
    project_id: str,
    document_id: str,
    relation_type: str,
) -> dict[str, Any] | None:
    for link in links:
        if (
            str(link.get("project_id") or "") == project_id
            and str(link.get("document_id") or "") == document_id
            and str(link.get("relation_type") or "") == relation_type
        ):
            return dict(link)
    return None


def source_trace_from_entities(project: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_source_trace": copy.deepcopy(project.get("source_trace") or {}),
        "document_source_trace": copy.deepcopy(document.get("source_trace") or {}),
    }


def build_link_id(project_id: str, document_id: str, relation_type: str) -> str:
    digest = hashlib.sha256(f"{project_id}\n{document_id}\n{relation_type}".encode("utf-8")).hexdigest()[:16]
    return f"project_document_link_{digest}"


def load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected {label} JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_links(links: list[dict[str, Any]], output_path: Path = DEFAULT_PROJECT_DOCUMENT_LINKS) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly create or query project-document repository links.")
    parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    parser.add_argument("--links", default=str(DEFAULT_PROJECT_DOCUMENT_LINKS))
    parser.add_argument("--project-id", default="")
    parser.add_argument("--document-id", default="")
    parser.add_argument("--relation-type", default="")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()

    projects = load_json_array(Path(args.projects), "projects")
    documents = load_json_array(Path(args.documents), "documents")
    links = load_json_array(Path(args.links), "project document links")
    if args.create:
        if not args.project_id or not args.document_id or not args.relation_type:
            parser.error("--create requires --project-id, --document-id, and --relation-type")
        link = create_link(
            links,
            projects,
            documents,
            project_id=args.project_id,
            document_id=args.document_id,
            relation_type=args.relation_type,
            source=args.source,
        )
        if not find_existing_link(links, args.project_id, args.document_id, normalize_relation_type(args.relation_type)):
            links.append(link)
            write_links(links, Path(args.links))
        print(f"link_id={link['link_id']} project_id={link['project_id']} document_id={link['document_id']}")
    elif args.project_id:
        print(json.dumps(get_project_documents(links, documents, args.project_id), ensure_ascii=False, indent=2))
    elif args.document_id:
        print(json.dumps(get_document_project(links, projects, args.document_id), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(links, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
