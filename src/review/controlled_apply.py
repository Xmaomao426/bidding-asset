from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery_integration.candidate_promoter import DEFAULT_PROMOTED_CANDIDATES_OUTPUT
from src.discovery_integration.promotion_apply import (
    APPLY_CONFIDENCE_THRESHOLD,
    DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY,
    DEFAULT_PROMOTION_APPLY_SUMMARY,
    document_from_promoted_candidate,
    project_candidate_from_promoted,
)
from src.document.document_repository import DEFAULT_DOCUMENT_REPOSITORY_PATH, DocumentRepository


DEFAULT_ACCEPTED_DRY_RUN_INPUT = Path("data/diagnostics/accepted_asset_dry_run.json")
DEFAULT_CONTROLLED_APPLY_OUTPUT = Path("data/diagnostics/controlled_apply_result.json")
MAX_BATCH_COUNT = 10

APPLIED = "applied"
BLOCKED = "blocked"
SKIPPED = "skipped"
NOT_FOUND = "not_found"


def controlled_apply(
    dry_run_items: list[dict[str, Any]],
    promoted_candidates: list[dict[str, Any]],
    *,
    asset_id: str = "",
    batch: bool = False,
    max_count: int | None = None,
    apply: bool = False,
    document_repository_path: Path = DEFAULT_DOCUMENT_REPOSITORY_PATH,
    project_candidates_path: Path = DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY,
    promotion_summary_path: Path = DEFAULT_PROMOTION_APPLY_SUMMARY,
    result_path: Path = DEFAULT_CONTROLLED_APPLY_OUTPUT,
    confidence_threshold: float = APPLY_CONFIDENCE_THRESHOLD,
    apply_batch_id: str = "",
    applied_time: str = "",
) -> dict[str, Any]:
    """Apply a bounded set of ready dry-run items into diagnostics repositories only."""
    validate_mode(asset_id, batch, max_count, apply)
    batch_id = apply_batch_id or build_apply_batch_id()
    timestamp = applied_time or utc_now()
    promoted_by_asset = index_by_id(promoted_candidates, "candidate_id")
    selected = select_dry_run_items(dry_run_items, asset_id, batch, max_count)
    repository = DocumentRepository(document_repository_path)
    existing_project_candidates = load_json_array(project_candidates_path)
    new_project_candidates: list[dict[str, Any]] = []
    summary_documents: list[dict[str, Any]] = []
    summary_projects: list[dict[str, Any]] = []
    result_items: list[dict[str, Any]] = []

    for dry_run_item in selected:
        try:
            result_item, document_summary, project_summary = apply_one_item(
                dry_run_item,
                promoted_by_asset.get(str(dry_run_item.get("asset_id") or "")),
                repository,
                document_repository_path,
                project_candidates_path,
                promotion_summary_path,
                batch_id,
                timestamp,
                confidence_threshold,
            )
            result_items.append(result_item)
            if document_summary:
                summary_documents.append(document_summary)
            if project_summary:
                new_project_candidates.append(project_summary)
                summary_projects.append(project_summary)
        except Exception as exc:  # Keep one bad item from stopping the selected batch.
            result_items.append(
                result_item_from_dry_run(
                    dry_run_item,
                    status=BLOCKED,
                    reason=f"Controlled apply item failed: {type(exc).__name__}: {exc}",
                    written_files=[],
                    written_record_ids=[],
                    batch_id=batch_id,
                )
            )

    if new_project_candidates:
        existing_project_candidates.extend(new_project_candidates)
        write_json(project_candidates_path, existing_project_candidates)
    write_promotion_summary(
        promotion_summary_path,
        batch_id,
        timestamp,
        summary_documents,
        summary_projects,
        result_items,
    )
    result = {
        "apply_batch_id": batch_id,
        "applied_time": timestamp,
        "mode": "batch" if batch else "single",
        "applied_count": sum(1 for item in result_items if item["status"] == APPLIED),
        "blocked_count": sum(1 for item in result_items if item["status"] == BLOCKED),
        "skipped_count": sum(1 for item in result_items if item["status"] == SKIPPED),
        "items": result_items,
    }
    write_json(result_path, result)
    return result


def validate_mode(asset_id: str, batch: bool, max_count: int | None, apply: bool) -> None:
    if not apply:
        raise ValueError("Controlled apply requires explicit apply=True / --apply.")
    if bool(asset_id) == batch:
        raise ValueError("Specify exactly one mode: asset_id for single apply or batch=True for batch apply.")
    if batch:
        if max_count is None:
            raise ValueError("Batch apply requires explicit max_count / --max-count.")
        if max_count < 1 or max_count > MAX_BATCH_COUNT:
            raise ValueError(f"max_count must be between 1 and {MAX_BATCH_COUNT}.")
    elif max_count is not None:
        raise ValueError("max_count is only supported in batch mode.")


