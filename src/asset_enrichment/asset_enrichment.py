from __future__ import annotations

import argparse
import json
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.discovery.project_discovery import build_project_discovery_report, load_asset_views


DEFAULT_ASSET_ENRICHMENT_CANDIDATES = Path("data/diagnostics/asset_enrichment_candidates.json")
DEFAULT_ASSET_ENRICHMENT_RECORDS = Path("data/cache/asset_enrichment_records.json")
DEFAULT_ASSET_ENRICHMENT_SUMMARY = Path("data/diagnostics/asset_enrichment_summary.json")
HIGH_CONFIDENCE_THRESHOLD = 0.8
SUPPORTED_FIELDS = {
    "bid_open_time": {
        "record_keys": ["bid_open_time", "open_time", "bid_time"],
        "timeline_keys": ["bid_open_time"],
        "excel_field": "bid_open_time",
        "document_markers": ["bid", "tender", "\u62db\u6807", "\u91c7\u8d2d"],
    },
    "budget_amount": {
        "record_keys": ["budget_amount", "budget"],
        "timeline_keys": [],
        "excel_field": "budget",
        "document_markers": ["bid", "tender", "budget", "\u62db\u6807", "\u9884\u7b97"],
    },
    "winner_company": {
        "record_keys": ["winner_company", "winner"],
        "timeline_keys": [],
        "excel_field": "winner",
        "document_markers": ["award", "winner", "\u4e2d\u6807", "\u6210\u4ea4"],
    },
    "award_amount": {
        "record_keys": ["award_amount"],
        "timeline_keys": [],
        "excel_field": "award_amount",
        "document_markers": ["award", "winner", "\u4e2d\u6807", "\u6210\u4ea4"],
    },
}


