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

from src.discovery_integration.candidate_promoter import DEFAULT_PROMOTED_CANDIDATES_OUTPUT
from src.document.document_entity import DocumentEntity, document_entity_from_mapping
from src.document.document_repository import DEFAULT_DOCUMENT_REPOSITORY_PATH, DocumentRepository


DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY = Path("data/diagnostics/project_candidates_from_discovery.json")
DEFAULT_PROMOTION_APPLY_SUMMARY = Path("data/diagnostics/promotion_apply_summary.json")
APPLY_CONFIDENCE_THRESHOLD = 0.8


def apply_promotions(
    promoted_candidates: list[dict[str, Any]],
    apply: bool = False,
    document_repository_path: Path = DEFAULT_DOCUMENT_REPOSITORY_PATH,
    project_candidates_output: Path = DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY,
    summary_output: Path = DEFAULT_PROMOTION_APPLY_SUMMARY,
    confidence_threshold: float = APPLY_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    document_candidates: list[dict[str, Any]] = []
    project_candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in promoted_candidates:
        promotion_type = str(candidate.get("promotion_type") or "")
        confidence = float(candidate.get("confidence") or 0)
        project_id = str(candidate.get("project_id") or "")
        if promotion_type == "document_entity_candidate":
            if confidence >= confidence_threshold and project_id:
                document = document_from_promoted_candidate(candidate)
                document_candidates.append(
                    {
                        "candidate_id": candidate.get("candidate_id", ""),
                        "project_id": project_id,
                        "document": asdict(document),
                    }
                )
            else:
                skipped.append(skip_reason(candidate, "low_confidence_or_missing_project_id"))
        elif promotion_type == "project_candidate":
            project_candidates.append(project_candidate_from_promoted(candidate))
        else:
            skipped.append(skip_reason(candidate, "unsupported_promotion_type"))

    if apply:
        repository = DocumentRepository(document_repository_path)
        for item in document_candidates:
            repository.create_document(item["document"])
        write_json(project_candidates_output, project_candidates)

    summary = {
        "dry_run": not apply,
        "apply": apply,
        "input_count": len(promoted_candidates),
        "document_entity_candidate_count": len(document_candidates),
        "project_candidate_count": len(project_candidates),
        "skipped_count": len(skipped),
        "document_repository_path": str(document_repository_path),
        "project_candidates_output": str(project_candidates_output),
        "skipped": skipped,
        "document_candidates": document_candidates,
        "project_candidates": project_candidates,
    }
    write_json(summary_output, summary)
    return summary


def document_from_promoted_candidate(candidate: dict[str, Any]) -> DocumentEntity:
    payload = dict(candidate.get("document_candidate") or {})
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "project_id": str(candidate.get("project_id") or ""),
            "promotion_type": str(candidate.get("promotion_type") or ""),
            "promotion_source": str(candidate.get("source") or ""),
            "promotion_confidence": float(candidate.get("confidence") or 0),
            "promotion_status": "applied_candidate",
        }
    )
    payload["metadata"] = metadata
    return document_entity_from_mapping(payload)


def project_candidate_from_promoted(candidate: dict[str, Any]) -> dict[str, Any]:
    document_candidate = dict(candidate.get("document_candidate") or {})
    metadata = dict(document_candidate.get("metadata") or {})
    metadata.update(
        {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "promotion_source": str(candidate.get("source") or ""),
            "promotion_confidence": float(candidate.get("confidence") or 0),
            "promotion_status": "candidate_only",
        }
    )
    document_candidate["metadata"] = metadata
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "project_candidate": document_candidate,
        "confidence": float(candidate.get("confidence") or 0),
        "source": str(candidate.get("source") or ""),
    }


def skip_reason(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "promotion_type": str(candidate.get("promotion_type") or ""),
        "project_id": str(candidate.get("project_id") or ""),
        "confidence": float(candidate.get("confidence") or 0),
        "reason": reason,
    }


def load_promoted_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected promoted candidate JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply high-confidence promoted candidates into diagnostic repositories.")
    parser.add_argument("--input", default=str(DEFAULT_PROMOTED_CANDIDATES_OUTPUT))
    parser.add_argument("--document-repository", default=str(DEFAULT_DOCUMENT_REPOSITORY_PATH))
    parser.add_argument("--project-candidates-output", default=str(DEFAULT_PROJECT_CANDIDATES_FROM_DISCOVERY))
    parser.add_argument("--summary-output", default=str(DEFAULT_PROMOTION_APPLY_SUMMARY))
    parser.add_argument("--confidence-threshold", type=float, default=APPLY_CONFIDENCE_THRESHOLD)
    parser.add_argument("--apply", action="store_true", help="Persist document repository and project candidate diagnostics.")
    args = parser.parse_args()

    summary = apply_promotions(
        load_promoted_candidates(Path(args.input)),
        apply=args.apply,
        document_repository_path=Path(args.document_repository),
        project_candidates_output=Path(args.project_candidates_output),
        summary_output=Path(args.summary_output),
        confidence_threshold=args.confidence_threshold,
    )
    print(
        "promotion_apply completed "
        f"dry_run={summary['dry_run']} documents={summary['document_entity_candidate_count']} "
        f"project_candidates={summary['project_candidate_count']}"
    )


if __name__ == "__main__":
    main()
