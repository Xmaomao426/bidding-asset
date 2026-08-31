from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.project.project_entity import ProjectEntity
from src.project_asset.project_asset_view import build_project_asset_view


DEFAULT_PROJECT_DISCOVERY_REPORTS_OUTPUT = Path("data/diagnostics/project_discovery_reports.json")
REQUIRED_DOCUMENTS = ["bid_notice", "award_notice", "contract"]


def build_project_discovery_report(project_or_view: ProjectEntity | dict[str, Any]) -> dict[str, Any]:
    view = build_project_asset_view(project_or_view) if isinstance(project_or_view, ProjectEntity) else dict(project_or_view)
    documents = [dict(item) for item in view.get("documents", []) if isinstance(item, dict)]
    records = [dict(item) for item in view.get("records", []) if isinstance(item, dict)]
    timeline = dict(view.get("timeline_summary") or {})
    present_documents = _present_document_types(documents, records)
    missing_documents = [item for item in REQUIRED_DOCUMENTS if item not in present_documents]
    score_detail = _score_detail(documents, records, timeline)
    return {
        "project_id": str(view.get("project_id") or ""),
        "project_name": str(view.get("project_name") or ""),
        "missing_documents": missing_documents,
        "asset_score": score_detail["asset_score"],
        "score_detail": score_detail,
    }


def build_project_discovery_reports(projects_or_views: list[ProjectEntity | dict[str, Any]]) -> list[dict[str, Any]]:
    return [build_project_discovery_report(item) for item in projects_or_views]


def write_project_discovery_reports(
    reports: list[dict[str, Any]], output_path: Path = DEFAULT_PROJECT_DISCOVERY_REPORTS_OUTPUT
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


def _present_document_types(documents: list[dict[str, Any]], records: list[dict[str, Any]]) -> set[str]:
    present: set[str] = set()
    for item in documents + records:
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("file_name", "record_title", "document_id", "record_id", "source_url", "doc_type")
        ).lower()
        if _contains_any(haystack, ["bid", "tender", "\u62db\u6807", "\u91c7\u8d2d\u516c\u544a"]):
            present.add("bid_notice")
        if _contains_any(haystack, ["award", "winner", "win_notice", "\u4e2d\u6807", "\u6210\u4ea4"]):
            present.add("award_notice")
        if _contains_any(haystack, ["contract", "\u5408\u540c"]):
            present.add("contract")
    return present


def _contains_any(value: str, candidates: list[str]) -> bool:
    return any(candidate.lower() in value for candidate in candidates)


def _score_detail(documents: list[dict[str, Any]], records: list[dict[str, Any]], timeline: dict[str, Any]) -> dict[str, Any]:
    document_count = len(documents)
    record_count = len(records)
    time_fields = ["announcement_time", "bid_open_time", "award_time"]
    completed_time_fields = [field for field in time_fields if str(timeline.get(field) or "").strip()]
    document_score = min(document_count, 3) / 3 * 40
    record_score = min(record_count, 3) / 3 * 30
    time_score = len(completed_time_fields) / len(time_fields) * 30
    asset_score = round(document_score + record_score + time_score, 2)
    return {
        "asset_score": asset_score,
        "document_count": document_count,
        "record_count": record_count,
        "completed_time_fields": completed_time_fields,
        "document_score": round(document_score, 2),
        "record_score": round(record_score, 2),
        "time_score": round(time_score, 2),
    }


def load_asset_views(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("project_asset_views") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected asset view JSON array or object with project_asset_views: {path}")
    return [dict(item) for item in rows if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate read-only project discovery reports from asset views.")
    parser.add_argument("--input", default="data/diagnostics/project_asset_views.json", help="Input project asset views JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_PROJECT_DISCOVERY_REPORTS_OUTPUT), help="Output discovery reports JSON path.")
    args = parser.parse_args()

    views = load_asset_views(Path(args.input))
    reports = build_project_discovery_reports(views)
    write_project_discovery_reports(reports, Path(args.output))
    print(f"wrote {args.output} reports={len(reports)}")


if __name__ == "__main__":
    main()
