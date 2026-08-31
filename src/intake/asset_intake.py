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

from src.discovery_integration.candidate_promoter import build_project_candidate, safe_file_name
from src.document.document_entity import build_document_id, normalize_file_type
from src.review.review_decision import DEFAULT_REVIEW_DECISIONS_OUTPUT, DEFAULT_REVIEW_QUEUE_INPUT


DEFAULT_ASSET_INTAKE_OUTPUT = Path("data/diagnostics/asset_intake_candidates.json")
DEFAULT_INTAKE_AUDIT_OUTPUT = Path("data/diagnostics/intake_audit.json")

DOCUMENT_ENTITY_CANDIDATE = "document_entity_candidate"
PROJECT_CANDIDATE = "project_candidate"


def build_asset_intake(
    review_decisions: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
    *,
    operator: str = "system",
    intake_time: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build diagnostics-only intake candidates from latest ACCEPT decisions."""
    queue_by_asset = index_by_id(review_queue, "asset_id")
    latest_decisions = latest_decision_by_asset(review_decisions)
    timestamp = intake_time or utc_now()
    intake_items: list[dict[str, Any]] = []
    audit_items: list[dict[str, Any]] = []

    for asset_id in sorted(latest_decisions):
        decision = latest_decisions[asset_id]
        if str(decision.get("decision") or "") != "ACCEPT":
            continue
        snapshot = decision_snapshot(decision, queue_by_asset.get(asset_id, {}))
        source_trace = source_trace_from_snapshot(snapshot)
        related_project_id = str(decision.get("related_project_id") or "").strip()
        intake_id = build_intake_id(asset_id, str(decision.get("decision_id") or ""), related_project_id)
        if related_project_id:
            intake_type = DOCUMENT_ENTITY_CANDIDATE
            item = {
                "intake_id": intake_id,
                "asset_id": asset_id,
                "project_id": related_project_id,
                "document_candidate": build_document_candidate(asset_id, related_project_id, snapshot, source_trace),
                "source_trace": source_trace,
                "decision_snapshot": copy.deepcopy(decision),
            }
        else:
            intake_type = PROJECT_CANDIDATE
            item = {
                "intake_id": intake_id,
                "asset_id": asset_id,
                "project_candidate": build_intake_project_candidate(asset_id, snapshot, source_trace),
                "source_trace": source_trace,
                "decision_snapshot": copy.deepcopy(decision),
            }
        intake_items.append(item)
        audit_items.append(
            {
                "intake_id": intake_id,
                "asset_id": asset_id,
                "decision_id": str(decision.get("decision_id") or ""),
                "intake_time": timestamp,
                "intake_type": intake_type,
                "operator": operator.strip() or "system",
            }
        )
    return intake_items, audit_items


def latest_decision_by_asset(review_decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for decision in review_decisions:
        asset_id = str(decision.get("asset_id") or "")
        if not asset_id:
            continue
        current = latest.get(asset_id)
        if current is None or decision_sort_key(decision) >= decision_sort_key(current):
            latest[asset_id] = dict(decision)
    return latest


def decision_sort_key(decision: dict[str, Any]) -> tuple[str, str]:
    return (str(decision.get("review_time") or ""), str(decision.get("decision_id") or ""))


def decision_snapshot(decision: dict[str, Any], queue_item: dict[str, Any]) -> dict[str, Any]:
    snapshot = decision.get("asset_snapshot")
    if isinstance(snapshot, dict):
        return copy.deepcopy(snapshot)
    snapshot = decision.get("snapshot")
    if isinstance(snapshot, dict):
        return copy.deepcopy(snapshot)
    return copy.deepcopy(queue_item)


def build_document_candidate(
    asset_id: str,
    project_id: str,
    snapshot: dict[str, Any],
    source_trace: dict[str, Any],
) -> dict[str, Any]:
    detail = candidate_detail(snapshot)
    source_type = str(detail.get("source_type") or source_trace.get("source_type") or "external")
    source_url = str(detail.get("source_url") or source_trace.get("source_url") or "")
    title = original_title(snapshot)
    file_name = source_url.rsplit("/", 1)[-1] if "/" in source_url else ""
    if not file_name or "." not in file_name:
        file_name = safe_file_name(title) or f"{asset_id}.html"
    return {
        "document_id": build_document_id(source_type, "", source_url, file_name),
        "source_type": source_type,
        "source_path": "",
        "source_url": source_url,
        "file_name": file_name,
        "file_type": normalize_file_type(file_name),
        "metadata": {
            "asset_id": asset_id,
            "project_id": project_id,
            "source_title": title,
            "source_trace": copy.deepcopy(source_trace),
            "intake_status": "candidate_only",
        },
    }


def build_intake_project_candidate(
    asset_id: str,
    snapshot: dict[str, Any],
    source_trace: dict[str, Any],
) -> dict[str, Any]:
    detail = candidate_detail(snapshot)
    candidate = {
        "candidate_id": asset_id,
        "source_type": str(detail.get("source_type") or source_trace.get("source_type") or ""),
        "source_title": original_title(snapshot),
        "source_url": str(detail.get("source_url") or source_trace.get("source_url") or ""),
        "source_trace": copy.deepcopy(source_trace),
    }
    project_candidate = build_project_candidate(candidate)
    metadata = dict(project_candidate.get("metadata") or {})
    metadata.update({"asset_id": asset_id, "intake_status": "candidate_only"})
    project_candidate["metadata"] = metadata
    return project_candidate


def candidate_detail(snapshot: dict[str, Any]) -> dict[str, Any]:
    detail = snapshot.get("candidate_detail")
    return dict(detail) if isinstance(detail, dict) else {}


def source_trace_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    detail = candidate_detail(snapshot)
    trace = detail.get("source_trace")
    if isinstance(trace, dict):
        return copy.deepcopy(trace)
    return {}


def original_title(snapshot: dict[str, Any]) -> str:
    detail = candidate_detail(snapshot)
    return str(detail.get("original_title") or snapshot.get("title") or "")


def build_intake_id(asset_id: str, decision_id: str, related_project_id: str = "") -> str:
    digest = hashlib.sha256(f"{asset_id}\n{decision_id}\n{related_project_id}".encode("utf-8")).hexdigest()[:16]
    return f"intake_{digest}"


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def write_intake_outputs(
    intake_items: list[dict[str, Any]],
    audit_items: list[dict[str, Any]],
    output_path: Path = DEFAULT_ASSET_INTAKE_OUTPUT,
    audit_path: Path = DEFAULT_INTAKE_AUDIT_OUTPUT,
) -> None:
    write_json(output_path, intake_items)
    write_json(audit_path, audit_items)


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
    parser = argparse.ArgumentParser(description="Build diagnostics-only Asset Intake candidates from ACCEPT decisions.")
    parser.add_argument("--review-decisions", default=str(DEFAULT_REVIEW_DECISIONS_OUTPUT))
    parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_ASSET_INTAKE_OUTPUT))
    parser.add_argument("--audit", default=str(DEFAULT_INTAKE_AUDIT_OUTPUT))
    parser.add_argument("--operator", default="system")
    args = parser.parse_args()

    intake_items, audit_items = build_asset_intake(
        load_json_array(Path(args.review_decisions), "review decisions"),
        load_json_array(Path(args.review_queue), "review queue"),
        operator=args.operator,
    )
    write_intake_outputs(intake_items, audit_items, Path(args.output), Path(args.audit))
    print(f"wrote {args.output} intake_items={len(intake_items)} audit_items={len(audit_items)}")


if __name__ == "__main__":
    main()
