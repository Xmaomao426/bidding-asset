from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.document.document_entity import DocumentEntity, document_entity_from_mapping
from src.matcher.record_matcher import MatchResult

try:
    from .project_entity import ProjectEntity
except ImportError:  # pragma: no cover - direct script execution fallback.
    from src.project.project_entity import ProjectEntity


DEFAULT_PROJECT_CANDIDATES_OUTPUT = Path("data/diagnostics/project_candidates.json")


class ProjectManager:
    def __init__(self) -> None:
        self._projects: dict[str, ProjectEntity] = {}

    def create_project(self, project_name: str, project_id: str | None = None) -> ProjectEntity:
        project_id = project_id or build_project_id(project_name)
        project = ProjectEntity(project_id=project_id, project_name=project_name)
        self._projects[project_id] = project
        return project

    def add_record(self, project_id: str, record: dict[str, Any]) -> ProjectEntity:
        project = self.get_project(project_id)
        record_id = str(record.get("source_document_id") or record.get("source_file") or "")
        if record_id and any(str(item.get("source_document_id") or item.get("source_file") or "") == record_id for item in project.records):
            return project
        project.records.append(record)
        customer = str(record.get("customer") or "").strip()
        if customer and customer not in project.organizations:
            project.organizations.append(customer)
        date_value = str(record.get("bid_open_time") or "").strip()
        if date_value:
            project.timeline.append({"type": str(record.get("doc_type") or "record"), "date": date_value})
        return project

    def add_document(self, project_id: str, document: DocumentEntity | dict[str, Any]) -> ProjectEntity:
        project = self.get_project(project_id)
        entity = document if isinstance(document, DocumentEntity) else document_entity_from_mapping(document)
        if entity.document_id and any(item.document_id == entity.document_id for item in project.documents):
            return project
        project.documents.append(entity)
        return project

    def get_project(self, project_id: str) -> ProjectEntity:
        return self._projects[project_id]


def build_project_id(project_name: str) -> str:
    digest = hashlib.sha256(project_name.strip().encode("utf-8")).hexdigest()[:16]
    return f"project_{digest}"


def compact_candidate_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_document_id": record.get("source_document_id", ""),
        "source_file": record.get("source_file", ""),
        "customer": record.get("customer", ""),
        "project_name": record.get("project_name", ""),
        "bid_open_time": record.get("bid_open_time", ""),
    }


def candidate_id(records: list[dict[str, Any]]) -> str:
    parts = sorted(str(record.get("source_document_id") or record.get("source_file") or record.get("project_name") or "") for record in records)
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"candidate_{digest}"


def match_result_from_dict(payload: dict[str, Any]) -> MatchResult:
    return MatchResult(
        source_record=payload.get("source_record") or {},
        target_record=payload.get("target_record") or {},
        match_score=float(payload.get("match_score") or 0),
        match_reason=list(payload.get("match_reason") or []),
    )


def build_project_candidates(matches: list[MatchResult]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        records = [compact_candidate_record(match.source_record), compact_candidate_record(match.target_record)]
        current_id = candidate_id(records)
        if current_id in seen:
            continue
        seen.add(current_id)
        candidates.append(
            {
                "project_candidate_id": current_id,
                "matched_records": records,
                "confidence": round(match.match_score, 4),
                "reason": match.match_reason,
            }
        )
    return sorted(candidates, key=lambda item: item["confidence"], reverse=True)


def load_match_results(path: Path) -> list[MatchResult]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [match_result_from_dict(item) for item in payload]


def write_project_candidates(candidates: list[dict[str, Any]], output_path: Path = DEFAULT_PROJECT_CANDIDATES_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate project candidates from matcher diagnostic results.")
    parser.add_argument("--matches", default="data/diagnostics/match_results.json", help="Input match_results JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_PROJECT_CANDIDATES_OUTPUT), help="Output project candidates JSON path.")
    args = parser.parse_args()

    matches = load_match_results(Path(args.matches))
    candidates = build_project_candidates(matches)
    write_project_candidates(candidates, Path(args.output))
    print(f"wrote {args.output} candidates={len(candidates)}")


if __name__ == "__main__":
    main()
