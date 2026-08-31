from __future__ import annotations

import html
import json
import re
import socket
import time
import urllib.error
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from strategies.base import clean_text, clean_value, fetch, normalize


TARGET_FIELDS = ("winner", "award_amount", "customer", "budget", "bid_open_time")
NOTICE_TERMS = ("候选人公示", "中标候选人", "成交候选人", "评标结果公示")
FINAL_NOTICE_TERMS = ("中标公告", "成交公告", "结果公告", "中标结果", "成交结果")
PACKAGE_TERMS = ("标包", "包号", "采购包", "分包", "包一", "包二", "包1", "包2")
AMOUNT_UNCLEAR_TERMS = ("预算", "最高限价", "控制价", "估算价")
AWARD_AMOUNT_TERMS = ("中标金额", "成交金额", "中标价", "成交价", "中标（成交）金额", "中标(成交)金额")


@dataclass
class WebCandidate:
    record_id: str
    source_document_id: str
    source_file: str
    project_name: str
    target_field: str
    candidate_value: str
    source_url: str
    source_title: str
    source_type: str
    matched_query: str
    evidence_snippet: str
    confidence: str
    risk_flags: list[str]


def write_json(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def new_diagnostics() -> dict[str, Any]:
    return {
        "failed_sites": {},
        "http_status_counts": {},
        "timeout_count": 0,
        "candidate_parse_fail_count": 0,
        "request_success_count": 0,
        "request_failed_count": 0,
        "selected_sites": [],
        "site_diagnostics": {},
        "reason_counts": {},
    }


def source_label(source: dict[str, Any]) -> str:
    return str(source.get("name") or source.get("base_url") or source.get("url") or "unknown")


def record_request_success(diagnostics: dict[str, Any]) -> None:
    diagnostics["request_success_count"] = int(diagnostics.get("request_success_count", 0)) + 1


def record_candidate_parse_fail(diagnostics: dict[str, Any]) -> None:
    diagnostics["candidate_parse_fail_count"] = int(diagnostics.get("candidate_parse_fail_count", 0)) + 1


def record_reason(diagnostics: dict[str, Any], reason: str) -> None:
    counts = diagnostics.setdefault("reason_counts", {})
    counts[reason] = int(counts.get(reason, 0)) + 1


def site_metrics(diagnostics: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    sites = diagnostics.setdefault("site_diagnostics", {})
    name = source_label(source)
    return sites.setdefault(
        name,
        {
            "name": name,
            "category": str(source.get("category") or ""),
            "base_url": str(source.get("base_url") or source.get("url") or ""),
            "search_pages_fetched": 0,
            "detail_links_found": 0,
            "detail_pages_fetched": 0,
            "detail_parse_success": 0,
            "detail_parse_failed": 0,
            "candidates_by_site": 0,
            "search_blocked_count": 0,
            "search_no_result_count": 0,
            "search_no_detail_link_count": 0,
            "filtered_by_project_match": 0,
        },
    )


def increment_site_metric(diagnostics: dict[str, Any], source: dict[str, Any], key: str, amount: int = 1) -> None:
    metrics = site_metrics(diagnostics, source)
    metrics[key] = int(metrics.get(key, 0)) + amount


def record_request_failure(diagnostics: dict[str, Any], source: dict[str, Any], exc: Exception) -> None:
    diagnostics["request_failed_count"] = int(diagnostics.get("request_failed_count", 0)) + 1
    failed_sites = diagnostics.setdefault("failed_sites", {})
    name = source_label(source)
    site = failed_sites.setdefault(
        name,
        {
            "name": name,
            "category": str(source.get("category") or ""),
            "base_url": str(source.get("base_url") or source.get("url") or ""),
            "error_count": 0,
            "last_error": "",
        },
    )
    site["error_count"] = int(site.get("error_count", 0)) + 1
    site["last_error"] = str(exc)[:240]

    if isinstance(exc, urllib.error.HTTPError):
        status = str(exc.code)
        counts = diagnostics.setdefault("http_status_counts", {})
        counts[status] = int(counts.get(status, 0)) + 1

    message = str(exc).lower()
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in message or "timeout" in message:
        diagnostics["timeout_count"] = int(diagnostics.get("timeout_count", 0)) + 1


def finalize_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    failed_sites = sorted(
        diagnostics.get("failed_sites", {}).values(),
        key=lambda item: (-int(item.get("error_count", 0)), str(item.get("name") or "")),
    )
    return {
        "failed_site_count": len(failed_sites),
        "failed_sites": failed_sites,
        "http_status_counts": dict(sorted(diagnostics.get("http_status_counts", {}).items())),
        "timeout_count": int(diagnostics.get("timeout_count", 0)),
        "candidate_parse_fail_count": int(diagnostics.get("candidate_parse_fail_count", 0)),
        "selected_sites": diagnostics.get("selected_sites", []),
        "site_diagnostics": sorted(
            diagnostics.get("site_diagnostics", {}).values(),
            key=lambda item: str(item.get("name") or ""),
        ),
        "reason_counts": dict(sorted(diagnostics.get("reason_counts", {}).items())),
    }


def risk_flag_counts(candidates: list[WebCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        for flag in candidate.risk_flags:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


def build_candidate_summary(
    *,
    targets: list[dict[str, Any]],
    max_requests: int,
    request_budget: list[int],
    government_sites: list[dict[str, Any]],
    favorite_sites: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    candidates: list[WebCandidate],
    confirmed: list[WebCandidate],
    review: list[WebCandidate],
) -> dict[str, Any]:
    failure_diagnostics = finalize_diagnostics(diagnostics)
    return {
        "checked_records": len(targets),
        "max_requests": max_requests,
        "used_requests": max_requests - request_budget[0],
        "site_count": len(government_sites) + len(favorite_sites),
        "enabled_site_count": len(sources),
        "request_success_count": int(diagnostics.get("request_success_count", 0)),
        "request_failed_count": int(diagnostics.get("request_failed_count", 0)),
        "candidate_count": len(candidates),
        "confirmed_suggestions_count": len(confirmed),
        "review_candidates_count": len(review),
        "risk_flag_counts": risk_flag_counts(candidates),
        **failure_diagnostics,
    }


def markdown_escape(value: Any) -> str:
    text = str(value or "")
    text = text.replace("|", "\\|")
    return re.sub(r"\s+", " ", text).strip()


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def normalize_for_match(value: str | None) -> str:
    value = clean_text(value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value.lower()


def source_type_for(site: dict[str, Any], url: str) -> str:
    name = str(site.get("name") or "")
    category = str(site.get("category") or "")
    url_lower = url.lower()
    if "ccgp.gov.cn" in url_lower or "中国政府采购" in name:
        return "中国政府采购网"
    if "ggzy" in url_lower or "公共资源" in name or category == "public_resource":
        return "公共资源交易平台"
    if "zfcg" in url_lower or "政府采购" in name or category in {"central", "local"}:
        return "省市政府采购网"
    if "采购人" in name or category == "buyer":
        return "采购人官网"
    if "代理" in name or category == "agency":
        return "招标代理机构官网"
    if category == "favorites":
        return "收藏夹来源"
    return "其他来源"


def extract_links(page: str, base_url: str, limit: int) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.I | re.S):
        href = html.unescape(match.group(1))
        url = urllib.parse.urljoin(base_url, href)
        title = clean_text(match.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        links.append({"title": title[:180], "url": url})
        if len(links) >= limit:
            break
    return links


def site_key(source: dict[str, Any]) -> str:
    name = source_label(source)
    base_url = str(source.get("base_url") or source.get("url") or "")
    combined = f"{name} {base_url}"
    if "江苏政府采购" in combined or "ccgp-jiangsu" in combined:
        return "jiangsu_ccgp"
    if "天津政府采购" in combined or "ccgp-tianjin" in combined:
        return "tianjin_ccgp"
    if "湖北政府采购" in combined or "ccgp-hubei" in combined:
        return "hubei_ccgp"
    return "generic"


def build_site_search_url(source: dict[str, Any], query: str) -> str:
    key = site_key(source)
    encoded = urllib.parse.quote(query)
    if key == "jiangsu_ccgp":
        return (
            "http://www.ccgp-jiangsu.gov.cn/pss/jsp/search.jsp"
            f"?kw={encoded}&qt=0&plm=&sd=0&ed=0&validateCode=&page=1"
        )
    search_template = str(source.get("search_url") or "")
    return search_template.format(query=encoded)


def extract_jiangsu_links(page: str, base_url: str, limit: int, diagnostics: dict[str, Any], source: dict[str, Any]) -> list[dict[str, str]]:
    try:
        payload = json.loads(page.strip())
    except json.JSONDecodeError:
        record_reason(diagnostics, "jiangsu_search_json_parse_failed")
        return []
    if payload.get("msg") == "ERROR" or payload.get("code") not in {200, "200", None}:
        message = str(payload.get("message") or "")
        if "验证码" in message:
            increment_site_metric(diagnostics, source, "search_blocked_count")
            record_reason(diagnostics, "jiangsu_search_captcha_required")
        else:
            record_reason(diagnostics, "jiangsu_search_error")
        return []
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    items = result.get("list") if isinstance(result.get("list"), list) else []
    if not items:
        increment_site_metric(diagnostics, source, "search_no_result_count")
        record_reason(diagnostics, "search_no_result")
        return []

    links: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title") or "")
        if item.get("highlight") and isinstance(item.get("highlight"), dict):
            highlighted = item["highlight"].get("title")
            if isinstance(highlighted, list) and highlighted:
                title = clean_text("".join(str(part) for part in highlighted))
        if item.get("type") == 1 or str(item.get("type")) == "1":
            href = f"/jiangsu/js_cggg/details.html?gglb={item.get('ggCode', '')}&ggid={item.get('id', '')}"
        else:
            href = str(item.get("url") or "")
        url = urllib.parse.urljoin(base_url, href)
        if title and url:
            links.append({"title": title[:180], "url": url})
        if len(links) >= limit:
            break
    return links


def extract_tianjin_links(page: str, base_url: str, limit: int) -> list[dict[str, str]]:
    links = extract_links(page, base_url, limit * 5)
    filtered: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        url = link["url"]
        title = link["title"]
        if "viewer.do" not in url:
            continue
        if not any(term in title for term in ("公告", "中标", "成交", "采购", "项目")):
            continue
        if url in seen:
            continue
        seen.add(url)
        filtered.append(link)
        if len(filtered) >= limit:
            break
    return filtered


def extract_site_links(
    page: str,
    source: dict[str, Any],
    base_url: str,
    limit: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, str]]:
    key = site_key(source)
    if key == "jiangsu_ccgp":
        return extract_jiangsu_links(page, base_url, limit, diagnostics, source)
    if key == "tianjin_ccgp":
        return extract_tianjin_links(page, base_url, limit)
    return extract_links(page, base_url, limit)


def extract_identifiers(text: str) -> list[str]:
    patterns = [
        r"(?:项目编号|采购编号|招标编号|采购项目编号|政府采购编号|项目代码|代理编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./()（）]{3,80})",
        r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[A-Z0-9]*(?:[-_][A-Z0-9]+)+|ZC\d{2}-\d{3,}|[A-Z]{2,}\d{4}[A-Z0-9]*|\d{4}STC\d{4,})(?![A-Za-z0-9])",
    ]
    values: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.group(1) if match.lastindex else match.group(0)
            value = clean_value(value, max_len=80)
            value = re.split(r"\s|，|,|；|;|。|采购人|采购方式|预算", value)[0]
            if value and value not in values:
                values.append(value)
    return values[:6]


def build_queries(item: dict[str, Any]) -> list[str]:
    current = item.get("current_values") if isinstance(item.get("current_values"), dict) else {}
    project = clean_value(item.get("project_name") or current.get("project_name") or Path(item.get("source_file", "")).stem)
    customer = clean_value(item.get("customer") or current.get("customer") or "")
    source_text = " ".join(
        str(value or "")
        for value in [
            item.get("source_file"),
            item.get("project_name"),
            customer,
            current.get("content"),
            current.get("note"),
        ]
    )
    identifiers = extract_identifiers(source_text)
    queries: list[str] = []
    for identifier in identifiers:
        queries.append(f"{identifier} 中标 成交")
        if project:
            queries.append(f"{identifier} {project}")
    if project:
        queries.append(f"{project} 中标 成交")
        queries.append(f"{project} 预算 开标")
    if customer and project:
        queries.append(f"{customer} {project}")

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        key = normalize_for_match(query)
        if query and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:10]


def project_match_strength(text: str, project_name: str) -> str:
    compact_text = normalize_for_match(text)
    compact_project = normalize_for_match(project_name)
    if compact_project and len(compact_project) >= 12 and compact_project[:18] in compact_text:
        return "strong"
    tokens = [
        normalize_for_match(token)
        for token in re.split(r"[\s（）()【】\[\]、，,._\-]+", project_name)
        if len(normalize_for_match(token)) >= 5
    ]
    hits = sum(1 for token in tokens[:8] if token in compact_text)
    if hits >= 2:
        return "medium"
    if hits == 1:
        return "weak"
    return "none"


def year_values(text: str) -> set[str]:
    return set(re.findall(r"20\d{2}", text or ""))


def snippet_around(text: str, value: str, fallback_terms: tuple[str, ...] = ()) -> str:
    clean = clean_text(text)
    needles = [value, *fallback_terms]
    for needle in needles:
        if not needle:
            continue
        index = clean.find(needle)
        if index >= 0:
            start = max(0, index - 90)
            end = min(len(clean), index + len(needle) + 130)
            return clean[start:end]
    return clean[:220]


def first_match(patterns: list[str], text: str) -> tuple[str, str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        value = clean_value(match.group(1), max_len=180)
        if value:
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 120)
            return value, text[start:end]
    return "", ""


def extract_field_candidates(text: str, target_field: str) -> list[tuple[str, str]]:
    money = r"((?:人民币)?\s*[0-9][0-9,，]*(?:\.[0-9]+)?\s*(?:万元|元|亿元)?)"
    patterns = {
        "winner": [
            r"(?:中标供应商名称|成交供应商名称|中标供应商|成交供应商|中标人名称|成交人名称|中标人|成交人)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、，,. \s]{4,120})",
            r"(?:供应商名称)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、，,. \s]{4,120})",
        ],
        "award_amount": [
            rf"(?:中标金额|成交金额|中标价|成交价|中标（成交）金额|中标\(成交\)金额)[^\d]{{0,60}}{money}",
            rf"(?:总中标金额|总成交金额)[^\d]{{0,60}}{money}",
        ],
        "customer": [
            r"(?:采购人信息|采购人名称|采购人|招标人|建设单位)\s*[:：]?\s*(?:名称\s*[:：]?)?([\u4e00-\u9fffA-Za-z0-9（）()·\-—、，,. \s]{4,100})",
            r"(?:名称)\s*[:：]\s*([\u4e00-\u9fffA-Za-z0-9（）()·\-—、，,. \s]{4,100})",
        ],
        "budget": [
            rf"(?:预算金额|项目预算|采购预算)[^\d]{{0,60}}{money}",
            rf"(?:最高限价|最高投标限价|控制价)[^\d]{{0,60}}{money}",
        ],
        "bid_open_time": [
            r"(?:开标时间|投标截止时间|响应文件提交截止时间|提交投标文件截止时间|截止时间)\s*[:：]?\s*([0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日\s*[0-9]{1,2}\s*(?:时|:|：)\s*[0-9]{1,2}\s*(?:分)?)",
            r"(?:开标时间|投标截止时间|响应文件提交截止时间|提交投标文件截止时间|截止时间)\s*[:：]?\s*(20[0-9]{2}[-/][0-9]{1,2}[-/][0-9]{1,2}\s+[0-9]{1,2}[:：][0-9]{1,2}(?::[0-9]{1,2})?)",
        ],
    }
    value, evidence = first_match(patterns.get(target_field, []), text)
    if not value:
        return []
    if target_field in {"winner", "customer"}:
        value = re.split(r"地址|统一社会信用|金额|报价|得分|联系方式|电话|采购代理|代理机构", value)[0]
        value = clean_value(value, max_len=100)
    if target_field in {"award_amount", "budget"}:
        value = re.sub(r"\s+", "", value).replace("，", ",")
    return [(value, evidence)] if value else []


def risk_flags_for(
    item: dict[str, Any],
    field: str,
    value: str,
    title: str,
    page_text: str,
    match_strength: str,
    evidence: str,
) -> list[str]:
    flags: list[str] = []
    combined = f"{title} {page_text}"
    current = item.get("current_values") if isinstance(item.get("current_values"), dict) else {}

    if any(term in combined for term in NOTICE_TERMS):
        flags.append("candidate_notice_only")
    if match_strength != "strong":
        flags.append("weak_project_match")
    if field in {"award_amount", "budget"}:
        if field == "award_amount" and any(term in evidence for term in AMOUNT_UNCLEAR_TERMS):
            flags.append("amount_type_unclear")
        if field == "budget" and any(term in evidence for term in AWARD_AMOUNT_TERMS):
            flags.append("amount_type_unclear")
        if field == "budget" and "最高限价" in evidence and "预算" not in evidence:
            flags.append("amount_type_unclear")
    if any(term in combined for term in PACKAGE_TERMS):
        flags.append("package_unclear")
    if field != "customer":
        existing_customer = clean_value(item.get("customer") or current.get("customer") or "")
        page_customer = extract_field_candidates(page_text, "customer")
        if existing_customer and page_customer:
            page_customer_value = page_customer[0][0]
            if normalize_for_match(existing_customer) not in normalize_for_match(page_customer_value):
                flags.append("customer_mismatch")
    item_years = year_values(f"{item.get('source_file', '')} {item.get('project_name', '')}")
    page_years = year_values(combined)
    if item_years and page_years and item_years.isdisjoint(page_years):
        flags.append("year_mismatch")

    deduped: list[str] = []
    for flag in flags:
        if flag not in deduped:
            deduped.append(flag)
    return deduped


def confidence_for(match_strength: str, flags: list[str], source_type: str, title: str) -> str:
    if flags:
        return "low" if "candidate_notice_only" in flags or match_strength == "none" else "medium"
    if match_strength == "strong" and source_type in {"中国政府采购网", "省市政府采购网", "公共资源交易平台"}:
        return "high"
    if match_strength in {"strong", "medium"}:
        return "medium"
    return "low"


def build_search_sources(
    government_sites: list[dict[str, Any]],
    favorite_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources = []
    for site in government_sites + favorite_sites:
        if not site.get("enabled", True):
            continue
        priority = int(site.get("priority", 999))
        sources.append({**site, "priority": priority})
    return sorted(sources, key=lambda item: item.get("priority", 999))


def inspect_source(
    item: dict[str, Any],
    source: dict[str, Any],
    query: str,
    target_fields: list[str],
    timeout: int,
    results_per_source: int,
    request_budget: list[int],
    diagnostics: dict[str, Any],
) -> list[WebCandidate]:
    search_template = str(source.get("search_url") or "")
    base_url = str(source.get("base_url") or source.get("url") or "")
    if not search_template or not base_url:
        return []
    if request_budget[0] <= 0:
        return []

    search_url = build_site_search_url(source, query)
    request_budget[0] -= 1
    search_page = fetch(search_url, timeout)
    record_request_success(diagnostics)
    increment_site_metric(diagnostics, source, "search_pages_fetched")
    links = extract_site_links(search_page, source, base_url, results_per_source, diagnostics)
    increment_site_metric(diagnostics, source, "detail_links_found", len(links))
    if not links:
        increment_site_metric(diagnostics, source, "search_no_detail_link_count")
        record_reason(diagnostics, "search_no_detail_link")
        record_candidate_parse_fail(diagnostics)
    candidates: list[WebCandidate] = []

    for link in links:
        if request_budget[0] <= 0:
            break
        request_budget[0] -= 1
        page_text = clean_text(fetch(link["url"], timeout))
        record_request_success(diagnostics)
        increment_site_metric(diagnostics, source, "detail_pages_fetched")
        source_title = link["title"]
        source_url = link["url"]
        source_type = source_type_for(source, source_url)
        project_name = clean_value(item.get("project_name") or Path(item.get("source_file", "")).stem)
        match_strength = project_match_strength(f"{source_title} {page_text}", project_name)
        page_candidate_count = 0
        if match_strength == "none":
            increment_site_metric(diagnostics, source, "filtered_by_project_match")
            record_reason(diagnostics, "filtered_by_project_match")
            record_candidate_parse_fail(diagnostics)
            continue

        for field in target_fields:
            for value, evidence in extract_field_candidates(page_text, field):
                snippet = snippet_around(page_text, value, (field, "中标", "成交", "预算", "开标"))
                flags = risk_flags_for(item, field, value, source_title, page_text, match_strength, evidence)
                confidence = confidence_for(match_strength, flags, source_type, source_title)
                if field in {"winner", "award_amount"} and "candidate_notice_only" in flags:
                    confidence = "low"
                page_candidate_count += 1
                increment_site_metric(diagnostics, source, "candidates_by_site")
                candidates.append(
                    WebCandidate(
                        record_id=str(item.get("record_id") or item.get("source_document_id") or ""),
                        source_document_id=str(item.get("source_document_id") or ""),
                        source_file=str(item.get("source_file") or ""),
                        project_name=project_name,
                        target_field=field,
                        candidate_value=value,
                        source_url=source_url,
                        source_title=source_title,
                        source_type=source_type,
                        matched_query=query,
                        evidence_snippet=snippet,
                        confidence=confidence,
                        risk_flags=flags,
                    )
                )
        if page_candidate_count == 0:
            increment_site_metric(diagnostics, source, "detail_parse_failed")
            record_reason(diagnostics, "detail_parse_failed")
            record_candidate_parse_fail(diagnostics)
        else:
            increment_site_metric(diagnostics, source, "detail_parse_success")
    return candidates


def target_fields_for(item: dict[str, Any]) -> list[str]:
    missing = item.get("missing_fields") if isinstance(item.get("missing_fields"), list) else []
    fields = [field for field in TARGET_FIELDS if field in missing]
    return fields or list(TARGET_FIELDS)


def dedupe_candidates(candidates: list[WebCandidate]) -> list[WebCandidate]:
    deduped: list[WebCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            candidate.record_id,
            candidate.target_field,
            normalize_for_match(candidate.candidate_value),
            candidate.source_url,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        deduped,
        key=lambda item: (
            rank.get(item.confidence, 9),
            len(item.risk_flags),
            item.record_id,
            item.target_field,
        ),
    )


def generate_candidates(
    review_queue_path: Path,
    final_records_path: Path,
    government_sites_path: Path,
    favorite_sites_path: Path,
    timeout: int = 12,
    limit: int = 20,
    results_per_source: int = 4,
    sleep: float = 0.4,
    max_requests: int = 40,
) -> dict[str, Any]:
    queue = load_json_list(review_queue_path)
    final_records = load_json_list(final_records_path)
    record_by_id = {record.get("source_document_id"): record for record in final_records}
    government_sites = load_json_list(government_sites_path)
    favorite_sites = load_json_list(favorite_sites_path)
    sources = build_search_sources(government_sites, favorite_sites)

    candidates: list[WebCandidate] = []
    errors: list[dict[str, str]] = []
    targets = queue[:limit] if limit else queue
    request_budget = [max_requests]
    diagnostics = new_diagnostics()
    diagnostics["selected_sites"] = [source_label(source) for source in sources]
    for source in sources:
        site_metrics(diagnostics, source)

    for item in targets:
        if request_budget[0] <= 0:
            break
        record = record_by_id.get(item.get("source_document_id"), {})
        merged = {**record, **item}
        queries = build_queries(merged)
        fields = target_fields_for(merged)
        for query in queries:
            if request_budget[0] <= 0:
                break
            for source in sources:
                if request_budget[0] <= 0:
                    break
                try:
                    candidates.extend(
                        inspect_source(
                            merged,
                            source,
                            query,
                            fields,
                            timeout=timeout,
                            results_per_source=results_per_source,
                            request_budget=request_budget,
                            diagnostics=diagnostics,
                        )
                    )
                except Exception as exc:
                    record_request_failure(diagnostics, source, exc)
                    errors.append(
                        {
                            "record_id": str(merged.get("record_id") or merged.get("source_document_id") or ""),
                            "query": query,
                            "source": str(source.get("name") or source.get("base_url") or source.get("url") or ""),
                            "error": str(exc)[:240],
                        }
                    )
                if sleep:
                    time.sleep(sleep)

    deduped = dedupe_candidates(candidates)
    confirmed = [item for item in deduped if item.confidence == "high" and not item.risk_flags]
    review = [item for item in deduped if item not in confirmed]
    summary = build_candidate_summary(
        targets=targets,
        max_requests=max_requests,
        request_budget=request_budget,
        government_sites=government_sites,
        favorite_sites=favorite_sites,
        sources=sources,
        diagnostics=diagnostics,
        candidates=deduped,
        confirmed=confirmed,
        review=review,
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_review_queue": str(review_queue_path),
        "records_considered": len(targets),
        "max_requests": max_requests,
        "requests_remaining": request_budget[0],
        "requests_used": max_requests - request_budget[0],
        "candidate_count": len(deduped),
        "confirmed_suggestions_count": len(confirmed),
        "review_candidates_count": len(review),
        "diagnostics": {
            "failed_site_count": summary["failed_site_count"],
            "failed_sites": summary["failed_sites"],
            "http_status_counts": summary["http_status_counts"],
            "timeout_count": summary["timeout_count"],
            "candidate_parse_fail_count": summary["candidate_parse_fail_count"],
            "selected_sites": summary["selected_sites"],
            "site_diagnostics": summary["site_diagnostics"],
            "reason_counts": summary["reason_counts"],
        },
        "summary": summary,
        "errors": errors[:100],
        "confirmed_suggestions": [asdict(item) for item in confirmed],
        "review_candidates": [asdict(item) for item in review],
    }


def write_candidates_summary(payload: dict[str, Any], path: Path) -> None:
    write_json(payload.get("summary", {}), path)


def write_candidates_markdown(payload: dict[str, Any], path: Path) -> None:
    rows = payload.get("confirmed_suggestions", []) + payload.get("review_candidates", [])
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# Web Enrichment V2 候选结果",
        "",
        f"- Generated At: {payload.get('generated_at', '')}",
        f"- Records Considered: {payload.get('records_considered', 0)}",
        f"- Used Requests: {summary.get('used_requests', payload.get('requests_used', 0))}",
        f"- Enabled Sites: {summary.get('enabled_site_count', 0)} / {summary.get('site_count', 0)}",
        f"- Request Success: {summary.get('request_success_count', 0)}",
        f"- Request Failed: {summary.get('request_failed_count', 0)}",
        f"- Failed Sites: {summary.get('failed_site_count', 0)}",
        f"- Timeout Count: {summary.get('timeout_count', 0)}",
        f"- Candidate Parse Fail Count: {summary.get('candidate_parse_fail_count', 0)}",
        f"- Candidate Count: {payload.get('candidate_count', 0)}",
        f"- Confirmed Suggestions: {payload.get('confirmed_suggestions_count', 0)}",
        f"- Review Candidates: {payload.get('review_candidates_count', 0)}",
        "",
        "| 排名 | 文件 | 项目名称 | 字段 | 候选值 | 来源 | 置信度 | 风险标记 | 证据片段 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(rows, 1):
        source = f"[{markdown_escape(item.get('source_title'))}]({item.get('source_url')})"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_escape(item.get("source_file")),
                    markdown_escape(item.get("project_name")),
                    markdown_escape(item.get("target_field")),
                    markdown_escape(item.get("candidate_value")),
                    source,
                    markdown_escape(item.get("confidence")),
                    markdown_escape(", ".join(item.get("risk_flags") or [])),
                    markdown_escape(item.get("evidence_snippet")),
                ]
            )
            + " |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
