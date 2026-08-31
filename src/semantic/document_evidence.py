"""Bounded ordered evidence for the uploaded-document semantic mainline."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Iterable, Mapping


TARGET_DOCUMENT_EVIDENCE_BYTES = 18_432
MAX_DOCUMENT_EVIDENCE_BYTES = 24_576
TABLE_PAYLOAD_BUDGET_BYTES = TARGET_DOCUMENT_EVIDENCE_BYTES // 3
SECTION_TARGET_BYTES = 2_048
MAX_TABLE_CELL_CHARS = 512
MAX_TABLE_COLUMNS = 32
BUSINESS_ANCHORS = re.compile(
    r"项目(?:名称|编号|概况)|采购人|招标人|预算|最高限价|开标|投标|中标|成交|"
    r"供应商|合同|金额|服务范围|采购需求|主要标的|联系方式|公告期限",
    re.IGNORECASE,
)

# These are structural categories only.  They reserve representation for the
# kinds of evidence a semantic consumer needs without extracting any business
# value in this layer.
FIELD_CATEGORY_ORDER = (
    "opening_identity",
    "customer",
    "project_number",
    "budget",
    "bid_opening",
    "core_scope",
    "award_result",
)
_OPENING_TIME_LABELS = (
    r"(?:响应文件开启\s*时间|响应文件开标\s*时间|投标文件启封\s*时间|"
    r"投标文件开封\s*时间|投标文件开启\s*时间|开标\s*时间)"
)
_DEADLINE_TIME_LABELS = (
    r"(?:提交投标文件截止\s*时间|投标文件递交截止\s*时间|响应文件递交截止\s*时间|"
    r"投标文件截止\s*时间|投标截止\s*时间|递交投标文件截止\s*时间|响应截止\s*时间|截止\s*时间)"
)
_TIME_LABELS = rf"(?:{_OPENING_TIME_LABELS}|{_DEADLINE_TIME_LABELS})"
_PROJECT_NAME_LABEL = r"(?:采购\s*项目\s*名称|招标\s*项目\s*名称|项目\s*名称|项目名)"
_PURCHASER_LABEL = (
    r"(?:采\s*购\s*(?:人|单\s*位)(?:\s*名\s*称)?|"
    r"招\s*标\s*人(?:\s*名\s*称)?|买\s*方|甲\s*方|委\s*托\s*人)"
)
_AGENCY_LABEL = (
    r"(?:采\s*购\s*代\s*理\s*机\s*构|招\s*标\s*代\s*理\s*机\s*构|"
    r"招\s*标\s*采\s*购\s*代\s*理\s*机\s*构|代\s*理\s*机\s*构|"
    r"发布者|发布单位|发布人)"
)
FIELD_CATEGORY_PATTERNS = {
    "customer": re.compile(rf"{_PURCHASER_LABEL}", re.IGNORECASE),
    "project_number": re.compile(r"项目(?:编号|代码)|采购编号|招标编号|合同编号", re.IGNORECASE),
    "budget": re.compile(r"预算|最高限价|控制价|采购预算|金额上限", re.IGNORECASE),
    "bid_opening": re.compile(
        rf"开标|{_OPENING_TIME_LABELS}|投标截止|响应文件递交|截止时间|递交截止|投标时间",
        re.IGNORECASE,
    ),
    "core_scope": re.compile(
        r"采购项目内容|采购内容|采购需求|项目概况|服务范围|主要内容|建设内容|"
        r"工作内容|交付物|技术要求|技术需求|标的",
        re.IGNORECASE,
    ),
    "award_result": re.compile(
        r"中标|成交|供应商|中选|评标结果|候选人|中标金额|成交金额",
        re.IGNORECASE,
    ),
    "opening_identity": re.compile(
        rf"{_PROJECT_NAME_LABEL}|公告名称|公告标题",
        re.IGNORECASE,
    ),
}
_CURRENCY_SIGNAL = re.compile(
    r"(?:[¥￥]\s*\d|\d[\d,\s]*(?:\.\d+)?\s*(?:元|万元|亿元|万))",
    re.IGNORECASE,
)
_BUDGET_OVERALL_SIGNAL = re.compile(r"预算金额|项目预算|采购预算", re.IGNORECASE)
_BUDGET_SUBORDINATE_SIGNAL = re.compile(
    r"最高限价|控制价|预\s*[（(]?\s*概\s*[）)]?\s*算|"
    r"包(?:预算|金额)|分包|标段预算|子项金额|投标报价|代理费",
    re.IGNORECASE,
)
_DATE_TIME_SIGNAL = re.compile(
    r"(?:19|20)\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}"
    r"(?:\s*[日号])?(?:\s*[0-2]?\d\s*(?:时|点|:)\s*\d{1,2}"
    r"(?:\s*分)?(?:\s*\d{1,2}\s*秒)?)?"
    r"|\b[0-2]?\d:[0-5]\d\b",
    re.IGNORECASE,
)
_PROJECT_NUMBER_SIGNAL = re.compile(
    r"(?:[A-Za-z]{1,}[A-Za-z0-9]*\d[A-Za-z0-9]*|\d{2,}[A-Za-z][A-Za-z0-9]*|\d{5,})"
    r"[-_/A-Za-z0-9]*|[A-Za-z0-9]{3,}[-_/][A-Za-z0-9_/-]{2,}",
)


def _has_project_number_signal(text: str) -> bool:
    """Avoid pathological project-number matching on digit-free filler."""
    if re.search(
        r"(?:项目\s*(?:编号|代码)|采购\s*编号|招标\s*编号|合同\s*编号)"
        r"\s*[:：]?\s*[A-Za-z0-9][A-Za-z0-9_/-]{2,64}",
        text,
        re.IGNORECASE,
    ) is not None:
        return True
    if not any("0" <= character <= "9" for character in text):
        return False
    if any(separator in text for separator in "-_/\\"):
        return _PROJECT_NUMBER_SIGNAL.search(text) is not None
    if re.search(r"[A-Za-z]{2,}\d|\d{2,}[A-Za-z]", text) is None:
        return False
    return _PROJECT_NUMBER_SIGNAL.search(text) is not None
_REFERENCE_SIGNAL = re.compile(r"详见|参见|见第|另见|以.+为准|具体要求.+(?:见|详见)")
_CORE_SCOPE_HEADING_PATTERN = re.compile(
    r"^\s*(?:(?:第?\s*(?:[一二三四五六七八九十百千万]+|\d+)\s*"
    r"(?:部分|[、.．)])\s*))?"
    r"(?:采购项目内容|采购内容|采购需求|技术要求|技术需求|服务范围|主要内容|"
    r"建设内容|工作内容|交付物)\s*$",
    re.IGNORECASE,
)
_CORE_SCOPE_ACTION_SIGNAL = re.compile(
    r"提供|包括|涵盖|实施|部署|开发|建设|运维|维护|分析|输出|形成|完成|"
    r"支持|培训|验收|关联|扩展|交付",
    re.IGNORECASE,
)
_CORE_SCOPE_OBJECT_SIGNAL = re.compile(
    r"系统|平台|服务|功能|模块|接口|数据|报告|账号|期限|设备|运维|分析|"
    r"交付|验收|培训|成果|方案|文档|指标|画像|关联|扩线|监测|识别|查询|保障",
    re.IGNORECASE,
)
CORE_SCOPE_FORWARD_SECTION_WINDOW = 12
CORE_SCOPE_FORWARD_BYTE_BUDGET = 4_096
CORE_SCOPE_FORWARD_TAIL_SECTIONS = 3
_CORE_SCOPE_MAJOR_HEADING_PATTERN = re.compile(
    r"^\s*(?:第\s*(?:[一二三四五六七八九十百千万]+|\d+)\s*"
    r"(?:部分|章|篇|节)|[一二三四五六七八九十百千万]+\s*[、.．])\s*.+$",
    re.IGNORECASE,
)

FIELD_INTEGRITY_SCHEMA_VERSION = "document-field-integrity/v1"
MAX_FIELD_INTEGRITY_ISSUES = 16
_KNOWN_LABEL_BOUNDARY = (
    rf"(?:{_PROJECT_NAME_LABEL}|{_AGENCY_LABEL}|{_PURCHASER_LABEL}|"
    r"项目编号|采购编号|招标编号|合同编号|预算金额|项目预算|采购预算|"
    r"最高限价|控制价|预\s*[（(]?\s*概\s*[）)]?\s*算|"
    r"包(?:预算|金额)|分包|标段预算|子项金额|投标报价|代理费|"
    rf"{_OPENING_TIME_LABELS}|{_DEADLINE_TIME_LABELS}|"
    r"投标截止|响应文件递交|递交截止|"
    r"采购内容|采购需求|项目概况|服务范围|主要内容|建设内容|技术需求|"
    r"中标单位|中标人|成交供应商|成交人|供应商|中选|中标金额|成交金额)"
)
_LABEL_VALUE_END = (
    rf"(?=(?:{_KNOWN_LABEL_BOUNDARY})\s*[:：]|[\r\n；;。]|$)"
)
_PROJECT_NAME_LABELLED_VALUE = re.compile(
    rf"{_PROJECT_NAME_LABEL}\s*[:：]\s*"
    rf"(?P<value>[^\r\n；;。]{{1,256}}?){_LABEL_VALUE_END}"
)
_PURCHASER_LABELLED_VALUE = re.compile(
    rf"{_PURCHASER_LABEL}\s*[:：]\s*"
    rf"(?P<value>[^\r\n；;。]{{1,256}}?){_LABEL_VALUE_END}"
)
_AGENCY_LABELLED_VALUE = re.compile(
    rf"{_AGENCY_LABEL}"
    rf"\s*[:：]\s*(?P<value>[^\r\n；;。]{{1,256}}?){_LABEL_VALUE_END}"
)
_LABELLED_TIME_VALUE_END = (
    rf"(?=(?:{_TIME_LABELS})\s*[:：]?|[\r\n；;。]|$)"
)
_OPENING_REFERENCE_PATTERN = re.compile(
    rf"{_OPENING_TIME_LABELS}\s*[:：]\s*(?:同上|同前|上述|前述)"
    r"(?=$|[\s,，；;。])",
    re.IGNORECASE,
)
_OPENING_TIME_LABEL_SIGNAL = re.compile(_OPENING_TIME_LABELS, re.IGNORECASE)
_SPLIT_OPENING_HEADING_PATTERN = re.compile(
    r"^\s*(?:(?:[一二三四五六七八九十百千万]+|\d+)\s*[、.．)]\s*)?"
    r"(?:响应文件开启|响应文件开标|投标文件启封|投标文件开封|投标文件开启|"
    r"开标|开封|开启)"
    r"\s*(?:(?:时间(?:\s*(?:和|与)\s*地点)?)|(?:和|与)\s*地点)?\s*$",
    re.IGNORECASE,
)
_SPLIT_OPENING_TIME_ROW_PATTERN = re.compile(
    r"^\s*时间\s*[:：]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_INLINE_DATETIME_ROW_PATTERN = re.compile(
    r"^\s*(?:19|20)\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}"
    r"\s*[日号]?\s*"
    r"(?:(?:上午|下午|早上|晚上)\s*)?"
    r"(?:"
    r"[0-2]?\d\s*(?:时|点)\s*[0-5]?\d\s*分?"
    r"(?:\s*[0-5]?\d\s*秒)?"
    r"|[0-2]?\d:[0-5]\d(?::[0-5]\d)?"
    r")\s*"
    r"(?:[（(]\s*北京时间\s*[）)]|北京时间)?\s*$",
    re.IGNORECASE,
)


def _labelled_time_values(text: str, labels: str) -> list[str]:
    pattern = re.compile(
        rf"{labels}\s*[:：]?\s*(?P<value>[^\r\n；;。]*?){_LABELLED_TIME_VALUE_END}",
        re.IGNORECASE,
    )
    return [match.group("value").strip() for match in pattern.finditer(text)]


def _has_explicit_opening_datetime(text: str) -> bool:
    return any(
        _DATE_TIME_SIGNAL.search(value)
        for value in _labelled_time_values(text, _OPENING_TIME_LABELS)
    )


def _has_deadline_datetime(text: str) -> bool:
    return any(
        _DATE_TIME_SIGNAL.search(value)
        for value in _labelled_time_values(text, _DEADLINE_TIME_LABELS)
    )


def _has_opening_reference(text: str) -> bool:
    return bool(_OPENING_REFERENCE_PATTERN.search(text))


def _nonempty_rows(section: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in section.get("rows", [])
        if str(row.get("text") or "").strip()
    ]


def _has_split_opening_heading(section: dict[str, Any]) -> bool:
    rows = _nonempty_rows(section)
    return bool(
        rows
        and _SPLIT_OPENING_HEADING_PATTERN.fullmatch(
            str(rows[-1].get("text") or "").strip()
        )
    )


def _has_split_opening_time_row(section: dict[str, Any]) -> bool:
    rows = _nonempty_rows(section)
    if not rows:
        return False
    match = _SPLIT_OPENING_TIME_ROW_PATTERN.fullmatch(
        str(rows[0].get("text") or "").strip()
    )
    return bool(match and _DATE_TIME_SIGNAL.match(match.group("value")))


def _first_nonempty_text(section: dict[str, Any]) -> str:
    rows = _nonempty_rows(section)
    return str(rows[0].get("text") or "").strip() if rows else ""


def _last_nonempty_text(section: dict[str, Any]) -> str:
    rows = _nonempty_rows(section)
    return str(rows[-1].get("text") or "").strip() if rows else ""


def _has_adjacent_opening_datetime_pair(
    section: dict[str, Any], next_section: dict[str, Any] | None,
) -> bool:
    """Recognize one immediate opening heading -> date section relationship."""
    if next_section is None:
        return False
    heading = _last_nonempty_text(section)
    value = _first_nonempty_text(next_section)
    if (
        not heading
        or not value
        or _DATE_TIME_SIGNAL.search(heading)
        or _has_opening_reference(heading)
    ):
        return False
    if _has_split_opening_heading(section) and _has_split_opening_time_row(next_section):
        return True
    return bool(
        _OPENING_TIME_LABEL_SIGNAL.search(heading)
        and _DATE_TIME_SIGNAL.search(value)
    )


def _inline_opening_datetime_value(section: dict[str, Any]) -> str | None:
    """Return the date row bound to an immediate opening heading, if any."""
    rows = _nonempty_rows(section)
    for heading_row, value_row in zip(rows, rows[1:]):
        heading = str(heading_row.get("text") or "").strip()
        value = str(value_row.get("text") or "").strip()
        if (
            not heading
            or not value
            or _DATE_TIME_SIGNAL.search(heading)
            or _has_opening_reference(heading)
            or not _OPENING_TIME_LABEL_SIGNAL.search(heading)
        ):
            continue
        if _INLINE_DATETIME_ROW_PATTERN.fullmatch(value):
            return value
    return None


def _has_inline_opening_datetime_pair(section: dict[str, Any]) -> bool:
    """Recognize an opening heading immediately followed by a date row."""
    return _inline_opening_datetime_value(section) is not None


def _inline_opening_datetime_text_value(text: str) -> str | None:
    rows = [
        {"text": line.strip()}
        for line in str(text or "").splitlines()
        if line.strip()
    ]
    return _inline_opening_datetime_value({"rows": rows})


def _strip_bounded_outline_prefix(value: Any) -> str:
    return re.sub(
        r"^\s*(?:(?:第\s*)?[一二三四五六七八九十百千万]+|\d+)\s*[、.．)]\s*",
        "",
        str(value or "").strip(),
        count=1,
    )


def _adjacent_purchaser_value(
    section: dict[str, Any], next_section: dict[str, Any] | None,
) -> str | None:
    """Return a bounded generic-name row immediately below a purchaser heading."""
    if next_section is None:
        return None
    heading = _last_nonempty_text(section)
    value = _first_nonempty_text(next_section)
    if not heading or not value:
        return None
    # Parser section headings may retain a short outline marker (for example
    # ``1.采购人信息``).  Remove only that leading marker; do not normalize the
    # body broadly enough to turn arbitrary prose into a role heading.
    heading = _strip_bounded_outline_prefix(heading)
    if re.fullmatch(rf"{_PURCHASER_LABEL}(?:信息)?", heading, re.IGNORECASE) is None:
        return None
    if _PURCHASER_LABELLED_VALUE.search(heading) or _AGENCY_LABELLED_VALUE.search(heading):
        return None
    # The adjacent row may carry only the generic ``名称:`` label.  Other
    # labelled rows (address, contact, agency, etc.) are intentionally not
    # treated as the purchaser value.
    match = re.fullmatch(r"名\s*称\s*[:：]\s*(?P<value>[^\r\n；;。]{1,256}?)\s*", value)
    if match is None:
        return None
    candidate = match.group("value").strip()
    if (
        not candidate
        or re.search(_PURCHASER_LABEL, candidate, re.IGNORECASE)
        or re.search(_AGENCY_LABEL, candidate, re.IGNORECASE)
    ):
        return None
    return candidate


def _has_adjacent_purchaser_value_pair(
    section: dict[str, Any], next_section: dict[str, Any] | None,
) -> bool:
    return _adjacent_purchaser_value(section, next_section) is not None


def _has_explicit_project_name_value(text: str) -> bool:
    return _PROJECT_NAME_LABELLED_VALUE.search(text) is not None


def _has_explicit_purchaser_value(text: str) -> bool:
    return _PURCHASER_LABELLED_VALUE.search(text) is not None


def _has_core_scope_heading(section: dict[str, Any]) -> bool:
    rows = _nonempty_rows(section)
    return bool(
        rows
        and _CORE_SCOPE_HEADING_PATTERN.fullmatch(
            str(rows[-1].get("text") or "").strip()
        )
    )


def _has_concrete_core_scope(section: dict[str, Any]) -> bool:
    content = "\n".join(
        str(row.get("text") or "").strip()
        for row in _nonempty_rows(section)
        if _CORE_SCOPE_HEADING_PATTERN.fullmatch(
            str(row.get("text") or "").strip()
        ) is None
    )
    return bool(
        content
        and _CORE_SCOPE_ACTION_SIGNAL.search(content)
        and _CORE_SCOPE_OBJECT_SIGNAL.search(content)
    )


def _is_core_scope_major_heading(section: dict[str, Any]) -> bool:
    rows = _nonempty_rows(section)
    if not rows or _has_core_scope_heading(section):
        return False
    return _CORE_SCOPE_MAJOR_HEADING_PATTERN.fullmatch(
        str(rows[0].get("text") or "").strip()
    ) is not None


def _section_text_bytes(section: dict[str, Any]) -> int:
    return sum(
        len(str(row.get("text") or "").encode("utf-8"))
        for row in _nonempty_rows(section)
    )


def _core_scope_context_indexes(
    sections: list[dict[str, Any]], index: int
) -> list[int]:
    if not _has_core_scope_heading(sections[index]):
        return [index]

    related = [index]
    concrete_index: int | None = None
    window_end = min(
        len(sections), index + CORE_SCOPE_FORWARD_SECTION_WINDOW + 1
    )
    for candidate_index in range(index + 1, window_end):
        candidate = sections[candidate_index]
        if _is_core_scope_major_heading(candidate):
            break
        if _has_concrete_core_scope(candidate):
            concrete_index = candidate_index
            break

    if concrete_index is None:
        return related

    used_bytes = _section_text_bytes(sections[index])
    for candidate_index in range(index + 1, concrete_index + 1):
        candidate = sections[candidate_index]
        candidate_bytes = _section_text_bytes(candidate)
        if used_bytes + candidate_bytes <= CORE_SCOPE_FORWARD_BYTE_BUDGET:
            related.append(candidate_index)
            used_bytes += candidate_bytes
        elif candidate_index == concrete_index:
            compact = [index, concrete_index]
            if (
                concrete_index != index
                and _section_text_bytes(sections[index]) + candidate_bytes
                <= CORE_SCOPE_FORWARD_BYTE_BUDGET
            ):
                return compact
            return [index]

    tail_end = min(
        len(sections), concrete_index + CORE_SCOPE_FORWARD_TAIL_SECTIONS + 1
    )
    for candidate_index in range(concrete_index + 1, tail_end):
        candidate = sections[candidate_index]
        if _is_core_scope_major_heading(candidate):
            break
        candidate_bytes = _section_text_bytes(candidate)
        if used_bytes + candidate_bytes > CORE_SCOPE_FORWARD_BYTE_BUDGET:
            break
        related.append(candidate_index)
        used_bytes += candidate_bytes
    return sorted(set(related))


def _evidence_context_indexes(
    sections: list[dict[str, Any]], index: int
) -> list[int]:
    return sorted({
        *(_opening_context_indexes(sections, index)),
        *(_purchaser_context_indexes(sections, index)),
        *(_generic_adjacent_context_indexes(sections, index)),
        *(_core_scope_context_indexes(sections, index)),
    })


@dataclass(frozen=True)
class DocumentEvidence:
    structured_dom: dict[str, Any]
    audit: dict[str, Any]


def _utf8_chunks(text: str, limit_bytes: int = SECTION_TARGET_BYTES) -> list[str]:
    """Split text on Unicode code-point boundaries within a UTF-8 byte cap."""
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > limit_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        chunks.append("".join(current))
    return chunks


def _section_rows(elements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = str(element.get("text") or "").strip()
        if not text:
            continue
        category = str(element.get("category") or "NarrativeText")
        pieces = _utf8_chunks(text)
        for piece_index, piece in enumerate(pieces):
            starts_section = category == "Title" and piece_index == 0 and current
            piece_bytes = len(piece.encode("utf-8"))
            if starts_section or (current and current_bytes + piece_bytes > SECTION_TARGET_BYTES):
                sections.append(_make_section(current, len(sections)))
                current = []
                current_bytes = 0
            current.append({**element, "text": piece})
            current_bytes += piece_bytes
    if current:
        sections.append(_make_section(current, len(sections)))
    return sections


def _make_section(elements: list[dict[str, Any]], index: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for element in elements:
        category = str(element.get("category") or "NarrativeText")
        text = str(element.get("text") or "").strip()
        key = (category, text)
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {"kind": category, "text": text}
        table_index = element.get("table_index")
        if isinstance(table_index, int):
            row["table_index"] = table_index
        page_number = element.get("page_number")
        if isinstance(page_number, int):
            row["page"] = page_number
        rows.append(row)
    joined = "\n".join(row["text"] for row in rows)
    return {
        "index": index,
        "rows": rows,
        "character_count": len(joined),
        "business_anchor_count": len(BUSINESS_ANCHORS.findall(joined)),
        "has_table": any("table_index" in row for row in rows),
    }


def _section_field_categories(
    section: dict[str, Any], index: int, section_count: int
) -> frozenset[str]:
    """Classify structural evidence without interpreting business values."""
    text = "\n".join(str(row.get("text") or "") for row in section["rows"])
    categories: set[str] = set()
    if index == 0 or any(
        str(row.get("kind") or "").casefold() in {"title", "header"}
        for row in section["rows"]
    ):
        categories.add("opening_identity")
    for category, pattern in FIELD_CATEGORY_PATTERNS.items():
        if pattern.search(text):
            categories.add(category)
    if _has_split_opening_heading(section):
        categories.add("bid_opening")
    # A document with no explicit title still has an opening boundary.  The
    # index is the only positional fact used here; no source-specific rule is
    # introduced.
    if index == 0 and section_count:
        categories.add("opening_identity")
    return frozenset(categories)


def _category_structure_rank(
    section: dict[str, Any], category: str, categories: frozenset[str]
) -> tuple[int, int, int, int]:
    """Prefer a direct category heading over multi-anchor boilerplate."""
    pattern = FIELD_CATEGORY_PATTERNS.get(category)
    direct_rows = [
        row for row in section["rows"]
        if pattern is not None and pattern.search(str(row.get("text") or ""))
    ]
    direct_heading = any(
        str(row.get("kind") or "").casefold() in {"title", "header"}
        and pattern is not None
        and pattern.search(str(row.get("text") or ""))
        for row in section["rows"]
    )
    non_boundary_categories = categories - {"opening_identity"}
    dedicated = (
        categories == frozenset({category})
        if category == "opening_identity"
        else non_boundary_categories == frozenset({category})
    )
    return (
        0 if direct_heading else 1,
        0 if dedicated else 1,
        -len(direct_rows),
        int(section["has_table"]),
    )


def _category_evidence_strength(
    section: dict[str, Any],
    category: str,
    *,
    split_next_section: dict[str, Any] | None = None,
    core_scope_context: list[int] | None = None,
) -> int:
    """Rank generic value-bearing evidence ahead of labels and references."""
    text = "\n".join(str(row.get("text") or "") for row in section["rows"])
    pattern = FIELD_CATEGORY_PATTERNS.get(category)
    if pattern is None:
        return 2
    if category == "opening_identity":
        if _has_explicit_project_name_value(text):
            return 0
        if any(
            str(row.get("kind") or "").casefold() in {"title", "header"}
            for row in section["rows"]
        ):
            return 1
        return 2
    if category == "customer":
        if _has_explicit_purchaser_value(text):
            return 0
        if _has_adjacent_purchaser_value_pair(section, split_next_section):
            return 0
        if _AGENCY_LABELLED_VALUE.search(text):
            return 2
        return 1
    if category == "bid_opening":
        if _has_inline_opening_datetime_pair(section):
            return 0
        if _has_adjacent_opening_datetime_pair(section, split_next_section):
            if split_next_section is not None:
                return 0
    if not pattern.search(text):
        return 2
    if category == "budget":
        if not _CURRENCY_SIGNAL.search(text):
            return 1
        if _BUDGET_OVERALL_SIGNAL.search(text):
            return 0
        if _BUDGET_SUBORDINATE_SIGNAL.search(text):
            return 1
        return 0 if re.search(r"预算", text) else 1
    if category == "bid_opening":
        if _has_explicit_opening_datetime(text):
            return 0
        if _has_opening_reference(text) or _OPENING_TIME_LABEL_SIGNAL.search(text):
            return 1
        return 2
    if category == "project_number":
        return 0 if _has_project_number_signal(text) else 1
    if category == "core_scope":
        if _has_concrete_core_scope(section):
            return 0
        if (
            _has_core_scope_heading(section)
            and core_scope_context is not None
            and len(core_scope_context) > 1
        ):
            return 0
        has_narrative = any(
            str(row.get("kind") or "").casefold() not in {"title", "header"}
            and len(str(row.get("text") or "").strip()) >= 40
            for row in section["rows"]
        )
        return 1 if has_narrative else 2
    if category == "award_result":
        return 0 if _CURRENCY_SIGNAL.search(text) else 1
    return 0 if any(
        str(row.get("kind") or "").casefold() in {"title", "header"}
        for row in section["rows"]
    ) else 1


def _encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _normalized_table_row(row: Any) -> list[str]:
    if not isinstance(row, (list, tuple)):
        return []
    try:
        values = row[:MAX_TABLE_COLUMNS]
    except Exception:
        return []
    cells: list[str] = []
    for cell in values:
        try:
            text = str(cell or "").strip()[:MAX_TABLE_CELL_CHARS]
        except Exception:
            text = ""
        cells.append(text)
    return cells if any(cells) else []


def _table_row_relevance(row: list[str]) -> int:
    """Score only generic structural/value signals; never expose this score."""
    text = " ".join(row)
    try:
        field_hits = sum(
            1 for pattern in FIELD_CATEGORY_PATTERNS.values()
            if pattern.search(text)
        )
        anchor_hits = min(len(BUSINESS_ANCHORS.findall(text)), 3)
        has_digit = any("0" <= character <= "9" for character in text)
        value_hits = (
            sum(
                1 for pattern in (
                    _CURRENCY_SIGNAL,
                    _DATE_TIME_SIGNAL,
                )
                if pattern.search(text)
            )
            if has_digit
            else 0
        )
        if has_digit and _has_project_number_signal(text):
            value_hits += 1
        action_object = bool(
            _CORE_SCOPE_ACTION_SIGNAL.search(text)
            and _CORE_SCOPE_OBJECT_SIGNAL.search(text)
        )
        return field_hits * 2 + anchor_hits + value_hits * 2 + (2 if action_object else 0)
    except Exception:
        return 0


def _bounded_table_rows(
    table_index: int,
    source_table: Any,
    existing_tables: list[dict[str, Any]],
) -> list[list[str]] | None:
    """Keep a header and signal-bearing rows, then fill in source order."""
    if not isinstance(source_table, (list, tuple)):
        return None
    normalized: list[list[str]] = []
    for source_row in source_table:
        row = _normalized_table_row(source_row)
        if row:
            normalized.append(row)
    if not normalized:
        return None

    existing_json = json.dumps(
        existing_tables,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    existing_inner_bytes = len(existing_json) - 2
    table_prefix_bytes = len(b'{"tables":[')
    table_suffix_bytes = len(b']}')

    def fits(rows: list[list[str]]) -> bool:
        candidate = {"table_index": table_index, "rows": rows}
        try:
            candidate_bytes = len(
                json.dumps(
                    candidate,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            separator_bytes = 1 if existing_tables else 0
            return (
                table_prefix_bytes
                + existing_inner_bytes
                + separator_bytes
                + candidate_bytes
                + table_suffix_bytes
                <= TABLE_PAYLOAD_BUDGET_BYTES
            )
        except Exception:
            return False

    selected_indexes: set[int] = {0}
    if not fits([normalized[0]]):
        return None
    scored = sorted(
        (
            (_table_row_relevance(row), index)
            for index, row in enumerate(normalized)
            if index
        ),
        key=lambda item: (-item[0], item[1]),
    )
    has_signal = any(score > 0 for score, _index in scored)
    candidates = (
        [index for score, index in scored if score > 0]
        if has_signal
        else list(range(1, len(normalized)))
    )
    for index in candidates:
        trial_indexes = sorted((*selected_indexes, index))
        if fits([normalized[item] for item in trial_indexes]):
            selected_indexes.add(index)
    if has_signal:
        for index in range(1, len(normalized)):
            if index in selected_indexes:
                continue
            trial_indexes = sorted((*selected_indexes, index))
            if fits([normalized[item] for item in trial_indexes]):
                selected_indexes.add(index)
    return [normalized[index] for index in sorted(selected_indexes)]


def _payload_for_selection(
    base: dict[str, Any],
    sections: list[dict[str, Any]],
    selected_indexes: list[int],
    source_tables: list[Any],
    limit_bytes: int,
) -> dict[str, Any] | None:
    selected_sections = [sections[index] for index in selected_indexes]
    table_indexes = sorted({
        int(row["table_index"])
        for section in selected_sections
        for row in section["rows"]
        if isinstance(row.get("table_index"), int)
    })
    payload = {**base, "sections": selected_sections, "tables": []}
    for table_index in table_indexes:
        if table_index < 0 or table_index >= len(source_tables):
            return None
        rows = _bounded_table_rows(
            table_index,
            source_tables[table_index],
            payload["tables"],
        )
        if rows is None:
            return None
        payload["tables"].append({"table_index": table_index, "rows": rows})
        if _encoded_size({"tables": payload["tables"]}) > TABLE_PAYLOAD_BUDGET_BYTES:
            payload["tables"].pop()
            return None
    return payload if _encoded_size(payload) <= limit_bytes else None


def _opening_context_indexes(
    sections: list[dict[str, Any]], index: int
) -> list[int]:
    """Keep only bounded opening-reference or split-heading context."""
    related = {index}
    text = "\n".join(str(row.get("text") or "") for row in sections[index]["rows"])
    if _has_opening_reference(text):
        neighbor = index - 1
        if neighbor >= 0:
            neighbor_text = "\n".join(
                str(row.get("text") or "") for row in sections[neighbor]["rows"]
            )
            if (
                _has_deadline_datetime(neighbor_text)
                or _has_explicit_opening_datetime(neighbor_text)
            ):
                related.add(neighbor)
    if (
        (
            _has_split_opening_heading(sections[index])
            or (
                index + 1 < len(sections)
                and _has_adjacent_opening_datetime_pair(
                    sections[index], sections[index + 1]
                )
            )
        )
        and index + 1 < len(sections)
        and (
            _has_split_opening_time_row(sections[index + 1])
            or _has_adjacent_opening_datetime_pair(
                sections[index], sections[index + 1]
            )
        )
    ):
        related.add(index + 1)
    return sorted(related)


def _purchaser_context_indexes(
    sections: list[dict[str, Any]], index: int
) -> list[int]:
    related = {index}
    if index + 1 < len(sections) and _has_adjacent_purchaser_value_pair(
        sections[index], sections[index + 1]
    ):
        related.add(index + 1)
    return sorted(related)


def _generic_adjacent_context_indexes(
    sections: list[dict[str, Any]], index: int
) -> list[int]:
    related = {index}
    if index + 1 >= len(sections):
        return sorted(related)
    rows = list(sections[index].get("rows") or [])
    next_rows = list(sections[index + 1].get("rows") or [])
    for field in _ADJACENT_FIELD_LABEL_PATTERNS:
        if _adjacent_field_value_rows(rows + next_rows, field):
            related.add(index + 1)
    return sorted(related)


def build_document_evidence(document: Any) -> DocumentEvidence:
    """Select complete ordered sections within the fixed document byte budget."""
    started = perf_counter()
    sections = _section_rows(getattr(document, "elements", []) or [])
    if not sections:
        raise ValueError("document_evidence_empty")

    section_categories = {
        index: _section_field_categories(section, index, len(sections))
        for index, section in enumerate(sections)
    }

    # Keep boundary context and all table/business-bearing sections available as
    # tie breakers, but reserve uncovered structural categories first.  This is
    # still a local selection decision; final business values remain semantic.
    def selection_rank(index: int) -> tuple[int, int, int, int]:
        section = sections[index]
        has_table = bool(section["has_table"])
        anchor_count = int(section["business_anchor_count"])
        if has_table or anchor_count:
            tier = 0
        elif index in {0, len(sections) - 1}:
            tier = 1
        else:
            tier = 2
        return tier, -int(has_table), -anchor_count, index

    ranked = sorted(range(len(sections)), key=selection_rank)
    selected: set[int] = set()
    source_tables = list(getattr(document, "tables", []) or [])
    base = {
        "schema_version": "document-evidence/v1",
        "detected_type": str((getattr(document, "parser_audit", {}) or {}).get("detected_type") or ""),
        "sections": [],
        "tables": [],
    }

    strength_cache: dict[tuple[int, str], int] = {}

    def strength(index: int, category: str) -> int:
        cache_key = (index, category)
        if cache_key in strength_cache:
            return strength_cache[cache_key]
        next_section = sections[index + 1] if index + 1 < len(sections) else None
        value = _category_evidence_strength(
            sections[index],
            category,
            split_next_section=next_section,
            core_scope_context=_core_scope_context_indexes(sections, index),
        )
        strength_cache[cache_key] = value
        return value

    payload_cache: dict[tuple[tuple[int, ...], int], dict[str, Any] | None] = {}

    def payload_for(indexes: Iterable[int], limit_bytes: int) -> dict[str, Any] | None:
        normalized_indexes = tuple(sorted(set(indexes)))
        cache_key = (normalized_indexes, limit_bytes)
        if cache_key not in payload_cache:
            payload_cache[cache_key] = _payload_for_selection(
                base,
                sections,
                list(normalized_indexes),
                source_tables,
                limit_bytes,
            )
        return payload_cache[cache_key]

    def strong_categories(index: int) -> set[str]:
        return {
            category
            for category in section_categories[index]
            if strength(index, category) == 0
        }

    reservation_candidate_attempt_count = 0

    def try_add(
        index: int,
        *,
        category: str | None = None,
        limit_bytes: int = TARGET_DOCUMENT_EVIDENCE_BYTES,
        allow_replace: bool = False,
    ) -> bool:
        if index in selected:
            return False
        related = set(_evidence_context_indexes(sections, index))
        new_indexes = related - selected
        if not new_indexes:
            return False
        candidate_indexes = sorted((*selected, *related))
        candidate = payload_for(candidate_indexes, limit_bytes)
        if candidate is None and allow_replace and category is not None:
            candidate_strength = strength(index, category)
            removable = [
                selected_index
                for selected_index in sorted(selected)
                if selected_index != 0
                and category in section_categories[selected_index]
                and strength(selected_index, category) > candidate_strength
                and not (strong_categories(selected_index) - {"opening_identity", category})
            ]
            for remove_index in removable:
                trial_indexes = sorted(
                    (selected - {remove_index}) | related
                )
                candidate = payload_for(trial_indexes, limit_bytes)
                if candidate is not None:
                    selected.remove(remove_index)
                    selected.update(related)
                    return True
        if candidate is None:
            return False
        selected.update(related)
        return True

    # The opening boundary is a first-class reservation.  This keeps title /
    # identity context ahead of interior boilerplate even when the latter has
    # a larger raw anchor count.
    try_add(0)

    # Each reservation pass prefers a small, deterministic set of sections that
    # covers the greatest number of still-uncovered categories, then falls back
    # to the original table / anchor / document-order ranking.  Every stable
    # candidate is attempted; no arbitrary prefix can hide a later strong
    # section from the reservation.
    for category in FIELD_CATEGORY_ORDER:
        if any(
            category in section_categories[index]
            and strength(index, category) == 0
            for index in selected
        ):
            continue
        covered = {
            covered_category
            for selected_index in selected
            for covered_category in section_categories[selected_index]
            if strength(selected_index, covered_category) == 0
        }
        candidates = [
            index for index in ranked
            if index not in selected and category in section_categories[index]
        ]
        candidates.sort(
            key=lambda index: (
                strength(index, category),
                _category_structure_rank(
                    sections[index], category, section_categories[index]
                ),
                -len({
                    candidate_category
                    for candidate_category in section_categories[index]
                    if strength(index, candidate_category) == 0
                } - covered),
                selection_rank(index),
                int(sections[index]["character_count"]),
            )
        )
        for index in candidates:
            reservation_candidate_attempt_count += 1
            if try_add(index, category=category):
                break

    for index in ranked:
        try_add(index)

    payload = payload_for(selected, TARGET_DOCUMENT_EVIDENCE_BYTES)
    available_field_categories = [
        category
        for category in FIELD_CATEGORY_ORDER
        if any(
            category in section_categories[index] and strength(index, category) == 0
            for index in range(len(sections))
        )
    ]

    def selected_categories() -> list[str]:
        return [
            category
            for category in FIELD_CATEGORY_ORDER
            if any(
                category in section_categories[index] and strength(index, category) == 0
                for index in selected
            )
        ]

    missing_field_categories = [
        category
        for category in available_field_categories
        if category not in selected_categories()
    ]
    category_rescue_used = False
    if missing_field_categories:
        for category in missing_field_categories:
            candidates = [
                index
                for index in ranked
                if index not in selected
                and category in section_categories[index]
                and strength(index, category) == 0
            ]
            candidates.sort(
                key=lambda index: (
                    _category_structure_rank(
                        sections[index], category, section_categories[index]
                    ),
                    selection_rank(index),
                    int(sections[index]["character_count"]),
                )
            )
            for index in candidates:
                reservation_candidate_attempt_count += 1
                if try_add(
                    index,
                    category=category,
                    limit_bytes=MAX_DOCUMENT_EVIDENCE_BYTES,
                    allow_replace=True,
                ):
                    category_rescue_used = True
                    break
        missing_field_categories = [
            category
            for category in available_field_categories
            if category not in selected_categories()
        ]
        if missing_field_categories:
            raise ValueError("document_evidence_category_budget_exhausted")

        payload = payload_for(selected, MAX_DOCUMENT_EVIDENCE_BYTES)
    if payload is None:
        raise ValueError("document_evidence_budget_exhausted")
    payload_bytes = _encoded_size(payload)
    if not selected or payload_bytes > MAX_DOCUMENT_EVIDENCE_BYTES:
        raise ValueError("document_evidence_budget_exhausted")
    selected_field_categories = selected_categories()
    missing_field_categories = [
        category
        for category in available_field_categories
        if category not in selected_field_categories
    ]
    if missing_field_categories:
        raise ValueError("document_evidence_category_budget_exhausted")
    source_characters = sum(int(section["character_count"]) for section in sections)
    selected_characters = sum(int(sections[index]["character_count"]) for index in selected)
    return DocumentEvidence(
        structured_dom=payload,
        audit={
            "schema_version": "document-evidence-audit/v1",
            "source_characters": source_characters,
            "selected_characters": selected_characters,
            "payload_bytes": payload_bytes,
            "payload_limit_bytes": MAX_DOCUMENT_EVIDENCE_BYTES,
            "payload_target_bytes": TARGET_DOCUMENT_EVIDENCE_BYTES,
            "source_section_count": len(sections),
            "selected_section_count": len(selected),
            "selected_table_count": len(payload["tables"]),
            "selected_table_row_count": sum(len(table["rows"]) for table in payload["tables"]),
            "available_field_categories": available_field_categories,
            "selected_field_categories": selected_field_categories,
            "missing_field_categories": missing_field_categories,
            "category_rescue_used": category_rescue_used,
            "target_budget_exceeded": payload_bytes > TARGET_DOCUMENT_EVIDENCE_BYTES,
            "reservation_candidate_attempt_count": reservation_candidate_attempt_count,
            "reduction_ratio": round(
                selected_characters / source_characters, 6
            ) if source_characters else 0.0,
            "elapsed_chunking_ms": round((perf_counter() - started) * 1000, 3),
        },
    )


def _is_project_number_template(value: Any) -> bool:
    """Recognize obvious template segments without rejecting normal IDs."""
    normalized = re.sub(r"\s+", "", str(value or "")).upper()
    if not normalized:
        return False
    segments = [segment for segment in re.split(r"[-_/\\]+", normalized) if segment]
    if any(re.fullmatch(r"X{2,}", segment) for segment in segments):
        return True
    if re.search(r"20(?:XX|[0-9]X)", normalized):
        return True
    if re.search(r"\d{2,}X{2,}", normalized):
        return True
    return False


def reject_placeholder_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Reject generic form labels without guessing replacement business values."""
    invalid_exact = {"采购人全称", "中标人全称", "代表）", "代表)"}
    cleaned = dict(fields)
    for name in ("project_name", "customer", "winner"):
        value = str(cleaned.get(name) or "").strip()
        normalized = re.sub(r"\s+", "", value)
        if normalized in invalid_exact or re.match(r"^名称[：:]", normalized):
            cleaned[name] = ""
    if _is_project_number_template(cleaned.get("project_number")):
        cleaned["project_number"] = ""
    return cleaned


