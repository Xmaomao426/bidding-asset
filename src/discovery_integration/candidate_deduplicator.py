from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery_integration.asset_candidate_importer import DEFAULT_ASSET_CANDIDATES_OUTPUT
from src.matcher.record_matcher import project_numbers


DEFAULT_DEDUPED_CANDIDATES_OUTPUT = Path("data/diagnostics/asset_candidates_deduped.json")
DEFAULT_DEDUP_SUMMARY_OUTPUT = Path("data/diagnostics/candidate_dedup_summary.json")
TITLE_SIMILARITY_THRESHOLD = 0.88


def deduplicate_candidates(
    candidates: list[dict[str, Any]],
    title_similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD,
    independent_source_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups = _build_duplicate_groups(candidates, title_similarity_threshold, independent_source_types or set())
    deduped = [_primary_candidate_view(group, candidates) for group in groups]
    duplicate_group_count = sum(1 for group in groups if len(group["indexes"]) > 1)
    summary = {
        "input_count": len(candidates),
        "output_count": len(deduped),
        "duplicate_group_count": duplicate_group_count,
        "removed_duplicate_count": len(candidates) - len(deduped),
        "title_similarity_threshold": title_similarity_threshold,
        "independent_source_types": sorted(independent_source_types or set()),
        "reason_counts": _reason_counts(groups),
    }
    return deduped, summary


def write_dedup_outputs(
    deduped_candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    deduped_output: Path = DEFAULT_DEDUPED_CANDIDATES_OUTPUT,
    summary_output: Path = DEFAULT_DEDUP_SUMMARY_OUTPUT,
) -> None:
    deduped_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    deduped_output.write_text(json.dumps(deduped_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_duplicate_groups(
    candidates: list[dict[str, Any]],
    title_similarity_threshold: float,
    independent_source_types: set[str],
) -> list[dict[str, Any]]:
    parent = list(range(len(candidates)))
    reasons: dict[int, set[str]] = {index: set() for index in range(len(candidates))}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int, reason: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root
            reasons[left_root].update(reasons[right_root])
        reasons[find(left)].add(reason)

    comparable = [
        index for index, candidate in enumerate(candidates)
        if str(candidate.get("source_type") or "") not in independent_source_types
    ]
    for left_position, left in enumerate(comparable):
        for right in comparable[left_position + 1:]:
            duplicate_reasons = duplicate_reasons_for_pair(candidates[left], candidates[right], title_similarity_threshold)
            for reason in duplicate_reasons:
                union(left, right, reason)

    grouped: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        grouped.setdefault(find(index), []).append(index)
    return [
        {
            "indexes": indexes,
            "reasons": sorted(reasons.get(find(indexes[0]), set())) or ["unique"],
        }
        for indexes in grouped.values()
    ]


def duplicate_reasons_for_pair(
    left: dict[str, Any], right: dict[str, Any], title_similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD
) -> list[str]:
    reasons: list[str] = []
    left_url = normalize_url(str(left.get("source_url") or ""))
    right_url = normalize_url(str(right.get("source_url") or ""))
    if left_url and right_url and left_url == right_url:
        reasons.append("source_url_exact")

    left_numbers = numbers_for_candidate(left)
    right_numbers = numbers_for_candidate(right)
    if left_numbers and right_numbers and left_numbers & right_numbers:
        reasons.append("project_number_exact")

    similarity = title_similarity(str(left.get("source_title") or ""), str(right.get("source_title") or ""))
    if similarity >= title_similarity_threshold:
        reasons.append(f"source_title_similar:{similarity:.2f}")
    return reasons


def _primary_candidate_view(group: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    group_candidates = [candidates[index] for index in group["indexes"]]
    primary = max(group_candidates, key=lambda item: (float(item.get("confidence") or 0), -group_candidates.index(item)))
    group_id = duplicate_group_id(group_candidates)
    reasons = list(group["reasons"])
    return {
        "candidate_id": str(primary.get("candidate_id") or ""),
        "duplicate_group_id": group_id,
        "is_primary": True,
        "duplicate_reason": reasons,
        "merged_sources": [_merged_source(candidate, reasons, candidate is primary) for candidate in group_candidates],
        "source_type": str(primary.get("source_type") or ""),
        "source_title": str(primary.get("source_title") or ""),
        "source_url": str(primary.get("source_url") or ""),
        "matched_project_id": str(primary.get("matched_project_id") or ""),
        "confidence": float(primary.get("confidence") or 0),
        "status": str(primary.get("status") or ""),
        "source_trace": dict(primary.get("source_trace") or {}),
    }


def _merged_source(candidate: dict[str, Any], reasons: list[str], is_primary: bool) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "source_type": str(candidate.get("source_type") or ""),
        "source_title": str(candidate.get("source_title") or ""),
        "source_url": str(candidate.get("source_url") or ""),
        "confidence": float(candidate.get("confidence") or 0),
        "is_primary": is_primary,
        "duplicate_reason": reasons,
        "source_trace": dict(candidate.get("source_trace") or {}),
    }


def duplicate_group_id(group_candidates: list[dict[str, Any]]) -> str:
    parts = sorted(str(candidate.get("candidate_id") or "") for candidate in group_candidates)
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"duplicate_group_{digest}"


def numbers_for_candidate(candidate: dict[str, Any]) -> set[str]:
    return project_numbers(
        {
            "project_name": candidate.get("source_title", ""),
            "source_file": candidate.get("source_url", ""),
            "content": "",
            "note": "",
        }
    )


def normalize_url(value: str) -> str:
    return value.strip().rstrip("/").lower()


def normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    for token in ("bidnotice", "awardnotice", "notice", "announcement"):
        value = value.replace(token, "")
    return value


def title_similarity(left: str, right: str) -> float:
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _reason_counts(groups: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in groups:
        for reason in group["reasons"]:
            counts[reason.split(":", 1)[0]] = counts.get(reason.split(":", 1)[0], 0) + 1
    return counts


def load_asset_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected asset candidate JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate Asset Candidate diagnostics.")
    parser.add_argument("--input", default=str(DEFAULT_ASSET_CANDIDATES_OUTPUT))
    parser.add_argument("--output", default=str(DEFAULT_DEDUPED_CANDIDATES_OUTPUT))
    parser.add_argument("--summary", default=str(DEFAULT_DEDUP_SUMMARY_OUTPUT))
    parser.add_argument("--title-threshold", type=float, default=TITLE_SIMILARITY_THRESHOLD)
    args = parser.parse_args()

    deduped, summary = deduplicate_candidates(load_asset_candidates(Path(args.input)), args.title_threshold)
    write_dedup_outputs(deduped, summary, Path(args.output), Path(args.summary))
    print(f"wrote {args.output} candidates={len(deduped)} removed={summary['removed_duplicate_count']}")


if __name__ == "__main__":
    main()
