from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("data/cache/enriched_records.json")
DEFAULT_OVERRIDES = Path("data/overrides/manual_overrides.json")
DEFAULT_OUTPUT = Path("data/cache/final_records.json")
DEFAULT_SUMMARY = Path("data/cache/override_summary.json")
DEFAULT_REVIEW_QUEUE = Path("data/diagnostics/manual_review_queue.json")
DEFAULT_REVIEW_MD = Path("data/diagnostics/manual_review_queue.md")
DEFAULT_DIFF = Path("data/diagnostics/override_diff.json")
DEFAULT_DIFF_MD = Path("data/diagnostics/override_diff.md")

OVERRIDABLE_FIELDS = {
    "customer",
    "project_name",
    "content",
    "budget",
    "bid_open_time",
    "winner",
    "award_amount",
    "note",
}
REQUIRED_OVERRIDE_META = {"source", "reason", "updated_at"}
CURRENT_VALUE_FIELDS = [
    "customer",
    "project_name",
    "content",
    "budget",
    "bid_open_time",
    "winner",
    "award_amount",
    "note",
]

SCORING_RULES = {
    "结果公告": {
        "weights": {"winner": 8, "award_amount": 8, "project_name": 3, "customer": 2},
        "action": "查找成交供应商/中标供应商、成交金额/中标金额。",
    },
    "合同": {
        "weights": {"award_amount": 8, "customer": 6, "winner": 6, "project_name": 3},
        "action": "从合同首页、签章页或金额条款补客户、中标厂商、中标金额。",
    },
    "采购公告/文件": {
        "weights": {"customer": 5, "project_name": 5, "budget": 4, "bid_open_time": 4, "content": 1},
        "action": "补客户、预算、开标时间、项目内容。",
    },
    "采购需求": {
        "weights": {"customer": 5, "project_name": 5, "budget": 4, "content": 3},
        "action": "补客户、预算、项目内容。",
    },
    "报价/明细": {
        "weights": {"customer": 4, "project_name": 6},
        "either_groups": [{"fields": ["budget", "award_amount"], "weight": 6, "label": "budget_or_award_amount"}],
        "action": "查找报价金额、供应商、项目名称。",
    },
    "资料": {
        "weights": {
            "customer": 4,
            "project_name": 4,
            "budget": 3,
            "winner": 3,
            "award_amount": 3,
            "bid_open_time": 1,
            "content": 1,
        },
        "action": "先判断是否应进入 Excel；不确定则人工复核。",
    },
}
DEFAULT_SCORING_RULE = {
    "weights": {"customer": 3, "project_name": 3, "budget": 2, "winner": 2, "award_amount": 2},
    "action": "按文档内容人工复核缺失字段。",
}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"[\s_]+", "", value)
    value = re.sub(r"[^\w]+", "", value, flags=re.UNICODE)
    return value.lower()


def is_blank(value: Any) -> bool:
    return not str(value or "").strip()


def get_doc_type(record: dict[str, Any]) -> str:
    return str(record.get("doc_type") or "资料").strip() or "资料"


