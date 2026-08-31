from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


GOLD_SCHEMA_VERSION = "semantic-gold/v1"
GOLD_LOCK_VERSION = "semantic-gold-lock/v1"


def build_gold_skeleton(manifest: Mapping[str, Any], split_config: Mapping[str, Any]) -> dict[str, Any]:
    development_ids = set(str(item) for item in split_config.get("development_sample_ids") or [])
    evaluation_ids = set(str(item) for item in split_config.get("evaluation_sample_ids") or [])
    manifest_ids = {str(item.get("sample_id") or "") for item in manifest.get("samples") or []}
    configured_ids = development_ids | evaluation_ids
    included_ids = {
        str(item.get("sample_id") or "")
        for item in manifest.get("samples") or []
        if str(item.get("candidate_status") or "") == "included"
    }
    if development_ids & evaluation_ids:
        raise ValueError("Development and evaluation sample IDs must not overlap")
    if configured_ids != included_ids:
        missing = sorted(included_ids - configured_ids)
        extra = sorted(configured_ids - manifest_ids)
        raise ValueError(f"Split must cover every included sample exactly once; missing={missing} extra={extra}")

    samples: list[dict[str, Any]] = []
    for item in manifest.get("samples") or []:
        sample_id = str(item.get("sample_id") or "")
        included = str(item.get("candidate_status") or "") == "included"
        split = "development" if sample_id in development_ids else "evaluation" if sample_id in evaluation_ids else "excluded"
        samples.append(
            {
                "sample_id": sample_id,
                "file_name": str(item.get("file_name") or ""),
                "source_sha256": str(item.get("sha256") or ""),
                "split": split,
                "inclusion_status": "included" if included else "excluded",
                "exclusion_reason": str(item.get("exclusion_reason") or ""),
                "annotation_status": "pending_human_review" if included else "excluded",
                "difficulty_features": list(item.get("difficulty_features") or []),
                "document_type": {
                    "annotation_status": "pending_human_review" if included else "excluded",
                    "status": "",
                    "value": "",
                    "alternatives": [],
                    "evidence": [],
                },
                "facts": [],
                "ambiguities": [],
                "conflicts": [],
                "gold_notes": [],
            }
        )
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "gold_status": "structure_ready_values_pending_human_review",
        "independence_rule": "Gold values must be confirmed from original source evidence; Rule or AI output is reference only.",
        "allowed_fact_fields": [
            "project_name",
            "project_number",
            "customer",
            "winner",
            "amount",
            "project_content",
            "date",
        ],
        "allowed_statuses": ["known", "unknown", "ambiguous", "conflict"],
        "amount_roles": ["budget", "ceiling", "award", "contract", "other"],
        "date_roles": ["bid_open_time", "publish_date", "signing_date", "other"],
        "sample_count": len(samples),
        "development_sample_count": sum(1 for sample in samples if sample["split"] == "development"),
        "evaluation_sample_count": sum(1 for sample in samples if sample["split"] == "evaluation"),
        "excluded_sample_count": sum(1 for sample in samples if sample["split"] == "excluded"),
        "samples": samples,
    }


def assert_gold_ready(gold: Mapping[str, Any], split: str = "evaluation") -> None:
    pending = [
        str(sample.get("sample_id") or "")
        for sample in gold.get("samples") or []
        if str(sample.get("split") or "") == split
        and str(sample.get("inclusion_status") or "") == "included"
        and str(sample.get("annotation_status") or "") != "complete"
    ]
    if pending:
        raise ValueError(f"Gold is not ready for scoring; pending {split} samples: {pending}")
    validate_gold(gold, require_locked=True)