def _explicit_section_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _section_text_by_explicit_index(structured_dom: Any) -> dict[int, str]:
    if not isinstance(structured_dom, Mapping):
        return {}
    sections = structured_dom.get("sections")
    if not isinstance(sections, list):
        return {}
    indexed: dict[int, str] = {}
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        index = _explicit_section_index(section.get("index"))
        if index is None:
            continue
        rows = section.get("rows")
        if not isinstance(rows, list):
            continue
        indexed[index] = "\n".join(
            str(row.get("text") or "")
            for row in rows
            if isinstance(row, Mapping)
        )
    return indexed


def _section_rows_by_explicit_index(structured_dom: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(structured_dom, Mapping):
        return {}
    raw_sections = structured_dom.get("sections")
    if not isinstance(raw_sections, list):
        return {}
    indexed: dict[int, dict[str, Any]] = {}
    for section in raw_sections:
        if not isinstance(section, Mapping):
            continue
        index = _explicit_section_index(section.get("index"))
        rows = section.get("rows")
        if index is None or not isinstance(rows, list):
            continue
        indexed[index] = {
            "rows": [row for row in rows if isinstance(row, Mapping)]
        }
    return indexed


def _labelled_values(text: str, pattern: re.Pattern[str]) -> list[str]:
    return [
        match.group("value").strip()
        for match in pattern.finditer(text)
        if match.group("value").strip()
    ]


_EVIDENCE_PUNCTUATION_TRANSLATION = str.maketrans({
    "：": ":", "，": ",", "；": ";", "。": ".", "（": "(", "）": ")",
    "【": "[", "】": "]", "、": ",", "－": "-", "—": "-", "–": "-",
})


def _normalized_evidence_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_EVIDENCE_PUNCTUATION_TRANSLATION)
    normalized = re.sub(r"\s+", "", normalized)
    return re.sub(r"[:：,，;；。.!！？?、]+", "", normalized)


