from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.matcher.record_matcher import score_pair
from src.project.project_entity import ProjectEntity


DEFAULT_ASSET_CANDIDATES_OUTPUT = Path("data/diagnostics/asset_candidates.json")
DEFAULT_TIANIANCHA_CANDIDATES = Path("data/diagnostics/tianyancha_new_record_candidates.json")
DEFAULT_WEB_CANDIDATES = Path("data/diagnostics/web_enrichment_candidates.json")
DEFAULT_SEARCH_CANDIDATES = Path("data/diagnostics/search_broker_candidates.json")
MATCH_THRESHOLD = 0.68


@dataclass
class AssetCandidate:
    candidate_id: str
    source_type: str
    source_title: str
    source_url: str
    matched_project_id: str
    confidence: float
    status: str
    source_trace: dict[str, Any]


def import_asset_candidates(
    tianyancha_path: Path | None = DEFAULT_TIANIANCHA_CANDIDATES,
    web_path: Path | None = DEFAULT_WEB_CANDIDATES,
    search_path: Path | None = DEFAULT_SEARCH_CANDIDATES,
    projects: list[ProjectEntity | dict[str, Any]] | None = None,
    match_threshold: float = MATCH_THRESHOLD,
) -> list[AssetCandidate]:
    project_index = [_project_record(project) for project in projects or []]
    candidates: list[AssetCandidate] = []
    for source_type, path in [
        ("tianyancha", tianyancha_path),
        ("web_enrichment", web_path),
        ("search_broker", search_path),
    ]:
        if path and Path(path).exists():
            payload = load_json(Path(path))
            for row in external_candidate_rows(payload, source_type):
                candidates.append(normalize_asset_candidate(row, source_type, project_index, match_threshold))
    return candidates