def select_dry_run_items(
    dry_run_items: list[dict[str, Any]], asset_id: str, batch: bool, max_count: int | None
) -> list[dict[str, Any]]:
    if not batch:
        matching = [item for item in dry_run_items if str(item.get("asset_id") or "") == asset_id]
        if matching:
            return [dict(matching[0])]
        return [{"asset_id": asset_id, "dry_run_status": NOT_FOUND, "reason": "Asset not found in accepted dry-run output."}]
    ready = [item for item in dry_run_items if str(item.get("dry_run_status") or "") == "ready_for_apply"]
    return [dict(item) for item in ready[: max_count or 0]]


def apply_one_item(
    dry_run_item: dict[str, Any],
    promotion: dict[str, Any] | None,
    repository: DocumentRepository,
    document_repository_path: Path,
    project_candidates_path: Path,
    promotion_summary_path: Path,
    batch_id: str,
    timestamp: str,
    confidence_threshold: float,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    dry_run_status = str(dry_run_item.get("dry_run_status") or "")
    if dry_run_status == NOT_FOUND:
        return (
            result_item_from_dry_run(dry_run_item, NOT_FOUND, str(dry_run_item.get("reason") or ""), [], [], batch_id),
            None,
            None,
        )
    if dry_run_status == SKIPPED:
        return (
            result_item_from_dry_run(dry_run_item, SKIPPED, str(dry_run_item.get("reason") or ""), [], [], batch_id),
            None,
            None,
        )
    if dry_run_status != "ready_for_apply":
        return (
            result_item_from_dry_run(
                dry_run_item,
                BLOCKED,
                "Dry-run item is not ready_for_apply: " + (dry_run_status or "missing"),
                [],
                [],
                batch_id,
            ),
            None,
            None,
        )
    if promotion is None:
        return (
            result_item_from_dry_run(
                dry_run_item, SKIPPED, "Promoted candidate no longer exists for the ready asset.", [], [], batch_id
            ),
            None,
            None,
        )

    promotion_type = str(promotion.get("promotion_type") or "")
    confidence = numeric_confidence(promotion)
    project_id = str(promotion.get("project_id") or "")
    if promotion_type == "document_entity_candidate":
        if confidence < confidence_threshold or not project_id:
            reasons = []
            if confidence < confidence_threshold:
                reasons.append(f"confidence {confidence:.4f} < {confidence_threshold:.4f}")
            if not project_id:
                reasons.append("missing project_id")
            return (
                result_item_from_dry_run(
                    dry_run_item,
                    BLOCKED,
                    "Promotion Apply conditions not met: " + ", ".join(reasons),
                    [],
                    [],
                    batch_id,
                    promotion,
                ),
                None,
                None,
            )
        document = document_from_promoted_candidate(promotion)
        document.metadata.update(provenance_metadata(dry_run_item, batch_id, timestamp))
        existing = repository.get_document(document.document_id)
        if existing is not None:
            return (
                result_item_from_dry_run(
                    dry_run_item,
                    SKIPPED,
                    "DocumentEntity already exists; existing diagnostic record was preserved.",
                    [],
                    [document.document_id],
                    batch_id,
                    promotion,
                ),
                None,
                None,
            )
        repository.create_document(document)
        document_summary = {
            "candidate_id": str(promotion.get("candidate_id") or ""),
            "project_id": project_id,
            "document": document_to_mapping(document),
        }
        return (
            result_item_from_dry_run(
                dry_run_item,
                APPLIED,
                "DocumentEntity candidate written to diagnostics repository.",
                [str(document_repository_path), str(promotion_summary_path)],
                [document.document_id],
                batch_id,
                promotion,
            ),
            document_summary,
            None,
        )

    if promotion_type == "project_candidate":
        project_candidate = project_candidate_from_promoted(promotion)
        metadata = dict(project_candidate.get("project_candidate", {}).get("metadata") or {})
        metadata.update(provenance_metadata(dry_run_item, batch_id, timestamp))
        project_candidate["project_candidate"]["metadata"] = metadata
        record_id = str(project_candidate["project_candidate"].get("project_candidate_id") or promotion.get("candidate_id") or "")
        return (
            result_item_from_dry_run(
                dry_run_item,
                APPLIED,
                "Project candidate diagnostic written; no formal ProjectEntity was created.",
                [str(project_candidates_path), str(promotion_summary_path)],
                [record_id],
                batch_id,
                promotion,
            ),
            None,
            project_candidate,
        )

    return (
        result_item_from_dry_run(
            dry_run_item,
            BLOCKED,
            "Unsupported promotion_type: " + (promotion_type or "missing"),
            [],
            [],
            batch_id,
            promotion,
        ),
        None,
        None,
    )


def provenance_metadata(dry_run_item: dict[str, Any], batch_id: str, timestamp: str) -> dict[str, str]:
    return {
        "apply_batch_id": batch_id,
        "applied_time": timestamp,
        "source_asset_id": str(dry_run_item.get("asset_id") or ""),
        "source_decision_id": str(dry_run_item.get("decision_id") or ""),
    }


def result_item_from_dry_run(
    dry_run_item: dict[str, Any],
    status: str,
    reason: str,
    written_files: list[str],
    written_record_ids: list[str],
    batch_id: str,
    promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    promotion = promotion or {}
    files = list(written_files)
    record_ids = list(written_record_ids)
    return {
        "asset_id": str(dry_run_item.get("asset_id") or ""),
        "decision_id": str(dry_run_item.get("decision_id") or ""),
        "status": status,
        "promotion_type": str(promotion.get("promotion_type") or dry_run_item.get("promotion_type") or ""),
        "project_id": str(promotion.get("project_id") or dry_run_item.get("project_id") or ""),
        "confidence": numeric_confidence(promotion) if promotion else numeric_confidence(dry_run_item),
        "written_files": files,
        "written_record_ids": record_ids,
        "reason": reason,
        "rollback_hint": rollback_hint(batch_id, files, record_ids),
        "snapshot": dict(dry_run_item.get("snapshot") or {}),
    }


def rollback_hint(batch_id: str, written_files: list[str], written_record_ids: list[str]) -> str:
    files = ", ".join(written_files) or "none"
    record_ids = ", ".join(written_record_ids) or "none"
    return (
        f"Locate diagnostics records by apply_batch_id={batch_id} or written_record_ids={record_ids}; "
        f"manually remove only those records from written_files={files}."
    )


def write_promotion_summary(
    path: Path,
    batch_id: str,
    timestamp: str,
    documents: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> None:
    payload = {
        "dry_run": False,
        "apply": True,
        "apply_batch_id": batch_id,
        "applied_time": timestamp,
        "input_count": len(items),
        "document_entity_candidate_count": len(documents),
        "project_candidate_count": len(projects),
        "skipped_count": sum(1 for item in items if item["status"] == SKIPPED),
        "blocked_count": sum(1 for item in items if item["status"] == BLOCKED),
        "document_candidates": documents,
        "project_candidates": projects,
    }
    write_json(path, payload)


def document_to_mapping(document: Any) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "source_type": document.source_type,
        "source_path": document.source_path,
        "source_url": document.source_url,
        "file_name": document.file_name,
        "file_type": document.file_type,
        "metadata": dict(document.metadata),
        "created_time": document.created_time,
        "records": list(document.records),
    }


def index_by_id(rows: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(id_key) or ""): dict(row) for row in rows if str(row.get(id_key) or "")}


def numeric_confidence(item: dict[str, Any]) -> float:
    try:
        return round(float(item.get("confidence") or 0), 4)
    except (TypeError, ValueError):
        return 0.0


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_apply_batch_id() -> str:
    return f"apply_batch_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled diagnostics-only apply for accepted dry-run assets.")
    parser.add_argument("--asset-id", default="")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--max-count", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Required: write diagnostics repositories only.")
    parser.add_argument("--dry-run-input", default=str(DEFAULT_ACCEPTED_DRY_RUN_INPUT))
    parser.add_argument("--promoted-candidates", default=str(DEFAULT_PROMOTED_CANDIDATES_OUTPUT))
    parser.add_argument("--document-repository", default=str(DEFAULT_DOCUMENT_REPOSITORY_PATH))
    parser.add_argument("--project-candidates", default=str(DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY))
    parser.add_argument("--promotion-summary", default=str(DEFAULT_PROMOTION_APPLY_SUMMARY))
    parser.add_argument("--output", default=str(DEFAULT_CONTROLLED_APPLY_OUTPUT))
    args = parser.parse_args()

    try:
        result = controlled_apply(
            load_json_array(Path(args.dry_run_input)),
            load_json_array(Path(args.promoted_candidates)),
            asset_id=args.asset_id,
            batch=args.batch,
            max_count=args.max_count,
            apply=args.apply,
            document_repository_path=Path(args.document_repository),
            project_candidates_path=Path(args.project_candidates),
            promotion_summary_path=Path(args.promotion_summary),
            result_path=Path(args.output),
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        "controlled_apply completed "
        f"batch_id={result['apply_batch_id']} applied={result['applied_count']} "
        f"blocked={result['blocked_count']} skipped={result['skipped_count']}"
    )


if __name__ == "__main__":
    main()