def build_asset_enrichment_payload(
    asset_views: list[dict[str, Any]],
    discovery_reports: list[dict[str, Any]] | None = None,
    high_confidence_threshold: float = HIGH_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    reports_by_project = {str(item.get("project_id") or ""): dict(item) for item in discovery_reports or []}
    high_confidence_records: list[dict[str, Any]] = []
    low_confidence_candidates: list[dict[str, Any]] = []
    threshold = _normalized_threshold(high_confidence_threshold)
    for view in asset_views:
        project_id = str(view.get("project_id") or "")
        report = reports_by_project.get(project_id) or build_project_discovery_report(view)
        candidate = _candidate_from_view(view, report)
        high_fields = [field for field in candidate["field_supplements"] if float(field.get("confidence") or 0) >= threshold]
        low_fields = [field for field in candidate["field_supplements"] if float(field.get("confidence") or 0) < threshold]
        if high_fields:
            high_confidence_records.append(_record_from_candidate(candidate, high_fields))
        if low_fields or not high_fields:
            low_candidate = dict(candidate)
            low_candidate["field_supplements"] = low_fields
            low_candidate["supplemented_fields"] = [field["field_name"] for field in low_fields]
            low_confidence_candidates.append(low_candidate)
    return {
        "high_confidence_records": high_confidence_records,
        "low_confidence_candidates": low_confidence_candidates,
        "summary": {
            "total_projects": len(asset_views),
            "high_confidence_count": len(high_confidence_records),
            "low_confidence_count": len(low_confidence_candidates),
            "high_confidence_threshold": threshold,
            "supported_fields": list(SUPPORTED_FIELDS),
        },
    }


def write_asset_enrichment_outputs(
    payload: dict[str, Any],
    candidates_output: Path = DEFAULT_ASSET_ENRICHMENT_CANDIDATES,
    high_confidence_records_output: Path = DEFAULT_ASSET_ENRICHMENT_RECORDS,
    summary_output: Path = DEFAULT_ASSET_ENRICHMENT_SUMMARY,
) -> None:
    candidates_output.parent.mkdir(parents=True, exist_ok=True)
    high_confidence_records_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    candidates_output.write_text(
        json.dumps(payload.get("low_confidence_candidates", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    high_confidence_records_output.write_text(
        json.dumps(payload.get("high_confidence_records", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_output.write_text(json.dumps(payload.get("summary", {}), ensure_ascii=False, indent=2), encoding="utf-8")


def apply_high_confidence_records_to_excel(
    records_path: Path = DEFAULT_ASSET_ENRICHMENT_RECORDS,
    excel_path: Path = Path("招投标.xlsx"),
    sheet_name: str = "Sheet1",
    summary_path: Path = Path("data/cache/asset_enrichment_excel_writer_summary.json"),
) -> dict[str, Any]:
    from src.excel_writer import run_writer

    args = Namespace(
        records=str(records_path),
        excel=str(excel_path),
        sheet_name=sheet_name,
        backup_dir="data/backups",
        summary=str(summary_path),
        override_summary="data/cache/override_summary.json",
        override_diff="data/diagnostics/override_diff.json",
    )
    return run_writer(args)


def _candidate_from_view(view: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    documents = [dict(item) for item in view.get("documents", []) if isinstance(item, dict)]
    records = [dict(item) for item in view.get("records", []) if isinstance(item, dict)]
    timeline = dict(view.get("timeline_summary") or {})
    field_supplements = _field_supplements(documents, records, timeline)
    source_document = _best_source_document(documents, [])
    source_record = _best_source_record(records)
    supplemented_fields = [field["field_name"] for field in field_supplements]
    return {
        "project_id": str(view.get("project_id") or ""),
        "project_name": str(view.get("project_name") or ""),
        "confidence": float(report.get("asset_score") or 0),
        "missing_documents": list(report.get("missing_documents") or []),
        "supplemented_fields": supplemented_fields,
        "field_supplements": field_supplements,
        "source_file": str(source_document.get("file_name") or source_record.get("record_title") or ""),
        "source_url": str(source_document.get("source_url") or ""),
        "source_document_id": str(source_document.get("document_id") or source_record.get("record_id") or ""),
        "timeline_summary": timeline,
        "reason": _candidate_reason(report, supplemented_fields),
    }


def _is_high_confidence(candidate: dict[str, Any], threshold: float) -> bool:
    normalized = _normalized_threshold(threshold)
    return any(float(field.get("confidence") or 0) >= normalized for field in candidate.get("field_supplements", []))


def _record_from_candidate(candidate: dict[str, Any], field_supplements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fields = field_supplements or list(candidate.get("field_supplements") or [])
    supplemented_fields = [field["field_name"] for field in fields]
    values_by_excel_field = _excel_values_from_fields(fields)
    source = fields[0] if fields else {}
    note = _enrichment_note(candidate, fields)
    return {
        "source_file": source.get("source_file", "") or candidate.get("source_file", ""),
        "source_document_id": source.get("source_document", "") or candidate.get("source_document_id", "") or candidate.get("project_id", ""),
        "doc_type": "asset_enrichment",
        "customer": "",
        "project_name": candidate.get("project_name", ""),
        "content": "",
        "budget": values_by_excel_field.get("budget", ""),
        "bid_open_time": values_by_excel_field.get("bid_open_time", ""),
        "winner": values_by_excel_field.get("winner", ""),
        "award_amount": values_by_excel_field.get("award_amount", ""),
        "note": note,
        "text_chars": 0,
        "error": "",
    }


def _enrichment_note(candidate: dict[str, Any], field_supplements: list[dict[str, Any]]) -> str:
    labels = {
        "updated_at": "\u66f4\u65b0\u65f6\u95f4",
        "source_file": "\u6765\u6e90\u6587\u4ef6\u540d",
        "source_url": "\u6765\u6e90URL",
        "supplemented_fields": "\u8865\u5145\u5b57\u6bb5",
        "field_confidence": "\u5b57\u6bb5\u7f6e\u4fe1\u5ea6",
    }
    source_file = _join_unique(str(field.get("source_file") or "") for field in field_supplements)
    source_url = _join_unique(str(field.get("source_url") or "") for field in field_supplements)
    supplemented_fields = [str(field.get("field_name") or "") for field in field_supplements]
    field_confidence = ",".join(
        f"{field.get('field_name')}={float(field.get('confidence') or 0):.2f}" for field in field_supplements
    )
    return "; ".join(
        [
            f"asset_enrichment",
            f"{labels['updated_at']}={_now()}",
            f"{labels['source_file']}={source_file or candidate.get('source_file', '')}",
            f"{labels['source_url']}={source_url or candidate.get('source_url', '')}",
            f"{labels['supplemented_fields']}={','.join(supplemented_fields)}",
            f"{labels['field_confidence']}={field_confidence}",
        ]
    )


def _field_supplements(
    documents: list[dict[str, Any]], records: list[dict[str, Any]], timeline: dict[str, Any]
) -> list[dict[str, Any]]:
    supplements: list[dict[str, Any]] = []
    for field_name, config in SUPPORTED_FIELDS.items():
        value, source_record, value_source = _field_value(field_name, config, records, timeline)
        if not value:
            continue
        source_document = _best_source_document(documents, config["document_markers"])
        confidence = _field_confidence(value_source, source_record, source_document)
        supplements.append(
            {
                "field_name": field_name,
                "field_value": value,
                "source_document": str(source_document.get("document_id") or source_record.get("record_id") or ""),
                "source_file": str(source_document.get("file_name") or source_record.get("record_title") or ""),
                "source_url": str(source_document.get("source_url") or ""),
                "confidence": confidence,
            }
        )
    return supplements


def _field_value(
    field_name: str, config: dict[str, Any], records: list[dict[str, Any]], timeline: dict[str, Any]
) -> tuple[str, dict[str, Any], str]:
    for record in records:
        for key in config["record_keys"]:
            value = str(record.get(key) or "").strip()
            if value:
                return value, record, "record"
    for key in config["timeline_keys"]:
        value = str(timeline.get(key) or "").strip()
        if value:
            return value, {}, "timeline"
    return "", {}, ""


def _field_confidence(value_source: str, source_record: dict[str, Any], source_document: dict[str, Any]) -> float:
    score = 0.55
    if value_source == "record":
        score += 0.15
    if value_source == "timeline":
        score += 0.1
    if source_document:
        score += 0.1
    if str(source_document.get("source_url") or "").strip():
        score += 0.1
    if str(source_record.get("extraction_time") or "").strip():
        score += 0.1
    return round(min(score, 0.99), 2)


def _excel_values_from_fields(field_supplements: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in field_supplements:
        config = SUPPORTED_FIELDS.get(str(field.get("field_name") or ""))
        if not config:
            continue
        values[str(config["excel_field"])] = str(field.get("field_value") or "")
    return values


def _best_source_document(documents: list[dict[str, Any]], markers: list[str]) -> dict[str, Any]:
    candidates = markers or ["bid", "tender", "award", "winner", "contract"]
    for document in documents:
        file_name = str(document.get("file_name") or "").lower()
        document_id = str(document.get("document_id") or "").lower()
        source_url = str(document.get("source_url") or "").lower()
        haystack = " ".join([file_name, document_id, source_url])
        if any(marker.lower() in haystack for marker in candidates):
            return document
    return documents[0] if documents else {}


def _best_source_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    return records[0] if records else {}


def _candidate_reason(report: dict[str, Any], supplemented_fields: list[str]) -> list[str]:
    reasons = [f"asset_score={float(report.get('asset_score') or 0):.2f}"]
    missing_documents = list(report.get("missing_documents") or [])
    if missing_documents:
        reasons.append("missing_documents=" + ",".join(missing_documents))
    if supplemented_fields:
        reasons.append("supplemented_fields=" + ",".join(supplemented_fields))
    else:
        reasons.append("no_supported_supplemented_fields")
    return reasons


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalized_threshold(value: float) -> float:
    return value / 100 if value > 1 else value


def _join_unique(values: Any) -> str:
    items: list[str] = []
    for value in values:
        if value and value not in items:
            items.append(value)
    return ",".join(items)


def load_discovery_reports(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("project_discovery_reports") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"Expected discovery report JSON array or object with project_discovery_reports: {path}")
    return [dict(item) for item in rows if isinstance(item, dict)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate asset enrichment candidates and high-confidence Excel records.")
    parser.add_argument("--asset-views", default="data/diagnostics/project_asset_views.json")
    parser.add_argument("--discovery-reports", default="data/diagnostics/project_discovery_reports.json")
    parser.add_argument("--candidates-output", default=str(DEFAULT_ASSET_ENRICHMENT_CANDIDATES))
    parser.add_argument("--high-confidence-records-output", default=str(DEFAULT_ASSET_ENRICHMENT_RECORDS))
    parser.add_argument("--summary-output", default=str(DEFAULT_ASSET_ENRICHMENT_SUMMARY))
    parser.add_argument("--high-confidence-threshold", type=float, default=HIGH_CONFIDENCE_THRESHOLD)
    parser.add_argument("--apply-high-confidence", action="store_true")
    parser.add_argument("--excel", default="招投标.xlsx")
    parser.add_argument("--sheet-name", default="Sheet1")
    args = parser.parse_args()

    asset_views = load_asset_views(Path(args.asset_views))
    discovery_reports = load_discovery_reports(Path(args.discovery_reports))
    payload = build_asset_enrichment_payload(asset_views, discovery_reports, args.high_confidence_threshold)
    write_asset_enrichment_outputs(
        payload,
        Path(args.candidates_output),
        Path(args.high_confidence_records_output),
        Path(args.summary_output),
    )
    if args.apply_high_confidence and payload["high_confidence_records"]:
        apply_high_confidence_records_to_excel(
            Path(args.high_confidence_records_output),
            Path(args.excel),
            args.sheet_name,
        )
    print(
        "asset_enrichment completed "
        f"high={payload['summary']['high_confidence_count']} low={payload['summary']['low_confidence_count']}"
    )


if __name__ == "__main__":
    main()
