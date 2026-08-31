from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_REVIEW_QUEUE_INPUT = Path("data/diagnostics/review_queue.json")
DEFAULT_REVIEW_DECISIONS_OUTPUT = Path("data/diagnostics/review_decisions.json")
SUPPORTED_DECISIONS = {"ACCEPT", "REJECT", "DEFER", "NEEDS_MORE_INFO"}


def create_review_decision(
    review_queue: list[dict[str, Any]],
    asset_id: str,
    decision: str,
    reason: str,
    reviewer: str = "",
    review_time: str = "",
    reviewer_note: str = "",
    related_project_id: str = "",
) -> dict[str, Any]:
    """Build one immutable human-review decision from the current queue snapshot."""
    normalized_decision = decision.strip().upper()
    if normalized_decision not in SUPPORTED_DECISIONS:
        supported = ", ".join(sorted(SUPPORTED_DECISIONS))
        raise ValueError(f"Unsupported decision: {decision}. Supported decisions: {supported}")
    queue_item = find_queue_item(review_queue, asset_id)
    if queue_item is None:
        raise ValueError(f"Asset not found in review queue: {asset_id}")
    asset_snapshot = copy.deepcopy(queue_item)
    return {
        "decision_id": build_decision_id(),
        "asset_id": asset_id,
        "decision": normalized_decision,
        "reason": reason.strip(),
        "reviewer": reviewer.strip() or "unknown",
        "reviewer_note": reviewer_note.strip(),
        "related_project_id": related_project_id.strip(),
        "review_time": review_time or utc_now(),
        "source_queue_priority": str(queue_item.get("priority") or ""),
        "asset_snapshot": asset_snapshot,
        "snapshot": copy.deepcopy(asset_snapshot),
    }


def append_review_decision(
    decision: dict[str, Any], output_path: Path = DEFAULT_REVIEW_DECISIONS_OUTPUT
) -> list[dict[str, Any]]:
    """Append a decision without modifying or collapsing prior decisions for the same asset."""
    decisions = load_review_decisions(output_path)
    decisions.append(dict(decision))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8")
    return decisions


def find_queue_item(review_queue: list[dict[str, Any]], asset_id: str) -> dict[str, Any] | None:
    for item in review_queue:
        if str(item.get("asset_id") or "") == asset_id:
            return item
    return None


def load_review_queue(path: Path) -> list[dict[str, Any]]:
    return load_json_array(path, "review queue")


def load_review_decisions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return load_json_array(path, "review decisions")


def load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected {label} JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def build_decision_id() -> str:
    return f"review_decision_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a human review decision to diagnostics.")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--decision", required=True, choices=sorted(SUPPORTED_DECISIONS), type=str.upper)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--reviewer", default="unknown")
    parser.add_argument("--reviewer-note", default="")
    parser.add_argument("--related-project-id", default="")
    parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_REVIEW_DECISIONS_OUTPUT))
    args = parser.parse_args()

    decision = create_review_decision(
        load_review_queue(Path(args.review_queue)),
        args.asset_id,
        args.decision,
        args.reason,
        args.reviewer,
        reviewer_note=args.reviewer_note,
        related_project_id=args.related_project_id,
    )
    decisions = append_review_decision(decision, Path(args.output))
    print(f"appended {args.output} decision_id={decision['decision_id']} history_count={len(decisions)}")


if __name__ == "__main__":
    main()
