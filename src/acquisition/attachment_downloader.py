from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .public_url_safety import PublicUrlSafetyError, validate_public_http_url


ALLOWED_ATTACHMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 20
CHUNK_SIZE = 1024 * 256


class AttachmentDownloadError(RuntimeError):
    pass


class FetchedAttachment(bytes):
    def __new__(cls, content: bytes, *, content_type: str = "", content_disposition: str = "") -> "FetchedAttachment":
        value = super().__new__(cls, content)
        value.content_type = content_type
        value.content_disposition = content_disposition
        return value


class SafeAttachmentRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> object:
        count = int(getattr(req, "attachment_redirect_count", 0) or 0) + 1
        if count > 5:
            raise AttachmentDownloadError("Attachment redirect limit exceeded")
        try:
            safe_url = validate_public_http_url(urljoin(str(getattr(req, "full_url", "")), newurl))
        except PublicUrlSafetyError as exc:
            raise AttachmentDownloadError(str(exc)) from exc
        redirected = super().redirect_request(req, fp, code, msg, headers, safe_url)
        if redirected is not None:
            redirected.attachment_redirect_count = count
        return redirected


@dataclass
class AttachmentDownloadRecord:
    filename: str
    url: str
    local_path: str
    status: str
    error_type: str = ""
    error_message: str = ""
    file_type: str = ""
    sha256: str = ""


def safe_filename(raw_name: str, url: str) -> str:
    parsed_name = unquote(Path(urlparse(url).path).name)
    name = raw_name.strip() or parsed_name or "attachment"
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    return name or "attachment"


def suffix_for(filename: str, url: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    return Path(urlparse(url).path).suffix.lower()


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        next_candidate = directory / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def fetch_attachment_bytes(url: str, timeout: int = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    try:
        safe_url = validate_public_http_url(url)
    except PublicUrlSafetyError as exc:
        raise AttachmentDownloadError(str(exc)) from exc
    request = Request(safe_url, headers={"User-Agent": "TenderAttachmentDownloader/0.1 (+https://openai.com/codex)"})
    try:
        with build_opener(SafeAttachmentRedirectHandler()).open(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise AttachmentDownloadError(f"Attachment exceeds size limit: {content_length} > {max_bytes}")

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentDownloadError(f"Attachment exceeds size limit: {total} > {max_bytes}")
                chunks.append(chunk)
            content_type = str(response.headers.get("Content-Type") or "")
            content_disposition = str(response.headers.get("Content-Disposition") or "")
    except URLError as exc:
        raise AttachmentDownloadError(str(exc)) from exc
    content = b"".join(chunks)
    leading = content[:200].lstrip().lower()
    if leading.startswith((b"<!doctype", b"<html")):
        raise AttachmentDownloadError("Attachment response looks like an HTML page, not a binary attachment")
    return FetchedAttachment(
        content,
        content_type=content_type,
        content_disposition=content_disposition,
    )
