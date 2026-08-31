from __future__ import annotations

from pathlib import Path
from typing import Any

from strategies.base import EnrichmentAttempt, StrategyContext, is_relevant


RESULT_DOC_TYPES = {"结果公告", "合同"}


def same_or_related_project(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_name = left.get("project_name") or Path(left.get("source_file", "")).stem
    right_name = right.get("project_name") or Path(right.get("source_file", "")).stem
    right_text = " ".join(str(right.get(key, "")) for key in ("project_name", "source_file", "customer", "content", "note"))
    return is_relevant(right_text, left) or is_relevant(left_name, {"project_name": right_name, "source_file": right.get("source_file", "")})


def run(record: dict[str, Any], context: StrategyContext) -> EnrichmentAttempt:
    for candidate in context.records:
        if candidate is record:
            continue
        if candidate.get("doc_type") not in RESULT_DOC_TYPES:
            continue
        if not candidate.get("winner") and not candidate.get("award_amount"):
            continue
        if not same_or_related_project(record, candidate):
            continue
        source_name = candidate.get("source_file", "")
        source_path = Path(source_name)
        source_url = source_path.resolve().as_uri() if source_name and source_path.exists() else ""
        return EnrichmentAttempt(
            source_type="local_result_announcement",
            method="match_local_result_document",
            success=True,
            source_url=source_url,
            source_title=source_name,
            winner=candidate.get("winner", ""),
            award_amount=candidate.get("award_amount", ""),
            message="matched_local_result_document",
        )
    return EnrichmentAttempt(
        source_type="local_result_announcement",
        method="match_local_result_document",
        success=False,
        message="no_matching_local_result_document",
    )
