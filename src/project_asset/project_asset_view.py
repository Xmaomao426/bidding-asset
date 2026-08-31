from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.document.document_entity import DocumentEntity, document_entity_from_mapping
from src.project.project_entity import ProjectEntity


DEFAULT_PROJECT_ASSET_VIEWS_OUTPUT = Path("data/diagnostics/project_asset_views.json")


def build_project_asset_view(project: ProjectEntity) -> dict[str, Any]:
    records = [dict(record) for record in project.records]
    return {
        "project_id": project.project_id,
        "project_name": project.project_name,
        "documents": [_document_view(document) for document in project.documents],
        "records": [_record_view(record) for record in records],
        "timeline_summary": _timeline_summary(records, project.timeline),
    }


def build_project_asset_views(projects: list[ProjectEntity]) -> list[dict[str, Any]]:
    return [build_project_asset_view(project) for project in projects]


def write_project_asset_views(views: list[dict[str, Any]], output_path: Path = DEFAULT_PROJECT_ASSET_VIEWS_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding="utf-8")


def _document_view(document: DocumentEntity | dict[str, Any]) -> dict[str, str]:
    entity = document if isinstance(document, DocumentEntity) else document_entity_from_mapping(document)
    return {
        "document_id": entity.document_id,
        "file_name": entity.file_name,
        "source_type": entity.source_type,
        "source_url": entity.source_url,
    }


def _record_view(record: dict[str, Any]) -> dict[str, str]:
    record_id = str(record.get("record_id") or record.get("source_document_id") or record.get("source_file") or "")
    return {
        "record_id": record_id,
        "record_title": _first_value(record, ["record_title", "title", "project_name", "source_file"]),
        "extraction_time": _first_value(record, ["extraction_time", "extracted_time", "created_time", "capture_time"]),
    }


def _timeline_summary(records: list[dict[str, Any]], project_timeline: list[dict[str, str]]) -> dict[str, str]:
    return {
        "announcement_time": _first_from_records(records, ["announcement_time", "announcement_date", "publish_time", "publish_date", "notice_time"])
        or _first_timeline_date(project_timeline, ["公告", "announcement", "notice"]),
        "bid_open_time": _first_from_records(records, ["bid_open_time", "open_time", "bid_time"])
        or _first_timeline_date(project_timeline, ["开标", "bid_open", "open"]),
        "award_time": _first_from_records(records, ["award_time", "award_date", "win_time", "winner_time"])
        or _first_timeline_date(project_timeline, ["中标", "award", "winner"]),
    }


def _first_from_records(records: list[dict[str, Any]], keys: list[str]) -> str:
    for record in records:
        value = _first_value(record, keys)
        if value:
            return value
    return ""


def _first_value(record: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _first_timeline_date(timeline: list[dict[str, str]], markers: list[str]) -> str:
    lowered_markers = [marker.lower() for marker in markers]
    for item in timeline:
        item_type = str(item.get("type") or "").lower()
        if any(marker in item_type for marker in lowered_markers):
            return str(item.get("date") or "").strip()
    return ""


def _project_from_dict(payload: dict[str, Any]) -> ProjectEntity:
    documents = [document_entity_from_mapping(item) for item in payload.get("documents", []) if isinstance(item, dict)]
    return ProjectEntity(
        project_id=str(payload.get("project_id") or ""),
        project_name=str(payload.get("project_name") or ""),
        records=[dict(item) for item in payload.get("records", []) if isinstance(item, dict)],
        documents=documents,
        organizations=list(payload.get("organizations") or []),
        timeline=list(payload.get("timeline") or []),
    )


def load_projects(path: Path) -> list[ProjectEntity]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("projects") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected project JSON array or object with projects: {path}")
    return [_project_from_dict(item) for item in rows if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate read-only project asset views.")
    parser.add_argument("--input", required=True, help="Input ProjectEntity JSON array path.")
    parser.add_argument("--output", default=str(DEFAULT_PROJECT_ASSET_VIEWS_OUTPUT), help="Output project asset views JSON path.")
    args = parser.parse_args()

    projects = load_projects(Path(args.input))
    views = build_project_asset_views(projects)
    write_project_asset_views(views, Path(args.output))
    print(f"wrote {args.output} views={len(views)}")


if __name__ == "__main__":
    main()