def validate_override(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_OVERRIDE_META:
        if not str(item.get(key) or "").strip():
            errors.append(f"missing_{key}")

    has_matcher = any(str(item.get(key) or "").strip() for key in ("source_document_id", "source_file", "project_name"))
    if not has_matcher:
        errors.append("missing_matcher")

    has_field = any(str(item.get(field) or "").strip() for field in OVERRIDABLE_FIELDS)
    if not has_field:
        errors.append("missing_override_fields")

    return errors


def build_indexes(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    indexes = {
        "source_document_id": {},
        "source_file": {},
        "project_name": {},
    }
    for index, record in enumerate(records):
        doc_id = str(record.get("source_document_id") or "").strip()
        source_file = str(record.get("source_file") or "").strip()
        project_name = normalize(record.get("project_name"))
        if doc_id and doc_id not in indexes["source_document_id"]:
            indexes["source_document_id"][doc_id] = index
        if source_file and source_file not in indexes["source_file"]:
            indexes["source_file"][source_file] = index
        if project_name and project_name not in indexes["project_name"]:
            indexes["project_name"][project_name] = index
    return indexes


def find_record_index(override: dict[str, Any], indexes: dict[str, dict[str, int]]) -> tuple[int | None, str]:
    doc_id = str(override.get("source_document_id") or "").strip()
    if doc_id and doc_id in indexes["source_document_id"]:
        return indexes["source_document_id"][doc_id], "source_document_id"

    source_file = str(override.get("source_file") or "").strip()
    if source_file and source_file in indexes["source_file"]:
        return indexes["source_file"][source_file], "source_file"

    project_name = normalize(override.get("project_name"))
    if project_name and project_name in indexes["project_name"]:
        return indexes["project_name"][project_name], "project_name_normalized"

    return None, "not_matched"


def build_multi_indexes(records: list[dict[str, Any]]) -> dict[str, dict[str, list[int]]]:
    indexes: dict[str, dict[str, list[int]]] = {
        "source_document_id": {},
        "source_file": {},
        "project_name": {},
    }
    for index, record in enumerate(records):
        doc_id = str(record.get("source_document_id") or "").strip()
        source_file = str(record.get("source_file") or "").strip()
        project_name = normalize(record.get("project_name"))
        if doc_id:
            indexes["source_document_id"].setdefault(doc_id, []).append(index)
        if source_file:
            indexes["source_file"].setdefault(source_file, []).append(index)
        if project_name:
            indexes["project_name"].setdefault(project_name, []).append(index)
    return indexes


def record_id_for(record: dict[str, Any], index: int) -> str:
    return str(record.get("record_id") or record.get("source_document_id") or f"record-{index + 1:04d}")


def override_fields(override: dict[str, Any]) -> list[str]:
    return [field for field in sorted(OVERRIDABLE_FIELDS) if not is_blank(override.get(field))]


def find_record_indexes(override: dict[str, Any], indexes: dict[str, dict[str, list[int]]]) -> tuple[list[int], str]:
    doc_id = str(override.get("source_document_id") or "").strip()
    if doc_id:
        return indexes["source_document_id"].get(doc_id, []), "source_document_id"

    source_file = str(override.get("source_file") or "").strip()
    if source_file:
        return indexes["source_file"].get(source_file, []), "source_file"

    project_name = normalize(override.get("project_name"))
    if project_name:
        return indexes["project_name"].get(project_name, []), "project_name"

    return [], "none"


def empty_diff(override: dict[str, Any], override_index: int, status: str, matched_by: str, field: str = "") -> dict[str, Any]:
    return {
        "override_index": override_index,
        "match_status": status,
        "matched_by": matched_by,
        "record_id": "",
        "source_document_id": override.get("source_document_id", ""),
        "source_file": override.get("source_file", ""),
        "project_name": override.get("project_name", ""),
        "field": field,
        "before": "",
        "after": override.get(field, "") if field else "",
        "source": override.get("source", ""),
        "reason": override.get("reason", ""),
        "updated_at": override.get("updated_at", ""),
    }


def record_diff(
    override: dict[str, Any],
    override_index: int,
    record: dict[str, Any],
    record_index: int,
    status: str,
    matched_by: str,
    field: str,
) -> dict[str, Any]:
    return {
        "override_index": override_index,
        "match_status": status,
        "matched_by": matched_by,
        "record_id": record_id_for(record, record_index),
        "source_document_id": record.get("source_document_id", ""),
        "source_file": record.get("source_file", ""),
        "project_name": record.get("project_name", ""),
        "field": field,
        "before": record.get(field, ""),
        "after": override.get(field, ""),
        "source": override.get("source", ""),
        "reason": override.get("reason", ""),
        "updated_at": override.get("updated_at", ""),
    }


def build_override_diff(records: list[dict[str, Any]], overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexes = build_multi_indexes(records)
    diffs: list[dict[str, Any]] = []

    for position, override in enumerate(overrides, 1):
        fields = override_fields(override)
        errors = validate_override(override)
        if errors:
            for field in fields or [""]:
                diffs.append(empty_diff(override, position, "invalid", "none", field))
            continue

        record_indexes, matched_by = find_record_indexes(override, indexes)
        if not record_indexes:
            for field in fields or [""]:
                diffs.append(empty_diff(override, position, "skipped", matched_by, field))
            continue
        if len(record_indexes) > 1:
            for field in fields or [""]:
                diffs.append(empty_diff(override, position, "ambiguous", matched_by, field))
            continue

        record_index = record_indexes[0]
        record = records[record_index]
        for field in fields:
            status = "no_change" if record.get(field) == override.get(field) else "applied"
            diffs.append(record_diff(override, position, record, record_index, status, matched_by, field))

    seen_values: dict[tuple[str, str], set[str]] = {}
    for diff in diffs:
        if diff["match_status"] not in {"applied", "no_change"}:
            continue
        key = (str(diff["record_id"]), str(diff["field"]))
        seen_values.setdefault(key, set()).add(str(diff["after"]))

    conflict_keys = {key for key, values in seen_values.items() if len(values) > 1}
    for diff in diffs:
        key = (str(diff["record_id"]), str(diff["field"]))
        if diff["match_status"] in {"applied", "no_change"} and key in conflict_keys:
            diff["match_status"] = "conflict"

    return diffs


def diff_stats(diffs: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "diff_count": len(diffs),
        "applied_diff_count": sum(1 for item in diffs if item["match_status"] == "applied"),
        "no_change_count": sum(1 for item in diffs if item["match_status"] == "no_change"),
        "invalid_override_count": len({item["override_index"] for item in diffs if item["match_status"] == "invalid"}),
        "ambiguous_override_count": len({item["override_index"] for item in diffs if item["match_status"] == "ambiguous"}),
        "conflict_override_count": len({item["override_index"] for item in diffs if item["match_status"] == "conflict"}),
        "skipped_override_count": len({item["override_index"] for item in diffs if item["match_status"] == "skipped"}),
    }


def apply_one(record: dict[str, Any], override: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for field in sorted(OVERRIDABLE_FIELDS):
        value = override.get(field)
        if value is None:
            continue
        if not str(value).strip():
            continue
        if record.get(field) != value:
            record[field] = value
            changed.append(field)

    if changed:
        record["manual_override_applied"] = True
        record["manual_override_source"] = override.get("source", "")
        record["manual_override_reason"] = override.get("reason", "")
        record["manual_override_updated_at"] = override.get("updated_at", "")

    return changed


def apply_overrides(
    records: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    final_records = deepcopy(records)
    diffs = build_override_diff(final_records, overrides)
    indexes = build_indexes(final_records)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    applied_positions: set[int] = set()
    skipped_positions: set[int] = set()

    for diff in diffs:
        position = int(diff["override_index"])
        override = overrides[position - 1]
        if diff["match_status"] != "applied":
            if position not in applied_positions and position not in skipped_positions:
                skipped.append(
                    {
                        "position": position,
                        "status": diff["match_status"],
                        "match_method": diff["matched_by"],
                        "source_document_id": override.get("source_document_id", ""),
                        "source_file": override.get("source_file", ""),
                        "project_name": override.get("project_name", ""),
                    }
                )
                skipped_positions.add(position)
            continue

        record_index, match_method = find_record_index(override, indexes)
        if record_index is None:
            skipped.append(
                {
                    "position": position,
                    "status": "not_matched",
                    "source_document_id": override.get("source_document_id", ""),
                    "source_file": override.get("source_file", ""),
                    "project_name": override.get("project_name", ""),
                }
            )
            continue

        field = str(diff["field"])
        final_records[record_index][field] = diff["after"]
        final_records[record_index]["manual_override_applied"] = True
        final_records[record_index]["manual_override_source"] = override.get("source", "")
        final_records[record_index]["manual_override_reason"] = override.get("reason", "")
        final_records[record_index]["manual_override_updated_at"] = override.get("updated_at", "")

        existing = next((item for item in applied if item["position"] == position), None)
        if existing:
            existing["changed_fields"].append(field)
        else:
            applied.append(
                {
                    "position": position,
                    "record_index": record_index,
                    "match_method": match_method,
                    "source_file": final_records[record_index].get("source_file", ""),
                    "changed_fields": [field],
                    "source": override.get("source", ""),
                    "reason": override.get("reason", ""),
                    "updated_at": override.get("updated_at", ""),
                }
            )
        applied_positions.add(position)

    summary = {
        "records": len(final_records),
        "overrides": len(overrides),
        "applied": len(applied),
        "skipped": len(skipped),
        "applied_items": applied,
        "skipped_items": skipped,
    }
    summary.update(diff_stats(diffs))
    return final_records, summary, diffs


def detect_suspicious_fields(record: dict[str, Any]) -> list[str]:
    suspicious: list[str] = []
    customer = str(record.get("customer") or "").strip()
    content = str(record.get("content") or "").strip()
    winner = str(record.get("winner") or "").strip()

    if customer and (len(customer) < 4 or len(customer) > 80):
        suspicious.append("customer")
    if content and (len(content) < 8 or content in {"-", "/", "无", "详见附件"}):
        suspicious.append("content")
    if winner and len(winner) < 4:
        suspicious.append("winner")

    return suspicious


def score_record(record: dict[str, Any]) -> tuple[int, list[str]]:
    doc_type = get_doc_type(record)
    rule = SCORING_RULES.get(doc_type, DEFAULT_SCORING_RULE)
    score = 0
    missing_fields: list[str] = []

    for field, weight in rule.get("weights", {}).items():
        if is_blank(record.get(field)):
            score += int(weight)
            missing_fields.append(field)

    for group in rule.get("either_groups", []):
        fields = list(group.get("fields", []))
        if fields and all(is_blank(record.get(field)) for field in fields):
            score += int(group.get("weight", 0))
            missing_fields.extend(fields)

    return score, missing_fields


def current_values(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field, "") for field in CURRENT_VALUE_FIELDS}


def build_override_template(record: dict[str, Any], missing_fields: list[str], suspicious_fields: list[str]) -> dict[str, Any]:
    fields_to_fill = [field for field in dict.fromkeys(missing_fields + suspicious_fields) if field in OVERRIDABLE_FIELDS]
    template: dict[str, Any] = {
        "source_document_id": record.get("source_document_id", ""),
        "source_file": record.get("source_file", ""),
        "source": "manual_review_queue",
        "reason": "",
        "updated_at": "",
    }
    for field in fields_to_fill:
        template[field] = ""
    return template


def suggested_action_for(record: dict[str, Any]) -> str:
    doc_type = get_doc_type(record)
    return str(SCORING_RULES.get(doc_type, DEFAULT_SCORING_RULE)["action"])


def build_review_queue(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for index, record in enumerate(records, 1):
        priority_score, missing_fields = score_record(record)
        suspicious_fields = detect_suspicious_fields(record)
        if priority_score <= 0 and not suspicious_fields:
            continue

        review_item = {
            "record_id": record.get("record_id") or record.get("source_document_id") or f"record-{index:04d}",
            "source_document_id": record.get("source_document_id", ""),
            "source_file": record.get("source_file", ""),
            "doc_type": get_doc_type(record),
            "project_name": record.get("project_name", ""),
            "customer": record.get("customer", ""),
            "current_values": current_values(record),
            "missing_fields": missing_fields,
            "suspicious_fields": suspicious_fields,
            "priority_score": priority_score + len(suspicious_fields),
            "suggested_action": suggested_action_for(record),
            "override_template": build_override_template(record, missing_fields, suspicious_fields),
        }
        queue.append(review_item)

    queue.sort(key=lambda item: (-int(item["priority_score"]), str(item["doc_type"]), str(item["source_file"])))
    return queue


def markdown_escape(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = text.replace("|", "\\|")
    return text


def render_review_queue_markdown(queue: list[dict[str, Any]]) -> str:
    lines = [
        "# Manual Review Queue",
        "",
        "## 文档元信息",
        "",
        "- 文档类型：人工复核待办清单",
        "- Version: v1.0",
        "- Status: Draft",
        "- Owner: damao",
        "- Last Updated: 2026-07-06",
        "",
        "## 评分说明",
        "",
        "- 结果公告：重点检查 `winner`、`award_amount`。",
        "- 合同：重点检查 `customer`、`winner`、`award_amount`。",
        "- 采购公告/文件：重点检查 `customer`、`budget`、`bid_open_time`、`content`。",
        "- 采购需求：重点检查 `customer`、`budget`、`content`。",
        "- 报价/明细：重点检查 `customer`、`project_name`、`budget` 或 `award_amount`。",
        "- 资料：低置信通用检查，优先建议人工判断是否应进入 Excel。",
        "",
        "## 待办清单",
        "",
        "| 排名 | 分数 | 文档类型 | 文件 | 项目名称 | 客户 | 问题 | 建议动作 |",
        "| ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]

    for rank, item in enumerate(queue, 1):
        problems = []
        if item["missing_fields"]:
            problems.append("缺失：" + "、".join(item["missing_fields"]))
        if item["suspicious_fields"]:
            problems.append("疑似异常：" + "、".join(item["suspicious_fields"]))
        problem_text = "；".join(problems)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    str(item["priority_score"]),
                    markdown_escape(item["doc_type"]),
                    markdown_escape(item["source_file"]),
                    markdown_escape(item["project_name"]),
                    markdown_escape(item["customer"]),
                    markdown_escape(problem_text),
                    markdown_escape(item["suggested_action"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Revision History",
            "",
            "### v1.0",
            "",
            "- 初始版本。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_override_diff_markdown(diffs: list[dict[str, Any]]) -> str:
    lines = [
        "# Override Diff",
        "",
        "## 文档元信息",
        "",
        "- 文档类型：Override Diff 审计",
        "- Version: v1.0",
        "- Status: Draft",
        "- Owner: damao",
        "- Last Updated: 2026-07-06",
        "",
        "## Diff 表",
        "",
        "| 序号 | 状态 | 匹配方式 | 文件 | 项目名称 | 字段 | 原值 | 新值 | 原因 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(diffs, 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_escape(item["match_status"]),
                    markdown_escape(item["matched_by"]),
                    markdown_escape(item["source_file"]),
                    markdown_escape(item["project_name"]),
                    markdown_escape(item["field"]),
                    markdown_escape(item["before"]),
                    markdown_escape(item["after"]),
                    markdown_escape(item["reason"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Revision History",
            "",
            "### v1.0",
            "",
            "- 初始版本。",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply manual overrides to enriched records and output final records.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input EnrichedRecord JSON path.")
    parser.add_argument("--overrides", default=str(DEFAULT_OVERRIDES), help="Manual overrides JSON path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output final records JSON path.")
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY), help="Override summary JSON path.")
    parser.add_argument("--review-queue", default="", help="Optional manual review queue JSON output path.")
    parser.add_argument("--review-md", default="", help="Optional manual review queue Markdown output path.")
    parser.add_argument("--diff", default="", help="Optional override diff JSON output path.")
    parser.add_argument("--diff-md", default="", help="Optional override diff Markdown output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = read_json(Path(args.input), [])
    overrides = read_json(Path(args.overrides), [])
    if not isinstance(records, list):
        raise ValueError(f"Expected input records JSON array: {args.input}")
    if not isinstance(overrides, list):
        raise ValueError(f"Expected overrides JSON array: {args.overrides}")

    final_records, summary, diffs = apply_overrides(records, overrides)
    write_json(Path(args.output), final_records)
    write_json(Path(args.summary), summary)

    review_queue: list[dict[str, Any]] = []
    if args.review_queue or args.review_md:
        review_queue = build_review_queue(final_records)
    if args.review_queue:
        write_json(Path(args.review_queue), review_queue)
    if args.review_md:
        review_md_path = Path(args.review_md)
        review_md_path.parent.mkdir(parents=True, exist_ok=True)
        review_md_path.write_text(render_review_queue_markdown(review_queue), encoding="utf-8")

    if args.diff:
        write_json(Path(args.diff), diffs)
    if args.diff_md:
        diff_md_path = Path(args.diff_md)
        diff_md_path.parent.mkdir(parents=True, exist_ok=True)
        diff_md_path.write_text(render_override_diff_markdown(diffs), encoding="utf-8")

    print(
        "override completed "
        f"records={summary['records']} "
        f"overrides={summary['overrides']} "
        f"applied={summary['applied']} "
        f"skipped={summary['skipped']} "
        f"review_items={len(review_queue)} "
        f"diffs={len(diffs)}"
    )


if __name__ == "__main__":
    main()
