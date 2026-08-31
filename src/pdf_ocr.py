"""Small, fail-closed OCRmyPDF adapter for empty-text PDF inputs.

The adapter owns only local dependency probing and one bounded subprocess call.
It never performs network access and never exposes command output, paths, or
credentials in its result.
"""

from __future__ import annotations

import importlib.util
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any


OCR_LANGUAGES = ("chi_sim", "eng")
OCR_TIMEOUT_DEFAULT_SECONDS = 900.0
OCR_TIMEOUT_MIN_SECONDS = 60.0
OCR_TIMEOUT_MAX_SECONDS = 3600.0
OCR_ERROR_CODES = frozenset({
    "ocr.runtime_unavailable",
    "ocr.language_missing",
    "ocr.timeout",
    "ocr.execution_failed",
    "ocr.output_missing",
})


@dataclass(frozen=True)
class OCRResult:
    status: str
    error_code: str
    duration_ms: float
    languages: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status == "success" and not self.error_code


def _duration_ms(started: float) -> float:
    try:
        value = (perf_counter() - started) * 1000
        return value if math.isfinite(value) and value >= 0 else 0.0
    except Exception:
        return 0.0


def _result(
    started: float,
    *,
    status: str,
    error_code: str = "",
) -> OCRResult:
    return OCRResult(
        status=status,
        error_code=error_code if error_code in OCR_ERROR_CODES else "ocr.execution_failed" if error_code else "",
        duration_ms=_duration_ms(started),
        languages=OCR_LANGUAGES if status == "success" else (),
    )


def _configured_timeout_seconds() -> float:
    raw = os.environ.get("BIDDING_ASSET_OCR_TIMEOUT_SECONDS", "")
    try:
        value = float(raw) if raw.strip() else OCR_TIMEOUT_DEFAULT_SECONDS
    except (AttributeError, TypeError, ValueError):
        value = OCR_TIMEOUT_DEFAULT_SECONDS
    if not math.isfinite(value):
        value = OCR_TIMEOUT_DEFAULT_SECONDS
    return min(max(value, OCR_TIMEOUT_MIN_SECONDS), OCR_TIMEOUT_MAX_SECONDS)


_OCR_PREFLIGHT_ERRORS = frozenset({"ocr.runtime_unavailable", "ocr.language_missing"})


def _valid_tessdata_directory(directory: Path) -> bool:
    try:
        return (
            directory.is_dir()
            and (directory / "chi_sim.traineddata").is_file()
            and (directory / "eng.traineddata").is_file()
            and (directory / "configs" / "hocr").is_file()
        )
    except OSError:
        return False


def _runtime_directory_candidates() -> list[Path]:
    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles", "").strip()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if program_files:
        candidates.append(Path(program_files) / "Tesseract-OCR")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Tesseract-OCR")
    return candidates


def _resolve_tesseract_directory() -> Path | None:
    configured_dir = os.environ.get("BIDDING_ASSET_TESSERACT_DIR", "").strip()
    if configured_dir:
        directory = Path(configured_dir)
        try:
            directory = directory.resolve()
            return directory if (directory / "tesseract.exe").is_file() else None
        except OSError:
            return None

    path_environment = os.environ.get("PATH", "")
    executable = shutil.which("tesseract", path=path_environment)
    if executable:
        try:
            return Path(executable).resolve().parent
        except OSError:
            return Path(executable).parent

    for directory in _runtime_directory_candidates():
        try:
            if (directory / "tesseract.exe").is_file():
                return directory.resolve()
        except OSError:
            continue
    return None


def _resolve_tessdata_directory() -> tuple[Path | None, str | None]:
    configured = os.environ.get("BIDDING_ASSET_TESSDATA_DIR", "").strip()
    if configured:
        candidates = [Path(configured)]
    else:
        prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
        if prefix:
            candidates = [Path(prefix)]
        else:
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            candidates = [Path(local_app_data) / "bidding_asset" / "ocr" / "tessdata"] if local_app_data else []
    for directory in candidates:
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if _valid_tessdata_directory(directory):
            return directory, None
    return None, "ocr.language_missing"


def _runtime_environment() -> tuple[str | None, dict[str, str] | None, str | None]:
    """Resolve the one bounded environment shared by preflight and OCR subprocess."""
    environment = os.environ.copy()
    directory = _resolve_tesseract_directory()
    if directory is None:
        return None, None, "ocr.runtime_unavailable"
    environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")
    executable = str(directory / "tesseract.exe")
    tessdata, tessdata_error = _resolve_tessdata_directory()
    if tessdata_error:
        return None, None, tessdata_error
    environment["TESSDATA_PREFIX"] = str(tessdata)
    return executable, environment, None


