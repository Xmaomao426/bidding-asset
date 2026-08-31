from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery_integration.asset_candidate_importer import build_candidate_id, first_value
from src.discovery_integration.candidate_deduplicator import deduplicate_candidates
from src.discovery_integration.candidate_promoter import promote_candidates
from src.review.review_queue import build_review_queue


DEFAULT_TIANIANCHA_NEW_RECORD_CANDIDATES = Path("data/diagnostics/tianyancha_new_record_candidates.json")
DEFAULT_PRODUCTION_ASSET_CANDIDATES_OUTPUT = Path("data/diagnostics/production_asset_candidates.json")
DEFAULT_PRODUCTION_IMPORT_SUMMARY_OUTPUT = Path("data/diagnostics/production_asset_import_summary.json")
FORMAL_DATA_PATHS = [
    Path("data/cache/final_records.json"),
    Path("data/overrides/manual_overrides.json"),
    Path("招投标.xlsx"),
]


def import_production_assets(
    tianyancha_path: Path = DEFAULT_TIANIANCHA_NEW_RECORD_CANDIDATES,
    output_path: Path = DEFAULT_PRODUCTION_ASSET_CANDIDATES_OUTPUT,
    summary_path: Path = DEFAULT_PRODUCTION_IMPORT_SUMMARY_OUTPUT,
    *,
    write_outputs: bool = True,
) -> dict[str, Any]:
    """Import real Tianyancha export candidates into diagnostics-only Asset Candidates."""
    before_hashes = formal_data_hashes(FORMAL_DATA_PATHS)
    payload = load_json(tianyancha_path)
    rows = tianyancha_candidate_rows(payload)
    candidates = [tianyancha_row_to_asset_candidate(row) for row in rows]
    deduped_candidates, dedup_summary = deduplicate_candidates(candidates)
    promoted_candidates = promote_candidates(deduped_candidates)
    review_queue = build_review_queue(deduped_candidates, [], promoted_candidates)
    after_hashes = formal_data_hashes(FORMAL_DATA_PATHS)
    summary = build_summary(
        tianyancha_path,
        output_path,
        summary_path,
        rows,
        candidates,
        deduped_candidates,
        dedup_summary,
        promoted_candidates,
        review_queue,
        before_hashes,
        after_hashes,
    )

    result = {
        "asset_candidates": candidates,
        "deduped_candidates": deduped_candidates,
        "promoted_candidates": promoted_candidates,
        "review_queue": review_queue,
        "summary": summary,
    }
    if write_outputs:
        write_json(output_path, candidates)
        write_json(summary_path, summary)
    return result


def tianyancha_candidate_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    rows = payload.get("new_record_candidates")
    if isinstance(rows, list):
        return [dict(item) for item in rows if isinstance(item, dict)]
    return []


def tianyancha_row_to_asset_candidate(row: dict[str, Any]) -> dict[str, Any]:
    source_type = first_value(row, ["source_type", "source"]) or "tianyancha_export"
    source_title = first_value(row, ["source_title", "title", "project_name", "content"])
    source_url = first_value(row, ["source_url", "url", "detail_url", "link"])
    source_file = first_value(row, ["source_file", "file", "input_file"])
    discovered_time = normalize_discovered_time(
        first_value(row, ["discovered_time", "publish_date", "date", "generated_at"])
    )
    candidate_id = first_value(row, ["candidate_id", "id"]) or build_candidate_id(source_type, source_title, source_url)
    source_trace = {
        "source_type": source_type,
        "source_name": "tianyancha_new_record_candidates",
        "source_url": source_url,
        "source_file": source_file,
        "discovered_time": discovered_time,
        "pipeline_stage": "production_asset_import",
        "row_id": first_value(row, ["row_id"]),
        "source_row": first_value(row, ["source_row"]),
    }
    return {
        "candidate_id": candidate_id,
        "source_type": source_type,
        "source_title": source_title,
        "source_url": source_url,
        "source_file": source_file,
        "discovered_time": discovered_time,
        "matched_project_id": "",
        "confidence": confidence_for_tianyancha_row(row),
        "status": "new_project_candidate",
        "source_trace": source_trace,
        "metadata": {
            "project_name": first_value(row, ["project_name"]) or source_title,
            "publish_date": first_value(row, ["publish_date", "date"]),
            "region": first_value(row, ["region"]),
            "notice_type": first_value(row, ["notice_type"]),
            "customer": first_value(row, ["customer", "buyer", "purchaser"]),
            "winner": first_value(row, ["winner", "winner_company"]),
            "award_amount": first_value(row, ["award_amount"]),
            "matched_keywords": list(row.get("matched_keywords") or []),
            "risk_flags": list(row.get("risk_flags") or []),
            "review_status": first_value(row, ["review_status"]),
        },
    }


