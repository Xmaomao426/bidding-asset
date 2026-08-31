from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from strategies.base import HEADERS, clean_text, clean_value, fetch
from strategies import web_candidates


TARGET_FIELDS = ("winner", "award_amount", "customer", "budget", "bid_open_time")
BROKER_SEARCH_URLS = {
    "google": "https://www.google.com/search?q={query}&num={count}&hl=zh-CN&filter=0",
    "bing": "https://www.bing.com/search?q={query}&count={count}&setlang=zh-CN",
    "baidu": "https://www.baidu.com/s?wd={query}&rn={count}",
    "sogou": "https://www.sogou.com/web?query={query}&num={count}",
    "so": "https://www.so.com/s?q={query}&pn=1",
}


@dataclass
class SearchHit:
    record_id: str
    source_document_id: str
    source_file: str
    project_name: str
    title: str
    url: str
    snippet: str
    source_domain: str
    matched_query: str
    search_engine: str = "bing"
    risk_flags: list[str] | None = None


@dataclass
class BrokerCandidate:
    record_id: str
    source_document_id: str
    source_file: str
    project_name: str
    target_field: str
    candidate_value: str
    source_url: str
    source_title: str
    source_domain: str
    matched_query: str
    evidence_snippet: str
    confidence: str
    risk_flags: list[str]


def read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def write_json(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def markdown_escape(value: Any) -> str:
    text = str(value or "")
    text = text.replace("|", "\\|")
    return re.sub(r"\s+", " ", text).strip()


def normalize_domain(value: str) -> str:
    value = value.lower().strip()
    if "://" in value:
        value = urllib.parse.urlparse(value).netloc
    value = value.split("/")[0]
    return value[4:] if value.startswith("www.") else value


def source_domain(url: str) -> str:
    return normalize_domain(urllib.parse.urlparse(url).netloc)


def domain_allowed(domain: str, allowed_domains: list[str]) -> bool:
    domain = normalize_domain(domain)
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in allowed_domains)


def source_domains_for_item(item: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    text = " ".join(str(item.get(key) or "") for key in ("source_file", "project_name", "customer"))
    rules = [
        (("南京", "江苏", "苏州", "无锡", "宿豫", "武进"), ("ccgp-jiangsu.gov.cn", "jszfcg.jsczt.cn", "ggzy.nanjing.gov.cn")),
        (("湖北", "黄冈", "蕲春", "HZZFCG"), ("hubei.gov.cn", "hbggzyfwpt.cn", "ccgp.gov.cn")),
        (("天津",), ("ccgp-tianjin.gov.cn", "tjgp.cz.tj.gov.cn")),
        (("河北", "石家庄", "唐山"), ("hebpr.cn", "ggzy.hebei.gov.cn", "ccgp.gov.cn")),
        (("广东", "广州", "冠德"), ("gdgpo.czt.gd.gov.cn", "ccgp.gov.cn")),
        (("浙江", "杭州", "之江", "ZJLAB"), ("zfcg.czt.zj.gov.cn", "ggzy.zj.gov.cn", "ccgp.gov.cn")),
    ]
    enabled = [normalize_domain(source.get("domain", "")) for source in sources if source.get("enabled", True)]
    selected: list[str] = []
    for needles, domains in rules:
        if any(needle in text for needle in needles):
            for domain in domains:
                if domain in enabled and domain not in selected:
                    selected.append(domain)
    if not selected:
        selected = enabled[:3]
    return selected[:3]


def extract_identifiers(item: dict[str, Any]) -> list[str]:
    text = " ".join(str(item.get(key) or "") for key in ("source_file", "project_name", "customer"))
    patterns = [
        r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[A-Z0-9]*(?:[-_][A-Z0-9]+)+|ZC\d{2}-\d{3,}|[A-Z]{2,}\d{4}[A-Z0-9]*|\d{4}STC\d{4,})(?![A-Za-z0-9])",
        r"(?:项目编号|采购编号|招标编号|政府采购编号|代理编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./()（）]{3,80})",
    ]
    values: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.group(1) if match.lastindex else match.group(0)
            value = clean_value(value, max_len=80)
            if value and value not in values:
                values.append(value)
    return values[:4]


