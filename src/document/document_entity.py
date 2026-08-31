from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.acquisition.models import DocumentSource


@dataclass
class DocumentEntity:
    document_id: str
    source_type: str
    source_path: str
    source_url: str
    file_name: str
    file_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_time: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_document_id(source_type: str, source_path: str = "", source_url: str = "", file_name: str = "") -> str:
    source_key = "\n".join([source_type.strip(), source_path.strip(), source_url.strip(), file_name.strip()])
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:16]
    return f"document_{digest}"


def normalize_file_type(file_name: str, source_path: str = "") -> str:
    suffix = Path(file_name or source_path).suffix.lower().lstrip(".")
    return suffix or "html"


def document_entity_from_source(source: DocumentSource, source_path: str = "") -> DocumentEntity:
    inferred_path = source_path or str(Path("data/web_capture") / source.source_id / "raw.html")
    file_name = Path(inferred_path).name or f"{source.source_id}.html"
    file_type = normalize_file_type(file_name, inferred_path)
    metadata = {
        "source_id": source.source_id,
        "title": source.title,
        "capture_time": source.capture_time,
        "attachment_count": len(source.attachments),
        "attachments": list(source.attachments),
    }
    return DocumentEntity(
        document_id=build_document_id(source.source_type, inferred_path, source.source_url, file_name),
        source_type=source.source_type,
        source_path=inferred_path,
        source_url=source.source_url,
        file_name=file_name,
        file_type=file_type,
        metadata=metadata,
        created_time=current_timestamp(),
    )


def document_entity_from_mapping(payload: dict[str, Any]) -> DocumentEntity:
    source_path = str(payload.get("source_path") or "")
    file_name = str(payload.get("file_name") or payload.get("source_name") or Path(source_path).name or "")
    source_type = str(payload.get("source_type") or "file")
    source_url = str(payload.get("source_url") or "")
    document_id = str(payload.get("document_id") or "") or build_document_id(source_type, source_path, source_url, file_name)
    return DocumentEntity(
        document_id=document_id,
        source_type=source_type,
        source_path=source_path,
        source_url=source_url,
        file_name=file_name,
        file_type=str(payload.get("file_type") or normalize_file_type(file_name, source_path)),
        metadata=dict(payload.get("metadata") or {}),
        created_time=str(payload.get("created_time") or current_timestamp()),
        records=list(payload.get("records") or []),
    )
