"""Single Windows localhost-only runtime for the packaged Operator UI."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, Iterable


APP_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = APP_ROOT / "VERSION"
REQUIREMENTS_FILE = APP_ROOT / "requirements.txt"
DEFAULT_PORT = 5000
DEFAULT_MAX_UPLOAD_MIB = 256
MIN_UPLOAD_MIB = 1
MAX_UPLOAD_MIB = 1024
USER_ENV_NAMES = (
    "BIDDING_ASSET_AI_SEMANTIC_MODEL",
    "OPENROUTER_API_KEY",
    "BIDDING_ASSET_MAX_UPLOAD_MIB",
)
PATH_LAYOUT = {
    "review_queue": "data/diagnostics/review_queue.json",
    "review_decisions": "data/diagnostics/review_decisions.json",
    "intake_output": "data/diagnostics/asset_intake_candidates.json",
    "intake_audit": "data/diagnostics/intake_audit.json",
    "documents": "data/repository/documents.json",
    "projects": "data/repository/projects.json",
    "repository_audit": "data/repository/repository_audit.json",
    "links": "data/repository/project_document_links.json",
    "workflow_result": "data/diagnostics/operator_workflow_result.json",
    "asset_candidates": "data/diagnostics/asset_candidates.json",
    "deduped_candidates": "data/diagnostics/asset_candidates_deduped.json",
    "dedup_summary": "data/diagnostics/candidate_dedup_summary.json",
    "lifecycle": "data/diagnostics/asset_lifecycle.json",
    "upload_dir": "data/web_capture/operator_uploads",
    "acquisition_inbox": "data/diagnostics/acquisition_inbox.json",
    "acquisition_inbox_summary": "data/diagnostics/acquisition_inbox_summary.json",
    "manual_remediation_backup_root": "data/backups/manual_remediation",
    "excel": "招投标.xlsx",
    "excel_sync_records": "data/cache/inbox_excel_sync_records",
    "excel_sync_summary": "data/diagnostics/inbox_excel_sync_summary",
    "excel_backup_dir": "data/backups",
    "excel_sync_runtime": "data/cache/inbox_excel_sync_runtime",
}
CRITICAL_JSON_FIELDS = (
    "review_queue",
    "review_decisions",
    "intake_output",
    "intake_audit",
    "documents",
    "projects",
    "repository_audit",
    "links",
    "asset_candidates",
    "deduped_candidates",
    "lifecycle",
    "acquisition_inbox",
)


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise RuntimeError("VERSION 格式无效")
    return version


def normalize_workspace(value: str | Path | None) -> Path:
    workspace = APP_ROOT if value is None else Path(value).expanduser()
    return workspace.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def build_workspace_paths(workspace: str | Path | None = None) -> dict[str, Path]:
    root = normalize_workspace(workspace)
    values: dict[str, Path] = {}
    for name, relative in PATH_LAYOUT.items():
        candidate = (root / relative).resolve(strict=False)
        if not _is_within(candidate, root):
            raise ValueError(f"运行路径越出 workspace: {name}")
        values[name] = candidate
    return values


def build_operator_paths(workspace: str | Path | None = None) -> Any:
    from src.operator_ui.app import OperatorUiPaths

    values = build_workspace_paths(workspace)
    if set(PATH_LAYOUT) != set(OperatorUiPaths.__dataclass_fields__):
        raise RuntimeError("OperatorUiPaths 映射不完整")
    return OperatorUiPaths(**values)


def refresh_windows_user_environment() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except OSError:
        return
    with key:
        for name in USER_ENV_NAMES:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)


def max_upload_bytes() -> int:
    raw = os.environ.get("BIDDING_ASSET_MAX_UPLOAD_MIB", "").strip()
    if not raw:
        mib = DEFAULT_MAX_UPLOAD_MIB
    else:
        try:
            mib = int(raw)
        except ValueError as exc:
            raise ValueError("BIDDING_ASSET_MAX_UPLOAD_MIB 必须是整数") from exc
        if not MIN_UPLOAD_MIB <= mib <= MAX_UPLOAD_MIB:
            raise ValueError("BIDDING_ASSET_MAX_UPLOAD_MIB 必须在 1..1024")
    return mib * 1024 * 1024


def _result(level: str, name: str, message: str) -> dict[str, str]:
    return {"level": level, "name": name, "message": message}


def _required_distributions() -> Iterable[str]:
    if not REQUIREMENTS_FILE.is_file():
        return ()
    names: list[str] = []
    for raw in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(";", 1)[0].split("[", 1)[0]
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(separator, 1)[0]
        names.append(name.strip())
    return names


def _check_json_lists(paths: dict[str, Path]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for name in CRITICAL_JSON_FIELDS:
        path = paths[name]
        if not path.exists():
            results.append(_result("PASS", f"JSON:{name}", "缺失，按空列表处理"))
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise ValueError("顶层不是 list")
        except Exception as exc:
            results.append(_result("FAIL", f"JSON:{name}", f"不可用：{exc}"))
        else:
            results.append(_result("PASS", f"JSON:{name}", "现有文件可解析"))
    return results


def _port_result(port: int) -> dict[str, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return _result("FAIL", "端口", f"127.0.0.1:{port} 不可用")
    return _result("PASS", "端口", f"127.0.0.1:{port} 可用")


def _canonical_executable(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _validate_model_configuration() -> bool:
    try:
        from src.semantic.production_ai_first import validate_required_startup_configuration

        validate_required_startup_configuration()
    except Exception:
        return False
    return True


def _existing_executable(candidates: Iterable[str | Path | None]) -> bool:
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if Path(candidate).is_file():
                return True
        except OSError:
            continue
    return False


def _probe_chrome() -> bool:
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    program_files_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    return _existing_executable((
        shutil.which("chrome"),
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if local_app_data else None,
    ))


def _probe_libreoffice() -> bool:
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return _existing_executable((
        shutil.which("soffice.com"),
        shutil.which("soffice"),
        Path(program_files) / "LibreOffice" / "program" / "soffice.com",
        Path(program_files) / "LibreOffice" / "program" / "soffice.exe",
    ))


def _resolve_tesseract_executable() -> Path | None:
    configured = os.environ.get("BIDDING_ASSET_TESSERACT_DIR", "").strip()
    if configured:
        candidate = Path(configured) / "tesseract.exe"
        return candidate if _existing_executable((candidate,)) else None
    path_executable = shutil.which("tesseract", path=os.environ.get("PATH", ""))
    if path_executable and _existing_executable((path_executable,)):
        return Path(path_executable)
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [Path(program_files) / "Tesseract-OCR" / "tesseract.exe"]
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
    return next(
        (candidate for candidate in candidates if _existing_executable((candidate,))),
        None,
    )


def _resolve_tessdata_directory() -> Path | None:
    configured = os.environ.get("BIDDING_ASSET_TESSDATA_DIR", "").strip()
    prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if configured:
        candidates = [Path(configured)]
    elif prefix:
        candidates = [Path(prefix)]
    elif local_app_data:
        candidates = [Path(local_app_data) / "bidding_asset" / "ocr" / "tessdata"]
    else:
        candidates = []
    for directory in candidates:
        try:
            if (
                directory.is_dir()
                and (directory / "chi_sim.traineddata").is_file()
                and (directory / "eng.traineddata").is_file()
                and (directory / "configs" / "hocr").is_file()
            ):
                return directory
        except OSError:
            continue
    return None


def _probe_ocr() -> bool:
    return _resolve_tesseract_executable() is not None and _resolve_tessdata_directory() is not None


def _external_component_results() -> list[dict[str, str]]:
    checks = (
        ("Chrome", _probe_chrome(), "依赖 Chrome 的无头 URL 捕获/重试不可用"),
        ("LibreOffice", _probe_libreoffice(), "旧版 .doc 转换不可用"),
        ("OCR", _probe_ocr(), "扫描件/图片型 PDF OCR 不可用"),
    )
    return [
        _result("PASS", name, "可用") if available else _result("WARN", name, impact)
        for name, available, impact in checks
    ]


def run_preflight(workspace: Path, port: int) -> list[dict[str, str]]:
    """Return an aggregated, credential-safe, zero-write diagnosis."""
    results: list[dict[str, str]] = []
    results.append(_result("PASS" if os.name == "nt" else "FAIL", "Windows", platform.system()))
    py_ok = sys.version_info[:2] == (3, 12)
    results.append(_result("PASS" if py_ok else "FAIL", "Python", platform.python_version()))
    interpreter = APP_ROOT / ".venv" / "Scripts" / "python.exe"
    interpreter_ok = (
        interpreter.is_file()
        and _canonical_executable(sys.executable) == _canonical_executable(interpreter)
    )
    results.append(_result("PASS" if interpreter_ok else "FAIL", "根虚拟环境/解释器", "当前解释器匹配根 .venv" if interpreter_ok else "当前解释器不是根 .venv Python"))
    results.append(_result("PASS" if REQUIREMENTS_FILE.is_file() else "FAIL", "requirements.txt", "存在" if REQUIREMENTS_FILE.is_file() else "缺失"))
    for resource in (APP_ROOT / "config", APP_ROOT / "src"):
        results.append(_result("PASS" if resource.is_dir() else "FAIL", f"应用资源:{resource.name}", "存在" if resource.is_dir() else "缺失"))
    for distribution in _required_distributions():
        try:
            importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            results.append(_result("FAIL", f"依赖:{distribution}", "未安装"))
        else:
            results.append(_result("PASS", f"依赖:{distribution}", "已安装"))
    try:
        version = read_version()
    except Exception as exc:
        results.append(_result("FAIL", "VERSION", str(exc)))
    else:
        results.append(_result("PASS", "VERSION", version))
    results.append(_result("PASS" if workspace.is_dir() else "FAIL", "workspace", str(workspace)))
    try:
        paths = build_workspace_paths(workspace)
    except Exception as exc:
        results.append(_result("FAIL", "workspace 路径", str(exc)))
    else:
        results.extend(_check_json_lists(paths))
    results.extend(_external_component_results())
    results.append(_port_result(port))
    try:
        max_upload_bytes()
    except ValueError as exc:
        results.append(_result("FAIL", "上传上限", str(exc)))
    else:
        results.append(_result("PASS", "上传上限", "配置有效"))
    if not _validate_model_configuration():
        results.append(_result("FAIL", "模型配置", "Windows User scope 模型或 API 密钥缺失/无效"))
    else:
        results.append(_result("PASS", "模型配置", "已配置"))
    return results


def print_preflight(results: Iterable[dict[str, str]]) -> bool:
    failed = False
    for item in results:
        print(f"[{item['level']}] {item['name']}: {item['message']}")
        failed = failed or item["level"] == "FAIL"
    print("结论: FAIL" if failed else "结论: PASS（WARN 不阻止启动）")
    return not failed


class WorkspaceMutex:
    """Windows process mutex keyed only by the canonical workspace."""

    def __init__(self, workspace: Path) -> None:
        digest = hashlib.sha256(str(workspace).casefold().encode("utf-8")).hexdigest()
        self.name = f"Local\\BiddingAsset-{digest}"
        self.handle: int | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            raise RuntimeError("本地发布运行时仅支持 Windows")
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError("无法创建 workspace mutex")
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            raise RuntimeError("同一 workspace 已有运行实例")
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "WorkspaceMutex":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows localhost-only 招投标资产管理")
    parser.add_argument("--check", action="store_true", help="只读 Preflight")
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024..65535")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(read_version())
        return 0
    workspace = normalize_workspace(args.workspace)
    refresh_windows_user_environment()
    results = run_preflight(workspace, args.port)
    passed = print_preflight(results)
    if args.check:
        return 0 if passed else 2
    if not passed:
        return 2
    upload_bytes = max_upload_bytes()
    paths = build_operator_paths(workspace)
    version = read_version()
    from src.operator_ui.app import create_app

    app = create_app(
        paths,
        release_mode=True,
        app_version=version,
        max_content_length=upload_bytes,
    )
    from waitress import serve

    print(f"招投标资产管理 {version}：http://127.0.0.1:{args.port}")
    mutex = WorkspaceMutex(workspace)
    try:
        mutex.acquire()
        serve(
            app,
            host="127.0.0.1",
            port=args.port,
            max_request_body_size=upload_bytes,
        )
    finally:
        mutex.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