def _comparison_text(value: Any) -> str:
    return _normalized_evidence_text(value).strip(":")


_ADJACENT_FIELD_LABEL_PATTERNS = {
    "project_name": re.compile(
        rf"^\s*{_PROJECT_NAME_LABEL}(?:信息)?\s*[:：]?\s*$", re.IGNORECASE
    ),
    "customer": re.compile(
        rf"^\s*{_PURCHASER_LABEL}(?:信息)?\s*[:：]?\s*$", re.IGNORECASE
    ),
    "bid_open_time": re.compile(
        r"^\s*(?:响应文件开启|响应文件开标|投标文件启封|投标文件开封|"
        r"投标文件开启|开标|开封|开启)(?:\s*(?:时间|时间和地点))?\s*[:：]?\s*$",
        re.IGNORECASE,
    ),
    "content": re.compile(
        r"^\s*(?:采购项目内容|采购内容|采购需求|项目概况|服务范围|主要内容|"
        r"建设内容|工作内容|交付物|技术要求|技术需求|标的)\s*[:：]?\s*$",
        re.IGNORECASE,
    ),
}
_ADJACENT_VALUE_PREFIXES = {
    "project_name": re.compile(r"^\s*名\s*称\s*[:：]\s*", re.IGNORECASE),
    "customer": re.compile(r"^\s*名\s*称\s*[:：]\s*", re.IGNORECASE),
    "bid_open_time": re.compile(r"^\s*(?:开标|开启)?\s*时间\s*[:：]\s*", re.IGNORECASE),
}


