from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery_integration.promotion_apply import APPLY_CONFIDENCE_THRESHOLD
from src.lifecycle.asset_lifecycle import DEFAULT_ASSET_LIFECYCLE_OUTPUT, load_json_array
from src.review.review_decision import load_review_decisions
from src.review.review_decision_summary import latest_decisions_by_asset, load_review_queue


DEFAULT_PROMOTED_CANDIDATES_INPUT = Path("data/diagnostics/promoted_candidates.json")
DEFAULT_ACCEPTED_ASSET_DRY_RUN_OUTPUT = Path("data/diagnostics/accepted_asset_dry_run.json")

READY_FOR_APPLY = "ready_for_apply"
BLOCKED = "blocked"
SKIPPED = "skipped"


def build_accepted_asset_dry_run(
    review_decisions: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
    promoted_candidates: list[dict[str, Any]],
    lifecycles: list[dict[str, Any]],
    confidence_threshold: float = APPLY_CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Classify accepted assets against Promotion Apply rules without applying anything."""
    latest_by_asset = latest_decisions_by_asset(review_decisions)
    promotions_by_asset = index_by_id(promoted_candidates, "candidate_id")
    queue_by_asset = index_by_id(review_queue, "asset_id")
    lifecycle_by_asset = index_by_id(lifecycles, "asset_id")
    accepted_assets = sorted(
        asset_id
        for asset_id, decision in latest_by_asset.items()
        if str(decision.get("decision") or "") == "ACCEPT"
    )
    return [
        build_dry_run_item(
            asset_id,
            latest_by_asset[asset_id],
            promotions_by_asset.get(asset_id),
            queue_by_asset.get(asset_id),
            lifecycle_by_asset.get(asset_id),
            confidence_threshold,
        )
        for asset_id in accepted_assets
    ]


def build_dry_run_item(
    asset_id: str,
    decision: dict[str, Any],
    promotion: dict[str, Any] | None,
    queue_item: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
    confidence_threshold: float,
) -> dict[str, Any]:
    snapshot = {
        "review_queue": dict(queue_item or decision.get("snapshot") or {}),
        "lifecycle": dict(lifecycle or {}),
    }
    if promotion is None:
        return dry_run_payload(
            asset_id,
            decision,
            promotion_type="",
            project_id="",
            confidence=0.0,
            dry_run_status=SKIPPED,
            reason="No promoted candidate exists for the accepted asset.",
            required_manual_check=["re-run_candidate_promotion_or_review_source_candidate"],
            snapshot=snapshot,
        )

    promotion_type = str(promotion.get("promotion_type") or "")
    project_id = str(promotion.get("project_id") or "")
    confidence = numeric_confidence(promotion)
    if promotion_type == "document_entity_candidate":
        missing_conditions: list[str] = []
        if confidence < confidence_threshold:
            missing_conditions.append(f"confidence {confidence:.4f} < {confidence_threshold:.4f}")
        if not project_id:
            missing_conditions.append("missing project_id")
        if missing_conditions:
            return dry_run_payload(
                asset_id,
                decision,
                promotion_type,
                project_id,
                confidence,
                BLOCKED,
                "Promotion Apply conditions not met: " + ", ".join(missing_conditions),
                ["confirm_project_match", "confirm_source_material"],
                snapshot,
            )
        return dry_run_payload(
            asset_id,
            decision,
            promotion_type,
            project_id,
            confidence,
            READY_FOR_APPLY,
            "Meets existing document_entity_candidate Promotion Apply conditions in dry-run.",
            ["confirm_project_match", "confirm_source_material_before_explicit_apply"],
            snapshot,
        )

    if promotion_type == "project_candidate":
        return dry_run_payload(
            asset_id,
            decision,
            promotion_type,
            project_id,
            confidence,
            READY_FOR_APPLY,
            "Existing Promotion Apply can emit a diagnostic project candidate; no formal ProjectEntity is created.",
            ["manual_project_review_required", "do_not_auto_create_project_entity"],
            snapshot,
        )

    return dry_run_payload(
        asset_id,
        decision,
        promotion_type,
        project_id,
        confidence,
        BLOCKED,
        "Unsupported promotion_type for Promotion Apply: " + (promotion_type or "missing"),
        ["review_promotion_type"],
        snapshot,
    )


def dry_run_payload(
    asset_id: str,
    decision: dict[str, Any],
    promotion_type: str,
    project_id: str,
    confidence: float,
    dry_run_status: str,
    reason: str,
    required_manual_check: list[str],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "decision_id": str(decision.get("decision_id") or ""),
        "decision": str(decision.get("decision") or ""),
        "promotion_type": promotion_type,
        "project_id": project_id,
        "confidence": confidence,
        "dry_run_status": dry_run_status,
        "reason": reason,
        "required_manual_check": required_manual_check,
        "snapshot": snapshot,
    }


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def numeric_confidence(promotion: dict[str, Any]) -> float:
    try:
        return round(float(promotion.get("confidence") or 0), 4)
    except (TypeError, ValueError):
        return 0.0


def write_accepted_asset_dry_run(
    items: list[dict[str, Any]], output_path: Path = DEFAULT_ACCEPTED_ASSET_DRY_RUN_OUTPUT
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build accepted Asset Promotion Apply dry-run diagnostics.")
    parser.add_argument("--review-decisions", default="data/diagnostics/review_decisions.json")
    parser.add_argument("--review-queue", default="data/diagnostics/review_queue.json")
    parser.add_argument("--promoted-candidates", default=str(DEFAULT_PROMOTED_CANDIDATES_INPUT))
    parser.add_argument("--lifecycle", default=str(DEFAULT_ASSET_LIFECYCLE_OUTPUT))
    parser.add_argument("--output", default=str(DEFAULT_ACCEPTED_ASSET_DRY_RUN_OUTPUT))
    parser.add_argument("--confidence-threshold", type=float, default=APPLY_CONFIDENCE_THRESHOLD)
    args = parser.parse_args()

    items = build_accepted_asset_dry_run(
        load_review_decisions(Path(args.review_decisions)),
        load_review_queue(Path(args.review_queue)),
        load_json_array(Path(args.promoted_candidates)),
        load_json_array(Path(args.lifecycle)),
        args.confidence_threshold,
    )
    write_accepted_asset_dry_run(items, Path(args.output))
    print(f"wrote {args.output} accepted_assets={len(items)} dry_run=true")


if __name__ == "__main__":
    main()
