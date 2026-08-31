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

from src.discovery_integration.candidate_deduplicator import DEFAULT_DEDUPED_CANDIDATES_OUTPUT
from src.document.document_entity import build_document_id, normalize_file_type


DEFAULT_PROMOTED_CANDIDATES_OUTPUT = Path("data/diagnostics/promoted_candidates.json")


def promote_candidates(asset_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [promote_candidate(candidate) for candidate in asset_candidates if should_promote_candidate(candidate)]


def should_promote_candidate(candidate: dict[str, Any]) -> bool:
    if "is_primary" not in candidate:
        return True
    return bool(candidate.get("is_primary"))


def promote_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    status = str(candidate.get("status") or "")
    matched_project_id = str(candidate.get("matched_project_id") or "")
    if status == "project_asset_candidate" and matched_project_id:
        promotion_type = "document_entity_candidate"
        project_id = matched_project_id
        document_candidate = build_document_candidate(candidate)
    else:
        promotion_type = "project_candidate"
        project_id = ""
        document_candidate = build_project_candidate(candidate)
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "promotion_type": promotion_type,
        "project_id": project_id,
        "document_candidate": document_candidate,
        "confidence": float(candidate.get("confidence") or 0),
        "source": str(candidate.get("source_type") or ""),
        "source_trace": dict(candidate.get("source_trace") or {}),
    }


def build_document_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    source_url = str(candidate.get("source_url") or "")
    title = str(candidate.get("source_title") or "")
    file_name = source_url.rsplit("/", 1)[-1] if "/" in source_url else ""
    if not file_name or "." not in file_name:
        file_name = safe_file_name(title) or f"{candidate.get('candidate_id', 'asset_candidate')}.html"
    source_type = str(candidate.get("source_type") or "external")
    return {
        "document_id": build_document_id(source_type, "", source_url, file_name),
        "source_type": source_type,
        "source_path": "",
        "source_url": source_url,
        "file_name": file_name,
        "file_type": normalize_file_type(file_name),
        "metadata": {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "source_title": title,
            "source_trace": dict(candidate.get("source_trace") or {}),
            "promotion_status": "candidate_only",
        },
    }


def build_project_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    source_trace = dict(candidate.get("source_trace") or {})
    extracted_fields = dict(source_trace.get("extracted_fields") or {})
    project_name = str(extracted_fields.get("project_name") or "").strip()
    if not project_name:
        project_name = str(candidate.get("source_title") or "")
    return {
        "project_candidate_id": build_project_candidate_id(candidate),
        "project_name": project_name,
        "source_url": str(candidate.get("source_url") or ""),
        "metadata": {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "source_type": str(candidate.get("source_type") or ""),
            "source_trace": source_trace,
            "promotion_status": "candidate_only",
        },
    }


def safe_file_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value.strip())
    cleaned = cleaned.strip("._")
    return (cleaned[:80] + ".html") if cleaned else ""


def build_project_candidate_id(candidate: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        "\n".join(
            [
                str(candidate.get("candidate_id") or ""),
                str(candidate.get("source_title") or ""),
                str(candidate.get("source_url") or ""),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"project_candidate_{digest}"


def load_asset_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected asset candidate JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_promoted_candidates(
    promoted_candidates: list[dict[str, Any]], output_path: Path = DEFAULT_PROMOTED_CANDIDATES_OUTPUT
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(promoted_candidates, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote Asset Candidate diagnostics into project/document candidate diagnostics.")
    parser.add_argument("--input", default=str(DEFAULT_DEDUPED_CANDIDATES_OUTPUT))
    parser.add_argument("--output", default=str(DEFAULT_PROMOTED_CANDIDATES_OUTPUT))
    args = parser.parse_args()

    promoted = promote_candidates(load_asset_candidates(Path(args.input)))
    write_promoted_candidates(promoted, Path(args.output))
    print(f"wrote {args.output} promoted={len(promoted)}")


if __name__ == "__main__":
    main()
