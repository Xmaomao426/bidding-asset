from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.query.asset_query import AssetQueryService
from src.review.accepted_asset_dry_run import build_accepted_asset_dry_run, write_accepted_asset_dry_run
from src.review.controlled_apply import controlled_apply, load_json_array
from src.review.review_decision import load_review_decisions
from src.review.review_decision_summary import build_review_decision_summary, load_review_queue


DEFAULT_FIXTURE_ROOT = Path("data/diagnostics/controlled_apply_e2e_fixture")
DEFAULT_E2E_REPORT_OUTPUT = Path("data/diagnostics/controlled_apply_e2e_report.json")
FORMAL_DATA_PATHS = (
    Path("data/cache/final_records.json"),
    Path("data/overrides/manual_overrides.json"),
    Path("招投标.xlsx"),
)


def prepare_e2e_fixture(fixture_root: Path = DEFAULT_FIXTURE_ROOT) -> dict[str, Path]:
    """Create an isolated accepted asset fixture for a controlled diagnostics apply check."""
    fixture_root.mkdir(parents=True, exist_ok=True)
    paths = fixture_paths(fixture_root)
    asset_id = "e2e_accepted_asset"
    decision_id = "e2e_decision_accept"
    write_json(
        paths["review_decisions"],
        [
            {
                "decision_id": decision_id,
                "asset_id": asset_id,
                "decision": "ACCEPT",
                "reason": "Phase22 isolated end-to-end verification fixture",
                "reviewer": "phase22_verifier",
                "review_time": "2026-07-10T00:00:00Z",
                "source_queue_priority": "P1",
                "snapshot": {"asset_id": asset_id, "priority": "P1"},
            }
        ],
    )
    write_json(
        paths["review_queue"],
        [
            {
                "asset_id": asset_id,
                "source_type": "verification",
                "title": "Phase22 controlled apply verification asset",
                "lifecycle_status": "PROMOTION_READY",
                "confidence": 0.95,
                "priority": "P1",
                "reason": "verification fixture",
            }
        ],
    )
    write_json(
        paths["promoted_candidates"],
        [
            {
                "candidate_id": asset_id,
                "promotion_type": "document_entity_candidate",
                "project_id": "e2e_project",
                "confidence": 0.95,
                "source": "verification",
                "source_trace": {"source_type": "verification", "source_url": "https://example.gov.cn/e2e.html"},
                "document_candidate": {
                    "document_id": "document_e2e_accepted_asset",
                    "source_type": "verification",
                    "source_path": "",
                    "source_url": "https://example.gov.cn/e2e.html",
                    "file_name": "e2e.html",
                    "file_type": "html",
                    "metadata": {"source_title": "Phase22 controlled apply verification asset"},
                },
            }
        ],
    )
    write_json(
        paths["lifecycle"],
        [{"asset_id": asset_id, "asset_type": "asset_candidate", "status": "PROMOTION_READY", "history": []}],
    )
    return paths


def run_controlled_apply_e2e_verification(
    paths: dict[str, Path],
    report_path: Path = DEFAULT_E2E_REPORT_OUTPUT,
    formal_paths: tuple[Path, ...] = FORMAL_DATA_PATHS,
) -> dict[str, Any]:
    """Run accepted dry-run, bounded diagnostics apply, and post-write verification."""
    before_hashes = hash_paths(formal_paths)
    decisions = load_review_decisions(paths["review_decisions"])
    queue = load_review_queue(paths["review_queue"])
    promoted = load_json_array(paths["promoted_candidates"])
    lifecycles = load_json_array(paths["lifecycle"])
    summary_before = build_review_decision_summary(queue, decisions)
    dry_run_items = build_accepted_asset_dry_run(decisions, queue, promoted, lifecycles)
    write_accepted_asset_dry_run(dry_run_items, paths["accepted_dry_run"])
    apply_result = controlled_apply(
        dry_run_items,
        promoted,
        batch=True,
        max_count=10,
        apply=True,
        document_repository_path=paths["document_repository"],
        project_candidates_path=paths["project_candidates"],
        promotion_summary_path=paths["promotion_summary"],
        result_path=paths["controlled_apply_result"],
        apply_batch_id="phase22_e2e_batch",
        applied_time="2026-07-10T00:00:00Z",
    )
    summary_after = build_review_decision_summary(
        load_review_queue(paths["review_queue"]), load_review_decisions(paths["review_decisions"])
    )
    after_hashes = hash_paths(formal_paths)
    query_verification = verify_asset_queries(apply_result, paths)
    rollback_check = verify_rollback_hints(apply_result)
    formal_data_unchanged = before_hashes == after_hashes
    provenance_issues = verify_written_provenance(apply_result, paths)
    issues = collect_issues(provenance_issues, query_verification, formal_data_unchanged, rollback_check)
    report = {
        "apply_batch_id": apply_result["apply_batch_id"],
        "tested_asset_count": len(dry_run_items),
        "applied_count": apply_result["applied_count"],
        "blocked_count": apply_result["blocked_count"],
        "skipped_count": apply_result["skipped_count"],
        "written_files": unique_written_files(apply_result),
        "query_verification": query_verification,
        "provenance_verification": {"verified": not provenance_issues, "issues": provenance_issues},
        "formal_data_unchanged": formal_data_unchanged,
        "rollback_check": rollback_check,
        "issues": issues,
        "review_decision_summary_unchanged": summary_before == summary_after,
    }
    write_json(report_path, report)
    return report


