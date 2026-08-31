from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

WEAK_PROJECT_TOKENS = {
    "2024",
    "2023",
    "2025",
    "项目",
    "采购",
    "招标",
    "磋商",
    "文件",
    "合同",
    "公告",
    "结果",
    "成交",
    "中标",
    "服务",
    "建设",
}

MONEY_RE = r"((?:人民币)?\s*\d[\d,，]*(?:\.\d+)?\s*(?:万元|万|元|亿元|亿))"
WINNER_PATTERNS = [
    r"(?:中标供应商名称|成交供应商名称|中标供应商|成交供应商|中标人名称|成交人名称|中标人|成交人|供应商名称)"
    r"\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、&＆,.，\s]{4,120})",
    r"(?:第一中标候选人|第一成交候选人)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、&＆,.，\s]{4,120})",
]
AWARD_AMOUNT_PATTERNS = [
    rf"(?:中标金额|成交金额|中标价|成交价|投标报价|报价金额|中标（成交）金额|中标\(成交\)金额)[^\d]{{0,60}}{MONEY_RE}",
    rf"(?:总中标金额|总成交金额)[^\d]{{0,60}}{MONEY_RE}",
]
IDENTIFIER_PATTERNS = {
    "project_no": [
        r"(?:项目编号|项目代码)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./（）()]{3,60})",
    ],
    "procurement_no": [
        r"(?:采购编号|采购项目编号|政府采购编号|采购计划编号|计划编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./（）()]{3,60})",
    ],
    "bid_no": [
        r"(?:招标编号|招标文件编号|委托编号|代理编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./（）()]{3,60})",
    ],
    "package_no": [
        r"(?:包号|标包|采购包|包件)\s*[:：]?\s*(第?[A-Za-z0-9一二三四五六七八九十]+包|包[A-Za-z0-9一二三四五六七八九十]+)",
    ],
}
FILENAME_IDENTIFIER_PATTERN = (
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Z]{2,}[A-Z0-9]*(?:[-_][A-Z0-9]+)+"
    r"|ZC\d{2}-\d{3,}"
    r"|[A-Z]{2,}\d{4}[A-Z0-9]*"
    r"|\d{4}STC\d{4,}"
    r")(?![A-Za-z0-9])"
)


@dataclass
class EnrichmentAttempt:
    source_type: str
    method: str
    success: bool
    source_url: str = ""
    source_title: str = ""
    winner: str = ""
    award_amount: str = ""
    message: str = ""


@dataclass
class StrategyContext:
    records: list[dict[str, Any]]
    favorite_links: list[dict[str, Any]]
    government_sites: list[dict[str, Any]]
    timeout: int
    max_favorite_links: int
    government_results: int
    sleep: float


def clean_text(text: str | None) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_value(value: str | None, max_len: int = 180) -> str:
    value = clean_text(value)
    return value.strip(" ：:;；，,。")[:max_len]


def normalize(value: str | None) -> str:
    value = clean_text(value)
    value = re.sub(r"[（）()【】\[\]《》<>“”\"'：:；;，,。、\s_\-—+]+", "", value)
    return value.lower()


def project_tokens(project_name: str) -> list[str]:
    tokens = []
    for token in re.split(r"[\s（）()【】\[\]《》<>：:+_\-—、]+", project_name):
        token = token.strip()
        compact = normalize(token)
        if len(compact) < 5:
            continue
        if compact in WEAK_PROJECT_TOKENS or compact.isdigit():
            continue
        tokens.append(token)
    if tokens:
        return tokens[:8]
    compact = normalize(project_name)
    return [compact[:16]] if len(compact) >= 16 else []


def record_search_text(record: dict[str, Any]) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("source_file", "project_name", "customer", "content", "note")
    )


def clean_identifier(value: str | None) -> str:
    value = clean_value(value, max_len=80)
    value = re.split(r"\s|，|,|。|；|;|、|详见|采购人|采购方式|预算", value)[0]
    return value.strip(" ：:;；，,。()（）")


