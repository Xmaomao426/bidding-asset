"""Exact, centrally managed XLSX header mappings for acquisition imports."""

from __future__ import annotations

from collections import OrderedDict


# Mapping order is significant when diagnostics report the source of a match.
# Only exact headers already used by the formal workbook, confirmed aliases, or
# the existing Tianyancha import tooling belong here. No fuzzy matching is used.
XLSX_HEADER_ALIASES: "OrderedDict[str, tuple[str, ...]]" = OrderedDict({
    "sequence": ("序号",),
    "publish_date": ("发布日期",),
    "project_number": ("项目编号",),
    "project_name": ("项目名称", "project_name", "标题"),
    "customer": ("客户", "采购人", "招采单位", "buyer", "purchaser"),
    "winner": ("中标厂商", "中标单位", "成交供应商", "winner"),
    "award_amount": ("中标金额", "成交金额", "award_amount"),
    "content": ("项目内容", "content"),
    "budget": ("预算", "预算金额", "budget"),
    "bid_open_time": ("开标时间", "bid_open_time"),
    "source_url": ("来源链接", "链接", "公告链接", "详情链接", "来源URL", "url", "招投标详情"),
    "note": ("备注", "备注信息", "说明", "项目备注", "note"),
    "doc_type": ("文档类型", "公告类型"),
})

STANDARD_BUSINESS_HEADERS = {
    "项目编号": "project_number",
    "项目名称": "project_name",
    "客户": "customer",
    "中标厂商": "winner",
    "中标金额": "award_amount",
    "项目内容": "content",
    "预算": "budget",
    "开标时间": "bid_open_time",
    "来源链接": "source_url",
    "备注": "note",
}

TIANYANCHA_HEADER_FIELDS = {
    "标题": "project_name",
    "项目名称": "project_name",
    "发布日期": "publish_date",
    "省份地区": "region",
    "公告类型": "doc_type",
    "招采单位": "customer",
    "中标单位": "winner",
    "中标金额": "award_amount",
    "招投标详情": "source_url",
}

IDENTITY_FIELDS = {"project_name", "project_number", "source_url"}
BUSINESS_FIELDS = {
    "sequence", "publish_date", "project_number", "project_name", "customer", "winner", "award_amount",
    "content", "budget", "bid_open_time", "source_url", "note", "doc_type",
}


def mapped_field(header: object) -> str:
    """Return a field only for an exact confirmed header or ASCII case variant."""
    value = str(header or "").strip()
    if not value:
        return ""
    folded = value.casefold()
    for field, aliases in XLSX_HEADER_ALIASES.items():
        if any(value == alias or folded == alias.casefold() for alias in aliases):
            return field
    return ""


def mapping_source(header: object) -> str:
    value = str(header or "").strip()
    if value in STANDARD_BUSINESS_HEADERS:
        return "standard_business_header"
    if value in TIANYANCHA_HEADER_FIELDS:
        return "tianyancha_export"
    return "confirmed_existing_alias" if mapped_field(value) else ""