def confidence_for_tianyancha_row(row: dict[str, Any]) -> float:
    if "confidence" in row:
        try:
            return round(float(row["confidence"]), 4)
        except (TypeError, ValueError):
            pass
    risk_flags = set(str(flag) for flag in row.get("risk_flags") or [])
    if "weak_keyword_match" in risk_flags or "unusual_notice_type" in risk_flags:
        return 0.55
    return 0.65


def normalize_discovered_time(value: str) -> str:
    value = value.strip()
    if not value:
        return utc_now()
    if "T" in value:
        return value
    return f"{value}T00:00:00Z"


def build_summary(
    input_path: Path,
    output_path: Path,
    summary_path: Path,
    rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    deduped_candidates: list[dict[str, Any]],
    dedup_summary: dict[str, Any],
    promoted_candidates: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
    before_hashes: dict[str, str],
    after_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "phase": "V3 Phase23 Production Asset Import MVP",
        "input_path": str(input_path),
        "output_path": str(output_path),
        "source": "tianyancha_export",
        "input_count": len(rows),
        "asset_candidate_count": len(candidates),
        "deduped_candidate_count": len(deduped_candidates),
        "promoted_candidate_count": len(promoted_candidates),
        "review_queue_count": len(review_queue),
        "source_type_counts": count_by(candidates, "source_type"),
        "status_counts": count_by(candidates, "status"),
        "dedup_summary": dedup_summary,
        "formal_data_hash_before": before_hashes,
        "formal_data_hash_after": after_hashes,
        "formal_data_unchanged": before_hashes == after_hashes,
        "written_files": [str(output_path), str(summary_path)],
        "forbidden_writes": [
            "招投标.xlsx",
            "data/cache/final_records.json",
            "data/overrides/manual_overrides.json",
        ],
        "downstream_consumed_in_memory": {
            "candidate_deduplicator": True,
            "candidate_promoter": True,
            "review_queue": True,
        },
    }


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def formal_data_hashes(paths: list[Path]) -> dict[str, str]:
    return {str(path): sha256_file(path) for path in paths}


def sha256_file(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import production Tianyancha candidates into diagnostics Asset Candidates.")
    parser.add_argument("--tianyancha", default=str(DEFAULT_TIANIANCHA_NEW_RECORD_CANDIDATES))
    parser.add_argument("--output", default=str(DEFAULT_PRODUCTION_ASSET_CANDIDATES_OUTPUT))
    parser.add_argument("--summary", default=str(DEFAULT_PRODUCTION_IMPORT_SUMMARY_OUTPUT))
    args = parser.parse_args()

    result = import_production_assets(Path(args.tianyancha), Path(args.output), Path(args.summary))
    summary = result["summary"]
    print(
        f"wrote {args.output} candidates={summary['asset_candidate_count']} "
        f"deduped={summary['deduped_candidate_count']} promoted={summary['promoted_candidate_count']} "
        f"formal_data_unchanged={summary['formal_data_unchanged']}"
    )


if __name__ == "__main__":
    main()