def validate_gold(gold: Mapping[str, Any], *, require_locked: bool = False) -> dict[str, int]:
    if str(gold.get("schema_version") or "") != GOLD_SCHEMA_VERSION:
        raise ValueError("Unsupported Gold schema version")
    allowed_fields = set(str(item) for item in gold.get("allowed_fact_fields") or [])
    allowed_statuses = set(str(item) for item in gold.get("allowed_statuses") or [])
    samples = list(gold.get("samples") or [])
    if int(gold.get("sample_count") or 0) != len(samples):
        raise ValueError("Gold sample_count does not match samples")

    counts = {"known": 0, "unknown": 0, "ambiguous": 0, "conflict": 0, "known_with_evidence": 0}
    included = 0
    for sample in samples:
        inclusion_status = str(sample.get("inclusion_status") or "")
        if inclusion_status == "excluded":
            if str(sample.get("annotation_status") or "") != "excluded":
                raise ValueError(f"Excluded Gold sample must remain excluded: {sample.get('sample_id')}")
            continue
        included += 1
        if str(sample.get("annotation_status") or "") != "complete":
            raise ValueError(f"Gold sample is not complete: {sample.get('sample_id')}")
        document_type = dict(sample.get("document_type") or {})
        if str(document_type.get("annotation_status") or "") != "complete":
            raise ValueError(f"Gold document type is not complete: {sample.get('sample_id')}")
        _validate_semantic_item(document_type, allowed_statuses, counts, f"{sample.get('sample_id')}:document_type")
        for index, fact in enumerate(sample.get("facts") or []):
            field_name = str(fact.get("field_name") or "")
            if field_name not in allowed_fields:
                raise ValueError(f"Unsupported Gold field {field_name}: {sample.get('sample_id')}")
            _validate_semantic_item(
                dict(fact),
                allowed_statuses,
                counts,
                f"{sample.get('sample_id')}:facts[{index}]",
            )

    if int(gold.get("included_sample_count") or included) != included:
        raise ValueError("Gold included_sample_count does not match samples")
    if require_locked or str(gold.get("gold_status") or "") == "locked":
        lock = dict(gold.get("lock") or {})
        if str(gold.get("gold_status") or "") != "locked":
            raise ValueError("Gold is not locked")
        if str(lock.get("lock_version") or "") != GOLD_LOCK_VERSION:
            raise ValueError("Gold lock version is missing or unsupported")
        expected = gold_content_sha256(gold)
        if str(lock.get("content_sha256") or "") != expected:
            raise ValueError("Gold lock hash mismatch")
    return counts


def lock_gold(gold: Mapping[str, Any], *, locked_on: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(gold))
    result["gold_status"] = "locked"
    result["lock"] = {
        "lock_version": GOLD_LOCK_VERSION,
        "locked_on": locked_on,
        "content_sha256": "",
    }
    result["lock"]["content_sha256"] = gold_content_sha256(result)
    validate_gold(result, require_locked=True)
    return result


def gold_content_sha256(gold: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(gold))
    lock = dict(payload.get("lock") or {})
    if lock:
        lock["content_sha256"] = ""
        payload["lock"] = lock
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()


def _validate_semantic_item(
    item: Mapping[str, Any],
    allowed_statuses: set[str],
    counts: dict[str, int],
    label: str,
) -> None:
    status = str(item.get("status") or "")
    value = str(item.get("value") or "")
    evidence = list(item.get("evidence") or [])
    if status not in allowed_statuses:
        raise ValueError(f"Unsupported Gold status {status}: {label}")
    if status == "known":
        if not value:
            raise ValueError(f"Known Gold item requires a value: {label}")
        if not evidence:
            raise ValueError(f"Known Gold item requires evidence: {label}")
        for evidence_item in evidence:
            if str(evidence_item.get("availability") or "") != "available":
                raise ValueError(f"Known Gold evidence must be available: {label}")
            locator = dict(evidence_item.get("locator") or {})
            if not locator or not str(locator.get("kind") or ""):
                raise ValueError(f"Known Gold evidence requires a locator: {label}")
        counts["known_with_evidence"] += 1
    elif value:
        raise ValueError(f"Non-known Gold item must not contain a value: {label}")
    counts[status] += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a pending human-review Gold skeleton from the manifest and locked split.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    split_config = json.loads(Path(args.split).read_text(encoding="utf-8"))
    gold = build_gold_skeleton(manifest, split_config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {output} samples={gold['sample_count']} gold_status={gold['gold_status']}")


if __name__ == "__main__":
    main()
