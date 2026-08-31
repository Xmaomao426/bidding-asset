from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ASSET_CANDIDATES_INPUT = Path("data/diagnostics/asset_candidates.json")
DEFAULT_DEDUPED_CANDIDATES_INPUT = Path("data/diagnostics/asset_candidates_deduped.json")
DEFAULT_PROMOTED_CANDIDATES_INPUT = Path("data/diagnostics/promoted_candidates.json")
DEFAULT_PROMOTION_APPLY_SUMMARY_INPUT = Path("data/diagnostics/promotion_apply_summary.json")
DEFAULT_ASSET_LIFECYCLE_OUTPUT = Path("data/diagnostics/asset_lifecycle.json")

DISCOVERED = "DISCOVERED"
DEDUPED = "DEDUPED"
PROMOTION_READY = "PROMOTION_READY"
PROMOTED = "PROMOTED"
APPLIED = "APPLIED"


@dataclass
class AssetLifecycle:
    asset_id: str
    asset_type: str
    status: str
    created_time: str
    updated_time: str
    history: list[dict[str, Any]] = field(default_factory=list)


def build_asset_lifecycles(
    asset_candidates: list[dict[str, Any]] | None = None,
    deduped_candidates: list[dict[str, Any]] | None = None,
    promoted_candidates: list[dict[str, Any]] | None = None,
    promotion_apply_summary: dict[str, Any] | None = None,
    event_time: str | None = None,
) -> list[AssetLifecycle]:
    timestamp = event_time or utc_now()
    lifecycles: dict[str, AssetLifecycle] = {}

    for candidate in asset_candidates or []:
        candidate_id = candidate_id_from(candidate)
        if candidate_id:
            record_event(lifecycles, candidate_id, "asset_candidate", DISCOVERED, timestamp, candidate)

    for candidate in deduped_candidates or []:
        candidate_id = candidate_id_from(candidate)
        is_primary = bool(candidate.get("is_primary", True))
        if candidate_id:
            record_event(lifecycles, candidate_id, "asset_candidate", DEDUPED, timestamp, candidate)
            if is_primary:
                record_event(lifecycles, candidate_id, "asset_candidate", PROMOTION_READY, timestamp, candidate)
        for merged in candidate.get("merged_sources") or []:
            if not isinstance(merged, dict):
                continue
            merged_id = candidate_id_from(merged)
            if merged_id and merged_id != candidate_id:
                record_event(lifecycles, merged_id, "asset_candidate", DEDUPED, timestamp, merged)

    for candidate in promoted_candidates or []:
        candidate_id = candidate_id_from(candidate)
        if candidate_id:
            record_event(lifecycles, candidate_id, "promotion_candidate", PROMOTED, timestamp, candidate)

    if promotion_apply_summary:
        apply_was_executed = bool(promotion_apply_summary.get("apply")) and not bool(promotion_apply_summary.get("dry_run"))
        if apply_was_executed:
            for item in applied_items_from_summary(promotion_apply_summary):
                candidate_id = candidate_id_from(item)
                if candidate_id:
                    record_event(lifecycles, candidate_id, "promotion_candidate", APPLIED, timestamp, item)

    return sorted(lifecycles.values(), key=lambda lifecycle: lifecycle.asset_id)


def get_lifecycle(lifecycles: list[AssetLifecycle], asset_id: str) -> AssetLifecycle | None:
    for lifecycle in lifecycles:
        if lifecycle.asset_id == asset_id:
            return lifecycle
    return None


def record_event(
    lifecycles: dict[str, AssetLifecycle],
    asset_id: str,
    asset_type: str,
    status: str,
    timestamp: str,
    payload: dict[str, Any],
) -> None:
    lifecycle = lifecycles.get(asset_id)
    if lifecycle is None:
        lifecycle = AssetLifecycle(
            asset_id=asset_id,
            asset_type=asset_type,
            status=status,
            created_time=timestamp,
            updated_time=timestamp,
            history=[],
        )
        lifecycles[asset_id] = lifecycle
    lifecycle.asset_type = asset_type
    lifecycle.status = status
    lifecycle.updated_time = timestamp
    if lifecycle.history and lifecycle.history[-1].get("status") == status:
        lifecycle.history[-1] = {
            "status": status,
            "timestamp": timestamp,
            "asset_type": asset_type,
            "source": event_source(payload),
        }
        return
    lifecycle.history.append(
        {
            "status": status,
            "timestamp": timestamp,
            "asset_type": asset_type,
            "source": event_source(payload),
        }
    )


def event_source(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(payload.get("candidate_id") or ""),
        "source_type": str(payload.get("source_type") or payload.get("source") or ""),
        "source_url": str(payload.get("source_url") or ""),
        "source_trace": dict(payload.get("source_trace") or {}),
        "promotion_type": str(payload.get("promotion_type") or ""),
        "project_id": str(payload.get("project_id") or payload.get("matched_project_id") or ""),
        "confidence": float(payload.get("confidence") or 0),
    }


def applied_items_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("document_candidates", "project_candidates"):
        for item in summary.get(key) or []:
            if isinstance(item, dict):
                items.append(normalize_applied_item(dict(item)))
    return items


def normalize_applied_item(item: dict[str, Any]) -> dict[str, Any]:
    document = dict(item.get("document") or {})
    project_candidate = dict(item.get("project_candidate") or {})
    metadata = dict(document.get("metadata") or project_candidate.get("metadata") or {})
    if "source_trace" not in item and metadata.get("source_trace"):
        item["source_trace"] = dict(metadata.get("source_trace") or {})
    if "source_type" not in item and metadata.get("promotion_source"):
        item["source_type"] = str(metadata.get("promotion_source") or "")
    if "confidence" not in item and metadata.get("promotion_confidence") is not None:
        item["confidence"] = float(metadata.get("promotion_confidence") or 0)
    return item


def candidate_id_from(payload: dict[str, Any]) -> str:
    return str(payload.get("candidate_id") or "")


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return dict(payload)


def write_asset_lifecycles(
    lifecycles: list[AssetLifecycle], output_path: Path = DEFAULT_ASSET_LIFECYCLE_OUTPUT
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(lifecycle) for lifecycle in lifecycles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Asset Lifecycle diagnostics from candidate pipeline outputs.")
    parser.add_argument("--asset-candidates", default=str(DEFAULT_ASSET_CANDIDATES_INPUT))
    parser.add_argument("--deduped-candidates", default=str(DEFAULT_DEDUPED_CANDIDATES_INPUT))
    parser.add_argument("--promoted-candidates", default=str(DEFAULT_PROMOTED_CANDIDATES_INPUT))
    parser.add_argument("--promotion-apply-summary", default=str(DEFAULT_PROMOTION_APPLY_SUMMARY_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_ASSET_LIFECYCLE_OUTPUT))
    args = parser.parse_args()

    lifecycles = build_asset_lifecycles(
        asset_candidates=load_json_array(Path(args.asset_candidates)),
        deduped_candidates=load_json_array(Path(args.deduped_candidates)),
        promoted_candidates=load_json_array(Path(args.promoted_candidates)),
        promotion_apply_summary=load_json_object(Path(args.promotion_apply_summary)),
    )
    write_asset_lifecycles(lifecycles, Path(args.output))
    print(f"wrote {args.output} lifecycles={len(lifecycles)}")


if __name__ == "__main__":
    main()
