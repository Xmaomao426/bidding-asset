from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Mapping


NORMALIZED_DOCUMENT_SCHEMA_VERSION = "normalized-document/v1"


@dataclass(frozen=True)
class NormalizedDocument:
    """Small, versioned view over content the current parsers already provide."""

    schema_version: str
    document_id: str
    source_type: str
    source_name: str
    source_url: str
    title: str
    text: str
    tables: tuple[tuple[tuple[str, ...], ...], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

def normalize_document(
    document: NormalizedDocument | Mapping[str, Any] | Any,
    *,
    source_type: str = "",
    source_url: str = "",
    title: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> NormalizedDocument:
    if isinstance(document, NormalizedDocument):
        return document
    if isinstance(document, Mapping):
        payload = dict(document)
    elif is_dataclass(document):
        payload = asdict(document)
    elif hasattr(document, "__dict__"):
        payload = dict(vars(document))
    else:
        raise TypeError("document must be a NormalizedDocument, mapping, dataclass, or object with attributes")

    document_id = str(payload.get("document_id") or "").strip()
    if not document_id:
        raise ValueError("NormalizedDocument requires document_id")

    source_name = str(payload.get("source_name") or payload.get("source_file") or "")
    resolved_url = source_url or str(payload.get("source_url") or "")
    supplied_metadata = dict(payload.get("metadata") or {})
    supplied_metadata.update(dict(metadata or {}))
    parser_metadata = {
        key: payload.get(key)
        for key in (
            "source_path",
            "file_type",
            "file_hash",
            "file_size",
            "modified_time",
            "zip_members",
            "parse_status",
            "parse_error",
        )
        if key in payload
    }
    parser_metadata.update(supplied_metadata)

    raw_warnings = payload.get("warnings") or ()
    if isinstance(raw_warnings, str):
        raw_warnings = (raw_warnings,)
    warnings = [str(item) for item in raw_warnings if str(item)]
    parse_status = str(payload.get("parse_status") or "")
    parse_error = str(payload.get("parse_error") or "")
    if parse_status and parse_status != "success":
        warnings.append(f"parse_status:{parse_status}")
    if parse_error:
        warnings.append(f"parse_error:{parse_error}")

    raw_tables = payload.get("tables") or []
    tables = tuple(
        tuple(tuple("" if cell is None else str(cell) for cell in row) for row in table)
        for table in raw_tables
    )
    resolved_source_type = source_type or str(payload.get("source_type") or "")
    if not resolved_source_type:
        resolved_source_type = "url" if resolved_url else "file"

    return NormalizedDocument(
        schema_version=NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        document_id=document_id,
        source_type=resolved_source_type,
        source_name=source_name,
        source_url=resolved_url,
        title=title or str(payload.get("title") or source_name),
        text=str(payload.get("text") or ""),
        tables=tables,
        metadata=parser_metadata,
        warnings=tuple(dict.fromkeys(warnings)),
    )
