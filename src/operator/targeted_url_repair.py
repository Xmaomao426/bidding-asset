"""Audited, ID-scoped JSON repair support for explicitly authorized URL corrections."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.acquisition.inbox.acquisition_inbox import normalize_url, url_source_key


EXTRACTED_FIELD_NAMES = (
    "project_name", "customer", "project_number", "content", "budget",
    "bid_open_time", "winner", "award_amount", "doc_type",
)

@dataclass(frozen=True)
class TargetedUrlRepairPaths:
    inbox: Path
    projects: Path
    documents: Path
    links: Path
    excel: Path
    backup_dir: Path


def backup_target_files(paths: TargetedUrlRepairPaths, repair_id: str) -> dict[str, str]:
    """Create immutable pre-repair copies before any authorized data mutation."""
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    backups: dict[str, str] = {}
    for source in (paths.inbox, paths.projects, paths.documents, paths.links, paths.excel):
        if not source.exists():
            raise FileNotFoundError(f"Repair source not found: {source}")
        target = paths.backup_dir / f"{source.stem}_before_{repair_id}{source.suffix}"
        if target.exists():
            raise FileExistsError(f"Repair backup already exists: {target}")
        shutil.copy2(source, target)
        backups[str(source)] = str(target)
    return backups


def repair_target_json_records(
    *,
    paths: TargetedUrlRepairPaths,
    source_url: str,
    inbox_id: str,
    project_id: str,
    document_id: str,
    link_id: str,
    corrected_fields: dict[str, str],
    repair_id: str,
    reason: str,
    repaired_time: str | None = None,
) -> dict[str, Any]:
    """Repair exactly one pre-identified Inbox/Project/Document/Relation set."""
    timestamp = repaired_time or utc_now()
    specifications = (
        ("inbox", paths.inbox, "inbox_id", inbox_id),
        ("project", paths.projects, "project_id", project_id),
        ("document", paths.documents, "document_id", document_id),
        ("relation", paths.links, "link_id", link_id),
    )
    loaded: dict[str, tuple[Path, list[dict[str, Any]], dict[str, Any]]] = {}
    for entity_type, path, id_field, expected_id in specifications:
        items = load_array(path)
        matches = [item for item in items if str(item.get(id_field) or "") == expected_id]
        if len(matches) != 1 or source_url not in json.dumps(matches[0], ensure_ascii=False):
            raise ValueError(f"{entity_type} target did not uniquely match ID and source URL: {expected_id}")
        loaded[entity_type] = (path, items, matches[0])

    changes_by_entity: dict[str, list[dict[str, str]]] = {}
    for entity_type, (_path, _items, target) in loaded.items():
        changes: list[dict[str, str]] = []
        if entity_type == "inbox":
            processing = target.setdefault("processing_result", {})
            for field, new_value in corrected_fields.items():
                set_value(processing, field, new_value, f"processing_result.{field}", changes)
            set_value(processing, "source_url", source_url, "processing_result.source_url", changes)
            set_value(processing, "adapter", "ccgp", "processing_result.adapter", changes)
        else:
            update_extracted_fields(target, corrected_fields, "", changes)
            if entity_type == "project":
                set_value(target, "project_name", corrected_fields["project_name"], "project_name", changes)
        audit = {
            "repair_id": repair_id,
            "repaired_time": timestamp,
            "reason": reason,
            "source_url": source_url,
            "changes": changes,
        }
        history = target.setdefault("repair_history", [])
        if any(str(entry.get("repair_id") or "") == repair_id for entry in history if isinstance(entry, dict)):
            raise ValueError(f"Repair already recorded for {entity_type}: {repair_id}")
        history.append(audit)
        changes_by_entity[entity_type] = changes

    for path, items, _target in loaded.values():
        write_array_atomic(path, items)
    return {
        "repair_id": repair_id,
        "repaired_time": timestamp,
        "reason": reason,
        "source_url": source_url,
        "ids": {"inbox_id": inbox_id, "project_id": project_id, "document_id": document_id, "link_id": link_id},
        "changes": changes_by_entity,
    }


def converge_url_inbox_records(
    *,
    inbox_path: Path,
    source_url: str,
    primary_inbox_id: str,
    duplicate_inbox_ids: list[str],
    harness_inbox_id: str,
    expected_asset_id: str,
    repair_id: str,
    repaired_time: str | None = None,
) -> dict[str, Any]:
    """Hide only explicitly identified duplicate/harness tasks while preserving their audit history."""
    items = load_array(inbox_path)
    normalized = normalize_url(source_url)
    source_key = url_source_key(normalized)
    target_ids = {primary_inbox_id, *duplicate_inbox_ids, harness_inbox_id}
    targets = [item for item in items if str(item.get("inbox_id") or "") in target_ids]
    if len(targets) != len(target_ids):
        found = {str(item.get("inbox_id") or "") for item in targets}
        raise ValueError(f"Inbox convergence IDs did not match exactly: missing={sorted(target_ids - found)}")
    for item in targets:
        if normalize_url(str(item.get("source_url") or "")) != normalized:
            raise ValueError(f"Inbox convergence URL mismatch: {item.get('inbox_id', '')}")
    primary = next(item for item in targets if str(item.get("inbox_id") or "") == primary_inbox_id)
    if (
        str(primary.get("status") or "") != "COMPLETED"
        or expected_asset_id not in [str(value) for value in primary.get("generated_asset_ids", [])]
        or str((primary.get("processing_result") or {}).get("project_number") or "") != "KLSBJZ-2026-011"
    ):
        raise ValueError("Primary Inbox record failed status, asset, or structured-field checks")

    timestamp = repaired_time or utc_now()
    attempts = [attempt_snapshot(item) for item in sorted(targets, key=lambda row: str(row.get("created_time") or ""))]
    duplicate_ids = set(duplicate_inbox_ids)
    changes: dict[str, list[str]] = {}
    for item in targets:
        inbox_id = str(item.get("inbox_id") or "")
        changed_fields: list[str] = []
        values: dict[str, Any] = {
            "normalized_url": normalized,
            "source_key": source_key,
            "duplicate_of": primary_inbox_id if inbox_id in duplicate_ids else "",
            "hidden_from_operator": inbox_id != primary_inbox_id,
            "source_origin": (
                "browser_acceptance_harness" if inbox_id == harness_inbox_id
                else ("browser_acceptance" if inbox_id in duplicate_ids else "operator")
            ),
            "attempt_history": attempts if inbox_id == primary_inbox_id else [attempt_snapshot(item)],
        }
        for field, value in values.items():
            if item.get(field) != value:
                item[field] = value
                changed_fields.append(field)
        history = item.setdefault("repair_history", [])
        if not isinstance(history, list):
            raise ValueError(f"Invalid repair_history: {inbox_id}")
        if any(str(entry.get("repair_id") or "") == repair_id for entry in history if isinstance(entry, dict)):
            raise ValueError(f"Inbox convergence already recorded: {inbox_id}")
        history.append({
            "repair_id": repair_id,
            "repaired_time": timestamp,
            "reason": "收敛同一 URL 的普通资料待办展示并隔离浏览器验收辅助任务",
            "source_url": source_url,
            "primary_inbox_id": primary_inbox_id,
            "changed_fields": changed_fields,
        })
        changes[inbox_id] = changed_fields
    write_array_atomic(inbox_path, items)
    return {
        "repair_id": repair_id,
        "repaired_time": timestamp,
        "source_url": source_url,
        "source_key": source_key,
        "primary_inbox_id": primary_inbox_id,
        "hidden_inbox_ids": [*duplicate_inbox_ids, harness_inbox_id],
        "attempt_count": len(attempts),
        "changes": changes,
    }


def attempt_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "")
    return {
        "attempt_id": f"historical_{str(item.get('inbox_id') or '')}",
        "source_inbox_id": str(item.get("inbox_id") or ""),
        "source_origin": str(item.get("source_origin") or "historical"),
        "started_time": str(item.get("created_time") or ""),
        "finished_time": str(item.get("completed_time") or item.get("failed_time") or ""),
        "status": status,
        "error_type": str(item.get("error_type") or ""),
        "error_message": str(item.get("error_message") or ""),
    }


def update_extracted_fields(
    value: Any,
    corrected_fields: dict[str, str],
    path: str,
    changes: list[dict[str, str]],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key == "extracted_fields" and isinstance(child, dict):
                for field in EXTRACTED_FIELD_NAMES:
                    new_value = str(corrected_fields.get(field) or "")
                    set_value(child, field, new_value, f"{child_path}.{field}", changes)
            else:
                update_extracted_fields(child, corrected_fields, child_path, changes)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            update_extracted_fields(child, corrected_fields, f"{path}[{index}]", changes)


def set_value(
    target: dict[str, Any], key: str, new_value: str, field_path: str, changes: list[dict[str, str]]
) -> None:
    old_value = str(target.get(key) or "")
    if old_value == new_value:
        return
    target[key] = new_value
    changes.append({"field": field_path, "old_value": old_value, "new_value": new_value})


def load_array(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_array_atomic(path: Path, items: list[dict[str, Any]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".repair.tmp")
    temp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