def _is_adjacent_label_only(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        not text
        or any(pattern.fullmatch(text) for pattern in _ADJACENT_FIELD_LABEL_PATTERNS.values())
        or re.fullmatch(r"(?:名\s*称|时\s*间)\s*[:：]?", text)
    )


def _adjacent_field_value_rows(
    rows: list[Mapping[str, Any]],
    field: str,
) -> list[tuple[str, str]]:
    """Return only immediate label-to-value row relationships."""
    label_pattern = _ADJACENT_FIELD_LABEL_PATTERNS.get(field)
    if label_pattern is None:
        return []
    values: list[tuple[str, str]] = []
    for index, raw_row in enumerate(rows[:-1]):
        raw_label = str(raw_row.get("text") or "").strip()
        label = _strip_bounded_outline_prefix(raw_label)
        if label_pattern.fullmatch(label) is None:
            continue
        raw_value = str(rows[index + 1].get("text") or "").strip()
        if not raw_value:
            continue
        prefix = _ADJACENT_VALUE_PREFIXES.get(field)
        candidate = prefix.sub("", raw_value, count=1).strip() if prefix else raw_value
        if _is_adjacent_label_only(candidate):
            continue
        if field == "bid_open_time" and _DATE_TIME_SIGNAL.search(candidate) is None:
            continue
        if field in {"project_name", "customer"} and _DATE_TIME_SIGNAL.search(candidate):
            continue
        values.append((candidate, f"{raw_label}\n{raw_value}"))
    return values


