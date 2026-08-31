from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.project_relation.project_document_relation import DEFAULT_PROJECT_DOCUMENT_LINKS
from src.repository.asset_repository import DEFAULT_DOCUMENTS_REPOSITORY, DEFAULT_PROJECTS_REPOSITORY


class RepositoryQueryService:
    """Read-only aggregation over simplified asset repository JSON files."""

    def __init__(
        self,
        projects_path: Path | str = DEFAULT_PROJECTS_REPOSITORY,
        documents_path: Path | str = DEFAULT_DOCUMENTS_REPOSITORY,
        links_path: Path | str = DEFAULT_PROJECT_DOCUMENT_LINKS,
    ) -> None:
        self.projects = load_json_array(Path(projects_path), "projects")
        self.documents = load_json_array(Path(documents_path), "documents")
        self.links = load_json_array(Path(links_path), "project document links")
        self._projects_by_id = index_by_id(self.projects, "project_id")
        self._documents_by_id = index_by_id(self.documents, "document_id")

    def get_project_asset(self, project_id: str) -> dict[str, Any]:
        project = self._projects_by_id.get(project_id)
        project_links = [dict(link) for link in self.links if str(link.get("project_id") or "") == project_id]
        project_links.sort(key=link_sort_key)
        documents = []
        for link in project_links:
            document = self._documents_by_id.get(str(link.get("document_id") or ""))
            if document is None:
                continue
            documents.append(
                {
                    "document": dict(document),
                    "relation": relation_view(link),
                    "relation_type": str(link.get("relation_type") or ""),
                    "source_trace": dict(link.get("source_trace") or {}),
                }
            )
        return {
            "project_id": project_id,
            "project": dict(project or {}),
            "project_fields": project_fields(project or {}),
            "award_details": project_award_details(project or {}),
            "documents": documents,
            "relations": [relation_view(link) for link in project_links],
            "source_trace": dict((project or {}).get("source_trace") or {}),
        }

    def get_document_asset(self, document_id: str) -> dict[str, Any]:
        document = self._documents_by_id.get(document_id)
        document_links = [dict(link) for link in self.links if str(link.get("document_id") or "") == document_id]
        document_links.sort(key=link_sort_key)
        projects = []
        for link in document_links:
            project = self._projects_by_id.get(str(link.get("project_id") or ""))
            if project is None:
                continue
            projects.append(
                {
                    "project": dict(project),
                    "relation": relation_view(link),
                    "relation_type": str(link.get("relation_type") or ""),
                    "source_trace": dict(link.get("source_trace") or {}),
                }
            )
        return {
            "document_id": document_id,
            "document": dict(document or {}),
            "projects": projects,
            "relations": [relation_view(link) for link in document_links],
            "source_trace": dict((document or {}).get("source_trace") or {}),
        }

    def get_project_timeline(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for link in self.links:
            if str(link.get("project_id") or "") != project_id:
                continue
            rows.append(
                {
                    "relation_type": str(link.get("relation_type") or ""),
                    "document_id": str(link.get("document_id") or ""),
                    "created_time": str(link.get("created_time") or ""),
                    "link_id": str(link.get("link_id") or ""),
                    "source_trace": dict(link.get("source_trace") or {}),
                }
            )
        return sorted(rows, key=lambda item: (item["created_time"], item["relation_type"], item["document_id"]))

    def search_projects_by_name(self, query: str) -> list[dict[str, Any]]:
        """Compatibility wrapper for the Phase34 project-name lookup."""
        normalized = normalize_search_text(query)
        if not normalized:
            return []
        return [
            row
            for row in self.search_projects(query)
            if normalized in normalize_search_text(str(row.get("project_name") or ""))
        ]

    def list_projects(self) -> list[dict[str, Any]]:
        """List every project through the compatible field view, newest first."""
        rows = [self._project_summary(project) for project in self.projects]
        return sorted(
            rows,
            key=lambda row: (str(row.get("updated_time") or ""), str(row.get("project_id") or "")),
            reverse=True,
        )

    def search_projects(self, keyword: str) -> list[dict[str, Any]]:
        """Search project name, customer, winner and number using deterministic string rules."""
        normalized_keyword = normalize_search_text(keyword)
        if not normalized_keyword:
            return []

        matches: list[tuple[tuple[int, int], str, dict[str, Any]]] = []
        search_fields = ("project_name", "customer", "winner_company", "project_number")
        for project in self.projects:
            fields = project_fields(project)
            best_rank: tuple[int, int] | None = None
            search_values = {
                "project_name": [fields["project_name"]],
                "customer": [fields["customer"]],
                "winner_company": [
                    fields["winner_company"],
                    *(str(detail.get("winner") or "") for detail in project_award_details(project)),
                ],
                "project_number": [fields["project_number"]],
            }
            for field_priority, field_name in enumerate(search_fields):
                for value in search_values[field_name]:
                    match_rank = text_match_rank(normalize_search_text(value), normalized_keyword)
                    if match_rank is None:
                        continue
                    rank = (match_rank, field_priority)
                    if best_rank is None or rank < best_rank:
                        best_rank = rank
            if best_rank is None:
                continue
            summary = self._project_summary(project)
            matches.append((best_rank, fields["updated_time"], summary))

        # Stable two-pass sort keeps updated_time descending inside each semantic rank.
        matches.sort(key=lambda item: item[1], reverse=True)
        matches.sort(key=lambda item: item[0])
        return [item[2] for item in matches]

    def _project_summary(self, project: dict[str, Any]) -> dict[str, Any]:
        fields = project_fields(project)
        project_id = str(project.get("project_id") or "")
        return {
            **fields,
            "project_id": project_id,
            "document_count": sum(
                1 for link in self.links if str(link.get("project_id") or "") == project_id
            ),
        }


PROJECT_FIELD_ALIASES = {
    "project_name": ("project_name", "name", "title"),
    "customer": ("customer", "customer_name", "client"),
    "winner_company": ("winner_company", "winner", "supplier"),
    "project_number": ("project_number", "project_no"),
    "content": ("content", "project_content"),
    "budget": ("budget", "budget_amount"),
    "bid_open_time": ("bid_open_time", "bid_time"),
    "award_amount": ("award_amount", "winning_amount"),
    "business_sequence": ("business_sequence", "sequence"),
    "publish_date": ("publish_date",),
    "status": ("status",),
}


def project_fields(project: dict[str, Any]) -> dict[str, str]:
    """Return one compatible read view without mutating historical entities."""
    fields = {
        field_name: read_project_field(project, field_name)
        for field_name in PROJECT_FIELD_ALIASES
    }
    details = project_award_details(project)
    if details:
        fields["winner_company"] = "；".join(unique_nonempty(str(item.get("winner") or "") for item in details))
        fields["award_amount"] = "；".join(unique_nonempty(str(item.get("award_amount") or "") for item in details))
    return fields | {"updated_time": project_updated_time(project)}


def project_award_details(project: dict[str, Any]) -> list[dict[str, Any]]:
    details = project.get("award_details")
    if not isinstance(details, list):
        return []
    return [dict(item) for item in details if isinstance(item, dict)]


def unique_nonempty(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def read_project_field(project: dict[str, Any], field_name: str) -> str:
    for source in project_field_sources(project):
        for alias in PROJECT_FIELD_ALIASES[field_name]:
            value = source.get(alias)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def project_field_sources(project: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict) and value not in sources:
            sources.append(value)

    add(project)
    add(project.get("metadata"))
    add(project.get("extracted_fields"))
    trace = project.get("source_trace")
    add(trace)
    if isinstance(trace, dict):
        add(trace.get("metadata"))
        add(trace.get("extracted_fields"))

    for snapshot_name in ("source_snapshot", "snapshot"):
        snapshot = project.get(snapshot_name)
        add(snapshot)
        if isinstance(snapshot, dict):
            add(snapshot.get("metadata"))
            add(snapshot.get("extracted_fields"))
            snapshot_trace = snapshot.get("source_trace")
            add(snapshot_trace)
            if isinstance(snapshot_trace, dict):
                add(snapshot_trace.get("metadata"))
                add(snapshot_trace.get("extracted_fields"))

    decision = project.get("review_decision_snapshot")
    if isinstance(decision, dict):
        for snapshot_name in ("asset_snapshot", "snapshot"):
            snapshot = decision.get(snapshot_name)
            add(snapshot)
            if not isinstance(snapshot, dict):
                continue
            add(snapshot.get("metadata"))
            add(snapshot.get("extracted_fields"))
            detail = snapshot.get("candidate_detail")
            add(detail)
            if isinstance(detail, dict):
                add(detail.get("metadata"))
                add(detail.get("extracted_fields"))
                detail_trace = detail.get("source_trace")
                add(detail_trace)
                if isinstance(detail_trace, dict):
                    add(detail_trace.get("metadata"))
                    add(detail_trace.get("extracted_fields"))
    return sources


def project_updated_time(project: dict[str, Any]) -> str:
    for field_name in ("updated_time", "status_updated_time", "modified_time", "created_time"):
        value = project.get(field_name)
        if value:
            return str(value)
    return ""


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def text_match_rank(value: str, keyword: str) -> int | None:
    if not value:
        return None
    if value == keyword:
        return 0
    if value.startswith(keyword):
        return 1
    if keyword in value:
        return 2
    return None


def relation_view(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": str(link.get("link_id") or ""),
        "project_id": str(link.get("project_id") or ""),
        "document_id": str(link.get("document_id") or ""),
        "relation_type": str(link.get("relation_type") or ""),
        "created_time": str(link.get("created_time") or ""),
        "source": str(link.get("source") or ""),
        "source_trace": dict(link.get("source_trace") or {}),
    }


def link_sort_key(link: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(link.get("created_time") or ""),
        str(link.get("relation_type") or ""),
        str(link.get("document_id") or ""),
    )


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected {label} JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only query over simplified asset repository.")
    parser.add_argument("--project-id", default="", help="Return full project asset view.")
    parser.add_argument("--document-id", default="", help="Return one document asset view.")
    parser.add_argument("--timeline", default="", help="Return project document timeline for project_id.")
    parser.add_argument("--projects", default=str(DEFAULT_PROJECTS_REPOSITORY))
    parser.add_argument("--documents", default=str(DEFAULT_DOCUMENTS_REPOSITORY))
    parser.add_argument("--links", default=str(DEFAULT_PROJECT_DOCUMENT_LINKS))
    args = parser.parse_args()

    selectors = [args.project_id, args.document_id, args.timeline]
    if sum(bool(selector) for selector in selectors) != 1:
        parser.error("Specify exactly one selector: --project-id, --document-id, or --timeline.")
    service = RepositoryQueryService(args.projects, args.documents, args.links)
    if args.project_id:
        result: Any = service.get_project_asset(args.project_id)
    elif args.document_id:
        result = service.get_document_asset(args.document_id)
    else:
        result = service.get_project_timeline(args.timeline)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
