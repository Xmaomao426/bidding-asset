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

from src.document.document_entity import document_entity_from_mapping
from src.lifecycle.asset_lifecycle import DEFAULT_ASSET_LIFECYCLE_OUTPUT, load_json_array
from src.review.review_decision import SUPPORTED_DECISIONS, load_review_decisions


DEFAULT_ASSET_CANDIDATES_INPUT = Path("data/diagnostics/asset_candidates.json")
DEFAULT_DEDUPED_CANDIDATES_INPUT = Path("data/diagnostics/asset_candidates_deduped.json")
DEFAULT_PROMOTED_CANDIDATES_INPUT = Path("data/diagnostics/promoted_candidates.json")
DEFAULT_DOCUMENT_REPOSITORY_INPUT = Path("data/diagnostics/document_repository.json")
DEFAULT_PROJECT_CANDIDATES_INPUT = Path("data/diagnostics/project_candidates_from_discovery.json")
DEFAULT_REVIEW_DECISIONS_INPUT = Path("data/diagnostics/review_decisions.json")


class AssetQueryService:
    """Read-only index over asset candidate and project asset diagnostics."""

    def __init__(
        self,
        asset_candidates_path: Path | str = DEFAULT_ASSET_CANDIDATES_INPUT,
        deduped_candidates_path: Path | str = DEFAULT_DEDUPED_CANDIDATES_INPUT,
        promoted_candidates_path: Path | str = DEFAULT_PROMOTED_CANDIDATES_INPUT,
        lifecycle_path: Path | str = DEFAULT_ASSET_LIFECYCLE_OUTPUT,
        document_repository_path: Path | str = DEFAULT_DOCUMENT_REPOSITORY_INPUT,
        review_decisions_path: Path | str = DEFAULT_REVIEW_DECISIONS_INPUT,
        project_candidates_path: Path | str = DEFAULT_PROJECT_CANDIDATES_INPUT,
    ) -> None:
        self.asset_candidates = load_json_array(Path(asset_candidates_path))
        self.deduped_candidates = load_json_array(Path(deduped_candidates_path))
        self.promoted_candidates = load_json_array(Path(promoted_candidates_path))
        self.lifecycles = load_json_array(Path(lifecycle_path))
        self.documents = load_documents(Path(document_repository_path))
        self.review_decisions = load_review_decisions(Path(review_decisions_path))
        self.project_candidates = load_project_candidates(Path(project_candidates_path))
        self._candidates_by_id = self._build_candidate_index()
        self._promotions_by_id = index_by_id(self.promoted_candidates, "candidate_id")
        self._lifecycles_by_id = index_by_id(self.lifecycles, "asset_id")
        self._project_candidates_by_asset = index_by_id(self.project_candidates, "candidate_id")

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        """Return the unified read-only view for one Asset Candidate."""
        candidate = self._candidates_by_id.get(asset_id)
        promotion = self._promotions_by_id.get(asset_id)
        lifecycle = self._lifecycles_by_id.get(asset_id)
        project_candidate = self._project_candidates_by_asset.get(asset_id)
        if not candidate and not promotion and not lifecycle and not project_candidate:
            return None
        candidate_view = dict(candidate or {})
        return {
            "asset_id": asset_id,
            "asset_candidate": candidate_view,
            "source_trace": dict(candidate_view.get("source_trace") or promotion_source_trace(promotion)),
            "lifecycle": dict(lifecycle or {}),
            "promotion": dict(promotion or {}),
            "project_candidate": dict(project_candidate or {}),
        }

    def get_project_assets(self, project_id: str) -> dict[str, Any]:
        """Return applied documents plus candidate and lifecycle diagnostics for a project."""
        candidates = [
            candidate
            for candidate in self._candidates_by_id.values()
            if str(candidate.get("matched_project_id") or "") == project_id
        ]
        candidates.sort(key=lambda item: str(item.get("candidate_id") or ""))
        candidate_ids = {str(candidate.get("candidate_id") or "") for candidate in candidates}
        lifecycle_items = [
            lifecycle
            for lifecycle in self.lifecycles
            if str(lifecycle.get("asset_id") or "") in candidate_ids
        ]
        lifecycle_items.sort(key=lambda item: str(item.get("asset_id") or ""))
        documents = [
            document
            for document in self.documents
            if str((document.get("metadata") or {}).get("project_id") or "") == project_id
        ]
        documents.sort(key=lambda item: str(item.get("document_id") or ""))
        return {
            "project_id": project_id,
            "documents": documents,
            "candidates": candidates,
            "lifecycle": lifecycle_items,
        }

    def find_by_source(self, source_type: str) -> list[dict[str, Any]]:
        """Return all known original candidates for a discovery source type."""
        return [
            dict(candidate)
            for candidate in self.asset_candidates
            if str(candidate.get("source_type") or "") == source_type
        ]

    def get_review_decisions(self, asset_id: str) -> list[dict[str, Any]]:
        """Return the append-only human decision history for an asset."""
        decisions = [
            dict(item)
            for item in self.review_decisions
            if str(item.get("asset_id") or "") == asset_id
        ]
        return sorted(decisions, key=lambda item: str(item.get("review_time") or ""))

    def get_latest_review_decision(self, asset_id: str) -> dict[str, Any] | None:
        """Return the latest human decision for an asset, if one exists."""
        decisions = self.get_review_decisions(asset_id)
        return decisions[-1] if decisions else None

    def find_assets_by_decision(self, decision: str) -> list[dict[str, Any]]:
        """Return assets whose latest human decision matches the requested value."""
        normalized_decision = normalize_decision(decision)
        asset_ids = sorted({str(item.get("asset_id") or "") for item in self.review_decisions if item.get("asset_id")})
        return [
            {"asset_id": asset_id, "latest_review_decision": latest}
            for asset_id in asset_ids
            if (latest := self.get_latest_review_decision(asset_id)) is not None
            and str(latest.get("decision") or "") == normalized_decision
        ]

    def _build_candidate_index(self) -> dict[str, dict[str, Any]]:
        index = index_by_id(self.asset_candidates, "candidate_id")
        for candidate in self.deduped_candidates:
            add_candidate(index, candidate)
            for merged in candidate.get("merged_sources") or []:
                if isinstance(merged, dict):
                    add_candidate(index, merged)
        return index


