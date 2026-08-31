from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .html_adapter import LinkReference, NoticeContentRegion
from .models import DocumentSource


DEFAULT_BROWSER_CAPTURE_ROOT = Path("data/web_capture")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_browser_source_id(source_ref: str, capture_time: str, submission_id: str = "") -> str:
    """Build a unique capture identity even for repeated submissions in one second."""
    unique_submission = submission_id or uuid.uuid4().hex
    digest = hashlib.sha256(
        f"{source_ref}\n{capture_time}\n{unique_submission}".encode("utf-8")
    ).hexdigest()[:16]
    stamp = capture_time.replace("-", "").replace(":", "").replace("+", "Z")
    return f"browser_{stamp}_{digest}"


def capture_browser_structured_region(
    *,
    source_url: str,
    region_payload: dict[str, Any],
    capture_root: str | Path | None = None,
    acquisition_method: str = "playwright_system_chrome_notice_content_dom",
    capture_method: str = "playwright_system_chrome_notice_content_dom",
    submission_method: str = "operator_browser_capture",
    source_origin: str = "",
    capture_metadata: dict[str, Any] | None = None,
) -> DocumentSource:
    structured_dom = region_payload.get("structured_dom")
    if not isinstance(structured_dom, dict) or not structured_dom:
        raise ValueError("notice_content_dom_missing")
    links = tuple(
        LinkReference(
            text=str(row.get("text") or ""),
            url=str(row.get("url") or ""),
            raw_href=str(row.get("raw_href") or ""),
            download=str(row.get("download") or ""),
            executable=bool(row.get("executable")),
        )
        for row in region_payload.get("links") or []
        if isinstance(row, dict)
    )
    region = NoticeContentRegion(
        structured_dom=structured_dom,
        title=str(region_payload.get("title") or ""),
        text=str(region_payload.get("text") or ""),
        links=links,
        locator=dict(region_payload.get("locator") or {}),
    )
    return _capture_selected_region(
        source_url,
        region,
        capture_root=capture_root,
        acquisition_method=acquisition_method,
        capture_method=capture_method,
        submission_method=submission_method,
        source_origin=source_origin,
        capture_metadata=capture_metadata,
        capture_time=utc_now(),
    )


def _capture_selected_region(
    source_url: str,
    region: NoticeContentRegion,
    *,
    capture_root: str | Path | None,
    acquisition_method: str,
    capture_method: str,
    submission_method: str,
    source_origin: str,
    capture_metadata: dict[str, Any] | None,
    capture_time: str,
) -> DocumentSource:
    title = region.title
    text_content = region.text
    attachments = [
        {
            "filename": link.download or link.text,
            "url": link.url,
            "raw_href": link.raw_href,
            "download": link.download,
            "downloadable": link.executable,
        }
        for link in region.links
    ]
    html_sha256 = hashlib.sha256(region.to_json().encode("utf-8")).hexdigest()
    provenance = {
        "capture_method": capture_method,
        "submission_method": submission_method,
        "original_source_type": "url",
        "original_url": source_url,
    }
    if source_origin:
        provenance["source_origin"] = source_origin
    if capture_metadata:
        provenance["browser_capture"] = dict(capture_metadata)
    document = DocumentSource(
        source_id=build_browser_source_id(source_url, capture_time),
        source_type="browser",
        source_url=source_url,
        capture_time=capture_time,
        title=title,
        html_content="",
        text_content=text_content,
        attachments=attachments,
        metadata={
            "html_sha256": html_sha256,
            "notice_content_dom": region.structured_dom,
            "notice_content_locator": region.locator,
            "access": {
                "status": "success",
                "http_status": None,
                "original_url": source_url,
                "response_url": source_url,
                "final_url": source_url,
                "content_type": "text/html",
                "redirect_count": 0,
                "transport_error": "",
                "acquisition_method": acquisition_method,
            },
            "provenance": provenance,
        },
    )
    if capture_root is None:
        save_browser_capture(document, DEFAULT_BROWSER_CAPTURE_ROOT)
    else:
        save_browser_capture(document, Path(capture_root))
    return document


def save_browser_capture(document: DocumentSource, capture_root: Path) -> None:
    """Persist only the selected structured region and provenance, never the full page DOM."""
    capture_dir = capture_root / document.source_id
    capture_dir.mkdir(parents=True, exist_ok=True)
    notice_content = dict(document.metadata.get("notice_content_dom") or {})
    (capture_dir / "notice_content.json").write_text(
        json.dumps(notice_content, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    metadata = {
        "source_id": document.source_id,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "title": document.title,
        "capture_time": document.capture_time,
        "attachments": document.attachments,
        **{
            key: value for key, value in dict(document.metadata or {}).items()
            if key != "notice_content_dom"
        },
    }
    (capture_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_saved_browser_source(source_id: str, capture_root: Path) -> DocumentSource | None:
    """Reload the selected page state required by the explicit attachment pass."""
    capture_dir = Path(capture_root) / str(source_id or "")
    metadata_path = capture_dir / "metadata.json"
    content_path = capture_dir / "notice_content.json"
    if not metadata_path.is_file() or not content_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    structured_dom = json.loads(content_path.read_text(encoding="utf-8"))
    metadata["notice_content_dom"] = structured_dom
    return DocumentSource(
        source_id=str(metadata.get("source_id") or source_id),
        source_type=str(metadata.get("source_type") or "browser"),
        source_url=str(metadata.get("source_url") or ""),
        capture_time=str(metadata.get("capture_time") or ""),
        title=str(metadata.get("title") or ""),
        html_content="",
        text_content=_structured_dom_text(structured_dom),
        attachments=[dict(item) for item in metadata.get("attachments") or [] if isinstance(item, dict)],
        metadata=metadata,
    )


def _structured_dom_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    return "\n".join(
        value for value in (_structured_dom_text(child) for child in node.get("children") or [])
        if value
    )
