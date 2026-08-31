from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from difflib import SequenceMatcher

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.lifecycle.asset_lifecycle import DEFAULT_ASSET_LIFECYCLE_OUTPUT, load_json_array


DEFAULT_DEDUPED_CANDIDATES_INPUT = Path("data/diagnostics/asset_candidates_deduped.json")
DEFAULT_PROMOTED_CANDIDATES_INPUT = Path("data/diagnostics/promoted_candidates.json")
DEFAULT_REVIEW_QUEUE_OUTPUT = Path("data/diagnostics/review_queue.json")

P1 = "P1"
P2 = "P2"
P3 = "P3"


def build_review_queue(
    deduped_candidates: list[dict[str, Any]],
    lifecycles: list[dict[str, Any]],
    promoted_candidates: list[dict[str, Any]],
    projects: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a diagnostic-only human review queue from current pipeline snapshots."""
    candidates = candidate_index(deduped_candidates)
    lifecycle_by_id = index_by_id(lifecycles, "asset_id")
    promotions_by_id = index_by_id(promoted_candidates, "candidate_id")
    project_index = normalize_projects(projects or [])

    asset_ids = sorted(set(candidates) | set(lifecycle_by_id) | set(promotions_by_id))
    items = [
        build_review_item(
            asset_id,
            candidates.get(asset_id, {}),
            lifecycle_by_id.get(asset_id, {}),
            promotions_by_id.get(asset_id, {}),
            project_index,
        )
        for asset_id in asset_ids
    ]
    return sorted(items, key=lambda item: (priority_rank(item["priority"]), -item["confidence"], item["asset_id"]))


def build_review_item(
    asset_id: str,
    candidate: dict[str, Any],
    lifecycle: dict[str, Any],
    promotion: dict[str, Any],
    projects: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    lifecycle_status = str(lifecycle.get("status") or "")
    confidence = confidence_of(candidate, promotion, lifecycle)
    source_type = str(candidate.get("source_type") or promotion.get("source") or source_type_from_lifecycle(lifecycle))
    title = title_of(candidate, promotion)
    priority, reason = priority_for(lifecycle_status, confidence)
    item = {
        "asset_id": asset_id,
        "source_type": source_type,
        "title": title,
        "lifecycle_status": lifecycle_status,
        "confidence": confidence,
        "priority": priority,
        "reason": reason,
        "candidate_detail": candidate_detail(candidate, promotion),
    }
    hint = similar_candidate_hint(title, candidate, projects or [])
    if hint["possible_related_projects"]:
        item["similar_candidate_hint"] = hint
    return item


def candidate_detail(candidate: dict[str, Any], promotion: dict[str, Any]) -> dict[str, Any]:
    source_trace = source_trace_of(candidate, promotion)
    original_title = str(candidate.get("source_title") or "")
    if not original_title:
        original_title = title_of(candidate, promotion)
    return {
        "source_type": str(candidate.get("source_type") or promotion.get("source") or source_trace.get("source_type") or ""),
        "source_url": str(candidate.get("source_url") or source_trace.get("source_url") or ""),
        "source_file": str(candidate.get("source_file") or source_trace.get("source_file") or ""),
        "discovered_time": str(candidate.get("discovered_time") or source_trace.get("discovered_time") or ""),
        "source_trace": dict(source_trace),
        "original_title": original_title,
        "original_row": str(candidate.get("source_row") or source_trace.get("source_row") or ""),
    }


def source_trace_of(candidate: dict[str, Any], promotion: dict[str, Any]) -> dict[str, Any]:
    trace = candidate.get("source_trace")
    if isinstance(trace, dict):
        return dict(trace)
    trace = promotion.get("source_trace")
    if isinstance(trace, dict):
        return dict(trace)
    document_candidate = promotion.get("document_candidate")
    if isinstance(document_candidate, dict):
        metadata = document_candidate.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("source_trace"), dict):
            return dict(metadata["source_trace"])
    return {}


def similar_candidate_hint(
    title: str,
    candidate: dict[str, Any],
    projects: list[dict[str, str]],
    threshold: float = 0.6,
    limit: int = 5,
) -> dict[str, Any]:
    candidate_project_name = str((candidate.get("metadata") or {}).get("project_name") or candidate.get("project_name") or title)
    matches: list[dict[str, Any]] = []
    for project in projects:
        project_name = str(project.get("project_name") or "")
        if not project_name:
            continue
        score = max(text_similarity(title, project_name), text_similarity(candidate_project_name, project_name))
        if score >= threshold:
            matches.append(
                {
                    "project_id": str(project.get("project_id") or ""),
                    "project_name": project_name,
                    "similarity_score": round(score, 4),
                }
            )
    matches.sort(key=lambda item: (-float(item["similarity_score"]), item["project_id"]))
    return {
        "hint_type": "diagnostic_only",
        "rule": "title/project_name similarity",
        "possible_related_projects": matches[:limit],
    }


def text_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def normalize_text(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def normalize_projects(projects: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for project in projects:
        project_id = str(project.get("project_id") or "")
        project_name = str(project.get("project_name") or "")
        if project_id or project_name:
            normalized.append({"project_id": project_id, "project_name": project_name})
    return normalized


def candidate_index(deduped_candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for candidate in deduped_candidates:
        add_candidate(index, candidate)
        for merged in candidate.get("merged_sources") or []:
            if isinstance(merged, dict):
                add_candidate(index, merged)
    return index


def add_candidate(index: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    asset_id = str(candidate.get("candidate_id") or "")
    if not asset_id:
        return
    existing = dict(index.get(asset_id) or {})
    existing.update({key: value for key, value in candidate.items() if value not in (None, "", [], {})})
    index[asset_id] = existing


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def confidence_of(candidate: dict[str, Any], promotion: dict[str, Any], lifecycle: dict[str, Any]) -> float:
    for row in (candidate, promotion):
        value = row.get("confidence")
        if value is not None:
            try:
                return round(float(value), 4)
            except (TypeError, ValueError):
                pass
    history = lifecycle.get("history") or []
    if history and isinstance(history[-1], dict):
        try:
            return round(float((history[-1].get("source") or {}).get("confidence") or 0), 4)
        except (TypeError, ValueError):
            pass
    return 0.0


def title_of(candidate: dict[str, Any], promotion: dict[str, Any]) -> str:
    title = str(candidate.get("source_title") or "")
    if title:
        return title
    document_candidate = promotion.get("document_candidate")
    if isinstance(document_candidate, dict):
        metadata = document_candidate.get("metadata")
        if isinstance(metadata, dict):
            return str(metadata.get("source_title") or "")
        return str(document_candidate.get("file_name") or "")
    return ""


def source_type_from_lifecycle(lifecycle: dict[str, Any]) -> str:
    history = lifecycle.get("history") or []
    if history and isinstance(history[-1], dict):
        source = history[-1].get("source") or {}
        if isinstance(source, dict):
            return str(source.get("source_type") or "")
    return ""


def priority_for(lifecycle_status: str, confidence: float) -> tuple[str, str]:
    if lifecycle_status == "PROMOTION_READY" and confidence >= 0.8:
        return P1, "PROMOTION_READY and confidence >= 0.8"
    if lifecycle_status == "DISCOVERED" and confidence >= 0.6:
        return P2, "DISCOVERED and confidence >= 0.6"
    status_label = lifecycle_status or "NO_LIFECYCLE"
    return P3, f"diagnostic asset: lifecycle_status={status_label}, confidence={confidence:.4f}"


def priority_rank(priority: str) -> int:
    return {P1: 1, P2: 2, P3: 3}.get(priority, 99)


def write_review_queue(items: list[dict[str, Any]], output_path: Path = DEFAULT_REVIEW_QUEUE_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_projects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("projects") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected projects JSON array or object with projects: {path}")
    return [dict(item) for item in rows if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a diagnostic-only Asset Review Queue.")
    parser.add_argument("--deduped-candidates", default=str(DEFAULT_DEDUPED_CANDIDATES_INPUT))
    parser.add_argument("--lifecycle", default=str(DEFAULT_ASSET_LIFECYCLE_OUTPUT))
    parser.add_argument("--promoted-candidates", default=str(DEFAULT_PROMOTED_CANDIDATES_INPUT))
    parser.add_argument("--projects", default="", help="Optional ProjectEntity JSON array for diagnostic similarity hints.")
    parser.add_argument("--output", default=str(DEFAULT_REVIEW_QUEUE_OUTPUT))
    args = parser.parse_args()

    items = build_review_queue(
        load_json_array(Path(args.deduped_candidates)),
        load_json_array(Path(args.lifecycle)),
        load_json_array(Path(args.promoted_candidates)),
        load_projects(Path(args.projects)) if args.projects else [],
    )
    write_review_queue(items, Path(args.output))
    print(f"wrote {args.output} review_items={len(items)}")


if __name__ == "__main__":
    main()
