from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.review.review_decision import SUPPORTED_DECISIONS, load_review_decisions


DEFAULT_REVIEW_QUEUE_INPUT = Path("data/diagnostics/review_queue.json")
DEFAULT_REVIEW_DECISIONS_INPUT = Path("data/diagnostics/review_decisions.json")
DEFAULT_REVIEW_DECISION_SUMMARY_OUTPUT = Path("data/diagnostics/review_decision_summary.json")
PRIORITIES = ("P1", "P2", "P3")


def build_review_decision_summary(
    review_queue: list[dict[str, Any]], review_decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build diagnostic-only coverage and latest-decision statistics."""
    queue_by_asset = {
        str(item.get("asset_id") or ""): dict(item)
        for item in review_queue
        if str(item.get("asset_id") or "")
    }
    latest_by_asset = latest_decisions_by_asset(review_decisions)
    queue_asset_ids = set(queue_by_asset)
    decision_asset_ids = set(latest_by_asset)

    accepted_assets = assets_for_latest_decision(latest_by_asset, "ACCEPT")
    rejected_assets = assets_for_latest_decision(latest_by_asset, "REJECT")
    deferred_assets = assets_for_latest_decision(latest_by_asset, "DEFER")
    needs_more_info_assets = assets_for_latest_decision(latest_by_asset, "NEEDS_MORE_INFO")
    return {
        "total_assets_in_queue": len(queue_asset_ids),
        "total_decisions": len(review_decisions),
        "assets_with_decision": len(decision_asset_ids),
        "assets_without_decision": len(queue_asset_ids - decision_asset_ids),
        "latest_decision_counts": {
            "ACCEPT": len(accepted_assets),
            "REJECT": len(rejected_assets),
            "DEFER": len(deferred_assets),
            "NEEDS_MORE_INFO": len(needs_more_info_assets),
        },
        "priority_counts": {
            priority: sum(1 for item in queue_by_asset.values() if str(item.get("priority") or "") == priority)
            for priority in PRIORITIES
        },
        "accepted_assets": accepted_assets,
        "rejected_assets": rejected_assets,
        "deferred_assets": deferred_assets,
        "needs_more_info_assets": needs_more_info_assets,
    }


def latest_decisions_by_asset(review_decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for decision in review_decisions:
        asset_id = str(decision.get("asset_id") or "")
        if not asset_id:
            continue
        current = latest.get(asset_id)
        if current is None or str(decision.get("review_time") or "") >= str(current.get("review_time") or ""):
            latest[asset_id] = dict(decision)
    return latest


def assets_for_latest_decision(latest_by_asset: dict[str, dict[str, Any]], decision: str) -> list[str]:
    return sorted(
        asset_id
        for asset_id, item in latest_by_asset.items()
        if str(item.get("decision") or "") == decision
    )


def load_review_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected review queue JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_review_decision_summary(
    summary: dict[str, Any], output_path: Path = DEFAULT_REVIEW_DECISION_SUMMARY_OUTPUT
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build diagnostic-only human review decision summary.")
    parser.add_argument("--review-queue", default=str(DEFAULT_REVIEW_QUEUE_INPUT))
    parser.add_argument("--review-decisions", default=str(DEFAULT_REVIEW_DECISIONS_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_REVIEW_DECISION_SUMMARY_OUTPUT))
    args = parser.parse_args()

    summary = build_review_decision_summary(
        load_review_queue(Path(args.review_queue)), load_review_decisions(Path(args.review_decisions))
    )
    write_review_decision_summary(summary, Path(args.output))
    print(
        "wrote "
        f"{args.output} queue_assets={summary['total_assets_in_queue']} decisions={summary['total_decisions']}"
    )


if __name__ == "__main__":
    main()
