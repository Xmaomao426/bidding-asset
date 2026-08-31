from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

# Unstructured otherwise attempts to fetch NLTK data during import.  The active
# document route is deliberately local-only and does not need sentence tagging.
os.environ.setdefault("AUTO_DOWNLOAD_NLTK", "False")

from unstructured.partition.docx import partition_docx
from unstructured.partition.text import partition_text
from pdfminer.high_level import extract_text as extract_pdf_text
from pdfminer.pdfdocument import PDFEncryptionError

from src import pdf_ocr


SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".zip"}
DEFAULT_SKIP_FILENAMES = {"招投标.xlsx"}
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
PDF_SIGNATURE = b"%PDF-"
LIBREOFFICE_EXE = Path(r"C:\Program Files\LibreOffice\program\soffice.com")
LIBREOFFICE_TIMEOUT_SECONDS = 45
# A force-OCR pass is reserved for a document-dominant sparse text layer:
# every page in a multi-page PDF must be both image-bearing and below this
# small normalized-character threshold.  Isolated illustrated pages stay on
# the existing no-OCR/skip-text behavior.
PDF_LOW_TEXT_CHAR_THRESHOLD = 64
PDF_THIN_TEXT_IMAGE_MIN_PAGES = 2
OCR_AUDIT_LANGUAGES = ["chi_sim", "eng"]
OCR_PARSE_ERROR_CODES = {
    "ocr.runtime_unavailable",
    "ocr.language_missing",
    "ocr.timeout",
    "ocr.execution_failed",
    "ocr.output_missing",
    "ocr.output_text_empty",
}


class _PDFParseError(ValueError):
    def __init__(self, error_code: str, parser_audit: dict[str, Any]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.parser_audit = dict(parser_audit)


@dataclass
class ParsedDocument:
    document_id: str
    source_path: str
    source_name: str
    file_type: str
    file_hash: str
    file_size: int
    modified_time: str
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)
    zip_members: list[str] = field(default_factory=list)
    parse_status: str = "success"
    parse_error: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    parser_audit: dict[str, Any] = field(default_factory=dict)


def clean_text(text: str | None) -> str:
    text = text or ""
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_id(path: Path, digest: str) -> str:
    return hashlib.sha256(f"{path.name}\n{digest}".encode("utf-8")).hexdigest()[:24]


def detect_document_type(path: Path) -> str:
    """Identify the real supported container before choosing a partitioner."""
    header = path.read_bytes()[:8]
    if header.startswith(PDF_SIGNATURE):
        return "pdf"
    if header.startswith(OLE_SIGNATURE):
        return "doc"
    if header.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise ValueError("corrupt_zip_container") from exc
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return "docx"
        return "zip"
    raise ValueError("unsupported_or_corrupt_document")


def _table_rows(element: Any) -> list[list[str]]:
    html = str(getattr(getattr(element, "metadata", None), "text_as_html", "") or "")
    if not html:
        text = clean_text(str(element))
        return [[text]] if text else []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        rows = [
            [clean_text(cell.get_text(" ")) for cell in row.find_all(["th", "td"])]
            for row in soup.find_all("tr")
        ]
        return [row for row in rows if any(row)]
    except Exception:
        text = clean_text(str(element))
        return [[text]] if text else []


def _elements_payload(raw_elements: list[Any]) -> tuple[str, list[list[list[str]]], list[dict[str, Any]]]:
    parts: list[str] = []
    tables: list[list[list[str]]] = []
    elements: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for position, element in enumerate(raw_elements):
        category = str(getattr(element, "category", "") or type(element).__name__)
        text = clean_text(str(element))
        if not text or (category, text) in seen:
            continue
        seen.add((category, text))
        table_index: int | None = None
        if category == "Table":
            rows = _table_rows(element)
            if rows:
                table_index = len(tables)
                tables.append(rows)
        metadata = getattr(element, "metadata", None)
        page_number = getattr(metadata, "page_number", None)
        elements.append({
            "index": position,
            "category": category,
            "text": text,
            "table_index": table_index,
            "page_number": int(page_number) if isinstance(page_number, int) else None,
        })
        parts.append(text)
    return clean_text("\n".join(parts)), tables, elements


