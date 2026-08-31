"""Shared Tianyancha business-group and award-detail identity helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any


PACKAGE_DETAIL_FIELDS = (
    "package_number",
    "package_name",
    "lot_number",
    "lot_name",
    "section_number",
    "section_name",
    "package",
)
PACKAGE_DETAIL_HEADERS = {
    "package_number": ("标包编号", "包号", "分包编号"),
    "package_name": ("标包名称", "包名称", "分包名称"),
    "lot_number": ("标段编号",),
    "lot_name": ("标段名称",),
    "section_number": ("合同包编号",),
    "section_name": ("合同包名称",),
}


def business_sequence(payload: dict[str, Any]) -> str:
    return first_value(payload, "business_sequence", "sequence") or original_column_value(payload, "序号")


def publish_date(payload: dict[str, Any]) -> str:
    return first_value(payload, "publish_date") or original_column_value(payload, "发布日期")


def business_group_id(payload: dict[str, Any]) -> str:
    supplied = first_value(payload, "business_group_id")
    if supplied:
        return supplied
    sequence = business_sequence(payload)
    if not sequence or not is_tianyancha_xlsx_payload(payload):
        return ""
    trace = source_trace(payload)
    source_type = first_value(payload, "source_type") or str(trace.get("source_type") or "xlsx_file_upload")
    file_hash = first_value(payload, "file_hash", "file_sha256") or str(trace.get("file_sha256") or "")
    raw = "\n".join((normalize_identity(source_type), file_hash.lower(), normalize_text(sequence)))
    return f"business_group_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def business_project_id(payload: dict[str, Any]) -> str:
    group_id = business_group_id(payload)
    if not group_id:
        return ""
    digest = hashlib.sha256(f"tianyancha_business_group\n{group_id}".encode("utf-8")).hexdigest()[:16]
    return f"project_{digest}"


def award_detail_id(payload: dict[str, Any], trace: dict[str, Any] | None = None) -> str:
    supplied = first_value(payload, "award_detail_id")
    if supplied:
        return supplied
    combined = with_trace(payload, trace)
    group_id = business_group_id(combined)
    source = source_trace(combined)
    package_detail = package_details(combined)
    winner = first_value(combined, "winner", "winner_company")
    award_amount = first_value(combined, "award_amount")
    if not group_id:
        source_url = first_value(combined, "source_url") or str(source.get("source_url") or "")
        source_key = first_value(combined, "source_key") or str(source.get("source_key") or "")
        if not (winner or award_amount) or not (source_url or source_key):
            return ""
        source_identity = (
            {"source_url": normalize_text(source_url)}
            if source_url
            else {
                "source_type": normalize_identity(
                    first_value(combined, "source_type") or source.get("source_type")
                ),
                "source_key": normalize_text(source_key),
            }
        )
        raw = json.dumps(
            {
                "source_identity": source_identity,
                "project_number": normalize_identity(first_value(combined, "project_number")),
                "winner": normalize_identity(winner),
                "award_amount": normalize_identity(award_amount),
                "package_detail": package_detail,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return f"award_detail_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"
    source_identity = {
        "file_sha256": first_value(combined, "file_hash", "file_sha256") or str(source.get("file_sha256") or ""),
        "sheet_name": first_value(combined, "sheet_name") or str(source.get("sheet_name") or ""),
        "excel_row_number": first_value(combined, "excel_row_number") or str(source.get("excel_row_number") or ""),
        "source_key": first_value(combined, "source_key") or str(source.get("source_key") or ""),
    }
    raw = json.dumps(
        {
            "source_type": normalize_identity(first_value(combined, "source_type") or source.get("source_type")),
            "business_group_id": group_id,
            "winner": normalize_identity(winner),
            "award_amount": normalize_identity(award_amount),
            "package_detail": package_detail,
            "source_identity": source_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"award_detail_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def build_award_detail(payload: dict[str, Any], trace: dict[str, Any] | None = None) -> dict[str, Any]:
    combined = with_trace(payload, trace)
    detail_id = award_detail_id(combined)
    if not detail_id:
        return {}
    source = source_trace(combined)
    sequence = business_sequence(combined)
    sheet_name = first_value(combined, "sheet_name") or str(source.get("sheet_name") or "")
    row_number = first_value(combined, "excel_row_number") or str(source.get("excel_row_number") or "")
    return {
        "award_detail_id": detail_id,
        "business_sequence": sequence,
        "winner": first_value(combined, "winner", "winner_company"),
        "award_amount": first_value(combined, "award_amount"),
        "package_detail": package_details(combined),
        "source_type": first_value(combined, "source_type") or str(source.get("source_type") or ""),
        "sheet_name": sheet_name,
        "excel_row_number": int(row_number) if str(row_number).isdigit() else row_number,
        "source_trace": copy.deepcopy(source),
    }


def merge_award_details(
    existing: list[dict[str, Any]] | Any,
    incoming: list[dict[str, Any]] | Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged = [dict(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    known = {str(item.get("award_detail_id") or "") for item in merged}
    added: list[dict[str, Any]] = []
    if not isinstance(incoming, list):
        return merged, added
    for item in incoming:
        if not isinstance(item, dict):
            continue
        detail_id = str(item.get("award_detail_id") or "")
        if not detail_id or detail_id in known:
            continue
        copied = copy.deepcopy(item)
        merged.append(copied)
        added.append(copied)
        known.add(detail_id)
    return merged, added


def package_details(payload: dict[str, Any]) -> dict[str, str]:
    details: dict[str, str] = {}
    for field in PACKAGE_DETAIL_FIELDS:
        value = first_value(payload, field)
        if not value:
            for header in PACKAGE_DETAIL_HEADERS.get(field, ()):
                value = original_column_value(payload, header)
                if value:
                    break
        if value:
            details[field] = value
    return details


def source_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("source_trace")
    return dict(trace) if isinstance(trace, dict) else dict(payload)


def original_column_value(payload: dict[str, Any], header: str) -> str:
    for source in payload_sources(payload):
        columns = source.get("original_columns")
        if not isinstance(columns, list):
            continue
        for column in columns:
            if isinstance(column, dict) and str(column.get("header") or "").strip() == header:
                value = normalize_text(column.get("value"))
                if value:
                    return value
    return ""


def first_value(payload: dict[str, Any], *keys: str) -> str:
    for source in payload_sources(payload):
        for key in keys:
            value = normalize_text(source.get(key))
            if value:
                return value
    return ""


def payload_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict) and value not in sources:
            sources.append(value)

    add(payload)
    add(payload.get("processing_result"))
    add(payload.get("extracted_fields"))
    trace = payload.get("source_trace")
    add(trace)
    if isinstance(trace, dict):
        add(trace.get("extracted_fields"))
    return sources


def is_xlsx_payload(payload: dict[str, Any]) -> bool:
    source = normalize_identity(first_value(payload, "source_type"))
    file_type = normalize_text(first_value(payload, "file_type")).lower()
    trace = source_trace(payload)
    return (
        "xlsx" in source
        or file_type == ".xlsx"
        or bool(first_value(payload, "file_hash", "file_sha256") or trace.get("file_sha256"))
        and bool(first_value(payload, "sheet_name") or trace.get("sheet_name"))
    )


def is_tianyancha_xlsx_payload(payload: dict[str, Any]) -> bool:
    if not is_xlsx_payload(payload):
        return False
    if first_value(payload, "is_tianyancha_xlsx").lower() in {"true", "1", "yes"}:
        return True
    source = normalize_identity(first_value(payload, "source_type", "source_name"))
    if "tianyancha" in source:
        return True
    headers: set[str] = set()
    for candidate in payload_sources(payload):
        columns = candidate.get("original_columns")
        if isinstance(columns, list):
            headers.update(
                str(column.get("header") or "").strip()
                for column in columns
                if isinstance(column, dict)
            )
        original_headers = candidate.get("original_headers")
        if isinstance(original_headers, list):
            headers.update(str(header or "").strip() for header in original_headers)
    return bool(headers.intersection({"发布日期", "省份地区", "招采单位", "招投标详情"}))


def with_trace(payload: dict[str, Any], trace: dict[str, Any] | None) -> dict[str, Any]:
    combined = dict(payload)
    if trace is not None:
        combined["source_trace"] = dict(trace)
    return combined


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", normalize_text(value)).casefold()
    return re.sub(r"[\s（）()【】\[\]《》<>“”\"':：;；,，.。_\-—+]", "", normalized)