def write_asset_candidates(candidates: list[AssetCandidate], output_path: Path = DEFAULT_ASSET_CANDIDATES_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(candidate) for candidate in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def external_candidate_rows(payload: Any, source_type: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if source_type == "tianyancha":
        keys = ["new_record_candidates", "candidates", "review_candidates"]
    else:
        keys = ["confirmed_suggestions", "review_candidates", "candidates", "new_record_candidates"]
    rows: list[dict[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(dict(item) for item in value if isinstance(item, dict))
    return rows


def normalize_asset_candidate(
    row: dict[str, Any],
    source_type: str,
    project_index: list[dict[str, Any]],
    match_threshold: float = MATCH_THRESHOLD,
) -> AssetCandidate:
    source_title = first_value(row, ["source_title", "title", "project_name", "record_title", "query", "content"])
    source_url = first_value(row, ["source_url", "url", "detail_url", "link"])
    candidate_id = first_value(row, ["candidate_id", "id"]) or build_candidate_id(source_type, source_title, source_url)
    matched_project_id, match_score = best_project_match(row, source_title, project_index, match_threshold)
    raw_confidence = numeric_first_value(row, ["confidence", "score", "match_score"])
    confidence = round(max(raw_confidence, match_score), 4)
    status = "project_asset_candidate" if matched_project_id else "new_project_candidate"
    return AssetCandidate(
        candidate_id=candidate_id,
        source_type=source_type,
        source_title=source_title,
        source_url=source_url,
        matched_project_id=matched_project_id,
        confidence=confidence,
        status=status,
        source_trace=build_source_trace(row, source_type, source_url),
    )


def build_source_trace(row: dict[str, Any], source_type: str, source_url: str) -> dict[str, Any]:
    supplied_trace = row.get("source_trace")
    trace: dict[str, Any] = dict(supplied_trace) if isinstance(supplied_trace, dict) else {}
    trace.update({
        "source_type": source_type,
        "source_name": source_name_for_type(source_type),
        "source_url": source_url,
        "source_file": first_value(row, ["source_file", "file", "input_file"]) or str(trace.get("source_file") or ""),
        "discovered_time": first_value(row, ["discovered_time", "generated_at", "publish_date", "date"]) or utc_now(),
        "pipeline_stage": "asset_discovery_integration",
    })
    extracted = {
        key: first_value(row, [key])
        for key in (
            "project_name", "customer", "project_number", "content", "budget",
            "bid_open_time", "winner", "award_amount", "doc_type", "note",
            "sequence", "business_sequence", "publish_date", "business_group_id", "award_detail_id",
        )
    }
    trace["extracted_fields"] = extracted
    return trace


def source_name_for_type(source_type: str) -> str:
    return {
        "tianyancha": "tianyancha_new_record_candidates",
        "web_enrichment": "web_enrichment_candidates",
        "search_broker": "search_broker_candidates",
    }.get(source_type, source_type)


def best_project_match(
    row: dict[str, Any], source_title: str, project_index: list[dict[str, Any]], match_threshold: float
) -> tuple[str, float]:
    candidate_record = {
        "project_name": first_value(row, ["project_name"]) or source_title,
        "customer": first_value(row, ["customer", "buyer", "purchaser"]),
        "source_file": first_value(row, ["source_file"]),
        "content": first_value(row, ["content", "source_title", "title"]),
        "note": first_value(row, ["note", "notice_type"]),
        "bid_open_time": first_value(row, ["bid_open_time", "publish_date", "date"]),
    }
    best_id = ""
    best_score = 0.0
    for project in project_index:
        score, _reasons = score_pair(candidate_record, project["record"])
        if score > best_score:
            best_id = project["project_id"]
            best_score = score
    if best_score >= match_threshold:
        return best_id, round(best_score, 4)
    return "", round(best_score, 4)


def _project_record(project: ProjectEntity | dict[str, Any]) -> dict[str, Any]:
    if isinstance(project, ProjectEntity):
        records = project.records
        organizations = project.organizations
        project_id = project.project_id
        project_name = project.project_name
    else:
        records = [dict(item) for item in project.get("records", []) if isinstance(item, dict)]
        organizations = list(project.get("organizations") or [])
        project_id = str(project.get("project_id") or "")
        project_name = str(project.get("project_name") or "")
    customer = first_value(records[0], ["customer"]) if records else (str(organizations[0]) if organizations else "")
    source_file = first_value(records[0], ["source_file"]) if records else ""
    content = first_value(records[0], ["content"]) if records else ""
    note = first_value(records[0], ["note"]) if records else ""
    bid_open_time = first_value(records[0], ["bid_open_time"]) if records else ""
    return {
        "project_id": project_id,
        "record": {
            "project_name": project_name,
            "customer": customer,
            "source_file": source_file,
            "content": content,
            "note": note,
            "bid_open_time": bid_open_time,
        },
    }


def first_value(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def numeric_first_value(row: dict[str, Any], keys: list[str]) -> float:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def build_candidate_id(source_type: str, source_title: str, source_url: str) -> str:
    digest = hashlib.sha256(f"{source_type}\n{source_title}\n{source_url}".encode("utf-8")).hexdigest()[:16]
    return f"asset_candidate_{digest}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_projects(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    rows = payload.get("projects") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected project JSON array or object with projects: {path}")
    return [dict(item) for item in rows if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import external discovery candidates into Asset Candidate diagnostics.")
    parser.add_argument("--tianyancha", default=str(DEFAULT_TIANIANCHA_CANDIDATES))
    parser.add_argument("--web", default=str(DEFAULT_WEB_CANDIDATES))
    parser.add_argument("--search", default=str(DEFAULT_SEARCH_CANDIDATES))
    parser.add_argument("--projects", default="", help="Optional ProjectEntity JSON array path for matching.")
    parser.add_argument("--output", default=str(DEFAULT_ASSET_CANDIDATES_OUTPUT))
    parser.add_argument("--match-threshold", type=float, default=MATCH_THRESHOLD)
    args = parser.parse_args()

    projects = load_projects(Path(args.projects)) if args.projects else []
    candidates = import_asset_candidates(
        Path(args.tianyancha),
        Path(args.web),
        Path(args.search),
        projects,
        args.match_threshold,
    )
    write_asset_candidates(candidates, Path(args.output))
    print(f"wrote {args.output} candidates={len(candidates)}")


if __name__ == "__main__":
    main()