def _table_adjacent_field_values(
    structured_dom: Mapping[str, Any] | None,
    section_rows: list[Mapping[str, Any]],
    field: str,
) -> list[tuple[str, str]]:
    """Return values from a label cell and its immediate right-hand cell."""
    if not isinstance(structured_dom, Mapping):
        return []
    table_indexes = {
        int(row["table_index"])
        for row in section_rows
        if isinstance(row.get("table_index"), int)
    }
    if not table_indexes:
        return []
    label_pattern = _ADJACENT_FIELD_LABEL_PATTERNS.get(field)
    if label_pattern is None:
        return []
    results: list[tuple[str, str]] = []
    for table in structured_dom.get("tables") or []:
        if not isinstance(table, Mapping):
            continue
        table_index = _explicit_section_index(table.get("table_index"))
        if table_index not in table_indexes or not isinstance(table.get("rows"), list):
            continue
        for raw_row in table["rows"]:
            cells = _normalized_table_row(raw_row)
            for index, raw_cell in enumerate(cells[:-1]):
                label = str(raw_cell or "").strip()
                if label_pattern.fullmatch(label) is None:
                    continue
                candidate = str(cells[index + 1] or "").strip()
                prefix = _ADJACENT_VALUE_PREFIXES.get(field)
                if prefix:
                    candidate = prefix.sub("", candidate, count=1).strip()
                if _is_adjacent_label_only(candidate):
                    continue
                if field == "bid_open_time" and _DATE_TIME_SIGNAL.search(candidate) is None:
                    continue
                results.append((candidate, f"{label}\t{cells[index + 1]}"))
    return results