def extract_search_identifiers(record: dict[str, Any]) -> dict[str, list[str]]:
    text = record_search_text(record)
    identifiers: dict[str, list[str]] = {key: [] for key in IDENTIFIER_PATTERNS}

    for key, patterns in IDENTIFIER_PATTERNS.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                value = clean_identifier(match.group(1))
                if value and value not in identifiers[key]:
                    identifiers[key].append(value)

    filename = str(record.get("source_file") or "")
    for match in re.finditer(FILENAME_IDENTIFIER_PATTERN, filename, flags=re.I):
        value = clean_identifier(match.group(0))
        if value and value not in identifiers["project_no"]:
            identifiers["project_no"].append(value)

    return identifiers


def build_search_queries(record: dict[str, Any]) -> list[str]:
    project = clean_value(record.get("project_name") or Path(record.get("source_file", "")).stem)
    customer = clean_value(record.get("customer") or "")
    identifiers = extract_search_identifiers(record)
    queries: list[str] = []

    for key, values in identifiers.items():
        if key == "package_no":
            continue
        for value in values:
            queries.append(f"{value} 中标 成交")
            if project:
                queries.append(f"{value} {project} 中标")
            if customer:
                queries.append(f"{customer} {value} 中标")

    for package in identifiers.get("package_no", []):
        if project:
            queries.append(f"{project} {package} 中标 成交")

    if project:
        queries.append(f"{project} 中标 成交")
    if customer and project:
        queries.append(f"{customer} {project} 中标 成交")

    deduped: list[str] = []
    seen = set()
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        key = normalize(query)
        if query and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:12]


def is_relevant(text: str, record: dict[str, Any]) -> bool:
    compact_text = normalize(text)
    project = record.get("project_name") or Path(record.get("source_file", "")).stem
    compact_project = normalize(project)
    if len(compact_project) >= 16 and compact_project[:16] in compact_text:
        return True
    tokens = project_tokens(project)
    hits = sum(1 for token in tokens if normalize(token) in compact_text)
    if hits >= min(2, len(tokens)) and hits > 0:
        return True
    customer = normalize(record.get("customer"))
    return bool(customer and len(customer) >= 6 and customer in compact_text and compact_project[:12] in compact_text)


def extract_winner(text: str) -> str:
    for pattern in WINNER_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        value = clean_value(match.group(1))
        value = re.split(r"地址|统一社会信用|金额|报价|得分|服务|名称|评审|代理", value)[0]
        value = clean_value(value, max_len=100)
        if 4 <= len(value) <= 90 and not any(term in value for term in ("采购", "公告", "保证金", "评审委员会")):
            return value
    return ""


def extract_award_amount(text: str) -> str:
    for pattern in AWARD_AMOUNT_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", "", match.group(1))
    return ""


def build_attempt(
    source_type: str,
    method: str,
    record: dict[str, Any],
    text: str,
    source_url: str = "",
    source_title: str = "",
) -> EnrichmentAttempt:
    if not is_relevant(text, record):
        return EnrichmentAttempt(
            source_type=source_type,
            method=method,
            success=False,
            source_url=source_url,
            source_title=source_title,
            message="source_not_relevant",
        )
    winner = extract_winner(text)
    award_amount = extract_award_amount(text)
    success = bool(winner or award_amount)
    return EnrichmentAttempt(
        source_type=source_type,
        method=method,
        success=success,
        source_url=source_url,
        source_title=source_title,
        winner=winner,
        award_amount=award_amount,
        message="matched" if success else "no_winner_or_amount_found",
    )


def fetch(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(1_500_000)
        content_type = response.headers.get("content-type", "")
    encodings = ["utf-8", "gb18030"]
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        encodings.insert(0, match.group(1))
    for encoding in encodings:
        try:
            return data.decode(encoding, errors="ignore")
        except Exception:
            pass
    return data.decode("utf-8", errors="ignore")