def _tesseract_environment() -> tuple[str | None, dict[str, str] | None]:
    executable, environment, _error = _runtime_environment()
    return executable, environment


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def _rasterizer_available(environment: dict[str, str]) -> bool:
    if _module_available("pypdfium2"):
        return True
    for executable in ("gswin64c", "gswin32c", "gs"):
        if shutil.which(executable, path=environment.get("PATH")):
            return True
    return False


def _languages_available(executable: str, environment: dict[str, str]) -> bool:
    try:
        completed = subprocess.run(
            [executable, "--list-langs"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            shell=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return False
        output = bytes(completed.stdout or b"").decode("utf-8", errors="ignore")
        languages = {line.strip() for line in output.splitlines()}
        return all(language in languages for language in OCR_LANGUAGES)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False


def _preflight(runtime: tuple[str | None, dict[str, str] | None, str | None] | None = None) -> str | None:
    if not _module_available("ocrmypdf"):
        return "ocr.runtime_unavailable"
    if runtime is None:
        executable, environment, runtime_error = _runtime_environment()
    else:
        executable, environment, runtime_error = runtime
    if runtime_error in _OCR_PREFLIGHT_ERRORS:
        return runtime_error
    if not executable or environment is None:
        return "ocr.runtime_unavailable"
    if not _languages_available(executable, environment):
        return "ocr.language_missing"
    if not _rasterizer_available(environment):
        return "ocr.runtime_unavailable"
    return None


def _valid_output_path(input_pdf: Path, output_pdf: Path) -> bool:
    try:
        if not input_pdf.is_file() or not output_pdf.parent.is_dir():
            return False
        if input_pdf.resolve() == output_pdf.resolve():
            return False
        parent = output_pdf.parent.resolve()
        output_pdf.resolve().relative_to(parent)
        return True
    except (OSError, ValueError):
        return False


def run_ocr(
    input_pdf: Path,
    output_pdf: Path,
    *,
    timeout_seconds: float | None = None,
    force_ocr: bool = False,
) -> OCRResult:
    """Run OCRmyPDF once, returning only a fixed status/error contract.

    The normal route preserves searchable text with ``--skip-text``.  The
    bounded thin-text/image route may explicitly request ``--force-ocr`` so
    that pages containing a sparse text layer are not silently skipped.
    """
    started = perf_counter()
    try:
        input_path = Path(input_pdf)
        output_path = Path(output_pdf)
    except (TypeError, ValueError):
        return _result(started, status="failed", error_code="ocr.output_missing")
    if not _valid_output_path(input_path, output_path):
        return _result(started, status="failed", error_code="ocr.output_missing")
    runtime = _runtime_environment()
    preflight_error = _preflight(runtime)
    if preflight_error:
        return _result(started, status="failed", error_code=preflight_error)
    _executable, runtime_environment, runtime_error = runtime
    if runtime_error or runtime_environment is None:
        return _result(started, status="failed", error_code=runtime_error or "ocr.runtime_unavailable")
    try:
        timeout = _configured_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(timeout):
            timeout = OCR_TIMEOUT_DEFAULT_SECONDS
        timeout = min(max(timeout, OCR_TIMEOUT_MIN_SECONDS), OCR_TIMEOUT_MAX_SECONDS)
    except (TypeError, ValueError):
        timeout = OCR_TIMEOUT_DEFAULT_SECONDS
    text_mode = "--force-ocr" if force_ocr else "--skip-text"
    command = [
        sys.executable,
        "-m", "ocrmypdf",
        "-l", "+".join(OCR_LANGUAGES),
        text_mode,
        "--invalidate-digital-signatures",
        "--output-type", "pdf",
        "--optimize", "0",
        "--jobs", "1",
        "--quiet",
        str(input_path),
        str(output_path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
            env=runtime_environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return _result(started, status="failed", error_code="ocr.timeout")
    except (OSError, ValueError, TypeError):
        return _result(started, status="failed", error_code="ocr.execution_failed")
    if completed.returncode != 0:
        return _result(started, status="failed", error_code="ocr.execution_failed")
    try:
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            return _result(started, status="failed", error_code="ocr.output_missing")
    except OSError:
        return _result(started, status="failed", error_code="ocr.output_missing")
    return _result(started, status="success")


ocr_pdf = run_ocr


__all__ = ["OCRResult", "OCR_ERROR_CODES", "OCR_LANGUAGES", "ocr_pdf", "run_ocr"]