def build_base_queries(item: dict[str, Any]) -> list[str]:
    current = item.get("current_values") if isinstance(item.get("current_values"), dict) else {}
    project = clean_value(item.get("project_name") or current.get("project_name") or Path(item.get("source_file", "")).stem)
    customer = clean_value(item.get("customer") or current.get("customer") or "")
    queries: list[str] = []
    for identifier in extract_identifiers(item):
        queries.append(f'"{identifier}" "中标公告"')
        queries.append(f'"{identifier}" "成交公告"')
    if project:
        short_project = re.sub(r"(招标书|采购文件|正式稿|定稿|合同盖章扫描|报价明细附件)$", "", project).strip()
        queries.append(f'"{short_project}" "成交公告"')
        queries.append(f'"{short_project}" "中标公告"')
        queries.append(f'"{short_project}" "合同公告"')
        queries.append(f'"{short_project}" "开标时间"')
    if customer and project:
        queries.append(f'"{customer}" "{project}" "政府采购"')
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = re.sub(r"\s+", " ", query).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:6]


def broker_queries_for_item(item: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    domains = source_domains_for_item(item, sources)
    queries: list[str] = []
    for base_query in build_base_queries(item):
        for domain in domains:
            queries.append(f"site:{domain} {base_query}")
    return queries


def fetch_search_page(query: str, results_per_query: int, timeout: int, engine: str) -> str:
    template = BROKER_SEARCH_URLS.get(engine)
    if not template:
        raise ValueError(f"Unsupported search engine: {engine}")
    url = template.format(query=urllib.parse.quote(query), count=results_per_query)
    request = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(800_000)
        content_type = response.headers.get("content-type", "")
    encoding = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        encoding = match.group(1)
    return data.decode(encoding, errors="ignore")


def normalize_result_url(url: str) -> str:
    url = html.unescape(url)
    if url.startswith("/url?"):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        values = query.get("q") or query.get("url") or []
        return values[0] if values else ""
    return url


def resolve_search_redirect(url: str, timeout: int) -> str:
    parsed = urllib.parse.urlparse(url)
    domain = normalize_domain(parsed.netloc)
    if domain not in {"baidu.com", "sogou.com", "so.com", "www.baidu.com", "www.sogou.com", "www.so.com"}:
        return url
    try:
        request = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.url
    except Exception:
        return url


def parse_bing_hits(page: str, query: str, item: dict[str, Any], allowed_domains: list[str], limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    blocks = re.findall(r"(?is)<li class=\"b_algo\".*?</li>", page)
    if not blocks:
        blocks = re.findall(r"(?is)<h2.*?</h2>(?:.*?<p>.*?</p>)?", page)
    for block in blocks:
        link_match = re.search(r"(?is)<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block)
        if not link_match:
            continue
        url = html.unescape(link_match.group(1))
        title = clean_text(link_match.group(2))
        snippet_match = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = clean_text(snippet_match.group(1) if snippet_match else "")
        domain = source_domain(url)
        flags: list[str] = []
        if not domain_allowed(domain, allowed_domains):
            flags.append("third_party_repost")
        hits.append(
            SearchHit(
                record_id=str(item.get("record_id") or item.get("source_document_id") or ""),
                source_document_id=str(item.get("source_document_id") or ""),
                source_file=str(item.get("source_file") or ""),
                project_name=clean_value(item.get("project_name") or Path(item.get("source_file", "")).stem),
                title=title,
                url=url,
                snippet=snippet,
                source_domain=domain,
                matched_query=query,
                risk_flags=flags,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def parse_google_hits(page: str, query: str, item: dict[str, Any], allowed_domains: list[str], limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?is)<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", page):
        url = normalize_result_url(match.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        domain = source_domain(url)
        if domain in {"google.com", "accounts.google.com", "support.google.com", "policies.google.com"}:
            continue
        title = clean_text(match.group(2))
        if not title or title in {"网页", "图片", "地图", "新闻", "视频"}:
            continue
        if url in seen:
            continue
        seen.add(url)
        flags: list[str] = []
        if not domain_allowed(domain, allowed_domains):
            flags.append("third_party_repost")
        hits.append(
            SearchHit(
                record_id=str(item.get("record_id") or item.get("source_document_id") or ""),
                source_document_id=str(item.get("source_document_id") or ""),
                source_file=str(item.get("source_file") or ""),
                project_name=clean_value(item.get("project_name") or Path(item.get("source_file", "")).stem),
                title=title,
                url=url,
                snippet="",
                source_domain=domain,
                matched_query=query,
                search_engine="google",
                risk_flags=flags,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def parse_generic_anchor_hits(
    page: str,
    query: str,
    item: dict[str, Any],
    allowed_domains: list[str],
    limit: int,
    engine: str,
    timeout: int,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    seen: set[str] = set()
    engine_domains = {"baidu.com", "sogou.com", "so.com"}
    for match in re.finditer(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page):
        raw_url = normalize_result_url(match.group(1))
        title = clean_text(match.group(2))
        if not raw_url or raw_url.startswith(("javascript:", "#", "/")):
            continue
        if not title or len(title) < 4:
            continue
        if title in {"首页", "图片", "视频", "新闻", "地图", "登录", "注册", "反馈", "设置", "高级搜索"}:
            continue
        if not raw_url.startswith(("http://", "https://")):
            continue
        url = resolve_search_redirect(raw_url, timeout)
        domain = source_domain(url)
        raw_domain = source_domain(raw_url)
        if raw_domain in engine_domains and domain == raw_domain:
            continue
        if engine == "so" and (
            domain.endswith("so.com")
            or domain.endswith("360.cn")
            or domain in {"baidu.com", "bing.com"}
        ):
            continue
        if engine == "sogou" and (domain.endswith("sogou.com") or domain.endswith("sogoucdn.com")):
            continue
        if engine == "baidu" and (domain.endswith("baidu.com") or domain.endswith("bdstatic.com")):
            continue
        if domain in {"miibeian.gov.cn", "beian.miit.gov.cn"}:
            continue
        if url in seen:
            continue
        seen.add(url)
        flags: list[str] = []
        if not domain_allowed(domain, allowed_domains):
            flags.append("third_party_repost")
        hits.append(
            SearchHit(
                record_id=str(item.get("record_id") or item.get("source_document_id") or ""),
                source_document_id=str(item.get("source_document_id") or ""),
                source_file=str(item.get("source_file") or ""),
                project_name=clean_value(item.get("project_name") or Path(item.get("source_file", "")).stem),
                title=title,
                url=url,
                snippet="",
                source_domain=domain,
                matched_query=query,
                search_engine=engine,
                risk_flags=flags,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def parse_search_hits(
    page: str,
    query: str,
    item: dict[str, Any],
    allowed_domains: list[str],
    limit: int,
    engine: str,
    timeout: int,
) -> list[SearchHit]:
    if engine == "google":
        return parse_google_hits(page, query, item, allowed_domains, limit)
    if engine in {"baidu", "sogou", "so"}:
        return parse_generic_anchor_hits(page, query, item, allowed_domains, limit, engine, timeout)
    return parse_bing_hits(page, query, item, allowed_domains, limit)


def search_page_blocked(page: str) -> bool:
    text = page.lower()
    return any(
        marker in text
        for marker in (
            "captcha",
            "unusual traffic",
            "blocked",
            "our systems have detected unusual traffic",
            "detected unusual traffic",
            "sorry",
            "验证",
            "请输入验证码",
        )
    )


def target_fields_for(item: dict[str, Any]) -> list[str]:
    missing = item.get("missing_fields") if isinstance(item.get("missing_fields"), list) else []
    fields = [field for field in TARGET_FIELDS if field in missing]
    return fields or list(TARGET_FIELDS)


def build_candidates_from_hit(hit: SearchHit, item: dict[str, Any], page_text: str, allowed_domains: list[str]) -> list[BrokerCandidate]:
    candidates: list[BrokerCandidate] = []
    match_strength = web_candidates.project_match_strength(f"{hit.title} {page_text}", hit.project_name)
    if match_strength == "none":
        return []
    for field in target_fields_for(item):
        for value, evidence in web_candidates.extract_field_candidates(page_text, field):
            snippet = web_candidates.snippet_around(page_text, value, (field, "中标", "成交", "预算", "开标"))
            flags = web_candidates.risk_flags_for(item, field, value, hit.title, page_text, match_strength, evidence)
            if not domain_allowed(hit.source_domain, allowed_domains):
                flags.append("third_party_repost")
            if "third_party_repost" not in flags and hit.risk_flags:
                flags.extend(flag for flag in hit.risk_flags if flag not in flags)
            confidence = web_candidates.confidence_for(match_strength, flags, "省市政府采购网", hit.title)
            if field in {"winner", "award_amount"} and "candidate_notice_only" in flags:
                confidence = "low"
            candidates.append(
                BrokerCandidate(
                    record_id=hit.record_id,
                    source_document_id=hit.source_document_id,
                    source_file=hit.source_file,
                    project_name=hit.project_name,
                    target_field=field,
                    candidate_value=value,
                    source_url=hit.url,
                    source_title=hit.title,
                    source_domain=hit.source_domain,
                    matched_query=hit.matched_query,
                    evidence_snippet=snippet,
                    confidence=confidence,
                    risk_flags=sorted(set(flags)),
                )
            )
    return candidates


def dedupe_hits(hits: list[SearchHit]) -> list[SearchHit]:
    deduped: list[SearchHit] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        key = (hit.record_id, hit.url, hit.matched_query)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def dedupe_candidates(candidates: list[BrokerCandidate]) -> list[BrokerCandidate]:
    deduped: list[BrokerCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        key = (candidate.record_id, candidate.target_field, candidate.candidate_value, candidate.source_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        deduped,
        key=lambda item: (rank.get(item.confidence, 9), len(item.risk_flags), item.record_id, item.target_field),
    )


def count_values(items: list[Any], getter) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = getter(item)
        if isinstance(value, list):
            values = value
        else:
            values = [value]
        for entry in values:
            if not entry:
                continue
            counts[str(entry)] = counts.get(str(entry), 0) + 1
    return dict(sorted(counts.items()))


def generate(
    review_queue_path: Path,
    final_records_path: Path,
    search_sources_path: Path,
    limit: int = 30,
    results_per_query: int = 3,
    max_queries: int = 80,
    timeout: int = 10,
    sleep: float = 0.2,
    search_engine: str = "google",
) -> dict[str, Any]:
    queue = read_json_list(review_queue_path)
    final_records = read_json_list(final_records_path)
    sources = sorted(
        [source for source in read_json_list(search_sources_path) if source.get("enabled", True)],
        key=lambda item: int(item.get("priority", 999)),
    )
    allowed_domains = [normalize_domain(source.get("domain", "")) for source in sources if source.get("domain")]
    record_by_id = {record.get("source_document_id"): record for record in final_records}
    targets = queue[:limit] if limit else queue

    hits: list[SearchHit] = []
    candidates: list[BrokerCandidate] = []
    errors: list[dict[str, str]] = []
    generated_queries = 0
    detail_pages_fetched = 0
    detail_parse_success = 0
    detail_parse_failed = 0
    search_blocked_count = 0
    search_no_hit_count = 0

    for item in targets:
        record = record_by_id.get(item.get("source_document_id"), {})
        merged = {**record, **item}
        queries = broker_queries_for_item(merged, sources)
        for query in queries:
            if generated_queries >= max_queries:
                break
            generated_queries += 1
            try:
                search_page = fetch_search_page(query, results_per_query, timeout, search_engine)
                if search_page_blocked(search_page):
                    search_blocked_count += 1
                query_hits = parse_search_hits(search_page, query, merged, allowed_domains, results_per_query, search_engine, timeout)
                if not query_hits:
                    search_no_hit_count += 1
                hits.extend(query_hits)
            except Exception as exc:
                errors.append({"query": query, "error": str(exc)[:240]})
                query_hits = []
            for hit in query_hits:
                if hit.risk_flags and "third_party_repost" in hit.risk_flags:
                    continue
                try:
                    page_text = clean_text(fetch(hit.url, timeout))
                    detail_pages_fetched += 1
                except Exception as exc:
                    errors.append({"query": query, "url": hit.url, "error": str(exc)[:240]})
                    continue
                hit_candidates = build_candidates_from_hit(hit, merged, page_text, allowed_domains)
                if hit_candidates:
                    detail_parse_success += 1
                    candidates.extend(hit_candidates)
                else:
                    detail_parse_failed += 1
            if sleep:
                time.sleep(sleep)
        if generated_queries >= max_queries:
            break

    hits = dedupe_hits(hits)
    candidates = dedupe_candidates(candidates)
    confirmed = [
        item
        for item in candidates
        if item.confidence == "high"
        and not item.risk_flags
        and not any(flag in item.risk_flags for flag in ("third_party_repost", "candidate_notice_only"))
    ]
    review = [item for item in candidates if item not in confirmed]
    summary = {
        "search_engine": search_engine,
        "checked_records": len(targets),
        "generated_queries": generated_queries,
        "search_hit_count": len(hits),
        "detail_pages_fetched": detail_pages_fetched,
        "detail_parse_success": detail_parse_success,
        "detail_parse_failed": detail_parse_failed,
        "candidate_count": len(candidates),
        "confirmed_suggestions_count": len(confirmed),
        "review_candidates_count": len(review),
        "risk_flag_counts": count_values(candidates, lambda item: item.risk_flags),
        "source_domain_counts": count_values(hits, lambda item: item.source_domain),
        "allowed_domains": allowed_domains,
        "search_blocked_count": search_blocked_count,
        "search_no_hit_count": search_no_hit_count,
        "error_count": len(errors),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_review_queue": str(review_queue_path),
        "search_sources": str(search_sources_path),
        "search_engine": search_engine,
        "summary": summary,
        "errors": errors[:100],
        "hits": [asdict(item) for item in hits],
        "confirmed_suggestions": [asdict(item) for item in confirmed],
        "review_candidates": [asdict(item) for item in review],
    }


def write_hits_markdown(payload: dict[str, Any], path: Path) -> None:
    rows = payload.get("hits", [])
    lines = [
        "# Search Broker Hits",
        "",
        f"- Generated At: {payload.get('generated_at', '')}",
        f"- Search Hit Count: {len(rows)}",
        "",
        "| 排名 | 文件 | 项目名称 | 标题 | 域名 | URL | Query | 风险 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(rows, 1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_escape(item.get("source_file")),
                    markdown_escape(item.get("project_name")),
                    markdown_escape(item.get("title")),
                    markdown_escape(item.get("source_domain")),
                    f"[link]({item.get('url')})",
                    markdown_escape(item.get("matched_query")),
                    markdown_escape(", ".join(item.get("risk_flags") or [])),
                ]
            )
            + " |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_candidates_markdown(payload: dict[str, Any], path: Path) -> None:
    rows = payload.get("confirmed_suggestions", []) + payload.get("review_candidates", [])
    lines = [
        "# Search Broker Candidates",
        "",
        f"- Generated At: {payload.get('generated_at', '')}",
        f"- Candidate Count: {len(rows)}",
        "",
        "| 排名 | 文件 | 项目名称 | 字段 | 候选值 | 来源 | 域名 | 置信度 | 风险 | 证据片段 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
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
                    markdown_escape(item.get("source_domain")),
                    markdown_escape(item.get("confidence")),
                    markdown_escape(", ".join(item.get("risk_flags") or [])),
                    markdown_escape(item.get("evidence_snippet")),
                ]
            )
            + " |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