def add_candidate(index: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    candidate_id = str(candidate.get("candidate_id") or "")
    if not candidate_id:
        return
    current = dict(index.get(candidate_id) or {})
    updated = dict(candidate)
    # Dedup data adds group and primary fields while original candidates retain source detail.
    current.update({key: value for key, value in updated.items() if value not in (None, "", [], {})})
    index[candidate_id] = current


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def promotion_source_trace(promotion: dict[str, Any] | None) -> dict[str, Any]:
    if not promotion:
        return {}
    trace = promotion.get("source_trace")
    if isinstance(trace, dict):
        return dict(trace)
    document_candidate = promotion.get("document_candidate")
    if isinstance(document_candidate, dict):
        metadata = document_candidate.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("source_trace"), dict):
            return dict(metadata["source_trace"])
    return {}


def normalize_decision(decision: str) -> str:
    normalized = decision.strip().upper()
    if normalized not in SUPPORTED_DECISIONS:
        supported = ", ".join(sorted(SUPPORTED_DECISIONS))
        raise ValueError(f"Unsupported decision: {decision}. Supported decisions: {supported}")
    return normalized


def load_documents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("documents") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected document repository JSON: {path}")
    return [asdict(document_entity_from_mapping(row)) for row in rows if isinstance(row, dict)]


def load_project_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected project candidates JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only query over asset candidate diagnostics.")
    parser.add_argument("--asset-id", default="", help="Return one unified asset view.")
    parser.add_argument("--project-id", default="", help="Return documents, candidates, and lifecycle for a project.")
    parser.add_argument("--source-type", default="", help="Return original candidates for a source type.")
    parser.add_argument("--review-decisions-for", default="", help="Return all review decisions for an asset.")
    parser.add_argument("--latest-review-decision", default="", help="Return the latest review decision for an asset.")
    parser.add_argument("--decision", default="", help="Return assets whose latest review decision matches this value.")
    parser.add_argument("--asset-candidates", default=str(DEFAULT_ASSET_CANDIDATES_INPUT))
    parser.add_argument("--deduped-candidates", default=str(DEFAULT_DEDUPED_CANDIDATES_INPUT))
    parser.add_argument("--promoted-candidates", default=str(DEFAULT_PROMOTED_CANDIDATES_INPUT))
    parser.add_argument("--lifecycle", default=str(DEFAULT_ASSET_LIFECYCLE_OUTPUT))
    parser.add_argument("--document-repository", default=str(DEFAULT_DOCUMENT_REPOSITORY_INPUT))
    parser.add_argument("--review-decisions", default=str(DEFAULT_REVIEW_DECISIONS_INPUT))
    parser.add_argument("--project-candidates", default=str(DEFAULT_PROJECT_CANDIDATES_INPUT))
    args = parser.parse_args()

    selectors = [
        args.asset_id,
        args.project_id,
        args.source_type,
        args.review_decisions_for,
        args.latest_review_decision,
        args.decision,
    ]
    if sum(bool(selector) for selector in selectors) != 1:
        parser.error("Specify exactly one query selector.")
    service = AssetQueryService(
        args.asset_candidates,
        args.deduped_candidates,
        args.promoted_candidates,
        args.lifecycle,
        args.document_repository,
        args.review_decisions,
        args.project_candidates,
    )
    if args.asset_id:
        result: Any = service.get_asset(args.asset_id)
    elif args.project_id:
        result = service.get_project_assets(args.project_id)
    elif args.source_type:
        result = service.find_by_source(args.source_type)
    elif args.review_decisions_for:
        result = service.get_review_decisions(args.review_decisions_for)
    elif args.latest_review_decision:
        result = service.get_latest_review_decision(args.latest_review_decision)
    else:
        result = service.find_assets_by_decision(args.decision)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