def read_docx(path: Path) -> tuple[str, list[list[list[str]]], list[dict[str, Any]]]:
    return _elements_payload(list(partition_docx(filename=str(path), include_page_breaks=True)))


def _base_parser_audit(detected_type: str = "") -> dict[str, Any]:
    return {
        "adapter": "unstructured-community",
        "detected_type": detected_type,
        "partition_strategy": "fast" if detected_type == "pdf" else "native",
        "element_count": 0,
        "source_characters": 0,
        "libreoffice_used": detected_type == "doc",
        "ocr_attempted": False,
        "ocr_used": False,
        "ocr_status": "skipped",
        "ocr_route": "none",
        "ocr_error_code": "ocr.not_applicable" if detected_type and detected_type != "pdf" else "",
        "ocr_duration_ms": 0.0,
        "ocr_languages": [],
        "ocr_trigger": "none",
        "pdf_page_count": 0,
        "pdf_text_page_count": 0,
        "pdf_low_text_image_page_count": 0,
        "pdf_image_only_page_count": 0,
        "pdf_blank_page_count": 0,
        "pdf_page_profile_status": "not_run",
        "pdf_page_profile_duration_ms": 0.0,
    }


def _safe_duration_ms(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _partition_pdf_text(text: str) -> tuple[str, list[list[list[str]]], list[dict[str, Any]]]:
    return _elements_payload(list(partition_text(text=text, paragraph_grouper=False)))


def _profile_pdf_pages(path: Path) -> dict[str, Any]:
    """Count text/image-only pages without extracting or persisting page data."""
    started = perf_counter()
    profile = {
        "pdf_page_count": 0,
        "pdf_text_page_count": 0,
        "pdf_low_text_image_page_count": 0,
        "pdf_image_only_page_count": 0,
        "pdf_blank_page_count": 0,
        "pdf_page_profile_status": "failed",
        "pdf_page_profile_duration_ms": 0.0,
    }
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            profile["pdf_page_count"] = len(document)
            for index in range(len(document)):
                page = document[index]
                text_page = None
                try:
                    text_page = page.get_textpage()
                    raw_text = text_page.get_text_range()
                    normalized_text = re.sub(r"\s+", "", raw_text or "")
                    has_image = False
                    for page_object in page.get_objects():
                        try:
                            has_image = (
                                type(page_object).__name__ == "PdfImage"
                                or getattr(page_object, "type", None) == 3
                            )
                        except Exception:
                            has_image = False
                        if has_image:
                            break
                    if normalized_text:
                        profile["pdf_text_page_count"] += 1
                        if has_image and len(normalized_text) <= PDF_LOW_TEXT_CHAR_THRESHOLD:
                            profile["pdf_low_text_image_page_count"] += 1
                    elif has_image:
                        profile["pdf_image_only_page_count"] += 1
                    else:
                        profile["pdf_blank_page_count"] += 1
                finally:
                    if text_page is not None:
                        try:
                            text_page.close()
                        except Exception:
                            pass
                    try:
                        page.close()
                    except Exception:
                        pass
        finally:
            try:
                document.close()
            except Exception:
                pass
        profile["pdf_page_profile_status"] = "success"
    except Exception:
        profile.update({
            "pdf_page_count": 0,
            "pdf_text_page_count": 0,
            "pdf_low_text_image_page_count": 0,
            "pdf_image_only_page_count": 0,
            "pdf_blank_page_count": 0,
            "pdf_page_profile_status": "failed",
        })
    profile["pdf_page_profile_duration_ms"] = _safe_duration_ms((perf_counter() - started) * 1000)
    return profile


def _read_pdf_with_audit(
    path: Path, ocr_output_dir: Path,
) -> tuple[str, list[list[list[str]]], list[dict[str, Any]], dict[str, Any]]:
    # `unstructured.partition.pdf` imports the optional inference package even
    # for strategy="fast" in the pinned release.  Keep this text-only route
    # inference-free: pdfminer performs decoding and Unstructured performs the
    # ordered element partition. OCR is attempted for an empty document or
    # when a bounded page profile finds image-only pages, and its output
    # returns through this same route.
    audit = _base_parser_audit("pdf")
    try:
        text = clean_text(extract_pdf_text(str(path)))
    except PDFEncryptionError:
        raise _PDFParseError("pdf.password_required", audit) from None
    force_ocr = False
    if text:
        profile = _profile_pdf_pages(path)
        audit.update(profile)
        low_text_image_page_count = int(
            profile.get("pdf_low_text_image_page_count", 0) or 0
        )
        page_count = int(profile.get("pdf_page_count", 0) or 0)
        has_thin_text_image_pages = (
            profile.get("pdf_page_profile_status") == "success"
            and page_count >= PDF_THIN_TEXT_IMAGE_MIN_PAGES
            and low_text_image_page_count == page_count
        )
        has_image_only_pages = (
            profile.get("pdf_page_profile_status") == "success"
            and profile.get("pdf_image_only_page_count", 0) > 0
        )
        if has_thin_text_image_pages:
            audit["ocr_trigger"] = "mixed_thin_text_image_pages"
            should_ocr = True
            force_ocr = True
        elif has_image_only_pages:
            audit["ocr_trigger"] = "mixed_image_only_pages"
            should_ocr = True
            force_ocr = False
        else:
            audit["ocr_trigger"] = "none"
            should_ocr = False
        if not should_ocr:
            audit["ocr_error_code"] = "ocr.not_needed"
            try:
                parsed_text, tables, elements = _partition_pdf_text(text)
            except Exception:
                raise _PDFParseError("document_partition_failed", audit)
            audit.update({
                "element_count": len(elements),
                "source_characters": len(parsed_text),
            })
            return parsed_text, tables, elements, audit
    else:
        audit.update({
            "ocr_trigger": "full_document_empty",
            "pdf_page_profile_status": "not_run",
            "pdf_page_profile_duration_ms": 0.0,
        })
        should_ocr = True
    if should_ocr:
        audit.update({
            "ocr_attempted": True,
            "ocr_route": "ocrmypdf",
            "ocr_status": "failed",
            "ocr_languages": [],
        })
    else:
        raise _PDFParseError("document_partition_failed", audit)
    ocr_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = ocr_output_dir / "ocr-output.pdf"
    try:
        if force_ocr:
            ocr_result = pdf_ocr.run_ocr(path, output_path, force_ocr=True)
        else:
            ocr_result = pdf_ocr.run_ocr(path, output_path)
    except Exception:
        ocr_result = None
    result_status = str(getattr(ocr_result, "status", "failed") or "failed")
    result_code = str(getattr(ocr_result, "error_code", "") or "")
    result_duration = _safe_duration_ms(getattr(ocr_result, "duration_ms", 0.0))
    if result_status != "success" or result_code or not output_path.is_file():
        error_code = result_code if result_code in OCR_PARSE_ERROR_CODES else "ocr.execution_failed"
        if not output_path.is_file() and result_status == "success" and not result_code:
            error_code = "ocr.output_missing"
        audit.update({"ocr_status": "failed", "ocr_error_code": error_code, "ocr_duration_ms": result_duration})
        raise _PDFParseError(error_code, audit)

    audit.update({
        "ocr_used": True,
        "ocr_status": "success",
        "ocr_error_code": "",
        "ocr_duration_ms": result_duration,
        "ocr_languages": list(OCR_AUDIT_LANGUAGES),
    })
    try:
        ocr_text = clean_text(extract_pdf_text(str(output_path)))
    except Exception:
        audit.update({
            "ocr_status": "failed",
            "ocr_used": False,
            "ocr_error_code": "ocr.execution_failed",
        })
        raise _PDFParseError("ocr.execution_failed", audit)
    if not ocr_text:
        audit["ocr_status"] = "failed"
        audit["ocr_used"] = False
        audit["ocr_error_code"] = "ocr.output_text_empty"
        raise _PDFParseError("ocr.output_text_empty", audit)
    try:
        parsed_text, tables, elements = _partition_pdf_text(ocr_text)
    except Exception:
        raise _PDFParseError("document_partition_failed", audit)
    audit.update({"element_count": len(elements), "source_characters": len(parsed_text)})
    return parsed_text, tables, elements, audit


def read_pdf(path: Path) -> tuple[str, list[list[list[str]]], list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="bidding-asset-ocr-") as temp_dir:
        text, tables, elements, _audit = _read_pdf_with_audit(path, Path(temp_dir))
        return text, tables, elements


def convert_legacy_doc(path: Path, output_dir: Path) -> Path:
    """Convert one genuine OLE DOC with an isolated, noninteractive LO profile."""
    executable = Path(os.environ.get("BIDDING_ASSET_LIBREOFFICE_PATH", str(LIBREOFFICE_EXE)))
    if not executable.is_file():
        raise RuntimeError("libreoffice_not_available")
    env = {**os.environ, "SAL_USE_VCLPLUGIN": "svp", "AUTO_DOWNLOAD_NLTK": "False"}
    completed: subprocess.CompletedProcess[bytes] | None = None
    for attempt in range(2):
        profile_dir = Path(tempfile.mkdtemp(prefix="lo-profile-", dir=output_dir))
        profile_uri = profile_dir.resolve().as_uri()
        command = [
            str(executable), "--headless", "--nologo", "--nodefault", "--nolockcheck",
            "--norestore", "--safe-mode", f"-env:UserInstallation={profile_uri}",
            "--convert-to", "docx", "--outdir", str(output_dir), str(path),
        ]
        try:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=LIBREOFFICE_TIMEOUT_SECONDS, check=False, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            break
        except subprocess.TimeoutExpired:
            if attempt == 1:
                raise RuntimeError("libreoffice_doc_conversion_timeout") from None
    converted = output_dir / f"{path.stem}.docx"
    if completed is None or completed.returncode != 0 or not converted.is_file():
        raise RuntimeError("libreoffice_doc_conversion_failed")
    if detect_document_type(converted) != "docx":
        raise RuntimeError("libreoffice_doc_conversion_invalid")
    return converted


def read_doc(path: Path) -> tuple[str, list[list[list[str]]], list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="bidding-asset-doc-") as temp_dir:
        converted = convert_legacy_doc(path, Path(temp_dir))
        return read_docx(converted)


def read_zip(path: Path) -> tuple[str, list[list[list[str]]], list[str]]:
    names: list[str] = []
    texts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist()[:50]:
            if info.is_dir():
                continue
            names.append(info.filename)
            suffix = Path(info.filename).suffix.lower()
            if suffix in {".txt", ".xml"}:
                try:
                    texts.append(zf.read(info).decode("utf-8", errors="ignore")[:6000])
                except Exception:
                    pass
    return clean_text("\n".join(names + texts)), [], names


def _read_file_with_audit(
    path: Path, *, ocr_output_dir: Path | None = None,
) -> tuple[str, list[list[list[str]]], list[str], list[dict[str, Any]], str, dict[str, Any]]:
    detected_type = detect_document_type(path)
    if detected_type == "pdf":
        if ocr_output_dir is None:
            with tempfile.TemporaryDirectory(prefix="bidding-asset-ocr-") as temp_dir:
                text, tables, elements, audit = _read_pdf_with_audit(path, Path(temp_dir))
        else:
            text, tables, elements, audit = _read_pdf_with_audit(path, ocr_output_dir)
        return text, tables, [], elements, detected_type, audit
    if detected_type == "docx":
        text, tables, elements = read_docx(path)
        return text, tables, [], elements, detected_type, _base_parser_audit(detected_type)
    if detected_type == "doc":
        text, tables, elements = read_doc(path)
        return text, tables, [], elements, detected_type, _base_parser_audit(detected_type)
    text, tables, members = read_zip(path)
    elements = [{"index": 0, "category": "NarrativeText", "text": text,
                 "table_index": None, "page_number": None}] if text else []
    return text, tables, members, elements, detected_type, _base_parser_audit(detected_type)


def read_file(path: Path) -> tuple[str, list[list[list[str]]], list[str], list[dict[str, Any]], str]:
    text, tables, members, elements, detected_type, _audit = _read_file_with_audit(path)
    return text, tables, members, elements, detected_type


def discover_files(input_dir: Path, skip_filenames: set[str] | None = None) -> list[Path]:
    skip = skip_filenames or DEFAULT_SKIP_FILENAMES
    files: list[Path] = []
    for path in sorted(input_dir.iterdir(), key=lambda p: p.name):
        if not path.is_file() or path.name in skip or path.name.startswith("~$"):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        files.append(path)
    return files


def parse_document(path: Path, root: Path) -> ParsedDocument:
    digest = file_hash(path)
    stat = path.stat()
    parser_audit = _base_parser_audit()
    try:
        detected_hint = detect_document_type(path)
        if detected_hint == "pdf":
            with tempfile.TemporaryDirectory(prefix="bidding-asset-ocr-") as temp_dir:
                text, tables, zip_members, elements, detected_type, parser_audit = _read_file_with_audit(
                    path, ocr_output_dir=Path(temp_dir)
                )
        else:
            text, tables, zip_members, elements, detected_type, parser_audit = _read_file_with_audit(path)
        if not text or not elements:
            raise ValueError("document_has_no_usable_text")
        parse_status = "success"
        parse_error = ""
    except Exception as exc:
        text = ""
        tables = []
        zip_members = []
        elements = []
        detected_type = ""
        parse_status = "failed"
        parser_audit = dict(getattr(exc, "parser_audit", parser_audit) or parser_audit)
        parse_error = str(getattr(exc, "error_code", "") or exc)

    try:
        source_path = str(path.relative_to(root))
    except ValueError:
        source_path = str(path)

    return ParsedDocument(
        document_id=document_id(path, digest),
        source_path=source_path,
        source_name=path.name,
        file_type=path.suffix.lower(),
        file_hash=digest,
        file_size=stat.st_size,
        modified_time=stat.st_mtime_ns.__str__(),
        text=text,
        tables=tables,
        zip_members=zip_members,
        parse_status=parse_status,
        parse_error=parse_error,
        elements=elements,
        parser_audit={
            **_base_parser_audit(detected_type),
            **parser_audit,
            "adapter": "unstructured-community",
            "detected_type": parser_audit.get("detected_type") or detected_type,
            "partition_strategy": "fast" if (parser_audit.get("detected_type") or detected_type) == "pdf" else "native",
            "element_count": len(elements),
            "source_characters": len(text),
            "libreoffice_used": detected_type == "doc" or bool(parser_audit.get("libreoffice_used")),
        },
    )


def parse_directory(input_dir: Path) -> list[ParsedDocument]:
    root = input_dir.resolve()
    return [parse_document(path, root) for path in discover_files(root)]


def write_json(documents: list[ParsedDocument], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload: list[dict[str, Any]] = [asdict(document) for document in documents]
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse supported tender source files into ParsedDocument JSON.")
    parser.add_argument("--input-dir", default=".", help="Directory containing source files.")
    parser.add_argument(
        "--output",
        default="data/cache/parsed_documents.json",
        help="Output JSON path for ParsedDocument records.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_path = Path(args.output)
    documents = parse_directory(input_dir)
    write_json(documents, output_path)

    failed = sum(1 for document in documents if document.parse_status != "success")
    print(f"wrote {output_path} documents={len(documents)} failed={failed}")


if __name__ == "__main__":
    main()