def _adjacent_field_evidence(
    section_rows: Mapping[int, Mapping[str, Any]],
    section_index: int,
    field: str,
    structured_dom: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    rows = list(section_rows.get(section_index, {}).get("rows") or [])
    next_rows = list(section_rows.get(section_index + 1, {}).get("rows") or [])
    related = _adjacent_field_value_rows(rows + next_rows, field)
    related.extend(_table_adjacent_field_values(structured_dom, rows, field))
    return related


REPAIR_EVIDENCE_SCHEMA_VERSION = "document-critical-repair-evidence/v1"
REPAIR_EVIDENCE_LIMIT_BYTES = 8_192
_REPAIR_TARGET_FIELDS = ("project_name", "customer", "bid_open_time", "content")
_BAD_REPAIR_CITATION_CODES = frozenset({
    "field_evidence_value_not_in_quote",
    "field_evidence_quote_not_in_section",
    "field_evidence_quote_missing",
    "field_evidence_quote_too_long",
    "field_evidence_role_label_missing",
    "field_evidence_section_not_found",
    "project_name_truncated",
    "customer_agency_role_mismatch",
    "customer_role_conflict",
    "customer_role_unverified",
    "bid_opening_label_missing",
    "bid_opening_deadline_only",
    "bid_opening_reference_unresolved",
    "bid_opening_ambiguous",
    "bid_opening_inline_value_mismatch",
    "content_scope_insufficient",
})


def build_repair_evidence(
    structured_dom: Mapping[str, Any] | None,
    target_fields: Iterable[str],
    integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a small, ordered evidence view for one bounded repair attempt."""
    targets = [
        field for field in _REPAIR_TARGET_FIELDS
        if field in {str(item) for item in target_fields}
    ]
    raw_sections = (
        structured_dom.get("sections")
        if isinstance(structured_dom, Mapping)
        else None
    )
    source_count = len(raw_sections) if isinstance(raw_sections, list) else 0
    base_audit: dict[str, Any] = {
        "schema_version": REPAIR_EVIDENCE_SCHEMA_VERSION,
        "target_fields": targets,
        "source_section_count": source_count,
        "selected_section_count": 0,
        "selected_table_count": 0,
        "payload_bytes": 0,
        "payload_limit_bytes": REPAIR_EVIDENCE_LIMIT_BYTES,
        "status": "skipped",
        "skip_reason": "",
        "excluded_cited_section_indexes": [],
    }

    def skipped(reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        bounded_payload = payload or {
            "schema_version": REPAIR_EVIDENCE_SCHEMA_VERSION,
            "target_fields": targets,
            "sections": [],
            "tables": [],
        }
        base_audit["payload_bytes"] = _encoded_size(bounded_payload)
        base_audit["skip_reason"] = reason
        return {"payload": bounded_payload, "audit": dict(base_audit)}

    if not targets:
        return skipped("no_target_fields")
    if not isinstance(raw_sections, list):
        return skipped("invalid_structured_dom")

    referenced_indexes: set[int] = set()
    bad_cited_indexes_by_field: dict[str, set[int]] = {}
    raw_issues = integrity.get("quality_issues") if isinstance(integrity, Mapping) else []
    for issue in raw_issues or []:
        if not isinstance(issue, Mapping):
            continue
        index = _explicit_section_index(issue.get("section_index"))
        if index is not None:
            referenced_indexes.add(index)
            if str(issue.get("code") or "") in _BAD_REPAIR_CITATION_CODES:
                field = str(issue.get("field") or "")
                if field:
                    bad_cited_indexes_by_field.setdefault(field, set()).add(index)
    bad_cited_indexes = {
        index
        for indexes in bad_cited_indexes_by_field.values()
        for index in indexes
    }
    base_audit["excluded_cited_section_indexes"] = sorted(bad_cited_indexes)

    nearby_indexes = set(referenced_indexes)
    for index in tuple(referenced_indexes):
        if index > 0:
            nearby_indexes.add(index - 1)
        nearby_indexes.add(index + 1)

    sections: list[dict[str, Any]] = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, Mapping):
            continue
        index = _explicit_section_index(raw_section.get("index"))
        rows = raw_section.get("rows")
        if index is None or not isinstance(rows, list):
            continue
        normalized_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                continue
            text = str(raw_row.get("text") or "")
            if not text:
                continue
            row: dict[str, Any] = {
                "kind": str(raw_row.get("kind") or "NarrativeText"),
                "text": text,
            }
            for key in ("table_index", "page"):
                value = raw_row.get(key)
                if isinstance(value, int):
                    row[key] = value
            normalized_rows.append(row)
        if not normalized_rows:
            continue
        section = {"index": index, "rows": normalized_rows}
        categories = _section_field_categories(
            {"rows": normalized_rows}, index, source_count
        )
        text = "\n".join(row["text"] for row in normalized_rows)
        sections.append({
            "index": index,
            "section": section,
            "categories": categories,
            "explicit_project_name": _has_explicit_project_name_value(text),
            "explicit_customer": _has_explicit_purchaser_value(text),
            "explicit_bid_opening": _has_explicit_opening_datetime(text),
            "explicit_content": _has_concrete_core_scope({"rows": normalized_rows}),
        })

    candidates = [
        entry for entry in sections
        if entry["index"] in nearby_indexes
        or bool(entry["categories"] & {
            "opening_identity", "customer", "bid_opening", "core_scope",
        })
        or entry["explicit_project_name"]
        or entry["explicit_customer"]
        or entry["explicit_bid_opening"]
        or entry["explicit_content"]
    ]
    if not candidates:
        return skipped("no_relevant_evidence")

    raw_tables = structured_dom.get("tables") or []
    if not isinstance(raw_tables, list):
        return skipped("invalid_table_payload")

    def payload_for(indexes: Iterable[int]) -> dict[str, Any] | None:
        selected_entries = sorted(
            (entry for entry in sections if entry["index"] in set(indexes)),
            key=lambda entry: entry["index"],
        )
        selected_sections = [entry["section"] for entry in selected_entries]
        table_indexes = sorted({
            int(row["table_index"])
            for section in selected_sections
            for row in section["rows"]
            if isinstance(row.get("table_index"), int)
        })
        tables: list[dict[str, Any]] = []
        for table_index in table_indexes:
            matching = next(
                (
                    table for table in raw_tables
                    if isinstance(table, Mapping)
                    and _explicit_section_index(table.get("table_index")) == table_index
                ),
                None,
            )
            if matching is None:
                return None
            rows = matching.get("rows")
            if not isinstance(rows, list):
                return None
            bounded_rows = [
                normalized
                for raw_row in rows
                if (normalized := _normalized_table_row(raw_row))
            ]
            tables.append({"table_index": table_index, "rows": bounded_rows})
        return {
            "schema_version": REPAIR_EVIDENCE_SCHEMA_VERSION,
            "target_fields": targets,
            "sections": selected_sections,
            "tables": tables,
        }

    def fits(indexes: Iterable[int]) -> dict[str, Any] | None:
        payload = payload_for(indexes)
        return payload if payload is not None and _encoded_size(payload) <= REPAIR_EVIDENCE_LIMIT_BYTES else None

    def labelled_value_length(entry: dict[str, Any], field: str) -> int:
        text = "\n".join(
            str(row.get("text") or "")
            for row in entry["section"]["rows"]
        )
        if field == "project_name":
            pattern = _PROJECT_NAME_LABELLED_VALUE
        elif field == "customer":
            pattern = _PURCHASER_LABELLED_VALUE
        else:
            return min(len(text), 512)
        return max((len(value) for value in _labelled_values(text, pattern)), default=0)

    def explicit_for_field(entry: dict[str, Any], field: str) -> bool:
        if field == "project_name":
            return bool(entry["explicit_project_name"])
        if field == "customer":
            return bool(entry["explicit_customer"])
        if field == "bid_open_time":
            return bool(
                entry["explicit_bid_opening"]
                or "bid_opening" in entry["categories"]
            )
        return bool(
            entry["explicit_content"]
            or "core_scope" in entry["categories"]
        )

    def candidate_key(entry: dict[str, Any], field: str) -> tuple[int, int, int, int, int]:
        explicit = explicit_for_field(entry, field)
        index = int(entry["index"])
        return (
            0 if explicit else 1,
            0 if index in referenced_indexes else (1 if index in nearby_indexes else 2),
            -labelled_value_length(entry, field),
            0 if field in entry["categories"] else 1,
            index,
        )

    def field_candidates(field: str) -> list[dict[str, Any]]:
        bad_indexes = bad_cited_indexes_by_field.get(field, set())
        available = [
            entry for entry in candidates
            if entry["index"] not in bad_indexes
        ]
        category_map = {
            "project_name": {"opening_identity"},
            "customer": {"customer", "opening_identity"},
            "bid_open_time": {"bid_opening"},
            "content": {"core_scope"},
        }
        def related(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            explicit = [
                entry for entry in entries
                if explicit_for_field(entry, field)
            ]
            if explicit:
                return explicit
            return [
                entry for entry in entries
                if entry["categories"] & category_map.get(field, set())
            ]

        alternatives = related(available)
        if alternatives:
            return alternatives
        return [
            entry for entry in candidates
            if entry["index"] in bad_indexes
            and explicit_for_field(entry, field)
        ]

    target_candidate_indexes = sorted({
        int(entry["index"])
        for field in targets
        for entry in field_candidates(field)
    })
    selected_indexes: list[int] = []
    selected_target_fields: set[str] = set()
    for field in targets:
        ordered_candidates = sorted(
            field_candidates(field),
            key=lambda entry: candidate_key(entry, field),
        )
        for entry in ordered_candidates:
            index = int(entry["index"])
            trial = [*selected_indexes]
            if index not in trial:
                trial.append(index)
            if fits(trial) is not None:
                selected_indexes = sorted(set(trial))
                selected_target_fields.add(field)
                break

    for index in sorted(referenced_indexes):
        if index in bad_cited_indexes:
            continue
        if index not in {int(value) for value in selected_indexes}:
            trial = [*selected_indexes, index]
            if fits(trial) is not None:
                selected_indexes = sorted(set(trial))

    if not selected_indexes:
        oversized: list[tuple[int, int]] = []
        for index in target_candidate_indexes:
            candidate_payload = payload_for([index])
            if candidate_payload is None:
                continue
            candidate_size = _encoded_size(candidate_payload)
            if candidate_size > REPAIR_EVIDENCE_LIMIT_BYTES:
                oversized.append((candidate_size, index))
        if oversized:
            selected_indexes = [min(oversized)[1]]
        else:
            return skipped("no_unreferenced_target_evidence")

    payload = payload_for(selected_indexes)
    if payload is None:
        return skipped("referenced_table_not_found")
    payload_bytes = _encoded_size(payload)
    base_audit.update(
        selected_section_count=len(payload["sections"]),
        selected_table_count=len(payload["tables"]),
        payload_bytes=payload_bytes,
        selected_target_fields=sorted(selected_target_fields),
        selection_strategy="field_directed_minimal",
    )
    if payload_bytes > REPAIR_EVIDENCE_LIMIT_BYTES:
        base_audit["skip_reason"] = "payload_over_limit"
        return {"payload": payload, "audit": dict(base_audit)}
    base_audit.update(status="available", skip_reason="")
    return {"payload": payload, "audit": dict(base_audit)}


def assess_document_field_integrity(
    fields: Mapping[str, Any] | None,
    field_evidence: Mapping[str, Any] | None,
    structured_dom: Mapping[str, Any] | None,
    *,
    critical_fields: Iterable[str] = ("project_name", "customer"),
) -> dict[str, Any]:
    """Audit selected field references without changing business values.

    The default preserves the legacy two-field contract.  Newer prompt
    contracts pass their explicit critical-field tuple so adding fields does
    not silently change callers that still use the v3/v4 shape.
    """
    values = fields if isinstance(fields, Mapping) else {}
    references = field_evidence if isinstance(field_evidence, Mapping) else {}
    sections = _section_text_by_explicit_index(structured_dom)
    section_rows = _section_rows_by_explicit_index(structured_dom)
    issues: list[dict[str, Any]] = []
    suspect_fields: set[str] = set()

    def add_issue(code: str, field: str, section_index: int | None = None) -> None:
        issue: dict[str, Any] = {"code": code, "field": field}
        if section_index is not None:
            issue["section_index"] = section_index
        key = (issue["code"], issue["field"], issue.get("section_index"))
        if any(
            (row.get("code"), row.get("field"), row.get("section_index")) == key
            for row in issues
        ):
            return
        if len(issues) < MAX_FIELD_INTEGRITY_ISSUES:
            issues.append(issue)
        suspect_fields.add(field)

    def reference_indexes(reference: Mapping[str, Any]) -> list[int]:
        raw_indices = reference.get("section_indices")
        values: list[int] = []
        if isinstance(raw_indices, list):
            for raw_index in raw_indices[:4]:
                index = _explicit_section_index(raw_index)
                if index is not None and index not in values:
                    values.append(index)
        if not values:
            index = _explicit_section_index(reference.get("section_index"))
            if index is not None:
                values.append(index)
        return sorted(values)

    def adjacent_evidence(indices: list[int], field: str) -> list[tuple[str, str]]:
        evidence: list[tuple[str, str]] = []
        for index in indices:
            evidence.extend(_adjacent_field_evidence(section_rows, index, field, structured_dom))
        return evidence

    def bounded_opening_reference(indices: list[int]) -> bool:
        if adjacent_evidence(indices, "bid_open_time"):
            return True
        index_set = set(indices)
        for index in indices:
            if not _has_opening_reference(sections.get(index, "")):
                continue
            previous = index - 1
            if previous in index_set and (
                _has_deadline_datetime(sections.get(previous, ""))
                or _has_explicit_opening_datetime(sections.get(previous, ""))
            ):
                return True
        return False

    def split_opening_reference(indices: list[int]) -> bool:
        index_set = set(indices)
        return any(
            (
                _has_split_opening_heading(section_rows[index])
                or _has_adjacent_opening_datetime_pair(
                    section_rows[index], section_rows.get(index + 1)
                )
            )
            and index + 1 in index_set
            and (
                _has_split_opening_time_row(section_rows[index + 1])
                or _has_adjacent_opening_datetime_pair(
                    section_rows[index], section_rows.get(index + 1)
                )
            )
            for index in indices
            if index in section_rows and index + 1 in section_rows
        )

    def adjacent_purchaser_reference(indices: list[int]) -> str | None:
        for value, _quote in adjacent_evidence(indices, "customer"):
            if value:
                return value
        return None

    assessed = tuple(
        field
        for field in critical_fields
        if field in {"project_name", "customer", "bid_open_time", "content"}
    )
    for field in assessed:
        value = str(values.get(field) or "").strip()
        if not value:
            continue
        reference = references.get(field)
        if not isinstance(reference, Mapping):
            add_issue("field_evidence_missing", field)
            continue
        raw_index = reference.get("section_index")
        section_indices = reference_indexes(reference)
        section_index = section_indices[0] if section_indices else None
        if not section_indices:
            add_issue("field_evidence_invalid_section_index", field)
            continue
        missing_indices = [index for index in section_indices if index not in sections]
        if missing_indices:
            add_issue("field_evidence_section_not_found", field, section_index)
            continue
        section_text = "\n".join(sections[index] for index in section_indices)
        quote = str(reference.get("quote") or "").strip()
        related_evidence: list[tuple[str, str]] = []
        for index in section_indices:
            related_evidence.extend(adjacent_evidence([index], field))
        normalized_value = _comparison_text(value)
        normalized_quote = _normalized_evidence_text(quote)
        relation_support = any(
            normalized_value
            and normalized_value in _comparison_text(candidate)
            and normalized_quote in _normalized_evidence_text(related_quote)
            for candidate, related_quote in related_evidence
        )
        if not quote:
            add_issue("field_evidence_quote_missing", field, section_index)
            continue
        if len(quote) > 512:
            add_issue("field_evidence_quote_too_long", field, section_index)
        if (
            quote not in section_text
            and normalized_quote not in _normalized_evidence_text(section_text)
            and not relation_support
        ):
            add_issue("field_evidence_quote_not_in_section", field, section_index)
            continue
        if (
            value not in quote
            and normalized_value not in normalized_quote
            and not relation_support
        ):
            add_issue("field_evidence_value_not_in_quote", field, section_index)
            continue

        if field == "project_name":
            if _PROJECT_NAME_LABELLED_VALUE.search(quote) is None and not relation_support:
                add_issue("field_evidence_role_label_missing", field, section_index)
            for labelled_value in _labelled_values(
                "\n".join(sections[index] for index in sorted(sections)),
                _PROJECT_NAME_LABELLED_VALUE,
            ):
                normalized_labelled = _comparison_text(labelled_value)
                if (
                    normalized_labelled != normalized_value
                    and normalized_labelled.startswith(normalized_value)
                ):
                    add_issue("project_name_truncated", field, section_index)
                    break
        elif field == "customer":
            if (
                _PURCHASER_LABELLED_VALUE.search(quote) is None
                and _AGENCY_LABELLED_VALUE.search(quote) is None
                and adjacent_purchaser_reference(section_indices) is None
                and not relation_support
            ):
                add_issue("field_evidence_role_label_missing", field, section_index)
            purchaser_values = _labelled_values(
                section_text, _PURCHASER_LABELLED_VALUE
            )
            agency_values = _labelled_values(section_text, _AGENCY_LABELLED_VALUE)
            purchaser_match = any(
                _comparison_text(candidate) == normalized_value
                for candidate in purchaser_values
            )
            adjacent_value = adjacent_purchaser_reference(section_indices)
            if adjacent_value is not None:
                purchaser_match = purchaser_match or (
                    _comparison_text(adjacent_value) == normalized_value
                )
            agency_match = any(
                _comparison_text(candidate) == normalized_value
                for candidate in agency_values
            )
            if agency_match:
                add_issue("customer_agency_role_mismatch", field, section_index)
            if purchaser_values and not purchaser_match:
                add_issue("customer_role_conflict", field, section_index)
            elif not purchaser_match:
                add_issue("customer_role_unverified", field, section_index)
        elif field == "bid_open_time":
            opening_values = [
                candidate
                for section_text in sections.values()
                for candidate in _labelled_time_values(section_text, _OPENING_TIME_LABELS)
                if _DATE_TIME_SIGNAL.search(candidate)
            ]
            distinct_openings = {
                _comparison_text(candidate) for candidate in opening_values
            }
            if len(distinct_openings) > 1:
                add_issue("bid_opening_ambiguous", field, section_index)
            else:
                inline_value = _inline_opening_datetime_text_value(quote)
                if inline_value is not None:
                    if _comparison_text(value) not in _comparison_text(inline_value):
                        add_issue(
                            "bid_opening_inline_value_mismatch",
                            field,
                            section_index,
                        )
                    continue
                if not (
                    _has_explicit_opening_datetime(quote)
                    or bounded_opening_reference(section_indices)
                    or split_opening_reference(section_indices)
                    or relation_support
                ):
                    if _has_opening_reference(quote):
                        add_issue(
                            "bid_opening_reference_unresolved", field, section_index
                        )
                    elif _has_deadline_datetime(quote):
                        add_issue("bid_opening_deadline_only", field, section_index)
                    else:
                        add_issue("bid_opening_label_missing", field, section_index)
        else:
            relation_has_scope = any(
                _CORE_SCOPE_ACTION_SIGNAL.search(candidate)
                and _CORE_SCOPE_OBJECT_SIGNAL.search(candidate)
                and normalized_quote in _normalized_evidence_text(related_quote)
                for candidate, related_quote in related_evidence
            )
            if not (
                _CORE_SCOPE_ACTION_SIGNAL.search(value)
                and _CORE_SCOPE_OBJECT_SIGNAL.search(value)
                and _CORE_SCOPE_ACTION_SIGNAL.search(quote)
                and _CORE_SCOPE_OBJECT_SIGNAL.search(quote)
            ) and not relation_has_scope:
                add_issue("content_scope_insufficient", field, section_index)

    return {
        "schema_version": FIELD_INTEGRITY_SCHEMA_VERSION,
        "status": "suspect" if issues else "verified",
        "suspect_fields": sorted(suspect_fields),
        "quality_issues": issues,
    }