def fixture_paths(fixture_root: Path) -> dict[str, Path]:
    return {
        "review_decisions": fixture_root / "review_decisions.json",
        "review_queue": fixture_root / "review_queue.json",
        "promoted_candidates": fixture_root / "promoted_candidates.json",
        "lifecycle": fixture_root / "asset_lifecycle.json",
        "accepted_dry_run": fixture_root / "accepted_asset_dry_run.json",
        "document_repository": fixture_root / "document_repository.json",
        "project_candidates": fixture_root / "project_candidates_from_discovery.json",
        "promotion_summary": fixture_root / "promotion_apply_summary.json",
        "controlled_apply_result": fixture_root / "controlled_apply_result.json",
    }


def verify_asset_queries(apply_result: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    service = AssetQueryService(
        asset_candidates_path=paths["asset_candidates"] if "asset_candidates" in paths else paths["promoted_candidates"].with_name("missing.json"),
        deduped_candidates_path=paths["promoted_candidates"].with_name("missing_deduped.json"),
        promoted_candidates_path=paths["promoted_candidates"],
        lifecycle_path=paths["lifecycle"],
        document_repository_path=paths["document_repository"],
        review_decisions_path=paths["review_decisions"],
        project_candidates_path=paths["project_candidates"],
    )
    entries: list[dict[str, Any]] = []
    for item in apply_result["items"]:
        if item["status"] != "applied":
            continue
        asset_id = str(item["asset_id"])
        asset = service.get_asset(asset_id) or {}
        project_id = str(item.get("project_id") or "")
        project_assets = service.get_project_assets(project_id) if project_id else {"documents": []}
        record_ids = set(item.get("written_record_ids") or [])
        document_found = any(str(document.get("document_id") or "") in record_ids for document in project_assets["documents"])
        project_candidate_found = bool(asset.get("project_candidate"))
        entries.append(
            {
                "asset_id": asset_id,
                "asset_found": bool(asset),
                "document_found": document_found,
                "project_candidate_found": project_candidate_found,
                "verified": bool(asset) and (document_found or project_candidate_found),
            }
        )
    return {"verified": all(entry["verified"] for entry in entries), "items": entries}


def verify_written_provenance(apply_result: dict[str, Any], paths: dict[str, Path]) -> list[str]:
    document_rows = load_document_rows(paths["document_repository"])
    project_rows = load_json_array(paths["project_candidates"])
    issues: list[str] = []
    for item in apply_result["items"]:
        if item["status"] != "applied":
            continue
        expected = {
            "apply_batch_id": apply_result["apply_batch_id"],
            "applied_time": apply_result["applied_time"],
            "source_asset_id": item["asset_id"],
            "source_decision_id": item["decision_id"],
        }
        for record_id in item["written_record_ids"]:
            metadata = find_written_metadata(record_id, document_rows, project_rows)
            if any(str(metadata.get(key) or "") != value for key, value in expected.items()):
                issues.append(f"missing_or_invalid_provenance:{record_id}")
    return issues


def load_document_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(item) for item in payload.get("documents", []) if isinstance(item, dict)] if isinstance(payload, dict) else []


def find_written_metadata(record_id: str, documents: list[dict[str, Any]], projects: list[dict[str, Any]]) -> dict[str, Any]:
    for document in documents:
        if str(document.get("document_id") or "") == record_id:
            return dict(document.get("metadata") or {})
    for item in projects:
        candidate = dict(item.get("project_candidate") or {})
        if str(candidate.get("project_candidate_id") or "") == record_id:
            return dict(candidate.get("metadata") or {})
    return {}


def verify_rollback_hints(apply_result: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(apply_result.get("apply_batch_id") or "")
    failures: list[str] = []
    for item in apply_result["items"]:
        if item["status"] != "applied":
            continue
        hint = str(item.get("rollback_hint") or "")
        if batch_id not in hint or not all(record_id in hint for record_id in item["written_record_ids"]):
            failures.append(str(item.get("asset_id") or ""))
    return {"verified": not failures, "failed_asset_ids": failures}


def collect_issues(
    provenance_issues: list[str],
    query_verification: dict[str, Any],
    formal_data_unchanged: bool,
    rollback_check: dict[str, Any],
) -> list[str]:
    issues = list(provenance_issues)
    if not query_verification["verified"]:
        issues.append("asset_query_verification_failed")
    if not formal_data_unchanged:
        issues.append("formal_data_changed")
    if not rollback_check["verified"]:
        issues.append("rollback_hint_verification_failed")
    return issues


def unique_written_files(apply_result: dict[str, Any]) -> list[str]:
    return sorted({path for item in apply_result["items"] for path in item.get("written_files") or []})


def hash_paths(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): hash_file(path) for path in paths}


def hash_file(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the accepted asset controlled apply chain in an isolated diagnostics fixture.")
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE_ROOT))
    parser.add_argument("--report", default=str(DEFAULT_E2E_REPORT_OUTPUT))
    parser.add_argument("--prepare-fixture", action="store_true")
    args = parser.parse_args()

    fixture_root = Path(args.fixture_root)
    paths = prepare_e2e_fixture(fixture_root) if args.prepare_fixture else fixture_paths(fixture_root)
    report = run_controlled_apply_e2e_verification(paths, Path(args.report))
    print(
        "controlled_apply_e2e completed "
        f"batch_id={report['apply_batch_id']} applied={report['applied_count']} issues={len(report['issues'])}"
    )


if __name__ == "__main__":
    main()
